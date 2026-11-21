"""Review queue, detective controls, the CLI and the API: the Phase 4 checks, end to end."""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bnpl.agent import SAMPLE_CASSETTE, ServicingAgent, Status
from bnpl.api import app
from bnpl.cli import main as cli
from bnpl.controls import build_digest, clear_pause, pause_state, run_controls
from bnpl.governed import GovernedLedger
from bnpl.guardrail import ReasonCode
from bnpl.ledger.seed import seed_ledger
from bnpl.mandate import CONTROL_MIN_SAMPLE
from bnpl.proposers import Cassette, RecordedProposer
from bnpl.review import ReviewError, approve, held_cases, reject
from tests.conftest import SEEDED_AT

NOW = SEEDED_AT + timedelta(hours=1)
HELD_ESCALATION = "case-0004"
HELD_ABSTENTION = "case-0022"


def open_ledger(path: Path) -> GovernedLedger:
    return GovernedLedger(path, clock=lambda: NOW)


@pytest.fixture
def worked_ledger(ledger_path: Path) -> Path:
    """The seeded ledger after the agent has worked the five sample cases."""
    cassette = Cassette.load(SAMPLE_CASSETTE)
    with open_ledger(ledger_path) as ledger:
        agent = ServicingAgent(ledger, RecordedProposer(cassette), clock=lambda: NOW)
        agent.run_queue(sorted(cassette.responses))
    return ledger_path


def force_auto_rate_breach(ledger: GovernedLedger) -> None:
    for n in range(CONTROL_MIN_SAMPLE + 5):
        ledger.record_decision(
            case_id=f"case-x{n:03d}",
            proposed_action="nudge",
            guardrail_verdict="ALLOW",
            final_action="nudge",
            actor="agent",
            reason="forced for the breach test",
        )


# --- review queue -------------------------------------------------------------------


def test_queue_lists_held_cases_with_owner_and_deadline(worked_ledger: Path) -> None:
    with open_ledger(worked_ledger) as ledger:
        queue = {h.case_id: h for h in held_cases(ledger)}
    assert set(queue) == {HELD_ESCALATION, HELD_ABSTENTION}
    held = queue[HELD_ESCALATION]
    assert held.proposed_action == "escalate_collections"
    assert held.reason_code == ReasonCode.HIGH_RISK_ACTION
    assert held.escalation_owner == "servicing lead"
    assert held.decide_by == (NOW + timedelta(hours=24)).isoformat()


def test_approving_a_held_case_executes_it_under_the_humans_name(worked_ledger: Path) -> None:
    with open_ledger(worked_ledger) as ledger:
        decision = approve(
            ledger, HELD_ESCALATION, actor="Dana Reyes", reason="Charged off; hand to collections."
        )
        remaining = [h.case_id for h in held_cases(ledger)]
    assert (decision.actor, decision.final_action) == ("Dana Reyes", "escalate_collections")
    assert decision.reason.startswith("APPROVED: ")
    assert HELD_ESCALATION not in remaining


def test_rejecting_a_held_case_executes_nothing(worked_ledger: Path) -> None:
    with open_ledger(worked_ledger) as ledger:
        decision = reject(ledger, HELD_ESCALATION, actor="Dana Reyes", reason="Call first.")
    assert decision.final_action is None and decision.reason.startswith("REJECTED: ")


def test_an_abstention_needs_the_human_to_choose_the_action(worked_ledger: Path) -> None:
    with open_ledger(worked_ledger) as ledger:
        with pytest.raises(ReviewError, match="--action"):
            approve(ledger, HELD_ABSTENTION, actor="Dana Reyes", reason="ok")
        decision = approve(
            ledger,
            HELD_ABSTENTION,
            actor="Dana Reyes",
            reason="Merchant owes a fix.",
            action="no_action",
        )
    assert decision.final_action == "no_action"


