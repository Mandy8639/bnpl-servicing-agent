"""The servicing agent: a LangGraph loop over one case at a time.

    fetch ──(paused)──────────────────────────────────────► END
      │
      ▼
    propose ──► guardrail ──(ALLOW)──► act ──────────────► END
                    │
                    └──(HOLD / BLOCK)──► hold ───────────► END

* fetch reads the case through the governed ledger and builds the guardrail context
  (freshness, merchant review, nudge count, recent decisions, pause state).
* propose asks the proposer (live Claude, or a recorded cassette) for ONE action.
* guardrail is the deterministic policy check. It never calls a model.
* act records an executed action. Execution is simulated: nothing leaves the process.
* hold records a held or blocked case to the audit log and exception inventory,
  and turns any pause condition into a PAUSE entry plus an alert.

A paused agent proposes nothing and spends nothing until a human clears the pause.
"""

import argparse
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from bnpl.config import db_path, load_env_file
from bnpl.controls import PauseState, pause_state, recent_agent_decisions
from bnpl.governed import Case, GovernedLedger, Payment, utc_now
from bnpl.guardrail import (
    GuardrailContext,
    GuardrailResult,
    PauseScope,
    Verdict,
    evaluate,
)
from bnpl.mandate import AGENT_ACTOR
from bnpl.proposers import (
    Cassette,
    Proposer,
    ProposerResponse,
    RecordedProposer,
    live_proposer_from_env,
)

SAMPLE_CASSETTE = Path("cassettes/ledger-sample.json")
DEMO_CASSETTE = Path("cassettes/ledger-live.json")

log = logging.getLogger("bnpl.agent")


class Status(StrEnum):
    EXECUTED = "executed"
    HELD = "held"
    BLOCKED = "blocked"
    SKIPPED_PAUSED = "skipped_paused"


@dataclass(frozen=True)
class Alert:
    reason_code: str
    scope: str
    detail: str
    case_id: str | None


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    status: Status
    proposed_action: str | None = None
    rationale: str | None = None
    verdict: Verdict | None = None
    reason_code: str | None = None
    final_action: str | None = None
    cost_usd: float = 0.0
    source: str | None = None
    alerts: tuple[Alert, ...] = ()


@dataclass
class RunSummary:
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return round(sum(o.cost_usd for o in self.outcomes), 6)

    @property
    def paused(self) -> bool:
        return any(o.status is Status.SKIPPED_PAUSED for o in self.outcomes)


class AgentState(TypedDict, total=False):
    case_id: str
    case: Case
    payments: list[Payment]
    pause: PauseState
    context: GuardrailContext
    response: ProposerResponse
    result: GuardrailResult
    outcome: CaseOutcome


def log_alert(alert: Alert) -> None:
    log.warning("ALERT %s [%s] %s: %s", alert.reason_code, alert.scope, alert.case_id, alert.detail)


class ServicingAgent:
    def __init__(
        self,
        ledger: GovernedLedger,
        proposer: Proposer,
        *,
        clock: Callable[[], datetime] = utc_now,
        on_alert: Callable[[Alert], None] = log_alert,
    ) -> None:
        self._ledger = ledger
        self._proposer = proposer
        self._clock = clock
        self._on_alert = on_alert
        self._graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("fetch", self._fetch)
        builder.add_node("propose", self._propose)
        builder.add_node("guardrail", self._guardrail)
        builder.add_node("act", self._act)
        builder.add_node("hold", self._hold)
        builder.add_edge(START, "fetch")
        builder.add_conditional_edges(
            "fetch", lambda s: END if "outcome" in s else "propose", ["propose", END]
        )
        builder.add_edge("propose", "guardrail")
        builder.add_conditional_edges(
            "guardrail",
            lambda s: "act" if s["result"].verdict is Verdict.ALLOW else "hold",
            ["act", "hold"],
        )
        builder.add_edge("act", END)
        builder.add_edge("hold", END)
        return builder.compile()

    # --- nodes ---------------------------------------------------------------------

    def _fetch(self, state: AgentState) -> AgentState:
        case_id = state["case_id"]
        case = self._ledger.case(case_id)
        if case is None:
            raise LookupError(f"unknown case {case_id}")
        pause = pause_state(self._ledger.exceptions())
        if pause.paused:
            return {"outcome": CaseOutcome(case_id=case_id, status=Status.SKIPPED_PAUSED)}

        now = self._clock()
        recent = recent_agent_decisions(self._ledger, pause)
        context = GuardrailContext(
            case_id=case_id,
            customer_ref=case.customer_ref,
            merchant_id=case.merchant_id,
            merchant_under_review=case.merchant_under_review,
            data_fresh=self._ledger.freshness().fresh,
            now=now,
            nudges_today_for_customer=self._ledger.nudges_sent_on(
                case.customer_ref, now.date().isoformat()
            ),
            amount_overdue=case.amount_overdue,
            recent_decisions=tuple(recent),
            agent_paused=False,
        )
        return {
            "case": case,
            "payments": self._ledger.payments(case.loan_id),
            "pause": pause,
            "context": context,
        }

    def _propose(self, state: AgentState) -> AgentState:
        return {"response": self._proposer.propose(state["case"], state["payments"])}

    def _guardrail(self, state: AgentState) -> AgentState:
        return {"result": evaluate(state["response"].proposal, state["context"])}

    def _act(self, state: AgentState) -> AgentState:
        response, result = state["response"], state["result"]
        proposal = response.proposal
        self._ledger.record_decision(
            case_id=proposal.case_id,
            proposed_action=proposal.action,
            guardrail_verdict=result.verdict,
            final_action=proposal.action,
            actor=AGENT_ACTOR,
            reason=f"{result.reason_code}: {proposal.rationale}",
        )
        return {"outcome": self._outcome(state, Status.EXECUTED, final_action=proposal.action)}

    def _hold(self, state: AgentState) -> AgentState:
        response, result = state["response"], state["result"]
        proposal = response.proposal
        self._ledger.record_decision(
            case_id=proposal.case_id,
            proposed_action=proposal.action,
            guardrail_verdict=result.verdict,
            final_action=None,
            actor=AGENT_ACTOR,
            reason=f"{result.reason_code}: {proposal.rationale}",
        )
        self._ledger.record_exception(
            kind=result.verdict,
            reason_code=result.reason_code,
            case_id=proposal.case_id,
            detail=json.dumps(
                {
                    "proposed_action": proposal.action,
                    "rationale": proposal.rationale,
                    "guardrail_detail": result.detail,
                    "escalation_owner": result.escalation_owner,
                    "decide_by": (
                        result.decide_by.isoformat(timespec="seconds") if result.decide_by else None
                    ),
                }
            ),
        )
        alerts: list[Alert] = []
        if result.pause is not None:
            alert = Alert(
                reason_code=result.pause.reason_code,
                scope=result.pause.scope,
                detail=result.pause.detail,
                case_id=proposal.case_id,
            )
            if result.pause.scope is PauseScope.AGENT:
                self._ledger.record_exception(
                    kind="PAUSE",
                    reason_code=result.pause.reason_code,
                    case_id=proposal.case_id,
                    detail=result.pause.detail,
                )
            self._on_alert(alert)
            alerts.append(alert)
        status = Status.BLOCKED if result.verdict is Verdict.BLOCK else Status.HELD
        return {"outcome": self._outcome(state, status, alerts=tuple(alerts))}

    def _outcome(
        self,
        state: AgentState,
        status: Status,
        *,
        final_action: str | None = None,
        alerts: tuple[Alert, ...] = (),
    ) -> CaseOutcome:
        response, result = state["response"], state["result"]
        return CaseOutcome(
            case_id=state["case_id"],
            status=status,
            proposed_action=response.proposal.action,
            rationale=response.proposal.rationale,
            verdict=result.verdict,
            reason_code=result.reason_code,
            final_action=final_action,
            cost_usd=response.cost_usd,
            source=response.source,
            alerts=alerts,
        )

    # --- entry points --------------------------------------------------------------

    def run_case(self, case_id: str) -> CaseOutcome:
        return self._graph.invoke({"case_id": case_id})["outcome"]

    def run_queue(self, case_ids: list[str]) -> RunSummary:
        """Work cases in order. Stops at the first case skipped because of a pause."""
        summary = RunSummary()
        for case_id in case_ids:
            outcome = self.run_case(case_id)
            summary.outcomes.append(outcome)
            if outcome.status is Status.SKIPPED_PAUSED:
                break
        return summary


