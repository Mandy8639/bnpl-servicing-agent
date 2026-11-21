"""The second agent: a weekly portfolio-health summary for ONE merchant.

It reads only governed metrics (``v_merchant_metrics``), asks a model to write a short
summary with a risk level and one recommended action, and then a deterministic report
guardrail decides whether it may publish:

* BLOCK when the report states a number that is not in the governed metrics, names a
  customer, or uses a risk level or action outside the report mandate;
* HOLD when the ledger is stale, the merchant is under review, the model understates
  the risk the metrics show, or the action is external or an escalation;
* ALLOW (publish) otherwise.

Reports are append-only. A held report is published only when a named human approves it.
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

import anthropic

from bnpl.governed import GovernedLedger, utc_now
from bnpl.guardrail import Verdict
from bnpl.mandate import (
    AGENT_ACTOR,
    REPORT_DISPUTES_FOR_REVIEW,
    REPORT_FRAUD_FLAGS_FOR_ESCALATION,
    REPORT_HIGH_DELINQUENCY,
    REPORT_HIGH_PLAN_UPTAKE,
    REPORT_MANDATE,
    REPORT_MEDIUM_DELINQUENCY,
    REPORT_OWNER,
    REVIEW_SLA,
    ReportAction,
    RiskLevel,
)
from bnpl.proposers import DEFAULT_MODEL, PRICES_PER_MTOK, Cassette, SpendLedger

RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
HELD_ACTIONS = {ReportAction.REVIEW_WITH_MERCHANT, ReportAction.ESCALATE_TO_RISK}
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
CUSTOMER_REF = re.compile(r"\bcust-\d+\b", re.IGNORECASE)
MAX_OUTPUT_TOKENS = 2_000


class ReportReason:
    OUT_OF_MANDATE = "OUT_OF_MANDATE_REPORT"
    MISSING_FINDINGS = "MISSING_FINDINGS"
    UNSUPPORTED_NUMBER = "UNSUPPORTED_NUMBER"
    CUSTOMER_DATA = "CUSTOMER_DATA_IN_REPORT"
    DATA_STALE = "DATA_STALE"
    MERCHANT_UNDER_REVIEW = "MERCHANT_UNDER_REVIEW"
    RISK_UNDERSTATED = "RISK_UNDERSTATED"
    EXTERNAL_REVIEW = "EXTERNAL_REVIEW"
    RISK_ESCALATION = "RISK_ESCALATION"
    PUBLISHED = "REPORT_WITHIN_MANDATE"


@dataclass(frozen=True)
class MerchantMetrics:
    merchant_id: str
    merchant_name: str
    sector: str
    under_review: bool
    loans_total: int
    loans_overdue: int
    loans_charged_off: int
    loans_in_plan: int
    amount_overdue: float
    open_disputes: int
    fraud_flags: int
    exceptions_held: int
    exceptions_blocked: int

    @property
    def delinquency_rate(self) -> float:
        late = self.loans_overdue + self.loans_charged_off
        return late / self.loans_total if self.loans_total else 0.0

    @property
    def plan_uptake(self) -> float:
        return self.loans_in_plan / self.loans_total if self.loans_total else 0.0

    def payload(self, period_end: str) -> dict[str, Any]:
        """Exactly what the model sees. Every number a report may state is in here."""
        return {
            "merchant": self.merchant_name,
            "sector": self.sector,
            "period_end": period_end,
            "loans_total": self.loans_total,
            "loans_overdue": self.loans_overdue,
            "loans_charged_off": self.loans_charged_off,
            "loans_in_plan": self.loans_in_plan,
            "delinquency_rate_pct": round(self.delinquency_rate * 100, 1),
            "plan_uptake_pct": round(self.plan_uptake * 100, 1),
            "amount_overdue_usd": round(self.amount_overdue, 2),
            "open_disputes": self.open_disputes,
            "fraud_flags": self.fraud_flags,
            "exceptions_held": self.exceptions_held,
            "exceptions_blocked": self.exceptions_blocked,
        }


def merchant_metrics(ledger: GovernedLedger, merchant_id: str) -> MerchantMetrics | None:
    fields = ledger.merchant_metrics(merchant_id)
    if fields is None:
        return None
    fields["under_review"] = bool(fields["under_review"])
    return MerchantMetrics(**fields)


def risk_band(m: MerchantMetrics) -> tuple[RiskLevel, ReportAction]:
    """The deterministic policy: what the metrics say, independent of any model."""
    if (
        m.delinquency_rate >= REPORT_HIGH_DELINQUENCY
        or m.fraud_flags >= REPORT_FRAUD_FLAGS_FOR_ESCALATION
    ):
        return RiskLevel.HIGH, ReportAction.ESCALATE_TO_RISK
    medium = m.delinquency_rate >= REPORT_MEDIUM_DELINQUENCY or m.plan_uptake > (
        REPORT_HIGH_PLAN_UPTAKE
    )
    if m.open_disputes >= REPORT_DISPUTES_FOR_REVIEW:
        return RiskLevel.MEDIUM, ReportAction.REVIEW_WITH_MERCHANT
    if medium:
        return RiskLevel.MEDIUM, ReportAction.MONITOR
    return RiskLevel.LOW, ReportAction.NO_CHANGE


# --- the report guardrail -------------------------------------------------------------


@dataclass(frozen=True)
class ReportDraft:
    headline: str
    findings: tuple[str, ...]
    risk_level: str
    recommended_action: str

    @classmethod
    def parse(cls, raw: object) -> "ReportDraft":
        if not isinstance(raw, dict):
            return cls("", (), "invalid_model_output", "invalid_model_output")
        findings = raw.get("findings")
        return cls(
            headline=str(raw.get("headline", "")),
            findings=tuple(str(f) for f in findings) if isinstance(findings, list) else (),
            risk_level=str(raw.get("risk_level", "invalid_model_output")),
            recommended_action=str(raw.get("recommended_action", "invalid_model_output")),
        )

    def text(self) -> str:
        return " ".join([self.headline, *self.findings])


@dataclass(frozen=True)
class ReportContext:
    metrics: MerchantMetrics
    period_end: str
    data_fresh: bool
    now: datetime


@dataclass(frozen=True)
class ReportVerdict:
    verdict: Verdict
    reason_code: str
    detail: str
    owner: str | None = None
    decide_by: str | None = None


POLICY_NUMBERS: frozenset[float] = frozenset(
    {
        REPORT_MEDIUM_DELINQUENCY * 100,
        REPORT_HIGH_DELINQUENCY * 100,
        REPORT_HIGH_PLAN_UPTAKE * 100,
        float(REPORT_DISPUTES_FOR_REVIEW),
        float(REPORT_FRAUD_FLAGS_FOR_ESCALATION),
    }
)


def grounded_numbers(payload: dict[str, Any]) -> set[float]:
    """Every number a report may state: the governed metrics, the period end's parts, and
    the published policy thresholds (a report may say "below the 5% watch line")."""
    allowed = {round(float(v), 6) for v in payload.values() if isinstance(v, int | float)}
    allowed |= {float(part) for part in str(payload["period_end"]).split("-") if part.isdigit()}
    return allowed | {round(n, 6) for n in POLICY_NUMBERS}


def unsupported_numbers(text: str, payload: dict[str, Any]) -> list[str]:
    allowed = grounded_numbers(payload)
    stripped = ISO_DATE.sub(" ", text)
    found = [n.rstrip(",.") for n in NUMBER.findall(stripped)]
    return [n for n in found if round(float(n.replace(",", "")), 6) not in allowed]


def evaluate_report(draft: ReportDraft, ctx: ReportContext) -> ReportVerdict:
    """One verdict per draft. Checks run in order; the first that fires decides."""

    def hold(code: str, detail: str) -> ReportVerdict:
        deadline = (ctx.now + REVIEW_SLA).isoformat(timespec="seconds")
        return ReportVerdict(Verdict.HOLD, code, detail, REPORT_OWNER, deadline)

    actions = {r.action.value for r in REPORT_MANDATE}
    if draft.risk_level not in {r.value for r in RiskLevel} or draft.recommended_action not in (
        actions
    ):
        return ReportVerdict(
            Verdict.BLOCK,
            ReportReason.OUT_OF_MANDATE,
            f"risk '{draft.risk_level}' / action '{draft.recommended_action}' is not allowed.",
        )
    if not draft.headline.strip() or not draft.findings:
        return ReportVerdict(Verdict.BLOCK, ReportReason.MISSING_FINDINGS, "Report is empty.")
    if CUSTOMER_REF.search(draft.text()):
        return ReportVerdict(
            Verdict.BLOCK, ReportReason.CUSTOMER_DATA, "Report names an individual customer."
        )
    unsupported = unsupported_numbers(draft.text(), ctx.metrics.payload(ctx.period_end))
    if unsupported:
        return ReportVerdict(
            Verdict.BLOCK,
            ReportReason.UNSUPPORTED_NUMBER,
            f"Numbers not in the governed metrics: {', '.join(unsupported)}.",
        )

    if not ctx.data_fresh:
        return hold(ReportReason.DATA_STALE, "Ledger is older than 24h; metrics may be stale.")
    if ctx.metrics.under_review:
        return hold(ReportReason.MERCHANT_UNDER_REVIEW, "Merchant is under review.")
    band, _ = risk_band(ctx.metrics)
    if RISK_ORDER[RiskLevel(draft.risk_level)] < RISK_ORDER[band]:
        return hold(
            ReportReason.RISK_UNDERSTATED,
            f"Report says {draft.risk_level} risk; the metrics put this merchant at {band}.",
        )
    if draft.recommended_action == ReportAction.ESCALATE_TO_RISK:
        return hold(ReportReason.RISK_ESCALATION, "Escalations wait for the risk lead.")
    if draft.recommended_action == ReportAction.REVIEW_WITH_MERCHANT:
        return hold(ReportReason.EXTERNAL_REVIEW, "A merchant conversation needs a human.")
    return ReportVerdict(Verdict.ALLOW, ReportReason.PUBLISHED, "Grounded and low risk.")


# --- writers: live Claude, or a recorded cassette ---------------------------------------


class ReportWriter(Protocol):
    def draft(self, key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]: ...


REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "risk_level": {"type": "string", "enum": [r.value for r in RiskLevel]},
        "recommended_action": {"type": "string", "enum": [a.value for a in ReportAction]},
    },
    "required": ["headline", "findings", "risk_level", "recommended_action"],
    "additionalProperties": False,
}

REPORT_PROMPT = f"""You write a weekly portfolio-health summary for one merchant of a \
buy-now-pay-later lender, for the lender's internal account team.

