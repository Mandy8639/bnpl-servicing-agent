"""Demo API. Every route is read-only except ``POST /cases/{id}/review``, which needs a
named human actor. Eval numbers come only from committed reports under evals/reports.

    uvicorn bnpl.api:app   (or: bnpl serve)
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bnpl.config import db_path
from bnpl.controls import build_digest
from bnpl.governed import Decision, GovernedLedger
from bnpl.mandate import (
    CONTROL_MIN_SAMPLE,
    CONTROL_WINDOW,
    ESCALATION_OWNER,
    MANDATE,
    MAX_AUTO_ACTION_RATE,
    MAX_CONSECUTIVE_BLOCKS,
    MAX_OFFER_PLAN_RATE,
    REPORT_HIGH_DELINQUENCY,
    REPORT_MEDIUM_DELINQUENCY,
    REVIEW_SLA,
)
from bnpl.merchant_report import decide_report, held_reports, merchant_metrics, risk_band
from bnpl.review import ReviewError, approve, case_status, held_cases, reject

REPORTS_DIR = Path(__file__).resolve().parents[1] / "evals" / "reports"
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"

app = FastAPI(
    title="bnpl-servicing-agent",
    description="A bounded BNPL servicing agent on synthetic data, with a human at the gate.",
    version="0.1.0",
)


app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "index.html")


@contextmanager
def ledger() -> Iterator[GovernedLedger]:
    try:
        with GovernedLedger(db_path()) as opened:
            yield opened
    except FileNotFoundError as exc:
        raise HTTPException(503, "ledger not seeded; run `bnpl seed`") from exc


def _decisions_by_case(opened: GovernedLedger) -> dict[str, list[Decision]]:
    grouped: dict[str, list[Decision]] = {}
    for decision in opened.decisions():
        grouped.setdefault(decision.case_id, []).append(decision)
    return grouped


@app.get("/cases")
def list_cases(status: str | None = None) -> list[dict[str, Any]]:
    with ledger() as opened:
        trail = _decisions_by_case(opened)
        rows = []
        for case in opened.cases():
            decisions = trail.get(case.case_id, [])
            last = decisions[-1] if decisions else None
            row = {
                "case_id": case.case_id,
                "case_type": case.case_type,
                "merchant_name": case.merchant_name,
                "sector": case.sector,
                "merchant_under_review": case.merchant_under_review,
                "amount_overdue": case.amount_overdue,
                "days_overdue": case.days_overdue,
                "risk_flags": list(case.risk_flags),
                "loan_status": case.loan_status,
                "status": case_status(decisions),
                "proposed_action": last.proposed_action if last else None,
                "verdict": last.guardrail_verdict if last else None,
                "final_action": last.final_action if last else None,
                "actor": last.actor if last else None,
            }
            if status is None or row["status"] == status:
                rows.append(row)
        return rows


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    with ledger() as opened:
        case = opened.case(case_id)
        if case is None:
            raise HTTPException(404, f"no case {case_id}")
        decisions = opened.decisions(case_id)
        held = next((h for h in held_cases(opened) if h.case_id == case_id), None)
        return {
            "case": asdict(case),
            "payments": [asdict(p) for p in opened.payments(case.loan_id)],
            "decisions": [asdict(d) for d in decisions],
            "status": case_status(decisions),
            "review": asdict(held) if held else None,
        }


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    actor: str = Field(min_length=1, description="The human making the decision.")
    reason: str = Field(min_length=1)
    action: str | None = Field(None, description="Required when the agent abstained.")


@app.post("/cases/{case_id}/review")
def review_case(case_id: str, body: ReviewRequest) -> dict[str, Any]:
    with ledger() as opened:
        if opened.case(case_id) is None:
            raise HTTPException(404, f"no case {case_id}")
        try:
            if body.decision == "approve":
                decision = approve(
                    opened, case_id, actor=body.actor, reason=body.reason, action=body.action
                )
            else:
                decision = reject(opened, case_id, actor=body.actor, reason=body.reason)
        except ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "case_id": case_id,
            "status": case_status(opened.decisions(case_id)),
            "decision": asdict(decision),
        }


@app.get("/audit")
def audit_log() -> list[dict[str, Any]]:
    """The append-only audit log, oldest first."""
    with ledger() as opened:
        return [asdict(d) for d in opened.decisions()]


@app.get("/reviews")
def list_reviews() -> list[dict[str, Any]]:
    with ledger() as opened:
        return [asdict(h) for h in held_cases(opened)]


@app.get("/controls/digest")
def controls_digest() -> dict[str, Any]:
    """Read-only snapshot. Breach handling (alert + pause) runs in `bnpl controls digest`."""
    with ledger() as opened:
        return asdict(build_digest(opened))


@app.get("/mandate")
def mandate() -> dict[str, Any]:
    return {
        "actions": [
            {
                "action": r.action.value,
                "meaning": r.meaning,
                "risk": r.risk.value,
                "handling": r.handling,
            }
            for r in MANDATE
        ],
        "escalation_owner": ESCALATION_OWNER,
        "review_sla_hours": REVIEW_SLA.total_seconds() / 3600,
        "thresholds": {
            "max_auto_action_rate": MAX_AUTO_ACTION_RATE,
            "max_offer_plan_rate": MAX_OFFER_PLAN_RATE,
            "max_consecutive_blocks": MAX_CONSECUTIVE_BLOCKS,
            "control_window": CONTROL_WINDOW,
            "control_min_sample": CONTROL_MIN_SAMPLE,
            "freshness_max_age_hours": 24,
        },
        "report_thresholds": {
            "watch_delinquency_pct": REPORT_MEDIUM_DELINQUENCY * 100,
            "escalation_delinquency_pct": REPORT_HIGH_DELINQUENCY * 100,
        },
    }


def committed_reports(
    reports_dir: Path = REPORTS_DIR, suite: str = "servicing"
) -> list[dict[str, Any]]:
    reports = []
    for path in sorted(reports_dir.glob("*.json")):
        data = json.loads(path.read_text())
        if data.get("suite", "servicing") == suite:
            reports.append({"file": path.name, **data})
    return sorted(reports, key=lambda r: (r["started_at"], r["file"]))


@app.get("/evals/latest")
def latest_eval(mode: Literal["stub", "live"] = Query("stub")) -> dict[str, Any]:
    """The newest committed report for ``mode``, plus the list of all committed runs."""
    reports = committed_reports()
    matching = [r for r in reports if r["mode"] == mode]
    if not matching:
        raise HTTPException(404, f"no committed {mode} report")
    return {
        "report": matching[-1],
        "history": [
            {
                "file": r["file"],
                "mode": r["mode"],
                "started_at": r["started_at"],
                "prompt_version": r.get("prompt_version", "v1"),
                "action_accuracy": r["metrics"]["action_accuracy"],
                "false_auto_count": r["metrics"]["false_auto_count"],
                "gate_passed": r["metrics"]["gate_passed"],
            }
            for r in reports
        ],
    }


# --- merchant reports -------------------------------------------------------------------


def _report_reviews(opened: GovernedLedger) -> dict[int, dict[str, Any]]:
    """Owner and deadline for each held report, from the exception inventory."""
    reviews: dict[int, dict[str, Any]] = {}
    for record in opened.exceptions():
        try:
            detail = json.loads(record.detail)
        except json.JSONDecodeError:
            continue
        if isinstance(detail, dict) and "report_id" in detail:
            reviews[int(detail["report_id"])] = detail
    return reviews


def _report_row(row: dict[str, Any], reviews: dict[int, dict[str, Any]]) -> dict[str, Any]:
    review = reviews.get(row["id"], {})
    return {
        **{k: v for k, v in row.items() if k != "body"},
        **json.loads(row["body"]),
        "escalation_owner": review.get("escalation_owner"),
        "decide_by": review.get("decide_by"),
        "guardrail_detail": review.get("guardrail_detail", row["reason"]),
    }


@app.get("/merchants")
def list_merchants() -> list[dict[str, Any]]:
    """Each merchant's governed metrics, the policy's risk band, and its latest report."""
    with ledger() as opened:
        reports = opened.merchant_reports()
        reviews = _report_reviews(opened)
        latest = {r["merchant_id"]: r for r in reports}
        rows = []
        for merchant in opened.merchants():
            m = merchant_metrics(opened, merchant.id)
            if m is None:
                continue
            risk, action = risk_band(m)
            report = latest.get(merchant.id)
            rows.append(
                {
                    **m.payload(opened.meta("as_of") or ""),
                    "merchant_id": merchant.id,
                    "under_review": m.under_review,
                    "policy_risk": risk.value,
                    "policy_action": action.value,
                    "latest_report": _report_row(report, reviews) if report else None,
                }
            )
        return rows


@app.get("/merchant-reports")
def list_merchant_reports(status: str | None = None) -> list[dict[str, Any]]:
    """Every report entry, newest first; ``status=held`` gives the reports waiting on a human."""
    with ledger() as opened:
        reviews = _report_reviews(opened)
        rows = held_reports(opened) if status == "held" else opened.merchant_reports()
        if status not in (None, "held"):
            rows = [r for r in rows if r["status"] == status]
        return [_report_row(r, reviews) for r in reversed(rows)]


class ReportReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    actor: str = Field(min_length=1, description="The human making the decision.")
    reason: str = Field(min_length=1)


@app.post("/merchant-reports/{report_id}/review")
def review_merchant_report(report_id: int, body: ReportReviewRequest) -> dict[str, Any]:
    with ledger() as opened:
        try:
            new_id = decide_report(
                opened,
                report_id,
                approve=body.decision == "approve",
                actor=body.actor,
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        row = next(r for r in opened.merchant_reports() if r["id"] == new_id)
        return _report_row(row, _report_reviews(opened))


@app.get("/evals/merchant/latest")
def latest_merchant_eval() -> dict[str, Any]:
    """The newest committed stub report for the merchant-report agent."""
    reports = [r for r in committed_reports(suite="merchant") if r["mode"] == "stub"]
    if not reports:
        raise HTTPException(404, "no committed merchant-report eval")
    latest = reports[-1]
    return {"file": latest["file"], "model": latest["model"], "metrics": latest["metrics"]}
