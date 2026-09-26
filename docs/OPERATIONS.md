# Operations

This document owns runtime configuration, worker/reliability invariants,
diagnosis, and safe repair boundaries.

## Runtime truth

The only operator-owned Tracefold application configuration is
`~/.tracefold/config.yaml`: deployment/domain choices, role-specific
PostgreSQL references, credentials, API/auth,
models, and storage. Worker topology, cadence, deadlines, batches, leases,
retry policy, timeouts, and resource budgets are code-owned.

Confirm the active paths with `uv run tracefold config`. Never infer live state
from fixtures, examples, `.env`, generated docs, or a new CLI process. Report
paths, redacted configured booleans, source names, error classes, and command
results; never secret values.

### Trading Analysis and Binance execution operation (#683)

Execution remains disabled by default, so credentials are optional for the
ordinary deployment. `trading.enabled` starts the separate Analysis process.
`trading.execution.enabled` starts the one-owner Binance USD-M Nautilus
Strategy/Risk/OMS/reconciliation path. `trading.execution.binance.environment`
selects `LIVE`, `DEMO` or `TESTNET`; omission uses the pinned SDK default.
The configured target and the Runtime's observed connection are shown separately.

Analysis runs `tracefold analysis` beside Serve and Workers. It
drains News' durable catalyst/OI outbox independently of delivery, claims
per-asset Cases with fenced leases, freezes market and model records under
`~/.tracefold/archive/trading-analysis`, and labels due opportunity paths.
For an existing installation, stop Analysis and run
`uv run python scripts/migrate_trading_analysis_archive.py` before switching
the image. It copies every legacy content-addressed ref from cache to archive,
verifies the digest and leaves the source intact. Include `archive/` in the
durable backup and restore set. Preserve the historical v1 path inventory and
run `uv run python scripts/relabel_trading_price_paths.py` while Analysis is
stopped. The command audits archived v1 endpoint values, clocks, identity,
receipts and arithmetic, then appends a v2 gross endpoint label with
`historical_quality=verified_endpoint_only` when they agree. It marks a
disagreement `missing` with a specific `historical_quality=unverifiable` reason.
The old rows and archived paths remain unchanged. It does not fetch today's
historical public bars to assert an observation from the original time. In the
fixed #690 window, 705 of 708 settled v1 `ok` labels meet the endpoint check;
three have mismatched endpoints. The pinned v1 `binance_public_v1` adapter
(introduced at `f1ef42091`, unchanged through deployed `c69b6bc09`) used
Binance USD-M mainnet klines and accepted only closed bars, so the correction
records `data_environment=live` with that code provenance. The v1 archive does
not retain the full bar path, execution prices or costs. These labels establish
only endpoint gross returns, never a tradable or net result.
`trading.analysis.publish_signals` is false by default. An unavailable model
is recorded as unavailable; it never silently activates the retired OI v5
policy. `entry_plan_v1` publishes a Signal only when `publish_signals=true`.
Turning on publication requires a separately running Nautilus deployment;
this flag does not start Nautilus or grant order authority.
The Analysis process runs native DSPy 3.4 ReAct with bounded, read-only Case
tools. Optional `llm.trading_semantics` connects the Jev ClaimSupport tool
through the TypeSafe System One SDK. The current OpenRouter base is
`https://openrouter.ai/api`; a future direct route changes only the complete
base URL/key/model triple. A missing Jev route leaves the other three tools and
the Agent available. Jev judgments are archived as semantic evidence, never
as a second trade approval. Each physical generator or Jev call is tied to
its Case attempt with requested/served model and known or unknown cost.
New Cases fetch executable contract rules and market frames from the configured
Binance connection. Each frozen Case records the actual source environment.
Changing the configured target does not relabel earlier evidence. A changed
connection under the same account slot is refused at Runtime startup until the
existing venue exposure and account identity have been reviewed.
The root tape retries a bounded recent closed-bar window after a failed market
read. Recovered bars retain their actual receipt time; a late bar can fill an
archive gap but cannot retroactively make WATCH timely or make a late mark path
eligible for net evaluation. Inspect tape coverage and receipt clocks before
interpreting an `unevaluable` result.

For a Case with no Decision, inspect `analysis_attempts` in the Case detail:
each claim attempt has a structured validation error, frozen evidence ref and
one indexed row per physical model response. A late attempt can remain visible
while `settled=false`; it did not replace the fenced Decision. A WATCH with a
machine condition shows its frozen side and level, latest closed-bar
observation, expiry and conditional child Case. The directed crossing consumes
the opportunity even when found after its 120-second entry window. Shadow
evaluations are historical research records, with archived quote, mark and
funding refs. They cannot be treated as exchange fills. Venue net values require
reconciled fills, fees, funding and protection receipts from the connected account;
the execution read model exposes signed funding income and scan coverage. A
missing coverage interval or competing same-symbol plan leaves net PnL
unknown. Funding reconciliation scans Binance USD-M `FUNDING_FEE` income every
30 minutes, overlapping recent windows and reading seven days on startup.
After a longer outage, recover signed income while Binance still retains it;
historical periods outside venue retention remain unknown.

