"""Write an eval run as JSON (the source of truth), Markdown and a standalone HTML page.

The Markdown and HTML are rendered only from the JSON payload, so every number in
them comes from the committed report.
"""

import html
import json
from pathlib import Path
from typing import Any

CATEGORY_LABELS = {
    "nudge": "Nudge",
    "offer_plan": "Payment plan",
    "escalate": "Collections",
    "ambiguous": "Ambiguous → hold",
    "no_action": "No action",
    "control": "Pause / control",
}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def headline(m: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """(metric, value, sample, meaning) rows, shared by Markdown, HTML and the README."""
    return [
        (
            "Action accuracy",
            _pct(m["action_accuracy"]),
            f"{m['action_correct']}/{m['model_decided_cases']}",
            "Proposed action matches the golden expectation (model-decided cases).",
        ),
        (
            "False-auto rate",
            _pct(m["false_auto_rate"]),
            f"{m['false_auto_count']}/{m['cases']}",
            "A case that must hold was executed unattended. Must be 0.",
        ),
        (
            "Hold accuracy",
            _pct(m["hold_accuracy"]),
            f"of {m['hold_cases']} ambiguous",
            "The agent abstained on ambiguous cases.",
        ),
        (
            "Pause detection",
            _pct(m["pause_detection_rate"]),
            f"of {m['control_cases']} controls",
            "The right control fired and paused or held as specified.",
        ),
        (
            "Guardrail BLOCK rate",
            _pct(m["guardrail_block_rate"]),
            f"of {m['out_of_mandate_proposals']} out-of-mandate",
            "Out-of-mandate proposals that the guardrail blocked.",
        ),
        ("LLM cost", f"${m['llm_cost_usd']:.4f}", "", "Model spend for this run."),
        ("Runtime", f"{m['runtime_seconds']:.1f}s", "", "Wall-clock time for all 48 cases."),
    ]


def next_report_stem(reports_dir: Path, date: str, mode: str) -> str:
    """``<date>-<mode>``, or ``-2``, ``-3``… so a re-run never overwrites a committed report."""
    stem, n = f"{date}-{mode}", 2
    while (reports_dir / f"{stem}.json").exists():
        stem, n = f"{date}-{mode}-{n}", n + 1
    return stem


def to_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        f"# Eval report · {report['started_at'][:10]} · {report['mode']} mode",
        "",
        f"- Model: `{report['model']}`  ",
        f"- Proposals from: {report['proposer_source']}  ",
        f"- Prompt version: {report.get('prompt_version', 'v1')}  ",
        f"- Golden set sha256: `{report['golden_sha256'][:16]}…`  ",
        f"- Gate (false-auto = 0): **{'PASS' if m['gate_passed'] else 'FAIL'}**",
        "",
        "| Metric | Value | Meaning |",
        "|---|---|---|",
        *[f"| {k} | {v}{f' ({n})' if n else ''} | {c} |" for k, v, n, c in headline(m)],
        "",
        "## By category",
        "",
        "| Category | Correct | Accuracy |",
        "|---|---|---|",
        *[
            f"| {CATEGORY_LABELS.get(k, k)} | {v['correct']}/{v['cases']} | {_pct(v['accuracy'])} |"
            for k, v in m["by_category"].items()
        ],
        "",
        "## Cases the agent got wrong",
        "",
    ]
    misses = [r for r in report["results"] if not r["injected"] and not r["action_correct"]]
    if not misses:
        lines.append("None.")
    else:
        lines += [
            "| Case | Expected | Proposed | Verdict | Agent rationale |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {r['id']} | {r['expected_action']} | {r['proposed_action']} | {r['verdict']} | "
            f"{(r['rationale'] or '').replace('|', '/')} |"
            for r in misses
        ]
    return "\n".join(lines) + "\n"


