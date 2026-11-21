"""``bnpl``: the operator's command line.

bnpl seed [--reset]                         build the synthetic ledger
bnpl run [--mode stub|live] [case ...]      work cases with the agent
bnpl review list                            held cases waiting on a human
bnpl review approve <case> --actor --reason [--action]
bnpl review reject  <case> --actor --reason
bnpl controls digest                        thresholds, inventory, pause state
bnpl controls clear-pause --actor --reason
bnpl evals run [--mode stub|live]           the golden-set eval
bnpl serve [--port]                         API + dashboard
"""

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from bnpl.config import db_path, load_env_file
from bnpl.controls import Digest, clear_pause, run_controls
from bnpl.governed import GovernedLedger
from bnpl.guardrail import PauseCondition
from bnpl.review import ReviewError, approve, held_cases, reject

RED, AMBER, GREEN, DIM, RESET = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def _color(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def cmd_review_list(ledger: GovernedLedger, _: argparse.Namespace) -> int:
    queue = held_cases(ledger)
    if not queue:
        print("Review queue is empty.")
        return 0
    print(f"{len(queue)} case(s) waiting on a human\n")
    for h in queue:
        flags = ", ".join(h.risk_flags) or "no flags"
        print(f"{_color(h.case_id, AMBER)}  {h.proposed_action}  ·  {h.reason_code}")
        print(f"  {h.merchant_name} ({h.sector}) · ${h.amount_overdue:,.2f} overdue · {flags}")
        print(f"  agent: {h.rationale}")
        print(_color(f"  owner: {h.escalation_owner} · decide by {h.decide_by}", DIM))
        print()
    return 0


def cmd_review_decide(ledger: GovernedLedger, args: argparse.Namespace) -> int:
    try:
        if args.decision == "approve":
            decision = approve(
                ledger, args.case_id, actor=args.actor, reason=args.reason, action=args.action
            )
        else:
            decision = reject(ledger, args.case_id, actor=args.actor, reason=args.reason)
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    outcome = f"executed {decision.final_action}" if decision.final_action else "nothing executed"
    past = {"approve": "approved", "reject": "rejected"}[args.decision]
    print(f"{args.case_id}: {past} by {decision.actor}; {outcome}.")
    return 0


def _print_alert(breach: PauseCondition) -> None:
    print(_color(f"ALERT {breach.reason_code}: {breach.detail}", RED))


def print_digest(d: Digest) -> None:
    state = _color("PAUSED", RED) if d.paused else _color("running", GREEN)
    print(f"Agent: {state}" + (f" ({d.pause_reason} since {d.pause_since})" if d.paused else ""))
    print(f"Cases processed: {d.cases_processed} · hold rate {_pct(d.hold_rate)}")
    print(f"Actions: {d.actions or '—'}")
    print(f"Verdicts: {d.verdicts or '—'}")
    print(f"\nThresholds (window: last {d.window_size} agent decisions since last clear)")
    for t in d.thresholds:
        is_rate = t.name.endswith("rate")
        value = "—" if t.value is None else f"{t.value:.0%}" if is_rate else f"{t.value:g}"
        limit = f"{t.limit:.0%}" if is_rate else f"{t.limit:g}"
        mark = _color("BREACH", RED) if t.breached else _color("ok", GREEN)
        print(f"  {t.name:<20} {value!s:>8}  limit {limit!s:>6}  {mark}")
    print(f"\nException inventory: {d.exceptions_total} {d.exceptions_by_kind or ''}")
    print(f"Open reviews: {d.open_reviews}")


def cmd_controls_digest(ledger: GovernedLedger, _: argparse.Namespace) -> int:
    print_digest(run_controls(ledger, on_alert=_print_alert))
    return 0


def cmd_controls_clear(ledger: GovernedLedger, args: argparse.Namespace) -> int:
    try:
        clear_pause(ledger, actor=args.actor, reason=args.reason)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Pause cleared by {args.actor}. Rate controls restart from a fresh window.")
    return 0


def _with_ledger(fn: Callable[[GovernedLedger, argparse.Namespace], int]):
    def run(args: argparse.Namespace) -> int:
        with GovernedLedger(args.db) as ledger:
            return fn(ledger, args)

    return run


def cmd_seed(args: argparse.Namespace) -> int:
    from bnpl.ledger.seed import seed_ledger

    summary = seed_ledger(args.db, reset=args.reset)
    print(f"Seeded {summary.path}: {summary.row_counts}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from bnpl.agent import main as agent_main

    argv = ["--db", str(args.db), "--mode", args.mode, *args.cases]
    return agent_main(argv)


def cmd_evals(args: argparse.Namespace) -> int:
    from evals.run_evals import main as evals_main

    return evals_main(["--mode", args.mode, *(["--record"] if args.record else [])])


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("bnpl.api:app", host=args.host, port=args.port)
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    from bnpl.mcp_server import main as mcp_main

    mcp_main()
    return 0


def cmd_report_draft(ledger: GovernedLedger, args: argparse.Namespace) -> int:
    from bnpl.merchant_report import ClaudeReportWriter, RecordedReportWriter, run_report
    from bnpl.proposers import DEFAULT_MODEL, Cassette, SpendLedger

    if args.mode == "live":
        cap = float(os.environ.get("BNPL_DAILY_SPEND_CAP_USD", "1.00"))
        spend = SpendLedger(Path(os.environ.get("BNPL_SPEND_PATH", "data/spend.json")), cap)
        writer = ClaudeReportWriter(spend=spend, model=os.environ.get("BNPL_MODEL", DEFAULT_MODEL))
    else:
        if not args.cassette.exists():
            print(f"No recorded reports at {args.cassette}; use --mode live.", file=sys.stderr)
            return 2
        writer = RecordedReportWriter(Cassette.load(args.cassette))
    try:
        outcome = run_report(ledger, args.merchant_id, writer)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{outcome.merchant_id}: {outcome.status.upper()} · {outcome.verdict.reason_code}")
    print(f"  {outcome.draft.headline}")
    for finding in outcome.draft.findings:
        print(f"  - {finding}")
    print(f"  risk {outcome.draft.risk_level} · action {outcome.draft.recommended_action}")
    print(_color(f"  {outcome.verdict.detail} · cost ${outcome.cost_usd:.4f}", DIM))
    return 0


def cmd_report_list(ledger: GovernedLedger, _: argparse.Namespace) -> int:
    from bnpl.merchant_report import held_reports

    held = held_reports(ledger)
    if not held:
        print("No merchant reports waiting on a human.")
    for r in held:
        body = json.loads(r["body"])
        print(
            f"report {r['id']}  {r['merchant_id']}  {r['reason_code']}  ({r['recommended_action']})"
        )
        print(f"  {body['headline']}")
    return 0


def cmd_report_decide(ledger: GovernedLedger, args: argparse.Namespace) -> int:
    from bnpl.merchant_report import decide_report

    try:
        decide_report(
            ledger,
            args.report_id,
            approve=args.decision == "approve",
            actor=args.actor,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    past = {"approve": "approved and published", "reject": "rejected"}[args.decision]
    print(f"report {args.report_id}: {past} by {args.actor}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bnpl", description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=db_path(), help="ledger path")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="build the synthetic ledger")
    seed.add_argument("--reset", action="store_true")
    seed.set_defaults(func=cmd_seed)

    run = sub.add_parser("run", help="work cases with the agent")
    run.add_argument("--mode", choices=["stub", "live"], default="stub")
    run.add_argument("cases", nargs="*")
    run.set_defaults(func=cmd_run)

    review = sub.add_parser("review", help="the human review queue").add_subparsers(
        dest="review_command", required=True
    )
    review.add_parser("list", help="held cases").set_defaults(func=_with_ledger(cmd_review_list))
    for decision in ("approve", "reject"):
        p = review.add_parser(decision, help=f"{decision} a held case")
        p.add_argument("case_id")
        p.add_argument("--actor", required=True, help="your name")
        p.add_argument("--reason", required=True)
        if decision == "approve":
            p.add_argument("--action", help="the action to take when the agent abstained")
        p.set_defaults(func=_with_ledger(cmd_review_decide), decision=decision)

    controls = sub.add_parser("controls", help="detective controls").add_subparsers(
        dest="controls_command", required=True
    )
    controls.add_parser("digest", help="run the controls and print the digest").set_defaults(
        func=_with_ledger(cmd_controls_digest)
    )
    clear = controls.add_parser("clear-pause", help="a human clears the auto-pause")
    clear.add_argument("--actor", required=True)
    clear.add_argument("--reason", required=True)
    clear.set_defaults(func=_with_ledger(cmd_controls_clear))

    evals = sub.add_parser("evals", help="golden-set evals").add_subparsers(
        dest="evals_command", required=True
    )
    ev = evals.add_parser("run")
    ev.add_argument("--mode", choices=["stub", "live"], default="stub")
    ev.add_argument("--record", action="store_true")
    ev.set_defaults(func=cmd_evals)

    serve = sub.add_parser("serve", help="API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    report = sub.add_parser("report", help="merchant portfolio reports").add_subparsers(
        dest="report_command", required=True
    )
    draft = report.add_parser("draft", help="write one merchant's weekly report")
    draft.add_argument("merchant_id")
    draft.add_argument("--mode", choices=["stub", "live"], default="live")
    draft.add_argument("--cassette", type=Path, default=Path("cassettes/merchant-ledger.json"))
    draft.set_defaults(func=_with_ledger(cmd_report_draft))
    report.add_parser("list", help="held reports").set_defaults(func=_with_ledger(cmd_report_list))
    for decision in ("approve", "reject"):
        p = report.add_parser(decision, help=f"{decision} a held report")
        p.add_argument("report_id", type=int)
        p.add_argument("--actor", required=True)
        p.add_argument("--reason", required=True)
        p.set_defaults(func=_with_ledger(cmd_report_decide), decision=decision)

    sub.add_parser("mcp", help="the agent's tools as an MCP server on stdio").set_defaults(
        func=cmd_mcp
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
