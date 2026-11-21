"""Build a one-merchant eval ledger from a merchant-report golden case.

The portfolio spec (loan counts by status, disputes, fraud flags, held exceptions) is
written as ledger rows, so the agent under test reads its metrics through the same
governed view it uses in production.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bnpl.ledger.db import connect, create_schema

AS_OF = "2026-11-16"


def merchant_id_for(golden: dict[str, Any]) -> str:
    return f"mer-{golden['id']}"


def build_merchant_ledger(path: Path, golden: dict[str, Any], now: datetime) -> str:
    gid, spec, merchant = golden["id"], golden["portfolio"], golden["merchant"]
    merchant_id = merchant_id_for(golden)
    loans: list[tuple[str, str]] = []
    for status in ("current", "overdue", "in_plan", "charged_off"):
        loans += [(f"loan-{gid}-{status}-{n:02d}", status) for n in range(spec[status])]

    cases: list[tuple[str, str, str, float, int, list[str]]] = []
    for loan_id, status in loans:
        if status == "overdue":
            cases.append(
                (loan_id, "missed_payment", "o", spec["overdue_each"], 21, ["first_missed"])
            )
        elif status == "charged_off":
            cases.append(
                (loan_id, "missed_payment", "c", spec["charged_off_each"], 150, ["repeat_missed"])
            )
    current = [loan_id for loan_id, status in loans if status == "current"]
    for n in range(spec["disputes"]):
        cases.append((current[n], "dispute", "d", 0.0, 0, []))
    for n in range(spec["fraud_flags"]):
        cases.append((current[-1 - n], "at_risk", "f", 0.0, 0, ["fraud_flag"]))

    with connect(path) as conn:
        create_schema(conn)
        conn.execute(
            "INSERT INTO merchants (id, name, sector, under_review) VALUES (?, ?, ?, ?)",
            (merchant_id, merchant["name"], merchant["sector"], int(merchant["under_review"])),
        )
        conn.executemany(
            "INSERT INTO loans (id, merchant_id, customer_ref, principal, term_months, apr, "
            "originated_on, status) VALUES (?, ?, ?, ?, 12, 0.1799, '2026-05-01', ?)",
            [
                (loan_id, merchant_id, f"cust-{gid}-{n:03d}", spec["principal_each"], status)
                for n, (loan_id, status) in enumerate(loans)
            ],
        )
        case_ids = []
        for n, (loan_id, kind, tag, amount, days, flags) in enumerate(cases):
            case_id = f"case-{gid}-{tag}{n:02d}"
            case_ids.append(case_id)
            conn.execute(
                "INSERT INTO cases (id, loan_id, type, opened_on, amount_overdue, days_overdue, "
                "risk_flags, comms_history) VALUES (?, ?, ?, ?, ?, ?, ?, '[]')",
                (case_id, loan_id, kind, AS_OF, amount, days, json.dumps(flags)),
            )
        conn.executemany(
            "INSERT INTO exceptions (case_id, kind, reason_code, detail, timestamp) "
            "VALUES (?, 'HOLD', 'HIGH_RISK_ACTION', '', ?)",
            [(case_id, now.isoformat(timespec="seconds")) for case_id in case_ids[: spec["holds"]]],
        )
        updated_at = now - timedelta(hours=golden["setup"]["ledger_age_hours"])
        conn.executemany(
            "INSERT INTO ledger_meta (key, value) VALUES (?, ?)",
            [("updated_at", updated_at.isoformat(timespec="seconds")), ("as_of", AS_OF)],
        )
    conn.close()
    return merchant_id
