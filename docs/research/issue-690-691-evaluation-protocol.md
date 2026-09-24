# Trading Agent evidence and strategy evaluation protocol (#690, #691)

## Scope and current conclusion

The fixed historical window is `[1790154395682,1790240795682)`. The Issue
reports 531 initial roots, 419 exclusions, 90 initial Decisions and 22 initial
failures, followed by 39 child Cases (34 Decisions, five failures). Those are
**Issue audit figures**, not a replay performed by this change. Its 22 invalid
model outputs and the complete 531-root export are not in the repository. The
older two-case [shadow diagnostic](issue-683-real-model-shadow-2026-09-23.md)
explicitly says its raw archive was lost. No result here may be described as
historical replay, positive net edge or PAPER venue evidence.

The strategy and observation code are safe to review in shadow. Publication
remains disabled by default. `oi_price_confirmation_v1` currently freezes the
preceding 15 closed one-minute bar high/low, requires positive source OI
change and value plus a current market OI quantity, and enters only on a closed
one-minute price crossing. It freezes a 200 bps stop, 400 bps take profit and
four-hour cap. **These are provisional research parameters, not optimized or
validated numbers.** The [earlier OI exit study](oi-exit-rules-replay-2026-09-07.md)
tested a different entry and 5-minute bars; none of its cells met its
preregistered adoption rule. Its numbers cannot validate this strategy.

## Export and fixed denominators

Export one JSONL row per Case from Trading's frozen Case, attempt and evidence
records, stripping headlines, credentials and raw provider text before sharing.
Required keys are `case_id`, `root_trigger_id`, `asset_id`, `created_at_ms`,
`run_kind` (`initial` or `recheck`), `state` and `decision_action`. Include
`watch_status`, `analysis_status`, `model_attempted`, `model_cost_microusd`,
the frozen `evidence` snapshot, and `arm_evaluations` where real, contemporary
capture exists. Each arm evaluation names `status` and `net_bps`; missing
counterfactual bid/ask, mark, funding, fee or latency is omitted, never zeroed.
For simplified Predict, include its separately recorded action and exact
program/prompt identity. Export the 22 raw invalid model outputs separately
as `{case_id,raw_output}` JSONL; keep that file local and access controlled.

`scripts/trading_analysis_cohort.py` fails unless it sees exactly the requested
root and invalid-output denominators and exactly one initial Case per root. It
excludes historical `run_kind=NULL` OI v5 rows. Example:

```bash
uv run python scripts/trading_analysis_cohort.py \
  --cases /secure/export/cases.jsonl \
  --invalid-outputs /secure/export/invalid-model-outputs.jsonl \
  --cutoff-ms 1790197595682 \
  --output /secure/export/aggregate-report.json
```

The cutoff is the midpoint of the stated window and must be fixed before
looking at outcomes. Asset identity hashes allocate 70% of assets to
development and 30% to holdout; development uses only Cases before the cutoff
and holdout only Cases at or after it. Other roots are reported as outside the
split. All child Cases follow their root. This can yield small or empty cohorts;
the report must state that, never move assets or the cutoff after inspection.

The `simple_rule` arm is rebuilt from each frozen OI source and perp/OI market
snapshot using the same versioned candidate builder. Recorded Predict uses the
actual stored action. Simplified Predict needs an independently recorded run;
it cannot be inferred from the old answer. The script aggregates all entry
denominators and reports net coverage, mean net bps, a trade-sequence drawdown
and known/unknown provider cost separately. Trade-sequence bps drawdown is
**not account drawdown**. The raw-output replay reports all Pydantic field/type
errors under the v2 wire contract, but contextual digest/evidence/entry checks
still require each frozen brief and candidate menu.

## Predeclared evidence checks and release decision

Check, per root and asset/time partition: source fact status and timestamp,
numeric/price correctness, eligible candidate rate, model schema failure rate,
WATCH hit/missing/expiry, quote and funding coverage, executable net coverage,
known/unknown model cost, and actual PAPER order rejection, partial fill,
protection, funding and capital use. Compare the simple rule, recorded Predict
and simplified Predict on the **same roots and same contemporary executable
prices**. Do not run GEPA/MIPRO or tune entry/exit parameters on the holdout.
Gross close-to-close labels are a separate opportunity-path diagnostic.

The release conclusion is **remain in shadow** until the fixed historical
replay, held-out net comparison and an authorized PAPER validation have
complete receipts. A positive result must include the full funnel, all three
arm denominators, actual cost and risk constraints, and no worse held-out net
and drawdown than the rule baseline. A negative or incomplete result keeps the
strategy unpublished; it is a valid research conclusion. This Issue does not
authorize LIVE orders or deployment.

## Environment checks still required

The local suite exercises response parsing, database idempotence/fencing,
source clock categories, closed-bar WATCH and conservative shadow arithmetic.
The target environment still needs recorded cold/warm cache, same-asset and
cross-asset bursts, 429 responses, queue expiry, restart, unknown send,
partial fills and protection outcomes. Those must come from the actual
Analysis/PAPER environment and venue reconciliation; unit tests and public
LIVE quotes cannot stand in for them.
