# Build spec

This is the specification the project was built from, written before any code and
lightly edited for publication. The build was done phase by phase with an AI coding
agent (Claude Code), with a human reviewing each phase before the next one started.
The spec set the rules; the agent wrote the code; every result had to be measured, not
asserted.

At the end is a record of where the build departed from the spec and why.

## The brief

Build a demonstration of how a bounded AI agent should work inside a lending
operation's servicing team: an agent that works a queue of buy-now-pay-later
payment-exception cases on governed data, with a person at the gate for anything risky.

It has to show five patterns:

1. **Evaluations for everything shipped.** A frozen golden set, a harness, and
   committed reports. No number is published unless a report produced it.
2. **A bounded agent.** A defined mandate, a fixed action set, a named escalation
   owner, and explicit pause conditions.
3. **A person at the gate.** The agent never acts unattended on anything risky. A
   human confirms, with their name and a reason on the record.
4. **Day-zero detective controls.** Thresholds, an exception inventory, alerting, and an
   auto-pause that only a human can clear.
5. **Governed data.** The agent reads only through a governed layer, never raw tables,
   and that layer checks the data is fresh.

All data is synthetic. The project is a demonstration, not a product, and must say so.

## What to build

- A **synthetic BNPL ledger**: merchants, loans, payment schedules, payments, servicing
  cases with risk flags and a customer message thread, an append-only audit log, and an
  append-only exception inventory.
- An **agent** that reads one case and proposes exactly one action from a fixed set,
  with a rationale.
- A **deterministic guardrail** (no model inside it) that returns `ALLOW`, `HOLD` or
  `BLOCK` with a machine-readable reason code for every proposal.
- **Detective controls** that watch the agent itself.
- A **frozen eval harness** that runs end to end for $0 by default.

### Stack

- Python 3.11+, `pyproject.toml`, `pytest`, `ruff`.
- LangGraph for the agent loop: fetch case → propose → guardrail → act or hold.
- FastAPI for a small API that also serves the dashboard; a `bnpl` CLI.
- SQLite for the ledger, with every agent read going through a `governed` module.
- Anthropic API for the model, chosen by `BNPL_MODEL` (a Haiku-class default), with a
  daily spend cap `BNPL_DAILY_SPEND_CAP_USD` enforced in code. No key is ever committed.
- Two eval modes: **stub** (recorded model responses, $0, the default and the only
  source of published numbers unless a live run is committed alongside) and **live**
  (the real model, under the cap).

## The mandate

| Action | Meaning | Risk | Handling |
|---|---|---|---|
| `nudge` | Send a payment reminder | Low | Auto, within a daily cap per customer |
| `offer_plan` | Offer a revised payment plan | Medium | Auto only inside bounds (term extension ≤ 3 months, no fee waiver); otherwise HOLD |
| `escalate_collections` | Hand the case to human collections | High | Always HOLD for the servicing lead |
| `hold_for_review` | The agent abstains: the case is ambiguous | — | Goes to the review queue |
| `no_action` | Resolved, or a false alarm | Low | Auto, with a logged reason |

The escalation owner is the servicing lead. Every held case records who must decide it
and by when.

**Pause conditions.** When any of these is true, the agent pauses itself and alerts:

1. The governed-data freshness check fails (ledger older than 24 hours).
2. The auto-action rate over the last 100 cases exceeds 80%.
3. The `offer_plan` rate over the last 100 cases exceeds 25%.
4. A merchant is flagged as under review: all its cases hold.
5. Three consecutive guardrail BLOCKs.

The guardrail has a unit test for every rule, and those tests must all pass.

## Data

Seeded deterministically, so a fresh checkout reproduces the same ledger. Around a dozen
fictional merchants across Travel, Medical, Retail and Home Improvement (never real
company names), a few hundred loans from $50 to $50,000 over six weeks to 84 months, a
few hundred payments, and servicing cases of four types: missed payment, dispute,
at-risk, duplicate charge. The data should feel real: coherent amounts and timelines,
and message threads that read like a real servicing conversation.

## The golden set

48 hand-written cases, frozen, never edited to make the agent pass:

- 12 × `nudge`: a straightforward first missed payment
- 8 × `offer_plan` inside bounds: repeat missed, hardship signal
- 8 × `escalate_collections`: charged-off trajectory, fraud flag, arrears beyond bounds
- 8 × `hold_for_review`: conflicting evidence, missing data, hardship plus fraud
- 6 × `no_action`: payment landed after the case opened, duplicate case
- 6 × control scenarios: stale data, merchant under review, auto-action rate breach,
  `offer_plan` rate breach, out-of-mandate proposal, repeated BLOCKs

Each case records `expected_action`, `must_hold`, and a one-line rationale.

