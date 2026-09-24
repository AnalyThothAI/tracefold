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
The first complete Case snapshot archives its exact candidate menu and brief.
If a model attempt loses its claim, the next attempt reads those same artifacts
under the new claim; it does not roll the 15-bar range or ATR forward. A stale
claim cannot replace the frozen refs. Missing or corrupt frozen files remain a
technical failure rather than silently creating a different setup.

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
and exit quote, contract rules, mark path, funding and fee references plus
observed latency.
Values without those references remain `net_unknown`.
The runtime captures a level-one bid/ask and displayed base size each minute
while a shadow evaluation is pending, preserving the per-sample archive ref and
an append-only logical tape manifest. The research quantity is the configured
fixed USD risk budget divided by the frozen stop fraction; it is not an actual
venue order or account-equity clamp. Both entry and the first archived exit
quote within 90 seconds after the mark-bar close must cover the quantity after
venue-step rounding at the displayed top level. A missing/late quote,
insufficient displayed size,
mixed environment or incomplete funding/mark path is `unevaluable`. Mark bars
select a possible protection trigger; the following executable quote supplies
the modeled exit price. A stop uses the worse of its threshold and the quote.
The initial evidence snapshot also freezes the target USDⓈ-M `exchangeInfo`
status, contract type, `PRICE_FILTER`, `MARKET_LOT_SIZE` and `MIN_NOTIONAL`
with a local receipt clock. Missing or invalid rules make the shadow result
unevaluable. The simulated base quantity is rounded down to the market step
and must pass the market quantity and notional filters; stop and take levels
are rounded to the tick in the conservative direction. The resulting quantity,
notional and levels are reported with the archived rule reference. Rule
availability still does not establish that an order would have been accepted.
The quote interval and OHLC order still limit timing certainty: a simulated
result is a diagnostic, never a venue fill or proof of protection placement.
Every selected initial root also has a bounded research tape independent of
its DSPy disposition. Until the root expiry plus the declared maximum holding
window, the Analysis runner archives a level-one quote and the latest two
closed price and mark 1m bars once per minute, then scans final funding
history with a short publication grace, including failed fetches and clocks.
The tape can supply the rule arm's own post-setup bar path and quote evidence
even when DSPy chooses NO_TRADE. A gap or late first crossing stays visible;
the offline comparison must reject paths whose actual contemporaneous receipt
times miss the fixed entry window. These tapes do not themselves claim a
complete simulated fill or measured advantage.
Export the 22 invalid raw model outputs
separately as `{case_id,raw_output}` in an access-controlled file. Do not infer
a v3 model answer from a historical v2 output.
The cohort export retains each Decision's policy version. A legacy model
Decision or retired timed recheck makes that root's new DSPy-arm result
uncomparable; it cannot be relabeled as a v3 run or treated as zero cashflow.
A terminal `FAILED` Case without a Decision has zero trading cashflow because
it published no order; the report separately counts this technical failure
and any unknown model cost. A pending Case without a Decision remains unknown.
An expired WATCH without a child is a recorded no-entry outcome; a still
waiting or otherwise unresolved WATCH is unknown. A deterministic `EXCLUDED`
root has zero trading cashflow in both arms under the shared eligibility gate.
The exporter does not count an excluded-at-source root's absent analysis snapshot
or research tape as a missing archive; neither artifact was required to exclude it.
The export also retains the WATCH observation's terminal state, last status,
child identity and archived observation ref. The funnel reports WATCH states;
an absent child is not inferred to mean that a complete no-crossing path was
observed.

For Cases written by the new schema, the read-only exporter joins root/child
identities, all attempt and physical-call cost/status/clock refs, shadow receipts
and archived evidence, then derives
the independent rule watch path from the root market tape. It writes a SHA-256
manifest with missing archive refs and coverage counts; missing data is not
recovered from today's market. Use a restricted local output directory:

```bash
uv run python scripts/export_trading_analysis_cohort.py \
  --archive-root /secure/analysis/archive \
  --start-ms 1790154395682 --end-ms 1790240795682 \
  --rule-risk-usdt 10 --shadow-fee-bps-per-side 5 \
  --max-spread-fraction-of-stop 0.25 \
  --output /secure/export/cases.jsonl \
  --manifest /secure/export/cases-manifest.json
```

Set `TRADING_RESEARCH_DSN` in the local environment rather than storing the
credential in a command line or repository file.

Use the same declared risk, fee and spread assumptions as the DSPy arm;
the example values above are placeholders, not measured costs. When the root
tape has a timely executable quote, complete mark path and final funding scan,
the exporter computes the rule arm's research-only shadow receipt with the
same simulator. It leaves missing or invalid inputs unevaluable. The 22
historical raw outputs still require a separate original archive export.

`scripts/trading_analysis_cohort.py` requires explicit root and invalid-output
denominators and exactly one initial Case per root. It excludes legacy rows
without a current root identity. Example:

```bash
uv run python scripts/trading_analysis_cohort.py \
  --cases /secure/export/cases.jsonl \
  --cases-manifest /secure/export/cases-manifest.json \
  --invalid-outputs /secure/export/invalid-model-outputs.jsonl \
  --cutoff-ms 1790197595682 \
  --model-usd-to-usdt-rate 1 \
  --output /secure/export/aggregate-report.json
```

The cutoff is fixed before examining outcomes. A four-hour purge precedes it;
all later roots are holdout. Every child follows its root. Roots sharing a
source group across splits are excluded from both splits and counted. Asset
identity is a reporting stratum, not a randomized split key. Rule and DSPy
arms use the same roots, timestamps, capital limits and strategy version.
Each source/asset stratum reports both arms' decisions, receipt coverage,
costs, isolated net equity and drawdown under the same starting capital. These
isolated stratum portfolios are diagnostic and cannot be summed into the full
portfolio, where trades from different strata compete for capital.
The portfolio evaluator admits only the exact quantity validated by a
contemporary execution receipt; insufficient capital rejects the entry rather
than resizing it without a new venue-filter and quote-capacity check.
Each root export supplies the rule arm's own continuous `rule_watch_bars`
with close and received clocks through root expiry, or an explicit missing
coverage status. The rule arm never borrows a conditional child produced by
the DSPy arm; a gap or late first crossing cannot become a later entry.
Known model cost and unknown calls remain separate. An arm with missing
contemporary receipts has an unknown net result; gross close-to-close price
paths are opportunity labels only. Closed-trade drawdown cannot establish
account drawdown while positions are open.
The physical-call ledger takes precedence over an attempt summary for research
costs: a request persisted before a worker crash still counts as an unknown
paid call even if the attempt aggregate was never finalized.
The USD-to-USDT conversion above is an explicit research assumption, not an
observed FX rate. The holdout conclusion is descriptive only: it requires
complete net, account marks and model cost, plus at least one evaluable entry.
Positive after-model difference supports further research; zero or negative
shows no observed advantage. Missing inputs yield evidence insufficient, and
none of these labels is a confidence interval or a venue-fill claim.
If any root may have traded but its action or receipt is unknown, the report
still counts validated receipts and names the missing reasons, but it does not
publish a portfolio ending equity, capital-reject total or drawdown as though
that unknown position had occupied no funds. A complete no-trade arm has zero
trading cashflow and retains its initial equity.

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
The current shadow simulator and local tests are not a PAPER receipt. No real
contemporaneous quote tape, complete historical export or controlled PAPER
entry/protection/exit receipt was available in this workspace. Keep
publication disabled until complete receipts support a net and drawdown
comparison; these Issues do not authorize deployment, LIVE orders or merging.