Write a one-sentence headline and two to four short findings, then choose a risk level \
and ONE recommended action:
- no_change: low risk, nothing needs doing.
- monitor: delinquency at or above {REPORT_MEDIUM_DELINQUENCY:.0%}, or more than \
{REPORT_HIGH_PLAN_UPTAKE:.0%} of loans on revised plans.
- review_with_merchant: {REPORT_DISPUTES_FOR_REVIEW} or more open disputes that need \
the merchant's input.
- escalate_to_risk: delinquency at or above {REPORT_HIGH_DELINQUENCY:.0%}, or \
{REPORT_FRAUD_FLAGS_FOR_ESCALATION} or more fraud flags.
Use the most severe action that applies. Risk is low for no_change, medium for monitor \
and review_with_merchant, high for escalate_to_risk.

Rules: quote numbers exactly as they appear in the metrics, and never compute or \
round new ones (no differences, ratios or totals of your own). Do not restate the \
date. Never mention individual customers. A deterministic checker rejects any number \
that is not in the metrics."""


class RecordedReportWriter:
    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette

    def draft(self, key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        raw = self._cassette.responses.get(key)
        if raw is None:
            raise LookupError(f"no recorded report for {key}")
        return raw, 0.0


class ClaudeReportWriter:
    def __init__(
        self,
        *,
        spend: SpendLedger,
        model: str = DEFAULT_MODEL,
        client: anthropic.Anthropic | None = None,
        record_to: Cassette | None = None,
    ) -> None:
        if model not in PRICES_PER_MTOK:
            raise ValueError(f"no price for {model}")
        self.model, self._spend = model, spend
        self._client = client or anthropic.Anthropic()
        self._record_to = record_to

    def draft(self, key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        price_in, price_out = PRICES_PER_MTOK[self.model]
        brief = json.dumps(payload, indent=1)
        worst = (len(REPORT_PROMPT) + len(brief)) // 3 * price_in + MAX_OUTPUT_TOKENS * price_out
        self._spend.check(worst / 1_000_000)
        response = self._client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=REPORT_PROMPT,
            messages=[{"role": "user", "content": brief}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": REPORT_SCHEMA},
            },
        )
        usage = response.usage
        cost = (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
        self._spend.add(cost)
        raw: dict[str, Any]
        if response.stop_reason != "end_turn":
            raw = {"error": f"stop_reason={response.stop_reason}"}
        else:
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = {"error": "response was not valid JSON"}
        if self._record_to is not None:
            self._record_to.responses[key] = raw
        return raw, cost


# --- running the agent, and the human gate ----------------------------------------------


@dataclass(frozen=True)
class ReportOutcome:
    report_id: int
    merchant_id: str
    status: str
    draft: ReportDraft
    verdict: ReportVerdict
    cost_usd: float


def _period_end(ledger: GovernedLedger) -> str:
    return ledger.meta("as_of") or ledger.freshness().updated_at.date().isoformat()


def run_report(
    ledger: GovernedLedger,
    merchant_id: str,
    writer: ReportWriter,
    *,
    key: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> ReportOutcome:
    metrics = merchant_metrics(ledger, merchant_id)
    if metrics is None:
        raise LookupError(f"unknown merchant {merchant_id}")
    period_end = _period_end(ledger)
    raw, cost = writer.draft(key or merchant_id, metrics.payload(period_end))
    draft = ReportDraft.parse(raw)
    ctx = ReportContext(metrics, period_end, ledger.freshness().fresh, clock())
    verdict = evaluate_report(draft, ctx)
    status = {Verdict.ALLOW: "published", Verdict.HOLD: "held", Verdict.BLOCK: "blocked"}[
        verdict.verdict
    ]
    report_id = ledger.record_report(
        merchant_id=merchant_id,
        period_end=period_end,
        risk_level=draft.risk_level,
        recommended_action=draft.recommended_action,
        body=json.dumps(asdict(draft)),
        verdict=verdict.verdict,
        reason_code=verdict.reason_code,
        status=status,
        actor=AGENT_ACTOR,
        reason=verdict.detail,
    )
    if verdict.verdict is not Verdict.ALLOW:
        ledger.record_exception(
            kind=verdict.verdict,
            reason_code=verdict.reason_code,
            detail=json.dumps(
                {
                    "merchant_id": merchant_id,
                    "report_id": report_id,
                    "guardrail_detail": verdict.detail,
                    "escalation_owner": verdict.owner,
                    "decide_by": verdict.decide_by,
                }
            ),
        )
    return ReportOutcome(report_id, merchant_id, status, draft, verdict, cost)


def held_reports(ledger: GovernedLedger) -> list[dict[str, Any]]:
    rows = ledger.merchant_reports()
    decided = {r["supersedes"] for r in rows if r["supersedes"] is not None}
    return [r for r in rows if r["status"] == "held" and r["id"] not in decided]


def decide_report(
    ledger: GovernedLedger, report_id: int, *, approve: bool, actor: str, reason: str
) -> int:
    """A named human publishes (approve) or rejects a held report."""
    if not actor.strip() or actor.strip().lower() == AGENT_ACTOR or not reason.strip():
        raise ValueError("a report review needs a named human actor and a reason")
    held = next((r for r in held_reports(ledger) if r["id"] == report_id), None)
    if held is None:
        raise ValueError(f"report {report_id} is not waiting for review")
    return ledger.record_report(
        merchant_id=held["merchant_id"],
        period_end=held["period_end"],
        risk_level=held["risk_level"],
        recommended_action=held["recommended_action"],
        body=held["body"],
        verdict=held["verdict"],
        reason_code=held["reason_code"],
        status="approved" if approve else "rejected",
        actor=actor.strip(),
        reason=reason.strip(),
        supersedes=report_id,
    )
