"""Deterministic policy guardrail. No LLM calls, no I/O: same inputs, same verdict.

Every proposal gets exactly one ``Verdict`` and one machine-readable ``ReasonCode``.
Checks run in a fixed order, and the first one that fires decides:

1. Is the proposal well formed and inside the mandate?          otherwise BLOCK
2. Is the agent paused, or does a pause condition fire now?     then HOLD
3. Is the case's merchant under review?                         then HOLD
4. The action's own handling rule from the mandate.             ALLOW or HOLD

Pause conditions (``detect_pause_conditions``) are agent-wide, except merchant
review, which holds only that merchant's cases.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from bnpl.mandate import (
    CONTROL_MIN_SAMPLE,
    CONTROL_WINDOW,
    ESCALATION_OWNER,
    MAX_AUTO_ACTION_RATE,
    MAX_CONSECUTIVE_BLOCKS,
    MAX_OFFER_PLAN_RATE,
    MAX_PLAN_EXTENSION_MONTHS,
    NUDGES_PER_CUSTOMER_PER_DAY,
    REVIEW_SLA,
    Action,
)


class Verdict(StrEnum):
    ALLOW = "ALLOW"
    HOLD = "HOLD"
    BLOCK = "BLOCK"


class ReasonCode(StrEnum):
    # BLOCK: the proposal itself is outside the mandate.
    OUT_OF_MANDATE_ACTION = "OUT_OF_MANDATE_ACTION"
    CASE_MISMATCH = "CASE_MISMATCH"
    MISSING_RATIONALE = "MISSING_RATIONALE"
    MISSING_PLAN_TERMS = "MISSING_PLAN_TERMS"
    MALFORMED_PLAN_TERMS = "MALFORMED_PLAN_TERMS"
    # HOLD: a pause condition or merchant review stops automation.
    AGENT_PAUSED = "AGENT_PAUSED"
    DATA_STALE = "DATA_STALE"
    AUTO_RATE_BREACH = "AUTO_RATE_BREACH"
    OFFER_PLAN_RATE_BREACH = "OFFER_PLAN_RATE_BREACH"
    REPEATED_BLOCKS = "REPEATED_BLOCKS"
    MERCHANT_UNDER_REVIEW = "MERCHANT_UNDER_REVIEW"
    # HOLD: the action's own rule needs a human.
    HIGH_RISK_ACTION = "HIGH_RISK_ACTION"
    PLAN_OUT_OF_BOUNDS = "PLAN_OUT_OF_BOUNDS"
    NUDGE_CAP_REACHED = "NUDGE_CAP_REACHED"
    AGENT_ABSTAINED = "AGENT_ABSTAINED"
    NO_ACTION_WITH_ARREARS = "NO_ACTION_WITH_ARREARS"
    # ALLOW: inside the mandate, safe to run unattended.
    NUDGE_WITHIN_CAP = "NUDGE_WITHIN_CAP"
    PLAN_WITHIN_BOUNDS = "PLAN_WITHIN_BOUNDS"
    NO_ACTION_LOGGED = "NO_ACTION_LOGGED"


_ACTIONS = frozenset(a.value for a in Action)


class PauseScope(StrEnum):
    AGENT = "agent"
    MERCHANT = "merchant"


@dataclass(frozen=True)
class PauseCondition:
    reason_code: ReasonCode
    scope: PauseScope
    detail: str


@dataclass(frozen=True)
class PlanTerms:
    term_extension_months: int
    fee_waiver: bool = False


@dataclass(frozen=True)
class Proposal:
    """What the agent wants to do. ``action`` is a raw string: the model may say anything."""

    case_id: str
    action: str
    rationale: str
    plan: PlanTerms | None = None


@dataclass(frozen=True)
class RecentDecision:
    proposed_action: str
    verdict: Verdict


@dataclass(frozen=True)
class GuardrailContext:
    """Everything the guardrail may look at, gathered by the caller from governed data."""

    case_id: str
    customer_ref: str
    merchant_id: str
    merchant_under_review: bool
    data_fresh: bool
    now: datetime
    nudges_today_for_customer: int = 0
    amount_overdue: float = 0.0
    recent_decisions: Sequence[RecentDecision] = field(default_factory=tuple)
    agent_paused: bool = False


@dataclass(frozen=True)
class GuardrailResult:
    verdict: Verdict
    reason_code: ReasonCode
    detail: str
    pause: PauseCondition | None = None
    escalation_owner: str | None = None
    decide_by: datetime | None = None

    @property
    def needs_human(self) -> bool:
        return self.verdict is Verdict.HOLD


def _rate(decisions: Sequence[RecentDecision], hit: str) -> float:
    if hit == "auto":
        matches = sum(d.verdict is Verdict.ALLOW for d in decisions)
    else:
        matches = sum(d.proposed_action == hit for d in decisions)
    return matches / len(decisions)


def trailing_blocks(decisions: Sequence[RecentDecision]) -> int:
    """How many of the most recent decisions in a row were BLOCKs."""
    count = 0
    for decision in reversed(decisions):
        if decision.verdict is not Verdict.BLOCK:
            break
        count += 1
    return count


def detect_pause_conditions(ctx: GuardrailContext) -> list[PauseCondition]:
    """Agent-wide pause conditions that hold right now, in mandate order.

    Rate controls look at the last ``CONTROL_WINDOW`` decisions and only fire once
    at least ``CONTROL_MIN_SAMPLE`` exist, so a cold start cannot trip them on one case.
    """
    found: list[PauseCondition] = []
    if not ctx.data_fresh:
        found.append(
            PauseCondition(
                ReasonCode.DATA_STALE,
                PauseScope.AGENT,
                "Governed-data freshness check failed: ledger older than 24h.",
            )
        )

    window = list(ctx.recent_decisions)[-CONTROL_WINDOW:]
    if len(window) >= CONTROL_MIN_SAMPLE:
        auto_rate = _rate(window, "auto")
        if auto_rate > MAX_AUTO_ACTION_RATE:
            found.append(
                PauseCondition(
                    ReasonCode.AUTO_RATE_BREACH,
                    PauseScope.AGENT,
                    f"Auto-action rate {auto_rate:.0%} over the last {len(window)} cases "
                    f"exceeds {MAX_AUTO_ACTION_RATE:.0%}.",
                )
            )
        offer_rate = _rate(window, Action.OFFER_PLAN)
        if offer_rate > MAX_OFFER_PLAN_RATE:
            found.append(
                PauseCondition(
                    ReasonCode.OFFER_PLAN_RATE_BREACH,
                    PauseScope.AGENT,
                    f"offer_plan rate {offer_rate:.0%} over the last {len(window)} cases "
                    f"exceeds {MAX_OFFER_PLAN_RATE:.0%}.",
                )
            )

    blocks = trailing_blocks(ctx.recent_decisions)
    if blocks >= MAX_CONSECUTIVE_BLOCKS:
        found.append(
            PauseCondition(
                ReasonCode.REPEATED_BLOCKS,
                PauseScope.AGENT,
                f"{blocks} consecutive guardrail BLOCKs.",
            )
        )
    return found


def _block(code: ReasonCode, detail: str, ctx: GuardrailContext) -> GuardrailResult:
    pause = None
    if trailing_blocks(ctx.recent_decisions) + 1 >= MAX_CONSECUTIVE_BLOCKS:
        pause = PauseCondition(
            ReasonCode.REPEATED_BLOCKS,
            PauseScope.AGENT,
            f"This is guardrail BLOCK number {MAX_CONSECUTIVE_BLOCKS} in a row.",
        )
    return GuardrailResult(Verdict.BLOCK, code, detail, pause=pause)


def _hold(
    code: ReasonCode, detail: str, ctx: GuardrailContext, pause: PauseCondition | None = None
) -> GuardrailResult:
    return GuardrailResult(
        Verdict.HOLD,
        code,
        detail,
        pause=pause,
        escalation_owner=ESCALATION_OWNER,
        decide_by=ctx.now + REVIEW_SLA,
    )


def _check_well_formed(proposal: Proposal, ctx: GuardrailContext) -> GuardrailResult | None:
    if proposal.action not in _ACTIONS:
        return _block(
            ReasonCode.OUT_OF_MANDATE_ACTION,
            f"'{proposal.action}' is not in the action set.",
            ctx,
        )
    if proposal.case_id != ctx.case_id:
        return _block(
            ReasonCode.CASE_MISMATCH,
            f"Proposal is for {proposal.case_id}, context is for {ctx.case_id}.",
            ctx,
        )
    if not proposal.rationale.strip():
        return _block(ReasonCode.MISSING_RATIONALE, "Every proposal needs a rationale.", ctx)
    if proposal.action == Action.OFFER_PLAN:
        if proposal.plan is None:
            return _block(
                ReasonCode.MISSING_PLAN_TERMS, "offer_plan was proposed without terms.", ctx
            )
        if proposal.plan.term_extension_months < 1:
            return _block(
                ReasonCode.MALFORMED_PLAN_TERMS,
                "A revised plan must extend the term by at least 1 month.",
                ctx,
            )
    return None


def _apply_action_rule(proposal: Proposal, ctx: GuardrailContext) -> GuardrailResult:
    action = Action(proposal.action)
    if action is Action.ESCALATE_COLLECTIONS:
        return _hold(
            ReasonCode.HIGH_RISK_ACTION,
            f"Collections escalation always waits for the {ESCALATION_OWNER}.",
            ctx,
        )
    if action is Action.HOLD_FOR_REVIEW:
        return _hold(ReasonCode.AGENT_ABSTAINED, "The agent abstained on an ambiguous case.", ctx)
    if action is Action.OFFER_PLAN:
        assert proposal.plan is not None
        plan = proposal.plan
        if plan.fee_waiver or plan.term_extension_months > MAX_PLAN_EXTENSION_MONTHS:
            return _hold(
                ReasonCode.PLAN_OUT_OF_BOUNDS,
                f"Plan extends {plan.term_extension_months} months"
                f"{' with a fee waiver' if plan.fee_waiver else ''}; auto bounds are "
                f"≤ {MAX_PLAN_EXTENSION_MONTHS} months and no fee waiver.",
                ctx,
            )
        return GuardrailResult(
            Verdict.ALLOW,
            ReasonCode.PLAN_WITHIN_BOUNDS,
            f"Plan extends {plan.term_extension_months} months with no fee waiver.",
        )
    if action is Action.NUDGE:
        if ctx.nudges_today_for_customer >= NUDGES_PER_CUSTOMER_PER_DAY:
            return _hold(
                ReasonCode.NUDGE_CAP_REACHED,
                f"{ctx.customer_ref} already had {ctx.nudges_today_for_customer} nudge(s) today.",
                ctx,
            )
        return GuardrailResult(
            Verdict.ALLOW, ReasonCode.NUDGE_WITHIN_CAP, "Reminder is within the daily cap."
        )
    if ctx.amount_overdue > 0:
        return _hold(
            ReasonCode.NO_ACTION_WITH_ARREARS,
            f"The record still shows ${ctx.amount_overdue:,.2f} overdue; closing the case "
            "needs a human.",
            ctx,
        )
    return GuardrailResult(
        Verdict.ALLOW, ReasonCode.NO_ACTION_LOGGED, f"No action: {proposal.rationale.strip()}"
    )


def evaluate(proposal: Proposal, ctx: GuardrailContext) -> GuardrailResult:
    """Return the single verdict for ``proposal`` under the mandate."""
    blocked = _check_well_formed(proposal, ctx)
    if blocked is not None:
        return blocked

    if ctx.agent_paused:
        return _hold(
            ReasonCode.AGENT_PAUSED, "The agent is paused until a human clears the pause.", ctx
        )
    conditions = detect_pause_conditions(ctx)
    if conditions:
        first = conditions[0]
        return _hold(first.reason_code, first.detail, ctx, pause=first)

    if ctx.merchant_under_review:
        condition = PauseCondition(
            ReasonCode.MERCHANT_UNDER_REVIEW,
            PauseScope.MERCHANT,
            f"Merchant {ctx.merchant_id} is under review; all its cases hold.",
        )
        return _hold(condition.reason_code, condition.detail, ctx, pause=condition)

    return _apply_action_rule(proposal, ctx)
