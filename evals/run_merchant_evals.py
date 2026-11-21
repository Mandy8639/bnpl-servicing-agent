"""Run the merchant-report agent over its frozen 12-case golden set.

    python -m evals.run_merchant_evals                      # stub: replay the recorded run, $0
    python -m evals.run_merchant_evals --mode live --record # live: call the model, record

Gate: no report that must hold may publish unattended (false-publish = 0).
"""

import argparse
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bnpl.config import load_env_file
from bnpl.governed import GovernedLedger
from bnpl.merchant_report import ClaudeReportWriter, RecordedReportWriter, ReportWriter, run_report
from bnpl.proposers import DEFAULT_MODEL, Cassette, SpendLedger
from evals.merchant_fixtures import build_merchant_ledger
from evals.report import next_report_stem
from evals.run_evals import REPORTS_DIR, GoldenSetChangedError

EVALS_DIR = Path(__file__).parent
GOLDEN_PATH = EVALS_DIR / "merchant_golden.json"
LOCK_PATH = EVALS_DIR / "merchant_golden.sha256"
CASSETTE_DIR = EVALS_DIR / "cassettes"
EVAL_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
GROUNDING_CODES = {"UNSUPPORTED_NUMBER", "CUSTOMER_DATA_IN_REPORT"}


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_frozen(golden: Path | None = None, lock: Path | None = None) -> str:
    golden, lock = golden or GOLDEN_PATH, lock or LOCK_PATH
    digest = _sha256(golden)
    if not lock.exists() or lock.read_text().strip() != digest:
        raise GoldenSetChangedError("merchant_golden.json is not frozen or has changed")
    return digest


