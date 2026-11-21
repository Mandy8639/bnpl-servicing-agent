"""Day-zero detective controls: pause state, the daily digest, and breach handling.

Pause state comes from the append-only exception inventory. The agent is paused while
the latest agent-wide PAUSE has no CLEAR after it. Only a named human writes a CLEAR
(``clear_pause``), and the CLEAR records the last decision id at that moment, so rate
controls restart from a clean window instead of re-tripping on the history that caused
the pause.

``run_controls`` is the watchdog: it computes the digest, and on any threshold breach
it raises an alert, writes a BREACH to the inventory, and pauses the agent.
"""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from bnpl.governed import ExceptionRecord, GovernedLedger
from bnpl.guardrail import PauseCondition, RecentDecision, Verdict, pause_conditions
from bnpl.mandate import (
    AGENT_ACTOR,
    CONTROL_MIN_SAMPLE,
    CONTROL_WINDOW,
    MAX_AUTO_ACTION_RATE,
    MAX_CONSECUTIVE_BLOCKS,
    MAX_OFFER_PLAN_RATE,
    Action,
)


@dataclass(frozen=True)
class PauseState:
    paused: bool
    reason_code: str | None = None
    since: str | None = None
    window_starts_after_decision: int = 0


def pause_state(exceptions: list[ExceptionRecord]) -> PauseState:
    """Replay the inventory: PAUSE opens a pause, CLEAR closes it."""
    state = PauseState(paused=False)
    for record in exceptions:
        if record.kind == "PAUSE" and not state.paused:
            state = PauseState(
                paused=True,
                reason_code=record.reason_code,
                since=record.timestamp,
                window_starts_after_decision=state.window_starts_after_decision,
            )
        elif record.kind == "CLEAR":
            after = int(json.loads(record.detail or "{}").get("after_decision_id", 0))
            state = PauseState(paused=False, window_starts_after_decision=after)
    return state


def clear_pause(ledger: GovernedLedger, *, actor: str, reason: str) -> int:
    """A human clears the pause. Returns the CLEAR record id."""
    if not actor.strip() or actor.strip().lower() == AGENT_ACTOR or not reason.strip():
        raise ValueError("clearing a pause needs a named human and a reason")
    detail = json.dumps(
        {"actor": actor, "reason": reason, "after_decision_id": ledger.last_decision_id()}
    )
    return ledger.record_exception(kind="CLEAR", reason_code="PAUSE_CLEARED", detail=detail)


def recent_agent_decisions(ledger: GovernedLedger, state: PauseState) -> list[RecentDecision]:
    """The agent's own decisions since the last CLEAR, newest last, capped at the window."""
    return [
        RecentDecision(d.proposed_action, Verdict(d.guardrail_verdict))
        for d in ledger.decisions()
        if d.actor == AGENT_ACTOR and d.id > state.window_starts_after_decision
    ][-CONTROL_WINDOW:]


@dataclass(frozen=True)
class Threshold:
    name: str
    value: float | None
    limit: float
    breached: bool


@dataclass
class Digest:
    """One snapshot of the agent's behaviour against its thresholds."""

    cases_processed: int
    actions: dict[str, int]
    verdicts: dict[str, int]
    hold_rate: float | None
    window_size: int
    thresholds: list[Threshold]
    data_fresh: bool
    data_age_hours: float
    exceptions_total: int
    exceptions_by_kind: dict[str, int]
    open_reviews: int
    paused: bool
    pause_reason: str | None
    pause_since: str | None
    breaches: list[PauseCondition] = field(default_factory=list)


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def build_digest(ledger: GovernedLedger) -> Digest:
    from bnpl.review import held_cases

    decisions = [d for d in ledger.decisions() if d.actor == AGENT_ACTOR]
    exceptions = ledger.exceptions()
    state = pause_state(exceptions)
    window = recent_agent_decisions(ledger, state)
    freshness = ledger.freshness()
    sampled = len(window) >= CONTROL_MIN_SAMPLE
    auto_rate = _rate(sum(d.verdict is Verdict.ALLOW for d in window), len(window))
    offer_rate = _rate(sum(d.proposed_action == Action.OFFER_PLAN for d in window), len(window))
    trailing = 0
    for d in reversed(window):
        if d.verdict is not Verdict.BLOCK:
            break
        trailing += 1
    return Digest(
        cases_processed=len(decisions),
        actions=dict(Counter(d.proposed_action for d in decisions)),
        verdicts=dict(Counter(d.guardrail_verdict for d in decisions)),
        hold_rate=_rate(sum(d.guardrail_verdict == "HOLD" for d in decisions), len(decisions)),
        window_size=len(window),
        thresholds=[
            Threshold(
                "auto_action_rate",
                auto_rate,
                MAX_AUTO_ACTION_RATE,
                sampled and (auto_rate or 0) > MAX_AUTO_ACTION_RATE,
            ),
            Threshold(
                "offer_plan_rate",
                offer_rate,
                MAX_OFFER_PLAN_RATE,
                sampled and (offer_rate or 0) > MAX_OFFER_PLAN_RATE,
            ),
            Threshold(
                "consecutive_blocks",
                float(trailing),
                float(MAX_CONSECUTIVE_BLOCKS),
                trailing >= MAX_CONSECUTIVE_BLOCKS,
            ),
            Threshold(
                "data_age_hours",
                round(freshness.age.total_seconds() / 3600, 2),
                freshness.max_age.total_seconds() / 3600,
                not freshness.fresh,
            ),
        ],
        data_fresh=freshness.fresh,
        data_age_hours=round(freshness.age.total_seconds() / 3600, 2),
        exceptions_total=len(exceptions),
        exceptions_by_kind=dict(Counter(e.kind for e in exceptions)),
        open_reviews=len(held_cases(ledger)),
        paused=state.paused,
        pause_reason=state.reason_code,
        pause_since=state.since,
        breaches=pause_conditions(data_fresh=freshness.fresh, recent_decisions=window),
    )


def _breaches_logged_this_pause(exceptions: list[ExceptionRecord]) -> set[str]:
    """Reason codes already inventoried as BREACH (or as the PAUSE) since the open pause began."""
    logged: set[str] = set()
    for record in exceptions:
        if record.kind == "CLEAR":
            logged = set()
        elif record.kind in ("BREACH", "PAUSE"):
            logged.add(record.reason_code)
    return logged


def run_controls(
    ledger: GovernedLedger, on_alert: Callable[[PauseCondition], None] = lambda _: None
) -> Digest:
    """Compute the digest; on any breach, alert, inventory it, and pause the agent.

    While a pause is open, a breach that is already inventoried still alerts but is
    not logged again, so re-running the digest does not flood the inventory.
    """
    digest = build_digest(ledger)
    already_logged = _breaches_logged_this_pause(ledger.exceptions()) if digest.paused else set()
    for breach in digest.breaches:
        on_alert(breach)
        if breach.reason_code not in already_logged:
            ledger.record_exception(
                kind="BREACH", reason_code=breach.reason_code, detail=breach.detail
            )
    if digest.breaches and not digest.paused:
        first = digest.breaches[0]
        ledger.record_exception(kind="PAUSE", reason_code=first.reason_code, detail=first.detail)
    return build_digest(ledger) if digest.breaches else digest