def _print_summary(summary: RunSummary) -> None:
    for o in summary.outcomes:
        if o.status is Status.SKIPPED_PAUSED:
            print(f"{o.case_id}  PAUSED  agent is paused; nothing proposed")
            continue
        print(f"{o.case_id}  {o.verdict:<5}  {o.proposed_action:<20}  {o.reason_code}")
        print(f"            {o.rationale}")
    print(f"\n{len(summary.outcomes)} case(s) · LLM cost ${summary.cost_usd:.2f}")


def open_case_ids(ledger: GovernedLedger, candidates: list[str]) -> list[str]:
    """The candidates that have no decision yet: the queue never re-works a decided case."""
    decided = {d.case_id for d in ledger.decisions()}
    return [case_id for case_id in candidates if case_id not in decided]


def default_cassette() -> Path:
    """The recorded live run over the whole ledger if present, else the 5-case sample."""
    return DEMO_CASSETTE if DEMO_CASSETTE.exists() else SAMPLE_CASSETTE


def record_proposals(ledger: GovernedLedger, proposer: Proposer, case_ids: list[str]) -> float:
    """Ask the proposer about each case without acting. Returns the spend in USD."""
    cost = 0.0
    for case_id in case_ids:
        case = ledger.case(case_id)
        if case is None:
            raise LookupError(f"unknown case {case_id}")
        cost += proposer.propose(case, ledger.payments(case.loan_id)).cost_usd
    return cost


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the servicing agent over ledger cases.")
    parser.add_argument("--db", type=Path, default=db_path())
    parser.add_argument("--mode", choices=["stub", "live"], default="stub")
    parser.add_argument("--cassette", type=Path, default=None)
    parser.add_argument(
        "--record-to", type=Path, default=None, help="live: propose only, save a cassette here"
    )
    parser.add_argument("cases", nargs="*", help="case ids (default: every case in the cassette)")
    args = parser.parse_args(argv)
    load_env_file()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if args.record_to:
        recorder = Cassette(source="")
        live = live_proposer_from_env(record_to=recorder)
        recorder.source = f"recorded live from {live.model} on {datetime.now(UTC).date()}"
        with GovernedLedger(args.db) as ledger:
            case_ids = args.cases or [c.case_id for c in ledger.cases()]
            cost = record_proposals(ledger, live, case_ids)
        recorder.save(args.record_to)
        print(f"Recorded {len(recorder.responses)} proposals to {args.record_to} · ${cost:.4f}")
        return 0

    cassette = Cassette.load(args.cassette or default_cassette())
    if args.mode == "stub":
        proposer: Proposer = RecordedProposer(cassette)
    else:
        proposer = live_proposer_from_env()
    with GovernedLedger(args.db, clock=lambda: datetime.now(UTC)) as ledger:
        case_ids = args.cases or open_case_ids(ledger, sorted(cassette.responses))
        if not case_ids:
            print("No open cases left to work.")
            return 0
        summary = ServicingAgent(ledger, proposer).run_queue(case_ids)
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
