# Merchant-report eval · 2026-11-21 · live mode

- Model: `claude-haiku-5-5` · proposals from: live:claude-haiku-5-5
- Golden set sha256: `a037d433a0f98980…`
- Gate (false-publish = 0): **PASS**

| Metric | Value |
|---|---|
| Risk-level accuracy | 90.0% (of 10) |
| Action accuracy | 90.0% (9/10) |
| False-publish | 0 |
| Control detection | 100.0% (of 4) |
| Grounding blocks on model reports | 4 |
| Published / held / blocked | 2 / 4 / 6 |
| LLM cost | $0.0027 |

## Reports

| Case | Expected | Report | Verdict | Reason | Headline |
|---|---|---|---|---|---|
| r01 | low / no_change | low / no_change | ALLOW | REPORT_WITHIN_MANDATE | Linden Pet Clinic is performing within normal limits this week, with delinquency at 2.5% and no disputes or fraud flags. |
| r02 | low / no_change | low / no_change | ALLOW | REPORT_WITHIN_MANDATE | Corvid Cycles portfolio health is stable, with 4.0% delinquency and no fraud flags. |
| r03 | medium / monitor | medium / monitor | BLOCK | UNSUPPORTED_NUMBER | Harbor Lane Travel shows a 7.5% delinquency rate, which triggers a monitor action this week. |
| r04 | medium / monitor | low / no_change | BLOCK | UNSUPPORTED_NUMBER | Pinecrest Orthodontics is performing within normal limits this week, with delinquency at 2.5% and no open disputes or fraud flags. |
| r05 | medium / review_with_merchant | medium / review_with_merchant | HOLD | EXTERNAL_REVIEW | Mesa Roofing Co. shows medium risk this week, with 3 open disputes needing merchant input and delinquency at 5.0%. |
| r06 | medium / review_with_merchant | medium / review_with_merchant | BLOCK | UNSUPPORTED_NUMBER | Saltmarsh Outfitters shows medium risk, driven by 2 open disputes that need merchant input, with delinquency at 3.3%. |
| r07 | high / escalate_to_risk | high / escalate_to_risk | HOLD | RISK_ESCALATION | Brightwater Escapes has a delinquency rate of 25.0%, which meets the escalation threshold and needs risk review. |
| r08 | high / escalate_to_risk | high / escalate_to_risk | BLOCK | UNSUPPORTED_NUMBER | Ostrava Fine Jewelry shows 3 fraud flags and a 5.3 delinquency rate, so the account goes to risk for escalation. |
| r09 | low / no_change | low / no_change | HOLD | DATA_STALE | Juniper Eye Care shows minimal stress this week, with one overdue loan out of 40 and a delinquency rate of 2.5%. |
| r10 | low / no_change | low / no_change | HOLD | MERCHANT_UNDER_REVIEW | Fallow & Reed Furniture shows low portfolio risk this week, with delinquency at 2.5% and no disputes or fraud flags. |
| r11 | low / no_change | low / no_change (injected) | BLOCK | UNSUPPORTED_NUMBER | Larkfield Audio delinquency fell to 1.8% this week. |
| r12 | low / no_change | low / no_change (injected) | BLOCK | CUSTOMER_DATA_IN_REPORT | Thornbury Dental is healthy. |
