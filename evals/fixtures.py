"""Build a one-case eval ledger from a golden case.

Each golden case runs in its own fresh ledger, so cases cannot leak state into each
other. This module writes base tables directly, the way the seed does; the agent
under test still reads only through ``GovernedLedger``.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bnpl.ledger.db import connect, create_schema


def case_id_for(golden: dict[str, Any]) -> str:
    return f"case-{golden['id']}"


def build_eval_ledger(path: Path, golden: dict[str, Any], now: datetime) -> str:
    """Write the ledger for ``golden`` at ``path`` and return the case id to run."""
    gid = golden["id"]
    merchant, loan, case, setup = (
        golden["merchant"],
        golden["loan"],
        golden["case"],
        golden["setup"],
    )
    merchant_id, loan_id, case_id = f"mer-{gid}", f"loan-{gid}", case_id_for(golden)
    updated_at = now - timedelta(hours=setup["ledger_age_hours"])

    with connect(path) as conn:
        create_schema(conn)
        conn.execute(
            "INSERT INTO merchants (id, name, sector, under_review) VALUES (?, ?, ?, ?)",
            (merchant_id, merchant["name"], merchant["sector"], int(merchant["under_review"])),
        )
        conn.execute(
            "INSERT INTO loans (id, merchant_id, customer_ref, principal, term_months, apr, "
            "originated_on, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                loan_id,
                merchant_id,
                f"cust-{gid}",
                loan["principal"],
                loan["term_months"],
                loan["apr"],
                loan["originated_on"],
                loan["status"],
            ),
        )
        conn.executemany(
            "INSERT INTO payments (loan_id, due_date, paid_date, amount, status) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (loan_id, p["due_date"], p["paid_date"], p["amount"], p["status"])
                for p in golden["payments"]
            ],
        )
        conn.execute(
            "INSERT INTO cases (id, loan_id, type, opened_on, amount_overdue, days_overdue, "
            "risk_flags, comms_history) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                case_id,
                loan_id,
                case["case_type"],
                case["opened_on"],
                case["amount_overdue"],
                case["days_overdue"],
                json.dumps(case["risk_flags"]),
                json.dumps(case["comms_history"]),
            ),
        )
        prior = [
            (block["proposed_action"], block["verdict"])
            for block in setup["prior_decisions"]
            for _ in range(block["count"])
        ]
        conn.executemany(
            "INSERT INTO decisions (case_id, proposed_action, guardrail_verdict, final_action, "
            "actor, reason, timestamp) VALUES (?, ?, ?, ?, 'agent', 'prior history', ?)",
            [
                (
                    f"case-prior-{i:03d}",
                    action,
                    verdict,
                    action if verdict == "ALLOW" else None,
                    (now - timedelta(minutes=len(prior) - i)).isoformat(timespec="seconds"),
                )
                for i, (action, verdict) in enumerate(prior)
            ],
        )
        conn.execute(
            "INSERT INTO ledger_meta (key, value) VALUES ('updated_at', ?)",
            (updated_at.isoformat(timespec="seconds"),),
        )
    conn.close()
    return case_id
