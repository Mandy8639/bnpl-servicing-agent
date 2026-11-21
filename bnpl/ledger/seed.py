"""Deterministic synthetic ledger.

``seed_ledger`` builds the same ledger every time for the same ``seed`` and ``as_of``:
merchants, loans with real instalment schedules, a recent window of payments, and
servicing cases whose amounts, dates and communication threads agree with those
payments. Only ``ledger_meta.updated_at`` depends on the clock, and it can be pinned.

Run ``python -m bnpl.ledger.seed --reset`` to (re)build ``data/ledger.db``.
"""

import argparse
import calendar
import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from bnpl.config import db_path
from bnpl.ledger.catalog import (
    DISPUTE_CLAIMS,
    HARDSHIP_REPLIES,
    MERCHANT_DISPUTE_RESPONSES,
    MERCHANTS,
    PRINCIPAL_BY_SECTOR,
)
from bnpl.ledger.db import connect, create_schema

DEFAULT_SEED = 42
DEFAULT_AS_OF = date(2026, 11, 16)
LOAN_COUNT = 240
PAY_IN_4_TERM = 1.5
LOAN_STATUS_WEIGHTS = {"current": 62, "overdue": 24, "in_plan": 9, "charged_off": 5}
DISPUTE_CASES = 8
DUPLICATE_CHARGE_CASES = 5
AT_RISK_CASES = 8
LANDED_AFTER_OPEN_CASES = 6


@dataclass(frozen=True)
class SeedSummary:
    path: Path
    seed: int
    as_of: date
    row_counts: dict[str, int]


@dataclass
class _Instalment:
    due: date
    amount: float
    status: str = "scheduled"
    paid: date | None = None


@dataclass
class _Loan:
    id: str
    merchant_id: str
    merchant_name: str
    sector: str
    customer_ref: str
    principal: float
    term_months: float
    apr: float
    originated_on: date
    status: str
    instalments: list[_Instalment] = field(default_factory=list)

    @property
    def missed(self) -> list[_Instalment]:
        return [i for i in self.instalments if i.status == "missed"]


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _instalment_amount(principal: float, term_months: float, apr: float) -> float:
    if term_months == PAY_IN_4_TERM:
        return round(principal / 4, 2)
    n = int(term_months)
    if apr == 0:
        return round(principal / n, 2)
    r = apr / 12
    return round(principal * r / (1 - (1 + r) ** -n), 2)


def _due_dates(originated_on: date, term_months: float) -> list[date]:
    if term_months == PAY_IN_4_TERM:
        return [originated_on + timedelta(days=14 * k) for k in range(1, 5)]
    return [_add_months(originated_on, k) for k in range(1, int(term_months) + 1)]


def _term_for(rng: random.Random, principal: float) -> float:
    if principal < 500:
        return rng.choice([PAY_IN_4_TERM, PAY_IN_4_TERM, 3.0, 6.0])
    if principal < 2_000:
        return rng.choice([3.0, 6.0, 12.0])
    if principal < 10_000:
        return rng.choice([6.0, 12.0, 18.0, 24.0, 36.0])
    return rng.choice([24.0, 36.0, 48.0, 60.0, 84.0])


def _apr_for(rng: random.Random, term_months: float) -> float:
    if term_months == PAY_IN_4_TERM:
        return 0.0
    if term_months <= 6:
        return rng.choice([0.0, 0.0, 0.0999])
    return round(rng.uniform(0.0999, 0.3599), 4)


def _build_loan(rng: random.Random, index: int, customer: int, as_of: date) -> _Loan:
    merchant_index = rng.randrange(len(MERCHANTS))
    spec = MERCHANTS[merchant_index]
    low, high, mode = PRINCIPAL_BY_SECTOR[spec.sector]
    principal = round(rng.triangular(low, high, mode), 2)
    term = _term_for(rng, principal)
    apr = _apr_for(rng, term)
    status = rng.choices(list(LOAN_STATUS_WEIGHTS), weights=list(LOAN_STATUS_WEIGHTS.values()))[0]

    first_gap = 14 if term == PAY_IN_4_TERM else 31
    min_missed = {"overdue": 1, "charged_off": 4}.get(status, 0)
    min_days = first_gap + 5 + first_gap * max(min_missed - 1, 0)
    term_days = 56 if term == PAY_IN_4_TERM else int(term * 30.4)
    ceiling = term_days + 60 if min_missed else term_days - 10
    days_back = rng.randint(min_days, max(min_days, min(540, ceiling)))
    originated_on = as_of - timedelta(days=days_back)

    loan = _Loan(
        id=f"loan-{index:04d}",
        merchant_id=f"mer-{merchant_index + 1:02d}",
        merchant_name=spec.name,
        sector=spec.sector,
        customer_ref=f"cust-{customer:04d}",
        principal=principal,
        term_months=term,
        apr=apr,
        originated_on=originated_on,
        status=status,
    )
    _build_schedule(rng, loan, as_of)
    return loan


