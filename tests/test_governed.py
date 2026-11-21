"""The governed module is the agent's only door to the ledger, and it is a narrow one."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bnpl.governed import FRESHNESS_MAX_AGE, GovernanceError, GovernedLedger
from tests.conftest import SEEDED_AT


def ledger_at(path: Path, now: datetime) -> GovernedLedger:
    return GovernedLedger(path, clock=lambda: now)


def test_reads_cases_through_the_governed_view(ledger_path: Path) -> None:
    with GovernedLedger(ledger_path) as ledger:
        cases = ledger.cases()
        first = ledger.case(cases[0].case_id)
    assert first == cases[0]
    assert first.merchant_name and first.sector and first.comms_history
    assert isinstance(first.risk_flags, tuple)


def test_unknown_case_returns_none(ledger_path: Path) -> None:
    with GovernedLedger(ledger_path) as ledger:
        assert ledger.case("case-9999") is None


def test_payments_and_merchants_are_readable(ledger_path: Path) -> None:
    with GovernedLedger(ledger_path) as ledger:
        case = ledger.cases()[0]
        payments = ledger.payments(case.loan_id)
        merchants = ledger.merchants()
    assert payments and all(p.loan_id == case.loan_id for p in payments)
    assert [m for m in merchants if m.under_review]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM loans",
        "SELECT COUNT(*) FROM cases",
        "SELECT v.id FROM v_loans v JOIN merchants m ON m.id = v.merchant_id",
    ],
)
def test_direct_reads_of_base_tables_are_denied(ledger_path: Path, sql: str) -> None:
    with GovernedLedger(ledger_path) as ledger, pytest.raises(GovernanceError):
        ledger._query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE merchants SET under_review = 0",
        "DELETE FROM decisions",
        "INSERT INTO loans (id) VALUES ('loan-x')",
        "DROP VIEW v_cases",
        "CREATE VIEW v_raw AS SELECT * FROM loans",
        "PRAGMA foreign_keys = OFF",
        "ATTACH DATABASE ':memory:' AS side",
    ],
)
def test_anything_but_an_append_is_denied(ledger_path: Path, sql: str) -> None:
    with GovernedLedger(ledger_path) as ledger, pytest.raises(GovernanceError):
        ledger._query(sql)


def test_decisions_are_appended_to_the_audit_log(ledger_path: Path) -> None:
    with ledger_at(ledger_path, SEEDED_AT) as ledger:
        decision_id = ledger.record_decision(
            case_id="case-0001",
            proposed_action="nudge",
            guardrail_verdict="ALLOW",
            final_action="nudge",
            actor="agent",
            reason="first missed instalment",
        )
        [decision] = ledger.decisions("case-0001")
    assert decision.id == decision_id
    assert decision.timestamp == SEEDED_AT.isoformat(timespec="seconds")


def test_exceptions_are_appended_to_the_inventory(ledger_path: Path) -> None:
    with GovernedLedger(ledger_path) as ledger:
        ledger.record_exception(kind="HOLD", reason_code="HIGH_RISK_ACTION", case_id="case-0002")
        [record] = ledger.exceptions()
    assert (record.kind, record.reason_code, record.case_id) == (
        "HOLD",
        "HIGH_RISK_ACTION",
        "case-0002",
    )


def test_audit_log_rejects_edits_even_with_raw_access(ledger_path: Path) -> None:
    with GovernedLedger(ledger_path) as ledger:
        ledger.record_decision(
            case_id="case-0001",
            proposed_action="nudge",
            guardrail_verdict="ALLOW",
            final_action="nudge",
            actor="agent",
            reason="seed",
        )
        ledger.record_exception(kind="HOLD", reason_code="HIGH_RISK_ACTION", case_id="case-0001")
    raw = sqlite3.connect(ledger_path)
    try:
        for sql in ("UPDATE decisions SET actor = 'someone'", "DELETE FROM exceptions"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(sql)
    finally:
        raw.close()


def test_ledger_is_fresh_within_24_hours(ledger_path: Path) -> None:
    with ledger_at(ledger_path, SEEDED_AT + timedelta(hours=23, minutes=59)) as ledger:
        check = ledger.freshness()
    assert check.fresh
    assert check.max_age == FRESHNESS_MAX_AGE


def test_ledger_is_stale_after_24_hours(ledger_path: Path) -> None:
    with ledger_at(ledger_path, SEEDED_AT + timedelta(hours=24, seconds=1)) as ledger:
        check = ledger.freshness()
    assert not check.fresh
    assert check.age > timedelta(hours=24)


def test_freshness_uses_the_ledger_stamp_not_file_times(tmp_path: Path) -> None:
    from bnpl.ledger.seed import seed_ledger

    old = datetime(2026, 1, 1, tzinfo=UTC)
    seed_ledger(tmp_path / "old.db", now=old)
    with ledger_at(tmp_path / "old.db", old + timedelta(days=3)) as ledger:
        assert not ledger.freshness().fresh


def test_missing_ledger_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="seed"):
        GovernedLedger(tmp_path / "absent.db")
