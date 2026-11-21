"""Governed access to the ledger: the only way the agent reads or writes data.

A ``GovernedLedger`` installs a SQLite authorizer on its connection, so the rules
below are enforced by the database engine, not by convention:

* reads are allowed only through the ``v_*`` views, never from base tables;
* writes are allowed only as INSERTs into the append-only ``decisions`` audit log
  and ``exceptions`` inventory; every UPDATE, DELETE, DDL or PRAGMA is denied.

It also owns the data-freshness check behind pause condition 1: the ledger's
``updated_at`` must be newer than 24 hours.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bnpl.ledger.db import connect

FRESHNESS_MAX_AGE = timedelta(hours=24)
APPEND_ONLY_TABLES = frozenset({"decisions", "exceptions"})
_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION}
)


class GovernanceError(RuntimeError):
    """Raised when code asks the governed ledger for something the rules forbid."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _authorize(
    action: int, arg1: str | None, _arg2: str | None, _db: str | None, source: str | None
) -> int:
    """SQLite authorizer: views-only reads, append-only writes, nothing else."""
    if action == sqlite3.SQLITE_READ:
        name = arg1 or ""
        is_view = name.startswith("v_")
        through_view = source is not None and source.startswith("v_")
        is_catalog = name.startswith("sqlite_")
        allowed = is_view or through_view or is_catalog
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_INSERT:
        return sqlite3.SQLITE_OK if arg1 in APPEND_ONLY_TABLES else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


@dataclass(frozen=True)
class FreshnessCheck:
    fresh: bool
    updated_at: datetime
    checked_at: datetime
    max_age: timedelta

    @property
    def age(self) -> timedelta:
        return self.checked_at - self.updated_at


@dataclass(frozen=True)
class Merchant:
    id: str
    name: str
    sector: str
    under_review: bool


@dataclass(frozen=True)
class Payment:
    loan_id: str
    due_date: str
    paid_date: str | None
    amount: float
    status: str


@dataclass(frozen=True)
class CommsEntry:
    date: str
    channel: str
    direction: str
    text: str


@dataclass(frozen=True)
class Case:
    """A servicing case joined with its loan and merchant, as the governed view exposes it."""

    case_id: str
    case_type: str
    opened_on: str
    amount_overdue: float
    days_overdue: int
    risk_flags: tuple[str, ...]
    comms_history: tuple[CommsEntry, ...]
    loan_id: str
    customer_ref: str
    principal: float
    term_months: float
    apr: float
    originated_on: str
    loan_status: str
    merchant_id: str
    merchant_name: str
    sector: str
    merchant_under_review: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Case":
        fields: dict[str, Any] = dict(row)
        fields["risk_flags"] = tuple(json.loads(fields["risk_flags"]))
        fields["comms_history"] = tuple(
            CommsEntry(**c) for c in json.loads(fields["comms_history"])
        )
        fields["merchant_under_review"] = bool(fields["merchant_under_review"])
        return cls(**fields)


@dataclass(frozen=True)
class Decision:
    id: int
    case_id: str
    proposed_action: str
    guardrail_verdict: str
    final_action: str | None
    actor: str
    reason: str
    timestamp: str


@dataclass(frozen=True)
class ExceptionRecord:
    id: int
    case_id: str | None
    kind: str
    reason_code: str
    detail: str
    timestamp: str