class FixedReportWriter:
    """Stands in for a faulty model in control scenarios."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def draft(self, key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        return self._raw, 0.0


def run_case(golden: dict[str, Any], writer: ReportWriter, workdir: Path) -> dict[str, Any]:
    path = workdir / f"{golden['id']}.db"
    merchant_id = build_merchant_ledger(path, golden, EVAL_NOW)
    injected = golden["setup"]["injected_report"]
    with GovernedLedger(path, clock=lambda: EVAL_NOW) as ledger:
        outcome = run_report(
            ledger,
            merchant_id,
            FixedReportWriter(injected) if injected else writer,
            key=golden["id"],
            clock=lambda: EVAL_NOW,
        )
    control = golden["control"]
    published = outcome.status == "published"
    return {
        "id": golden["id"],
        "category": golden["category"],
        "injected": injected is not None,
        "expected_risk": golden["expected_risk"],
        "expected_action": golden["expected_action"],
        "must_hold": golden["must_hold"],
        "expectation": golden["rationale"],
        "risk_level": outcome.draft.risk_level,
        "recommended_action": outcome.draft.recommended_action,
        "headline": outcome.draft.headline,
        "findings": list(outcome.draft.findings),
        "verdict": outcome.verdict.verdict.value,
        "reason_code": outcome.verdict.reason_code,
        "guardrail_detail": outcome.verdict.detail,
        "status": outcome.status,
        "risk_correct": outcome.draft.risk_level == golden["expected_risk"],
        "action_correct": outcome.draft.recommended_action == golden["expected_action"],
        "false_publish": published and golden["must_hold"],
        "control_detected": (
            None
            if control is None
            else (not published and outcome.verdict.reason_code == control["expected_reason_code"])
        ),
        "cost_usd": outcome.cost_usd,
    }


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def metrics(rows: list[dict[str, Any]], runtime: float) -> dict[str, Any]:
    model = [r for r in rows if not r["injected"]]
    controls = [r for r in rows if r["control_detected"] is not None]
    false_publish = [r["id"] for r in rows if r["false_publish"]]
    grounding = [r["id"] for r in model if r["reason_code"] in GROUNDING_CODES]
    return {
        "cases": len(rows),
        "model_written_reports": len(model),
        "risk_accuracy": _rate(sum(r["risk_correct"] for r in model), len(model)),
        "action_accuracy": _rate(sum(r["action_correct"] for r in model), len(model)),
        "action_correct": sum(r["action_correct"] for r in model),
        "false_publish_count": len(false_publish),
        "false_publish_cases": false_publish,
        "control_detection_rate": _rate(
            sum(r["control_detected"] for r in controls), len(controls)
        ),
        "control_cases": len(controls),
        "grounding_blocks": len(grounding),
        "grounding_block_cases": grounding,
        "published": sum(r["status"] == "published" for r in rows),
        "held": sum(r["status"] == "held" for r in rows),
        "blocked": sum(r["status"] == "blocked" for r in rows),
        "llm_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "runtime_seconds": runtime,
        "gate_passed": not false_publish,
    }


def run_golden_set(writer: ReportWriter, *, mode: str, model: str, source: str) -> dict[str, Any]:
    digest = verify_frozen()
    cases = json.loads(GOLDEN_PATH.read_text())["cases"]
    started = datetime.now(UTC)
    clock = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="bnpl-merchant-eval-") as tmp:
        rows = [run_case(golden, writer, Path(tmp)) for golden in cases]
    return {
        "suite": "merchant",
        "mode": mode,
        "model": model,
        "proposer_source": source,
        "golden_sha256": digest,
        "started_at": started.isoformat(timespec="seconds"),
        "metrics": metrics(rows, round(time.perf_counter() - clock, 3)),
        "results": rows,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def to_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        f"# Merchant-report eval · {report['started_at'][:10]} · {report['mode']} mode",
        "",
        f"- Model: `{report['model']}` · proposals from: {report['proposer_source']}",
        f"- Golden set sha256: `{report['golden_sha256'][:16]}…`",
        f"- Gate (false-publish = 0): **{'PASS' if m['gate_passed'] else 'FAIL'}**",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Risk-level accuracy | {_pct(m['risk_accuracy'])} (of {m['model_written_reports']}) |",
        f"| Action accuracy | {_pct(m['action_accuracy'])} ({m['action_correct']}/"
        f"{m['model_written_reports']}) |",
        f"| False-publish | {m['false_publish_count']} |",
        f"| Control detection | {_pct(m['control_detection_rate'])} (of {m['control_cases']}) |",
        f"| Grounding blocks on model reports | {m['grounding_blocks']} |",
        f"| Published / held / blocked | {m['published']} / {m['held']} / {m['blocked']} |",
        f"| LLM cost | ${m['llm_cost_usd']:.4f} |",
        "",
        "## Reports",
        "",
        "| Case | Expected | Report | Verdict | Reason | Headline |",
        "|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        lines.append(
            f"| {r['id']} | {r['expected_risk']} / {r['expected_action']} | "
            f"{r['risk_level']} / {r['recommended_action']}"
            f"{' (injected)' if r['injected'] else ''} | {r['verdict']} | {r['reason_code']} | "
            f"{r['headline'].replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def write_reports(report: dict[str, Any], reports_dir: Path = REPORTS_DIR) -> list[Path]:
    stem = next_report_stem(reports_dir, report["started_at"][:10], f"merchant-{report['mode']}")
    json_path, md_path = reports_dir / f"{stem}.json", reports_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(to_markdown(report))
    return [json_path, md_path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the merchant-report golden-set eval.")
    parser.add_argument("--mode", choices=["stub", "live"], default="stub")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args(argv)
    load_env_file()

    if args.freeze:
        if LOCK_PATH.exists():
            print(f"{LOCK_PATH} already exists; the golden set stays frozen.", file=sys.stderr)
            return 1
        LOCK_PATH.write_text(_sha256(GOLDEN_PATH) + "\n")
        print(f"Froze merchant golden set at {_sha256(GOLDEN_PATH)}")
        return 0

    cassette_path = CASSETTE_DIR / f"merchant-{DEFAULT_MODEL}.json"
    recorder: Cassette | None = None
    if args.mode == "live":
        import os

        cap = float(os.environ.get("BNPL_DAILY_SPEND_CAP_USD", "1.00"))
        spend = SpendLedger(Path(os.environ.get("BNPL_SPEND_PATH", "data/spend.json")), cap)
        model = os.environ.get("BNPL_MODEL", DEFAULT_MODEL)
        if args.record:
            recorder = Cassette(
                source=f"recorded live from {model} on {datetime.now(UTC).date().isoformat()}"
            )
        writer: ReportWriter = ClaudeReportWriter(spend=spend, model=model, record_to=recorder)
        source = f"live:{model}"
    else:
        if not cassette_path.exists():
            print(f"No cassette at {cassette_path}. Record one with --mode live --record.")
            return 2
        cassette = Cassette.load(cassette_path)
        writer, model, source = (
            RecordedReportWriter(cassette),
            DEFAULT_MODEL,
            f"stub:{cassette.source}",
        )

    report = run_golden_set(writer, mode=args.mode, model=model, source=source)
    if recorder is not None:
        recorder.save(cassette_path)
        print(f"Recorded {len(recorder.responses)} reports to {cassette_path}")
    m = report["metrics"]
    print(
        f"merchant {args.mode} · risk {_pct(m['risk_accuracy'])} · action "
        f"{_pct(m['action_accuracy'])} · false-publish {m['false_publish_count']} · controls "
        f"{_pct(m['control_detection_rate'])} · grounding blocks {m['grounding_blocks']} · "
        f"cost ${m['llm_cost_usd']:.4f}"
    )
    for path in write_reports(report):
        print(f"  wrote {path}")
    return 0 if m["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
