"""The agent loop end to end on a seeded ledger, with scripted or recorded proposals."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bnpl.agent import SAMPLE_CASSETTE, Alert, ServicingAgent, Status
from bnpl.controls import clear_pause, pause_state
from bnpl.governed import Case, GovernedLedger, Payment
from bnpl.guardrail import ReasonCode, Verdict
from bnpl.mandate import CONTROL_MIN_SAMPLE
from bnpl.proposers import (
    INVALID_OUTPUT,
    Cassette,
    ProposerResponse,
    RecordedProposer,
    parse_proposal,
)
from tests.conftest import SEEDED_AT

FRESH = SEEDED_AT + timedelta(hours=1)
STALE = SEEDED_AT + timedelta(hours=25)
UNDER_REVIEW_CASE = "case-0049"


def raw(action: str, months: int = 0, fee_waiver: bool = False) -> dict[str, Any]:
    return {
        "action": action,
        "rationale": f"scripted {action}",
        "term_extension_months": months,
        "fee_waiver": fee_waiver,
    }


class ScriptedProposer:
    """Returns the same raw response for every case unless told otherwise, and counts calls."""

    def __init__(self, default: dict[str, Any], **per_case: dict[str, Any]) -> None:
        self.default, self.per_case, self.calls = default, per_case, 0

    def propose(self, case: Case, payments: list[Payment]) -> ProposerResponse:
        self.calls += 1
        response = self.per_case.get(case.case_id.replace("-", "_"), self.default)
        return ProposerResponse(parse_proposal(case.case_id, response), response, 0.0, "scripted")


@pytest.fixture
def alerts() -> list[Alert]:
    return []


def agent_for(ledger_path: Path, proposer: Any, alerts: list[Alert], now=FRESH) -> ServicingAgent:
    ledger = GovernedLedger(ledger_path, clock=lambda: now)
    return ServicingAgent(ledger, proposer, clock=lambda: now, on_alert=alerts.append)


def ledger_at(ledger_path: Path) -> GovernedLedger:
    return GovernedLedger(ledger_path, clock=lambda: FRESH)


def reviewable_case_ids(ledger_path: Path, n: int) -> list[str]:
    with ledger_at(ledger_path) as ledger:
        return [c.case_id for c in ledger.cases() if not c.merchant_under_review][:n]


# --- The Phase 2 check: 5 sample cases in stub mode, $0 -----------------------------


def test_sample_cases_run_in_stub_mode_for_zero_dollars(ledger_path: Path, alerts) -> None:
    cassette = Cassette.load(SAMPLE_CASSETTE)
    agent = agent_for(ledger_path, RecordedProposer(cassette), alerts)
    summary = agent.run_queue(sorted(cassette.responses))

    assert len(summary.outcomes) == 5
    assert summary.cost_usd == 0.0
    assert {o.case_id: (o.verdict, o.proposed_action) for o in summary.outcomes} == {
        "case-0004": (Verdict.HOLD, "escalate_collections"),
        "case-0022": (Verdict.HOLD, "hold_for_review"),
        "case-0028": (Verdict.ALLOW, "no_action"),
        "case-0035": (Verdict.ALLOW, "offer_plan"),
        "case-0093": (Verdict.ALLOW, "nudge"),
    }
    assert all(o.source and o.source.startswith("stub:") for o in summary.outcomes)
    assert not alerts


def test_every_case_leaves_one_audit_entry(ledger_path: Path, alerts) -> None:
    cassette = Cassette.load(SAMPLE_CASSETTE)
    agent_for(ledger_path, RecordedProposer(cassette), alerts).run_queue(sorted(cassette.responses))
    with ledger_at(ledger_path) as ledger:
        decisions = ledger.decisions()
    assert sorted(d.case_id for d in decisions) == sorted(cassette.responses)
    assert all(d.actor == "agent" for d in decisions)


# --- What gets executed, held and blocked -------------------------------------------


def test_allowed_action_is_recorded_as_executed(ledger_path: Path, alerts) -> None:
    [case_id] = reviewable_case_ids(ledger_path, 1)
    outcome = agent_for(ledger_path, ScriptedProposer(raw("nudge")), alerts).run_case(case_id)
    assert (outcome.status, outcome.final_action) == (Status.EXECUTED, "nudge")
    with ledger_at(ledger_path) as ledger:
        [decision] = ledger.decisions(case_id)
    assert decision.final_action == "nudge" and decision.guardrail_verdict == "ALLOW"


def test_held_case_is_never_executed_and_names_owner_and_deadline(
    ledger_path: Path, alerts
) -> None:
    [case_id] = reviewable_case_ids(ledger_path, 1)
    proposer = ScriptedProposer(raw("escalate_collections"))
    outcome = agent_for(ledger_path, proposer, alerts).run_case(case_id)
    assert (outcome.status, outcome.final_action) == (Status.HELD, None)
    with ledger_at(ledger_path) as ledger:
        [decision] = ledger.decisions(case_id)
        [record] = ledger.exceptions()
    assert decision.final_action is None
    detail = json.loads(record.detail)
    assert (record.kind, record.reason_code) == ("HOLD", ReasonCode.HIGH_RISK_ACTION)
    assert detail["escalation_owner"] == "servicing lead"
    assert detail["decide_by"] == (FRESH + timedelta(hours=24)).isoformat()


def test_out_of_mandate_proposal_is_blocked_and_inventoried(ledger_path: Path, alerts) -> None:
    [case_id] = reviewable_case_ids(ledger_path, 1)
    outcome = agent_for(ledger_path, ScriptedProposer(raw("waive_fees")), alerts).run_case(case_id)
    assert (outcome.status, outcome.reason_code) == (
        Status.BLOCKED,
        ReasonCode.OUT_OF_MANDATE_ACTION,
    )
    with ledger_at(ledger_path) as ledger:
        assert [e.kind for e in ledger.exceptions()] == ["BLOCK"]


def test_unparseable_model_output_is_blocked(ledger_path: Path, alerts) -> None:
    [case_id] = reviewable_case_ids(ledger_path, 1)
    proposer = ScriptedProposer({"error": "stop_reason=max_tokens"})
    outcome = agent_for(ledger_path, proposer, alerts).run_case(case_id)
    assert (outcome.status, outcome.proposed_action) == (Status.BLOCKED, INVALID_OUTPUT)


def test_second_nudge_to_a_customer_on_the_same_day_holds(ledger_path: Path, alerts) -> None:
    [case_id] = reviewable_case_ids(ledger_path, 1)
    agent = agent_for(ledger_path, ScriptedProposer(raw("nudge")), alerts)
    assert agent.run_case(case_id).status is Status.EXECUTED
    second = agent.run_case(case_id)
    assert (second.status, second.reason_code) == (Status.HELD, ReasonCode.NUDGE_CAP_REACHED)


# --- Pause conditions, end to end ---------------------------------------------------


def test_stale_data_pauses_the_agent_and_alerts(ledger_path: Path, alerts) -> None:
    case_ids = reviewable_case_ids(ledger_path, 3)
    proposer = ScriptedProposer(raw("nudge"))
    summary = agent_for(ledger_path, proposer, alerts, now=STALE).run_queue(case_ids)

    assert [o.status for o in summary.outcomes] == [Status.HELD, Status.SKIPPED_PAUSED]
    assert summary.outcomes[0].reason_code is ReasonCode.DATA_STALE
    assert [a.reason_code for a in alerts] == [ReasonCode.DATA_STALE]
    assert proposer.calls == 1


def test_a_paused_agent_spends_nothing(ledger_path: Path, alerts) -> None:
    case_ids = reviewable_case_ids(ledger_path, 5)
    proposer = ScriptedProposer(raw("waive_fees"))
    summary = agent_for(ledger_path, proposer, alerts).run_queue(case_ids)

    assert [o.status for o in summary.outcomes] == [
        Status.BLOCKED,
        Status.BLOCKED,
        Status.BLOCKED,
        Status.SKIPPED_PAUSED,
    ]
    assert proposer.calls == 3
    assert [a.reason_code for a in alerts] == [ReasonCode.REPEATED_BLOCKS]
    with ledger_at(ledger_path) as ledger:
        state = pause_state(ledger.exceptions())
    assert state.paused and state.reason_code == ReasonCode.REPEATED_BLOCKS


def test_auto_action_rate_breach_pauses_the_agent(ledger_path: Path, alerts) -> None:
    case_ids = reviewable_case_ids(ledger_path, CONTROL_MIN_SAMPLE + 2)
    proposer = ScriptedProposer(raw("nudge"))
    summary = agent_for(ledger_path, proposer, alerts).run_queue(case_ids)

    statuses = [o.status for o in summary.outcomes]
    assert statuses == [Status.EXECUTED] * CONTROL_MIN_SAMPLE + [Status.HELD, Status.SKIPPED_PAUSED]
    assert summary.outcomes[CONTROL_MIN_SAMPLE].reason_code is ReasonCode.AUTO_RATE_BREACH


def test_merchant_under_review_holds_its_cases_without_pausing(ledger_path: Path, alerts) -> None:
    other = reviewable_case_ids(ledger_path, 1)
    proposer = ScriptedProposer(raw("nudge"))
    summary = agent_for(ledger_path, proposer, alerts).run_queue([UNDER_REVIEW_CASE, *other])

    held, next_case = summary.outcomes
    assert (held.status, held.reason_code) == (Status.HELD, ReasonCode.MERCHANT_UNDER_REVIEW)
    assert next_case.status is Status.EXECUTED
    assert [a.scope for a in alerts] == ["merchant"]


def test_a_human_clearing_the_pause_resumes_with_a_fresh_window(ledger_path: Path, alerts) -> None:
    case_ids = reviewable_case_ids(ledger_path, CONTROL_MIN_SAMPLE + 3)
    agent = agent_for(ledger_path, ScriptedProposer(raw("nudge")), alerts)
    agent.run_queue(case_ids[: CONTROL_MIN_SAMPLE + 1])
    with ledger_at(ledger_path) as ledger:
        assert pause_state(ledger.exceptions()).paused
        clear_pause(ledger, actor="Dana (servicing lead)", reason="Reviewed the nudge run.")
        assert not pause_state(ledger.exceptions()).paused

    outcome = agent.run_case(case_ids[-1])
    assert outcome.status is Status.EXECUTED


def test_clearing_a_pause_needs_a_named_human_and_reason(ledger_path: Path) -> None:
    with ledger_at(ledger_path) as ledger, pytest.raises(ValueError):
        clear_pause(ledger, actor=" ", reason="ok")


def test_unknown_case_is_an_error(ledger_path: Path, alerts) -> None:
    with pytest.raises(LookupError):
        agent_for(ledger_path, ScriptedProposer(raw("nudge")), alerts).run_case("case-9999")


def test_a_queue_run_never_reworks_a_decided_case(ledger_path: Path, alerts) -> None:
    from bnpl.agent import open_case_ids

    case_ids = reviewable_case_ids(ledger_path, 3)
    agent_for(ledger_path, ScriptedProposer(raw("nudge")), alerts).run_case(case_ids[0])
    with ledger_at(ledger_path) as ledger:
        assert open_case_ids(ledger, case_ids) == case_ids[1:]