class GovernedLedger:
    """Views-only, append-only access to the ledger, with a freshness check."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = utc_now) -> None:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"no ledger at {path}; run `python -m bnpl.ledger.seed`")
        self._conn = connect(path)
        self._conn.set_authorizer(_authorize)
        self._clock = clock

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "GovernedLedger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as exc:
            raise GovernanceError(str(exc)) from exc

    @contextmanager
    def _append(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._conn:
                yield self._conn
        except sqlite3.DatabaseError as exc:
            raise GovernanceError(str(exc)) from exc

    def freshness(self, max_age: timedelta = FRESHNESS_MAX_AGE) -> FreshnessCheck:
        rows = self._query("SELECT value FROM v_ledger_meta WHERE key = 'updated_at'")
        if not rows:
            raise GovernanceError("ledger has no updated_at stamp")
        updated_at = datetime.fromisoformat(rows[0]["value"])
        checked_at = self._clock()
        return FreshnessCheck(
            fresh=checked_at - updated_at <= max_age,
            updated_at=updated_at,
            checked_at=checked_at,
            max_age=max_age,
        )

    def merchants(self) -> list[Merchant]:
        rows = self._query("SELECT id, name, sector, under_review FROM v_merchants ORDER BY id")
        return [Merchant(r["id"], r["name"], r["sector"], bool(r["under_review"])) for r in rows]

    def case(self, case_id: str) -> Case | None:
        rows = self._query("SELECT * FROM v_cases WHERE case_id = ?", (case_id,))
        return Case.from_row(rows[0]) if rows else None

    def cases(self) -> list[Case]:
        return [Case.from_row(r) for r in self._query("SELECT * FROM v_cases ORDER BY case_id")]

    def payments(self, loan_id: str) -> list[Payment]:
        rows = self._query(
            "SELECT loan_id, due_date, paid_date, amount, status FROM v_payments "
            "WHERE loan_id = ? ORDER BY due_date",
            (loan_id,),
        )
        return [Payment(**dict(r)) for r in rows]

    def decisions(self, case_id: str | None = None) -> list[Decision]:
        sql = "SELECT * FROM v_decisions"
        params: tuple[Any, ...] = ()
        if case_id is not None:
            sql += " WHERE case_id = ?"
            params = (case_id,)
        return [Decision(**dict(r)) for r in self._query(sql + " ORDER BY id", params)]

    def nudges_sent_on(self, customer_ref: str, day: str) -> int:
        """How many nudges were executed for a customer on a UTC date (``YYYY-MM-DD``)."""
        rows = self._query(
            "SELECT COUNT(*) AS n FROM v_decisions d JOIN v_cases c ON c.case_id = d.case_id "
            "WHERE c.customer_ref = ? AND d.final_action = 'nudge' "
            "AND substr(d.timestamp, 1, 10) = ?",
            (customer_ref, day),
        )
        return int(rows[0]["n"])

    def last_decision_id(self) -> int:
        # A dedicated view: MAX() or ORDER BY on v_decisions lets SQLite read the base
        # table's rowid outside the view, which the authorizer (correctly) refuses.
        rows = self._query("SELECT last_id FROM v_decision_watermark")
        return int(rows[0]["last_id"])

    def exceptions(self) -> list[ExceptionRecord]:
        rows = self._query("SELECT * FROM v_exceptions ORDER BY id")
        return [ExceptionRecord(**dict(r)) for r in rows]

    def record_decision(
        self,
        *,
        case_id: str,
        proposed_action: str,
        guardrail_verdict: str,
        final_action: str | None,
        actor: str,
        reason: str,
    ) -> int:
        """Append one entry to the audit log and return its id."""
        with self._append() as conn:
            cursor = conn.execute(
                "INSERT INTO decisions (case_id, proposed_action, guardrail_verdict, "
                "final_action, actor, reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    proposed_action,
                    guardrail_verdict,
                    final_action,
                    actor,
                    reason,
                    self._clock().isoformat(timespec="seconds"),
                ),
            )
        return int(cursor.lastrowid or 0)

    def record_exception(
        self, *, kind: str, reason_code: str, case_id: str | None = None, detail: str = ""
    ) -> int:
        """Append one entry to the exception inventory and return its id."""
        with self._append() as conn:
            cursor = conn.execute(
                "INSERT INTO exceptions (case_id, kind, reason_code, detail, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (case_id, kind, reason_code, detail, self._clock().isoformat(timespec="seconds")),
            )
        return int(cursor.lastrowid or 0)
