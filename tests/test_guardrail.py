"""The guardrail, rule by rule. Each test name states the rule it pins down."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from bnpl.guardrail import (
    GuardrailContext,
    GuardrailResult,
    PauseScope,
    PlanTerms,
    Proposal,
    ReasonCode,
    RecentDecision,
    Verdict,
    detect_pause_conditions,
    evaluate,
    trailing_blocks,
)
from bnpl.mandate import (
    CONTROL_MIN_SAMPLE,
    CONTROL_WINDOW,
    ESCALATION_OWNER,
    MANDATE,
    REVIEW_SLA,
    Action,
)

NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
CTX = GuardrailContext(
    case_id="case-0001",
    customer_ref="cust-0142",
    merchant_id="mer-03",
    merchant_under_review=False,
    data_fresh=True,
    now=NOW,
)
GOOD_PLAN = PlanTerms(term_extension_months=3)

ALLOWED = RecentDecision(Action.NUDGE, Verdict.ALLOW)
HELD = RecentDecision(Action.ESCALATE_COLLECTIONS, Verdict.HOLD)
BLOCKED = RecentDecision("refund_customer", Verdict.BLOCK)
PLAN_ALLOWED = RecentDecision(Action.OFFER_PLAN, Verdict.ALLOW)


def propose(action: str, *, plan: PlanTerms | None = None, rationale: str = "why") -> Proposal:
    return Proposal(case_id=CTX.case_id, action=action, rationale=rationale, plan=plan)


def history(*parts: tuple[RecentDecision, int]) -> tuple[RecentDecision, ...]:
    return tuple(d for decision, n in parts for d in [decision] * n)


def check(proposal: Proposal, **ctx_changes: object) -> GuardrailResult:
    return evaluate(proposal, replace(CTX, **ctx_changes))


# --- Action rules from the mandate table ---------------------------------------------


def test_nudge_runs_unattended_within_the_daily_cap() -> None:
    result = check(propose(Action.NUDGE))
    assert (result.verdict, result.reason_code) == (Verdict.ALLOW, ReasonCode.NUDGE_WITHIN_CAP)
    assert result.escalation_owner is None


def test_second_nudge_to_the_same_customer_on_the_same_day_holds() -> None:
    result = check(propose(Action.NUDGE), nudges_today_for_customer=1)
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.NUDGE_CAP_REACHED)


@pytest.mark.parametrize("months", [1, 2, 3])
def test_payment_plan_inside_bounds_runs_unattended(months: int) -> None:
    result = check(propose(Action.OFFER_PLAN, plan=PlanTerms(term_extension_months=months)))
    assert (result.verdict, result.reason_code) == (Verdict.ALLOW, ReasonCode.PLAN_WITHIN_BOUNDS)


def test_payment_plan_longer_than_three_months_holds() -> None:
    result = check(propose(Action.OFFER_PLAN, plan=PlanTerms(term_extension_months=4)))
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.PLAN_OUT_OF_BOUNDS)


def test_payment_plan_with_a_fee_waiver_holds_even_when_short() -> None:
    plan = PlanTerms(term_extension_months=1, fee_waiver=True)
    result = check(propose(Action.OFFER_PLAN, plan=plan))
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.PLAN_OUT_OF_BOUNDS)


def test_collections_escalation_always_holds_for_the_servicing_lead() -> None:
    result = check(propose(Action.ESCALATE_COLLECTIONS))
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.HIGH_RISK_ACTION)
    assert result.escalation_owner == ESCALATION_OWNER


def test_abstaining_sends_the_case_to_the_review_queue() -> None:
    result = check(propose(Action.HOLD_FOR_REVIEW))
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.AGENT_ABSTAINED)


def test_no_action_runs_unattended_and_logs_its_reason() -> None:
    result = check(propose(Action.NO_ACTION, rationale="Payment landed after the case opened."))
    assert (result.verdict, result.reason_code) == (Verdict.ALLOW, ReasonCode.NO_ACTION_LOGGED)
    assert "Payment landed after the case opened." in result.detail


def test_no_action_holds_while_the_record_still_shows_arrears() -> None:
    result = check(propose(Action.NO_ACTION), amount_overdue=250.0)
    assert (result.verdict, result.reason_code) == (
        Verdict.HOLD,
        ReasonCode.NO_ACTION_WITH_ARREARS,
    )


def test_every_hold_names_an_owner_and_a_deadline() -> None:
    result = check(propose(Action.ESCALATE_COLLECTIONS))
    assert result.escalation_owner == ESCALATION_OWNER
    assert result.decide_by == NOW + REVIEW_SLA


def test_allowed_actions_carry_no_owner_or_deadline() -> None:
    result = check(propose(Action.NUDGE))
    assert result.escalation_owner is None and result.decide_by is None


# --- BLOCK: proposals outside the mandate --------------------------------------------


@pytest.mark.parametrize("action", ["refund_customer", "waive_fees", "write_off", "", "NUDGE"])
def test_actions_outside_the_action_set_are_blocked(action: str) -> None:
    result = check(propose(action))
    assert (result.verdict, result.reason_code) == (Verdict.BLOCK, ReasonCode.OUT_OF_MANDATE_ACTION)


def test_proposal_for_a_different_case_is_blocked() -> None:
    proposal = Proposal(case_id="case-0002", action=Action.NUDGE, rationale="why")
    result = check(proposal)
    assert (result.verdict, result.reason_code) == (Verdict.BLOCK, ReasonCode.CASE_MISMATCH)


@pytest.mark.parametrize("rationale", ["", "   "])
def test_proposal_without_a_rationale_is_blocked(rationale: str) -> None:
    result = check(propose(Action.NUDGE, rationale=rationale))
    assert (result.verdict, result.reason_code) == (Verdict.BLOCK, ReasonCode.MISSING_RATIONALE)


def test_payment_plan_without_terms_is_blocked() -> None:
    result = check(propose(Action.OFFER_PLAN))
    assert (result.verdict, result.reason_code) == (Verdict.BLOCK, ReasonCode.MISSING_PLAN_TERMS)


@pytest.mark.parametrize("months", [0, -2])
def test_payment_plan_that_does_not_extend_the_term_is_blocked(months: int) -> None:
    result = check(propose(Action.OFFER_PLAN, plan=PlanTerms(term_extension_months=months)))
    assert (result.verdict, result.reason_code) == (
        Verdict.BLOCK,
        ReasonCode.MALFORMED_PLAN_TERMS,
    )


def test_blocks_win_over_pauses_so_bad_proposals_are_always_counted() -> None:
    result = check(propose("refund_customer"), agent_paused=True, data_fresh=False)
    assert result.verdict is Verdict.BLOCK


# --- Pause condition 1: governed-data freshness --------------------------------------


def test_stale_ledger_pauses_the_agent() -> None:
    result = check(propose(Action.NUDGE), data_fresh=False)
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.DATA_STALE)
    assert result.pause is not None and result.pause.scope is PauseScope.AGENT


# --- Pause condition 2: auto-action rate ---------------------------------------------


def test_auto_action_rate_above_80_percent_pauses_the_agent() -> None:
    recent = history((ALLOWED, 81), (HELD, 19))
    result = check(propose(Action.NUDGE), recent_decisions=recent)
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.AUTO_RATE_BREACH)


def test_auto_action_rate_of_exactly_80_percent_is_allowed() -> None:
    recent = history((ALLOWED, 80), (HELD, 20))
    assert check(propose(Action.NUDGE), recent_decisions=recent).verdict is Verdict.ALLOW


def test_auto_rate_only_counts_the_last_100_cases() -> None:
    recent = history((ALLOWED, 50), (ALLOWED, 70), (HELD, 30))
    assert len(recent[-CONTROL_WINDOW:]) == CONTROL_WINDOW
    assert check(propose(Action.NUDGE), recent_decisions=recent).verdict is Verdict.ALLOW


def test_rate_controls_wait_for_a_minimum_sample() -> None:
    recent = history((ALLOWED, CONTROL_MIN_SAMPLE - 1))
    assert check(propose(Action.NUDGE), recent_decisions=recent).verdict is Verdict.ALLOW


def test_rate_controls_fire_once_the_minimum_sample_is_reached() -> None:
    recent = history((ALLOWED, CONTROL_MIN_SAMPLE))
    result = check(propose(Action.NUDGE), recent_decisions=recent)
    assert result.reason_code is ReasonCode.AUTO_RATE_BREACH


# --- Pause condition 3: offer_plan rate ----------------------------------------------


def test_offer_plan_rate_above_25_percent_pauses_the_agent() -> None:
    recent = history((PLAN_ALLOWED, 26), (HELD, 74))
    result = check(propose(Action.NUDGE), recent_decisions=recent)
    assert (result.verdict, result.reason_code) == (
        Verdict.HOLD,
        ReasonCode.OFFER_PLAN_RATE_BREACH,
    )


def test_offer_plan_rate_of_exactly_25_percent_is_allowed() -> None:
    recent = history((PLAN_ALLOWED, 25), (HELD, 75))
    assert check(propose(Action.NUDGE), recent_decisions=recent).verdict is Verdict.ALLOW


# --- Pause condition 4: merchant under review ----------------------------------------


@pytest.mark.parametrize("action", [Action.NUDGE, Action.NO_ACTION, Action.OFFER_PLAN])
def test_every_case_of_a_merchant_under_review_holds(action: Action) -> None:
    plan = GOOD_PLAN if action is Action.OFFER_PLAN else None
    result = check(propose(action, plan=plan), merchant_under_review=True)
    assert (result.verdict, result.reason_code) == (
        Verdict.HOLD,
        ReasonCode.MERCHANT_UNDER_REVIEW,
    )
    assert result.pause is not None and result.pause.scope is PauseScope.MERCHANT


def test_merchant_review_pauses_only_that_merchant() -> None:
    assert not detect_pause_conditions(replace(CTX, merchant_under_review=True))


# --- Pause condition 5: three consecutive BLOCKs -------------------------------------


def test_third_consecutive_block_raises_a_pause() -> None:
    result = check(propose("refund_customer"), recent_decisions=history((BLOCKED, 2)))
    assert result.verdict is Verdict.BLOCK
    assert result.pause is not None and result.pause.reason_code is ReasonCode.REPEATED_BLOCKS


def test_second_consecutive_block_does_not_pause_yet() -> None:
    result = check(propose("refund_customer"), recent_decisions=history((BLOCKED, 1)))
    assert result.verdict is Verdict.BLOCK and result.pause is None


def test_after_three_blocks_valid_proposals_hold() -> None:
    result = check(propose(Action.NUDGE), recent_decisions=history((BLOCKED, 3)))
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.REPEATED_BLOCKS)


def test_a_non_block_resets_the_consecutive_count() -> None:
    recent = history((BLOCKED, 2), (HELD, 1))
    assert trailing_blocks(recent) == 0
    assert check(propose("refund_customer"), recent_decisions=recent).pause is None


# --- Paused state and precedence -----------------------------------------------------


@pytest.mark.parametrize("action", [Action.NUDGE, Action.NO_ACTION, Action.ESCALATE_COLLECTIONS])
def test_a_paused_agent_holds_every_valid_proposal(action: Action) -> None:
    result = check(propose(action), agent_paused=True)
    assert (result.verdict, result.reason_code) == (Verdict.HOLD, ReasonCode.AGENT_PAUSED)


def test_agent_wide_pause_reasons_win_over_merchant_review() -> None:
    result = check(propose(Action.NUDGE), data_fresh=False, merchant_under_review=True)
    assert result.reason_code is ReasonCode.DATA_STALE


def test_pause_conditions_are_reported_together_in_mandate_order() -> None:
    recent = history((PLAN_ALLOWED, 30), (ALLOWED, 67), (BLOCKED, 3))
    codes = [
        c.reason_code
        for c in detect_pause_conditions(replace(CTX, data_fresh=False, recent_decisions=recent))
    ]
    assert codes == [
        ReasonCode.DATA_STALE,
        ReasonCode.AUTO_RATE_BREACH,
        ReasonCode.OFFER_PLAN_RATE_BREACH,
        ReasonCode.REPEATED_BLOCKS,
    ]


# --- Properties of the guardrail as a whole ------------------------------------------


def test_no_high_risk_action_is_ever_allowed() -> None:
    contexts = [
        CTX,
        replace(CTX, nudges_today_for_customer=5),
        replace(CTX, recent_decisions=history((HELD, 100))),
    ]
    for ctx in contexts:
        assert evaluate(propose(Action.ESCALATE_COLLECTIONS), ctx).verdict is Verdict.HOLD


def test_same_inputs_always_give_the_same_verdict() -> None:
    proposal = propose(Action.OFFER_PLAN, plan=GOOD_PLAN)
    assert len({evaluate(proposal, CTX) for _ in range(50)}) == 1


def test_mandate_table_covers_every_action() -> None:
    assert {rule.action for rule in MANDATE} == set(Action)


SCENARIOS: list[tuple[Proposal, dict[str, object]]] = [
    (propose("refund_customer"), {}),
    (Proposal(case_id="case-0009", action=Action.NUDGE, rationale="why"), {}),
    (propose(Action.NUDGE, rationale=""), {}),
    (propose(Action.OFFER_PLAN), {}),
    (propose(Action.OFFER_PLAN, plan=PlanTerms(0)), {}),
    (propose(Action.NUDGE), {"agent_paused": True}),
    (propose(Action.NUDGE), {"data_fresh": False}),
    (propose(Action.NUDGE), {"recent_decisions": history((ALLOWED, 100))}),
    (propose(Action.NUDGE), {"recent_decisions": history((PLAN_ALLOWED, 30), (HELD, 70))}),
    (propose(Action.NUDGE), {"recent_decisions": history((BLOCKED, 3))}),
    (propose(Action.NUDGE), {"merchant_under_review": True}),
    (propose(Action.ESCALATE_COLLECTIONS), {}),
    (propose(Action.OFFER_PLAN, plan=PlanTerms(6)), {}),
    (propose(Action.NUDGE), {"nudges_today_for_customer": 1}),
    (propose(Action.HOLD_FOR_REVIEW), {}),
    (propose(Action.NUDGE), {}),
    (propose(Action.OFFER_PLAN, plan=GOOD_PLAN), {}),
    (propose(Action.NO_ACTION), {"amount_overdue": 10.0}),
    (propose(Action.NO_ACTION), {}),
]


def test_every_reason_code_is_reachable_and_exercised() -> None:
    reached = {check(proposal, **changes).reason_code for proposal, changes in SCENARIOS}
    assert reached == set(ReasonCode)
