"""Pause state, derived from the append-only exception inventory.

The agent is paused when the latest agent-wide PAUSE has no CLEAR after it. A CLEAR
is written only by a human (``clear_pause``) and records the last decision id at the
moment of clearing, so rate controls restart from a clean window instead of
re-tripping on the history that caused the pause.
"""

import json
from dataclasses import dataclass

from bnpl.governed import ExceptionRecord, GovernedLedger


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
    if not actor.strip() or not reason.strip():
        raise ValueError("clearing a pause needs a named human and a reason")
    detail = json.dumps(
        {"actor": actor, "reason": reason, "after_decision_id": ledger.last_decision_id()}
    )
    return ledger.record_exception(kind="CLEAR", reason_code="PAUSE_CLEARED", detail=detail)