def _build_schedule(rng: random.Random, loan: _Loan, as_of: date) -> None:
    """Keep a recent window of the schedule: the last few past instalments and the next one."""
    amount = _instalment_amount(loan.principal, loan.term_months, loan.apr)
    dues = _due_dates(loan.originated_on, loan.term_months)
    past = [d for d in dues if d < as_of]
    upcoming = [d for d in dues if d >= as_of][:1]

    if loan.status == "overdue":
        missed = rng.choices([1, 2, 3], weights=[60, 28, 12])[0]
    elif loan.status == "charged_off":
        missed = rng.randint(4, 6)
    else:
        missed = 0
    missed = min(missed, len(past))
    window = min(len(past), max(missed, rng.randint(1, 2)))
    kept_past = past[len(past) - window :]

    for position, due in enumerate(kept_past):
        inst = _Instalment(due=due, amount=amount)
        if position >= window - missed:
            inst.status = "missed"
        elif rng.random() < 0.15 and due + timedelta(days=12) < as_of:
            inst.status = "late_paid"
            inst.paid = due + timedelta(days=rng.randint(2, 12))
        else:
            inst.status = "paid"
            inst.paid = due - timedelta(days=rng.randint(0, 3))
        loan.instalments.append(inst)

    if loan.status != "charged_off":
        loan.instalments.extend(_Instalment(due=d, amount=amount) for d in upcoming)


@dataclass
class _Case:
    loan: _Loan
    type: str
    opened_on: date
    amount_overdue: float
    days_overdue: int
    risk_flags: list[str]
    comms: list[dict[str, str]]


def _comm(day: date, channel: str, direction: str, text: str) -> dict[str, str]:
    return {"date": day.isoformat(), "channel": channel, "direction": direction, "text": text}


def _reminder(loan: _Loan, inst: _Instalment) -> dict[str, str]:
    text = (
        f"Reminder: your {_money(inst.amount)} instalment for {loan.merchant_name} "
        f"is due {inst.due:%b %d}."
    )
    return _comm(inst.due - timedelta(days=3), "sms", "outbound", text)


def _missed_notice(loan: _Loan, inst: _Instalment) -> dict[str, str]:
    text = (
        f"We couldn't collect your {_money(inst.amount)} instalment due {inst.due:%b %d}. "
        "You can pay any time from your account."
    )
    return _comm(inst.due + timedelta(days=1), "email", "outbound", text)


def _missed_payment_case(rng: random.Random, loan: _Loan, as_of: date) -> _Case:
    missed = loan.missed
    first = missed[0]
    opened = min(first.due + timedelta(days=rng.randint(1, 3)), as_of)
    flags = ["first_missed" if len(missed) == 1 else "repeat_missed"]
    comms = [_reminder(loan, first), _missed_notice(loan, first)]

    if len(missed) > 1:
        call_day = min(missed[1].due + timedelta(days=4), as_of)
        text = f"Left voicemail about {len(missed)} missed instalments."
        comms.append(_comm(call_day, "call", "outbound", text))
    if loan.status != "charged_off" and rng.random() < 0.25:
        flags.append("hardship_signal")
        reply_day = min(first.due + timedelta(days=rng.randint(2, 6)), as_of)
        comms.append(_comm(reply_day, "sms", "inbound", rng.choice(HARDSHIP_REPLIES)))
    if rng.random() < 0.05:
        flags.append("fraud_flag")
        text = "Card on file flagged by the issuer for suspected fraud."
        comms.append(_comm(opened, "system", "internal", text))
    if loan.status == "charged_off":
        text = f"No contact for {(as_of - first.due).days} days. Account charged off."
        comms.append(_comm(as_of - timedelta(days=2), "system", "internal", text))

    return _Case(
        loan=loan,
        type="missed_payment",
        opened_on=opened,
        amount_overdue=round(sum(i.amount for i in missed), 2),
        days_overdue=(as_of - first.due).days,
        risk_flags=flags,
        comms=sorted(comms, key=lambda c: c["date"]),
    )


