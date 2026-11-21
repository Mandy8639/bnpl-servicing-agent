"""Proposal parsing, cassette replay, and the live proposer's spend cap (with a fake client)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bnpl.governed import GovernedLedger
from bnpl.guardrail import PlanTerms
from bnpl.proposers import (
    INVALID_OUTPUT,
    Cassette,
    CassetteMissError,
    ClaudeProposer,
    RecordedProposer,
    SpendCapExceededError,
    SpendLedger,
    case_brief,
    parse_proposal,
)

DAY = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def good(action: str = "nudge", months: int = 0, fee: bool = False) -> dict[str, Any]:
    return {"action": action, "rationale": "r", "term_extension_months": months, "fee_waiver": fee}


# --- parse_proposal -----------------------------------------------------------------


def test_parses_a_plan_offer_with_its_terms() -> None:
    proposal = parse_proposal("case-0001", good("offer_plan", 2, True))
    assert proposal.plan == PlanTerms(term_extension_months=2, fee_waiver=True)


def test_ignores_plan_fields_for_other_actions() -> None:
    assert parse_proposal("case-0001", good("nudge", 9)).plan is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "nudge",
        {"rationale": "missing action"},
        {"action": "nudge"},
        {"action": "offer_plan", "rationale": "r", "term_extension_months": "two"},
        {"action": "offer_plan", "rationale": "r", "term_extension_months": True},
    ],
)
def test_malformed_output_becomes_an_invalid_action(raw: object) -> None:
    assert parse_proposal("case-0001", raw).action == INVALID_OUTPUT


def test_unknown_actions_pass_through_for_the_guardrail_to_block() -> None:
    assert parse_proposal("case-0001", good("waive_fees")).action == "waive_fees"


# --- cassettes ----------------------------------------------------------------------


def test_cassette_round_trips(tmp_path: Path) -> None:
    cassette = Cassette(source="test", responses={"case-0002": good(), "case-0001": good()})
    cassette.save(tmp_path / "c.json")
    loaded = Cassette.load(tmp_path / "c.json")
    assert loaded == cassette
    assert list(json.loads((tmp_path / "c.json").read_text())["responses"]) == [
        "case-0001",
        "case-0002",
    ]


def test_replay_misses_loudly_rather_than_inventing_a_response(ledger_path: Path) -> None:
    with GovernedLedger(ledger_path) as ledger:
        case = ledger.cases()[0]
        with pytest.raises(CassetteMissError):
            RecordedProposer(Cassette(source="empty")).propose(case, [])


# --- live proposer, with a fake client so tests never spend money ---------------------


class FakeMessages:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.text, self.stop_reason, self.calls = text, stop_reason, []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=1_000, output_tokens=200),
            content=[SimpleNamespace(type="text", text=self.text)],
        )


def live(tmp_path: Path, text: str, cap: float = 1.0, **kw: Any) -> tuple[ClaudeProposer, Any]:
    messages = FakeMessages(text, **kw)
    spend = SpendLedger(tmp_path / "spend.json", cap, clock=lambda: DAY)
    client: Any = SimpleNamespace(messages=messages)
    return ClaudeProposer(spend=spend, client=client, record_to=Cassette("rec")), messages


def test_live_call_is_priced_and_added_to_todays_spend(tmp_path: Path, ledger_path: Path) -> None:
    proposer, messages = live(tmp_path, json.dumps(good()))
    with GovernedLedger(ledger_path) as ledger:
        case = ledger.cases()[0]
        response = proposer.propose(case, ledger.payments(case.loan_id))
    assert response.cost_usd == pytest.approx((1_000 * 0.10 + 200 * 0.50) / 1_000_000)
    assert SpendLedger(tmp_path / "spend.json", 1.0, clock=lambda: DAY).spent_today() == (
        pytest.approx(response.cost_usd)
    )
    assert messages.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_live_responses_can_be_recorded_for_stub_replay(tmp_path: Path, ledger_path: Path) -> None:
    proposer, _ = live(tmp_path, json.dumps(good("no_action")))
    with GovernedLedger(ledger_path) as ledger:
        case = ledger.cases()[0]
        proposer.propose(case, [])
    assert proposer._record_to is not None
    assert proposer._record_to.responses[case.case_id]["action"] == "no_action"


def test_live_call_is_refused_before_it_would_break_the_cap(
    tmp_path: Path, ledger_path: Path
) -> None:
    proposer, messages = live(tmp_path, json.dumps(good()), cap=0.0)
    with GovernedLedger(ledger_path) as ledger, pytest.raises(SpendCapExceededError):
        proposer.propose(ledger.cases()[0], [])
    assert messages.calls == []


@pytest.mark.parametrize(("text", "stop"), [("not json", "end_turn"), ("{}", "refusal")])
def test_bad_live_output_becomes_an_invalid_action(
    tmp_path: Path, ledger_path: Path, text: str, stop: str
) -> None:
    proposer, _ = live(tmp_path, text, stop_reason=stop)
    with GovernedLedger(ledger_path) as ledger:
        response = proposer.propose(ledger.cases()[0], [])
    assert response.proposal.action == INVALID_OUTPUT


def test_models_without_a_price_are_refused(tmp_path: Path) -> None:
    spend = SpendLedger(tmp_path / "s.json", 1.0)
    with pytest.raises(ValueError, match="no price"):
        ClaudeProposer(spend=spend, model="claude-unknown", client=SimpleNamespace())  # type: ignore[arg-type]


def test_case_brief_carries_governed_facts_only(ledger_path: Path) -> None:
    with GovernedLedger(ledger_path) as ledger:
        case = ledger.cases()[0]
        brief = json.loads(case_brief(case, ledger.payments(case.loan_id)))
    assert brief["case_id"] == case.case_id
    assert "customer_ref" not in json.dumps(brief)
    assert brief["payments"] and brief["comms_history"]