The harness reports at minimum: action accuracy, **false-auto rate** (a case that must
hold was executed unattended; it must be 0), hold accuracy on the ambiguous cases,
pause-detection rate on the control cases, the guardrail's BLOCK rate on out-of-mandate
proposals, runtime, and model cost.

## Review queue, controls and API

- `bnpl review list | approve | reject`, where every decision carries a human's name and
  reason into the audit log. Nothing held runs without a human decision.
- `bnpl controls digest`: cases processed, counts by action, hold and auto-action rates,
  the exception inventory, and breaches. On any breach: alert, log to the inventory, and
  pause until `bnpl controls clear-pause --reason …`.
- API: `GET /cases`, `GET /cases/{id}`, `POST /cases/{id}/review` (needs a human actor),
  `GET /controls/digest`, `GET /evals/latest`. Read-only apart from the review endpoint.

## Phases

Build in order. Do not start a phase until the previous one passes its check.

| Phase | Build | Check |
|---|---|---|
| 0 | Ledger, deterministic seed, governed module, freshness check | Tests pass; reseeding reproduces identical row counts |
| 1 | Mandate and guardrail with all pause conditions | Every guardrail test passes |
| 2 | The agent loop, with a stub model for tests | Runs over sample cases in stub mode for $0 |
| 3 | Golden set, harness, first committed report | Report exists; false-auto rate is 0; golden set untouched after the first run |
| 4 | Review queue, controls, alerts, auto-pause, API | A held case is approved end to end; a forced breach pauses the agent |
| 5 | Dashboard and README | Dashboard renders; every number traces to a committed report; lint clean |
| 6 | MCP server and a second, merchant-reporting agent | MCP tools respond over stdio; the second agent has its own committed eval report |
| 7 | Publish | Public repository renders correctly |

The second agent writes a weekly portfolio-health summary for one merchant from governed
metrics, with its own 12-case golden set and the same guardrail and hold rules. If time
is short, ship without it and say so: one strong agent beats two half-built ones.

## Acceptance criteria

1. Tests pass, with every guardrail test passing.
2. The stub-mode eval completes end to end at $0 cost, with its report committed.
3. The committed report shows a false-auto rate of 0. Otherwise the build is not done.
4. Every number in the README traces to a committed eval report.
5. The README carries synthetic-data and non-affiliation notes.
6. No secrets, no real customer or merchant data, no real company branding.
7. The dashboard shows the review queue, the controls digest and the latest eval, with
   every number coming from the API.
8. `ruff check` and `ruff format --check` pass; no dead code.
9. The repository is public and renders correctly.

## Honesty rules

- Never tune the golden set to make the agent pass. If the agent fails, fix the agent.
- Never claim the agent is production-ready or deployed anywhere.
- Never present a stub-mode number as a live result, or the reverse.
- If a phase's check fails, stop and fix it. Do not paper over it in the README.

## Build beautifully

Correctness first, polish second, but polish is part of the spec: a dark, clean,
phone-readable dashboard with one accent colour; an eval report page good enough to
screenshot; a README that reads like a product page; typed code with names that say what
things do; tests that read like documentation.

---

## Where the build departed from the spec

Recorded so a reader can tell the spec from what shipped.

- **Model.** The spec suggested an older Haiku model; the build defaults to
  `claude-haiku-5-5`, the current Haiku-class model at the time.
- **The first live eval failed.** Two ambiguous cases were closed with `no_action`
  unattended. The fix was in the agent, never the golden set: `no_action` now holds
  whenever the record still shows money overdue (a rule added to the mandate), and the
  prompt was revised once. Both runs are committed. Because the prompt was revised after
  seeing the set, the accuracy is an in-sample score.
- **Rate controls need a minimum sample.** Without one, the first unattended case would
  read as a 100% auto-action rate and pause the agent. Rates count once 20 decisions
  exist since the last human clear.
- **Merchant review is scoped to the merchant.** It holds that merchant's cases and
  alerts, but does not pause the agent for every other merchant.
- **Governance is enforced by the database.** The governed module installs a SQLite
  authorizer, so direct table reads and any write other than an append are refused by
  the engine, not by convention.
- **Two control cases inject a bad proposal.** The model's structured output only allows
  the five real actions, so the out-of-mandate and repeated-BLOCK scenarios hand the
  agent a deliberately bad proposal. They are excluded from action accuracy.
- **The merchant report guardrail was too strict at first.** It blocked sound reports
  that quoted a published policy threshold. Policy thresholds now count as grounded; the
  live run is kept as it ran, and a stub replay shows the fix.
- **MCP server location.** The spec named a top-level `mcp_server.py`; it lives at
  `bnpl/mcp_server.py` so `bnpl mcp` can run it. The MCP SDK in use is 2.x.
- **Demo data.** A live recording of the model's proposals for every ledger case lets
  `bnpl run` replay a real model's decisions for $0.
