"""The synthetic ledger is deterministic, coherent and plausibly shaped."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from bnpl.ledger.catalog import MERCHANTS
from bnpl.ledger.seed import DEFAULT_AS_OF, seed_ledger
from tests.conftest import SEEDED_AT

TABLES = ("merchants", "loans", "payments", "cases")


def dump(path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}
    finally:
        conn.close()


def query(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_reseeding_reproduces_identical_row_counts(tmp_path: Path) -> None:
    first = seed_ledger(tmp_path / "a.db")
    second = seed_ledger(tmp_path / "a.db", reset=True)
    assert first.row_counts == second.row_counts


def test_same_seed_produces_identical_rows(tmp_path: Path) -> None:
    seed_ledger(tmp_path / "a.db", now=SEEDED_AT)
    seed_ledger(tmp_path / "b.db", now=SEEDED_AT)
    assert dump(tmp_path / "a.db") == dump(tmp_path / "b.db")


def test_different_seed_produces_a_different_ledger(tmp_path: Path) -> None:
    seed_ledger(tmp_path / "a.db", seed=42)
    seed_ledger(tmp_path / "b.db", seed=7)
    assert dump(tmp_path / "a.db")["loans"] != dump(tmp_path / "b.db")["loans"]


def test_existing_ledger_is_never_overwritten_by_accident(ledger_path: Path) -> None:
    with pytest.raises(FileExistsError):
        seed_ledger(ledger_path)


def test_ledger_has_the_specified_shape(ledger_path: Path) -> None:
    counts = seed_ledger(ledger_path, reset=True).row_counts
    assert counts["merchants"] == 12
    assert counts["loans"] == 240
    assert 550 <= counts["payments"] <= 700
    assert 60 <= counts["cases"] <= 120
    assert counts["decisions"] == counts["exceptions"] == 0


def test_every_sector_is_represented_and_one_merchant_is_under_review(ledger_path: Path) -> None:
    rows = query(ledger_path, "SELECT sector, under_review FROM merchants")
    assert {sector for sector, _ in rows} == {"Travel", "Medical", "Retail", "Home Improvement"}
    assert sum(flag for _, flag in rows) == 1
    assert len(rows) == len(MERCHANTS)


def test_loan_amounts_and_terms_stay_inside_product_limits(ledger_path: Path) -> None:
    (low, high, short, long_) = query(
        ledger_path,
        "SELECT MIN(principal), MAX(principal), MIN(term_months), MAX(term_months) FROM loans",
    )[0]
    assert low >= 50 and high <= 50_000
    assert short >= 1.5 and long_ <= 84


def test_case_overdue_amounts_match_missed_payments(ledger_path: Path) -> None:
    rows = query(
        ledger_path,
        "SELECT c.id, c.amount_overdue, COALESCE(SUM(p.amount), 0) FROM cases c "
        "LEFT JOIN payments p ON p.loan_id = c.loan_id AND p.status = 'missed' "
        "WHERE c.type IN ('missed_payment', 'dispute') AND c.amount_overdue > 0 "
        "GROUP BY c.id",
    )
    assert rows
    for case_id, overdue, missed_total in rows:
        assert overdue == pytest.approx(missed_total, abs=0.01), case_id


def test_dates_never_run_past_the_as_of_date(ledger_path: Path) -> None:
    as_of = DEFAULT_AS_OF.isoformat()
    assert not query(ledger_path, f"SELECT 1 FROM payments WHERE paid_date > '{as_of}'")
    assert not query(ledger_path, f"SELECT 1 FROM cases WHERE opened_on > '{as_of}'")
    for (comms,) in query(ledger_path, "SELECT comms_history FROM cases"):
        assert all(date.fromisoformat(c["date"]) <= DEFAULT_AS_OF for c in json.loads(comms))


def test_every_case_carries_a_readable_comms_thread(ledger_path: Path) -> None:
    for (comms,) in query(ledger_path, "SELECT comms_history FROM cases"):
        thread = json.loads(comms)
        assert thread
        assert [c["date"] for c in thread] == sorted(c["date"] for c in thread)
        assert all(c["text"] and c["channel"] in {"sms", "email", "call", "system"} for c in thread)


def test_case_types_cover_the_servicing_mix(ledger_path: Path) -> None:
    types = {t for (t,) in query(ledger_path, "SELECT DISTINCT type FROM cases")}
    assert types == {"missed_payment", "dispute", "at_risk", "duplicate_charge"}