def _dispute_case(rng: random.Random, loan: _Loan, as_of: date) -> _Case:
    case = _missed_payment_case(rng, loan, as_of)
    claim = rng.choice(DISPUTE_CLAIMS[loan.sector]).format(
        merchant=loan.merchant_name, date=f"{loan.originated_on + timedelta(days=9):%b %d}"
    )
    claim_day = min(case.opened_on + timedelta(days=1), as_of)
    response_day = min(claim_day + timedelta(days=rng.randint(2, 5)), as_of)
    case.type = "dispute"
    case.comms = sorted(
        [
            *case.comms,
            _comm(claim_day, "email", "inbound", claim),
            _comm(claim_day, "email", "outbound", "Dispute opened with the merchant."),
            _comm(response_day, "system", "internal", rng.choice(MERCHANT_DISPUTE_RESPONSES)),
        ],
        key=lambda c: c["date"],
    )
    return case


def _landed_after_open_case(rng: random.Random, loan: _Loan, inst: _Instalment) -> _Case:
    assert inst.paid is not None
    opened = inst.due + timedelta(days=1)
    received = f"Payment of {_money(inst.amount)} received."
    return _Case(
        loan=loan,
        type="missed_payment",
        opened_on=opened,
        amount_overdue=0.0,
        days_overdue=0,
        risk_flags=["first_missed"],
        comms=[
            _reminder(loan, inst),
            _missed_notice(loan, inst),
            _comm(inst.paid, "system", "internal", received),
        ],
    )


def _duplicate_charge_case(rng: random.Random, loan: _Loan, as_of: date) -> _Case:
    paid = [i for i in loan.instalments if i.paid is not None]
    inst = paid[-1]
    assert inst.paid is not None
    report_day = min(inst.paid + timedelta(days=rng.randint(1, 4)), as_of)
    finding = rng.choice(
        [
            "Settlement check: two captures found for the same instalment.",
            "Settlement check: one capture found; second charge may be a pending hold.",
        ]
    )
    return _Case(
        loan=loan,
        type="duplicate_charge",
        opened_on=report_day,
        amount_overdue=0.0,
        days_overdue=0,
        risk_flags=[],
        comms=[
            _comm(
                report_day,
                "email",
                "inbound",
                f"I was charged {_money(inst.amount)} twice on {inst.paid:%b %d}. "
                "Please refund one.",
            ),
            _comm(report_day, "system", "internal", finding),
        ],
    )


def _at_risk_case(rng: random.Random, loan: _Loan, as_of: date) -> _Case:
    opened = as_of - timedelta(days=rng.randint(1, 10))
    return _Case(
        loan=loan,
        type="at_risk",
        opened_on=opened,
        amount_overdue=0.0,
        days_overdue=0,
        risk_flags=["hardship_signal"],
        comms=[
            _comm(
                opened - timedelta(days=2),
                "system",
                "internal",
                "Linked account balance below the instalment amount for 2 cycles.",
            ),
            _comm(opened, "sms", "inbound", rng.choice(HARDSHIP_REPLIES)),
        ],
    )


def _build_cases(rng: random.Random, loans: list[_Loan], as_of: date) -> list[_Case]:
    overdue = [ln for ln in loans if ln.status in ("overdue", "charged_off") and ln.missed]
    disputes = {
        ln.id
        for ln in rng.sample([ln for ln in overdue if ln.status == "overdue"], k=DISPUTE_CASES)
    }
    cases = [
        _dispute_case(rng, ln, as_of) if ln.id in disputes else _missed_payment_case(rng, ln, as_of)
        for ln in overdue
    ]

    current = [ln for ln in loans if ln.status == "current"]
    late_paid = [
        (ln, i) for ln in current for i in ln.instalments if i.status == "late_paid" and i.paid
    ]
    landed = rng.sample(late_paid, k=min(LANDED_AFTER_OPEN_CASES, len(late_paid)))
    cases.extend(_landed_after_open_case(rng, ln, inst) for ln, inst in landed)

    used = {ln.id for ln, _ in landed}
    with_payment = [
        ln for ln in current if ln.id not in used and any(i.paid for i in ln.instalments)
    ]
    duplicates = rng.sample(with_payment, k=DUPLICATE_CHARGE_CASES)
    cases.extend(_duplicate_charge_case(rng, ln, as_of) for ln in duplicates)

    used |= {ln.id for ln in duplicates}
    at_risk = rng.sample([ln for ln in current if ln.id not in used], k=AT_RISK_CASES)
    cases.extend(_at_risk_case(rng, ln, as_of) for ln in at_risk)

    return sorted(cases, key=lambda c: (c.opened_on, c.loan.id))


