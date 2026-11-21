"""Where proposals come from: a live Claude model, or a recorded cassette replayed for $0.

Both paths return the same raw JSON shape, and both go through ``parse_proposal``, so
stub mode exercises exactly the parsing and guardrail code that live mode does.

Anything the model returns that does not parse becomes the action
``invalid_model_output``, which is outside the mandate and is BLOCKed by the guardrail.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import anthropic

from bnpl.governed import Case, Payment
from bnpl.guardrail import PlanTerms, Proposal
from bnpl.mandate import MANDATE, MAX_PLAN_EXTENSION_MONTHS, Action

INVALID_OUTPUT = "invalid_model_output"
PROMPT_VERSION = "v2"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_DAILY_SPEND_CAP_USD = 1.00
MAX_OUTPUT_TOKENS = 2_000

# USD per million tokens (input, output). Live mode refuses models not listed here,
# because the spend cap cannot be enforced without a price.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}

PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [a.value for a in Action]},
        "rationale": {"type": "string"},
        "term_extension_months": {"type": "integer"},
        "fee_waiver": {"type": "boolean"},
    },
    "required": ["action", "rationale", "term_extension_months", "fee_waiver"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ProposerResponse:
    proposal: Proposal
    raw: dict[str, Any]
    cost_usd: float
    source: str


class Proposer(Protocol):
    def propose(self, case: Case, payments: list[Payment]) -> ProposerResponse: ...


def parse_proposal(case_id: str, raw: object) -> Proposal:
    """Turn the model's JSON into a Proposal. Malformed output becomes an invalid action."""
    if not isinstance(raw, dict):
        return Proposal(case_id, INVALID_OUTPUT, "Model output was not a JSON object.")
    action, rationale = raw.get("action"), raw.get("rationale")
    if not isinstance(action, str) or not isinstance(rationale, str):
        return Proposal(case_id, INVALID_OUTPUT, "Model output missed action or rationale.")
    plan = None
    if action == Action.OFFER_PLAN:
        months = raw.get("term_extension_months")
        if not isinstance(months, int) or isinstance(months, bool):
            return Proposal(case_id, INVALID_OUTPUT, "offer_plan without integer term months.")
        plan = PlanTerms(term_extension_months=months, fee_waiver=bool(raw.get("fee_waiver")))
    return Proposal(case_id=case_id, action=action, rationale=rationale, plan=plan)


# --- Stub mode: replay a cassette -----------------------------------------------------


class CassetteMissError(LookupError):
    """The cassette has no recorded response for this case."""


@dataclass
class Cassette:
    """Recorded model responses keyed by case id, plus where they came from."""

    source: str
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Cassette":
        data = json.loads(path.read_text())
        return cls(source=data["source"], responses=data["responses"])

    def save(self, path: Path) -> None:
        payload = {"source": self.source, "responses": dict(sorted(self.responses.items()))}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


class RecordedProposer:
    """Replays a cassette. Costs nothing and never touches the network."""

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette

    def propose(self, case: Case, payments: list[Payment]) -> ProposerResponse:
        raw = self._cassette.responses.get(case.case_id)
        if raw is None:
            raise CassetteMissError(f"no recorded response for {case.case_id}")
        return ProposerResponse(
            proposal=parse_proposal(case.case_id, raw),
            raw=raw,
            cost_usd=0.0,
            source=f"stub:{self._cassette.source}",
        )


# --- Live mode: call Claude under a daily spend cap -----------------------------------


class SpendCapExceededError(RuntimeError):
    """A live call would push today's spend over ``BNPL_DAILY_SPEND_CAP_USD``."""


