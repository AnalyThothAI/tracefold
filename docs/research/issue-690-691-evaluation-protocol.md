# Trading evidence and strategy evaluation (#690, #691)

## Recorded scope

The fixed historical window is `[1790154395682,1790240795682)`. The Issues
report 531 initial roots, 419 exclusions, 90 initial Decisions, 22 initial
failures and 39 child Cases. A read-only audit of the existing runtime on
2026-09-24 recovered a restricted local inventory of all 531 initial Cases and
39 child Cases (`historical-cases.jsonl`, SHA-256
`45e042b634769baf45bcc82231b40171659a5bd1f87ac8dc29fa5480c12ea2a0`),
plus all 22 invalid model response texts (`invalid-model-outputs.jsonl`, SHA-256
`794b17e76188b943cee20c92eccb850b602d51d091add7879f57fc7c396516b7`).
The companion `legacy-window-originals.tar.gz` (SHA-256
`c2012332076a705fd3f6ce01331345639b322ba358110863215b0c3181c29987`)
preserves the 531 Trigger rows, 570 Case rows, 124 Decision rows, 944 outcome
rows, the 22 orphaned failure-assessment references, and 1,439 referenced
content-addressed files. Its database and archive member digests verified.
They are held outside Git at `~/.tracefold/research/issue-690-691/` on the audit
host in a mode-700 directory; the exports are mode 600. The 22 responses were
recovered from content-addressed assessment
files that the old failure path did not link from the database. Each file's
Case, claim attempt and token matched the database; all content digests and
request, response and brief refs verified. The 124 adopted assessment refs
and 147 evidence refs in the fixed root chain also verified. This inventory is
a legacy metadata and raw-output audit, not a v3 Case export or a new model run.
Of the 22 raw responses, 20 were JSON objects with an `assessment` wrapper and
two were not parseable JSON. Applying the pinned legacy contract offline to the
20 wrapped objects accepted none; validation errors included watch conditions
on non-WATCH actions, candidate identity on non-TRADE actions, invalid weight
totals, overlong WATCH delays and an extra field. Error counts overlap within
responses. These results do not predict how the new v3 model would answer.
One separately archived offline v3 call used the earliest legacy catalyst Case
whose 16 closed bars supported a WATCH, mapping its old `headline` and `why`
fields to the new names without inventing a new fact. The restricted
`v3-offline-model-sample.json` (SHA-256
`fad86113d1e2e084edfac80542a2f8cb838b94736036dae5ac047fbd487f29b3`)
contains its frozen projected brief, one actual physical model request and
response, and the compiled Decision. The model returned a valid v3 `NO_TRADE`;
the compiler accepted two cited evidence IDs. The provider reported 2,375
input and 292 output tokens but no exact price, so this call's cost remains
unknown. This verifies the model/adapter/contract path on an explicitly
projected historical input, not a contemporaneous v3 Case or shadow trade.
The same preserved window has 708 settled `price_path_v1` `ok` outcomes. A
read-only replay of the correction audit verified 705 archived endpoint values,
clocks, receipts, identities and arithmetic as v2 gross endpoint labels; three
have mismatched endpoints and remain `historical_quality=unverifiable`. The 705
carry `historical_quality=verified_endpoint_only` and
`full_path_quality=unknown`. The deployed runtime was `c69b6bc09`; its pinned
`binance_public_v1` adapter was introduced at `f1ef42091` and unchanged at
that revision. Its source was Binance USD-M mainnet klines and it accepted only
closed bars. Thus `data_environment=live` is inferred from pinned code, with
that provenance recorded in each correction. The archive does not contain the
full bar path, execution prices or costs. This audit did not write v2 rows to
the running service or refetch current history, and no net result follows from
an endpoint gross return.
The older two-case
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
select a possible protection trigger; the first executable quote after the
mark bar was actually received supplies
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
The DSPy arm also consumes the root tape's concurrently archived mark bars and
final funding scan. Both arms require each used mark bar to have been received
within 120 seconds of its close. They do not retrospectively fetch a replacement
mark path after the holding window. An incomplete tape leaves the shadow result
unknown.
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

For Cases written by the new schema, the exporter reads one read-only,
repeatable-read database snapshot and joins root/child
identities, all attempt and physical-call cost/status/clock refs, shadow receipts
and archived evidence, then derives
the independent rule watch path from the root market tape. It writes a SHA-256
manifest with missing archive refs and coverage counts; missing data is not
recovered from today's market. A shadow receipt still within its scheduled
evaluation window remains `receipt_pending`, with its due time, instead of being
counted as a missing terminal receipt. Simulated receipts with absent, corrupt,
or non-object archive refs are unevaluable and listed in the manifest. Use a
restricted local output directory:

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
historical raw outputs remain a separate restricted export; the new-schema
cohort exporter must not treat them as v3 responses.

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

The cutoff is fixed before examining outcomes. A development root must have
expired at least four hours and four minutes before the cutoff. This covers
the last WATCH entry, four-hour hold, entry-window margin and final funding
scan. A root created at or after the cutoff is holdout; intervening roots are
purged. The report and research manifest record this split rule and purge
duration. Every child follows its root. Roots sharing a source group across
splits are excluded from both splits and counted. Asset
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
invalid outputs can be audited for legacy structure, but replay of the
new program needs the original frozen briefs and candidate menus.

An authorized PAPER run must add reconciled venue fills, partial fills,
commissions, funding cashflows, protection orders, latency and account marks.
The current shadow simulator and local tests are not a PAPER receipt. The
restricted legacy inventory and raw outputs above still lack v3 decisions and
contemporaneous executable quote, mark, funding and account evidence. No real
contemporaneous quote tape or controlled PAPER entry/protection/exit receipt was
available for this branch. Keep
publication disabled until complete receipts support a net and drawdown
comparison; these Issues do not authorize deployment, LIVE orders or merging.
