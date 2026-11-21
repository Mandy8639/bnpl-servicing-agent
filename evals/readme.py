"""Keep the README's numbers in sync with the committed eval reports.

    python -m evals.readme          # rewrite the generated blocks in README.md
    python -m evals.readme --check  # exit 1 if README.md is out of date (used by tests)

Every number in a generated block is read from evals/reports/*.json or from the
mandate module. Nothing between the markers is typed by hand.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from bnpl.governed import FRESHNESS_MAX_AGE
from bnpl.mandate import (
    CONTROL_MIN_SAMPLE,
    CONTROL_WINDOW,
    ESCALATION_OWNER,
    MANDATE,
    MAX_AUTO_ACTION_RATE,
    MAX_CONSECUTIVE_BLOCKS,
    MAX_OFFER_PLAN_RATE,
    REPORT_MANDATE,
    REVIEW_SLA,
)
from evals.report import headline

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
REPORTS = ROOT / "evals" / "reports"


def _reports(suite: str = "servicing") -> list[tuple[str, dict[str, Any]]]:
    loaded = [(p.name, json.loads(p.read_text())) for p in REPORTS.glob("*.json")]
    picked = [(n, r) for n, r in loaded if r.get("suite", "servicing") == suite]
    return sorted(picked, key=lambda item: (item[1]["started_at"], item[0]))


def numbers_block() -> str:
    name, report = next((n, r) for n, r in reversed(_reports()) if r["mode"] == "stub")
    rows = [
        f"| {k} | **{v}**{f' ({n})' if n else ''} |" for k, v, n, _ in headline(report["metrics"])
    ]
    return "\n".join(
        [
            "| Metric | Result |",
            "|---|---|",
            *rows,
            "",
            f"From [`evals/reports/{name}`](evals/reports/{name}): stub mode replaying a "
            f"recorded live run of `{report['model']}` (prompt {report['prompt_version']}) "
            f"over the {report['metrics']['cases']}-case golden set.",
        ]
    )


def history_block() -> str:
    lines = [
        "| Report | Mode | Prompt | Accuracy | False-auto | Gate |",
        "|---|---|---|---|---|---|",
    ]
    for name, r in _reports():
        m = r["metrics"]
        lines.append(
            f"| [`{name}`](evals/reports/{name}) | {r['mode']} | {r.get('prompt_version', 'v1')} "
            f"| {m['action_accuracy']:.1%} | {m['false_auto_count']} "
            f"| {'pass' if m['gate_passed'] else '**fail**'} |"
        )
    return "\n".join(lines)


def mandate_block() -> str:
    lines = ["| Action | Meaning | Risk | Handling |", "|---|---|---|---|"]
    lines += [
        f"| `{r.action.value}` | {r.meaning} | {r.risk.value} | {r.handling} |" for r in MANDATE
    ]
    hours = int(FRESHNESS_MAX_AGE.total_seconds() // 3600)
    lines += [
        "",
        f"Escalation owner: the {ESCALATION_OWNER}, who has "
        f"{int(REVIEW_SLA.total_seconds() // 3600)} hours to decide each held case. "
        "The agent pauses itself and raises an alert when any of these is true:",
        "",
        f"1. The ledger is more than {hours} hours old.",
        f"2. More than {MAX_AUTO_ACTION_RATE:.0%} of its last {CONTROL_WINDOW} decisions "
        "ran unattended.",
        f"3. More than {MAX_OFFER_PLAN_RATE:.0%} of them were payment plans.",
        "4. A merchant is under review: all of that merchant's cases hold.",
        f"5. The guardrail has blocked it {MAX_CONSECUTIVE_BLOCKS} times in a row.",
        "",
        f"Rate controls start counting once {CONTROL_MIN_SAMPLE} decisions exist since the "
        "last time a human cleared a pause.",
    ]
    return "\n".join(lines)


def merchant_block() -> str:
    lines = [
        "| Report | Mode | Risk accuracy | Action accuracy | False-publish | Controls "
        "| Grounding blocks | Gate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, r in _reports("merchant"):
        m = r["metrics"]
        lines.append(
            f"| [`{name}`](evals/reports/{name}) | {r['mode']} | {m['risk_accuracy']:.1%} "
            f"| {m['action_accuracy']:.1%} ({m['action_correct']}/{m['model_written_reports']}) "
            f"| {m['false_publish_count']} | {m['control_detection_rate']:.0%} "
            f"({m['control_cases']}) | {m['grounding_blocks']} "
            f"| {'pass' if m['gate_passed'] else '**fail**'} |"
        )
    lines += ["", "| Action | Meaning | Risk | Handling |", "|---|---|---|---|"]
    lines += [
        f"| `{r.action.value}` | {r.meaning} | {r.risk.value} | {r.handling} |"
        for r in REPORT_MANDATE
    ]
    return "\n".join(lines)


BLOCKS = {
    "numbers": numbers_block,
    "history": history_block,
    "mandate": mandate_block,
    "merchant": merchant_block,
}


def render(text: str) -> str:
    for name, build in BLOCKS.items():
        pattern = re.compile(
            rf"(<!-- generated:{name} -->\n).*?(<!-- /generated:{name} -->)", re.DOTALL
        )
        match = pattern.search(text)
        if match is None:
            raise ValueError(f"README is missing the generated:{name} markers")
        block = match.group(1) + build() + "\n" + match.group(2)
        text = text[: match.start()] + block + text[match.end() :]
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    current = README.read_text()
    updated = render(current)
    if args.check:
        if updated != current:
            print("README.md is out of date: run `python -m evals.readme`", file=sys.stderr)
            return 1
        return 0
    README.write_text(updated)
    print("README.md generated blocks updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