@pytest.mark.parametrize(("actor", "reason"), [("", "why"), ("agent", "why"), ("Dana", " ")])
def test_reviews_need_a_named_human_and_a_reason(
    worked_ledger: Path, actor: str, reason: str
) -> None:
    with open_ledger(worked_ledger) as ledger, pytest.raises(ReviewError):
        approve(ledger, HELD_ESCALATION, actor=actor, reason=reason)


def test_cases_not_in_the_queue_cannot_be_reviewed(worked_ledger: Path) -> None:
    with open_ledger(worked_ledger) as ledger, pytest.raises(ReviewError, match="not waiting"):
        approve(ledger, "case-0093", actor="Dana Reyes", reason="already executed")


def test_out_of_mandate_actions_cannot_be_approved(worked_ledger: Path) -> None:
    with open_ledger(worked_ledger) as ledger, pytest.raises(ReviewError, match="action set"):
        approve(ledger, HELD_ESCALATION, actor="Dana", reason="x", action="waive_fees")


# --- detective controls -------------------------------------------------------------


def test_digest_reports_counts_rates_and_inventory(worked_ledger: Path) -> None:
    with open_ledger(worked_ledger) as ledger:
        digest = build_digest(ledger)
    assert digest.cases_processed == 5
    assert digest.verdicts == {"HOLD": 2, "ALLOW": 3}
    assert digest.hold_rate == 0.4
    assert digest.open_reviews == 2
    assert digest.exceptions_by_kind == {"HOLD": 2}
    assert not digest.paused and not digest.breaches


def test_a_forced_breach_alerts_inventories_and_pauses_the_agent(ledger_path: Path) -> None:
    alerts = []
    with open_ledger(ledger_path) as ledger:
        force_auto_rate_breach(ledger)
        digest = run_controls(ledger, on_alert=alerts.append)
        kinds = [e.kind for e in ledger.exceptions()]
    assert [a.reason_code for a in alerts] == [ReasonCode.AUTO_RATE_BREACH]
    assert kinds == ["BREACH", "PAUSE"]
    assert digest.paused and digest.pause_reason == ReasonCode.AUTO_RATE_BREACH


def test_a_paused_agent_works_no_cases_until_a_human_clears_it(ledger_path: Path) -> None:
    cassette = Cassette.load(SAMPLE_CASSETTE)
    with open_ledger(ledger_path) as ledger:
        force_auto_rate_breach(ledger)
        run_controls(ledger)
        agent = ServicingAgent(ledger, RecordedProposer(cassette), clock=lambda: NOW)
        assert agent.run_case("case-0093").status is Status.SKIPPED_PAUSED

        clear_pause(ledger, actor="Dana Reyes", reason="Forced test breach; reviewed.")
        assert not pause_state(ledger.exceptions()).paused
        assert not run_controls(ledger).breaches
        assert agent.run_case("case-0093").status is Status.EXECUTED


def test_a_breach_while_already_paused_is_inventoried_without_a_second_pause(
    ledger_path: Path,
) -> None:
    with open_ledger(ledger_path) as ledger:
        force_auto_rate_breach(ledger)
        run_controls(ledger)
        run_controls(ledger)
        kinds = [e.kind for e in ledger.exceptions()]
    assert kinds == ["BREACH", "PAUSE", "BREACH"]


# --- CLI ----------------------------------------------------------------------------


def test_cli_approves_a_held_case_end_to_end(worked_ledger: Path, capsys) -> None:
    db = ["--db", str(worked_ledger)]
    assert cli([*db, "review", "list"]) == 0
    assert HELD_ESCALATION in capsys.readouterr().out
    code = cli(
        [
            *db,
            "review",
            "approve",
            HELD_ESCALATION,
            "--actor",
            "Dana Reyes",
            "--reason",
            "Charged off 141 days; collections.",
        ]
    )
    assert code == 0
    assert "executed escalate_collections" in capsys.readouterr().out
    cli([*db, "review", "list"])
    assert HELD_ESCALATION not in capsys.readouterr().out