def generate(
    seed: int = DEFAULT_SEED, as_of: date = DEFAULT_AS_OF
) -> tuple[list[_Loan], list[_Case]]:
    """Generate loans and cases in memory, deterministically."""
    rng = random.Random(seed)
    customers = rng.sample(range(1, 2_000), k=LOAN_COUNT)
    loans = [_build_loan(rng, i + 1, customers[i], as_of) for i in range(LOAN_COUNT)]
    return loans, _build_cases(rng, loans, as_of)


def _write(conn: sqlite3.Connection, loans: list[_Loan], cases: list[_Case]) -> None:
    conn.executemany(
        "INSERT INTO merchants (id, name, sector, under_review) VALUES (?, ?, ?, ?)",
        [
            (f"mer-{i + 1:02d}", m.name, m.sector, int(m.under_review))
            for i, m in enumerate(MERCHANTS)
        ],
    )
    conn.executemany(
        "INSERT INTO loans (id, merchant_id, customer_ref, principal, term_months, apr, "
        "originated_on, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                ln.id,
                ln.merchant_id,
                ln.customer_ref,
                ln.principal,
                ln.term_months,
                ln.apr,
                ln.originated_on.isoformat(),
                ln.status,
            )
            for ln in loans
        ],
    )
    conn.executemany(
        "INSERT INTO payments (loan_id, due_date, paid_date, amount, status) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                ln.id,
                i.due.isoformat(),
                i.paid.isoformat() if i.paid else None,
                i.amount,
                i.status,
            )
            for ln in loans
            for i in ln.instalments
        ],
    )
    conn.executemany(
        "INSERT INTO cases (id, loan_id, type, opened_on, amount_overdue, days_overdue, "
        "risk_flags, comms_history) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                f"case-{n:04d}",
                c.loan.id,
                c.type,
                c.opened_on.isoformat(),
                c.amount_overdue,
                c.days_overdue,
                json.dumps(c.risk_flags),
                json.dumps(c.comms),
            )
            for n, c in enumerate(cases, start=1)
        ],
    )


def seed_ledger(
    path: Path | str,
    *,
    seed: int = DEFAULT_SEED,
    as_of: date = DEFAULT_AS_OF,
    now: datetime | None = None,
    reset: bool = False,
) -> SeedSummary:
    """Build the synthetic ledger at ``path``.

    Refuses to touch an existing file unless ``reset`` is true, because reseeding
    wipes the append-only audit log along with everything else.
    """
    path = Path(path)
    if path.exists():
        if not reset:
            raise FileExistsError(f"{path} exists; pass reset=True to rebuild it")
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    loans, cases = generate(seed, as_of)
    updated_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    with connect(path) as conn:
        create_schema(conn)
        _write(conn, loans, cases)
        conn.executemany(
            "INSERT INTO ledger_meta (key, value) VALUES (?, ?)",
            [("updated_at", updated_at), ("as_of", as_of.isoformat()), ("seed", str(seed))],
        )
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("merchants", "loans", "payments", "cases", "decisions", "exceptions")
        }
    conn.close()
    return SeedSummary(path=path, seed=seed, as_of=as_of, row_counts=counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the synthetic BNPL ledger.")
    parser.add_argument("--db", type=Path, default=db_path(), help="ledger path")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--reset", action="store_true", help="overwrite an existing ledger")
    args = parser.parse_args()
    summary = seed_ledger(args.db, seed=args.seed, reset=args.reset)
    print(f"Seeded {summary.path} (seed={summary.seed}, as_of={summary.as_of})")
    for table, count in summary.row_counts.items():
        print(f"  {table:<11} {count:>4}")


if __name__ == "__main__":
    main()
