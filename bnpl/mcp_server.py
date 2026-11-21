"""The agent's tools as a governed MCP server (stdio).

    python -m bnpl.mcp_server        (or: bnpl mcp)

Any MCP client can read cases and the exception inventory, and propose actions, but
only through the same mandate as the built-in agent: every proposal goes through the
governed ledger, the deterministic guardrail, the pause conditions, and the human
review queue. A client cannot execute anything the guardrail would hold or block.
"""

import contextlib
import json
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from bnpl.agent import ServicingAgent
from bnpl.config import db_path
from bnpl.governed import GovernedLedger
from bnpl.mandate import ESCALATION_OWNER, MANDATE, Action
from bnpl.proposers import FixedProposer
from bnpl.review import case_status, held_cases

MCP_SOURCE = "mcp"

server = MCPServer(
    name="bnpl-servicing-agent",
    instructions=(
        "Governed tools for a buy-now-pay-later servicing desk on synthetic data. "
        "Read a case with get_case before proposing. propose_action runs your proposal "
        "through a deterministic guardrail: low-risk actions inside the mandate run, "
        f"risky ones are held for the {ESCALATION_OWNER}, out-of-mandate ones are blocked. "
        "Use submit_for_review when a case is ambiguous. Allowed actions: "
        + ", ".join(f"{r.action.value} ({r.risk.value} risk)" for r in MANDATE)
        + "."
    ),
)


def _open() -> GovernedLedger:
    return GovernedLedger(db_path())


def _run(case_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    with _open() as ledger:
        if ledger.case(case_id) is None:
            raise ToolError(f"no case {case_id}")
        if ledger.decisions(case_id):
            raise ToolError(
                f"{case_id} has already been worked ({case_status(ledger.decisions(case_id))}); "
                "held cases are decided by a human in the review queue"
            )
        outcome = ServicingAgent(ledger, FixedProposer(raw, MCP_SOURCE)).run_case(case_id)
        review = next((h for h in held_cases(ledger) if h.case_id == case_id), None)
    return {
        "case_id": case_id,
        "status": outcome.status.value,
        "verdict": outcome.verdict.value if outcome.verdict else None,
        "reason_code": outcome.reason_code,
        "executed_action": outcome.final_action,
        "escalation_owner": review.escalation_owner if review else None,
        "decide_by": review.decide_by if review else None,
        "alerts": [asdict(a) for a in outcome.alerts],
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_case(case_id: str) -> dict[str, Any]:
    """Read one servicing case: loan, merchant, risk flags, payments, messages, audit trail."""
    with _open() as ledger:
        case = ledger.case(case_id)
        if case is None:
            raise ToolError(f"no case {case_id}")
        decisions = ledger.decisions(case_id)
        return {
            "case": asdict(case),
            "payments": [asdict(p) for p in ledger.payments(case.loan_id)],
            "status": case_status(decisions),
            "audit_trail": [asdict(d) for d in decisions],
        }


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def list_exceptions(kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """List the exception inventory, newest first: HOLD, BLOCK, PAUSE, BREACH or CLEAR."""
    with _open() as ledger:
        records = [r for r in ledger.exceptions() if kind is None or r.kind == kind.upper()]
    rows = []
    for record in reversed(records[-max(1, min(limit, 500)) :]):
        row = asdict(record)
        with contextlib.suppress(json.JSONDecodeError):
            row["detail"] = json.loads(record.detail)
        rows.append(row)
    return rows


@server.tool(annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
def propose_action(
    case_id: str,
    action: str,
    rationale: str,
    term_extension_months: int = 0,
    fee_waiver: bool = False,
) -> dict[str, Any]:
    """Propose ONE action for an unworked case. The guardrail decides what happens.

    action is one of: nudge, offer_plan, escalate_collections, hold_for_review, no_action.
    For offer_plan, set term_extension_months (1-3 runs unattended; more is held).
    Returns the verdict, reason code, whether anything ran, and who must decide if held.
    """
    raw = {
        "action": action,
        "rationale": rationale,
        "term_extension_months": term_extension_months,
        "fee_waiver": fee_waiver,
    }
    return _run(case_id, raw)


@server.tool(annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
def submit_for_review(case_id: str, reason: str) -> dict[str, Any]:
    """Abstain on an ambiguous case: send it to the human review queue with your reason."""
    raw = {
        "action": Action.HOLD_FOR_REVIEW.value,
        "rationale": reason,
        "term_extension_months": 0,
        "fee_waiver": False,
    }
    return _run(case_id, raw)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