class SpendLedger:
    """Today's live-model spend, persisted per UTC date in a small JSON file."""

    def __init__(
        self,
        path: Path,
        cap_usd: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path, self.cap_usd, self._clock = path, cap_usd, clock

    def _load(self) -> dict[str, float]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def spent_today(self) -> float:
        return self._load().get(self._clock().date().isoformat(), 0.0)

    def check(self, worst_case_usd: float) -> None:
        spent = self.spent_today()
        if spent + worst_case_usd > self.cap_usd:
            raise SpendCapExceededError(
                f"spent ${spent:.4f} today; a call costing up to ${worst_case_usd:.4f} "
                f"would exceed the ${self.cap_usd:.2f} cap"
            )

    def add(self, usd: float) -> None:
        data = self._load()
        day = self._clock().date().isoformat()
        data[day] = round(data.get(day, 0.0) + usd, 6)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _mandate_text() -> str:
    lines = [
        f"- {r.action.value} ({r.risk.value} risk): {r.meaning}. {r.handling}." for r in MANDATE
    ]
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You are a servicing agent for a buy-now-pay-later lender. You work one \
payment-exception case at a time and propose exactly ONE action from this action set:

{_mandate_text()}

How to choose:
- nudge: a first missed instalment with no hardship, dispute or fraud signal.
- offer_plan: repeat missed payments or a hardship signal where a longer term would \
help; set term_extension_months (1 to {MAX_PLAN_EXTENSION_MONTHS} stays inside the \
automatic bounds) and fee_waiver.
- escalate_collections: charged-off trajectory, a fraud flag, or arrears beyond what a \
bounded plan can fix. A fraud flag without a hardship signal is escalate_collections: \
fraud review belongs to the human collections team, which is where escalation sends it.
- hold_for_review: the evidence conflicts or is missing, or signals point different \
ways (for example hardship and a fraud flag together). When the customer's account and \
the lender's own records disagree, do not pick a side: hold_for_review. Abstaining is \
correct when unsure.
- no_action: the records agree the payment has landed, or the case is a duplicate or \
false alarm that no one disputes.

A deterministic guardrail checks every proposal and a human reviews risky ones, so \
propose what the case needs, not what you think will be approved. For actions other \
than offer_plan, set term_extension_months to 0 and fee_waiver to false. Keep the \
rationale to one or two sentences that cite the facts you used."""


def case_brief(case: Case, payments: list[Payment]) -> str:
    """The case as the model sees it: governed fields only, as compact JSON."""
    brief = {
        "case_id": case.case_id,
        "case_type": case.case_type,
        "opened_on": case.opened_on,
        "amount_overdue": case.amount_overdue,
        "days_overdue": case.days_overdue,
        "risk_flags": list(case.risk_flags),
        "loan": {
            "principal": case.principal,
            "term_months": case.term_months,
            "apr": case.apr,
            "originated_on": case.originated_on,
            "status": case.loan_status,
        },
        "merchant": {"name": case.merchant_name, "sector": case.sector},
        "payments": [
            {"due": p.due_date, "paid": p.paid_date, "amount": p.amount, "status": p.status}
            for p in payments
        ],
        "comms_history": [
            {"date": c.date, "channel": c.channel, "direction": c.direction, "text": c.text}
            for c in case.comms_history
        ],
    }
    return json.dumps(brief, indent=1)


class ClaudeProposer:
    """Asks a Claude model for a structured proposal, inside the daily spend cap."""

    def __init__(
        self,
        *,
        spend: SpendLedger,
        model: str = DEFAULT_MODEL,
        client: anthropic.Anthropic | None = None,
        record_to: Cassette | None = None,
    ) -> None:
        if model not in PRICES_PER_MTOK:
            raise ValueError(f"no price for {model}; add it to PRICES_PER_MTOK to cap spend")
        self.model = model
        self._spend = spend
        self._client = client or anthropic.Anthropic()
        self._record_to = record_to

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = PRICES_PER_MTOK[self.model]
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000

    def propose(self, case: Case, payments: list[Payment]) -> ProposerResponse:
        brief = case_brief(case, payments)
        estimated_input = (len(SYSTEM_PROMPT) + len(brief)) // 3
        self._spend.check(self._cost(estimated_input, MAX_OUTPUT_TOKENS))

        response = self._client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": brief}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA},
            },
        )
        cost = self._cost(response.usage.input_tokens, response.usage.output_tokens)
        self._spend.add(cost)

        raw: dict[str, Any]
        if response.stop_reason != "end_turn":
            raw = {"error": f"stop_reason={response.stop_reason}"}
        else:
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = {"error": "response was not valid JSON", "text": text}
        if self._record_to is not None:
            self._record_to.responses[case.case_id] = raw
        return ProposerResponse(
            proposal=parse_proposal(case.case_id, raw),
            raw=raw,
            cost_usd=cost,
            source=f"live:{self.model}",
        )


def live_proposer_from_env(
    *, model: str | None = None, record_to: Cassette | None = None
) -> ClaudeProposer:
    """Build the live proposer from ``BNPL_MODEL`` and ``BNPL_DAILY_SPEND_CAP_USD``."""
    cap = float(os.environ.get("BNPL_DAILY_SPEND_CAP_USD", DEFAULT_DAILY_SPEND_CAP_USD))
    spend = SpendLedger(Path(os.environ.get("BNPL_SPEND_PATH", "data/spend.json")), cap)
    return ClaudeProposer(
        spend=spend,
        model=model or os.environ.get("BNPL_MODEL", DEFAULT_MODEL),
        record_to=record_to,
    )
