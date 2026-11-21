"""The merchant-report agent: its policy, its guardrail rule by rule, its human gate, its eval."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bnpl.cli import main as cli
from bnpl.governed import GovernedLedger
from bnpl.guardrail import Verdict
from bnpl.mandate import RiskLevel
from bnpl.merchant_report import (
    MerchantMetrics,
    ReportContext,
    ReportDraft,
    ReportReason,
    decide_report,
    evaluate_report,
    held_reports,
    merchant_metrics,
    risk_band,
    run_report,
)
from bnpl.proposers import Cassette
from evals.merchant_fixtures import build_merchant_ledger
from evals.run_merchant_evals import (
    CASSETTE_DIR,
    GOLDEN_PATH,
    LOCK_PATH,
    RecordedReportWriter,
    run_golden_set,
    verify_frozen,
)

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
PERIOD = "2026-11-16"
GOLDEN = json.loads(GOLDEN_PATH.read_text())["cases"]

HEALTHY = MerchantMetrics(
    merchant_id="mer-x",
    merchant_name="Linden Pet Clinic",
    sector="Medical",
    under_review=False,
    loans_total=40,
    loans_overdue=1,
    loans_charged_off=0,
    loans_in_plan=1,
    amount_overdue=210.4,
    open_disputes=0,
    fraud_flags=0,
    exceptions_held=1,
    exceptions_blocked=0,
)
CTX = ReportContext(HEALTHY, PERIOD, data_fresh=True, now=NOW)


def draft(**changes: Any) -> ReportDraft:
    base = ReportDraft(
        headline="Linden Pet Clinic is healthy, with delinquency at 2.5%.",
        findings=("1 of 40 loans is overdue, $210.40 in total.", "No open disputes."),
        risk_level="low",
        recommended_action="no_change",
    )
    return replace(base, **changes)


def check(d: ReportDraft, **ctx_changes: Any):
    return evaluate_report(d, replace(CTX, **ctx_changes))


# --- the policy ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, ("low", "no_change")),
        ({"loans_overdue": 3}, ("medium", "monitor")),
        ({"loans_in_plan": 11}, ("medium", "monitor")),
        ({"open_disputes": 2}, ("medium", "review_with_merchant")),
        ({"loans_overdue": 6, "loans_charged_off": 2}, ("high", "escalate_to_risk")),
        ({"fraud_flags": 2}, ("high", "escalate_to_risk")),
    ],
)
def test_risk_band_follows_the_published_thresholds(changes: dict, expected: tuple) -> None:
    risk, action = risk_band(replace(HEALTHY, **changes))
    assert (risk.value, action.value) == expected


# --- the report guardrail, rule by rule ----------------------------------------------


def test_a_grounded_low_risk_report_publishes() -> None:
    result = check(draft())
    assert (result.verdict, result.reason_code) == (Verdict.ALLOW, ReportReason.PUBLISHED)


def test_an_invented_number_is_blocked() -> None:
    result = check(draft(headline="Delinquency fell to 1.8% this week."))
    assert (result.verdict, result.reason_code) == (Verdict.BLOCK, ReportReason.UNSUPPORTED_NUMBER)
    assert "1.8" in result.detail


def test_a_computed_number_is_blocked_too() -> None:
    result = check(draft(findings=("39 of 40 loans are current.",)))
    assert result.reason_code == ReportReason.UNSUPPORTED_NUMBER


def test_published_policy_thresholds_count_as_grounded() -> None:
    result = check(draft(findings=("Delinquency of 2.5% is below the 5% watch line.",)))
    assert result.verdict is Verdict.ALLOW


def test_the_period_end_date_may_be_stated() -> None:
    assert check(draft(headline=f"Week ending {PERIOD}: delinquency 2.5%.")).verdict is (
        Verdict.ALLOW
    )


def test_a_report_naming_a_customer_is_blocked() -> None:
    result = check(draft(findings=("Customer cust-0412 is overdue.",)))
    assert (result.verdict, result.reason_code) == (Verdict.BLOCK, ReportReason.CUSTOMER_DATA)


@pytest.mark.parametrize(
    "changes", [{"risk_level": "severe"}, {"recommended_action": "suspend_merchant"}]
)
def test_out_of_mandate_risk_or_action_is_blocked(changes: dict) -> None:
    assert check(draft(**changes)).reason_code == ReportReason.OUT_OF_MANDATE


def test_an_empty_report_is_blocked() -> None:
    assert check(draft(findings=())).reason_code == ReportReason.MISSING_FINDINGS


def test_stale_data_holds_the_report() -> None:
    result = check(draft(), data_fresh=False)
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReportReason.DATA_STALE)
    assert result.owner == "portfolio risk lead"
    assert result.decide_by == (NOW + timedelta(hours=24)).isoformat(timespec="seconds")


def test_a_merchant_under_review_holds_the_report() -> None:
    result = check(draft(), metrics=replace(HEALTHY, under_review=True))
    assert result.reason_code == ReportReason.MERCHANT_UNDER_REVIEW


def test_understating_the_risk_the_metrics_show_holds_the_report() -> None:
    risky = replace(HEALTHY, loans_in_plan=12)
    result = check(draft(findings=("12 loans are on revised plans.",)), metrics=risky)
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReportReason.RISK_UNDERSTATED)


def test_overstating_risk_is_allowed_through_to_its_own_rule() -> None:
    result = check(draft(risk_level=RiskLevel.MEDIUM, recommended_action="monitor"))
    assert result.verdict is Verdict.ALLOW


def test_an_escalation_always_holds() -> None:
    result = check(draft(risk_level="high", recommended_action="escalate_to_risk"))
    assert result.reason_code == ReportReason.RISK_ESCALATION


def test_a_merchant_conversation_always_holds() -> None:
    result = check(draft(risk_level="medium", recommended_action="review_with_merchant"))
    assert result.reason_code == ReportReason.EXTERNAL_REVIEW


# --- the agent on a governed ledger, and the human gate --------------------------------


class Fixed:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    def draft(self, key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        return self.raw, 0.0


@pytest.fixture
def review_ledger(tmp_path: Path) -> tuple[Path, str]:
    golden = next(c for c in GOLDEN if c["id"] == "r05")
    path = tmp_path / "m.db"
    return path, build_merchant_ledger(path, golden, NOW)


def escalating_report() -> dict[str, Any]:
    return {
        "headline": "Mesa Roofing Co. has 3 open disputes.",
        "findings": ["Delinquency is 5.0%."],
        "risk_level": "medium",
        "recommended_action": "review_with_merchant",
    }


def test_metrics_come_from_the_governed_view(review_ledger) -> None:
    path, merchant_id = review_ledger
    with GovernedLedger(path, clock=lambda: NOW) as ledger:
        m = merchant_metrics(ledger, merchant_id)
    assert m is not None and m.open_disputes == 3 and m.loans_total == 40


def test_a_held_report_waits_for_a_named_human(review_ledger) -> None:
    path, merchant_id = review_ledger
    with GovernedLedger(path, clock=lambda: NOW) as ledger:
        outcome = run_report(ledger, merchant_id, Fixed(escalating_report()), clock=lambda: NOW)
        assert outcome.status == "held"
        assert [r["id"] for r in held_reports(ledger)] == [outcome.report_id]
        assert [e.kind for e in ledger.exceptions()][-1] == "HOLD"
        with pytest.raises(ValueError):
            decide_report(ledger, outcome.report_id, approve=True, actor="agent", reason="ok")
        decide_report(
            ledger, outcome.report_id, approve=True, actor="Priya Shah", reason="Call booked."
        )
        assert held_reports(ledger) == []
        latest = ledger.merchant_reports()[-1]
    assert (latest["status"], latest["actor"], latest["supersedes"]) == (
        "approved",
        "Priya Shah",
        outcome.report_id,
    )


def test_reports_are_append_only(review_ledger) -> None:
    import sqlite3

    path, merchant_id = review_ledger
    with GovernedLedger(path, clock=lambda: NOW) as ledger:
        run_report(ledger, merchant_id, Fixed(escalating_report()), clock=lambda: NOW)
    raw = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE merchant_reports SET status = 'published'")
    finally:
        raw.close()


def test_cli_lists_and_approves_held_reports(review_ledger, capsys) -> None:
    path, merchant_id = review_ledger
    with GovernedLedger(path, clock=lambda: NOW) as ledger:
        outcome = run_report(ledger, merchant_id, Fixed(escalating_report()), clock=lambda: NOW)
    db = ["--db", str(path)]
    cli([*db, "report", "list"])
    assert f"report {outcome.report_id}" in capsys.readouterr().out
    rid = str(outcome.report_id)
    assert cli([*db, "report", "approve", rid, "--actor", "Priya", "--reason", "ok"]) == 0
    cli([*db, "report", "list"])
    assert "No merchant reports waiting" in capsys.readouterr().out


# --- the frozen golden set and its eval --------------------------------------------


def test_merchant_golden_set_is_frozen() -> None:
    assert verify_frozen() == LOCK_PATH.read_text().strip()


def test_golden_expectations_agree_with_the_policy(tmp_path: Path) -> None:
    for golden in GOLDEN:
        path = tmp_path / f"{golden['id']}.db"
        merchant_id = build_merchant_ledger(path, golden, NOW)
        with GovernedLedger(path, clock=lambda: NOW) as ledger:
            risk, action = risk_band(merchant_metrics(ledger, merchant_id))
        assert (risk.value, action.value) == (
            golden["expected_risk"],
            golden["expected_action"],
        ), golden["id"]


def test_the_committed_recording_replays_for_zero_dollars_and_passes_the_gate() -> None:
    cassette = Cassette.load(CASSETTE_DIR / "merchant-claude-haiku-5-5.json")
    report = run_golden_set(
        RecordedReportWriter(cassette), mode="stub", model="claude-haiku-5-5", source="stub"
    )
    m = report["metrics"]
    assert m["llm_cost_usd"] == 0.0
    assert m["false_publish_count"] == 0
    assert m["control_detection_rate"] == 1.0
