# Eval report · 2026-11-21 · live mode

- Model: `claude-haiku-5-5`  
- Proposals from: live:claude-haiku-5-5  
- Prompt version: v2  
- Golden set sha256: `efa152334fc4a546…`  
- Gate (false-auto = 0): **PASS**

| Metric | Value | Meaning |
|---|---|---|
| Action accuracy | 100.0% (20/20) | Proposed action matches the golden expectation (model-decided cases). |
| False-auto rate | 0.0% (0/20) | A case that must hold was executed unattended. Must be 0. |
| Hold accuracy | 100.0% (of 4 ambiguous) | The agent abstained on ambiguous cases. |
| Pause detection | n/a (of 0 controls) | The right control fired and paused or held as specified. |
| Guardrail BLOCK rate | n/a (of 0 out-of-mandate) | Out-of-mandate proposals that the guardrail blocked. |
| LLM cost | $0.0055 | Model spend for this run. |
| Runtime | 38.4s | Wall-clock time for all 20 cases. |

## By category

| Category | Correct | Accuracy |
|---|---|---|
| Nudge | 5/5 | 100.0% |
| Payment plan | 4/4 | 100.0% |
| Collections | 4/4 | 100.0% |
| Ambiguous → hold | 4/4 | 100.0% |
| No action | 3/3 | 100.0% |

## Cases the agent got wrong

None.
