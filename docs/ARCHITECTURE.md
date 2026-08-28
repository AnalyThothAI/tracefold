# Architecture

Tracefold is one Python codebase/image with separate Serve, Workers, automatic
Nautilus, and manual-execution composition roots, one CLI, one React console,
and one PostgreSQL database.
It has two business capabilities: News V3, and — since #104 — the Trading
core, a bounded context that turns persisted News and open-interest verdicts
into at most one small, deterministic, recoverable order. They are siblings,
not layers: neither imports the other and neither reads the other's tables. The
architecture remains Kappa/CQRS: append-oriented material facts are the only
business truth; deterministic current views and bounded immutable model
publications are derived state.

## Data flow

```text
OpenNews Strategy WSS
  -> tracefold workers (RabbitMQ is the News transport plane)
  -> PostgreSQL material facts
  -> single-writer read models
  -> tracefold serve
  -> HTTP / React
```

Beside that hot path runs one strictly bounded review plane, the Price Review
plane (#88, #304): two polling loops in Workers read their own work from PostgreSQL,
call public venue REST with no database connection held, and write two derived
read models — latest-only current quotes and versioned Event Reactions. It is
not a market lane: no tick history, no socket, no OI, no order book, and no
price reaches the Gate, Triage or `decide()`. Delivery may make bounded,
ephemeral public-history reads solely to render the already-approved card; a
price failure never changes whether the card is sent. The plane's failure is
local by construction; News ingestion, judgment and readiness do not depend on
it.

Beside both runs the Trading core (#104), disabled by default. One cold
CandidateRunner in Workers reads persisted Triage/OI facts through two
**public News projections**, freezes one content-addressed Case, decides it —
arithmetic for an OI case, one Predictor call for a News one — and, only
for the exact frozen Demo contract, atomically inserts one TradeIntent while
advancing the Case. It shares Event Reaction's one-slot heavy database
admission rather than the four News lane slots; Quote plan/store uses an
existing ordinary business permit so a Reaction backlog cannot age display
quotes.

One separate Nautilus process is the sole automatic execution authority. It polls
`trading_intents`, fences each economic entry before a provider write, owns
native protection/full close and venue reconciliation, and writes the Outcome
onto the same row. PostgreSQL is the handoff, restart checkpoint, current
projection, and audit truth; RabbitMQ remains News-only.

The disabled-by-default Telegram manual lane is parallel, not a bypass into
that automatic lifecycle. Workers owns the Telegram callback cursor, operator
allowlist, interaction session, preview/guard, and immutable confirmed manual
Intent, but receives no venue credential. A separate `manual-executor` process
mounts only the manual Binance USD-M Demo credential, publishes a fresh account
equity snapshot, and reconciles each entry/TP/SL leg by deterministic client ID.
Both execution roots also hash Binance's account alias from the signed account
projection; the database rejects either exact credential reuse or two different
keys that resolve to the same provider account between the manual and automatic
lanes. The alias itself is never persisted.

`tracefold serve` initializes only public HTTP/static, read repositories, and
serve telemetry. `tracefold workers` initializes the bounded external
capability, singleton runtime status, and the RabbitMQ-driven News consumers
when News is enabled. News consumers recover by re-consuming durable broker
queues plus database idempotency keys. There is no database wake plane, no
projection/EDF coordinator, no CPU-process lane, and no in-memory correctness
dependency. Provider raw frames remain inputs until normalized and persisted
as material facts.

The deployment composition has four required boundaries: PostgreSQL, one
successful migration job, Serve, and Workers, plus exactly one Nautilus process
when automatic Trading is enabled and exactly one manual executor when manual
Trading is enabled. `make up` is only their
fail-closed lifecycle orchestrator; it does not merge the two runtime roots.
On an empty PostgreSQL volume, the image's `initdb` hook creates the
non-login owner plus least-privilege Serve, Workers, Nautilus, and migrate roles from
separate password files, then revokes the bootstrap login before the migration
job runs. That hook is never replayed against a non-empty cluster. Repeated
startup therefore preserves the database and operator-owned credentials, while
an unknown existing schema or missing role fails instead of being implicitly
hard-cut.

The same project-scoped application image contains the Python service and a
production React build. Migration, Serve, and Workers use that exact image and
build revision with different commands and credentials; an enabled
Nautilus process uses it too.
`make up` builds the image once and recreates migration, Serve, Workers, and
Nautilus when Trading is enabled; missing Demo credentials or a replica count
other than one fails closed. It
starts PostgreSQL when absent but does not recreate a running PostgreSQL
container. Serve owns the static console and public HTTP
boundary; Workers exposes only its loopback operational boundary. Image
construction and Compose startup do not become alternate configuration
sources: `tracefold init` remains the single generated-default authority and
`~/.tracefold/config.yaml` remains the single live application config.

## External Data runtime contract

External Data is not a third business capability or a shared runtime. It is a
classification applied before choosing a transport: business semantics decide
whether work belongs on RabbitMQ, in a latest-state collector, behind a bounded
PostgreSQL planner, or in the Trading capital ledger. The four canonical
classes are:

- `durable_event`: every admitted item matters; persist facts, hand off at
  least once internally, make consumers idempotent, and retain explicit
  retry/DLQ/outbox/recovery semantics.
- `latest_state`: only the newest answer matters; coalesce work, skip missed
  refreshes, never queue stale refresh jobs, and preserve the last good value
  when a provider fails.
- `derived_work`: the result remains useful and can be rebuilt from durable
  facts plus provider history; plan bounded batches from PostgreSQL and catch
  up idempotently.
- `capital_truth`: an external write or venue state can be irreversible;
  prepare durable intent, attempt once, reconcile against provider truth, and
  fail closed when the result is uncertain.

One runtime may host all four classes without making their lifecycle rules the
same. In particular, a shared retry policy would erase information: a missed
quote refresh has no durable value, while an ambiguous order must not simply be
resent.

The class states the required contract, not proof that every failure path has
already achieved it. Issue #187 owns the known RabbitMQ settlement, outbox and
recovery gaps; this inventory neither repairs nor hides them.

<!-- BEGIN EXTERNAL DATA INVENTORY -->

### Canonical inventory

This table is review evidence, not a runtime registry. Production never reads
it, and adding a flow here cannot enable a provider, task, queue or business
action. `Worker task` names the stable task interface when the flow has one;
`-` means the flow runs inside the task named by its parent row.

Business runner classes carry only a typed `work_semantics` review annotation.
The architecture harness discovers stages independently from the typed
`NewsPipeline` and `TradingPipeline` production composition: every stage must
declare its semantics or an explicit internal-maintenance exemption, and each
non-durable external stage must emit the common telemetry. Durable broker stages
retain the existing broker/worker measurements. No scheduler, provider selector
or business branch reads these annotations, so the inventory remains review
evidence rather than executable configuration.

| Flow | Owner | Semantic class | Source / authority | Transport | Worker task / trigger | Storage owner and consumers |
| --- | --- | --- | --- | --- | --- | --- |
| OpenNews live frames | News | `durable_event` | enabled OpenNews Strategies | WebSocket -> RabbitMQ | `news-receiver`; provider frames | `news_items` / `news_events`; Deduper, Triage, feed |
| OpenNews history recovery | News | `durable_event` | official Strategy hits history | REST -> `raw.recovery.*` | `news-recovery`; startup or closed incident | same Admission path and News facts; never direct delivery |
| RabbitMQ raw handoff | News | `durable_event` | OpenNews live/recovery envelopes | RabbitMQ quorum queue | `news-deduper`; message delivery | `news_items` / `news_events`; Triage projection |
| RabbitMQ event handoff | News | `durable_event` | admitted `news_events` | RabbitMQ quorum queue | `news-triage`; message delivery | versioned `news_verdicts`; Delivery decision |
| RabbitMQ verdict handoff | News | `durable_event` | push/escalate verdicts | RabbitMQ quorum queue | `news-deliverer`; message delivery | `news_deliveries`; reader receipt truth |
| Binance spot quote/day quote | News Market Review | `latest_state` | Binance public spot REST | REST polling | `news-quotes`; 20 s price / 300 s day reference | `news_quote_snapshots`; feed/event review readers |
| Binance perpetual quote/day quote | News Market Review | `latest_state` | Binance public USD-M REST | REST polling | `news-quotes`; 20 s price / 300 s day reference | `news_quote_snapshots`; feed/event review readers |
| Hyperliquid quote | News Market Review | `latest_state` | Hyperliquid public REST | REST polling | `news-quotes`; 20 s | `news_quote_snapshots`; feed/event review readers |
| OKX quote | News Market Review | `latest_state` | OKX public REST | REST polling | `news-quotes`; 20 s | `news_quote_snapshots`; feed/event review readers |
| Delivery price anchors | News Delivery | `derived_work` | Binance aggregate trades, then Hyperliquid/OKX/Lighter/Bitget recent trades; closed 1 m candles as fallback | bounded public REST on one approved delivery | `news-deliverer`; verdict message | ephemeral `ReaderDeliveryPresentation` only; no persisted tick history |
| Single-name tradeability verification | News Delivery | `derived_work` | fresh Binance, Hyperliquid, OKX, Lighter and Bitget public catalogues | one bounded post-send fan-out | `news-deliverer`; eligible sent message | result stored in desired card or receipt-bound deletion evidence |
| Binance candles | News Market Review / Trading adapter | `derived_work` | Binance public closed 5 m bars | REST on planned demand | `news-reactions` or `trading-candidate`; due work | versioned `news_event_reactions` or frozen Trading case evidence |
| Hyperliquid candles | News Market Review / Trading adapter | `derived_work` | Hyperliquid public closed 5 m bars | REST on planned demand | `news-reactions` or `trading-candidate`; due work | versioned `news_event_reactions` or frozen Trading case evidence |
| OKX candles | News Market Review | `derived_work` | OKX public closed bars | REST on planned demand | `news-reactions` or delivery fallback | versioned `news_event_reactions` or ephemeral delivery presentation |
| Binance instruments | News Market Review | `latest_state` | Binance spot and USD-M catalogues | REST polling | `news-instruments`; 6 h, 15 m retry if none answer | `news_market_instruments`; Gate, quote/reaction planning, Trading projection |
| Hyperliquid instruments | News Market Review | `latest_state` | main perp, spot and bounded HIP-3 catalogues | REST polling | `news-instruments`; 6 h, 15 m retry if none answer | `news_market_instruments`; Gate, quote/reaction planning, Trading projection |
| OKX instruments | News Market Review | `latest_state` | OKX live USDT swaps and USDT/USDC spot catalogues | REST polling | `news-instruments`; 6 h, 15 m retry if none answer | `news_market_instruments`; Gate and price-source resolution |
| US reference instruments | News Market Review | `latest_state` | Nasdaq Trader symbol directories | REST polling | `news-instruments`; 6 h, 15 m retry if none answer | reference rows in `news_market_instruments`; non-crypto classification only |
| Event Reaction | News Market Review | `derived_work` | persisted Events plus venue candle history | PostgreSQL planner + REST | `news-reactions`; 60 s and bounded immediate catch-up | versioned `news_event_reactions`; review projections |
| Trading candidate planning | Trading | `derived_work` | public News projections, OI facts and venue bars | PostgreSQL planner + REST/model | `trading-candidate`; configured poll, default 2 s | frozen `trading_cases`; Trading policy |
| Trading Intent handoff | Trading | `capital_truth` | accepted frozen Case | one PostgreSQL transaction | `trading-candidate`; after an accepted decision | immutable Intent plus guarded `INTENT_EMITTED` Case; Trading/operator |
| Nautilus Binance Demo execution | Trading | `capital_truth` | one immutable `trading_intents` row plus Binance Demo truth | PostgreSQL poll + Nautilus adapter | `tracefold nautilus run`; 1 s while Trading is enabled | entry fence and execution Outcome on the same Intent row; operator |
| Telegram manual intent | Trading via App seam | `capital_truth` | one delivered News receipt plus an allowlisted callback | Telegram polling + short PostgreSQL transactions | `trading-telegram`; code-owned 1 s poll while manual Trading is enabled | callback cursor, callback-effect checkpoint, current session, append-only event ledger, immutable manual Intent |
| Manual Binance Demo execution | Trading | `capital_truth` | one immutable `trading_manual_intents` row plus Binance Demo truth | PostgreSQL poll + signed REST adapter | `tracefold manual-executor run`; code-owned 1 s poll | per-leg client ID, fence, provider-write attempt marker, receipt/outcome, material Telegram notification |

The runtime limits behind that inventory are code-owned safety policy. `shared`
means one turn-wide cap is divided among the named rows. `adapter-owned` names a
real boundary that has no second timeout imposed by these loops; it is evidence
for a future budget issue, not a claim of infinity. A dash means the concept
does not apply.

| Flow | Cadence / trigger | Freshness SLO | Batching key | Max targets | Max source groups / requests | External concurrency | Turn / provider deadline | Catch up / coalesce / stale-not-blank | Failure semantics |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| OpenNews live frames | provider push; reconnect after 3 s | provider-current | Strategy stream | provider-enabled Strategies; provider-owned | one account / one WSS | one WSS session | receiver idle/provider budgets; broker confirm | history recovery / incident windows / no | disconnect opens a durable incident; a frame is not business truth before Admission persists it |
| OpenNews history recovery | startup or closed incident; 30 s overlap | recover while provider history exists | Strategy + incident window | 100 Strategies from the bounded list read | 100 hits/page, 60 pages/Strategy | serial Strategies/pages | provider-client budget | yes / requested pass coalesces / no | partial window remains explicit and all hits re-enter `raw.recovery.*` |
| RabbitMQ raw handoff | message delivery | durable backlog | message ID | raw prefetch 1 | one queue delivery | broker prefetch 1 | broker connection/confirm budgets | yes / no / no | PostgreSQL Admission is idempotent; retry/DLQ/recovery semantics remain explicit (#187) |
| RabbitMQ event handoff | message delivery | durable backlog | Event ID | configured bounded Triage prefetch | one queue delivery | configured bounded consumer | broker connection/confirm budgets | yes / no / no | versioned verdict persistence is idempotent; settlement gaps remain owned by #187 |
| RabbitMQ verdict handoff | message delivery | durable backlog | Event ID + delivery kind | delivery prefetch 1 | one queue delivery | broker prefetch 1 | broker connection/confirm budgets | yes / no / no | delivery ledger preserves the at-most-once reader contract; outbox/recovery gaps remain owned by #187 |
| Binance spot quote/day quote | start-based 20 s current; 300 s day reference | current <=45 s; reference <=360 s | `binance.spot` source group | shared cap 256 symbols | shared cap 12 current groups; at most 2 due Binance day calls/turn; 100 requested symbols where supported | current 4; due day calls parallel after store | 10 s current turn / 8 s provider | no / yes / yes | completed current answers commit together; failed/pending source keeps its previous row; day failure cannot undo current |
| Binance perpetual quote/day quote | start-based 20 s current; 300 s day reference | current <=45 s; reference <=360 s | `binance.perp` source group | shared cap 256 symbols | shared cap 12 current groups; at most 2 due Binance day calls/turn; 100 requested symbols where supported | current 4; due day calls parallel after store | 10 s current turn / 8 s provider | no / yes / yes | completed current answers commit together; failed/pending source keeps its previous row; day failure cannot undo current |
| Hyperliquid quote | start-based 20 s | current <=45 s; native reference <=360 s | bounded `hl.*` source group | shared cap 256 symbols | shared cap 12 current groups; one group request | current 4 | 10 s current turn / 8 s provider | no / yes / yes | completed answer commits even when another source times out; failed/pending source keeps its previous row |
| OKX quote | 20 s | fresh through 60 s | `okx.spot` / `okx.perp` source group | shared cap 256 symbols | shared cap 12 groups; one whole-market request/group | shared cap 4 calls | 10 s turn / 8 s provider | no / yes / yes | failed or empty answer leaves the previous source row untouched |
| Delivery price anchors | one approved delivery | event/push-time | one venue symbol + news/push-1h/push/push-24h anchors | displayed assets only | at most two Binance contracts, then one Hyperliquid, one OKX, one Lighter, and one Bitget contract; 2 s/contract | displayed assets parallel; anchors parallel where supported | bounded by the per-contract deadline | no / duplicate anchors coalesced / no | use the last trade at/before each anchor only within 60 s, then the last closed 1 m candle within 90 s; venue failure tries the next whole calculation and never changes delivery policy |
| Single-name tradeability verification | one eligible sent card | current catalogue | exact ticker aliases | one single-name ticker | exactly five venue families | venue families parallel | 25 s whole review | no / exact contract dedupe / yes | any hit edits and keeps; only five successful empty answers delete; any failure or unresolved identity keeps |
| Binance candles | planned Event/Trading demand | useful while provider history exists | venue symbol + merged time range | shared Reaction cap 100 due rows; Trading policy bounded | shared Reaction cap 32 requests | shared Reaction cap 4; Trading serial | 8 s for Reaction; Trading adapter-owned | yes / merge identical ranges / no | unanswered Reaction work stays due; Trading cannot create a case without price evidence |
| Hyperliquid candles | planned Event/Trading demand | useful while provider history exists | venue symbol + merged time range | shared Reaction cap 100 due rows; Trading policy bounded | shared Reaction cap 32 requests | shared Reaction cap 4; Trading serial | 8 s for Reaction; Trading adapter-owned | yes / merge identical ranges / no | unanswered Reaction work stays due; Trading cannot create a case without price evidence |
| OKX candles | planned Event/delivery demand | useful while provider history exists | venue symbol + bounded time range | shared Reaction cap 100 due rows; delivery displayed assets only | shared Reaction cap 32; delivery one request/missing anchor | shared Reaction cap 4; delivery displayed assets parallel | 8 s provider; delivery 2 s/contract outer deadline | yes for Reaction / duplicate delivery anchors coalesced / no | same local failure semantics as the other price venues |
| Binance instruments | 6 h; 15 m retry if no venue answers | latest catalogue | venue family | one catalogue snapshot | one family fetch; adapter owns spot/perp subrequests | venue families serial | 20 s provider | no / yes / yes | failed venue is omitted from reconciliation, preventing false mass delisting |
| Hyperliquid instruments | 6 h; 15 m retry if no venue answers | latest catalogue | venue family / DEX | main perp, spot and at most 32 builder DEXes | one bounded family fetch | venue families serial | 20 s provider | no / yes / yes | failed venue is omitted from reconciliation, preventing false mass delisting |
| OKX instruments | 6 h; 15 m retry if no venue answers | latest catalogue | venue family | live USDT swaps and USDT/USDC spot | one bounded family fetch | venue families serial | 8 s provider | no / yes / yes | failed venue is omitted from reconciliation, preventing false mass delisting |
| US reference instruments | 6 h; 15 m retry if no venue answers | latest directory | reference family | one directory snapshot | one family fetch | venue families serial | 20 s provider | no / yes / yes | failed reference source is omitted; it cannot remove crypto venue rows |
| Event Reaction | 60 s; 1 h/4 h horizons; at most 20 chained turns | complete before candle history expires | instrument + merged time range | 100 due rows/turn | 32 merged requests/turn | 4 provider calls | no outer deadline / 8 s provider | yes / yes / no | transient no-answer stays due; terminal gap/expiry is persisted explicitly |
| Trading candidate planning | configured poll, default 2 s | evidence eligibility window | underlying / durable source key | 4 case freezes and 4 advances/turn | provider/model calls serial | one | adapter-owned | yes while eligible / durable source-key rejection / no | missing or uncertain price/model evidence cannot create exposure |
| Trading Intent handoff | accepted decision | immediate within 60 s Intent TTL | Case / global active fence | one nonterminal Intent globally | no provider call in the transaction | one | PostgreSQL deadline | bounded rescan / content identity / no | Intent insert and Case transition commit or roll back together |
| Nautilus Binance Demo execution | 1 s PostgreSQL poll | 60 s Intent TTL | globally single active Intent | one active row | one entry, stop, or close operation at a time | one TradingNode | adapter-owned | restart reconciliation / database uniqueness / no | entry is fenced before send; exposure is protected or closing; terminal flat requires targeted venue-zero proof |
| Telegram manual intent | code-owned 1 s poll | current callback stream | Telegram update ID / callback ID | 20 updates | one Bot API poll plus bounded callback operations | one | 8 s per operation | durable cursor / callback-effect identity / no | an effect checkpoint commits with each mutation; cursor advances only after settlement; target/user/session binding fails closed |
| Manual Binance Demo execution | code-owned 1 s poll | account snapshot <=30 s for preview | account + symbol / economic leg | one intent | one signed query and at most one fenced write per leg | one process under advisory lock | adapter-owned | query by deterministic client ID / active account-symbol uniqueness / no | an attempt marker commits before every provider write, including leverage; unknown outcome remains ambiguous and is never resent |

Workers exposes one bounded Prometheus vocabulary at the existing telemetry
seam. The concrete metric names carry the project prefix:

```text
tracefold_external_data_turn_duration_seconds{name}
tracefold_external_data_turn_total{name,outcome}
tracefold_external_data_target_count{name}
tracefold_external_data_source_count{name}
tracefold_external_data_last_success_age_seconds{name}
tracefold_external_data_provider_call_duration_seconds{name,source}
tracefold_external_data_provider_call_total{name,source,outcome}
tracefold_external_data_provider_bytes_total{name,source}
tracefold_external_data_skipped_or_coalesced_total{name,reason}
```

The last-success age is evaluated when Prometheus scrapes rather than frozen at
the end of a turn, so it keeps growing if one collector stops while Workers is
still alive. `name`, `source`, `outcome` and `reason` accept only code-owned
finite values; symbols, Event IDs, URLs, Strategy IDs and dynamic Hyperliquid
DEX names are never labels. Response bytes are recorded only when an adapter
already exposes an exact byte count.

### Extension and extraction gates

Every new external-data flow must state its semantic class, authoritative
source, current-versus-historical meaning, freshness target, missed-turn rule,
failure behavior, batching key, target/source/request bounds, concurrency and
deadline, storage owner, consumers, and whether it can change a News or Trading
action. Raw data and an event derived from it are classified independently:
current OI can be `latest_state`, while a product-level OI jump signal may be
`derived_work` or `durable_event` depending on its consumer contract.

Direct provider wiring remains the smallest correct seam. A capability registry
requires at least four production providers, four capabilities and the same
selection matrix repeated in three wiring locations. A shared latest-state
runner requires a second production collector to duplicate at least the
non-overlap, missed-cadence and telemetry envelope with the same failure
semantics. Until those facts exist, do not add an `ExternalDataService`,
provider plugin registry, `BaseCollector`, `BaseWorker`, generic task engine or
new top-level package.

NautilusTrader `1.231.0` is a pinned dependency and the execution authority only
inside the #283 Binance Demo process. It is not a new business truth,
scheduler, News transport, or provider registry. The adapter receives frozen
capital policy from `TradeIntent`, verifies the dedicated account once at
startup, and projects venue outcomes back onto the same row. PostgreSQL remains
business truth; News, Triage, Review and Learning stay on their present
runtimes.

<!-- END EXTERNAL DATA INVENTORY -->

## Truth, control state, and derived state

Material facts include:

- news: canonical provider Item facts pushed by the Strategies enabled in the
  operator's OpenNews account and persisted in `news_items` (provenance union,
  provider metadata, raw first line, first ingest mode). Tracefold applies no
  local Strategy allowlist.

The two Price Review read models are derived and rebuildable, with different
lifecycles on purpose (#88). `news_quote_snapshots` is last-value-wins current
display state: one row per provider source holding a bounded normalized quote
map, no history, no tick id, no raw payload. `news_event_reactions` is the
deterministic return between an Event's market anchor (`opened_at_ms`, the
provider publication time) and a fixed horizon, keyed by
`(event_id, symbol, metric_version)`; `reaction_v1` freezes candle interval,
alignment, gap tolerance, source selection, aggregation and the hit definition,
so a later revision publishes a new version beside v1 rather than changing what
a stored row means. The row also records `is_primary` — whether the model called
that asset a primary at measurement time — because the review's event-level
sample is the median over primaries and re-deriving that from verdict JSONB per
request costs 1.2 s at the 720 h bound, past Serve's one-second statement
timeout. Both tables cascade with their Event under existing retention.

The current read model is `news_events` (plus `news_event_members`,
`news_event_bands`, `news_event_assets`). It uses stable product identity, has
exactly one runtime writer, is rebuildable from facts, and writes zero serving
rows when its business payload is unchanged. `news_events` is rebuildable by
replaying `news_items` through the Deduper: `admit_frame` expands every
first-seen Strategy tuple retained on the material Item, while `tracefold news
replay` applies the same classifier and Deduper policy to saved provider hits.
Its durable `event_kind` is the closed source
classification `news|listing|oi|liquidation|unsupported_market`; exact,
artifact and near-duplicate joins are restricted to the same kind so source
contracts cannot collapse into each other. Its nullable durable
`source_contract_reason` records only `source_contract_drift` or
`source_contract_unverified` or `unsupported_market_contract`. A missing value
means the current writer parsed the selected contract (or it needs no strict
parser); pre-cut OI/liquidation rows without durable typed success evidence are
backfilled as unverified rather than inferred successful. An OI signal row is
derived read-model evidence for that conservative migration, not a second
material fact source. OpenNews's raw `coins` annotation remains
source evidence in `news_items.provider_metadata`; the Gate derives the bounded
`grounded_assets` from it. `news_event_assets` is the durable Event-market
identity ledger: it contains those Gate-grounded symbols and the primary symbol
a deterministic judge records when provider evidence is empty. The read API
exposes the evidence and the resolved ledger projection as different fields.

OpenNews connection state in `news_ingest_state`, explicit incident intervals
in `news_opennews_incidents`,
and broker queues are control state. Retry attempts and terminal reasons are
likewise queue policy, not facts. `news_verdicts` (Triage decisions bound to a
policy version) are derived model outputs bound to frozen evidence; they are
not material facts. `news_deliveries` is the one-attempt outbound ledger keyed
by `(event_id, kind)`; there is no retry, lease, or backfill. Reader receipt
truth is `news_deliveries.state = 'sent'`; a model decision, pending attempt,
or missing delivery row never means that the reader saw a card.
`news_event_evidence_snapshots` freezes the exact fact unit and evidence
version read by Triage so a later member cannot rewrite the meaning of an old
verdict.

The learning plane is append-only. `news_reviews` stores rubric judgments and
their separate acceptance receipts; `news_external_miss_snapshots` stores
important facts that never became an Event. Content-addressed datasets,
candidate manifests, evaluation reports, pairwise cases, model recordings,
deployments and rollback receipts live in `news_learning_artifacts` and
`news_learning_cases`. `news_canary_activations`, `news_agent_assignments`, and
`news_agent_runtime_manifests` are the durable production control/audit seam.
Workers registers the runtime manifest and its linked active/deployment receipt
as a synchronous startup barrier before its probe can become ready.
`news_learning_epochs` records immutable evidence epochs. Since #314 the running
deployment opens its own: the same startup barrier that appoints the active
Agent appends a row for its bundle if it has never run here, named
`bundle_<first eight hex of bundle_sha>`, carrying that bundle's
`envelope_sha256`, and tripping every armed or active canary. Because a bundle
covers the two instructions, the computed execution envelope, the four model
slots, the retrieval contract and the policy, a deployment that changes what the
model sees cannot go on accruing evidence into the previous cohort. Rows
`program_v1`–`program_v9`, each opened by a hand-written migration, remain
append-only audit history. Only accepted `news_review_v4` evidence created in
the running bundle's epoch and bound to that exact bundle is eligible for
metric v5, compiler, replay or release gates.
Beside that plane, and deliberately not in it, sits the operator's fast loop
(`tracefold.news.learning.experiment`, #193). It freezes one closed window into
a run directory on disk, compares arms on the frozen cases, and runs the same
`run_gepa` core a trusted compile runs — in process, against endpoints named on
the command line. It reads the database once as `serve` and writes nothing back:
no verdict, no review, no dataset, no candidate, no activation. A run directory
is not a second truth, and the release plane cannot read one. What it produces
is a `tracefold.news.experiment_candidate.v1` marked `promotable: false`, which
exists to tell an operator whether a sealed compile is worth spending.

`news_learning_retention_state` makes the bounded 90/365-day cold purge and
its current backlog/error observable; the database function pins the current
and previous distinct stable release chains. The exact `news_*` base-table set
and four security-barrier review views are executable in
`tests/integration/test_news_v3_pipeline.py`, not repeated as a hand-maintained
table-count contract.

## Package map

```text
tracefold.news
  opennews.py         canonical OpenNews frame adapter (raw_text, provenance)
  bus.py              broker envelope, routing keys, error classes, Publisher/Consumer protocols
  events/
    facts.py / titles.py  atomic FactUnits and content-block title extraction
    identity.py / javascript_text.py  exact comparison identity and pinned JavaScript text semantics
    tokens.py / minhash.py  comparison tokens and MinHash 32x4 band keys
    gate.py / storyline.py  deterministic admission, scheduling metadata, grounded assets, storyline keys
  similarity.py       Program-bound reader-card similarity; relocation waits for an approved identity migration
  market_review/
    instruments.py / pricing.py  instrument and quote/reaction domain contracts
    loops.py           current Quotes on ordinary DB admission; Event Reactions on one-slot heavy admission
    storage.py         instrument, quote, Event Reaction, and bounded review persistence composition
  review/
    desk.py           ReviewDesk queues, evidence views, rubrics, acceptance receipts
    drafter.py        model-proposed rubrics a human accepts or rewrites; writes a file, never the DB
  learning/
    dataset.py        freeze / load / project the immutable corpora; holds no release authority
    objective.py      framework-neutral: which accepted cases GEPA may optimize, hold as controls, or exclude
    optimizer.py      the one offline entry: role identities, budget, Objective Plan, GEPA, terminal state
    evaluate.py       run both arms over a frozen corpus and return evidence; decides no state
    ledger.py / profile.py  the learning plane's own rows, its bundle's epoch, and the release profile
    experiment/       operator run directories: frozen window and arm comparison
  release/
    candidate.py      admit a Prompt candidate: derive its Program identity, re-derive the Objective Plan
    canary.py         deterministic one-arm assignment and durable trip/close control
  recording_replay.py sealed-corpus verification composition for exact Program re-execution
  triage_rules.py     decide() post-rules (DecidePolicy), throttle, fail-closed fallback
  program/            SemanticJudge, artifact/registry, seed instructions, chat transport, artifact_tool
  delivery.py / control.py  cards, control commands
  pipeline/
    admission.py      the atomic Deduper transaction and raw-queue consumer
    receiver.py / recovery.py  live OpenNews ingest and official-history recovery
    triage.py / triage_audit.py  SemanticJudge route, policy persistence, execution audit
    triage_route.py   the route's typed vocabulary: arm selection, inputs, attempts, outcome
    delivery.py       one-attempt reader-card delivery consumer
    maintenance.py    instrument snapshot, retention, broker snapshot, outbox catch-up
    root.py / runtime.py  Workers composition and the NewsDatabasePort/stop mechanics
  storage/
    events.py / decisions.py  material facts/evidence and verdict/delivery ledgers
    feed.py / operations.py   bounded public reads and ingest/retention operations
    trade_projection.py       News-owned point-in-time handoff queried by App for Trading
    learning.py / root.py     learning persistence and the concrete repository composition
  query_specs.py      audited News read statements
  eval/               provider-hits Deduper+Gate replay only

tracefold.trading
  contracts.py        App-facing values/ports plus Case/Manifest vocabulary
  intent.py           immutable TradeIntent and current Outcome contract
  manual.py           manual session, preset, preview, guard and immutable Intent contracts
  manual_ledger.py    append-only manual session/execution event contract
  manual_execution.py deterministic venue-neutral entry/TP/SL execution plan
  manual_executor.py  venue-neutral execution reconciliation and typed outcome
  candidate/          deny-list, eligibility/funnel, News/OI fusion, venue routing
  decision/           measured OI/price regime, one-call DSPy Program (#306 follow-on), pure trade policy
  storage/            lifecycle-owned trading_* persistence behind one concrete repository
  pipeline/           CandidateRunner and its one-runner composition root

tracefold.integrations
  provider and external-system adapters: OpenNews, RabbitMQ, Feishu, Telegram,
  public market data, the pinned Nautilus/Binance Demo strategy adapter, and
  the manual Binance USD-M Demo REST adapter and shared read-only account-identity adapter

tracefold.platform
  config models/loader, PostgreSQL/Alembic (`postgres/client.py`, `audit.py`, `migrations.py`), telemetry, paths,
  bounded resource primitives, docker host translation

tracefold.app
  Serve/Workers database composition, `repository_session.py`, HTTP, CLI,
  the Workers lifecycle root plus capability wiring (`app/workers/`), and the
  thin Nautilus process/database/probe composition root (`app/nautilus/`).
  `manual_trading.py` and `manual_executor_root.py` compose the Telegram and
  credential-owning manual halves without crossing their secret boundary.
  `workers/wiring/database.py` satisfies each capability's own database port;
  `workers/wiring/news_to_trading.py` is the single News -> Trading mapper.
  News CLI commands are owned by their bus/instrument/review/learning/diagnostic
  modules; HTTP routes and exact schemas are owned by feed/event/review/status
  resource modules under `app/http/`.
```

Each business package root is its stable public Python interface:
`tracefold.news` and `tracefold.trading` export only value and port contracts.
Ordinary feature callers import those contracts from the root. Runtime
policies, evaluators, persistence, review workflows, and composition helpers
remain with their concrete owners and are not re-exported.

The application composition root and concrete provider adapters are private
implementation collaborators, not product consumers. Where one of them must
construct a repository, schedule an internal worker, or reuse the exact pinned
parser/composer implementation behind a public protocol, its consumer family
and allowed contract family are bounded by the architecture harness. Those
seams use explicit owner imports so they cannot masquerade as product APIs.
They are not re-exported, compatibility interfaces, or available to feature
callers; all public models and protocols still come from the package root.

The dependency direction is:

```text
app -> integrations + business packages + platform
integrations -> business package interfaces + platform
news -> platform
trading -> platform
platform -> Python / third-party libraries only
```

`tracefold.app` is the only seam that knows both capabilities. It is where a
public News projection row or an exact sent Telegram receipt becomes a Trading
candidate/source, and it is the reason
Trading can consume News truth without a cross-domain import or a reach-through
read.

That seam is typed on both sides. Each business package declares the narrow
port it needs from the process — `NewsDatabasePort`, `QuoteDatabasePort`,
`ReactionDatabasePort`, `TradingDatabasePort`, each just a bounded read and a bounded transaction — and
`app/workers/wiring/database.py` implements them over `WorkerDatabase`, choosing
the lane, the deadline default and the error vocabulary. A business module never
names `worker_session`, `run_news` or `heavy_business`: no import edge was never
the same thing as no dependency. The handoff itself is two independent frozen
row contracts, News's `news_trade_projection_v5` and Trading's own candidate
input rows, translated field by field in `news_to_trading.py`, so a rename on
either side fails at the seam rather than inside a runner. Version 5 publishes
three independent lanes: editorial News, deterministic OI, and typed
liquidation facts. The mapper never turns one into another.

`tracefold.app` decides how capabilities are assembled and run, never what a
business fact means. It reads business projections; it does not write business
tables. Every `news_*` / `trading_*` `INSERT`, `UPDATE` and `DELETE` lives in
the owning package's storage behind a named repository method.

Business packages never import `tracefold.app`, provider integrations, or each
other. Transport adapters do not own business rules. `app/workers/root.py` owns
only process lifecycle and TaskGroup coordination; concrete News, Market Review,
and Trading construction lives under `app/workers/wiring/`. Platform exposes only
bounded resource contracts. Queue state machines and
read-model behavior stay with their business owner. These rules are executable
in `tests/architecture/test_backend_boundaries.py`.

A Provider is an integration adapter, not a product layer, registry, or second
source of truth. Each adapter translates one upstream transport and error model
into a business-package protocol. The adapters are OpenNews (the authenticated
Strategy WSS plus the official Strategy list/hits endpoints), RabbitMQ
(`aio-pika`), Feishu (the custom-bot webhook), Telegram (one operator-bound
channel via the Bot API; fixed origin and configured target), and the isolated
automatic and manual Binance Demo boundaries. No provider owns a durable queue. Expected
provider failures stay inside the owning bounded
loop; an unhandled child exception is deliberately a Workers-root failure and
the container restarts the single process.

SQL ownership follows the same boundary: News owns `news_*`; Trading owns
`trading_*`; platform owns Alembic and `workers_runtime`. News makes no cross-domain read: its single
read-only seam (`macro_module_current` as Analyst evidence) went with the
Analyst lane in #57, and the Macro tables themselves went in #68. The
architecture gate checks SQL table references against the generated current
schema.

## Transaction ownership

Application services and workers own transaction scope. Repository writes use
the supplied connection and never expose commit switches or open hidden
transactions.

Important atomic units are:

- one accepted OpenNews frame: NewsItem upsert with provenance union plus its
  Event assignment (new Event, bands, assets, or membership);
- one Triage verdict insert; one delivery begin or settle;
- one TradeIntent insert plus the guarded Case `RUNNING -> INTENT_EMITTED`
  transition.

Provider, model, filesystem, and network I/O occurs outside database
transactions.

Each Worker database session owns exactly one bounded PostgreSQL transaction.
It installs its statement and transaction limits as transaction-local settings
in one setup round trip, so PostgreSQL is the native deadline authority for all
SQL in that session. Transaction exit restores the connection automatically;
there is no session reset round trip. Awaiting DB, finite-operation, and model
work adds only a bounded completion grace so the native result wins at
its deadline. If an asyncio wrapper callback is delayed, an already-completed
native future is consumed directly. A typed recurring business-DB future that
remains alive beyond the grace is a local loop failure: its permit stays bound
to native completion and the loop retries on its natural cadence. Control-DB,
model, cleanup, and otherwise unclassified overruns remain fatal. This
decision uses the exception's typed physical capability, never an error-string
or operation-name prefix. A business-lane PostgreSQL idle-transaction
disconnect is bounded admission failure. The idempotent runtime-heartbeat
child retries only precise transient database failures; 15 seconds without a
fresh heartbeat degrades readiness without killing the root, while recovery
restores readiness. Pinned-singleton loss, invariant failures, and an unfinished
native control future remain fatal. Only an explicitly classified true
external provider seam may translate a finite-operation overrun into its
existing durable retry, degradation, or terminal policy; doing so never
releases the shared capability permit before the underlying future actually
finishes.

News consumers have no frontier lease; the broker's single-active-consumer and
per-message ack are their fences.

## Workers task set

The Workers root TaskGroup contains exactly: `workers-probe` (loopback
health/readiness/metrics), the News consumer tasks when News is enabled
(`news-receiver`, `news-recovery`, `news-deduper`, `news-triage`,
`news-deliverer`, `news-janitor`), the bounded polling loops
(`news-instruments`, and with venues enabled `news-quotes`,
`news-reactions`), the one Trading loop when Trading is enabled
(`trading-candidate`), and `workers-control` (singleton
lock, heartbeat, runtime row). There is no acquisition clock, projection
coordinator, model arbiter, stream ingester, identity backfill, or universe
sync task. The polling loops read public catalogues and prices on code-owned
cadences. Their database admission is explicit per capability: instrument
snapshots use the four-slot News lane; current Quotes use ordinary business
admission; Janitor, Event Reactions and Trading share the one-slot heavy
admission. None creates another pool or worker.

## Product flows

### News

News V3 is a broker-driven Event pipeline. RabbitMQ is the only transport,
buffer, retry, concurrency, and dead-letter plane; PostgreSQL holds facts,
decisions, and audit; every write is idempotent by key. The Story/Brief/RSS/
pinned-WorldMonitor lane and the title-translation lane are retired.

```text
OpenNews account Strategies (whatever the account has enabled; no local allowlist)
  -> authenticated persistent WSS; server pushes strategy.triggered; no app subscribe frame
  -> Receiver publishes each accepted frame to x:news with publisher confirms
     (routing key raw.opennews.<strategy_id>; recovery frames use raw.recovery.<strategy_id>)
  -> q:news.raw [single-active-consumer] Deduper:
       Item upsert (provenance union) -> content-block title + pinned normalization
       -> exact normalized source-contract classification and durable event_kind
       -> same-kind exact fingerprint / MinHash 32x4 LSH near-duplicate + strong-fact veto
       -> Event new|member (family window) -> Gate (provider-graded grounded_assets,
          macro/energy lexicon, PR-template veto, low-signal switch) -> preliminary
          storyline key; a stronger later member re-gates a suppressed Event
       -> publish event.<family>.<queue_priority> for every admitted candidate,
          listing, OI or liquidation Event; unsupported_market is a named held
          Event and never reaches Triage; the suffix affects broker scheduling only
  -> q:news.triage [prefetch = news.triage.concurrency, handled concurrently] Triage:
       SemanticJudge.judge(TriageContext) -> EventSemantics.v2 with nested
          TradeRelevanceV1
       -> deterministic SemanticNormalizer -> ReaderCard.v2
       -> deterministic VerdictAssembler -> one atomic SemanticJudgment
          (verdict + editorial envelope + trace/runtime identities); normally two
       serial provider calls through Predictor-local Adapters/token caps
       (ReaderCard.v2 optionally has a dedicated primary endpoint), one shared
       fast retry per route (at most three calls), and a full fallback restart
       only after primary failure (at most six calls across both routes) -> final storyline key
       from the verdict (written back) -> decide() policy -> verdict row
       (title_zh="" compatibility sentinel, audience, editorial and scored-
       judgment hashes, exact runtime manifest,
       Program identity, per-Predictor execution/cost trace, preliminary + final status snapshots,
       named rule) -> publish verdict.push (an escalate rides the same routing key at AMQP priority 5)
  -> q:news.deliver [single-active-consumer] Deliverer: provider prepare/preflight -> begin(sending)
       -> one configured-provider delivery attempt
       -> settle sent|terminal; crash between send and ack
       -> ambiguous_after_crash
  -> news.retry (one 30 s TTL lane -> back to x:news): TransientError counted (3 attempts),
     DeferError uncounted; x:news.dlx -> q:news.dead for permanent/exhausted/crashed messages
  -> Janitor: outbox catch-up, band expiry, 30/365-day Item retention,
              bounded learning-evidence retention on the one-slot heavy gate,
     broker depth snapshot
  -> Serve: /api/news/feed, /api/news/events/{event_id}, /api/news/status
```

#### Price Review plane (#88, #304)

Two bounded loops beside the hot path, sharing one instrument-resolution strategy
and no state with it:

```text
recent live Events + watchlist -> exact-symbol-first resolution (alias only as fallback,
  reference tiers never candidates) -> unique Price Instruments deduplicated by
  (venue, venue_symbol, price_kind) -> grouped by provider source
  -> mandatory current REST phase: <=12 source groups, concurrency 4, deadline 10 s;
     preserve done, cancel+await pending, sample received_at_ms per normalized source response
  -> all successful current sources in one short transaction, one latest-only row per source
     in news_quote_snapshots
  -> after commit, <=2 due Binance ticker/24hr calls in parallel update only the
     in-process reference cache; the next natural current turn persists the reference
  -> GET /api/news/quotes (<=100 symbols, resolved server-side, fresh|stale|unavailable|unlisted)

due Event-assets (live Events, pushed and held alike) -> pinned or resolved instrument
  -> merged historical 5m candle ranges (<=32 requests/turn, concurrency 4)
  -> p0 = last closed candle at or before opened_at_ms; p1/p4 the same at +1H/+4H
  -> (pH/p0)-1 in integer basis points -> news_event_reactions (reaction_v1)
  -> Feed/Detail attachment + `news review queue --view market`

one approved delivery -> the same exact-symbol-first contract candidates
  -> Binance first, then Hyperliquid, then OKX
  -> for news time, push-minus-1H and push time: last trade at/before the
     millisecond anchor when no more than 60 s old
  -> otherwise the last closed 1 m candle at/before that anchor within 90 s
  -> accept one venue/contract for the complete calculation; never mix endpoints
     from different venues in one return
  -> ephemeral Telegram presentation only; no tick table and no continuous collector
```

Work is `O(source groups)`, never `O(Events x assets)`: a hundred Events naming
BTC are one Quote target and one provider result. Reaction identity keeps its
Event anchor — that cannot be deduplicated without corrupting the metric — but
its provider reads are coalesced by instrument and merged time range, so one
candle response fills many Events.

Failure is local: a venue that times out, blocks, rate-limits or answers
nonsense is skipped for that turn and leaves its previous row untouched, so a
stale quote stays visibly stale rather than becoming zero or vanishing. A
transient provider failure writes no Reaction row at all, which leaves the work
due; only a stable semantic reason (`instrument_unresolved`, `reference_only`,
`history_expired`, `no_candle_within_gap`) terminalizes one. Price never enters
the Gate, Triage, `decide()`, a throttle key or a ranking signal. Since card v10
(#113) it does reach the reader's card, as display and only as display: one
行情 line rendered from a `fresh` (effective age <= 45 s) Quote Snapshot, never read back by
any decision, and absent rather than approximated when no fresh value exists —
68.7% of a week's cards carried one.

OI telemetry has no provider coin tag, so `grounded_assets` remains empty. Its
deterministic parser records the verified primary in `news_event_assets` in the
same transaction as the Verdict (#267). That row is the shared Event-market
identity consumed by Reaction planning, symbol filters, Feed/detail projection,
reader history and delivery; `news_oi_signals(event_id, oi_signal_v1)` remains
the separate rank ledger. The Quote planner unions recent live OI symbols into
its existing bounded working set.
The price remains display-only: it cannot change OI judgment, policy, rank, or
delivery eligibility, and a stale or unavailable quote silently removes the
行情 line.

The price and the day change are two questions on two cadences (#304 hard-cuts
#109's replacement rule). Binance
answers "what is it worth now" in 45.5 kB (`ticker/price`, whole USD-M market,
weight 2) and both questions in 270 kB (`ticker/24hr`, weight 42) — 92% of the
bigger payload is fields we never display, for symbols nobody asked about. Every
turn therefore asks mandatory current first. The loop uses start-based,
non-overlapping 20 s cadence; a turn taking 8 s sleeps 12 s and one taking 25 s
starts the next turn immediately, without catch-up work. Binance spot/perp are
in the first current wave. At the 10 s deadline every completed result is kept,
pending tasks are cancelled and awaited, and all successful sources commit in
one transaction. Each source is stamped when its own normalized response
finishes rather than when the slowest source or database write finishes.

Only after that transaction succeeds can the two due Binance day reads run in
parallel. They update the bounded process-local `openPrice` cache and never
perform a reference-only write; the next natural current snapshot carries the
new reference. Thus a turn makes at most 12 current plus 2 day calls, and a day
timeout cannot delay, replace or roll back a current price. Quote plan/store
uses ordinary business admission; Event Reaction, Janitor and Trading keep the
one-slot heavy gate over the same existing business pool.

What the wide read caches is the rolling window's `openPrice`, **not** the
percentage. `priceChangePercent` is `lastPrice/openPrice - 1`, and the numerator
is the number the next turn is about to replace — freezing the ratio for 300 s
while refreshing the price every 20 s would put a price and a percentage that
cannot be derived from each other side by side, most visibly in the minutes after
a push. Caching the denominator instead means the percentage is recomputed from
each turn's own price, and the only thing ageing is a 24 h window open, which
moves 0.023% per turn.

Nothing is cached for a day read that failed or was cancelled, so the source
remains due while its already-written current row stays intact. A reference is
valid through 360,000 ms; at 360,001 ms, when missing, or when more than 5,000 ms
in the future, only `change_pct` becomes `null`. The price, `change_basis`, raw
timestamps and reference timestamp stay visible. Hyperliquid never adds a day
request: its current response already carries `prevDayPx`, stamped with that
current response's receipt time.

Current freshness is one read-time calculation shared by Quote HTTP, status and
Delivery. Receipt and applicable provider ages are exposed separately and
clamped only for display; `effective_age_ms` is their maximum. A provider or
receipt timestamp more than 5,000 ms in the future remains visible but forces
`stale`. With no provider timestamp the basis is `received_only`; otherwise it
is `source_and_received`. This is not a timer write and does not add another
stored state.

A symbol that joins the working set triggers a wide read immediately instead of
waiting out the cadence, since the plan is ordered newest Event first and that
symbol is the card the operator is looking at; coverage records what the last
wide read *asked* for, so a symbol no venue lists cannot pin a source to the
expensive endpoint. `binance.spot` asks by name on both endpoints, with the
`symbols=` list dropped only on `ticker/24hr` past 100 symbols where the weight
tiers make the whole market cheaper.

### Why the quote source is REST and not a WebSocket

Recorded so the question is answered by measurement rather than re-litigated
(#109, measured 2026-08-21 from the deployment host):

USD-M REST rows are whole-market payloads — 744 symbols, the endpoint has no
`symbols=` filter — so their cost does not scale with how many we read. The WSS
rows do:

| transport | steady state | per day |
|---|---|---|
| REST `fapi/v1/ticker/price` @ 20 s (whole market) | 45.5 kB/turn | 0.20 GB |
| REST `fapi/v1/ticker/24hr` @ 20 s (whole market) | 270.1 kB/turn | 1.19 GB |
| REST after #304 (mandatory price @ 20 s + post-store 24hr @ 300 s) | — | **0.27 GB** |
| WSS `fstream` `!miniTicker@arr` (whole market) | **0 frames in 22 s** | — |
| WSS spot `<sym>@miniTicker` x 218 | 185 B/frame, ~1 fps | ~3.5 GB |
| WSS Hyperliquid `allMids` | 3.0 kB/s | 0.27 GB |

Three facts, each sufficient on its own. A subscription is cheaper than polling
only when you consume *faster* than the venue pushes; the console polls over
HTTP every 15 s, so a socket would multiply bandwidth 12–20× to move a number
nobody reads faster. The real saving is in payload choice, not transport. And
Binance's futures socket produced no frames at all from this host across all
three documented URL forms while its REST worked throughout — "connected but
silent" would be the *normal* state for 218 of 256 targets, which is the one
failure mode a socket hides and REST cannot (a REST error becomes `stale`
immediately; a silent socket freezes the last price and says nothing).

A WSS Quote Source becomes right only when all four hold, each verified rather
than assumed: (1) the browser is no longer an HTTP-polling reader, which is its
own architecture decision; (2) a product requirement names a freshness SLO
tighter than the collector cadence, and someone can say what a reader does with
it; (3) the venue's socket is verified to deliver from the deployment host for a
sustained window; (4) its steady-state bandwidth measures lower than the
REST plane it would replace at the accepted cadence — 0.27 GB/day for USD-M
after #304, not the 1.19 GB/day figure that motivated the question. If it is ever built it
must meet what the OpenNews receiver already meets — jittered reconnect with
resubscription, forced reconnect before the venue's connection lifetime,
ping/pong liveness, **a per-symbol staleness watchdog that degrades a
silent-but-connected socket to `stale` instead of freezing the last value**,
subscription diffing inside the venue's subscribe rate limit, one connection per
venue with isolated failure, no socket in Serve, and REST as the source of truth
on startup and after any gap.


Ownership: `tracefold.integrations.rabbitmq` is the only module that imports
`aio_pika`; `tracefold.news.bus` owns the envelope, routing keys, error classes,
and Publisher/Consumer protocols. `tracefold.news.pipeline` physically separates
Receiver, Recovery, Admission, Triage, Delivery, and Maintenance; the concrete
stages are wired directly by `tracefold.app.workers.wiring.news` and run as
asyncio tasks in the single Workers process but coordinate only through the
broker and PostgreSQL keys, so they can be scaled out without code changes.
News consumers use their own four-slot database lane
(`WorkerDatabase.run_news`) so background backlog never starves a live Event;
a lane admission timeout is a `DeferError` (uncounted requeue), a statement
overrun is a `TransientError` (counted).

Identity: `news_items.item_id = sha256(source_id, params.id)`. Event identity
v5 keeps the legacy encoding for ordinary News (`item_id` for a whole Item,
`sha256(item_id, fact_id)` for a split FactUnit) and uses
`sha256(item_id, fact_id, event_kind)` for every non-News kind.
If migration reclassified a pre-v5 ordinary-News identity — `item_id` for a
whole Item or `sha256(item_id, fact_id)` for a split FactUnit — as non-News, a
later ordinary-News projection for that same fact uses the deterministic
collision fallback `sha256(item_id, fact_id, "news")`; no existing Event or
dependent ledger is rekeyed.
`tracefold.news.events.titles`
extracts the first content block (skipping URL-only, label-only, `reply/quote:`
lines and pinned wire source labels/suffixes; exchange names and `@handles`
are subjects and stay — `@Krakenfx launches ...` keeps `Krakenfx`),
`tracefold.news.events.identity`
normalizes for comparison, `tracefold.news.events.tokens` + `minhash` produce the
band keys stored in `news_event_bands`, and `tracefold.news.pipeline.admission.admit_item`
is the single Deduper transaction. Fingerprints of at most two tokens never
share an Event. `event_kind` fences every dedupe candidate lookup and namespaces
non-News Event identity. A source-contract reason never splits the same
Item/FactUnit/kind identity: a migrated `source_contract_unverified` Event is
settled in place by the current parser. Cross-Item exact/artifact/near joins are
reason-fenced; the sole exception is an unverified migration candidate, which
is resolved to the current parser result before the new member joins.

That text-derived identity is deliberately weak, and #154 adds the exact one
beside it rather than loosening it. `news_items.source_artifact_id` is the
artifact a frame is *about* — for X, `x:<status_id>`, parsed by
`tracefold.news.opennews.source_artifact_identity` — because the provider
re-emits the same tweet under new record ids and under inconsistent URL
spellings (`twitter.com` vs `x.com`, `coindesk` vs `CoinDesk`; `_article_url`
lowercases the host but not the path). 17 of 29 repeat ingests in a 30-day
window differed only in that spelling, so the URL string is not an identity and
the status id is. After the text path misses, the Deduper looks up the same
artifact **and the same fingerprint** inside a 7-day window: pairing it with the
fingerprint is what keeps a split digest from collapsing into one Event, while
the artifact id is what earns the right to skip the three-token `shareable`
floor and the 12 h family window. A hit joins the existing Event as an ordinary
member, so nothing new is delivered. The same parse yields how old the artifact
already was when the provider pushed it — an X status id is a Snowflake — which
`decide()` reads as `stale_source_artifact` for the case the ledger cannot see:
a stale artifact arriving for the first time. Measured over 3174 frames in 30
days that age is bimodal (2491 within 10 s, 7 beyond 16 h, nothing between) and
never negative. `published_at_ms` is untouched: `opened_at_ms` derives from it
and anchors `reaction_v1`.

Before Gate, one pure OpenNews source classifier matches the normalized
`strategy_id + strategy_name + source_type + engine_type` tuple exactly. It is
the sole owner of the five route families: ordinary News and listing continue
through Gate and the generic Program, OI and liquidation select their existing
strict deterministic parsers, and an unbound scoreless market/wallet contract
or known tuple drift becomes the named held `unsupported_market_contract`.
Parser failure is `source_contract_drift` and never falls back to a model.
Recovery runs the same classifier and, when history carries the complete tuple,
the same side-effect-free parser and persists that reason; it does not write the
live OI rank fact and never delivers. A provider history row missing a tuple
field fails closed as drift rather than inferring a contract from its id.
Ordinary and
deterministic recovery keeps `admission=recovery`, while unsupported contracts
retain the named `unsupported_market_contract` admission. Event identity v5
keeps ordinary News identities stable and namespaces non-News FactUnits by
kind, so different contracts for one provider record cannot merge by arrival
order. There is no source registry, queue, worker, or ID-only routing.
The hard cut keeps historical verdict/delivery rows, moves unsupported Events
to the current named hold, and gives a durable `delivery.state=sent` priority
over that routing admission so a card the reader received remains delivered.
Both consumers re-read current `event_kind` before model or outbound work,
which safely drains pre-cut queued messages without leaving a false pending card.
Migration 0315 deliberately does not manufacture extra historical Events from
secondary Strategy tuples on a merged Item. For an already judged Event it
uses the exact evidence snapshot bound to the latest Triage verdict and keeps
the route named by that verdict's Program; unsupported contracts and known tuple
drift still fail closed first. This migration-only compatibility rule prevents
a pre-cut generic verdict from being replayed as an OI or liquidation verdict.
An unjudged Event uses its latest evidence focus and the exact source classifier.
The tuple and provider score always come from the same snapshot. Only an
incomplete tuple may fall back to an explicit deterministic admission, or to a
recovery liquidation typed fact. This preserves the Item's complete tuple union
without rewriting a verdict already queued for delivery. The missing secondary
Event is a declared historical projection residual, not a loss of fact truth;
rebuilding the Item through current Admission, or a later provider redelivery,
creates it deterministically.

Gate and storyline (`tracefold.news.events.gate`, `tracefold.news.events.storyline`) are pure
functions and keep no Strategy name table of their own: grounded assets are the
provider's grade B+/A/A+ coin tags plus any literal `$TICKER` cashtag (the
provider already resolved Bitcoin -> BTC, Home Depot -> HD); `CL`/`XYZ-CL` is
grounded only in energy context and a short stop-list drops English-word tags.
Existence on a venue is deliberately not a condition: #75 shipped that filter
behind a flag and the dry-run killed it — every tag the provider had itself
mapped to a venue was already listed, and the ones it would have removed were
real equities with no crypto perp (#89). The instrument universe labels a tag
instead: `asset_class` is `equity_or_commodity` when a grounded symbol resolves
to an `equity`/`commodity`/`index`/`fx`/`pre_ipo` instrument, `crypto` when it
resolves to a coin, and falls back to the provider's `XYZ-` prefix when the
universe is empty or does not know the symbol. Equities with no crypto perp
(`UWMC`, `TLX`) are answered by the `us.listed` reference tier (#91), which is
consulted only for symbols no traded venue lists — `ATOM` is the Cosmos token
on three exchanges *and* Atomera on the NYSE, and the venue that lists a symbol
always describes it. The tier is excluded from `asset_refs`, from the console's
`符号落表` funnel segment, and from the `trading` / `by_venue` figures; only
`instrument_classes()` reads it.
The Gate does not decide relevance: every ordinary-news Item is a `candidate`
unless it is a recovery replay, a law-firm template notice (strong template
phrases always; weak ones only without a grounded asset), an under-80 scored
market frame, or — behind `news.gate.suppress_low_signal`, default off — an ungrounded,
non-macro social post under 70. A `listing` frame takes the
`listing_deterministic` admission, which is admitted and judged like a
candidate (#72). A member that
joins a suppressed Event with stronger evidence (score >= 80, an A/A+ grounded
tag, or a different source) re-gates it in place and it publishes once.
`queue_priority` is `high` (AMQP priority 5) for score >= 90, watchlist hits,
listing frames, or rate/yield macro. It is a broker scheduling hint only: it may
be persisted and measured, but cannot enter a Predictor, `decide()`, ReaderCard
or reader-facing importance UI. The preliminary storyline key (status bar only)
is theme-first (`crypto_treasury`, `mideast_energy`, `rates`, `trade`,
`china_macro`, `metals`, `us_equity_macro`, `us_macro_data`), then the first
A/A+ or cashtag asset; the final key is computed after Triage from the
verdict's grounded primaries and scope, written back to `news_events`, and
used by duplicate comparison, operator grouping, and advisory locking.

Triage is a deep semantic-judgment **Module**. Its only hot-path generation
**Interface** is `SemanticJudge.judge(TriageContext) -> SemanticJudgment`; the
consumer does not know Predictor instructions, output schemas, model
routing, retry state, or artifact layout. That **Interface** lives at the
semantic-judgment **Seam**, and `NewsSemanticProgram` is the production
**Adapter** there. Cold strict replay is an evaluator-side sealed-corpus
verification composition seam, not a second production generation Interface:
it scopes one persisted run's physical calls to the requested arm/case/trial,
then re-executes the real arm-scoped `NewsSemanticProgram` graph. The graph
still enters through `judge(TriageContext)`. A missing corpus or recording makes
the verification evaluation `incomplete`/`UNKNOWN` without falling through to a
live provider; an identity or tamper mismatch fails closed. This shape gives
the hot-path caller **Leverage** (one call owns graph execution, validation,
fallback and audit) while keeping replay authority outside production
generation. Its **Depth** is the amount of behavior hidden behind the single
hot-path `judge()` method, not the number of internal Predictor calls.

Inside the Module, the fixed Program graph is
`EventSemantics.v2 -> deterministic SemanticNormalizer -> ReaderCard.v2 ->
deterministic VerdictAssembler`.
`EventSemantics.v2` judges novelty, grounded entities, direction, scope,
magnitude and audience without writing reader copy, and emits one nested typed
`TradeRelevanceV1`: impact breadth, tradability, surprise, development delta,
at most four canonical channels/affected markets, and the sole model delivery
intent `reader_value` (`escalate|realtime|background|none`). It has no separate
model-authored `decision` or `actionable` field.
For `new_fact` and `progression`, the normalizer discards any stray non-negative
`restates` index and records the raw and normalized values on the originating
call trace; a real `restatement` still requires a valid told-ledger index. It
also preserves raw relevance arrays in trace, then de-duplicates and sorts them
by code-owned enum order. The normalizer makes no provider call.
`ReaderCard.v2` receives the original evidence plus an explicit
`ReaderCardSemanticView` containing only event type, assets, direction,
magnitude, novelty/restates, scope, channels and affected markets. It produces
only `headline_zh` and `why_zh`; it cannot read reader intent, tradability,
surprise, development delta or ToldContext. The assembler makes no model call:
it explicitly projects the compatibility `TriageVerdict`, derives `actionable`
from normalized trade surfaces, maps `reader_value` to its legacy decision
sentinel, and keeps public `title_zh` empty. Splitting semantic judgment from copy creates internal
per-Predictor feedback, demonstration, routing and future fine-tuning seams;
it does not add a second product stage or a second card.

The only executable generation is `news_semantic_program_v5`. Issue #193
hard-cuts the artifact to one canonical JSON document; issue #306 keeps that
shape and changes what the two instructions *are*, each becoming the complete
prompt for its Predictor rather than a bounded advisory appended to a rendered
stack, with the code-owned seed text in `tracefold/news/program/seed.py`. Issue
#314 removes the last field that was not a written instruction: the artifact
holds `schema_version` `news_program_strategy_artifact_v1` and one instruction
per Predictor, and `program_sha256` is the canonical hash of exactly those
three values. The stable root is
`c71bd9041f26d8ee75f055dc0997a92a2b44c1fbdb0d00d1a2e9ecb18ee675a4`.

**Program identity has two halves, and they have two authors.**
`program_sha256` addresses the write-set a human or GEPA may edit.
`envelope_sha256` — `compute_execution_identity()` in
`tracefold/news/program/identity.py` — addresses everything the code decides
about a model call: the golden render of each Predictor's complete chat request
in all three structured-output modes, the single output contract and its JSON
schema, the model-visible input shapes and their delimiters, the endpoint
capability table, the model binding slots, the route deadline, the token
ceilings and the breaker. It is computed from those values rather than declared
beside them, so a change to any of them moves the identity whether or not anyone
remembers to say so. One contract test
(`tests/contract/test_program_release_identity.py`) pins it, and re-pinning that
line is the signature on an identity migration.

This replaced a declared `factory_id` literal, which had the failure mode every
hand-maintained version has: not that somebody picks the wrong string, but that
a change lands and nobody bumps anything. Three identity-clearing incidents in
four days were that, and the pin net that grew to catch them — nine epoch
counts, four byte-equality tests, a mirrored constant module and five documents
each restating the current identity — was guarding the declaration rather than
the behavior.

`program_sha256` is behavior identity and nothing else. It no longer contains
parent lineage, optimization cost, trajectory or teacher endpoint, so two runs
that reach the same two instructions are the same running Program however much
they cost and whoever launched them. Lineage is a property of the candidate
(`ProposalReceipt.program_parent_sha256` and `program_candidate_sha256`), and
since #202 it is *derived* at registration by re-applying the patch rather than
declared. Folding any of it into the runtime root let "who produced this" change
what "this Program" meant.

Everything else the Program needs — the two-Predictor graph, the typed schemas,
the normalizer, the assembler, the model route and the execution budget — is
code, and `envelope_sha256` is computed over what that code renders. It is one
hash over one golden render rather than twenty-odd component hashes that the
same package generated and verified in the same process; that was a self-proof,
not an attestation, and neither it nor this replaces exact image/CI evidence.

Every model is sent the same two messages, whatever structured-output format its
endpoint accepts (#315). Only `response_format` follows the endpoint: the real
constraint where it is accepted, `{"type": "json_object"}` where it is refused.
The output contract always carries the schema inline, because a structured-output
constraint expresses shape and the schema's field descriptions express meaning —
`restates` is a visible `event_status.told` index only when novelty is
restatement, and no format carries that. #306 Phase 3 briefly moved those
descriptions out of the model's view into `response_format` alone; llama.cpp
compiles that into a GBNF grammar, so the primary route was held to a shape while
the rules it needed sat in a document it never read.

There is one prompt text per Predictor and no renderer (#306 Phase 2). Until
then the prompt was a layering — a sealed QualityKernel, nine ordered code-owned
RulePacks, one bounded advisory slot the optimizer could write, and a final
authority seal telling the model to resolve conflicts in that order — assembled
on every call, guarded by 55 reviewed coverage anchors and by authority patterns
that refused any advisory claiming to outrank the packs. What that bought was
the ability to say "the learned part cannot override the reviewed part" *inside
the prompt*. What it cost was that the learned part could only ever be an
addendum, blind to the text it was appended to and structurally unable to repair
a sentence in it — and the measured result was a shipped stable artifact whose
two advisories were both the empty string, i.e. a learning plane that had never
contributed a byte to a reader-visible prompt.

So the governance moved to where it already lived: a human edits `seed.py` and
GEPA proposes a replacement for the same string, both produce a new
`program_sha256`, and both travel the same candidate -> canary -> reviewed diff
-> promote pipeline. `RulePackSpec`, `CoverageAnchor`,
`validate_expert_baseline_coverage`, the advisory authority patterns, the
optimizer-owned Predictor's mutable-surface check, the proposer's read-only
brief and the four-part renderer are all retired. What survived, because none of
it was ever about authority, is `validate_program_instruction`: NFC canonicality,
the byte and estimated-token budget, credential shapes, and the injection
markers (template braces, a script tag, a URL, a credential header, a
prompt-injection opener). It applies identically to a human's edit and to an
optimizer's proposal, which is the point — there is one author role now.

An instruction carries no identity hash: a digest cannot help a model judge
news, it was billed on every call, and carrying one meant a pure identity change
rewrote the prompt. There is no demo section either. The DemoBank family is
deleted rather than left empty, and the transport composes one system message
and one user message from the instruction and the bounded fields, so there is no
path by which a demo could reach a provider at all.

The factory owns route topology, slot roles, token ceilings, deadlines and
breaker policy. The concrete model bound to each slot has a separate
secret-free `configured_endpoint_model_v3` identity over provider, model,
endpoint fingerprint, temperature behavior, structured-output mode and normalized LM kwargs.
That boundary makes provider execution semantics auditable without pretending
an endpoint change rewrote the Program graph. Every endpoint can explicitly omit
temperature, choose JSON Schema, JSON-object, or prompt-only JSON, and add guarded
OpenAI-compatible body fields. There is no Kimi URL/model special case. Known
provider defaults remain narrow (including MiniMax M3's valid sampling and JSON
prompt envelope), while local and other models are configured through the same
request block. The learning experiment student arm inherits the production
endpoint request contract when it rebinds a model name.

The production registry resolves an image-carried SHA, never arbitrary database
instructions, and the document is one `<program_sha256>.json` file. Loading
fails closed on an unknown version, hash or factory, non-canonical or
duplicate-keyed JSON, a non-finite number, an unsafe or secret-bearing key, a
symlink or traversal path, or a file whose name is not its own root. Pickle,
cloudpickle, dynamic code/classes, endpoints and credentials are not
supported formats. This is the executable-state Seam: Program evolution can
change reviewed state without allowing a data row to become Python control
flow. There is no LangChain Prompt executor, dual-run mode, legacy Adapter, or
compatibility fallback; Prompt-era columns/rows are read-only audit history.

The Module never retrieves from a network; it ranks bounded local reader
history. `repository.reader_history` reads only first deliveries durably settled
`sent` for a Triage `push`/`escalate`, excluding the current Event. It returns
two disjoint projections from that one material truth:

- `recent_seen_rows`: every receipt aged at most 4 h, newest first, cap 128;
- `targeted_told_rows`: receipts older than 4 h and at most 48 h, with up to 8
  exact `(family, comparison_fingerprint)` matches followed by up to 24
  canonical-asset overlaps. Exact matches win when one Event qualifies twice.

Only the recent projection reaches deterministic `decide().seen`; the targeted
projection is semantic evidence for the Program and cannot extend a policy
throttle. Telemetry requests `include_targeted=False`. Production initial load
and stale refresh, plus CandidateEvaluator seed and in-run receipt replay, use
the same pure `build_reader_history` boundary/cap/dedup rules.

`ToldLedgerSnapshot.select` is a pure, deterministic, candidate-conditioned
selector — not a Retriever service, Protocol or Adapter — that ranks the union
against *this* Event and shows the Program at most 16 rows. Its tiers are
targeted exact fact, exact storyline, shared instrument (canonical symbol
sets), positive same-fact similarity over the Deduper's normalized
`comparison_title`, then recency. Inside a tier the order is similarity desc,
sent time newest-first, then the stable Event identity, so the same history
always produces the same selection whatever order the database returned it in.
The storyline tier is capped at 8 of the 16 rows and its overflow yields to the
tiers below before filling what is left, because ranking storyline first with no
cap starves everything under it: a dense storyline puts 14-17 same-key cards in
one window, and measured on the accepted corpus that scored *below* the
predecessor.

Two numbers were measured, not chosen. Against every accepted `restatement`
whose duplicate target was inside the 4 h ledger (n=22), target recall@N was:
predecessor 19/22; strict tier order at 12 rows 18/22; capped tiers at 12 rows
19/22; capped tiers at 16 rows **21/22**. The binding constraint on the
predecessor was never the ranking — no ordering recovers what the cap excludes —
so the row budget moved with it, paid for by `ReaderCard` no longer receiving
the ledger at all (the two-call total moves about +2%). The single remaining
miss is a cross-lingual paraphrase naming a different instrument, which no
deterministic primitive reaches. Issue #175's fixed overnight cases then moved
source and selected recall from 0/2 at 4 h to 2/2 at 48 h; an exploratory 7-day
window recovered no additional fixed target and substantially enlarged the
candidate pool. Each model-visible
entry carries index `i`, age, final storyline key, event type, instrument
symbols, magnitude, direction and `headline_zh`; the Event id, sent time,
selection tier, similarity, history scope and retrieval reason stay audit-only.
`READER_HISTORY_SHA256` binds source truth/windows/caps/projection;
`TOLD_SELECTOR_SHA256` binds selection and the unchanged model-visible schema;
their composite `NEWS_RETRIEVAL_SHA256` is the arm's `retrieval_sha256`, so
either source or selector behavior changes the Program and bundle identities.

The two Predictors do not read the same input. `EventSemantics.v2` receives the
model-safe Event evidence, grounded Gate facts and selected told context;
`ReaderCard` receives the evidence plus only `ReaderCardSemanticView`.
`queue_priority`, provider score, Gate macro lexicon, queue lag and the
watchlist are excluded from both model-visible schemas; the watchlist remains a
code-owned objective policy guard. The boundary is the schema rather than a
prompt reminder: the card input forbids ToldContext and extras, so a card
payload or recorded demo carrying history or delivery intent is rejected at the
renderer. Novelty is `EventSemantics`' job; a copy step that can re-read old
cards can re-interpret them. Both Predictor instructions are English. `ReaderCard.v2` has exactly two Chinese text outputs:
`headline_zh` (the card header — a complete headline that keeps the decisive
fact, not a stub) and `why_zh` (the one card sentence adding what the headline
does not say). It has no `title_zh` output. The public `TriageVerdict` retains
`title_zh=""` only as a compatibility sentinel, while `audience` (crypto /
us_equity / macro / none) is an EventSemantics field. The verdict also carries
`novelty`
(`new_fact` / `progression` / `restatement`, judged against the told ledger)
and `restates` (the ledger index a restatement points at; -1 otherwise) —
the reader-facing memory Triage has (issue #61): dedup is byte/word-level,
novelty is the semantic last line against the same fact told again from
another outlet or under another storyline key. Magnitude remains a Program
output; `actionable` is now a deterministic assembler projection from
tradability, channels and affected markets. Policy v10 owns the final action
from one atomic `ScoredJudgment`. A *grounded* restatement is handled first, and
the existing stale-source and content-similarity protections remain after
action selection. The action section is exactly:

1. deterministic listing/telemetry;
2. grounded-watchlist objective guard;
3. `reader_value=escalate` and `realtime_eligible` -> `escalate`;
4. `reader_value=realtime` and `realtime_eligible` -> `push`;
5. `background|none` -> `drop`;
6. every other combination -> `trade_relevance_inconsistent`.

`realtime_eligible` requires magnitude >= 2; tradability `direct` or
`second_order`; non-empty channels and affected markets; and either a
`state_change`, or `material_detail` that is direct/unscheduled/material versus
expectation. Queue priority, provider score, macro lexicon and `scope=macro`
cannot select or rescue an action. A Gate-admitted `listing_deterministic` frame
(`listing_exempt_from_duplicate`) skips the restatement drop and similarity
throttle only when the matched card names none of its instruments, compared as
symbol sets rather than headline text; a re-issued notice for the same
instrument is still withheld. `news.policy` exposes only four v10 knobs:
`restatement_drop`, `similarity_max`, `stale_source_max_age_s`, and
`listing_exempt_from_duplicate`.

Policy v7 deliberately
has **no hourly, two-hour, or four-hour reader quota**. Historical push counts
remain observable metrics, but they are not included in the model input and
cannot change `push`/`escalate` into `throttled`. Once the semantic conditions
pass, the delivery harness executes the decision; it only enforces explicit
idempotency, provider pacing, and real delivery receipts.

Duplicate protection is content evidence rather than a quota: each ordinary
`push` headline is compared with cards the reader actually received in the
last four hours (character-bigram Jaccard, `tracefold.news.similarity`). At or
above `similarity_max` (0.25) the card is withheld with
`storyline:<key>:seen`; otherwise it is sent regardless of prior volume.
`similarity_max = 0` disables this check and never restores a count cap.
`escalate` and degraded wire-headline fallbacks skip similarity because a
false positive is least affordable there; a directional reversal also passes
because bigrams are blind to negation. `trace.seen_scope=all` records that the
ordinary push path was measured. This preserves the useful part of policies
v5/v6 (catching same-fact repeats such as a cross-key provider batch) while
removing their second, count-based editor. Every path names its rule; nothing
drops silently.

The Program factory owns the execution contract. A successful primary route
normally makes two serial provider calls: EventSemantics, then ReaderCard.v2.
The in-process normalizer and assembler make no provider request, and the
non-restatement normalization spends no fast retry. One
fast retry is shared by the entire route, so a retry consumed by the first
Predictor is unavailable to the second and a route makes at most three calls.
A retryable transport failure or a non-truncated unusable answer can spend that
retry; `max_tokens` truncation cannot. The code-owned 20-second deadline
applies to the whole route, not to each call. If primary still fails, fallback
restarts the full graph with its own shared retry and route deadline; the
complete chain therefore makes at most six visible provider attempts. Client-side
cache and hidden provider retries are disabled so the trace count equals real
attempts. A verdict complete except for `novelty` can still be accepted as
`new_fact` (`novelty_defaulted`) after the retry. A Program failure is
degraded, not silent: deterministic listing/telemetry and a grounded watchlist
hit fail open on the wire headline; every other failure drops as
`degraded_no_objective_guard`, even when the provider score or queue priority is
high or the text contains macro words. Three
consecutive retryable whole-chain failures open the default 60-second consumer
circuit that also opens a
`triage_circuit_open` incident (closed by the next success); an output failure
(`news_program_output_truncated` when a Predictor hit `max_tokens`, or a
typed Program output error on schema mismatch) is degraded but never
counts toward the circuit and records the failing Predictor, finish reason,
tokens and error code. After the Program returns the consumer decides and
persists in one transaction under a per-storyline advisory lock on the final
key (`repository.lock_storyline`; `pg_advisory_xact_lock('NEWS', hashtext(key))`),
re-reading the reader evidence inside the lock so two same-key Events in flight
cannot both send the same fact (the lock raises the lane's 250 ms
`lock_timeout` for that transaction only). The wide sent ledger is always
re-read inside that lock, because `decide()` must measure this card against
every card the reader received including one that landed while the model was
thinking. Only the *selected* told context decides whether the judgment itself
is stale: the consumer rebuilds it from the refreshed ledger with the same
selector and compares `novelty_context_sha256` — the hash of the shown rows that
are evidence *about this candidate* (storyline, instrument, same-fact), which
deliberately excludes the recency filler. Filler is there so a sparse candidate
still sees what the reader has been reading; a card at the top of it cannot turn
this Event into a restatement of anything, and hashing it would put the whole
selection back under "any delivery invalidates the judgment", which is the rule
this replaced. A card that joins on storyline, instrument or same fact does
change the question, so the consumer reloads sent content evidence under a fresh
stamp and calls the full Program once more. `selected_context_sha256` records
the whole selection for replay identity. The old rule compared the raw recent
event-id set, so any delivery anywhere in the window forced a re-ask; in the
fixed production cohort that fired on 16% of judgments, of which only 3 of 11
were actually ledger-driven — the other 8 were Event evidence changing, which
still re-asks. The trace distinguishes a stale sent ledger
(`reask_reason=told`, `reasked_after_told_change`) from changed Event evidence
(`reask_reason=evidence`, `reasked_after_evidence_change`). If a ledger-only
re-ask fails, the complete first `SemanticJudgment` (verdict and editorial
envelope together) is still bound to the same evidence and is persisted with
`reask_failed`. If the evidence changed, the first judgment
cannot truthfully be rebound to the refreshed snapshot: a failed re-ask uses
the deterministic degraded fallback over the refreshed Gate facts, with no
selected Program execution; a second evidence change before persistence raises
`news_event_evidence_changed` for durable retry.
The re-ask is a separate Program execution: the ordinary rare case is four
provider calls total, while each execution independently retains the six-call,
two-route ceiling. All work from both executions remains in audit and cost
telemetry even when the first result is superseded or the second fails.
`news_verdicts` atomically stores the verdict JSON, editorial envelope,
`scored_judgment_sha256`, exact `runtime_manifest_sha`, `model_decision`,
`rule_baseline_decision`, `final_decision`, `override_rule`, `throttled_by`,
`degraded`, and a replayable trace (Program version/SHA, runtime provider/model identity,
per-Predictor request/input/instruction/demo/output hashes, finish reason,
latency, tokens and provider-reported cost (or explicit unknown), the
preliminary storyline key, the preliminary and final status-bar snapshots,
the told context as shown with event ids, selection tier and similarity,
`told_count`, `selected_context_sha256`, `restates_event_id`,
every initial/re-ask Program execution and which one was selected (when the
persisted verdict came from the Program), `reask_reason`,
`first_judgment`/`first_input_sha256`/`reask_failed` when re-asked,
`verdict_sha256`, `editorial_sha256`, `novelty_defaulted`, and the final
storyline key). Exact record/replay and every scoring path validate one typed
`ScoredJudgment`; they never reconstruct editorial state from an independent
verdict dict. Exact replay binds
the request to the resolved runtime model identity; a recording mismatch or
miss fails rather than falling through to live I/O.

There is no second product model stage: one Event persists one
SemanticJudgment and one card (issue #57), produced by the two internal serial
Predictors above.
That changes the normal provider-call cost from one to two and expands the
latency and failure surface; the benefit is future per-Predictor optimization,
not a claim that the initial Program is already more accurate. `escalate`
stays a `decide()` outcome — a high-importance
push that rides the same `verdict.push` routing key at AMQP priority 5 and
wears a ⚡ card header — and never triggers another Program execution. The retired
Analyst lane (`q:news.deep`, the `verdict.escalate`/`verdict.deep` routing
keys, the evidence bundle and its `verify_verdict()` gate, follow-up cards)
left `stage='deep'` verdicts and `kind='followup'` deliveries as historical
rows that are never written again, and topology declaration deletes an old
`news.deep` queue at startup.

Delivery (`tracefold.news.delivery`, `consumers.DelivererConsumer`) renders the
reader contract (`news_delivery_card_v10`): the header is `headline_zh` (⚡ when
the decision is escalate); only after headline sanitization failure may it
inspect a historical compatibility `title_zh` — which `ReaderCard.v2` never
produces — then the original title. The first body line is `why_zh`, and the
second is the facts in plain
words — direction label, `新进展` when the verdict's `novelty` is `progression`
(#113: 28.8% of a week's cards advanced a story the reader already had one for
and the card said nothing), magnitude label, the tickers the model called
primary and the Gate grounded, source（N 条报道）, and the leader item's
publication time in the reader's zone (UTC+8). Ordinary News derives this list
from model-primary ∩ Gate-grounded assets; deterministic OI derives one symbol
from its matching rank-ledger row only when the current Event kind and full OI Program
identity also match. The third body line is the market's own number for those
same verified tickers — `行情 CL $86.43 24h +2.30%（永续）`. At delivery, an
on-demand trade/candle point becomes the current number; if no contract
produces one, an existing `fresh` Quote Snapshot may still provide the display
price. A stale, unavailable or unlisted result leaves no line, no placeholder
and no zero. The change window is named from `change_basis` rather than assumed
(`rolling_24h` -> `24h`, `provider_day` -> `日内`, unknown -> the price without a
percentage), `（永续）` marks each asset whose number comes from a proxy
market rather than its own — an equity/commodity/index on a Binance TradFi perp
or a Hyperliquid/OKX TradFi perp, 51.9% of a week's card assets. It is keyed on
`instrument_class`, not on the contract type: BTC also prices on a perpetual,
but for a crypto asset that *is* its own market, so it carries no mark. The mark
is repeated per asset rather than said once for the line, because a trailing
mark on a mixed line cannot say whether it covers the last asset or all of them.
The price/percentage formatting
mirrors the console's `web/src/features/news/model/newsPrice.ts` character for
character. Source candidates and the existing quote snapshot are read in one
separate short database session over exactly the code-verified
`reader_assets()` result. Provider I/O begins only after that connection is
returned, so the facts and market lines cannot name different assets and no
database transaction is held across public REST calls. Any price failure
degrades to the existing fresh display quote or no line: delivery eligibility
never depends on the price plane.
That same read may produce an ephemeral typed `ReaderTradeTarget` only from an exact official catalogue contract.
The target is not part of `news_delivery_card_v10`: only the Telegram Adapter uses it to link the displayed ticker
to the matching Binance, Hyperliquid, OKX, Lighter, or Bitget trade page. Untyped URLs and inconsistent metadata
stay plain text, and Feishu receives the card unchanged.
The same ephemeral `ReaderDeliveryPresentation` carries one ordered market row
per displayed asset. `新闻后` is the delivery-time point versus the provider
publication-time point; `1h` is that same delivery-time point versus the point
exactly one hour before delivery. For Telegram, “delivery time” is the timestamp returned in the receipt of the
initial `sendMessage`, not the later edit time. The Deliverer intentionally renders a pending presentation first,
settles that receipt as `sent`, and starts price enrichment only afterward in a background task. The pending
message shows `计算中` for `新闻后`, `1h`, and `24h`; the ready presentation is applied to the same provider
message with `editMessageText`. Immediately before the provider mutation, the desired ready card is durably
recorded as `pending_card/editing`. Provider confirmation atomically promotes it to the canonical card and
`edited`; an uncertain failure retains the previous confirmed card plus the desired card under `ambiguous`.
This keeps public price latency outside the reader's initial-news path and gives
later enrichers one receipt-bound in-place update capability without creating a follow-up card. Edit work is
serialized independently of initial sends, so a slow update cannot delay the next accepted news message.
These are request-time presentation returns,
not `reaction_v1`: they do not wait for a future horizon and are never persisted
as review evidence. For every anchor the adapter first selects the latest trade
at or before the millisecond timestamp when it is at most 60 seconds old, then
falls back to the last closed one-minute candle within 90 seconds. Binance is
tried first, Hyperliquid second, and OKX third for an already grounded asset; a newly discovered exact contract
may also use Lighter or Bitget. One row always retains the same venue and contract for its current, news,
push-minus-1h, and push-minus-24h anchors. `24h` is calculated from the current and minus-24h anchors on that
same contract; a fresh same-contract snapshot is only a fallback when the on-demand point path is unavailable. A missing value is
shown as `暂无`, never borrowed from another window. Telegram renders each asset as a separate four-line block:
`🎯 标的 BTC`, `新闻后 +1.10%`, `1h +0.80%，`, and `24h +3.20%`; multiple assets repeat the complete block with
a blank line between them. Impact and polarity share one direction row, such as `🧭 方向 明显利空`, while
novelty is a badge immediately below the title (`🆕 新事实` or `🔄 新进展`). A progression names the previous
headline immediately only when the optional post-delivery verifier is unavailable and an exact-fact retrieval or
a stored title-similarity score of at least `0.50` supports it. With the verifier configured, the first message
shows a one-line indented `关联确认中` child block without naming a parent. After the send receipt is durable, one bounded structured LLM
call compares the current Event with at most eight selected told-ledger candidates. It confirms only the same
concrete subject and event chain with a material new action, result, number, confirmation, reversal, or state
change; a shared topic, sector, ticker, country, or storyline bucket is insufficient. Price reads and this review
run concurrently and settle through one edit of the original Telegram message. A confirmation replaces the
pending child block with a nested `✅ 已确认关联` block that links the stored prior Telegram receipt as
`此前：<parent headline>`, shows the age calculated from the two actual push timestamps, and gives one concise
`现进展：<reason>` line; a rejection shows
`↩️ 未确认关联: <concise reason>`; timeout or invalid output shows an explicit unavailable child block. Rejection,
unavailability, or a model confirmation without a sent, undeleted, same-target parent receipt changes the edited
message to `🆕 新事实`; it never retains `🔄 新进展`, invents an unlinked parent, or derives age from candidate
event time. The exact
verifier result and its content-addressed verifier identity are stored inside the desired durable card before the
edit intent. Broad zero-similarity storyline buckets therefore never become an unaudited “上一条”. If a macro or sector verdict has no
code-verified ticker, Telegram explicitly renders its scope and `暂无直接标的`. It turns
the normalized reporting-origin
text itself into the original-source HTTPS link (X/Twitter handles become `<handle> 的推特`; known wire brands
use their reader names), so it has no separate source button. The footer has no time heading and lists the
original artifact or provider publication time, send-start time, and normalized linked source in that order.
Times use the reader's UTC+8 zone at whole-second precision. A missing input is shown as `暂无` while known fields
remain visible. Typed trade targets, movements, and timing context are not persisted; the progression-review
result is part of the desired card, alongside the rendered desired card and edit lifecycle. The edit ledger CAS identifies the original provider, message ID, push timestamp,
and keyed target digest, and a canonical receipt admits no extra provider fields. Every `editing` row inherited by
a new process is changed to `ambiguous` at startup rather than guessed successful or retried. The consumer does not
start until that reconciliation commits. A 30-second runtime sweep also terminalizes an `editing` intent older than
60 seconds, so a temporary failure of both edit settlement and ambiguity recording cannot strand it forever.
For a `single_name` card with one candidate ticker, or with no grounded ticker but a confident code-like identity
in its title, a second post-send task derives exact ticker aliases (including market-coded forms such as
`02605.HK`) and queries fresh Binance, Hyperliquid, OKX, Lighter, and Bitget catalogues.
An exact hit keeps the message, adds the typed target link, and prices that contract without waiting for the
periodic universe snapshot. Only five successful empty catalogue answers authorize deletion. Any missing identity,
timeout, blocked response, malformed catalogue, or partial venue fan-out keeps the message. Before `deleteMessage`,
the full catalogue outcome and reason become a durable `deleting` intent bound to the original receipt; provider
success becomes `deleted`, while uncertainty becomes `ambiguous` and is never retried destructively at startup.
Feishu still receives the stable card unchanged.
The stable Feishu card then has a 打开来源 button and a small `Tracefold · <event_id[:8]>`
note. There is no original headline line, no translated title, no event type or
scope enum, no provider score, and no line labelled as AI: those internals stay
in the console and `tracefold news why`.
A degraded Event (the model chain failed and the rule baseline still pushes)
gets the wire text instead of a verdict view: the original headline as header,
the original description as the body line, and a facts line of tickers,
source and time only — no direction, magnitude or novelty the model never judged
and no "模型不可用" copy; the degraded verdict's `headline_zh` is the wire headline
too, so the console feed and the context line name the Event (issue #65). The
行情 line still renders there: the price is our own fact, not the model's.
AI copy is sanitized (URLs fall back to the code-owned title). There is no
initial-send retry: `news_deliveries(event_id, kind)` (`kind` is always `first`) is
inserted as `sending` after provider prepare/preflight and before the single
initial delivery HTTP call, then settled `sent`/`terminal`. Telegram enrichment begins only after a successful
settlement. It records edit intent before provider I/O; update success confirms the ready card/receipt, while an
uncertain update or post-provider persistence failure keeps the initial `sent` state and records edit ambiguity;
interrupted rows are terminalized at startup. Recovery items, suppressed
events never deliver. There is no operator pause or mute: `news_control_state`
was removed after never withholding a single card in the whole retained history,
and an unread singleton that two hot-path consumers still SELECT is how a second
decision plane grows beside `decide()`. Policy v7 has
no hourly, two-hour, or four-hour reader quota: a push/escalate decision reaches
the Deliverer regardless of how many earlier cards were sent.

Incidents and recovery: WSS transport/auth/protocol/idle failures, broker
backpressure/unavailability, and Triage circuit opens are rows in
`news_opennews_incidents`; reconnect closes transport incidents and requests
recovery, which pages the official Strategy hits endpoints for the closed
interval and publishes `raw.recovery.*` frames (normally
`admission=recovery`; unsupported contracts retain their named admission;
never delivered). Dead letters are operator-visible through `tracefold news dlq
inspect|replay|purge`.

News storage is split by meaning, not by a fragile table count. Material
evidence and current Event state remain in the ingestion/Event tables;
judgment, delivery and exact evidence snapshots are immutable observations;
reviews and learning artifacts form the cold learning plane. Read queries are
registered in `tracefold.news.query_specs` for the query audit.

Learning loop (#112): `ReviewDesk` draws deterministic, version-homogeneous
tasks from sent, model-drop, Gate-suppressed, throttled, delivery-failed,
high-reaction and random strata. The operator sees the exact historical
evidence, verdict, policy trace and real sent receipt, then records a
multi-dimensional rubric (`should_push`, factuality, evidence sufficiency,
entity grounding, novelty, direction, magnitude, copy value, timeliness and
first bad owner). `news_review_v4` adds exact gold for the seven
TradeRelevance fields, including `reader_value`; a failed scored dimension without
expected gold is not scored. A judgment becomes training/eval truth only after a separate
acceptance receipt. An important fact missing before Event creation enters as
an immutable external-miss snapshot, rather than a fake Event id.

Issue #129 first starts the immutable `program_v1` learning epoch at migration
deployment time. Corrective migration `0293` preserves that history and appends
`program_v2` after fixing the semantic retry state machine. Issue #132 migration
`0294` preserves both prior rows and appends `program_v3` for the expert quality
baseline and semantic normalization. Issue #134 migration `0295` appends
`program_v4`; `0298` appends `program_v5` for candidate-conditioned ToldContext.
Issue #160 migration `0301` hard-renames persisted `priority` to
`queue_priority`, adds atomic editorial/runtime-manifest judgment identity, and
appends `program_v6` for factory v4/executable v4/policy v10. All earlier reviews, datasets, recordings,
reports and release receipts remain readable audit evidence, but they are
promotion-ineligible and cannot seed the current Program. Evidence
accumulation starts from zero: Event reviews and acceptance receipts must be
created after the current epoch, and eligible verdicts must match the exact
stable Program bundle.

`CandidateEvaluator` is a deep Module whose Interface freezes accepted
current-epoch / `news_review_v4` evidence, compares stable with exactly one declared `program` or
`policy` variable, and publishes release evidence. Validation/holdout replay
both arms sequentially because each arm's would-reach-reader ledger changes
later decisions. Predictor requests/responses are recorded per call and
content-addressed; replay mode must match request, Program and resolved runtime
model identities exactly or fail. A frozen dataset accepts Event cases only
from the exact active Program bundle cohort and records every Program,
retrieval, runtime-model, execution and policy hash plus the reader-contract
version; a mutable provider model alias is marked as mutable rather than
described as an immutable snapshot. Hidden validation
pre-registers at most 50 independent fact-cluster representatives before either
arm output is inspected, permits at most 100 human judgments, and returns
`UNKNOWN` when the batch remains unresolved. A candidate-only critical error
(unsupported fact, wrong entity/direction, missed key fact, severe repetition,
or injection obedience) is a release failure. Mean and peak delivery load are
reported for operator impact analysis but are not candidate-release quotas;
correctly recognizing many distinct facts cannot fail a release by count alone.
The optional GEPA optimization is a cold, manual development tool, never a
Workers loop. `news learning optimize` reads a frozen development corpus once
and then holds three model endpoints and a typed budget — no DB write, broker,
delivery, canary or promotion credential — and can emit only a bounded
`PromptPatchV1`. That patch carries the two Predictor instructions and nothing
else: the graph, the output schemas, the execution budget, the model slots and
the policy are code, covered by `envelope_sha256`, and outside the write set. Since #306
Phase 3 the optimizer calls `gepa.optimize` directly through a `GEPAAdapter`
this repository owns, and evaluates a candidate by running the production
Program over the frozen corpus — so "the optimized bytes are the production
bytes" is structural rather than something a refactor-baseline test has to keep
proving. GEPA cannot accept a review,
register/deploy its output, move a stable pointer, or promote a candidate.
#202 deleted the container platform that used to surround it — image, launcher,
metered proxy sidecar, sandbox policy, tariff, build attestation — because it
proved *where* two strings came from, which was never what made them safe.
Automated optimizers may propose a Program candidate but cannot modify the
reader contract, rubric, accepted reviews, holdout, thresholds, stable bundle,
or production assignment.

One optimization produces one `news_prompt_candidate_v1`, and only when it ends
in `ADVANCE`. Every terminal state — `NO_OP`, `REJECTED`, `ADVANCE` — also
writes a complete `news_optimization_run_report_v1`, so a run that spent a
budget and shipped nothing is still readable. Issue #193 had already collapsed
the compile's evidence into a single `CompileRecordV1`; #202 removed the compile
itself, and with it the record, the sealed input bundle, the sidecar's per-call
ledger, the `CompilerBuildAttestation` and the tariff. Those documents proved
*where* two instructions were produced. Nothing downstream ever needed that:
`gepa.optimize` returns a `dict[str, str]` of named component texts, so the
write-set is two strings and `run_gepa` refuses a winner that is not exactly the
two. Rows written under the old chain stay in `news_learning_artifacts`
as append-only audit and no longer parse, so they cannot be re-armed.

What replaced provenance is binding, checked at registration by a party that did
not produce the candidate. `release register` re-applies the patch to the
running stable Program to *derive* the arm's identity, re-projects the frozen
corpus, records its own `development_episode_projection_root_sha256`, and
re-derives the #199 Objective Plan rather than trusting the candidate's summary.
A declared optimizer split is registrable only when its Objective Plan schema,
representative case IDs/count/root, and split all equal that re-derived plan;
registration checks this before writing candidate artifacts.
A patch a person wrote and a patch GEPA wrote are admissible on exactly the same
evidence.

Each of the three roles — task, reflection (32k tokens) and `metric_judge` — is
one `ModelExecutionIdentity` carrying the complete secret-free execution
contract, in place of the `endpoint_sha256 -> model_sha256 -> binding_sha256`
chain and the role binding above it. Its one surviving digest is
`endpoint_fingerprint`, because the endpoint URL names the host a credential is
presented to and therefore may not be stored. All three are required before a
budget is spent, the judge included: it is called by the metric rather than
through the metered LM, so its own admission ceiling is bound to the declared
`max_metric_judge_model_calls` up front. The judge is wired explicitly for
headline/why/factual semantic equivalence; its calls, cost and failures stay
separate facts, and unavailable means failure-as-zero rather than byte equality,
hidden retry or cache.

What bounds the offline job now is what it holds, not what surrounds it: a
frozen corpus read once as `serve`, three model endpoints, and a typed in-process
budget whose per-call ceiling is also the rate an unpriced call is charged at.
No database write credential, no broker, no delivery, no canary, no promotion.
If dynamic code generation ever becomes a candidate again, the sandbox threat
model is rebuilt with it under a new Issue rather than kept warm for it.

What GEPA is allowed to optimize is decided once, by `learning/objective.py`,
and every plane that needs the answer rebuilds the same plan from the same
frozen episodes: `news learning readiness`, `news learning baseline --dataset`,
`run_gepa` through the one offline entry point, and `CandidateEvaluator` when it
re-projects a registered candidate's corpus. A case is a **target** only when an operator wrote
`first_bad_owner = triage_prompt` into the submission itself — a ReviewDesk-derived
owner routes queue work and grants nothing — and the failure belongs to
EventSemantics or ReaderCard with something checkable behind it: an exact typed
gold value, or a `factual_fidelity` failure with evidence refs plus a stated
correction, or a novelty prior that reached the ToldContext the model actually
saw. A failed `headline_fidelity` / `why_support` / `why_value` is *not* a target
however well attributed: `ExpectedCorrection` holds no value for a copy
dimension — "the correct Chinese sentence" is not a label — so `_component` files
it as `not_scored_no_gold` and drops it from the denominator, and a target the
ruler cannot see lets GEPA pick a winner without ever scoring the repair it was
pointed at. `factual_fidelity` is the exception because a failed one arms the
`factual_contradiction` hard gate, which zeroes the case until the judge verifies
the candidate's facts against the frozen evidence. A **control** is a case the
stable Program already answers correctly under the accepted review and that
trips no hard gate. Everything else is an **excluded diagnostic** — retrieval,
Gate, storyline, policy, delivery, taxonomy, provider failures, derived-only
owners, failed dimensions with no stated correct value, accepted external misses
with no stable output — and stays visible in readiness and baseline reports
without ever entering a reflective minibatch. `run_gepa` splits `target +
control` and nothing else, after Objective Plan v2 elects one deterministic
representative per connected fact cluster. Targets beat controls, then the
election prefers more target dimensions, safety status, the
newer Event and stable case id. Shadowed media members remain frozen audit facts
but add no optimizer weight; required split coverage still fails closed. Before
#199 it scoped targets owner-blind and split the whole corpus, so a retrieval
miss became an instruction to repair.
The candidate's `optimization_objective_summary.v2` binds the plan schema and
representative ids/count/root; registration re-derives that population and refuses claims that do not
carry the current identity, while leaving their artifact bytes intact.
`news learning readiness --development SHA` publishes the plan with zero model
calls, and `optimize` rebuilds it and refuses on the same conditions before any
endpoint is touched. Its report (`gepa_readiness_report.v2`) also carries the
frozen dataset's own sealed `coverage` counts, so one document answers both
whether a corpus may be optimized and how much separable evidence is in it.

Whether a development corpus is *enough* is decided by coverage, never by the
calendar (#259). The release profile asks for independent connected fact
clusters by role — boundary, retention, negative, at least one safety — plus the
strata both split halves must carry, and the Objective Plan asks for verified
Prompt targets, Stable-correct controls and a cluster-disjoint, time-ordered
split. `natural_day_n` — how many distinct UTC dates the accepted cases opened
on — and `window_duration_hours` are published beside those counts as
diagnostics of case concentration and gate nothing. The two say different things
and may disagree freely: a 72 h freeze whose reviews all landed in one afternoon
reads `1` and `72.0`. Counting dates measures midnights rather than evidence,
and because a frozen corpus admits only cases produced by the *active* Stable
bundle, a calendar gate delayed every Stable iteration by days it had no way to
produce. Out-of-time generalization is
proven once, later, by the Future Holdout — a ValidationDataset frozen strictly
after candidate registration, at least 24 h long, with its own eligible-Event
and reviewed-cluster floors. No stable-age, window-age or calendar-day gate may
stand in for it, and a development temporal diagnostic is never holdout
evidence.

`news learning run` (#253) is the recommended way to reach a terminal report:
one command that runs readiness, the standalone `compile_live` baseline and the
one optimization over the same frozen corpus, and then publishes
`run_summary.json`. That summary is a projection, not an authority — it reads
what the three reports already published — and it exists to keep three different
baselines apart. The **standalone** number is an independent physical run; the
**GEPA seed** number is the seed Program's score inside the run that proposed
against it, and is the real *before*; the **future test** number is Stable on
accepted examples that did not exist when the candidate was made, and only the
release plane's holdout stage can produce one. The summary publishes the first
two with their difference and refuses to imply a comparison when dataset,
representative set, split, metric, Program or model binding disagree.

Metric v5 (`tracefold.news.production_action_trade_relevance_v5`) uses the one
version-bound production-action projection shared by baseline, failure-cluster
selection and CandidateEvaluator. Its candidate scalar weights 45% final
production action, 35% exact TradeRelevance dimensions, 10% semantics/novelty,
10% ReaderCard reviewer anchors and 10% the deterministic ReaderCard copy lint,
normalized over the components a case actually carries, with component
denominators/effective weight mass/gold coverage published. Listing/telemetry
are outside the relevance denominator; watchlist guard cases are policy evidence
and do not send action feedback to GEPA.

The copy lint (`tracefold.news.reader_card_lint_v1`, #306 Phase 1) is what makes
the ReaderCard side scorable at all without a reviewer label. Before it, the
only card dimension the ruler could measure was `factual_fidelity`, through the
sealed equivalence judge; the rest of the card contract — banned evaluative
filler, meta openings, self-description, emoji, URLs, the Chinese language
boundary, the 15-60 character headline band, the count of decision-relevant
numbers the original headline stated, a single-sentence `why_zh` — lived only as
prose inside a RulePack, and prose cannot score a candidate. The lint is pure, framework-neutral code with no
model call and no Gold dependency, so the metric, the Objective Plan's mirrored
gate ladder and any offline report read the same answer, and its tables are
hashed into the metric receipt like the rest of the ruler.

Two severities, and the split is published in the receipt rather than implied.
**Hard gates** are `card_lint_url` and `card_lint_self_description` only: a card
carrying a URL or describing its writer as a model is not a worse card, it is
not a reader card, so it zeroes the case the way `must_hold_send` does and never
sends a repair instruction to EventSemantics, which cannot cause it. Everything
else is **scored** — one point per applicable check in the `reader_card_lint`
component — including the language boundary, which is a real rule but leaves the
rest of the card measurable and is the check most likely to fire on copy that is
otherwise fine. Number retention reads only standalone numeric literals: a digit
that continues a word (an identifier, a build hash, `COVID19`) is not a number
the headline promised to keep, and treating one as such would fail faithful
cards, and the number check counts figures rather than matching them, because a
faithful rendering converts the unit (`$1.5B` -> `15亿美元`) and a
literal-identity test would fail the conversions the contract asks for — and
feed that failure back to the optimizer as a repair instruction. A gated card
publishes its gate and no per-check outcomes, so the component denominator never
disagrees with the zero.

Promotion is monotonic: development screen -> future temporal validation ->
blind pairwise review -> 24 h shadow -> deterministic 10% canary -> stable.
Every stage requires the prior sealed PASS. One Event is assigned to exactly
one production arm before Program execution and runs exactly one assigned
Program. Canary selector `news_canary_selector_v2` excludes recovery, deterministic listing and
telemetry, but includes queue-high Events. Startup, resume and assignment bind
and validate selector version, eligibility-profile SHA, rolling-profile SHA and
the exact runtime manifest; any drift trips the activation. A
candidate artifact/schema fault trips the canary to stable, and activation,
assignment, deployment and rollback receipts remain auditable. The market view
is secondary discovery evidence only: it defaults to one exact
Program/policy/runtime-model cohort, uses horizon-mature coverage denominators,
clusters similar withheld Events at fact grain, and never treats a 1 h/4 h move or a
directional hit as causality, reward, or `should_push` truth. The former
directional-hit, price-by-magnitude and price-by-event-type rankings are
retired: ReviewDesk does not render them, the taxonomy is not reliable enough,
and they consumed the 30-day read budget without producing release evidence.
Coverage may span 30 days, while the operator discovery queue is explicitly
bounded to the most recent seven days; the market view rejects a larger window,
while the separate evidence-coverage view retains 30 days.

`tracefold news replay <hits.json> [--gate-policy]` remains the deterministic
provider-hits Deduper+Gate regression; `tracefold news why <event_id>` prints a
single production chain. The retired single-label evaluator, policy-only
corpus gate, label-copy UI and `news_event_labels` table no longer exist.

Migration history begins at a squashed root. `20260818_0275_baseline` is the single root
revision: it executes the frozen `current_schema_20260818_0275.sql` dump
(every table, index, constraint, seed row of the schema as it stood after the
News V3 hard cut and Radar removal) plus `runtime_roles.sql`, and it is
irreversible. The first chained hard cuts follow. `20260818_0276_review_49_hard_cut`
drops the retired title-translation, DEX discovery, token profile, token
image, and Radar-era checkpoint tables. `20260818_0277_gmgn_lane_removal`
drops the whole GMGN lane: the social evidence tables (`raw_frames`, `events`,
`event_entities`, `enriched_events`, `collector_pending_items`,
`event_anchor_backfill_jobs`), token identity and registry tables
(`token_evidence`, `token_intents`, `token_intent_lookup_keys`,
`token_intent_evidence`, `token_intent_resolutions`, `registry_assets`,
`asset_identity_evidence`, `asset_identity_current`, `us_equity_symbols`),
DEX/CEX market data tables (`market_ticks` with its default partition,
`market_tick_current`, `price_feeds`, `cex_tokens`), the persisted live
broadcast journal (`persisted_live_events`), `provider_circuit_state`, and the
News market-mark table (`news_event_market_marks`), plus the
`forbid_market_fact_update()` trigger function and the terminal-evidence rows
of the dropped queues. `20260819_0278_macro_lane_removal` drops the whole Macro
lane: the ten `macro_*` fact/derived/queue/frontier tables, the four general
market observation tables (`market_instruments`, `market_observations`,
`market_settlements`, `market_position_facts`), the durable queue
terminal-evidence table (`queue_terminal_events`, whose only writers were the
Macro repository and the projection frontier), and the
`reject_macro_fact_mutation()` trigger function. Revisions `0279` through
`0283` add listing admission, the consolidated instrument universe, the
retired label-v1 foundation, and Price Review. The #112 hard cut is `0284`
through `0290`: atomic fact/evidence snapshots, ReviewDesk v2 (including
verified migration and removal of `news_event_labels`), content-addressed
learning artifacts/recordings, durable canary control, and bounded
learning-evidence retention with release-chain pinning, plus the production
Workers evidence-append grant/lock repair and role-authentic audit. `0291`
removes the local OpenNews Strategy allowlist. Issue #129's irreversible
`0292` migration adds Program identity and per-Predictor recording fields,
creates the append-only deployment-time `program_v1` epoch, and marks all
earlier Prompt-era learning evidence audit-only. `0293` preserves that row and
appends the corrected `program_v2` epoch, making `program_v1` evidence
audit-only for current release decisions. `0294` preserves both earlier Program
epochs and appends the expert-quality `program_v3` epoch, making `program_v2`
evidence audit-only for current release decisions. `0295` preserves v1-v3 and
appends `program_v4` with factory v2; `0298` preserves v1-v4 and appends
`program_v5` with factory v3 on the artifact-v2 envelope.
`0301` performs the #160 hard cut: `news_events.priority` becomes
`queue_priority` with no alias; verdicts gain atomic editorial/scored/runtime-
manifest identity; `program_v6` binds factory v4, executable v4, policy v10,
review v4 and metric/compiler protocol v3; and older evidence becomes audit-only.
`0303` preserves that history and appends the #162 `program_v7` epoch for
factory v5/executable v5 after the Program/Learning package split; the v6
baseline remains immutable audit evidence. `0304` carries the #193
strategy-artifact hard cut into the database: it trips every armed or active
canary, because the candidate it points at is unloadable in the new image, and
records one migration receipt in the append-only learning ledger. It
deliberately does not re-open `program_v7`. A serialization and identity change
is not an evidence reset, so accepted `news_review_v4` truth stays eligible and
the epoch row goes on naming the factory, schema and baseline root the epoch was
opened with — the same way it already did across the #175 and #190 re-issues.
`20260825_0305` carries the compile-record half of #193 the same way: it admits
`compile_record` as a learning-artifact kind, keeps `compile_receipt` readable
so existing rows stay audit history, and trips open canary activations because
a candidate registered against the old chain names a receipt that no longer
validates and can no longer be evaluated. It does not re-open `program_v7`
either — how a compile is serialized says nothing about whether an accepted
review is true.
`20260828_0320` ends the practice these rows document (#314): it gives
`news_learning_epochs` a `bundle_sha` and an `envelope_sha256`, relaxes
`program_factory_id` to nullable, and grants `tracefold_workers` the INSERT that
lets a deployment open its own epoch. UPDATE and DELETE stay revoked and the
append-only trigger stays, so a runtime writer may add history and still cannot
rewrite it. No migration appends an epoch row after this one.

`20260827_0315` persists the #288 exact source-contract route and Event-kind
hard cut, trips open canary activations, and records the factory-v6 to
factory-v7 migration receipt. It neither rewrites nor appends the `program_v7`
epoch row: all earlier rows and bundles remain immutable audit history. Because
current acceptance is bound to the exact factory and Program bundle, prior
factory evidence is audit-only and the factory-v7 cohort starts with zero
eligible evidence.
`20260828_0316` adds the one-table Trading Intent handoff, its Nautilus
execution projection, and the least-privilege grants for that separate runtime.
`20260828_0317` makes Nautilus the sole automatic execution authority.
`20260828_0318` starts the single-instruction Program-v8 evidence epoch.
`20260828_0319` starts the endpoint-capable-envelope Program-v9 evidence epoch.
`20260828_0320` adds capability-governed TradeIntentV2 and immutable replay receipts.
`20260828_0321` lets the running deployment open its computed-identity evidence epoch.
`20260828_0322` adds the durable desired/edited/ambiguous lifecycle for in-place News delivery edits.
`20260828_0323` adds the durable deleting/deleted/ambiguous lifecycle and five-venue evidence for confirmed
untradeable single-name Telegram messages.
`20260828_0324` closes the PostgreSQL `NULL`-truth gap in both delivery lifecycle shape constraints and rejects
any preexisting partial edit or delete intent before replacing those constraints; #325 owns its evidence-preserving
repair and roll-forward plan.
`20260829_0325` adds the Telegram manual-trading cursor, account binding and
snapshot, session/event/notification ledgers, immutable Intent, per-leg
write-attempt fences, and least-privilege runtime grants.
No chained revision has a downgrade. Exact-image replacement requires the
source, image and live database to share the current migration head; a schema
change uses an explicitly reviewed recovery or roll-forward plan. Earlier hard
cuts live only in Git history, never in a compatibility loader. A fresh database
and a database upgraded through the chain reach byte-identical schemas.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.


## Trading core (#104)

`tracefold.trading` is the disabled-by-default capital capability. It turns
persisted News/OI evidence into one frozen research decision and, only for an
instrument in the active Binance Demo execution-capability snapshot, hands one
immutable Intent to Nautilus. It does not claim Alpha.

```text
public News projections + public closed bars
  -> Candidate Gate
  -> immutable TradingCase
  -> strategy decision
  -> one transaction:
       INSERT immutable TradeIntent
       guarded Case RUNNING -> INTENT_EMITTED
  -> Nautilus bounded PostgreSQL poll
  -> Binance USD-M Demo execution and reconciliation
  -> execution projection on the same TradeIntent row
  -> Case -> Intent -> Outcome API / CLI / console
```

News and Trading remain sibling contexts. Trading reads only the two public News
projections supplied by the app composition root; neither package imports the
other or reads the other's tables directly. RabbitMQ remains News-only. No
Trading exchange, queue, outbox, LISTEN/NOTIFY channel, Redis, second database,
or in-memory correctness ledger exists.

### Telegram-first manual lane (#327)

Manual Trading starts only from the exact receipt of a sent, non-degraded,
directional News card with exactly one grounded primary asset. App resolves
that receipt through the public News repository and freezes a
`ManualTradeSource`; Trading never imports News or reads a `news_*` table. The
Telegram channel and operator user are independently bound before a callback
can mutate a session.

```text
sent News receipt -> [详细数据] [交易]
  -> tight-stop or wide-stop preset
  -> current Demo equity + public quote -> complete preview
  -> optional parameter edits -> combined Modification Guard
  -> normal confirm or distinct high-risk confirm
  -> immutable manual Intent
  -> independent manual executor
  -> market entry -> close-position TP + SL
```

The interaction edits one reply while material lifecycle events reply to the
source News message through a durable notification fence. Recommendation and
selected parameters are both retained. Preview includes notional, leverage,
margin, reference entry, TP/SL prices, expected PnL, account risk/return, and an
authoritative liquidation distance when one exists. The guard evaluates both
individual deviation and combined maximum loss; a rejected modification cannot
be confirmed.

Only `binance_usdm_demo` is executable in this slice. The venue-neutral plan
and adapter boundary are the insertion seam for a later OKX DEX/on-chain
adapter, but there is no wallet, signer, broadcast, live/mainnet route, partial
close, or simulated on-chain protection. The manual account credential is a
different file pair and database lane from the automatic account. Both runtime
roots record one-way credential and provider-account fingerprints; a duplicate
credential or shared provider account fails the binding transaction.

Every economic leg has three distinct durable moments: client-ID fence,
attempt marker, and confirmed receipt. The executor queries by client ID before
the first attempt. It commits the attempt marker before the signed write and
never writes that leg again. An explicit provider 4xx rejection is a terminal
`REJECTED` outcome; a timeout, rate limit, server-side write uncertainty, or
crash window is `AMBIGUOUS` until a read observes provider truth. Transient read
failures defer without changing durable intent state. Entry uses a market order;
TP and SL use Binance conditional algo orders with mark-price triggers and
`closePosition=true`, preventing stale protection from opening reverse
exposure. Stop loss is submitted before take profit. If either protection leg
is explicitly rejected after entry, the capital state becomes `EXPOSED`, keeps
the account-symbol lock, stops automation, and emits a high-priority Telegram
instruction for immediate manual handling. An `OPEN` snapshot means only that entry and both protection orders
were confirmed; trigger/close monitoring and partial-close protection resize
remain follow-up lifecycle work.

Material Telegram notifications are claimed in `(created_at, session,
event_index)` order. Interaction-message edit and original-News reply have
independent durable attempt/outcome fields; a failed or unknown edit cannot
suppress the required reply, and a crash never causes either attempted effect
to be replayed blindly.

**Trigger and context are different types.** A trigger is the one persisted
fact that starts an evaluation and fixes its cutoff. Context may enrich that
evaluation only when it existed no later than the cutoff. Notification `sent` is
notification transport success, not a trigger; capital must not depend on a
notification channel being reachable. The frozen manifest names exactly one
`primary_trigger`, one `strategy_id` / `strategy_version` /
exact typed `strategy_config` / `strategy_config_digest`, and a point-in-time
`contexts` object. `contexts.market` is the sole market truth; the manifest does
not serialize a second market copy. A restart rebuilds the exact strategy from
the frozen values instead of comparing the Case with today's thresholds. This prevents
a later News, OI, or market observation from leaking backwards into the case.

### Frozen execution contract

Execution permission is `active capability snapshot − canonical blacklist`,
not a target-symbol list. A cold refresh mechanically joins the complete public
News Binance-perp projection with every instrument returned by the pinned
Nautilus `1.231.0` Binance USD-M Demo provider. The content-addressed snapshot
partitions that full union: each instrument is either executable with frozen
native identity, underlying, quote, exact price/size increments, precision,
minimums, and stop support, or
excluded with one closed reason. Provider return order cannot change its hash.

Only a Case whose frozen decision is `long`, whose strategy permission is not
`shadow`, and whose complete `InstrumentRef` matches an included capability may
emit an Intent. There is no reroute or fallback venue. The canonical blacklist
keys only `crypto:{BASE}` and therefore denies every provider spelling of the
underlying. Both emission and the last entry fence capture its monotonic
revision, digest, and immutable payload; an intervening deny is a terminal flat
`REJECTED/blacklisted` outcome.

`trading.order.fixed_notional_usd` is the only operator execution value and is
validated as `0 < value <= 10`. The deployed image owns every other execution
value:

- Binance USD-M Demo, NETTING/one-way, long-only, 1x, market entry;
- 60-second Intent TTL;
- fixed-quantity native `STOP_MARKET`, 200 bps below entry and
  `reduce_only=true`;
- 180-second maximum holding from authoritative first fill;
- 25 bps maximum entry drift and 30 bps maximum spread;
- globally at most one nonterminal Intent and one entry fence per UTC day;
- quantity equal to
  `floor_to_venue_precision(target_notional_usd / fresh_price)`.

A quantity below venue minimum, insufficient balance, unacceptable quote, an
identity mismatch, or any inability to prove the contract is a refusal. Nothing
silently changes size, venue, side, leverage, or account.

CandidateRunner owns the handoff transaction. It reads the active capability
and blacklist under the same PostgreSQL transaction, inserts the content-
addressed `trade_intent_v2` row, then guards the already-claimed Case from
`RUNNING` to `INTENT_EMITTED` in the same caller-owned transaction. A failed
insert or failed Case transition rolls both writes back. There is no prepared
order, provider payload, approval state, backend selector, dual-write, or
fallback writer.

V1 rows remain readable audit history, but the database rejects every new V1
insert. Capability activation is a cold operation: Trading must be `PAUSED` and
no nonterminal Intent may exist. Activation consumes either the current
process's fresh green heartbeat/account-wide zero proof or a fresh proof from
the explicit zero-claim process; that process binds the expected active pointer,
never reports formal readiness, and cannot run outside `PAUSED`. The deployment
lifecycle uses it for first activation and replacement so an unloadable old
snapshot cannot prevent its own safe rotation. Activation clears the bootstrap
proof and invalidates readiness; before activation it remains valid for the
bounded provider load, up to five minutes. The replacement process then loads every
included instrument, revalidates all frozen provider facts, and reconciles a
complete provider account report before it can become ready.

### Nautilus execution authority

The separate `tracefold nautilus run` process is the only execution authority.
One process loads the active snapshot but owns at most one Intent lifecycle at
a time. It polls PostgreSQL once per second and claims a fresh `PENDING` Intent
only when `trading_runtime_state.control = RUNNING`, startup reconciliation has
proved the dedicated Demo account is one-way, every included instrument is 1x,
and the whole account has no unexpected position or open order. `PAUSED` and `CLOSE_ONLY`
block new entry fences but never stop query, protection, or exit work for an
already-fenced lifecycle. After restart, an already-fenced lifecycle remains in
the bounded command slot until the complete provider account report has been
reconciled; only then may recovery query protection, close exposure, or report
an unknown outcome.

The one `trading_intents` row is simultaneously the durable inbox, immutable
instruction, entry fence, restart checkpoint, current execution projection, and
audit identity. Workers may insert its immutable columns; the Nautilus role may
read the row and update only the execution projection; Serve is read-only.
Database constraints enforce the Demo environment, V2 capability/blacklist
identity, immutable Intent columns,
Case-to-Intent one-to-one identity, one nonterminal row globally, and one
fenced entry per UTC day.

### OI BAR replay and attribution

`tracefold trading replay-oi` is App-owned composition over public News and
Trading interfaces. After one short Workers transaction materializes timed
blacklist expiry, it reads the exact bounded OI projection plus the active
capability and blacklist from one Serve repeatable-read snapshot, then fetches
each fact's source-native Binance or Hyperliquid OHLCV bars. Every source fact
receives exactly one terminal replay outcome. Alpha evaluation ignores the
current blacklist; capital admission is a separate field, so research is not
rewritten by today's deny policy.

Instrument resolution uses the last immutable News catalogue validity event at
or before each source timestamp. The mutable current universe remains the live
view; the event ledger is only its historical listing/relisting/delisting
evidence, so a future contract cannot leak into an older replay.

Every directional scenario produces a typed replay intent and runs in a fresh
Nautilus `BacktestEngine`. Live and replay share the quantity, spread/drift,
stop, maximum-holding, and economic-leg identity policy. BAR fidelity is
reported honestly: funding and portfolio drawdown are `null`, never fabricated
as zero. `ReplaySpecV1` contains only input/policy identities, so equal inputs
produce the same `run_id`. App atomically publishes the content-addressed
artifact before Workers append the immutable PostgreSQL success receipt; a
duplicate validates and reuses the exact artifact, while corruption fails
closed.

The execution state is deliberately small:

```text
PENDING
IN_FLIGHT          phase = ENTRY | PROTECTION | EXIT
OPEN_PROTECTED
MANUAL_REVIEW
TERMINAL           outcome = EXPIRED | REJECTED | CLOSED_FLAT
```

Three invariants define the lifecycle:

1. Before a provider write, Nautilus durably commits the deterministic entry
   client ID and `entry_fenced_at_ms`. A restart seeing that fence queries and
   reconciles; it never submits a second economic entry. Unknown entry outcome
   is `MANUAL_REVIEW`, never a fabricated rejection.
2. A nonzero authoritative Position is either covered by a working native
   fixed-quantity reduce-only stop or is being fully flattened. Position
   quantity changes replace protection by deterministic stop generation.
   Projection failure stops new claims/fences but does not stop protection or
   exit work.
3. `TERMINAL/CLOSED_FLAT` requires fresh targeted venue proof that the Position
   is zero, the owned closing leg is terminal, and sibling stop/close legs are
   terminal or canceled. Unknown evidence remains `MANUAL_REVIEW`.

The venue is execution-outcome authority; PostgreSQL becomes durable truth only
when reconciliation writes the observed result back. The application does not
copy Nautilus's complete Order/Fill history.

### Runtime and cutover

A deployment with `trading.enabled=true` must have both secure Binance Demo
credential files and exactly one healthy Nautilus Compose replica; `make up`,
`make deploy-image`, and `make status` fail closed otherwise. A disabled
Trading lane does not start Nautilus.

The one-time PR 2 cutover is run only while control is `PAUSED`.
`make trading-hard-cut-preflight` proves one ready Nautilus replica (whose
readiness includes authoritative flat/no unexpected exposure) and checks that
legacy `PENDING/RUNNING` Cases, nonterminal Intents, and legacy active/unknown
Orders are all zero. Migration `20260828_0317` repeats the database predicates
inside the authority-changing transaction, adds `INTENT_EMITTED`, and revokes
legacy order/observation and retired runtime-counter mutations from Workers.
After migration, the operator proves Nautilus readiness and changes control to
`RUNNING`. There is no `accept_intents` rollout flag.

Rollback is allowed only with venue-proven flat and a schema-compatible image.
When exposure exists, the only safe direction is roll-forward: Nautilus retains
sole authority until it protects or closes the position.

### Research, admission, and durable data

The Candidate Gate still owns deterministic eligibility, source-aligned
research routing, freshness, deny-list, rank/liquidity, cooldown, and capacity
answers. It records one
`(source_key, gate_version, gate_config_digest)` decision so the console can
explain why a source did not become a Case. The scanner re-reads a bounded
overlap and relies on durable source identity rather than an in-memory cursor.

Cases freeze source identity, cutoff, attached context, exact strategy/version/
config digest, regime, price evidence, and instrument. OI-triggered Cases use
the arithmetic `oi_smart_money_momentum_v1`; News-triggered Cases may use the
single bounded Trading decision Program before the same pure strategy policy
returns the final decision. Shadow liquidation strategies remain research-only:
they can write immutable strategy evaluations and outcomes, never Cases or
Intents.

Current product reads are Case -> Intent -> Outcome only.
`trading_cases`, `trading_intents`, the candidate-gate ledger, runtime
control, blacklist, and strategy research ledgers are current inputs.
`trading_orders` and `trading_order_observations` remain read-only historical
audit after the hard cut and are excluded from status, cooldown, active/daily
admission, milestones, API, CLI, and UI. They have no production writer.

Stage latency is computed from the durable source, Case, Intent fence, open,
protection, and close timestamps. No report reconstructs an Order or treats an
HTTP response, process cache, model output, or provider response as alternate
truth.

What this capability does not contain: mainnet or real money, multiple venues
or accounts, shorting, take profit, scale-in/out, partial-exit attribution,
smart routing, a general workflow engine, a second execution backend, or an
operator command that places/amends/cancels an order.

## Open-interest telemetry (#137)

OpenNews strategy `1019` (`OI Event Monitor`) pushes a fixed-format frame about
190 times a day: `{SYM} OI Rise {x}%, OI Value {y}, Whale Long Profit {z}%,
Whale/OI Ratio {w}%`. The shared classifier admits it as
`telemetry_deterministic` only when the complete normalized tuple is exactly
`1019 / OI Event Monitor / market / market`; `1019` alone has no routing
authority.

Those four numbers are the whole message, so Triage judges the frame by
arithmetic instead of spending two structured model calls re-reading them.
`tracefold.news.oi_signals` parses it, ranks it against the symbol's other
threshold-eligible frames in a rolling `window_ms` (4 h), and returns an ordinary
`ScoredJudgment` with `editorial_origin=telemetry_deterministic` and null
relevance:
a qualifying frame — inside `max_rank_in_window` (2), with `whale_oi_ratio_bps`
above `whale_oi_ratio_above_bps` (8000, which the frame must exceed), and with
absolute OI change at least `oi_change_at_least_bps` — is an
actionable, directional
magnitude-2 `oi_spike` with a push intent, and a rejected one is a
self-consistent magnitude-0 `noise` that the deterministic telemetry action
preserves.

Returning the same typed scored judgment rather than a separate delivery lane
is the design, not a detail.
The rule counts, and `decide()` deliberately cannot: policy v7 removed every
reader quota and `StorylineStatus` is tested to carry no capacity field.
Counting eligible history in PostgreSQL inside the Triage transaction and
handing `decide()` a verdict it already knows
how to rule on keeps one decision plane, and keeps delivery, receipts,
`event_outcome`, the feed, the counters and the audit trail on the single path
they were built for.

Duplicate protection for this lane *is* the rank ceiling, so `decide()`'s content
check is skipped for it entirely. Every telemetry headline is one template: two
cards about unrelated symbols score 0.33 against the 0.25 threshold, and two
frames for one symbol score 0.41. `WINDOW_MS` and `TOLD_WINDOW_MS` are both 4 h,
so a rank-2 frame is always inside its rank-1 sibling's ledger — running the
content check as well would have shipped "the first two per symbol" as "one per
symbol". Two frames for one symbol are two different observations, and the
reader asked for the opening ones by count; a byte-identical repeat is still
collapsed upstream by the exact fingerprint. Listing frames keep the different
exemption they were given in #72, which is per instrument rather than blanket,
because two notices for the same instrument really are one fact.

Rank, ledger row and verdict are written in one transaction under the storyline's
advisory lock. PostgreSQL filters the complete `(cutoff, observed_at]` range by
the same strict whale-ratio and inclusive absolute-change thresholds before
`count(*)`; ineligible frames stay in the ledger for audit but never spend a
later signal's rank. Reading that count outside the lock would let two frames for
one symbol both see a history without the other and both claim the same rank.

`news_oi_signals` is the rank ledger and nothing more: a derived read model with
one writer, idempotent by `event_id`, rebuildable by re-parsing the Item, and
cascade-deleted with it. `rank_in_window` is the eligible rank under the policy
recorded in the verdict trace; rows that failed a threshold are still stored and
carry the eligible position they would have occupied without consuming it for
later rows. Two consequences of judging these frames rather than
suppressing them are deliberate and worth stating: every 1019 frame now carries
a verdict, so `news.retention` keeps its Item for 365 days instead of purging at
30 (~70k small rows a year), and the card may show the ledger-verified ticker
plus a fresh Quote Snapshot without altering the empty provider/Gate
`grounded_assets` evidence. A pre-reader-contract verdict, a mismatched Program SHA, or an unavailable
quote leaves that ticker/行情 context absent. The decision itself lives in `news_verdicts` like every
other decision, which is also where the lane's idempotency comes from — Triage
already re-publishes an unpublished push on redelivery.

Strategy provenance and parser success are separate contracts. Every live `oi_v1`
frame bypasses near-duplicate matching so provider format drift reaches Triage;
an unparseable frame fails closed without a model call and persists the named
`oi_parse_failed` rule/error. Its trace and structured warning carry strategy
id, OpenNews/provider source, a title SHA-256 rather than raw text, parser
version, source-classifier version and failure stage. Status exposes 24 h
received, parsed, parse-failed and pushed counts, while ordinary OI prose keeps
the normal Deduper and model path.

Two places treat these Events specially and both are explicit: they are exempt
from near-duplicate matching (two frames for one symbol differ only in their
numbers, which is their entire content, and would otherwise merge), and they are
excluded from `news_review_task_source_v1` and the model-health denominators,
because an arithmetic judgment is not model output and rating one teaches the
optimizer nothing.