def test_cli_controls_digest_pauses_on_breach_and_clear_pause_resumes(
    tmp_path: Path, capsys
) -> None:
    fresh = tmp_path / "fresh.db"
    seed_ledger(fresh)  # stamped now: the CLI uses the real clock for freshness
    with GovernedLedger(fresh) as ledger:
        force_auto_rate_breach(ledger)
    db = ["--db", str(fresh)]
    cli([*db, "controls", "digest"])
    out = capsys.readouterr().out
    assert "ALERT AUTO_RATE_BREACH" in out and "PAUSED" in out
    assert "DATA_STALE" not in out
    assert cli([*db, "controls", "clear-pause", "--actor", "Dana", "--reason", "ok"]) == 0
    cli([*db, "controls", "digest"])
    out = capsys.readouterr().out
    assert "Agent: running" in out and "ALERT" not in out


def test_cli_refuses_a_review_without_a_reason(worked_ledger: Path) -> None:
    code = cli(
        [
            "--db",
            str(worked_ledger),
            "review",
            "reject",
            HELD_ESCALATION,
            "--actor",
            "Dana",
            "--reason",
            " ",
        ]
    )
    assert code == 1


# --- API ----------------------------------------------------------------------------


@pytest.fixture
def client(worked_ledger: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BNPL_DB_PATH", str(worked_ledger))
    return TestClient(app)


def test_api_lists_cases_with_their_status(client: TestClient) -> None:
    cases = {c["case_id"]: c for c in client.get("/cases").json()}
    assert cases[HELD_ESCALATION]["status"] == "held"
    assert cases["case-0093"]["status"] == "executed"
    assert cases["case-0001"]["status"] == "open"
    assert [c["case_id"] for c in client.get("/cases", params={"status": "held"}).json()] == [
        HELD_ESCALATION,
        HELD_ABSTENTION,
    ]


def test_api_case_detail_includes_payments_trail_and_review(client: TestClient) -> None:
    body = client.get(f"/cases/{HELD_ESCALATION}").json()
    assert body["payments"] and body["decisions"]
    assert body["review"]["escalation_owner"] == "servicing lead"
    assert client.get("/cases/case-9999").status_code == 404


def test_api_review_requires_a_human_actor(client: TestClient) -> None:
    url = f"/cases/{HELD_ESCALATION}/review"
    assert client.post(url, json={"decision": "approve", "reason": "x"}).status_code == 422
    assert (
        client.post(url, json={"decision": "approve", "actor": "", "reason": "x"}).status_code
        == 422
    )
    assert (
        client.post(url, json={"decision": "approve", "actor": "agent", "reason": "x"}).status_code
        == 400
    )


def test_api_approves_a_held_case_end_to_end(client: TestClient) -> None:
    response = client.post(
        f"/cases/{HELD_ESCALATION}/review",
        json={"decision": "approve", "actor": "Dana Reyes", "reason": "Collections."},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert HELD_ESCALATION not in [r["case_id"] for r in client.get("/reviews").json()]


def test_api_digest_is_read_only(client: TestClient, worked_ledger: Path) -> None:
    with open_ledger(worked_ledger) as ledger:
        force_auto_rate_breach(ledger)
        before = len(ledger.exceptions())
    digest = client.get("/controls/digest").json()
    assert digest["breaches"] and not digest["paused"]
    with open_ledger(worked_ledger) as ledger:
        assert len(ledger.exceptions()) == before


def test_api_serves_the_latest_committed_stub_report(client: TestClient) -> None:
    body = client.get("/evals/latest").json()
    assert body["report"]["mode"] == "stub"
    assert body["report"]["metrics"]["false_auto_count"] == 0
    assert {h["file"] for h in body["history"]} >= {
        "2026-11-21-live.json",
        "2026-11-21-live-2.json",
        "2026-11-21-stub.json",
    }


def test_api_exposes_the_mandate(client: TestClient) -> None:
    body = client.get("/mandate").json()
    assert [a["action"] for a in body["actions"]] == [
        "nudge",
        "offer_plan",
        "escalate_collections",
        "hold_for_review",
        "no_action",
    ]
    assert body["thresholds"]["max_auto_action_rate"] == 0.8