CSS = """
:root{--bg:#0b0f14;--panel:#121821;--line:#1f2a36;--text:#e6edf3;--muted:#8b98a5;
--accent:#3ecf8e;--warn:#f5a524;--bad:#f2555a;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1040px;margin:0 auto;padding:40px 16px 64px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}h2{font-size:16px;margin:36px 0 12px;
color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.meta{color:var(--muted);font-size:13px}.meta code{color:var(--text)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:24px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.tile .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.tile .v{font-size:24px;font-weight:650;margin-top:6px;font-variant-numeric:tabular-nums}
.tile .s{color:var(--muted);font-size:12px;margin-top:2px}
.pass{color:var(--accent)}.fail{color:var(--bad)}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;background:var(--panel)}tr:last-child td{border-bottom:0}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
.ALLOW{background:#13301f;color:var(--accent)}.HOLD{background:#33270f;color:var(--warn)}
.BLOCK{background:#3a1416;color:var(--bad)}.ok{color:var(--accent)}.no{color:var(--bad)}
td.r{color:var(--muted);max-width:420px}
"""


def to_html(report: dict[str, Any]) -> str:
    m = report["metrics"]
    esc = html.escape
    gate = "pass" if m["gate_passed"] else "fail"
    tiles = "".join(
        f'<div class="tile"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div>'
        f'<div class="s">{esc(n)}</div></div>'
        for k, v, n, _ in headline(m)
    )
    rows = "".join(
        "<tr>"
        f"<td>{esc(r['id'])}</td><td>{esc(CATEGORY_LABELS.get(r['category'], r['category']))}</td>"
        f"<td>{esc(r['expected_action'])}</td><td>{esc(r['proposed_action'] or '—')}"
        f"{' <span class=meta>(injected)</span>' if r['injected'] else ''}</td>"
        f"<td><span class='badge {esc(r['verdict'] or '')}'>{esc(r['verdict'] or '—')}</span></td>"
        f"<td>{esc(r['reason_code'] or '')}</td>"
        f"<td class='{'ok' if r['action_correct'] or r['injected'] else 'no'}'>"
        f"{'✓' if r['action_correct'] else ('—' if r['injected'] else '✗')}</td>"
        f"<td class='r'>{esc(r['rationale'] or '')}</td>"
        "</tr>"
        for r in report["results"]
    )
    categories = "".join(
        f"<tr><td>{esc(CATEGORY_LABELS.get(k, k))}</td><td>{v['correct']}/{v['cases']}</td>"
        f"<td>{esc(_pct(v['accuracy']))}</td></tr>"
        for k, v in m["by_category"].items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Eval report · {esc(report["started_at"][:10])} · {esc(report["mode"])}</title>
<style>{CSS}</style></head><body><main>
<h1>Servicing agent · eval report</h1>
<div class="meta">{esc(report["started_at"])} · {esc(report["mode"])} mode ·
<code>{esc(report["model"] or "")}</code> · {esc(report["proposer_source"])}<br>
golden set <code>{esc(report["golden_sha256"][:16])}…</code> ·
gate (false-auto = 0): <b class="{gate}">{gate.upper()}</b></div>
<div class="tiles">{tiles}</div>
<h2>By category</h2>
<div class="wrap"><table><tr><th>Category</th><th>Correct</th><th>Accuracy</th></tr>
{categories}</table></div>
<h2>All 48 cases</h2>
<div class="wrap"><table><tr><th>Case</th><th>Category</th><th>Expected</th><th>Proposed</th>
<th>Verdict</th><th>Reason</th><th></th><th>Agent rationale</th></tr>{rows}</table></div>
<p class="meta" style="margin-top:28px">Synthetic data. A demonstration project; not
affiliated with any lender.</p>
</main></body></html>
"""


def write_reports(report: dict[str, Any], reports_dir: Path) -> list[Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = next_report_stem(reports_dir, report["started_at"][:10], report["mode"])
    paths = [reports_dir / f"{stem}.{ext}" for ext in ("json", "md", "html")]
    paths[0].write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    paths[1].write_text(to_markdown(report))
    paths[2].write_text(to_html(report))
    return paths