Run `uv run tracefold init` before the first current startup; canonical
`make up` and `make deploy-image` already do so. It creates and permissions the
operator directory and never rewrites config content: a config still holding a
retired key is refused by `Settings` validation naming that key, and the
operator edits it (#589). For a direct schema upgrade, require this order:
`uv run tracefold init`, `uv run tracefold config`, then `make db-migrate`.
This cut removes the old `trading.candidates` setting; remove that key from an
existing operator config before starting the new image. Set
`trading.analysis.active_policy: entry_plan_v1` when an older config explicitly
names `event_price_confirmation_v1`.

Run `uv run tracefold config` to inspect only the execution setting, account slot,
risk section and resolved secret-file references. Never print or copy a
credential. Verify the configured lifecycle with:

```text
make up
make runtime-build
make runtime-up
make status
docker compose exec -T workers tracefold trading status
```

Disabled status reports `alive=false` and `entries_armed=false`;
`make runtime-status` fails closed if a container is still running in that mode.
It does not fail on the readiness payload itself: that is printed, whatever it
says. An active Runtime additionally requires exactly one current account-slot
owner, secure non-empty credential files, a fresh heartbeat, and the configured
account slot and connection. `tracefold trading status` and `/api/trading/status`
report what the Runtime is doing and nothing about which build is doing it.
Serve and Workers never receive Binance secrets.

#### Who owns which execution fact (#680)

Nautilus owns execution state. The venue is the truth about positions, orders and
fills, and the Nautilus Cache is the only in-process copy of it: Nautilus'
startup reconciliation rebuilds the Cache from the venue before the Strategy
starts, over a lookback covering the oldest still-open Plan's creation time
(including time spent outside a running generation), and its 5-second
open-order and position checks keep it converged. There is no Cache database; a
restart is the same reconciliation a start is. PostgreSQL holds intent (the
TradePlan), the append-only execution journal and the operator's inputs. Realized
PnL is folded from the journal's fills, net of every commission the venue charged.
See [Architecture](ARCHITECTURE.md#runtime-ownership) for the ownership table.

What reconciliation may do, and what it may not (#680 PR-3). It applies the
venue's own orders and fills to the Cache. It does not invent an order or a fill
to make the Cache match a position report (`generate_missing_orders=False`), and
the Binance fill reports it replays name each venue trade once. On 2026-09-23 both
of those happened on Nautilus 1.231.0 and closed an open Demo APT long in the Cache
while the venue still held it: a user-data re-subscribe asked for a full mass
status whose fill reports were duplicated (the adapter queries `APTUSDT-PERP` and
`APTUSDT` for one market), and the fill adjustment added a synthetic SELL; later a
positionRisk `-1021` came back as "no position report" and the position check
closed the position as flat. The cost of the flag: at startup, a venue position
with no fill inside the reconciliation lookback is no longer adopted into the
Cache. The venue-truth invariant below names it instead.

Native `userTrades` reads normalize symbol spelling before requesting and share
one budget of 32 signed requests per report generation. Each page is at most
1,000 rows. Full time windows are subdivided to avoid losing earlier trades when
the endpoint returns the most recent page; windows never exceed seven days.
Exact-order queries page by `fromId`, without mixing it with time parameters.
The reader returns immutable native rows and unfinished cursors. A timeout,
contradictory native trade identity, exhausted budget or saturated millisecond
is not a complete history and cannot authorize an inferred fill. Dense exact
orders can still be paged without losing trades sharing one timestamp. These
limits follow Binance's [account trade endpoint](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#account-trade-list-user-data);
its three-month retention still limits what can be recovered from that endpoint.

For a triggered stop or take-profit, Binance reports the fill under the regular
child order ID, while Nautilus may still cache the original Algo order ID (#699).
The Runtime's Binance client uses the same immutable order-evidence reader for
full reconciliation, live order/Algo updates, exact-order queries, and known
orders missing from successful complete open-order lists. It verifies the
parent's signed `GET /fapi/v1/algoOrder` receipt, including `actualOrderId`, against
the child order and its complete venue trades. It then sends Nautilus an
`OrderUpdated` to move the cached order to the child ID before the engine replays
those real fills. A partial child fill reduces only the traded quantity; its
remaining position and Plan stay open for protection and exit. A triggered
child with missing trades or contradictory signed evidence fails
reconciliation; a matching client order ID by itself never authorizes a close.
The original strategy order and its Plan retain the stop or take-profit
attribution. `ALGO_UPDATE FINISHED` cannot create an aggregate zero-fee fill:
missing trades remain incomplete. Reads coalesce per symbol/client order, allow
two concurrent chains, time out after ten seconds, and retain a 5–60 second
backoff across duplicate notifications. Native client shutdown cancels its reads;
a completed historical-only chain never injects an isolated exit into an empty
current Cache. An old inferred Cache quantity that cannot be matched to native
trade IDs produces `binance_cache_native_trade_history_conflict`, rather than
applying its quantity twice. Venue integer millisecond timestamps are preserved
when constructing native reports.

The recorded INJ fixture contains signed Demo historical reads captured on
2026-09-26 (entry `308643511`/`63767654`, child `308654865`/`63772472`, Algo
`1000000218190388`). Offline installed-engine replay confirms quantity 121.3,
exit time 2026-09-25 12:12:45.075 UTC, fees 0.805432 USDT and realized PnL
19.087768 USDT before funding. This is a replay of recorded receipts, not a new
Demo trade or a production history correction.

On a generation restart, a flat venue and empty Cache otherwise provide no
"active" symbol for Nautilus to query. PostgreSQL's open Plans supply only the
bounded symbol query scope. A signed Algo parent receipt then identifies a
historical regular child as a stop or take-profit before Nautilus replays its
actual trades; an ordinary reduce-only exit with no Algo parent remains a market
exit. After replay, the Strategy may attribute a closed Cache Position to a Plan
only when its opening order matches that Plan's entry and the closing order has
real fill quantity. If multiple distinct exit legs supplied the closing fills,
the Plan records `mixed_exit` instead of assigning the whole close to the final
leg. The absence of closing proof leaves `venue_unknown`.

On top of the Cache the Strategy runs one invariant every five seconds, and on
every fill and position event, over the Cache, the plans and the latest read of
the venue's own positions:

- a position with any entry fill gets one reduce-only `STOP_MARKET` and one
  reduce-only `MARKET_IF_TOUCHED` take-profit order, both triggered on the
  **mark price**, at the plan's stop and take-profit distance from the average
  fill. A missing one is placed again. When a partial fill changes quantity or
  average price, the Runtime submits a new mark-price reduce-only Algo order and
  keeps the old one live until the replacement is accepted, then cancels the old
  one. Nautilus 1.231.0's Binance adapter rejects in-place modification of
  STOP_MARKET and MARKET_IF_TOUCHED orders; the installed-adapter regression
  verifies the submit/cancel path. Position-opened and position-changed events
  run this check immediately; the periodic pass recovers missed or refused
  replacements. A stop or
  take-profit the venue refuses with `-2021 would immediately trigger` means the
  condition is already met, so the position is closed at market under that leg's
  reason;
- a position held past the plan's maximum holding time is closed with a
  reduce-only market order (`time_exit`);
- when one of the Runtime's own closing legs closes a position — its stop
  (`stop_filled`), take-profit (`take_profit`), time exit (`time_exit`) or an
  operator flatten (`operator_flatten`) — every order left on its instrument is
  canceled and its plan ends with that reason;
- any other close — one placed on the venue by hand, or a fill Nautilus'
  reconciliation invented — is not evidence that the venue is flat. The position
  observation records `exit_reason=external`, but the stop, the take-profit and
  the plan stay, and the instrument is unexpected exposure
  (`unconfirmed_close:<instrument>`) until a venue read confirms it flat; only
  then are the orders canceled and the plan ended (`external`, or the reason of a
  closing leg that filled meanwhile). A plan whose end the Runtime never saw ends
  as `venue_unknown`, also only on a flat venue read;
- on an instrument with no position and no plan, an order that could add
  exposure is canceled at once; a reduce-only order may be the only protection a
  position the Cache lost still has, so it is canceled only after a venue read
  says the instrument is flat, and is named (`order:<id>`) until then;
- **venue truth**: the Runtime reads the account's signed positions from Binance
  (`GET /fapi/v3/positionRisk`, the same credentials and Nautilus HTTP stack)
  every 30 s, bounded by a 25 s timeout, outside any database transaction. A read
  that fails is *unknown*, never flat, and never clears anything. A symbol whose
  venue quantity differs from the Cache's net position — a position only the
  venue holds, one only the Cache holds, or a different size — is a suspect on the
  first read and unexpected exposure (`venue:<SYMBOL>:venue=<q>:cache=<q>`) on the
  second; one agreeing read clears it. An instrument with a fill or position event
  less than 5 s before a read began is judged by the next read, not that one. The
  invariant itself submits and cancels nothing. After a stable mismatch, the
  Runtime may ask Nautilus for one bounded native reconciliation, only when a
  unique open Plan claims the instrument and direction. The same discrepancy
  retries with 5, 10, 20, 40, then 60-second backoff, without a terminal retry
  count. Identical new reads do not reset that delay; genuine quantity/Plan
  changes or agreement do. Instruments are considered oldest-attempt-first.
  Only one recovery task runs per generation, and stopping it cancels that task.
  The public recovery path still works after Nautilus' own position timer has
  exhausted its retries; the Runtime never resets native private counters. A position only the Cache holds
  gets no new stop, take-profit or time exit, since the venue would refuse them;
- a position, or a non-reduce-only order, that no plan claims is *unexpected
  exposure* too: new entries are refused with `unexpected_exposure` and a `risk`
  observation names every entry. **Nothing is ever flattened because the picture
  is unclear**; the operator decides, with `/flatten account`.

Nautilus 1.231 behaviours this design routes around rather than patches: orders it
reconciles carry no account id, so the Runtime only asks the Cache by instrument and
strategy; a failed Algo-order report during reconciliation is only logged, so a
triggered child's signed parent receipt is checked separately before replaying its
fills; a lost user-data listen key is
recovered once, after which the 5 s checks keep the Cache honest; the Binance
adapter's duplicate symbol reads are eliminated by the Runtime's execution
client before requesting native trades. The position-report override preserves Binance read failures; a
successful instrument-specific empty read reports genuine flatness.
The Binance position-report adapter uses one pinned private Nautilus helper to preserve
venue read errors; the installed-version regression test fixes that seam. Other Runtime
code does not read private Nautilus members. The offline regression tests cover the
2026-09-23 paths.

Nautilus' own WARN and ERROR lines — every reconciliation decision among them —
also go to `~/.tracefold/logs/nautilus-engine_*.log`, rotated at 10 MiB with five
backups, so they outlive the container that wrote them. The Runtime's own log is
`nautilus.log` beside it.

#### Entries

A Signal (or a manual `/long`/`/short`) passes these gates in order, and the first
one it fails is its disposition: `emergency_halted`, `entries_paused`,
`unexpected_exposure`, `venue_unverified` (it waits, inside its TTL, for a venue
read younger than two minutes that agreed with the Cache on every instrument; at
startup that is the first read, seconds after the node starts),
`post_stop_cooldown` (Signals only: a stop-out on the same
market inside `post_stop_cooldown_seconds`, read from the durable plans),
`instrument_unmapped`.
It then waits, inside its own TTL, for a fresh quote (`market_unavailable`), for a
spread no wider than `max_spread_fraction_of_stop` of its stop distance
(`spread_limit`, which records the spread it measured as `spread_bps`), and for
its instrument to hold no position and no order (`instrument_busy`); if the TTL
runs out while it waits, the reason it was waiting for is its disposition. It is
sized once, its plan is committed, and only then is one market order sent with the
plan's deterministic client order id. Nothing is re-measured between the commit
and the order. The Runtime sizes each entry from current equity times
`risk_fraction_per_trade`, constrained by `max_leverage` and the venue's quantity
and notional filters. It has no separate per-trade dollar, position-count or
daily-loss admission limit. Existing exposure on the same instrument still
blocks another entry, and the account snapshot still reports daily drawdown.

A Signal's disposition is written once the venue answered: `accepted` when the
venue accepted or filled the entry, `venue_rejected` (with `venue_reason`) when the
venue or the pre-trade risk engine refused it — that plan ends at once as
`not_submitted`. A restart between the order and the answer leaves the verdict for
the next generation, which writes it from the plan and the Cache.

#### Failure semantics

A Strategy callback or pump step that raises is logged and runs again on the
next pump; the database bridge and current-state writer replace lost sessions after
bounded backoff. A Nautilus node that fails to start or stops is disposed and a new
generation is built after 5-60 s. The process exits for unusable configuration or
credentials, an older database schema, loss of the account-slot lock, or failure to
stop an old writer before rebuilding a generation. Database-refused Plan transitions,
fills and order bindings are retained for another durable verdict. Other refused
observations are logged and dropped. Transient journal failures retry after backoff
while later rows keep flowing. A critical replay must match the immutable stored
fact (a later observation timestamp is allowed); a conflicting event ID is refused
and the batch's other inserts roll back. Missing observation write receipts never
release a critical row from the journal. The same identity check also applies while
a fill or binding is still pending in memory.

The Runtime process holds three fixed PostgreSQL connections. The singleton session
holds the account-slot advisory lock. The bridge reads Commands and flushes the
journal with a five-second `statement_timeout`; the independent current-state writer
has a one-second `statement_timeout`. Neither holds a transaction across Binance I/O.
The event loop publishes the newest candidate to the writer without waiting for
journal work. Semantic changes are written promptly and an unchanged generation
heartbeats every 500 ms, before the public five-second stale threshold. A failed
write does not advance the projector's durable state, and repeated failures make
the public status stale.

Quote streams are opened per waiting entry and per held position rather than for the
whole route catalogue, so an operator reading Nautilus logs should expect one
market-data stream per live thesis and none at rest. The operator-owned numbers
under `trading.execution.risk` — `risk_fraction_per_trade`,
`max_leverage`, `stop_distance_bps`, `max_spread_fraction_of_stop`, `post_stop_cooldown_seconds`
and `market_stale_after_seconds` — and `trading.execution.exit_policy` reach the
Runtime at start; editing one needs a restart and nothing else, and moves neither
the account slot, the persisted client order namespace nor the Nautilus instance id.
Existing plans keep the stop distance, take-profit
and maximum holding time they were admitted with.
Remove retired `max_risk_per_trade_usd`, `max_positions` and `max_daily_loss_usd`
entries from `config.yaml` before upgrading; configuration validation rejects them.

**A runtime replacement is a restart, and a product deploy is not one.**
Changing the runtime image, the release or any `trading.execution.*` value does
not require a new name or a fresh `/resume`: `account_slot` is the
execution identity, the account-slot advisory lock is what keeps two Runtimes
apart, and control state lives on the slot. Entries stay exactly as the last
accepted Command left them. `execution.enabled: false` is the switch that means
"do not trade". A News, Serve or Workers release does not restart the Runtime at
all: `make up` never names the service, so the process keeps its position, its
protective orders, and its `started_at_ns` across a product deploy (#537 D3).

For the first Demo start on a slot, confirm the Binance account is flat, populate
the configured Binance files as regular mode-`0600` files, set
`execution.enabled: true` with `execution.binance.environment: DEMO`, and run `make up` followed by
`make runtime-build && make runtime-up`. A slot with no Command history starts
with entries armed. Inspect `make status` and
`docker compose exec -T workers tracefold trading status` before letting a Signal
or `/long`/`/short` enter, and use `/pause REASON` if it should not. After the
bounded Demo exercise, issue `/flatten account TTL_SECONDS`, confirm the plan ended
as `operator_flatten` and `current_account` holds no position or order, then run
`make runtime-down` and restore `execution.enabled: false`. `make runtime-down` is
what stops trading; restoring the config afterwards is what stops the next
`make runtime-up`. Demo evidence is not live-money evidence. Never select `LIVE`
or perform a live canary without separate explicit operator authority.

#### Reading the Demo receipt out of the durable facts

`trading status` carries the last successful account projection: Cache positions,
last observed venue-only positions, protection evidence, and working orders.
The venue read time and failure identify when a venue-only row is historical.
The trade itself is in the plan and the journal. Run these against the
Tracefold database, substituting the entry's `signal_id` or manual `command_id`:

```sql
-- 1. the plan: admitted intent, when it opened, how and when it ended
SELECT status, opened_at_ns, terminal_at_ns, exit_reason, stop_distance_bps, take_profit_bps
  FROM trading_trade_plans
 WHERE entry_id = :entry_id;

-- 2. the venue's side: orders, protection, fills with their commissions, positions
SELECT normalized_kind, summary, native_identity_references, occurred_at_ns
  FROM trading_execution_observations
 WHERE signal_id = :entry_id OR command_id = :entry_id
 ORDER BY seq;

-- 3. the flatten: one control disposition, accepted once the closes were sent
SELECT summary ->> 'disposition' AS disposition, summary ->> 'reason' AS reason, observed_at_ns
  FROM trading_execution_observations
 WHERE command_id = :flatten_command_id
   AND normalized_kind = 'control_disposition';
```

The restart receipt is a new `started_at_ns` on `trading_execution_runtime_state`
with the plan row unchanged and no new order or protection observation for it:
the reconciled position and its resting orders were adopted, not replaced.

### Trading operator control

The sole operator ingress is `tracefold trading issue`, run **inside the Workers
container** and authenticated by the container OS uid. The CLI reaches PostgreSQL
over the compose network, where the configured DSN resolves. The web console is
read-only: its manual controls and HTTP command endpoint were removed in #624.
There is no `trading.control` configuration block or Telegram control webhook.
The retired `trading.control` and `trading.notifications` blocks fail config
validation and must be deleted from older configs.

The closed commands are `/status`, `/pause REASON`, `/resume REASON`,
`/halt REASON`, `/flatten account TTL_SECONDS`, and optional
`/long MARKET_KEY TTL_SECONDS` / `/short MARKET_KEY TTL_SECONDS`. Flatten and
manual TTL are 5–120 seconds; control TTL is five minutes. There is no quantity,
notional, leverage, venue, order type, or direct order option. Manual direction
enters the same Runtime gates, sizing, deterministic client-ID, order,
protection and journal path as a Signal; it has no bypass (it only skips the
post-stop cooldown, which applies to automatic Signals).
An accepted emergency halt is sticky for the Runtime lifetime: `/resume` is
explicitly rejected as `emergency_halt_sticky` and cannot manufacture a resumed
state.

A CLI `ok` proves only the PostgreSQL intent. With no
Runtime running, the intent remains `awaiting_runtime` until its TTL passes;
ingress never fabricates a terminal Runtime Observation.
Inspect `trading commands` for the command disposition and
`trading observations` for later Runtime facts. The only valid evidence ladder
is: intent recorded, Runtime accepted, order accepted, fill observed, and an
empty `current_account` (no position, no order) on a live, fresh execution
projection. Never infer a later stage from an earlier one, from a recent
`decision.last_case_at_ms`, or from Runtime readiness. Do not read flatness out
of the observation ledger: it records orders, fills and positions, not the
absence of them.

The browser reads and the one Command write both use the bootstrap `ws_token`
(#520 PR-B deleted the separate `console_write_token`). The write still requires
it as an `Authorization: Bearer` header: a `?token=` query parameter, which lands
in proxy logs and browser history, authenticates reads only.

The local fallback writer uses the identical parser and an OS UID identity:

```text
docker compose exec -T workers tracefold trading issue "/pause maintenance" \
  --request-id ops-20260901-1 --requested-at-ns <unix-nanoseconds>
```

The same applies to `/flatten account`, `/halt`, `trading status`,
`trading commands` and `trading observations`: every Trading CLI entry runs in
the Workers container.

Preserve both request fields exactly on retries.

The Runtime publishes separate process, account and check evidence. `alive` means
the loop is running; only `entries_armed` permits a new entry. `readyz` returns
HTTP 200 with a diagnostic payload even when entries are blocked. `healthz` is the
container liveness check. A failed account projection does not stop heartbeats:
the last successful `current_account.observed_at_ms` remains visible with
`account_projection_failure`. Convergence and venue reads have separate success
times and failures. `protection_status` is `not_applicable`, `protected`,
`pending`, `unprotected` or `unknown`. The bounded account list includes Cache
and venue-only positions, source, strict Plan association, typed findings and
totals. A venue-only position has unknown protection. A stale PG heartbeat
means the status channel cannot confirm the process; compare probe, writer,
database and HTTP samples before attributing the gap.

A restart while in a position is Nautilus reconciliation: the position and its
resting stop and take-profit are rebuilt into the Cache from the venue and the
open plan claims its position by opening order ID, instrument, Strategy and direction.
Logical order bindings carry the Plan and leg independently of the native order type:
a triggered Binance child stays a `MARKET` report. Initial leg IDs derive from the
committed Plan. Replacement bindings are registered before submission and recorded
asynchronously in the execution journal. PostgreSQL failures retain critical binding and
fill evidence for retry without delaying risk-reducing protection. Restart pages recorded
bindings from PostgreSQL. A late order event retains its
original Plan binding and cannot cancel a newer Plan's protection on the same instrument.
Recovery has no age cutoff beyond the
reconciliation lookback, which is always longer than the maximum holding time.
Config edits affect new plans: existing positions retain the admitted stop
distance, TP and maximum holding duration.

`unexpected_exposure=true` means a position, or a non-reduce-only order, exists on
an instrument no open plan of this account slot claims; or a working order has no
binding to that Plan; or the venue and
the Cache disagree about a position; or a close none of the Runtime's legs sent is
waiting for the venue to confirm it. The structured `current_account.findings` name each actual object, instrument,
quantities, Plan association and check time. A `risk` observation is a bounded
transition summary, not the current full object list. The Trading page and
`trading status` show Cache and venue-only observations; Nautilus logs explain
native reconciliation. `/flatten account TTL_SECONDS`
pauses entries, closes every open position the Cache holds with a reduce-only
market order, closes every position the latest fresh venue read reports on an
instrument where the Cache holds none with a reduce-only market order for the
venue's quantity, and cancels working entry orders. Reduce-only makes that safe
on a read up to two minutes old: the venue refuses an order that would open or
flip a position, and Nautilus never opens a netting position from a reduce-only
fill. Its disposition is `accepted` with `flatten_submitted` once the closes were
sent, with `positions` (Cache), `venue_only_positions`, `venue_positions`
(`read`, or `unknown` when no fresh read existed and only the Cache was closed)
and `venue_unroutable_positions` (a venue symbol this Runtime loaded no
instrument for — close it on the Binance UI). The flat account is read from the
next venue read clearing `unexpected_exposure`, not from the disposition; the
kept stop and take-profit are canceled by that same read. With
`venue_positions=unknown`, wait until `entry_block_reason` stops saying
`venue_unverified` and flatten again, or close the position on the Binance UI;
either way the next flat venue read cancels what was kept and ends the plan. Then
`/resume`.

### TradePlan exit policy (#644)

`trading.execution.exit_policy` contains `policy_id: oi_fixed_v1`,
`take_profit_bps` and `max_holding_seconds`. All connections use the engineering
defaults of 200 bps TP and 14,400 seconds when this object is absent. These are
not an Alpha result or an optimized claim: the
[OI-chain research](research/oi-chain-backtest-2026-09-03.md) studies four-hour
outcomes with limited eligible samples and does not establish an optimal TP.
Alpha admission is unchanged.

### Nautilus-owned execution cutover (#680)

Migration `20260922_0389` is a forward cut. It deletes the private account proof's
`reconciliation`, `readiness` and `audit_gap` observations, the projection columns
that carried it (`execution_safe`, `startup_reconciled`, `account_flat`,
`reconciliation_observed_at_ns`, `facts_expire_at_ns`), the stored account
snapshot, and the plan statuses and history-gap column no current writer produces;
every row it keeps is left with `entries_armed=false` until the new Runtime writes
its own. Its downgrade refuses. The old Runtime cannot run against it and the new
Runtime cannot run against the old schema, so the Runtime is down across it:

1. `make runtime-build` with the new image while the old Runtime still runs.
2. Confirm the Binance account is flat and every plan is terminal: with the old
   Runtime, `/flatten account TTL_SECONDS` if needed, then an empty
   `current_account` and
   `SELECT count(*) FROM trading_trade_plans WHERE status NOT IN ('closed')` = 0.
   The new Runtime would adopt an open position through reconciliation anyway; a
   flat cut simply leaves nothing to prove afterwards.
3. `make runtime-down`.
4. Edit `trading.execution.risk` in the operator config: delete
   `max_total_risk_usd` and `reconciliation_interval_seconds` if present (the config
   is refused while they remain), and set `max_spread_fraction_of_stop` and
   `post_stop_cooldown_seconds` only to depart from their defaults (0.3 and 14400).
5. `make up` (applies the migration).
6. `make runtime-up`, then `make runtime-status` and
   `docker compose exec -T workers tracefold trading status` until `alive=true`
   and `entries_armed` is what the last accepted Command left.

Never use live credentials for this procedure. Roll forward; the schema backup
cannot roll back a Binance fill.

Native execution reads offer the same immutable receipts to the journal before
Cache application. The ledger enforces one economic fill per account slot,
venue environment, native symbol and trade ID. Later costs and Plan associations
are separate facts; they cannot add another economic quantity. Native order
completion records bind the exact trade set and executed quantity. A conflicting
fact stays a named write failure and is not silently replaced or dropped.

The execution list, realized totals and post-stop cooldown use the same result
projection. Once a Plan has associated native evidence, historical engine fills
cannot supply missing native trades. Complete receipts determine the actual exit
time and original business leg; the detail panel also shows the unchanged
original termination and the later verification time. The raw Plan and historical
observations remain auditable. A historical stop uses its actual exit time for
cooldown, not the time its evidence was appended.

For one closed Plan, use the bounded historical reader in the matching Nautilus
image (which includes the installed adapter):

```bash
tracefold trading verify-execution --entry-id ENTRY_SHA256 \
  --account-slot binance_usdm_primary --environment DEMO
```

The default preview is SELECT-only and shows the original Plan, exact native
receipt candidates, individual read failures, before/after result projection and
impact scope. It closes its database session before signed venue reads. It uses
the same reader/normalizer and SQL projection as the Runtime/console, with at most
16 known order chains, a 10-second timeout per chain, eight trade pages per chain
and 10,000 queued evidence rows. Incomplete reads retain explicit remaining
cursors; they do not prove zero fills or a complete result. A missing unused stop
receipt does not invalidate a separately proved complete entry/take-profit set.

Only an explicitly authorized invocation with `--apply` appends these immutable
evidence rows. This flag never submits/cancels an order, creates a TradingNode,
reopens a Plan, resets controls or injects an old exit into the current Cache.
An identical replay is a no-op; contradictory stored/native facts refuse the
append. Account, environment and terminal lifecycle must match the selected
scope. The preserved opaque execution namespace reconstructs original initial
client IDs; replacement IDs require their durable Plan bindings. This procedure
has not been applied to the incident ledger by the implementation tests.

Known realized PnL is folded from the journal's fills: exit notional minus entry
notional, signed by side, minus every recorded commission. It is known only when the
native order sets are complete, exit fills sum to the entry quantity and every commission was charged in the
settlement currency (USDT); otherwise it is absent, never synthesized as zero or
reconstructed from unrelated account balance changes. Binance
[account updates](https://github.com/nautechsystems/nautilus_trader/blob/v1.231.0/nautilus_trader/adapters/binance/futures/schemas/user.py)
update balances; they do not allocate funding to a position. The historical
`realized_pnl_usd` stays fee-adjusted and excludes funding.
`net_pnl_usd` adds signed income cashflows only with complete coverage
and a sole plan for that symbol and account interval. Missing funding remains
unknown, never zero. This per-plan result is not an account-equity statement.

Runtime control restart reads the single
`trading_execution_runtime_control_state` row for the active profile. Accepted
Runtime control dispositions advance it atomically with append-only history;
do not reconstruct current pause/halt state by scanning historical Commands.

If Decision is enabled and schema, wiring, policy, or News-generation
composition is invalid, Workers must fail startup/readiness or record Decision
`FAULTED`. Do not classify an arbitrary exception as legal no-key observer
mode.

On a fresh database, `tracefold init` creates one shared application password
and the independent bootstrap password. `make up` creates only the `tracefold`
application login and the NOLOGIN `tracefold_app` bootstrap identity, and
idempotently creates empty mode-`0600` Binance credential placeholders. Restores
must land in a fresh PostgreSQL cluster carrying that same current login shape.

Do not seed a Case to make the console non-empty. A Case exists only because a
News trade event passed Analysis admission.

Rollback is permitted only while the Binance account is authoritatively flat and
only to an image compatible with the live schema. A non-flat incident rolls
forward: the Runtime remains the sole authority until exposure is protected or
closed, and `/flatten account` is the operator's convergence command.

### Trading runtime inspection

For a bounded, read-only sample inside Workers:

```text
docker compose exec -T workers tracefold trading diagnose \
  --probe-url http://nautilus:8767/readyz \
  --status-url http://serve:8765/api/trading/status
```

For deployment identity, also inspect the two containers without dumping their
environment or secrets:

~~~text
docker compose ps --all
docker inspect --format '{{.Image}} {{.State.StartedAt}} {{.RestartCount}}' <serve-container-id> <nautilus-container-id>
~~~

The probe records the Runtime image/revision, installed Nautilus version and
process start time. Workers and serve share the application image.

The JSON carries separate request start and end times for the database, probe
and HTTP. These sequential reads are not an atomic snapshot. The database
transaction uses a local three-second statement timeout, reads at most 1,001
open Plans and 20 risk transitions, and flags Plan truncation. The Runtime
probe exposes the event loop's immutable account snapshot, writer last durable
heartbeat/failure, Bridge step durations/failures, and journal backlog/oldest
wait. Probe GET never traverses Cache or calls Binance. The CLI sends the
configured token only to a local or Compose serve endpoint and never prints it.
Keep the JSON with the incident record; attach only a redacted excerpt.

`heartbeat_at_ms`, `current_account.observed_at_ms`,
`convergence_checked_at_ms` and venue read times describe different evidence.
A new heartbeat does not renew an older account or venue observation. The
browser spends the server's remaining budget using a monotonic clock; it
does not compare wall clocks from different machines.

Migration 0400 is a forward contract cut for Runtime, serve and web. Preserve
the incident sample and a verified database backup, stop the Runtime and serve,
apply the migration, then start matching images together. Existing current
rows convert once to the v3 account shape with ownership and protection
unverified; active Plans, controls and observations remain. Roll back the
complete image/database combination from the verified pre-cut backup.

Workers no longer polls Trading execution facts or sends Trading alerts through
News push. Check `tracefold trading status` for the Runtime and current account,
`tracefold trading observations` for execution decisions and risk events, and the
open plans in the Trading console when investigating a stalled position. Container
state and `/readyz` still report process health. These read paths do not send
automatic messages for a stale Runtime, repeated refusals, overdue plans or
unexpected exposure.

## Operator lifecycle

The canonical complete-product lifecycle is:

```bash
make up
make status
make logs
make down
```

`make up` preflights Git, `uv`, Docker, Compose, an authenticated GitHub
CLI, the project interpreter (3.13, matching the image), and daemon access; runs
idempotent initialization; builds one shared Python/React image; starts
PostgreSQL when absent; runs the one-shot migration and waits for its container
to exit; starts Serve, Workers and Analysis only if it exited 0; and then runs the same
fail-closed application status gate. The wait is `docker wait`, not Compose's
`service_completed_successfully` edge alone: `up --wait` bounds that edge by
`--wait-timeout` as well, and when the budget ran out during `20260922_0387`
Compose started Workers against the old head anyway, which then restarted on
`migration_status: stale` for ten minutes (#680 PR-2). A migration that fails now
leaves Serve and Workers stopped, with the migration's last log lines on stderr.
`make deploy-image` uses the same sequence. That
preflight is a prerequisite of exactly four entries — `up`, `deploy-image`,
`db-migrate` and `runtime-build`, the ones that build an image, start the stack
or migrate the database. It used to guard fourteen, including every way of
looking at or stopping a running deployment, so an operator whose Docker daemon
had died could not run `make logs` to find out why and one on the wrong
interpreter could not run `make runtime-down` to stop trading. A read or a stop
now fails, when it fails, on the real command. `curl` left the list for the same
reason: the recipes that use it fail on their own `curl` line. It does
not recreate a running PostgreSQL container, and it never names the `nautilus`
service in any state: the execution runtime has its own image and its own
lifecycle (below). If a Runtime is running and the source Alembic head is ahead
of the live database, `make up` refuses before building, rather than migrating
the schema beneath a process that owns an open position; `make runtime-down`
first, or set `TRACEFOLD_MIGRATE_UNDER_RUNTIME=1` deliberately. An image whose
digest cannot be read is a hard failure, not a warning: every receipt it wrote
would record `image_digest=unversioned`. On failure, use `make logs`. Operator
config, two PostgreSQL password files, and named-volume data remain in place.
`make down` stops containers without deleting that volume. `docker compose down`
would delete the `nautilus` container along with the project network, so
`make down` runs the `runtime-down` recipe itself first — the same `-t 90` stop,
so `singleton.release()` still runs and the account-slot advisory lock is still
released — and prints which of the two it did. It used to exit 2 and tell the
operator to type `make runtime-down`, which is a refusal that knew the exact
command it wanted and would not run it.

All twelve published Compose bindings (`TRACEFOLD_{POSTGRES,RABBITMQ,RABBITMQ_MGMT,API,WORKERS,NAUTILUS}_{HOST,PORT}`)
are declared once in the Makefile with Compose's own defaults and exported from
there. Do not export them in an operator shell and do not add a `.env`: an
inconsistently exported port changes the rendered service definition, and
Compose then recreates the container it belongs to — which is how the database
container used to be recreated by a forgotten `export`. Override one for a
single deployment on the command line (`make up TRACEFOLD_POSTGRES_PORT=...`)
or change the default in the Makefile, in a commit.

### Execution runtime lifecycle

The Binance execution runtime is deployed on its own, from its own image:

```bash
make runtime-build     # gated build, tags tracefold-runtime:<main sha>
make runtime-up        # stop the old container, start that tag, verify readiness
make runtime-restart   # same, on the exact image the container is already running
make runtime-status    # container, health, image, and /readyz identity
make runtime-logs
make runtime-down      # stop -t 90 and remove the container
```

`runtime-build` is the only one of the six that builds, reaches GitHub, or takes
the exact-main gate; it holds the same deployment lock as `make up`.
`runtime-up`, `runtime-restart` and `runtime-down` move an image that is already
in the local store — `--no-build`, no migration, no GitHub call — because
restoring the process that protects an open position must not depend on an
authenticated `gh`, a reachable github.com, or a green check on a SHA that is
deliberately not the one being restored.

`make runtime-up` refuses before it stops anything if the configured execution
mode is `disabled`, if the requested image is not in the local store, or if the
image's Alembic head does not equal the live database head. It prints the image
it is replacing, so the rollback command is the one on the screen:
`make runtime-up RUNTIME_IMAGE=tracefold-runtime:<older sha>`. The same head
comparison is what refuses a rollback across a schema change.

The container's `stop_grace_period` is 90 s and every `stop` uses `-t 90`.
Worst-case shutdown is three sequential 20 s Nautilus stop budgets plus the
bridge's final projection write; at the old 40 s budget SIGKILL was the normal
exit, which skips `singleton.release()` and leaves a ghost backend holding the
account-slot advisory lock. Both long-lived Runtime sessions now carry TCP
keepalives on the client and server side (idle 30 s, interval 10 s, count 3), so
such a backend is reaped within about a minute instead of blocking the next
start with `oi_runtime_account_slot_already_owned`.

`restart: unless-stopped` stays. A bounded `on-failure:N` gives up after N
transient failures, and what it would give up on is the process protecting an
open position. A crash loop on an unreadable schema costs one `SELECT
version_num` per attempt, because the runtime asserts the Alembic revision
before it takes the lock or builds a node — and `make runtime-up`'s pre-stop
comparison keeps it from entering that loop at all.

That in-process assertion is a direction, not an equality: it refuses only a
database *older* than the image, meaning one missing migrations the code was
compiled against, decided by whether the live revision is an ancestor of the
image's head. A database at the image's head or ahead of it starts, and logs
both revisions. Being ahead is what the ordinary release order produces —
`make up` migrates and `make runtime-up` does not — so head equality made the
normal state of affairs a refusal to restart the process holding an open
position. `make runtime-up`'s own pre-stop check still requires equality,
because that one is an operator choosing to replace a running image and can be
answered by choosing a different one.

The cutover order for a release that changes both halves is
`make runtime-build` -> `make up` -> `make runtime-up`. A release that does not
change the Trading schema needs no runtime step at all.

Each container writes its own log file under `~/.tracefold/logs/`:
`serve.log`, `workers.log`, `nautilus.log`; the execution runtime also keeps
Nautilus' own WARN/ERROR lines in `nautilus-engine_*.log` (10 MiB, five backups).

Fresh PostgreSQL bootstrap belongs only to the image's `initdb` phase. It
creates one ordinary non-superuser `tracefold` application login from the
mode-`0600` `postgres_database_password`, assigns the public schema to it, and
then revokes the `tracefold_app` bootstrap login. Alembic and every steady
application process share that login; process identity is reported through
`application_name`. Bootstrap is not a periodic reconciler and never mutates an
unknown non-empty cluster.

`make status` is `make status-app` followed by `make runtime-status`.
`status-app` prints Compose state and returns non-zero unless PostgreSQL,
RabbitMQ, migration, Serve, Workers, Analysis, the Serve and Workers readiness endpoints,
and the HTML console all pass. The Analysis container healthcheck reads its
five-second database heartbeat and configured-model state through the Trading
status projection; a running container with a stalled Analysis loop fails the
gate. `runtime-status` is read-only and returns
non-zero when execution is enabled and no container is running,
when the container is unhealthy, or when execution is disabled and a container
is still running; it prints the running image and the whole readiness payload.
The runtime's `/readyz` answers 200 with that payload whatever it says, so the
payload is what an operator gets: `alive`, `entries_armed`,
`entry_block_reason`, `unexpected_exposure`, `protection_status`, the position
and order counts. It used to answer 503 when
`ok` was false and `curl -fsS` then discarded the body, so the one endpoint that
explains the process holding live exposure went silent exactly when it had
something to say. An unreachable endpoint is reported and the report continues;
the container state and health above it are what notice a dead process.
Deployment targets call `status-app` only, so a Runtime that
is deliberately down never fails a News release. Neither may be replaced by a
liveness-only `curl` or a Compose command whose exit status ignores an unhealthy
Worker.

### Exact-image replacement with the current database schema

An image replacement is a runtime change, never an Alembic downgrade. Use only a
reviewed local image identified by its full `sha256:` ID. The current source, the
target image and the live database must report the same migration head.

From the deployment-clean primary checkout on `main`, select the retained
previous image digest from the deployment record, inspect it locally, then
deploy that exact ID:

```bash
cd ~/Documents/Code/tracefold
docker image inspect --format '{{.Id}}' sha256:REPLACE_WITH_64_LOWERCASE_HEX
make deploy-image IMAGE_ID=sha256:REPLACE_WITH_64_LOWERCASE_HEX
```

Both `make up` and `make deploy-image` acquire the repository deployment lock,
then run `verify-main-ci` while that lock is held and before any deployment
mutation. The private implementation targets verify the inherited lock file
descriptor, so setting an environment flag or invoking them directly cannot
bypass either control. `make db-migrate` takes the same lock and runs the same
verifier (#373) because it applies working-tree Alembic revisions to the
production database without starting the application image.

Which entries are covered is no longer a list someone maintains:
`tests/deploy/test_main_ci_gate.py` derives the set from the Makefile by what each
recipe does. It classifies a recipe that runs `docker compose up`, `build` or
`run`, that applies Alembic revisions, or that runs the image directly, and it
asserts the derived set still contains the known entries so a derivation that
stopped matching cannot pass by finding nothing. The three read-only preflights
and the observe-or-stop targets (`down`, `status`, `logs`, the `*-shell` pair) are
not classified, because none of them puts new code in front of production —
`make down` stops the execution runtime and then the stack, and builds nothing.
The gate requires the primary checkout on `main`, a
clean source tree, `HEAD` equal to both the local and live remote `origin/main`,
and that exact SHA's latest `ci-gate` check to be completed and successful
under GitHub Actions integration id `15368`. It also refuses inherited Compose
topology variables and pins Compose to this checkout's `compose.yaml` and the
`tracefold` project. A pull-request result cannot authorize a squash commit,
an old green local ref cannot authorize deployment, an untrusted check with the
same name cannot authorize deployment, and missing GitHub status fails closed.

The active strict `main-production-verification` Ruleset separately requires
`ci-gate` on the exact PR HEAD, permits squash merges only, and has no bypass
actor. The deployment verifier requires the fixed workflow's successful main
push run for the resulting main SHA; PR evidence does not attest that new SHA.
See [Testing and CI implementation](TESTING.md#fixed-full-ci-implementation).

The target accepts no tag, short ID or registry reference. It never builds or
pulls, never touches the execution runtime, and it checks the checkout, Compose
inputs, active config, three migration heads, deployment lock, recreated
container IDs and Workers readiness before reporting success. It no longer reads
`news_learning_artifacts` to require the newest `deployment_receipt` and
`active_agent` rows to name the requested image: Workers writes those rows after
it boots, so that gate asked a deployment to prove a fact the deployment it was
blocking is what produces, and ordinary lag or a News epoch changing under it
refused a correct exact-image deploy. It does **not** require the image to
carry current main's revision: the
image an operator needs during an incident is by definition the previous one, and
the Alembic heads are the compatibility rule. A recorded previous image digest is only a
candidate: local retention and schema compatibility are still required.

A schema-changing release cannot roll back to an older-schema image. Its Issue must
approve a current-schema recovery or roll-forward plan before migration. Historical
implementations remain in Git history and never become compatibility code in the
runtime.

## Health and status

| Surface | Meaning | SQL/queue inspection |
|---|---|---|
| `/healthz` | process liveness | none |
| Serve `/readyz` | DB liveness plus cached startup schema/composition | no queue inspection |
| Workers `/readyz` | root running, singleton session healthy, latest O(1) heartbeat persisted within 15 s, and (when News is enabled) the runtime manifest plus linked active/deployment receipt committed, plus the `runtime_revision` / `image_digest` this process can prove | no queue inspection |
| `/api/status` | `{measured_at_ms, runtime}`: database probe plus the Workers heartbeat row | bounded control read |
| `/api/news/status` | four-layer News state (`ingest`, `broker`, `pipeline`, `delivery`) plus `control` | bounded News reads |
| `make status` | PostgreSQL, migration, Serve, Workers, readiness, and console | fail-closed lifecycle check |
| `tracefold ops validate-projections` | bounded News singleton and delivery-state invariants | strict Serve-role read |

Source degradation does not make the HTTP process unready. `/api/status` has no
provider block; it fails closed only on a stale Workers heartbeat or a
database/schema mismatch. There is no durable worker queue left to inspect:
News backlog lives in RabbitMQ and is reported by `/api/news/status.broker`
and `tracefold news bus-check`.

## RabbitMQ durable-event plane (#400)

RabbitMQ 4.3 owns News retry. There is no application retry lane, scheduler or
attempt counter, and there is no `news.retry` queue or exchange. What the broker
does is configured by one policy per queue, generated from
`tracefold.news.broker_policy` into `docker/rabbitmq/definitions.json` and
imported by the one-shot `rabbitmq-policy` Compose service (`tracefold news
bus-policy apply`) before Workers starts. Provisioning proves the policy
documents it just imported — a policy is a name-pattern rule that exists before
any queue matches it, so this holds on a fresh broker volume with no topology
at all. The per-queue effective policy is Workers' and `news bus-check`'s
question: Workers verifies it at startup (waiting out the management statistics
interval that publishes a freshly declared queue's effective policy) and
refuses to consume on a mismatch.

| Setting | Value | Why this value |
|---|---|---|
| `delayed-retry-type` | `all` | Delays counted returns (`TransientError`) and uncounted ones (`DeferError`) alike, so a defer waits exactly as long as it did through the old TTL lane. |
| `delayed-retry-min` / `-max` | `30000` / `30000` | Frozen from the removed lane's TTL. A flat delay, not a backoff: changing it needs its own production evidence. |
| `delivery-limit` | `2` | Measured on 4.3.5: a quorum queue delivers `delivery-limit + 1` times, because the first delivery carries no `x-delivery-count`. Two keeps the frozen three total handler attempts. |
| `dead-letter-strategy` | `at-least-once` | A `news.dead` that is unavailable or full must hold the message on its source queue, not drop it. |
| `overflow` | `reject-publish` | At the bound the newest publish is rejected as `BrokerBackpressure`; the oldest message is never dropped. |
| `dead-letter-exchange` | `news.dlx` | Terminal deliveries only: decode failure, `PermanentError`, spent delivery limit. |
| `max-length-bytes` | see below | Bounded so the queue rejects before the node-wide memory alarm blocks every publisher. |

### How the byte bounds were measured

`max-length-bytes(q) = p99 envelope bytes x peak messages per minute x 10`,
rounded up to a power-of-two MiB and floored at 4 MiB. `news.dead` is terminal
evidence rather than arrival-driven, so it is sized as 8,192 dead letters
instead. Envelope sizes are the broker's own `message_bytes` (body plus AMQP
properties and headers); rates are the worst single minute in a seven-day
window.

| Queue | p99 envelope | Peak/min | Bound | Backlog that buys |
|---|---:|---:|---:|---|
| `news.raw` | 2,048 B | 2,882 | 64 MiB | ~11 min of the worst minute ever observed (a Recovery backfill, itself capped at 1,000 messages per 30 s run), or ~42 h at the p99 minute of 13/min |
| `news.triage` | 512 B | 111 | 4 MiB | ~8,192 Events: ~73 min at the worst minute, ~12 h at the p99 minute |
| `news.dead` | 2,048 B | n/a | 16 MiB | ~8,192 dead letters an operator can still page through |

The three bounds total 84 MiB against a 768 MiB broker container whose default
`vm_memory_high_watermark` blocks publishers near 460 MiB. That ordering is the
point: a queue bound rejects one queue's publishes as a typed
`BrokerBackpressure`, which opens an incident and later replays through
Recovery, while the memory alarm blocks every publisher on the node with no
typed signal at all. Re-measure with `SELECT` over `news_items` /
`news_events` / `news_verdicts` per-minute counts and the management API's
`message_bytes / messages`, then edit `tracefold/news/broker_policy.py` and run
`uv run python scripts/regen_rabbitmq_definitions.py`.

### Signals

`/api/news/status.broker.queues` and `tracefold news bus-check` carry, per
queue: `messages`, `ready`, `unacked`, `delayed` (inside a native retry window),
`dead_letter_pending` (at-least-once dead letters the source queue is holding
because `news.dead` would not take them), `message_bytes` / `bytes_used_bps`
against the bound, `consumers`, and `policy_ok`. The broker health item turns
`bad` on policy drift, a queue with no consumer, any pending dead letter, or a
queue past 80% of its byte bound.

`/api/news/status.broker.last_publish_error_code` and
`last_publish_error_at_ms` retain the latest confirmed-publish failure observed
by the running Workers process; `news_broker_publish_failed:TimeoutError`
therefore remains visible after the handler delivery has been returned to the
broker. Prometheus counts the same bounded classes in
`tracefold_news_rabbitmq_publish_failure_total{reason_class=...}`. The 10-second
publisher-confirm wait is a local bounded-wait policy, not a RabbitMQ delivery
contract; RabbitMQ gives no deadline guarantee for asynchronous confirms.

A blocked dead letter is never lost, but it is not instant either: RabbitMQ
retries the transfer roughly every three minutes, so `dead_letter_pending`
staying above zero for one tick is expected during a `news.dead` outage and
staying there for many is not.

### Deployment boundary

One RabbitMQ node, one durable volume. Process, channel and broker restart on
that persisted node are covered and tested. Node-level HA and survival of the
volume's destruction are not: a real three-node cluster would be a separate
infrastructure change, and nothing here should be read as claiming it.

The healthcheck runs `rabbitmq-diagnostics` as the `rabbitmq` user, never as
root. On 4.3 the server runs as `rabbitmq`, and a root-run CLI that reaches
`/var/lib/rabbitmq` before the node has written `.erlang.cookie` creates that
file owned by root with mode 0400 — after which the server cannot read its own
cookie and refuses to boot. A volume that already carries a correctly owned
cookie hides this completely, so it only appears on a fresh volume.

### Cutting over from the removed TTL retry lane

Run this once, from the primary checkout, when deploying the #400 image onto a
deployment that still has `news.retry`. It fails closed at every step. Two things
change on the broker: the policies appear, and the three business queues lose the
arguments the policy now owns. `news.dead` is untouched — its declaration is
unchanged, and it holds evidence.

`rabbitmqctl` runs as the `rabbitmq` user with `PATH` spelled out, because `su`
resets it on the Debian image and the CLI would not be found. Deleting a queue
goes through the management API, because `rabbitmqctl delete_queue --if-empty`
and the API's `?if-empty=true` are both rejected by quorum queues — emptiness is
something this runbook proves, not something the broker will check.

```bash
RMQ () { docker compose exec -T rabbitmq su -s /bin/sh rabbitmq -c \
  "PATH=/opt/rabbitmq/sbin:/opt/erlang/bin:/usr/local/bin:/usr/bin:/bin $1"; }
API () { curl -fsS -u "$USER:$PASS" "$@" http://127.0.0.1:15672/api/queues/%2F; }
```

1. Prove the broker: `RMQ 'rabbitmqctl version'` must report 4.3 or newer. Native
   delayed retry does not exist before 4.3, and an older broker would silently
   retry immediately.
2. Apply the policies while the old image is still running, from a container
   built on the new checkout: `docker compose run --rm --no-deps rabbitmq-policy`,
   then `docker compose run --rm --no-deps --entrypoint tracefold migrate news
   bus-policy verify`. The deployed image predates the command, and a container
   is what resolves the compose broker host name; a host-side `uv run` does not
   (#537 D1). The command deliberately opens no
   AMQP connection and declares no topology, so it works while the queues still
   have their old shape. What it proves is the policy documents — every field of
   the checked-in entries, verbatim on the broker. A policy overrides a queue
   argument on 4.3 and applies the moment a matching queue exists, so the
   business queues are never argument-less and unconfigured at once; the
   per-queue confirmation that the policies actually govern the new queues is
   step 7, where Workers verifies the effective policy before consuming, and
   step 8's `bus-check`. Every deployment after this one re-applies the
   documents through the `rabbitmq-policy` Compose service.
3. Observe the old lane and let it drain: `API | jq '.[] | select(.name=="news.retry")'`.
   Record ready, unacked and the oldest message.
4. If anything in `news.retry` cannot drain deterministically, stop here. Do not
   purge it to make the migration proceed; the messages in it are business facts
   that have not been handled.
5. Stop the consumers so nothing can produce or redeclare:
   `docker compose stop -t 40 workers`. The OpenNews frames that arrive during
   this window are the ordinary deployment gap, and Recovery backfills them from
   official history afterwards.
6. Prove `news.raw`, `news.triage` and `news.deliver` each read zero for
   `messages`, `messages_ready`, `messages_unacknowledged`, `messages_dlx` and
   `consumers`, immediately before deleting them. Then delete them, so the new
   image can declare them without the arguments the policy now owns — keeping
   those arguments would work today and silently restore the old delivery limit,
   at-most-once dead lettering and message-count bound the moment the policy were
   removed:

   ```bash
   for q in news.raw news.triage news.deliver; do
     curl -fsS -u "$USER:$PASS" -X DELETE "http://127.0.0.1:15672/api/queues/%2F/$q"
   done
   ```

   Do not delete `news.dead`.
7. Deploy the hard-cut image (`make up`). Workers redeclares the three queues,
   verifies the effective policy and refuses to consume if it does not match.
   From this point no code path can publish to `news.retry`.
8. Wait at least one former TTL interval (30 s) and prove `messages_ready` and
   `messages_unacknowledged` on `news.retry` are both still zero. Then delete the
   old lane by hand — the application deliberately will not:

   ```bash
   curl -fsS -u "$USER:$PASS" -X DELETE http://127.0.0.1:15672/api/queues/%2F/news.retry
   curl -fsS -u "$USER:$PASS" -X DELETE http://127.0.0.1:15672/api/exchanges/%2F/news.retry
   ```

   `docker compose exec -T workers tracefold news bus-check` must then report empty `drift` lists and
   `policy_ok` on every queue.
9. Cold-restart the stack (`docker compose restart rabbitmq workers`) and re-run
   `make status`, `docker compose exec -T workers tracefold news bus-check`, and the open-incident check
   `SELECT cause_class, count(*) FROM news_opennews_incidents WHERE closed_at_ms
   IS NULL GROUP BY 1` — which the `0335` partial unique index now makes
   impossible to exceed one row per cause class.

   This step restarts both containers at once, which is **not** the graceful stop
   a deployment performs: RabbitMQ closes the AMQP connections underneath live
   handlers, Workers takes its fatal path, and the Receiver is cancelled without
   reporting a disconnect. Its receipt is therefore a `process_outage` interval
   opened by the next Workers process and closed when that process connects,
   recovered from official Strategy history like any other incident — never a
   `planned_shutdown`, which only a Receiver loop that exited on its own writes.
   Both are correct receipts for different events; a run that produces one must
   not be read as evidence for the other.

Rollback before step 6 may restore the previous image; the policies are additive
and the old image ignores them. After step 6 the queues carry the new shape and
after step 8 the old lane is gone, so rolling back would recreate an unconfigured
retry queue rather than the one that was deleted. Roll forward.

### Cutting over from the removed `news.deliver` queue (#598 D2)

Run this once, from the primary checkout, when deploying the D2 image onto a
deployment that still has `news.deliver`. The push Verdict's handoff to Delivery
becomes a `news_delivery_queue` row written in the verdict's own transaction, so
the queue, its `verdict.push` binding, its policy and the Janitor's repair of that
handoff all go away. Workers must be stopped for the whole of it: a Triage on the
new code writes a queue row a Deliverer on the old code never reads, and a Triage
on the old code publishes a message a Deliverer on the new code never receives.
Take the window from the News session that owns the running campaign.

1. `docker compose exec -T workers tracefold news dlq inspect` and record what is
   in `news.dead`. This image can no longer decode a `verdict` envelope — the kind
   is gone with the queue — so any `push:<event_id>` dead letter must be dealt with
   *before* the cutover: replay it on the old image (`tracefold news dlq replay`)
   or record it and purge. A `verdict` dead letter left in place is unreadable
   afterwards.
2. `docker compose stop -t 40 workers`. The graceful stop lets an in-flight card
   settle. Frames that arrive during the window are the ordinary deployment gap and
   Recovery backfills them.
3. Read `news.deliver` on the management API and record `messages`,
   `messages_ready` and `messages_unacknowledged`. Whatever is left there is a
   push Verdict whose card was never delivered, and step 6 is what recovers it.
4. `make db-migrate` with Workers down. `20260907_0374` creates
   `news_delivery_queue` and reads no other table.
5. `make up`. The new Workers declares two business queues and the dead-letter
   queue and verifies their policies; `news.deliver` is simply not in the topology
   it declares, so it is reported by `tracefold news bus-check` as drift until it is
   deleted by hand in step 7.
6. Seed the cards that were owed at the cutover, with Workers already running
   (a queue row is claimed the moment it is due, so ordering costs at most one
   poll). The window is the Janitor's own 30-minute relevance ceiling and no
   wider: a card nobody could still act on is not worth sending hours late.

   ```sql
   INSERT INTO news_delivery_queue (
     event_id, kind, state, attempts, enqueued_at_ms, next_attempt_at_ms, updated_at_ms
   )
   SELECT v.event_id, 'first', 'pending', 0,
          (extract(epoch FROM now()) * 1000)::bigint,
          (extract(epoch FROM now()) * 1000)::bigint,
          (extract(epoch FROM now()) * 1000)::bigint
     FROM news_verdicts v
    WHERE v.stage = 'triage'
      AND v.judgment_contract_version = 'news_judgment_v2'
      AND v.final_decision IN ('push', 'escalate')
      AND v.created_at_ms >= (extract(epoch FROM now()) * 1000)::bigint - 30 * 60000
      AND NOT EXISTS (
            SELECT 1 FROM news_deliveries d
             WHERE d.event_id = v.event_id AND d.kind = 'first')
   ON CONFLICT (event_id, kind) DO NOTHING
   ```

   Count what it inserted and compare it against the depth recorded in step 3.
   They should agree; a difference is a verdict whose message was still unacked
   in the Deliverer when Workers stopped, which the same statement covers.
7. Delete the queue and its policy by hand — the application deliberately will
   not, exactly as with `news.retry`:

   ```bash
   curl -fsS -u "$USER:$PASS" -X DELETE "http://127.0.0.1:15672/api/queues/%2F/news.deliver"
   curl -fsS -u "$USER:$PASS" -X DELETE "http://127.0.0.1:15672/api/policies/%2F/news-deliver"
   ```

   Then `docker compose exec -T workers tracefold news bus-check` must report empty
   `drift` lists and `policy_ok` on `news.raw`, `news.triage` and `news.dead`.
8. Prove the new lane end to end: the next push Verdict writes a
   `news_delivery_queue` row and the row is gone again once `news_deliveries` has
   it. Anything still owed is one `SELECT` away:

   ```sql
   SELECT event_id, state, attempts, error_code, next_attempt_at_ms
     FROM news_delivery_queue ORDER BY next_attempt_at_ms
   ```

   A `state = 'dead'` row is this lane's dead letter: three attempts were spent and
   `error_code` says on what. It is kept, never claimed again, and removed by hand
   once an operator has read it.

Rollback before step 4 may restore the previous image. After step 7 the queue is
gone, so a rollback would have to redeclare it (start the old image, which
declares its own topology) and re-publish the owed verdicts. Roll forward.

## Worker ownership

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
It wires one root `TaskGroup`; its due loops and dispositions are private
implementation details. Configuration cannot invent workers, owners, resource
lanes, or concurrency. An unknown child exception is a process failure, not an
individual-worker degraded state. The typed recurring business-DB overrun
below is the one resource-specific local recovery rule.

```text
tracefold serve
  -> read-only pool max 7 (6 ordinary + 1 control) -> HTTP/static

tracefold workers
  -> one singleton advisory lock and runtime_id
  -> one DB pool min 2 / max 8 / max_waiting 3
     (1 singleton lock + 2 business + 4 News lane + 1 control)
  -> one pinned singleton session / business DB executor 2 / News DB lane 4 /
     control DB executor 1
  -> finite external-operation executor 3
  -> tasks: workers-probe; when News is enabled, one RabbitMQ robust connection
     and the News consumer tasks (news-receiver, news-recovery, news-deduper,
     news-semantic, news-deliverer, news-janitor); the bounded polling loops
     (news-instruments, and with venues enabled news-quotes, news-reactions);
     workers-control
```

Quote plan/store and the wallet tape use ordinary
business permits. Event Reaction and the Janitor keep the one-slot
heavy-business gate over the same pool, so heavy work is serialized without
blocking display quote progress or consuming the four News hot-path slots. The
Quote provider calls are
bounded to 12 mandatory current source groups (concurrency 4, 10 s deadline)
plus at most two post-store Binance day reads; its 20 s cadence is start-based,
non-overlapping, and does not catch up. Reactions remain bounded to 32 merged
candle requests per 60 s turn with concurrency 4. None of these loops holds a
database connection while calling out.

Every News consumer turn is one short idempotent transaction; provider and
model work happens with no database connection held. There is no generic
scheduler, projection frontier, EDF coordinator, model arbiter, database wake
plane, startup rebuild, phased load shifting, or configurable concurrency
beyond `news.triage.concurrency`.

The control child distinguishes the pinned singleton session from its pooled
heartbeat write. Loss of the pinned advisory-lock session remains immediately
fatal. A precise transient PostgreSQL admission, timeout, pool-checkout, or
connection error from the idempotent heartbeat write is retried after 250 ms;
after 15 seconds the stale heartbeat makes readiness false without killing the
root, and recovery restores readiness. Invariant failures and an unfinished
native control future remain process-fatal. This retry does not apply to
general control writes whose commit outcome could be ambiguous.

Serve owns one read pool of seven with ordinary/control admission `6/1`,
50 ms permit wait, 250 ms checkout, one-second statement timeout, JIT off,
parallel gather off, and 8 MiB work memory. Connections and ordinary requests
default to read-only. The sole authenticated Trading Command POST opens a
semaphore-bounded short-lived write connection outside that pool; every other
HTTP route remains read-only. `tracefold news review submit` opens a short-lived connection under the
same `tracefold` login and uses one ordinary short transaction. Database
append-only triggers and business constraints—not an internal role ACL—protect
the review facts. Workers owns the exact pool/lane topology
above. Finite provider/filesystem operations share the three-slot
external capability; the OpenNews WSS socket remains a long-lived async root
child outside it. Only the owning source seam may map an outer
finite-operation overrun into its existing durable failure policy. A typed
recurring business-DB overrun remains local to its natural loop; its occupied
permit remains bound to the native future and the loop retries on its normal
cadence. Control-DB, model, cleanup, and unclassified overruns remain
process-fatal. Classification uses the typed physical capability carried by
the exception, never an operation-name or error-string prefix. A caller timeout
never releases a resource permit before the underlying future actually
completes; three stuck source futures therefore exhaust the shared external
capability even though the root heartbeat can remain healthy. Diagnose that
state from the resource-active/admission metrics and domain status. If an
underlying thread never returns, process exit is the only universal release
authority.

Each Worker DB session is exactly one bounded transaction. One transaction-local
setup statement installs the application name, statement/transaction deadlines,
JIT, parallel-gather, and work-memory policy for that transaction. PostgreSQL
restores those settings when the transaction exits, so pooling needs no reset
round trip. Every SQL statement and multi-statement repository operation is
therefore covered by the native database deadline; the async caller adds only a
bounded completion grace. An unfinished recurring business future is reported
to its loop as the typed local overrun above; every other unfinished capability
keeps the fatal policy. The default transaction deadline is the statement
deadline plus five seconds so a native statement cancellation has the same
bounded cleanup allowance as the Worker future; explicit per-operation
transaction deadlines remain authoritative.

The measured transaction is the true outer scope: setup, the capability-limited
callback, and commit or rollback produce one duration/outcome observation.
Callbacks receive only their News/Price/Instrument/Trading repositories. They
do not receive a raw connection and do not run provider I/O, Pydantic, hashing,
canonicalization, compression, large Python work, or backoff while PostgreSQL
is idle in transaction.

News consumers use a dedicated four-slot News DB lane
(`WorkerDatabase.run_news`: its own executor and gate, separate from the two
business slots) for short idempotent transactions; each message is one
transaction of a few milliseconds. `consume()` handles up to `prefetch`
messages concurrently with a per-message ack, so `news.triage.concurrency`
(default 4) is real concurrency and the only News concurrency knob;
single-active queues use prefetch 1. When the News lane cannot admit a message
the consumer raises `DeferError` and the message requeues uncounted through
the retry lane. Delivery restart reconciliation likewise waits out a typed
admission `DeferError` before claiming; statement overruns and unknown faults
remain process-fatal.

News has no projection lease: the broker's single-active-consumer and
per-message ack are the fences on `news.raw` and `news.triage`, and on the
delivery lane it is the row the claim holds with `FOR UPDATE SKIP LOCKED`,
leased until its next due time (#598 D2).

`/metrics` exposes low-cardinality worker transaction and shared capability
resource signals. Use shared resource and PostgreSQL activity/lock evidence for
diagnosis; CPU alone is not a root-cause claim.

News Feed search adds
`tracefold_news_search_requests_total{mode="asset|text",result="nonzero|zero"}`
and `tracefold_news_search_duration_seconds{mode="asset|text"}`. They record
successful first-page requests only; cursor pages are excluded, while repeated
browser polling remains repeated operational load. These counters are not
distinct user-search or user-session analytics. Labels never carry the raw
query, symbol, resolved identity, route, or user-controlled text.

News durable-event boundaries add the following bounded metrics. `stage`,
`outcome`, `queue`, `reason_class`, `cause`, and `budget` are closed code-owned
sets; Event/message/incident/Strategy IDs are log fields, never labels.

```text
tracefold_news_handoff_pending{stage}
tracefold_news_handoff_oldest_age_seconds{stage}
tracefold_news_handoff_repair_total{stage,outcome}
tracefold_news_handoff_expired_total{stage}
tracefold_news_rabbitmq_consumer_fatal_total{queue,reason_class}
tracefold_news_rabbitmq_publish_failure_total{reason_class}
tracefold_news_opennews_incident_open{provider,cause}
tracefold_news_opennews_incident_oldest_age_seconds{provider,cause}
tracefold_news_opennews_recovery_turn_total{outcome}
tracefold_news_opennews_recovery_provider_calls_total
tracefold_news_opennews_recovery_published_messages_total
tracefold_news_opennews_recovery_budget_exhaustion_total{budget}
```

`handoff_expired_total` is a Gauge despite its compatibility name: expiry is a
current marker-plus-age projection, not a durable transition that can be
incremented once. Counting it on each Janitor scan would manufacture growth.
The pending and expired gauges are each capped at 1,000 rows per stage; their
partial-index scans are bounded even when retained expired audit facts grow.

## Durable state and transaction rules

- PostgreSQL facts/control rows plus the durable broker queues are the only
  recovery sources.
- Every News write is idempotent by key; the broker owns retry, buffering, and
  the dead-letter lane.
- Success writes the current model and acknowledges the exact message in one
  application-owned transaction.
- Provider/network/filesystem I/O occurs outside DB transactions.
- Current rows use stable keys and skip unchanged payload writes.

## First checks

For missing or stale live data:

1. run `uv run tracefold config`;
2. check `/healthz` and `/readyz`;
3. inspect authenticated `/api/status`, then `/api/news/status`;
4. run `docker compose exec -T workers tracefold news bus-check` for per-queue depths;
5. run `docker compose exec -T workers tracefold news why <event_id>` for one Event's whole chain;
6. trace one stable target from fact -> Event row -> API.

| Symptom | Inspect first |
|---|---|
| no API row | current key and publication state |
| idle worker with expected work | durable target plus due/lease fields |
| stale row after a run | fact watermark, payload hash, zero-write comparison |
| growing queue | claim size, lease expiry, retry budget, terminal events |
| repeated source failure | target error state and deterministic terminal policy |
| readiness 503 | DB liveness and startup schema/composition |
| status degraded, readiness 200 | expected runtime/product separation |

The separate loopback Workers probe answers two questions, not one. `ok` is
basic readiness: this process still owns PostgreSQL, its schema and its
singleton session. `capabilities` is a separate object keyed by capability name
-- `news_ingestion`, `news_editorial`, `news_delivery`, `news_instruments`,
`news_quotes`, `news_reactions`, `market_notifications` -- each with a `state` of
`running`, `faulted`, `unavailable` or `disabled` and the reason that put it
there. The same object is persisted on `workers_runtime.capabilities` and
republished on `/api/status` under `runtime.workers_runtime.capabilities`, and
the console prints it as the **Workers 能力** card on 流水线状态 (`/news/status`),
so an operator sees a stopped lane without opening the loopback probe
(#553 PR-3). A stale runtime row publishes no report: a process that stopped
answering is not evidence that its lanes are still running.

An unexpected program error in one *optional* business task stops that task,
records its capability `faulted` with the failure that stopped it, and leaves
every other task running. Nothing restarts it: recovery is an operator restart
after the fix, which is why a `faulted` capability is a page-worthy fact even
while readiness stays 200. Analysis has its own process and its status is
reported on `/api/trading/status`; Workers does not own its lifecycle. A push sender that cannot be constructed from the
current configuration reports `news_delivery` `unavailable` with the
configuration reason, the Deliverer settles those Events `delivery_unavailable`
rather than presenting them as sent, and `/api/news/status` reports
`delivery.delivery_available` false; correct the configuration and restart.

News reception, admission and retention -- `news-receiver`, `news-recovery`,
`news-deduper`, `news-janitor` -- are **not** optional. They are the information
entry every other capability reads, so a program error there still fails the
root and the container restart that has always healed it still happens, rather
than becoming a permanent ingestion outage behind a 200 readiness.

Shared foundation failures are unchanged and still fail the root: PostgreSQL
unavailable, a schema that is not the code's head, a lost singleton session, an
unfinished native control future, and a graceful deadline overrun. A PostgreSQL
failure raised while a capability is being composed is also not confined: it
says the database failed, not that one Program is wrong.

A classified live broker incident or Recovery transient
is recoverable work, not a crashed task: Workers readiness stays up while
`/api/news/status` names the open incident or closed-pending recovery state as
`reason=recovery_pending|recovery_transient`, retains the typed error code, and
remains degraded.

## Domain traces

### Editorial News EventUpdate (#706)

```text
OpenNews -> RabbitMQ news.raw -> admission -> Item / Event / evidence revision
           -> durable semantic work -> RabbitMQ news.triage
           -> NewsAgent extraction + judgments -> adopted EventUpdate
                  |                              |
                  v                              v
           public outbox                   notification work
           -> App relay                    -> claim-level plan
           -> Trading catalyst or          -> selected intent -> card -> sender
              source amendment             -> exact receipt / ambiguous state
```

Typed OI, liquidation, smart-money and wallet frames stop at their
existing fact paths. They do not open an editorial Event or wait for
EventUpdate. Recovery material does not create a fresh reader
notification.

Admission commits a source-body revision and the semantic work marker
atomically. Exact retransmission is idempotent; a near match supplies
candidates and still wakes semantic work. The semantic worker claims a
fenced lease on the existing `news.triage` queue. Each turn saves
insert-only checkpoints and an observation; a short CAS transaction
adopts a substantive update, public outbox and notification marker.
A lost wake can be repaired from the persistent marker. Provider
failure is deferred within a bounded attempt budget and a persistent
incident identifies sustained outage. A semantic failure is not a
negative news decision.

The model route is configured by the direct News Triage and optional
Reader/fallback endpoints. Optional `llm.news_judgment` is the News-only
Jev System One route. Without it, generated judgments answer the same
narrow task contracts. A successful native batch is not voted on again.
No model has SQL, sending or order tools. The semantic stage allows
120 seconds, a notification planning stage 60 seconds, a generative
call 60 seconds and each native batch at most two seconds within its
remaining stage budget. The semantic lease is 180 seconds. These are
code-owned ceilings, not operator config keys.

NotificationPlanner records a named decision for each claim against
the adopted content and actual sent body history. A selected update
gets one stable intent. Card composition runs only for selected claims;
its failure leaves the semantic update and Trading outbox intact.
Before an external send, the Deliverer rechecks the head and reader
revision and sends the frozen body. A proved unsent attempt can retry
that intent; an uncertain send stays ambiguous and cannot be blindly
resent. Telegram's post-send quote/tradeability edit may change the
same provider message, with its own explicit edit state.

Trading consumes a `catalyst` or `source_update` before News acks the
public row. Catalyst target selection uses the changed claims and first
availability for freshness. A source update is stored idempotently as
a Trading amendment; it opens no Case and extends no TTL. The current
payload schema is `news_public_update_v1`; a legacy headline/why
catalyst is rejected by name. None of these actions grants execution
permission.

For diagnosis, inspect `/api/news/status` and the Event detail's
`processing`, `event_update`, claim decisions, public delivery and
legacy verdict separately. The current Events are not summarized by a
single historical verdict/drop ratio. Check:

1. `news_semantic_work`: wanted/done revision, lease, attempts,
   next attempt and last error.
2. `news_event_update_heads` and `news_event_updates`: adopted
   content revision and observation; a model result alone is not an
   adoption.
3. `news_notification_work`, `news_delivery_queue` and
   `news_deliveries`: plan, intent, exact body and provider outcome.
   Pending, sent, not sent and ambiguous mean different things.
4. `news_trade_events` and `trading_source_amendments`: public
   handoff and amendment; do not infer a new Trading Case from a
   correction.

Use read-only queries through the configured database access and
avoid displaying secrets or raw model credentials. A missing provider
or model affects only its capability. No manual SQL should synthesize
an EventUpdate, sent receipt, amendment or venue execution result.

### Current Analysis handoff and historical OI admission

For current OI and catalyst inputs, inspect the News outbox and Trading
Trigger/Case facts. `news_trade_events` records the immutable source payload,
its first recorded time, acknowledgement or rejection, and any conflicting
digest. `trading_triggers` preserves the accepted revision; `trading_cases`
records the target selection, pending/running/terminal state and named analysis
status. `GET /api/trading/status` reports the Analysis heartbeat and configured
policy. `GET /api/trading/cases` and the read-only Case replay show what was
frozen, judged and published. News card delivery is independent of this path.

`trading_candidate_gate_decisions` contains **historical OI v5 answers only**.
The retired lane no longer writes, sweeps or purges it. For a pre-cutover
source, the local read-only command remains available:

```bash
tracefold trading gate --limit 100
tracefold trading gate --source-key 'oi:<event_id>:oi_signal_v1'
```

A current source absent from that ledger is expected; use its outbox and Case
records. Historical rows retain their original status, stage, reason, evidence
and timestamps for audit. Neither the old ledger nor its CLI has current
signal or order authority.

### OI research replay

Not a `tracefold` command. The #459 Stage A corpus and replay are research
scripts under `notebooks/research/` (#537 PR-1):

```bash
uv run python notebooks/research/oi_research_cli.py oi-corpus pull --out DIR
uv run python notebooks/research/oi_research_cli.py oi-replay --corpus DIR
```

The first seals a Binance open-interest corpus under `--out`; the second scores
the one pre-registered rule over it and prints the table. Both are cold research
runs over local files: no database transaction, no receipt, no venue write, and
no execution adapter. Read
`uv run python notebooks/research/oi_research_cli.py oi-replay --help` for the
current flags rather than a copy of them here.

The #604 R0 exit study is the second one, and takes no flags:

```bash
uv run python notebooks/research/oi_exit_rules_replay_2026_09_07.py
```

It replays two pre-registered exit conventions over the sealed #535 corpus in
`~/.tracefold/research/oi_backtest_cache/` and rewrites
`docs/research/oi-exit-rules-replay-2026-09-07.json`. Offline, and the same cold
terms: no database, no venue endpoint, no credential.

### Price Review plane (#88)

`/api/news/status.price` is the first place to look:

- `sources[]` — one row per provider source with `received_age_ms`, optional
  `source_age_ms`, their maximum `effective_age_ms`, `freshness_basis`, raw
  timestamps and the worst `state` across that source's quotes. A source
  whose state has been `stale` for minutes is either rate-limited or blocked;
  the loop's last error names which (`venue_rate_limited`, `venue_blocked`,
  `venue_timeout`). One failing venue never clears another and never blanks a
  price: the previous row stays and simply ages. Current is stale above 45 s or
  when an applicable raw timestamp is more than 5 s in the future. Binance day
  reference is an independent post-store read: it is valid through 360 s and a
  failure or expiry removes only the percentage, never the current price.
- `reaction_partial_7d` / `reaction_complete_7d` / `reaction_unavailable_7d` —
  the Reaction backlog. A rising `partial` count with a flat `complete` count
  means the 4H leg is not landing; a rising `unavailable` count is a data
  question, not a health one, and `tracefold news review queue --view market`
  names the reason. (The HTTP route that used to answer this was removed with
  the ReviewDesk console in #256; the CLI reads the same projection.)

Read-only SQL for the same questions:

```sql
-- how old is each source's quote map, and how many quotes does it hold
SELECT source_key, target_count, jsonb_object_keys_count, received_at_ms
  FROM (SELECT source_key, target_count, received_at_ms,
               (SELECT count(*) FROM jsonb_object_keys(quotes)) AS jsonb_object_keys_count
          FROM news_quote_snapshots) s
 ORDER BY source_key;

-- the oldest Event-asset still waiting for a horizon
SELECT min(a.opened_at_ms) AS oldest_due
  FROM news_event_assets a
  JOIN news_events e ON e.event_id = a.event_id AND e.ingest_mode = 'live'
  LEFT JOIN news_event_reactions r
    ON r.event_id = a.event_id AND r.symbol = a.symbol AND r.metric_version = 'reaction_v2'
 WHERE a.opened_at_ms <= (EXTRACT(EPOCH FROM now()) * 1000)::bigint - 3600000
   AND (r.state IS NULL OR r.state IN ('pending', 'partial'));

-- why Events could not be priced, by named reason
SELECT unavailable_reason, count(*) FROM news_event_reactions
 WHERE state = 'unavailable' GROUP BY 1 ORDER BY 2 DESC;
```

Nothing here is on the delivery path. If both venues are unavailable the price
plane reports degraded coverage and the News feed, status, Triage, Delivery,
readiness and shutdown are unaffected. There is no operator knob: cadence,
caps, concurrency, freshness limit, metric version, candle interval and gap
tolerance are code-owned; `news.venues.binance` / `news.venues.hyperliquid` /
`news.venues.enabled` are the only switches, shared with the instrument
snapshot.

### Robinhood Chain concentrated net buy (#641)

The default-off roster refresh, collector, pure detector and independent price sampler run as
`news-wallet-roster`, `news-chain-tape`, `news-wallet-net-buy`, `news-wallet-prices`, with
corresponding `wallet_roster`, `chain_tape`, `wallet_net_buy`, `wallet_prices` capabilities. Their
clients and shutdown are independently supervised. A price/provider failure cannot hold the detection
turn, and a slow or throttled roster site cannot hold collection: the collector reads the last
published list out of PostgreSQL and makes no roster-provider call of its own.

Inspect operator-owned configuration with `uv run tracefold config`. Keep existing
`enabled`, `notifications_enabled`, provider URLs, roster settings, retention and polling
values. The three rule defaults are:

| Rule | Default | Meaning |
| --- | --- | --- |
| `net_buy_slow_n` | 5 | published roster addresses in the fixed 30m window; minimum 2 |
| `min_net_buy_usd` | 1000 | each address's complete window net spend; positive |
| `trigger_max_age_s` | 60 | chain→receive/detect/first-attempt age; future stamps refused |

There is one window and one quorum. `net_buy_fast_n` — the 5-minute window's quorum — is a **removed
key**: the loader forbids unknown keys, so a deployment whose `~/.tracefold/config.yaml` still holds
`net_buy_fast_n: 3` fails to start until the line is deleted. `uv run python
scripts/migrate_wallet_net_buy_config.py --config ~/.tracefold/config.yaml --output next.yaml`
removes it offline, and the migration that rewrites stored snapshots to the one-window shape is
`20260918_0382`; the running image reads the old shape, so Workers and Serve are stopped for that
deploy as usual.

The roster contains all unique valid addresses returned by `/api/traders?window=30d&stocks=false`
for chain 4663. No per-handle request or profitability ranking is made. Remove the obsolete
`news.chain_tape.roster.min_closed_trades`, `min_profit_factor`, `top_quality` and
`top_whale_by_open_cost` keys from the deployment configuration before starting the new image;
unknown old keys fail validation. Raising a Top-N value is no longer a way to enable coverage.

`news.chain_tape.roster` contains only `window` (default `30d`, source request scope) and
`refresh_interval_s` (default 3600). The source window does not change alert thresholds.
Membership changes alone open a new version. Successful unchanged refreshes and alias changes
preserve monitoring starts. New addresses build a real 30-minute monitored window; removed
addresses retain the existing sweep window without counting as current members. Rejoining
inherits a start only where uninterrupted collection is provable.

Roster and collector failures use persisted next-attempt times: 30 seconds, exponentially
increasing to 5 minutes; a longer provider Retry-After is respected. Success clears failures.
Malformed/empty/partially invalid lists preserve the previous membership; valid duplicate
addresses are deduplicated. Valid source shrinkage is published, not blocked by an arbitrary
percentage or manual approval. With a bare-list endpoint, completeness means the complete
validated response, not proof that the source itself omitted nothing.

These are engineering starting values, not optimized trading results. All removed rule
keys and `digest` are errors in the current loader. Use the
[offline configuration and stopped-writer cutover procedure](wallet-net-buy-cutover.md);
never change a shared config while an old process/image still depends on its schema.

Setting `notifications_enabled=false` keeps collection, episodes and prices running,
creates no new intent and terminates unfinished wallet sends under the existing mute rule.
Restore permits only newly opened episodes. It does not replay muted or already active
episodes. This issue does not authorize enabling live wallet notifications or trading.

#### Why there was no alert

`uv run tracefold news wallets` is the read-only diagnostic for this flow. It makes no provider call,
writes nothing, and prints five sections in the order the questions are asked (#649 §7.2):

```bash
uv run tracefold news wallets              # the last 24 h
uv run tracefold news wallets --hours 72   # a wider window for the flow and decision counts
```

| Section | Answers |
| --- | --- |
| `roster_funnel` | source address count, source scope, membership version, attempt/success/error, next retry and consecutive failures |
| `triggerability` | the configured `N`, the published roster's size, how many of those addresses have been monitored long enough to fill the 30-minute window, and how many more are needed |
| `flow_coverage` | the window's fills, receipts, addresses and tokens counted separately, the buy/sell/transfer split, priced against unpriced, the `derived_reason` distribution, and the oldest underived fill; the tape's lifetime discard totals are labelled as lifetime totals and are not a rate |
| `decision_and_delivery` | episodes, intents and each delivery state in the window, plus the reasons the unsent ones carry |
| `send_queue` | the one shared delivery queue in `market_due_delivery` order, with the head's waiting age and due time, across every family |

Read `triggerability` first. `monitoring_supported` includes the same start/gap predicate
as detection. A count below `net_buy_slow_n` means coverage cannot currently establish a
quorum, not that the market has no opportunities. Then inspect `collection_lagging` and
`flow_coverage` for a stale cutoff, blocked receipt or unpriced cash leg. Roster and collector
failures are independent; a roster failure keeps the previously published list usable.
The retired `compare_roster_windows.py` PF-ranking probe has been removed.

Read-only progress checks:

```sql
SELECT count(*) AS pending_fills, min(classified_at_ms) AS oldest_pending_ms
FROM news_market_wallet_fills WHERE derived_at_ms IS NULL;

SELECT high_water_block, high_water_tx_index, scanned_at_ms, scanned_block, scanned_log,
       coverage_from_ms, gap_at_ms, last_outcome, last_error,
       blocked_tx_hash, enrichment_error, next_attempt_at_ms, consecutive_failures
FROM news_market_wallet_tape_state;

-- what the roster refresh task last did, which is not what the collector last did
SELECT roster_last_attempt_at_ms, roster_last_success_at_ms, roster_last_error
FROM news_market_wallet_tape_state;

SELECT derived_reason, count(DISTINCT (chain_id, tx_hash, token)) AS transaction_tokens
FROM news_market_wallet_fills WHERE event_at_ms > (extract(epoch FROM now())*1000)::bigint - 86400000
GROUP BY derived_reason;

SELECT e.item_id, e.event_at_ms, e.ended_at_ms, e.change_reason,
       d.state, d.error, d.created_at_ms AS intent_at_ms, d.first_attempt_at_ms
FROM news_market_wallet_events e JOIN news_items i USING (item_id)
LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
ORDER BY e.event_at_ms DESC LIMIT 100;
```

Missing receipts retain the cursor indefinitely; they are not silently counted complete.
Reorg/overlap inconsistency records a coverage gap and suppresses positive conclusions.
The collector does not automatically rewrite old facts. Investigate the named range against
the RPC and apply the established recovery procedure. Network failure leaves the last
scanned chain time visible; wall time never advances a completed window.

A real missing receipt stops the current turn at the last complete prefix; it does not
repeatedly query the successful suffix or let the detector see a later buy before a missing
sell. On recovery the same durable position resumes and bounded batches drain normally.
Optional traded-token metadata is best effort. Cash decimals are queried independently;
unknown cash precision stays unpriced and never defaults to 18. Only explicit contract
revert is cached as unsupported metadata; transient RPC errors are retryable observations.

A first report is re-evaluated at the collector's committed cutoff `(scanned_block, scanned_log)`.
A cutoff that has not reached the trigger or contains underived evidence defers without spending
an attempt. A stale trigger or window no longer satisfying the one rule is suppressed.
Re-evaluation uses the same pure function as detection; no external pricing call is required.

`advance()` reports actual `rpc_requests` and `rpc_bytes`, including real metadata/header calls
and excluding cache hits. Address batching is not enabled speculatively: it reduces individual
request size but increases request count. Use a suitable production endpoint through `rpc_url`
and measure full-range latency/429/backlog under the actual address set before changing batching.
The 147/201/256-address integration cases are controlled transport checks, not live endpoint capacity
certification. Never drop addresses or advance incomplete coverage to satisfy a latency target.

`initial_snapshot` remains immutable; the detector alone updates current business facts.
Unchanged scans write no latest snapshot. Timers do not create episodes. Read price
`target_at_ms`, actual `at_ms` and status together; no baseline means unknown change.
The detector does not wait for or fetch a trigger quote. Late quotes are not backdated.
The raw archive holds retired wallet evidence; current APIs do not render an old strategy.

## Migrations

Alembic has one root, baseline `20260831_0340`, and one head, named by
[the migration guide](MIGRATIONS.md) rather than restated here — a head literal
copied into four documents is four things to update and three of them go stale.
A fresh PostgreSQL 18 database applies the baseline and every revision after it
in order; each revision's own docstring carries its evidence.
Four of them need an operator step before the upgrade runs: `20260901_0347`
drops twenty-two read-only execution tables, `20260903_0355` drops the six
dead `trading_cases` columns and refuses to run while any row still holds a
retired state or admission value, `20260903_0356` drops the profile and
activation ledgers, and `20260903_0357` drops the JSON-shape CHECKs with the
nine unread columns and their payload keys. All four archive to
`~/.tracefold/backups/`
first; [the migration guide](MIGRATIONS.md) carries the exact commands.
`20260905_0365` needs no archive step, but it does need writers stopped: the OI
ledger gains a unique key the old writer does not know and the liquidation table
renames a column the old writer names, so run it inside the ordinary `make up`
stop rather than against a live Workers process.

`20260908_0375` changes wallet outcome identity and introduces durable fill-derivation progress. Stop
old Workers first, apply the migration under the existing maintenance gate, then start matching new
Workers and Serve. Do not overlap old writers with the new schema. Existing fills retain their stored
classifications and receive `derived_at_ms = classified_at_ms`; they are not replayed into new buy
opportunities or ghost notifications. Existing outcomes keep their old delivery association and use
`reference_kind = 'legacy_delivery'` with `reference_price IS NULL`. Their old entry/mark is not
reinterpreted as a known observation or notification price. The migration is transactional and its
downgrade is refused; recover by roll-forward or verified backup restore.

`20260915_0378`, `20260915_0379` and `20260915_0380` are the #651 News cut and go
out as one sequence. Each of the first two drops and re-adds
`news_verdicts_current_judgment_check`, validating every verdict row in place,
and each moves an identity the running Workers emit: `0378` takes
`PROGRAM_VERSION` to `news_semantic_program_v10`, makes the typed asset a
database fact and moves the Reaction ledger to `reaction_v2`; `0379` takes the
editorial contract to `news_editorial_v3` and `TRIAGE_POLICY_VERSION` to
`news_triage_policy_v14`; `0380` replaces the review contract functions for
`news_review_v7` with `reader_contract_v3`. `20260922_0387` is the #675 PR-2 cut
and goes out the same way: it takes the judgment contract to `news_judgment_v3`,
the editorial contract to `news_editorial_v4`, `TRIAGE_POLICY_VERSION` to
`news_triage_policy_v16`, `PROGRAM_VERSION` to `news_semantic_program_v13` and
the review contract to `news_review_v8`, all in one transaction. Old Workers
cannot write under the new CHECKs and new Workers cannot write under the old
ones, so there is no
overlap window: stop Serve and Workers, drain the News queues, apply the three
revisions under the existing maintenance gate, then start the matching new
image. The separate Nautilus runtime writes no `news_*` table and needs no stop
of its own; a deploy that also carries a Trading schema change keeps the
`make runtime-down` -> `make up` -> `make runtime-up` order. Nothing is
rewritten: v8/v9 verdicts, `news_editorial_v2` judgments, `reaction_v1` rows and
`news_review_v6` reviews stay exactly as written and stay readable, and an Event
measured before `0378` reports no reaction number until the typed planner has
measured it again. All three refuse their downgrade; recover by roll-forward or
verified backup restore.

`20260923_0390` takes `TRIAGE_POLICY_VERSION` to `news_triage_policy_v17` (the
#504 per-storyline budget is deleted) and only widens the judgment CHECK's policy
lists, so it needs no step beyond the ordinary stopped-writer `make up`: the
revision lands before the new Workers start, and the old image's v16 writes
would still validate. It drops and re-adds `news_verdicts_current_judgment_check`,
revalidating every verdict row like `0386`. The new image refuses to start while
the operator config still sets `news.policy.storyline_budget_window_s` or
`storyline_budget_max`.

`20260904_0360` needs no operator step and refuses nothing: it collapses any
duplicate admission `source_key` to the row every reader already showed. It is
still destructive — ten columns and one payload key go with it — so the same
archive is taken before it runs. Because it changes the schema the execution
Runtime writes, `make up` refuses to apply it while the Nautilus container is up:
the deploy is `make runtime-build` -> `make runtime-down` (account flat) ->
`make up` -> `make runtime-up`.

This source may merge or deploy only after the supported pre-cut database
is advanced to the old terminal revision with its recorded image, backed up,
and put through the #449 stopped-writer role catalog cut while retaining the
same Alembic identity and all business rows. The issue receipt records the old
SHA/image, backup, before/after identities, and startup smoke.
Current source has no pre-baseline upgrade or old-role repair path. New schema
changes resume as immutable linear forward revisions after the baseline; an
irreversible downgrade is a verified backup restore. Stop Serve, Workers, and
Nautilus before migration; the maintenance gate refuses to run while the steady
Workers lock is held.

The normative authoring checklist, required evidence, and 0330–0332 object
authority/cost audit are in [the migration guide](MIGRATIONS.md). Published
revision files are immutable; a correction is a forward revision.

An existing volume at 0283 needs no new password or offline role bootstrap.
Before its first 0284–0295 upgrade, take a restorable volume backup, stop Serve
and Workers, run the normal migration, then deploy the matching image. The
migration owns the narrow ReviewDesk grants; the existing Serve credential is
unchanged.

0276 drops `news_title_presentations`, `token_discovery_results`,
`token_discovery_dirty_lookup_keys`, `asset_profiles`,
`asset_profile_refresh_targets`, `cex_token_profiles`, `token_image_assets`,
`token_image_source_dirty_targets`, `token_profile_current`,
`token_profile_projection_frontiers`, and the four unused `checkpoint_*`
tables, and deletes their `queue_terminal_events` rows.

0277 drops the whole GMGN lane in child-before-parent order:
`news_event_market_marks`, `asset_identity_current`,
`asset_identity_evidence`, `enriched_events`, `event_anchor_backfill_jobs`,
`market_tick_current`, `market_ticks` (with its default partition),
`price_feeds`, `cex_tokens`, `token_intent_lookup_keys`,
`token_intent_evidence`, `token_intent_resolutions`, `token_intents`,
`token_evidence`, `event_entities`, `events`, `raw_frames`,
`registry_assets`, `collector_pending_items`, `persisted_live_events`,
`us_equity_symbols`, and `provider_circuit_state`; it drops the
`forbid_market_fact_update()` function and deletes the
`queue_terminal_events` rows of `event_anchor_backfill_jobs` and
`collector_pending_items`.

0278 drops the whole Macro lane in child-before-parent order:
`macro_document_analysis_jobs`, `macro_document_analyses`, `macro_documents`,
`macro_fed_official_role_facts`, `macro_release_facts`, `macro_series_facts`,
`macro_module_current`, `macro_module_frontiers`,
`macro_dataset_projection_states`, `macro_acquisition_targets`,
`market_position_facts`, `market_settlements`, `market_observations`,
`market_instruments`, and `queue_terminal_events` (whose only writers were the
Macro repository and the projection frontier); it also drops the
`reject_macro_fact_mutation()` function. No revision performs a provider,
broker, or outbound call.

0279–0283 add listing admission, the instrument universe, legacy label-v1 and
Price Review. 0284 freezes fact/evidence versions. 0285 verifies legacy-label
migration, hard-deletes `news_event_labels`, creates append-only ReviewDesk
evidence and the security-barrier task view, and grants Serve only INSERT on
the two review fact tables in addition to its read access. 0286 adds content-addressed datasets,
candidate/evaluation/deployment artifacts, pairwise cases and exact model
recordings. 0287 adds durable canary activations, one assignment per Event and
runtime manifests. 0288 adds the bounded retention function, cold-Janitor
state, and indexes used by its ordered batches. 0289 reasserts the exact
Workers `SELECT`/`INSERT` evidence-snapshot grant and revokes rewrite access;
`db audit` now verifies that role contract so a missing runtime grant fails the
rollout check before a live Event discovers it. 0290 removes an ineffective
`FOR SHARE` from the append read: PostgreSQL otherwise requires UPDATE for the
locking SELECT even though the immutable table rejects UPDATE. Migration never
calls the model or derives a release PASS. 0291 removes the local OpenNews
Strategy allowlist. 0292 adds Program version/SHA to verdicts, Predictor/call/
attempt/route usage and cost fields to model recordings, and the append-only
`news_learning_epochs` row whose database deployment timestamp starts
`program_v1`; its explicit disposition makes all Prompt-era learning evidence
audit-only and promotion-ineligible. `0293` preserves that row and appends
`program_v2` after correcting the semantic fast-retry state machine and
hardening the restatement sentinel, making `program_v1` evidence audit-only as
well. `0294` preserves both prior Program epochs and appends `program_v3` for
the expert quality baseline and semantic normalization, making `program_v2`
evidence audit-only for its release decisions. `0295` preserves v1-v3 and
appends `program_v4` for the D-generation ownership hard cut; `0298` preserves
v1-v4 and appends `program_v5` for candidate-conditioned ToldContext, making
every earlier cohort audit-only for current release decisions. `0301`
hard-renames persisted `priority` to `queue_priority`, adds
atomic editorial/scored/runtime-manifest judgment identity, trips old canaries,
and starts `program_v6` for factory/executable v4 and policy v10. None of these migrations
deletes history or claims a release PASS.
`0303` preserves that history and appends `program_v7` for factory/executable
v5 after the #162 Program/Learning package split; v6 evidence remains audit-only.
`0304` is the #193 strategy-artifact hard cut: it adds no column, trips every
armed or active canary whose candidate the new image cannot load, and appends
one migration receipt to `news_learning_artifacts`. It leaves `program_v7`
open on purpose — the artifact serialization and the Program root changed, the
evidence did not — so the reviews accepted under the rubric of the day stayed
eligible and the epoch row goes on naming what the epoch was opened with. It is
irreversible.
`0305` is the #193 compile-record hard cut: it adds `compile_record` to the
learning-artifact kind constraint while keeping `compile_receipt` in it, and
trips every armed or active canary whose candidate was registered against the
retired receipt chain. It leaves `program_v7` open for the same reason and is
irreversible as well.
`0315` is the #288 exact source-contract route and Event-kind hard cut. It
trips open canary activations and appends the factory-v6 to factory-v7 receipt,
but neither rewrites nor appends the `program_v7` epoch row. Earlier rows and
bundles remain immutable audit history; exact current-bundle acceptance makes
prior-factory evidence audit-only, so the factory-v7 cohort starts at zero.
`0318` is the #306 prompt-layer hard cut. It
appends `program_v8` for `factory_v8` and trips every armed or active canary.
Two byte changes land under that one identity migration, deliberately paid once
rather than twice: the sealed kernel / nine RulePacks / advisory / authority-seal
layering collapses into one seed instruction per Predictor, and the Program's
self-owned chat transport composes the request envelope DSPy's JSON adapter used
to compose. `program_v7` evidence — which closed with zero accepted candidates,
zero canary activations and two empty advisory instructions — becomes immutable
audit history. It adds no column and is irreversible.
`0319` is the #310 envelope hard cut and the current epoch boundary. It appends
`program_v9` for `factory_v9`, trips every armed or active canary, and re-issues
the stable root over unchanged seed texts. The self-owned transport's
structured-output constraint now follows the endpoint — `json_schema` where
supported, `json_object` with the same schema inlined into the system message
for DeepSeek-class endpoints — which moves fallback-route prompt bytes; the
first hours of the v8 cohort, a third of whose verdicts degraded against the
rejected format, become immutable audit history.
`0320` adds the News catalogue's immutable listing-validity events and refuses a
warm migration. Its execution ledgers were dropped by `20260901_0347`.
`0321` is #314's computed-identity cut and the last epoch migration there will
be. It adds `bundle_sha` and `envelope_sha256` to `news_learning_epochs`, ties
`epoch_id` to `left(bundle_sha, 8)` by CHECK, relaxes `program_factory_id` to
nullable, and allowed the startup barrier to open the running bundle's epoch
itself; the append-only trigger remains the durable mutation boundary. The
artifact loses its `factory_id` field, which
re-issues the stable root over unchanged seed texts one last time, so the first
deployment after this migration opens a new `bundle_<sha8>` epoch and trips every
armed or active canary. After it, an identity migration is a code change plus a
re-pinned line in `tests/contract/test_program_release_identity.py`.
`0322` adds the durable News delivery edit-intent lifecycle and its stale-edit
index; it performs no provider call and requires no new credential or runtime
role. `0323` adds the receipt-bound deletion lifecycle and its stale-intent
index for authoritative five-venue single-name absence.
`0324` replaces both lifecycle shape constraints with two-valued predicates so
PostgreSQL `NULL` semantics cannot admit partial edit or delete intent. It fails
closed if an existing row violates either lifecycle before replacing the constraints.
Issue #325 owns the operator-approved recovery: keep the database at `0323`,
repair only the invalid lifecycle tuple from provider evidence, and then roll
forward to `0324`; never start an older-schema image after that migration commits.

The retired `trading.regime.*`, `trading.policy.*`,
`trading.candidates.symbol_cooldown_seconds`, `trading.candidates.max_rank_in_window`,
`trading.candidates.news_lookback_seconds`, `trading.candidates.oi_lookback_seconds`,
`trading.candidates.max_dspy_cases_per_day` and `llm.trading_decision_model` keys
must not appear in `~/.tracefold/config.yaml`; the settings models are
`extra="forbid"`, so any one of them fails Serve and Workers at settings load.
[Public contracts](CONTRACTS.md) carries the keys that are accepted today.

Before applying 0278 remove `providers.macro_sources` and the
`llm.macro_document_analysis_*` keys from `~/.tracefold/config.yaml`; the
settings schema rejects them and Serve/Workers fail to start with them
present. Verify after restart: `tracefold db audit` reports
`migration_status` `ready`, current News table counts, and
`news_schema.exact`; `tracefold news bus-check` shows one consumer on
`news.raw` and `news.triage`; `/api/news/status.state` becomes `ready` only
after the WSS, broker, model, delivery, and Workers health checks are all green;
`/api/macro/overview` answers `404`; and the first candidate
Event receives a Triage verdict within seconds.

## Operator actions and retention

There is no durable worker queue and therefore no terminal-evidence retry,
archive, or quarantine action. Failed News messages retry through the broker's
30 s lane three times and then dead-letter to `news.dead`, which
`tracefold news dlq inspect|replay|purge` owns. Current models retain one
stable row per identity.

News retention has two tiers (`news.retention`, issue #81). The Janitor deletes
`news_items` older than `raw_days` (30), which cascades to Event-owned verdict,
delivery, member, asset, band and evidence snapshots. An Item behind an Event
that carries a verdict or an accepted ReviewDesk judgment is evaluation
evidence and survives to `judged_days` (365). Accepted reviews, external-miss
snapshots, sealed datasets/evaluations/model recordings, canary assignments
and deployment/rollback receipts are append-only audit evidence; a retention
change must preserve every foreign-key dependency and the ability to replay a
sealed dataset. Narrowing the evidence window silently destroys the only
ground truth the system has.

Raw retention selects stable `(observed_at_ms, item_id)` candidates and deletes
at most 500 rows per transaction, four transactions and three seconds per
Janitor turn. Each batch rechecks the 30-day raw and 365-day judged predicates
inside its `DELETE`; reaching a row/batch/time budget leaves backlog for the
next turn. Band expiry is likewise an ordered 500-row transaction. The raw
retention metrics report deleted rows, batches, wall time, capped backlog
sample, whether the sample hit its cap, and oldest eligible age. Each batch,
band expiry, and learning-retention call uses a separate cold transaction.

The typed market ledgers are Item-owned children since migration
`20260905_0365` (#553): `news_market_liquidations` and `news_market_smart_money`
have a unique `item_id` foreign key with `ON DELETE CASCADE`, and
`news_oi_signals.source_item_id` already had one; `0372` added
`news_market_wallet_events` on the same terms (#572 PR-2). Purging an Item
therefore takes its typed fact with it, which is the point — a liquidation whose
Item was gone used to survive as unreachable evidence, and `0365` deletes the
orphans it cannot adopt.

Market Items are exempt from the raw tier. `RAW_RETENTION_CANDIDATE_SQL` and its
`DELETE` both carry a predicate that lets a row with a non-null `market_kind`
expire only past the judged cutoff, so an OI frame, a liquidation report or an
account report is retained for `judged_days` regardless of verdict, push or
parse status. A market observation can never carry a verdict or an accepted
review, so the ordinary evidence predicate could never promote one, and under
`raw_days` alone every one of them would expire in 30 days while the news beside
it kept a year.

Learning evidence follows #118's separate deterministic policy:

- an unreferenced `news_model_recordings` or `news_learning_cases` row is kept
  for at least 90 days;
- a run named by an evaluation report/release receipt and every ordinary
  learning artifact is kept for at least 365 days;
- the newest manifest for each of the current and previous distinct stable
  bundles, plus an armed/active canary, pins its candidate, datasets, reports,
  observations, per-case rows and exact model recordings regardless of age;
- Redeploying an earlier image re-appoints that bundle and re-enters its existing
  epoch rather than opening a new one, and the epoch keeps its original
  `starts_at_ms` because the table is append-only. Evidence produced by the
  intervening deployment is therefore inside the restored epoch's window for the
  readers that can only compare timestamps — external-miss eligibility above all,
  since an external miss carries no bundle to filter on. Freezing a dataset
  immediately after a rollback will carry those misses; if that matters, freeze a
  window that starts after the re-appointment.
- `news_learning_epochs` is append-only permanent audit truth, whether a
  migration or the startup barrier wrote the row. An epoch change alters
  eligibility, not retention: all earlier evidence remains auditable until the
  existing deterministic retention policy makes an otherwise-unpinned row
  eligible;
- `active_agent`, deployment and rollback receipts are permanent audit truth;
- every purge call deletes at most 500 recordings, 500 cases and 500 artifacts.
  Eligible counters are capped at 501: `501` means “at least one more full
  batch”, not an exact global count. A purge error is recorded but does not
  change News readiness or stop ingest/delivery.

Capacity assumption for V1: request and response JSON are each capped at
64 KiB, so a worst-case 200-case, two-arm, three-trial run is under roughly
150 MiB of payload before PostgreSQL/index overhead; typical one-trial runs
are much smaller. Operators should alert on a non-zero eligible count that
does not fall across Janitor turns, any `last_error_code`, or persistent table
growth outside the 90/365-day envelope. The purge shares the one-slot heavy
admission with Event Reaction and Trading, never ordinary Quote admission or
the four-slot News hot lane.

### Backup, recovery, and restore drill

The packaged deployment tier is a single-host, operator-managed Compose
database: it provides no automatic failover, replica, backup scheduler, WAL
archive, or PITR service. The deployment owner must provide encrypted daily
backups and an additional verified backup immediately before every destructive
migration. The operating targets are RPO at most 24 hours and RTO at most four
hours; the pre-migration snapshot makes the migration cutover RPO the snapshot
time. These are targets only when the external backup owner monitors freshness
and restoreability. If a deployment requires a tighter RPO, its platform owner
must archive WAL and operate PITR outside Tracefold; the application neither
ships nor retains WAL.

The weekly scheduled diagnostics run an isolated production-image restore
test. Run the same entry manually with
`TRACEFOLD_TEST_POSTGRES_DSN=<dedicated-admin-dsn>` and
`TRACEFOLD_TEST_POSTGRES_MIGRATION_DSN=<application-dsn>` set, then run
`make postgres-restore-drill`. The application DSN uses the shared `tracefold`
credential; it is never emitted in the result.
It creates uniquely named disposable source/target databases, seeds
representative News current/archive and Trading facts, uses the exact
PostgreSQL 18 Bookworm client image for custom-format dump/restore, migrates to
head directly as the ordinary application login, performs deep schema/identity audit
and bounded smoke, records head,
duration and identity counts, then drops both databases. It never reads or
writes the database named by the supplied DSN. This proves the mechanism, not
the freshness of a live operator backup.

Live restore/audit procedure:

1. Restore the PostgreSQL backup into an isolated database and migrate only to
   the image's recorded schema head; never set
   `tracefold.learning_retention_purge` or issue manual DELETEs. That setting is
   the append-only bypass the `SECURITY DEFINER` function
   `purge_news_learning_retention()` sets for itself, transaction-locally, and
   it is the only sanctioned writer of it.
2. Run `docker compose exec -T workers tracefold db audit --deep`; confirm migration head, exact News table
   set and role grants. Read `news_learning_retention_state` and retain its
   pre-restore snapshot for comparison.
3. Select the latest manifest for each distinct `stable_bundle_sha`; for the
   newest two bundles, verify candidate → release evidence → report → dataset,
   `news_learning_cases` and `news_model_recordings` references are present.
   Recompute content hashes through CandidateEvaluator/record-replay; do not
   accept row counts alone as proof.
4. Verify every deployment/rollback receipt from the backup still exists and
   compare counts plus oldest ages before allowing Workers to start. If any
   pinned link is missing, keep the restored system offline and restore an
   earlier backup; the irreversible migration's rollback is backup restore.

Destructive migrations use bounded timeouts, transform data before constraints,
drop children before parents, avoid `CASCADE`/`IF EXISTS`, and preserve material
facts plus unresolved side-effect/terminal evidence.

## PostgreSQL performance diagnosis

The database is both the fact store and the durable execution plane. Diagnose
pressure from database evidence before changing worker cadence, indexes, or
retention.

Start with redacted runtime context:

```bash
uv run tracefold config
curl -fsS http://127.0.0.1:8765/readyz
make status
```

Then inspect live activity, blockers, and normalized top SQL:

```sql
SELECT pid, application_name, state, wait_event_type, wait_event,
       now() - xact_start AS xact_age,
       left(query, 160) AS query
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY xact_age DESC NULLS LAST;

SELECT blocked.pid AS blocked_pid,
       blocking.pid AS blocking_pid,
       left(blocked.query, 120) AS blocked_query
FROM pg_stat_activity blocked
JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocker_pid ON true
JOIN pg_stat_activity blocking ON blocking.pid = blocker_pid;

SELECT calls,
       round(total_exec_time::numeric, 1) AS total_ms,
       round(mean_exec_time::numeric, 3) AS mean_ms,
       rows, shared_blks_read, temp_blks_written,
       left(regexp_replace(query, '\s+', ' ', 'g'), 220) AS query
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY total_exec_time DESC
LIMIT 20;
```

`tracefold db query-audit` (in the Workers container) verifies that every public HTTP route is
assigned to a bounded read-query family (`/readyz`, `/api/status`,
`/api/news/*`; `/healthz`, `/metrics`, and `/api/bootstrap`
are declared no-SQL) and checks that every query can be planned. `uv run
tracefold db query-audit --analyze` executes those read-only queries with JSON
`EXPLAIN (ANALYZE, BUFFERS)` and fails on a large-table sequential
scan, more scanned rows than that query declared it may touch, any temporary
read/write blocks, or read/return amplification above the budget declared by
that production query. An empty development database proves only SQL and route coverage;
production-scale plans need a production-sized database. Each runtime owner
supplies the same bound statement builder used by its serving read; the App
layer only composes those specs with route coverage, so an audit-only SQL
approximation is not accepted. `/api/news/status` is the worked example: the
eleven statements `status_snapshot` executes are eleven named specs holding the
same constants the production read executes, and a contract test drives that read
against a recording connection so SQL and bound parameters both have to match
(#570 A2). The instrument, asset-usage and price reads the HTTP route composes
after the snapshot are still unregistered.

`tracefold db audit` is the online fast path. It uses catalog/statistics
row estimates plus O(1) migration, schema, role/grant, PostgreSQL-major,
extension, and key-session-setting checks. It reports the externally declared
container image identity without pretending PostgreSQL can attest that digest;
it does not issue exact `COUNT(*)`
against every business table. `tracefold db audit --deep` adds those
exact counts and is reserved for offline migration/restore evidence.

Rows scanned and rows returned are separate measurements in the report, and a
scan is judged on the first (#570 A1). A node's `Actual Rows` is what it handed
upward after its own filter; `scanned_rows` adds what it discarded -- `Rows
Removed by Filter` and an index recheck -- with the loop count multiplying each
per-loop average. (`Rows Removed by Join Filter` is not counted: PostgreSQL
reports it on join nodes, which count candidate pairs rather than table rows, and
the rows those pairs were built from are already counted at the scans below.)
`large_seq_scans` names a sequential scan by the table rows *one pass* of it
actually read, never by `Plan Rows`: that is the planner's estimate of node
*output*, so a filter discarding 50,000 rows to return none has a `Plan Rows` in
the hundreds and used to be recorded as zero rows read. Per pass, because the
threshold is a claim about the table: a nested loop that scans a 20-row table a
thousand times has read no large table, and its 20,000-row total is reported as
`scanned_rows` beside `scanned_rows_per_loop` and `loops` rather than folded into
the judgement. `discarded_rows`, `scan_output_rows`, root
`shared_hit_blocks`/`shared_read_blocks` (root only, because buffer counters are
cumulative over children) and execution time are reported beside them.

Read/return amplification divides those scanned rows by the rows the statement's
own result is built from, and each query spec owns its budget. The denominator
comes from the plan, not from a per-query flag, and it folds only when the whole
statement folded. A statement that returned more than one row multiplied
something per row, so its returned rows stay the denominator however many
aggregates its plan contains: a page carrying `(SELECT count(*) FROM
observations)` beside fifty collapsed groups would otherwise divide the window by
itself and report a bounded read. Zero rows is the same case, not a smaller one --
an ungrouped aggregate emits exactly one row, so a result of none did not come
from one, and a group filter that matches nothing still reads the whole window.
A statement that returned exactly one row read everything it read to produce that
row, so an ungrouped aggregate's input is the honest denominator there -- asking a
bounded `count(*)` to return as many rows as it counted is a threshold no correct
aggregate can meet. A grouped aggregate keeps the rows it returns, because that
result grows with what it read.

Amplification cannot bound a folding read by itself: what a `count(*)` scanned
divided by what it folded is 1 whatever it scanned. The sequential-scan rule does
not cover for it either -- it catches an unbounded read only when that read is a
sequential scan of at least 10,000 rows in one pass, and a `count(*)` over a
million rows reached by an index path trips neither rule. So every spec declares
`max_scanned_rows`, the rows that read may touch whatever shape its plan takes,
and exceeding it is `scanned_rows_budget_exceeded`. Composition refuses a spec
that omits it, so the fold can never become an exemption. These ceilings are
declared bounds rather than measurements: an owner tightens one as real numbers
arrive, and a read that outgrows its claim is reported by `db query-audit
--analyze` for an operator to look at rather than failing anything that serves.

The three ceilings in use, and what each is measured against. The only production
figures available are #570's, so they are the only thing these are argued from:

| ceiling | reads | argument |
| --- | --- | --- |
| `INDEXED_ROW_SCAN_BUDGET` 10,000 | 22 single-row and small indexed lookups | The same number as `LARGE_SEQ_SCAN_ROWS`, deliberately: this repository already calls 10,000 table rows in one pass a large read, so an indexed lookup that touches that many is not the lookup it claims to be. The widest table these reach is `news_market_instruments`, ~16,500 rows in #570's snapshot, and they reach it by index. |
| `BOUNDED_WINDOW_SCAN_BUDGET` 100,000 | 46 windowed page, funnel and aggregate reads | ~7.5x the widest pass #570 measured on production: the status pipeline's Evidence scan, 13,273 rows actual against 61 estimated. These are bounded by a time window rather than a row cap, so there is no cap to derive from; the margin is for growth within the window, not a claim that 100,000 is safe. |
| `MARKET_WINDOW_SCAN_BUDGET` 500,000 | `news_market_groups`, `news_market_sources`, `news_market_delivery_summary` | `news_market_groups` caps its materialised window at `MARKET_WINDOW_ROW_CAP` 5,000 and joins it five ways, so a cap-derived ceiling would be ~30,000. The other two have no row cap at all: `news_market_sources` materialises the entire 168 h window, and `news_market_delivery_summary` aggregates the same window over `news_market_deliveries`. 500,000 is therefore a loose first ceiling -- ~500x the 977 observations #570 measured -- and its only claim is that two uncapped reads now have a ceiling where they had none. Tighten it, or derive it from a cap, when #570 A3/A4 bound those reads. |

None of these ceilings is exercised by the empty-schema catalog test: an empty
database scans nothing, so `db query-audit --analyze` there proves SQL and route
coverage and nothing about volume. Only the real-PostgreSQL guard test builds
tables large enough to cross one.

Use ad hoc `EXPLAIN (ANALYZE, BUFFERS)` only on a representative bounded
query. Since `ANALYZE` executes mutating SQL, wrap `INSERT`, `UPDATE`, `DELETE`,
or `MERGE` in `BEGIN` and `ROLLBACK`.

Frontier-backed hot paths claim narrow stable keys and hydrate wide JSONB only
after selection. Partial indexes must match the real due/status predicate. An
idle worker must not scan broad facts merely to prove that no work is
due. Use one representative `EXPLAIN (ANALYZE, BUFFERS)` for a
bounded evidence path; do not create a second planner-assertion control
plane. Current models remain bounded by stable product keys; a
latest-generation pointer is not a retention policy.

Worker sessions disable PostgreSQL parallel gather and JIT and use 16 MB
`work_mem`; API sessions keep PostgreSQL defaults. This prevents bounded
background iterations from multiplying into all available PostgreSQL CPU
workers. The News feed hot paths use ordered composite indexes; do not replace
them with periodic broad scans.

Compose uses the official PostgreSQL 18 Bookworm image and preloads only
`pg_stat_statements` for query diagnosis. `compute_query_id` remains enabled.
Use Compose logs for container output and the supported Tracefold database
health, audit, query-audit, status, metrics, and `ops` commands for diagnosis
and repair. There is no repository `ops/` infrastructure tree, auxiliary
observability service, host log collector, or persistent diagnostic script.

For an ordinary migration or production cutover:

1. stop writers or establish a maintenance boundary;
2. take and verify a PostgreSQL backup;
3. record Alembic head and non-empty fact/read-model counts;
4. apply migrations with bounded lock and statement timeouts;
5. verify the same fact identities and expected counts;
6. start one writer per current model, then verify readiness, broker queue
   movement, and unchanged-payload zero-write behavior;
7. retain the backup until the new runtime passes smoke checks.


### Local News evidence and review

News semantic input comes from persisted Items, Event members and bounded
local read targets. A citation link is an identifier, not an online
verification result. Empty, unavailable or conflicting material remains
explicit in the EventUpdate; it does not authorize a fabricated
equivalence or a new Trading catalyst. `news why` and the Event detail
show the source evidence and current processing state.

The current ReviewDesk uses `tracefold news review
queue|evidence|submit|external-miss`. Review the versioned task and its
source evidence before submitting an append-only judgment under the
actual reviewer identity. The removed taxonomy drafter, Gold/GEPA,
freeze/readiness/run/evaluate/canary and `accept-drafts` workflows
have no executable CLI path. Historical reviews and learning rows
remain audit evidence. `tracefold news learning judge-calibration`
is a separate fixed-corpus card judge diagnostic; it does not approve
a News model or release.

For a #706 deployment, remove retired `news.policy` and
`llm.news_compiler_reflection` from the operator config, stop Serve
and Workers, verify a backup, apply migration `20260926_0404`, then
start the matching image. Inspect the migrated legacy intent counts
and current semantic/notification progress; never seed old first-card
queue rows into the new intent path. See [Migrations](MIGRATIONS.md)
for the forward-only schema and [News EventUpdate](design/news-event-updates.md)
for the runtime contract.
