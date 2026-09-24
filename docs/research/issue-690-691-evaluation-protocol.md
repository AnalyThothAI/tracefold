# Trading evidence and strategy evaluation (#690, #691)

## Recorded scope

The fixed historical window is `[1790154395682,1790240795682)`. The Issues
report 531 initial roots, 419 exclusions, 90 initial Decisions, 22 initial
failures and 39 child Cases. Those are Issue audit figures, not a replay run
by this change. The complete 531-root export and 22 raw invalid model outputs
are absent from this repository. The older two-case
[shadow diagnostic](issue-683-real-model-shadow-2026-09-23.md) says its raw
archive was lost. No historical net result, model improvement or PAPER venue
result can be inferred from these counts.

`event_price_confirmation_v1` is a frozen research rule for both catalyst and
OI facts. It uses 16 continuous closed one-minute bars, the preceding 15-bar
high/low and ATR14, and an adjacent-close crossing of either boundary after
the source was first visible. OI quantity is context, not a positive-change
or direction gate. The stop is `clamp(ceil(2*ATR14/C0*10000),100,1000)` bps,
take profit twice the stop, with a four-hour maximum hold and a 120-second
entry window after the crossing close. These numbers are unvalidated.
The [earlier OI exit study](oi-exit-rules-replay-2026-09-07.md) used another
entry and five-minute bars; its results do not validate this rule.

## Frozen export and denominators

Export one JSONL row per Case with `case_id`, `root_trigger_id`, `asset_id`,
`created_at_ms`, `run_kind`, `state`, `decision_action`, frozen `evidence`,
attempts and `source_group_id`. Preserve the actual root-to-child chain. Include
`arm_evaluations` only when each arm has a contemporary, independently recorded
execution receipt; missing bid/ask, depth, latency, mark, funding, commission
or capital evidence remains unknown.
An evaluable simulated receipt carries the strategy version, entry/exit times,
requested notional, stop distance, signed funding cashflow and gross/cost bps
whose arithmetic exactly reconciles to net bps. It also names archived entry
and exit quote, mark path, funding and fee references plus observed latency.
Values without those references remain `net_unknown`.
Export the 22 invalid raw model outputs
separately as `{case_id,raw_output}` in an access-controlled file. Do not infer
a v3 model answer from a historical v2 output.

`scripts/trading_analysis_cohort.py` requires explicit root and invalid-output
denominators and exactly one initial Case per root. It excludes legacy rows
without a current root identity. Example:

```bash
uv run python scripts/trading_analysis_cohort.py \
  --cases /secure/export/cases.jsonl \
  --invalid-outputs /secure/export/invalid-model-outputs.jsonl \
  --cutoff-ms 1790197595682 \
  --output /secure/export/aggregate-report.json
```

The cutoff is fixed before examining outcomes. A four-hour purge precedes it;
all later roots are holdout. Every child follows its root. Roots sharing a
source group across splits are excluded from both splits and counted. Asset
identity is a reporting stratum, not a randomized split key. Rule and DSPy
arms use the same roots, timestamps, capital limits and strategy version.
Known model cost and unknown calls remain separate. An arm with missing
contemporary receipts has an unknown net result; gross close-to-close price
paths are opportunity labels only. Closed-trade drawdown cannot establish
account drawdown while positions are open.

## Validation still needed

On the fixed roots, measure source and clock completeness, candidate eligibility,
schema errors, WATCH crossings and missed windows, quote/mark/funding coverage,
known and unknown cost, net receipt coverage and capital rejects. Compare the
code rule and the new DSPy program with same-time executable prices and the
same portfolio constraints. Do not optimize on holdout. The 22 historical
invalid outputs can be audited for missing legacy fields, but replay of the
new program needs the original frozen briefs and candidate menus.

An authorized PAPER run must add reconciled venue fills, partial fills,
commissions, funding cashflows, protection orders, latency and account marks.
The current shadow simulator and local tests are not a PAPER receipt. Keep
publication disabled until complete receipts support a net and drawdown
comparison; these Issues do not authorize deployment, LIVE orders or merging.
