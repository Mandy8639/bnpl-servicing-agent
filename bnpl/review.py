"""The human review queue. Nothing held is ever executed without a named person's decision.

A case is in the queue while its latest audit entry is the agent's HOLD. A human
approves (the action runs, attributed to them) or rejects (nothing runs). Both write
to the append-only audit log with the human's reason.
"""

import json
from dataclasses import dataclass

from bnpl.governed import Decision, GovernedLedger
from bnpl.mandate import AGENT_ACTOR, Action

REJECTED_PREFIX = "REJECTED"


class ReviewError(ValueError):
    """A review request that the queue cannot accept."""


@dataclass(frozen=True)
class HeldCase:
    case_id: str
    proposed_action: str
    rationale: str
    reason_code: str
    guardrail_detail: str
    escalation_owner: str | None
    decide_by: str | None
    held_at: str
    merchant_name: str
    sector: str
    amount_overdue: float
    days_overdue: int
    risk_flags: tuple[str, ...]


def case_status(decisions: list[Decision]) -> str:
    """Where a case stands, from its audit trail."""
    if not decisions:
        return "open"
    last = decisions[-1]
    if last.actor == AGENT_ACTOR:
        return {"ALLOW": "executed", "HOLD": "held", "BLOCK": "blocked"}[last.guardrail_verdict]
    return "rejected" if last.reason.startswith(REJECTED_PREFIX) else "approved"


def _latest_decisions(ledger: GovernedLedger) -> dict[str, Decision]:
    latest: dict[str, Decision] = {}
    for decision in ledger.decisions():
        latest[decision.case_id] = decision
    return latest


def _hold_details(ledger: GovernedLedger) -> dict[str, dict[str, object]]:
    details: dict[str, dict[str, object]] = {}
    for record in ledger.exceptions():
        if record.kind == "HOLD" and record.case_id:
            details[record.case_id] = json.loads(record.detail or "{}") | {
                "reason_code": record.reason_code
            }
    return details


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def held_cases(ledger: GovernedLedger) -> list[HeldCase]:
    """Every case waiting on a human, oldest deadline first."""
    details = _hold_details(ledger)
    queue: list[HeldCase] = []
    for case_id, decision in _latest_decisions(ledger).items():
        if decision.actor != AGENT_ACTOR or decision.guardrail_verdict != "HOLD":
            continue
        case = ledger.case(case_id)
        if case is None:
            continue
        info = details.get(case_id, {})
        queue.append(
            HeldCase(
                case_id=case_id,
                proposed_action=decision.proposed_action,
                rationale=str(info.get("rationale", "")),
                reason_code=str(info.get("reason_code", decision.reason.split(":")[0])),
                guardrail_detail=str(info.get("guardrail_detail", "")),
                escalation_owner=_optional(info.get("escalation_owner")),
                decide_by=_optional(info.get("decide_by")),
                held_at=decision.timestamp,
                merchant_name=case.merchant_name,
                sector=case.sector,
                amount_overdue=case.amount_overdue,
                days_overdue=case.days_overdue,
                risk_flags=case.risk_flags,
            )
        )
    return sorted(queue, key=lambda h: (h.decide_by or "", h.case_id))


def _require_held(ledger: GovernedLedger, case_id: str) -> HeldCase:
    for held in held_cases(ledger):
        if held.case_id == case_id:
            return held
    raise ReviewError(f"{case_id} is not waiting for review")


def _require_human(actor: str, reason: str) -> None:
    if not actor.strip() or actor.strip().lower() == AGENT_ACTOR:
        raise ReviewError("a review needs a named human actor")
    if not reason.strip():
        raise ReviewError("a review needs a reason")


def approve(
    ledger: GovernedLedger, case_id: str, *, actor: str, reason: str, action: str | None = None
) -> Decision:
    """A human approves the held proposal (or picks the action, when the agent abstained)."""
    _require_human(actor, reason)
    held = _require_held(ledger, case_id)
    final = action or held.proposed_action
    if final not in {a.value for a in Action} or final == Action.HOLD_FOR_REVIEW:
        raise ReviewError(
            "the agent abstained on this case; choose the action to take with --action"
            if final == Action.HOLD_FOR_REVIEW
            else f"'{final}' is not in the action set"
        )
    ledger.record_decision(
        case_id=case_id,
        proposed_action=held.proposed_action,
        guardrail_verdict="HOLD",
        final_action=final,
        actor=actor.strip(),
        reason=f"APPROVED: {reason.strip()}",
    )
    return ledger.decisions(case_id)[-1]


def reject(ledger: GovernedLedger, case_id: str, *, actor: str, reason: str) -> Decision:
    """A human rejects the held proposal. Nothing is executed."""
    _require_human(actor, reason)
    held = _require_held(ledger, case_id)
    ledger.record_decision(
        case_id=case_id,
        proposed_action=held.proposed_action,
        guardrail_verdict="HOLD",
        final_action=None,
        actor=actor.strip(),
        reason=f"{REJECTED_PREFIX}: {reason.strip()}",
    )
    return ledger.decisions(case_id)[-1]
