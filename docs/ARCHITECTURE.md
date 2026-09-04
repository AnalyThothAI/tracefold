# Architecture

Tracefold is one Python codebase/image with two mutually exclusive runtime
composition roots, one CLI, one React console, and one PostgreSQL database.
It has two business capabilities: News V3, and — since #104 — the Trading
core, a bounded context that turns persisted open-interest facts into immutable,
engine-neutral Trade Signals. They are siblings,
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

Beside both runs the Trading Signal core. It is disabled by default.
When enabled, one cold `SignalLane` reads persisted OI facts through a **public
News projection**, keeps the source venue only as evidence provenance, freezes
one content-addressed Case with source-native public bars, and runs one
deterministic long-only Alpha policy. `NO_TRADE` remains on the Case; `long`
commits `Case=SIGNAL_EMITTED` and an engine-neutral `TradeSignalV1` in one
PostgreSQL transaction. The Signal contains no account, route, quantity,
notional, leverage, order, grant, reservation, or OMS state.

The Nautilus Runtime consumes the Signal and the authenticated
OperatorIntent/Observation transport. Execution is disabled by default; paper
and live activate one profile-gated Binance USD-M TradingNode under the same
Strategy/Risk/OMS/reconciliation owner. New profiles are cold, require
authoritative Binance flatness, and start entry-paused until an authenticated
durable resume. RabbitMQ remains News-only.

`tracefold serve` initializes public HTTP/static, read repositories, serve
telemetry, and the one authenticated bounded operator-command append. That
append opens its own short write transaction outside the read pool and owns no
Runtime or venue semantics. `tracefold workers` initializes the bounded external
capability, singleton runtime status, and the RabbitMQ-driven News consumers
when News is enabled. News consumers recover by re-consuming durable broker
queues plus database idempotency keys. There is no database wake plane, no
projection/EDF coordinator, no CPU-process lane, and no in-memory correctness
dependency. Provider raw frames remain inputs until normalized and persisted
as material facts.

The deployment composition has four required boundaries: PostgreSQL, one
successful migration job, Serve, and Workers. `make up` is only their
fail-closed lifecycle orchestrator; it does not merge the two runtime roots.
On an empty PostgreSQL volume, the image's `initdb` hook creates one ordinary,
non-superuser application login, `tracefold`, from
`postgres_database_password`. It owns the public application schema and is
shared by Alembic, Serve, Workers, Nautilus, and CLI processes. The hook creates
the required extensions, revokes the `tracefold_app` bootstrap login, and is
never replayed against a non-empty cluster. Process attribution remains in
stable `application_name` values; the HTTP Serve pool separately enforces
connection-level read-only transactions.

The same project-scoped application image contains the Python service and a
production React build. Migration, Serve, and Workers use that exact image and
build revision with different commands and credentials.
`make up` builds the image once and recreates migration, Serve, and Workers;
missing execution credentials are a legal product state. It
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
PostgreSQL planner, or under the Runtime's own account authority. The four
canonical classes are:

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

The class states the required contract. News implements its `durable_event`
boundary with confirmed publish before settlement, structured consumer-task
supervision, two database-backed handoff repair lanes, and durable incident
recovery; none of those mechanisms turns RabbitMQ into business truth.

<!-- BEGIN EXTERNAL DATA INVENTORY -->

### Canonical inventory

This table is review evidence, not a runtime registry. Production never reads
it, and adding a flow here cannot enable a provider, task, queue or business
action. `Worker task` names the stable task interface when the flow has one;
`-` means the flow runs inside the task named by its parent row.

Business runner classes carry only a typed `work_semantics` review annotation.
The architecture harness discovers stages independently from the typed
`NewsPipeline` composition and the one `SignalLane`: every stage must
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
| Binance candles | News Market Review / Trading Signal | `derived_work` | Binance public closed 5 m bars | REST on planned demand | `news-reactions` or `trading-signal-lane`; due work | versioned `news_event_reactions` or frozen Trading Case evidence |
| Hyperliquid candles | News Market Review / Trading Signal | `derived_work` | Hyperliquid public closed 5 m bars | REST on planned demand | `news-reactions` or `trading-signal-lane`; due work | versioned `news_event_reactions` or frozen Trading Case evidence |
| OKX candles | News Market Review | `derived_work` | OKX public closed bars | REST on planned demand | `news-reactions` or delivery fallback | versioned `news_event_reactions` or ephemeral delivery presentation |
| Binance instruments | News Market Review | `latest_state` | Binance spot and USD-M catalogues | REST polling | `news-instruments`; 6 h, 15 m retry if none answer | `news_market_instruments`; Gate, quote/reaction planning, Trading projection |
| Hyperliquid instruments | News Market Review | `latest_state` | main perp, spot and bounded HIP-3 catalogues | REST polling | `news-instruments`; 6 h, 15 m retry if none answer | `news_market_instruments`; Gate, quote/reaction planning, Trading projection |
| OKX instruments | News Market Review | `latest_state` | OKX live USDT swaps and USDT/USDC spot catalogues | REST polling | `news-instruments`; 6 h, 15 m retry if none answer | `news_market_instruments`; Gate and price-source resolution |
| US reference instruments | News Market Review | `latest_state` | Nasdaq Trader symbol directories | REST polling | `news-instruments`; 6 h, 15 m retry if none answer | reference rows in `news_market_instruments`; non-crypto classification only |
| Event Reaction | News Market Review | `derived_work` | persisted Events plus venue candle history | PostgreSQL planner + REST | `news-reactions`; 60 s and bounded immediate catch-up | versioned `news_event_reactions`; review projections |
| Trading Signal lane | Trading | `derived_work` | one public News OI projection and source-native closed bars | PostgreSQL planner + REST | `trading-signal-lane`; App-owned poll, 2 s when enabled | admission ledger, frozen `trading_cases`, and atomic `trading_trade_signals` |
| Nautilus OI Runtime | Trading execution | `capital_truth` | `TradeSignalV1`, authenticated `OperatorIntentV1`, Nautilus Cache/Portfolio, Binance | bounded PostgreSQL transport; CLI and console ingress are durable before acknowledgement | profile-gated `tracefold nautilus` | append-only `ExecutionObservationV1` plus one current durable Runtime generation; disabled by default |

The runtime limits behind that inventory are code-owned safety policy. `shared`
means one turn-wide cap is divided among the named rows. `adapter-owned` names a
real boundary that has no second timeout imposed by these loops; it is evidence
for a future budget issue, not a claim of infinity. A dash means the concept
does not apply.

| Flow | Cadence / trigger | Freshness SLO | Batching key | Max targets | Max source groups / requests | External concurrency | Turn / provider deadline | Catch up / coalesce / stale-not-blank | Failure semantics |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| OpenNews live frames | provider push; reconnect after 3 s | provider-current | Strategy stream | provider-enabled Strategies; provider-owned | one account / one WSS | one WSS session | receiver idle/provider budgets; broker confirm | history recovery / incident windows / no | disconnect opens a durable incident; a frame is not business truth before Admission persists it |
| OpenNews history recovery | startup, request, or 300 s fallback scan; 30 s overlap | recover while provider history exists | Strategy + incident window | bounded pending incidents and enabled Strategies | 100 hits/page; shared 60 provider calls and 1,000 confirmed messages/turn | serial Strategies/pages | shared 30 s wall budget plus provider-client budget | yes / requested pass coalesces / no | typed transient failures and budget exhaustion stay pending with bounded backoff; only explicit no-history/retention terminalizes |
| RabbitMQ raw handoff | message delivery | durable backlog | message ID | raw prefetch 1 | one queue delivery | broker prefetch 1 | broker connection/confirm budgets | yes / no / no | PostgreSQL Admission is idempotent; decode/permanent/exhausted transient failures are terminal, handler-side broker failures are counted returns, and only settlement or unknown failures reach root supervision |
| RabbitMQ event handoff | message delivery plus 60 s repair scan inside a 30 min relevance window | durable Event marker | Event ID | configured bounded Triage prefetch; repair batch 50 | one queue delivery | configured bounded consumer | broker connection/confirm budgets | yes / stable-ID duplicates coalesce at Triage / expired is explicit | confirmed publish precedes Event marker; PostgreSQL repairs marker-null Events while relevant and projects older rows as expired |
| RabbitMQ verdict handoff | message delivery plus 60 s repair scan inside a 30 min relevance window | durable Verdict marker | Event ID + delivery kind | delivery prefetch 1; repair batch 50 | one queue delivery | broker prefetch 1 | broker connection/confirm budgets | yes / stable-ID duplicates converge on the delivery ledger / expired is explicit | confirmed publish precedes Verdict marker; PostgreSQL repairs push/escalate Verdicts while relevant, while external delivery remains at-most-once |
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
| Trading Signal lane | App-owned poll, 2 s | source age <= configured admission window; Signal TTL = min(180 s, admission window) | underlying / durable source key | 1 Case freeze and 4 decisions/turn | source-native public bar calls serial | one | adapter-owned provider / 10 s PostgreSQL boundaries | bounded overlap / durable source idempotency / no | missing or uncertain evidence creates no Signal; Case+Signal commit atomically |
| Nautilus OI Runtime | active only for `paper|live`; 0.5 s current heartbeat; complete private proof every 5 s and immediately on ambiguity/flatten; Nautilus native in-flight/open/position checks at 2/5/5 s | command/Signal TTL; account clock <10 s and complete reconciliation <15 s, both derived from the one 5 s period; public heartbeat stale after 5 s | account slot advisory lock | Commands and Signals share one count-and-byte bound; Commands admit and execute first | one Binance USD-M account | one account-slot writer | Runtime-owned | bounded anti-join replay / deterministic client IDs / fail closed | disabled starts no node; control state survives restarts and only a Command moves it; unowned exposure or lost singleton halts |

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

NautilusTrader `1.231.0` is a pinned dependency and the execution authority
inside the one profile-gated Binance USD-M Runtime process. It is not a new
business truth, scheduler, News transport, or provider registry. It consumes
`TradeSignalV1` and `OperatorIntentV1`, proves the dedicated account before
the account slot, and projects venue outcomes back as append-only Observations plus
one current row. PostgreSQL remains business truth; News, Triage, Review and
Learning stay on their present runtimes.

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
`unsupported_market_contract`. A missing value means the current writer parsed
the selected contract (or it needs no strict parser). Migration `0336`
physically deletes every pre-genesis Event, including rows that carried the
retired `source_contract_unverified` value. An OI signal row is derived read-model evidence,
not a second material fact source. At insertion it freezes the exact source Item,
source venue, actual availability clock, and learning epoch; evidence capture never
reconstructs those fields from a later Event leader or the currently active epoch.
OpenNews's raw `coins` annotation remains
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
append-only audit history. Only accepted `news_review_v6` evidence created in
the running bundle's epoch and bound to that exact bundle is eligible for
metric v8, optimizer, replay or release gates.
The operator fast loop that used to sit beside that plane
(`tracefold.news.learning.experiment`, #193) was deleted in #343, and with it
its on-disk run directories, snapshot/compare arm comparison and the
`promotable: false` experiment candidate. Offline research now enters only
through `news learning run` over a frozen development dataset.

`news_learning_retention_state` makes the bounded 90/365-day cold purge and
its current backlog/error observable; the database function pins the current
and previous distinct stable release chains. The exact `news_*` base-table set
and four security-barrier review views are executable in
`tests/integration/test_news_v3_pipeline.py`, not repeated as a hand-maintained
table-count contract.

## Package map

The production package is `tracefold/` at the repository root; there is no `src/` parent
and no compatibility path back to one (#373). One consequence is worth stating, because
it changes what a green test run means: the repository root is itself an import root, so
any process whose working directory is the checkout can import `tracefold` off the working
tree, and an ordinary pytest pass no longer says anything about what the wheel contains.
`tests/package/test_installed_distribution.py` is the answer to that — it builds the real
wheel and sdist, installs the wheel outside the repository, and reads the product and its
packaged resources from there with the checkout absent from the working directory,
`PYTHONPATH`, and `sys.path`. The image runs the same kind of check on itself from `/`,
since `/app` is an import root for the same reason.

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
    drafter.py        model-proposed rubrics an authorized reviewer accepts or rewrites; never writes the DB
  learning/
    dataset.py        freeze / load / project the immutable corpora; holds no release authority
    objective.py      framework-neutral: which accepted cases GEPA may optimize, hold as controls, or exclude
    optimizer.py      the one offline entry: role identities, budget, Objective Plan, GEPA, terminal state
    evaluate.py       run both arms over a frozen corpus and return evidence; decides no state
    taxonomy_metric.py  pure four-axis taxonomy comparison used by the existing case metric
    ledger.py / profile.py  the learning plane's own rows, its bundle's epoch, and the release profile
  release/
    candidate.py      admit a Prompt candidate: derive its Program identity, re-derive the Objective Plan
    canary.py         deterministic one-arm assignment and durable trip/close control
    runtime.py        image candidate lineage/availability and startup Canary reconciliation
  triage_rules.py     decide() post-rules (DecidePolicy), throttle, fail-closed fallback
  program/            SemanticJudge, artifact/registry, seed instructions, chat transport, artifact_tool
  delivery.py / control.py  cards, control commands
  pipeline/
    admission.py      the atomic Deduper transaction and raw-queue consumer
    receiver.py / recovery.py  live OpenNews ingest and official-history recovery
    triage.py / triage_audit.py  SemanticJudge route, policy persistence, execution audit
    triage_route.py   the route's typed vocabulary: arm selection, inputs, attempts, outcome
    delivery.py       one-attempt reader-card delivery consumer
    maintenance.py    instrument snapshot, retention, broker snapshot, two handoff repairs
    root.py / runtime.py  Workers composition and the NewsDatabasePort/stop mechanics
  storage/
    events.py / decisions.py  material facts/evidence and verdict/delivery ledgers
    feed.py / operations.py   bounded public reads and ingest/retention operations
    feed_sql.py / query_specs.py  production Feed SQL and its audited bound statements
    trade_projection.py       News-owned point-in-time handoff queried by App for Trading
    learning.py / root.py     learning persistence and the concrete repository composition
  search.py           pure News Feed search planning over the instrument identity catalogue
  eval/               provider-hits Deduper+Gate replay only

tracefold.trading
  signal_lane.py      the one deep module: `advance()`, Source -> Case -> Signal
  contracts.py        App-facing values plus the Source/Case/Decision vocabulary
  execution_contracts.py  engine-neutral Signal/OperatorIntent/Observation transport values
  admission.py        the one place a Source is admitted, and its closed reason set
  sources.py          one projected OI row -> one typed Source, or a named failure
  policy.py           `source_native_oi_smart_money_long_v4`: pure, long-only, frozen evidence
  market_context.py   the price window a Case is frozen against
  storage/            Case/Signal/current reads plus active execution transport/state behind one repository

tracefold.integrations
  provider and external-system adapters: OpenNews, RabbitMQ, Feishu, Telegram,
  public market data, and the profile-gated pinned Nautilus/Binance OI Runtime

tracefold.platform
  config models/loader, PostgreSQL/Alembic (`postgres/client.py`, `audit.py`, `migrations.py`), telemetry, paths,
  bounded resource primitives, docker host translation

tracefold.app
  Serve/Workers database composition, `repository_session.py`, HTTP, CLI,
  the Workers lifecycle root plus capability wiring (`app/workers/`), and the
  thin Nautilus process/database/probe composition root (`app/nautilus/`).
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
public News projection row becomes a Trading candidate, and it is the reason
Trading can consume News truth without a cross-domain import or a reach-through
read.

That seam is typed on both sides. Each business package declares the narrow
port it needs from the process — `NewsDatabasePort`, `QuoteDatabasePort`,
`ReactionDatabasePort`, `TradingDatabasePort`, each just a bounded read and a bounded transaction — and
`app/workers/wiring/database.py` implements them over `WorkerDatabase`, choosing
the lane, the deadline default and the error vocabulary. A business module never
names `worker_session`, `run_news` or `heavy_business`: no import edge was never
the same thing as no dependency. The handoff itself is two independent frozen
row contracts, News's `news_trade_projection_v14` and Trading's own candidate
input rows, translated field by field in `news_to_trading.py`, so a rename on
either side fails at the seam rather than inside a runner. Version 14 publishes
the deterministic OI ledger and nothing else: sixteen keys read from
`news_oi_signals` joined to the source Item for its `first_ingest_mode`, plus a
bounded bulk point-in-time instrument catalogue and the complete fixed-window OI
source universe. The triage verdict, the learning epoch, the six `jsonb`
equalities that re-proved the ledger against the verdict's copy of it and the
four News version literals are gone (#510): a frame reaches Trading without
passing through the editorial pipeline or the active learning arm, so a News
policy or Program identity move is no longer a Trading contract change. Ingest
provenance is published and never filtered here — the Signal lane refuses a
recovery frame by name; editorial News and liquidation have no Signal-lane
projection.
The live OI handoff reads that projection through the News cold adapter, closes
the News transaction, and then lets the Trading adapter open its own bounded
transactions. No callback receives both repositories and there is no
cross-context transaction.

`tracefold.app` decides how capabilities are assembled and run, never what a
business fact means. It reads business projections; it does not write business
tables. Every `news_*` / `trading_*` `INSERT`, `UPDATE` and `DELETE` lives in
the owning package's storage behind a named repository method.

| Surface | Semantic owner | App responsibility |
| --- | --- | --- |
| Canary identity / durable reason | News Release | transaction + runtime facts |
| Candidate artifact lineage | News Release runtime | image/model composition caller |
| DSPy endpoint binding | App | full owner |
| News → Trading projection | App mapper | field-by-field mapping |

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
public venue catalog/price adapters, and the isolated profile-gated Nautilus
Binance USD-M boundary. Nautilus is absent in disabled mode and is a required
identity-bound runtime in paper/live mode. No provider owns a durable queue. Expected
provider failures stay inside the owning bounded
loop; an unhandled child exception is deliberately a Workers-root failure and
the container restarts the single process.

SQL ownership follows the same boundary: News owns `news_*`; Trading owns
`trading_*`; platform owns Alembic and `workers_runtime`. News makes no cross-domain read: its single
read-only seam (`macro_module_current` as Analyst evidence) went with the
Analyst lane in #57, and the Macro tables themselves went in #68. The
architecture gate checks SQL table references against the generated current
schema and fails if its production SQL scan is empty. Production statements
live in the owning storage/PostgreSQL boundary or the small named App adapter
allowlist. Public and cross-context projections list columns explicitly.
High-risk runtime and query-audit paths import the same canonical statement
builder; the audit does not maintain a representative copy.
SQL functions follow the same ownership rule: a Trading trigger may
call only Trading/platform helpers, including its own canonical-JSON seal
helper, never a News-prefixed function.

## Transaction ownership

Application services and workers own transaction scope. Repository writes use
the supplied connection and never expose commit switches or open hidden
transactions.

A Worker callback receives only the repository capabilities required by its
bounded context, never the raw connection or the cross-capability repository
session. The Worker database adapter owns the true outer transaction: setup,
callback SQL, commit or rollback, and its one telemetry observation. A nested
repository transaction cannot shorten that scope or manufacture a second
transaction count.

Important atomic units are:

- one accepted OpenNews frame: NewsItem upsert with provenance union plus its
  Event assignment (new Event, bands, assets, or membership);
- one Triage verdict insert; one delivery begin or settle;
- one `TradeSignalV1` insert plus the guarded Case `RUNNING -> SIGNAL_EMITTED`
  transition;
- one Case insert plus its `CASE_CREATED` admission row.

Provider, model, filesystem, and network I/O occurs outside database
transactions. The same rule excludes Pydantic materialization, canonical JSON,
hashing, compression, large sorts/deep comparisons, and sleep/backoff. A
callback may execute SQL/transaction-scoped locks, map rows to primitives, and
immediately check rowcount/`RETURNING`/CAS. Payloads are prepared before the
callback and rich objects are materialized after it.

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

The loopback Workers readiness probe classifies a fatal News task as
`news_consumer_fatal`, `news_receiver_fatal`, `news_broker_unavailable`, or
`news_recovery_fatal` instead of flattening every child failure to
`runtime_failed`. Recoverable Receiver broker incidents, Recovery
provider/broker/database faults, and consumer-handler `BrokerUnavailable` /
`BrokerBackpressure` failures do not kill the process. Receiver and Recovery
keep durable incident rows and `/api/news/status` recovery state until the
history gap closes; a consumer handler returns the delivery through the same
counted broker settlement as `TransientError`, so RabbitMQ delays it and
dead-letters it after the shared budget. The status broker layer retains the
latest confirmed-publish failure code and timestamp from the running Workers
process. Unclassified handler and settlement failures still reach root
supervision. Recovery exposes `reason=recovery_pending` before an attempt and
`reason=recovery_transient` after a typed failed attempt; neither is a false
process-readiness failure.

## Workers task set

The Workers root TaskGroup contains exactly: `workers-probe` (loopback
health/readiness/metrics), the News consumer tasks when News is enabled
(`news-receiver`, `news-recovery`, `news-deduper`, `news-triage`,
`news-deliverer`, `news-janitor`), the bounded polling loops
(`news-instruments`, and with venues enabled `news-quotes`,
`news-reactions`), the one Signal loop when Trading is enabled
(`trading-signal-lane`), and `workers-control` (singleton
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
       -> Event new|member (dedupe_family window) -> Gate (provider-graded grounded_assets,
          registry macro/energy flags, PR-template veto, low-signal switch) -> preliminary
          storyline key; a stronger later member re-gates a suppressed Event
       -> publish event.<dedupe_family>.<queue_priority> for every admitted candidate,
          listing, OI or liquidation Event; unsupported_market is a named held
          Event and never reaches Triage; the suffix affects broker scheduling only
  -> q:news.triage [prefetch = news.triage.concurrency, handled concurrently] Triage:
       SemanticJudge.judge(TriageContext) -> current EventSemantics with nested
          NewsTaxonomyV1 + TradeRelevanceV1
       -> deterministic SemanticNormalizer -> ReaderCard.v2
       -> deterministic VerdictAssembler -> one atomic SemanticJudgment
          (verdict + editorial envelope + trace/runtime identities); normally two
       serial provider calls through explicit Predictor-local LMs/token caps
       (ReaderCard.v2 optionally has a dedicated primary endpoint); JSONAdapter
       may make one format fallback per Predictor (at most four calls per route),
       and primary failure restarts the full fallback route (at most eight calls) -> final storyline key
       from the verdict (written back) -> model-origin decide() or the structured
       lane's typed DecisionResult -> current verdict row
       (news_judgment_v2 marker, judgment origin/hash, model editorial when applicable,
       headline_zh, audience, exact runtime manifest,
       Program identity, per-Predictor execution/cost trace, preliminary + final status snapshots,
       named rule) -> publish verdict.push (an escalate rides the same routing key at AMQP priority 5)
  -> q:news.deliver [single-active-consumer] Deliverer: restart edit/delete reconciliation waits out
       News-lane admission before consuming -> provider prepare/preflight -> begin(sending)
       -> one configured-provider delivery attempt
       -> settle sent|terminal; crash between send and ack
       -> ambiguous_after_crash
  -> RabbitMQ 4.3 quorum delayed retry inside each business queue (no retry lane):
     TransientError is a counted return delayed 30 s and terminal after 3 total attempts,
     DeferError is an uncounted return delayed the same 30 s and never terminal;
     x:news.dlx -> q:news.dead (at-least-once) for decode/permanent/exhausted deliveries
  -> Janitor: bounded Event->Triage and push-Verdict->Delivery handoff repair,
              band expiry, 30/365-day Item retention,
              bounded learning-evidence retention on the one-slot heavy gate,
     broker snapshot (depth, ready/unacked, delayed, pending dead letters, byte share, policy match)
  -> Serve: /api/news/feed, /api/news/events/{event_id}, /api/news/status
```

Feed search is a Serve-only read concern. A pure News-owned planner classifies
each request as either exact asset identity or Event text, using the existing
instrument catalogue for canonical symbol, alias, venue-symbol, and bounded
pair resolution. The PostgreSQL feed repository then applies exactly one
predicate before counts and cursor pagination: the durable
`news_event_assets.symbol` ledger for asset identity, or the persisted Event
search document for text. Search creates no Event or business row, publishes no
broker message, and is absent from Judge, Gate, Delivery, Learning, and Trading;
those pipelines therefore have no search dependency or alternate truth.

Every broker delivery lives inside its consumer channel's `TaskGroup`, and one
typed domain outcome becomes exactly one AMQP settlement. Success acks. A
`DeferError` is `basic.nack(requeue=true)`, which RabbitMQ does not count as a
failed delivery, so a process that cannot admit a message may say so
indefinitely. A `TransientError` is `basic.reject(requeue=true)`, which
increments the broker's `x-delivery-count` and becomes terminal once the queue's
`delivery-limit` is spent. A decode error or `PermanentError` is
`basic.reject(requeue=false)`. A handler-side `BrokerUnavailable` or
`BrokerBackpressure` is the same counted `basic.reject(requeue=true)` as a
`TransientError`; it shares the one delivery budget and never fails Workers
root by itself. An unclassified handler exception or an ack/reject failure
settles nothing and fails the consumer; channel closure releases the delivery
and Workers root supervision turns readiness unhealthy. The application
therefore holds no retry counter, no timer and no republish path:
`BusMessage.attempt` is read from the broker's counter and is one greater than
it. This deliberately permits duplicates at the confirm-to-marker crash window
and relies on the existing stable message IDs and PostgreSQL idempotency keys to
converge.

Retry configuration is a RabbitMQ policy, generated from
`tracefold.news.broker_policy` into `docker/rabbitmq/definitions.json` and
imported by `tracefold news bus-policy apply` (a one-shot Compose service before
Workers starts). The application declares queue type, exchanges, bindings and
passive consumer access; it never repairs policy drift. Workers verifies the
effective policy at startup and refuses to consume when it does not match,
because a missing policy is not a degraded mode — it is immediate redelivery,
the quorum default delivery limit and at-most-once dead lettering. Terminal
dead lettering is `at-least-once`, so a `news.dead` that is unavailable or full
leaves the message held on its source queue (visible as `messages_dlx`) instead
of dropping it; RabbitMQ retries that transfer about every three minutes.

The Event and Verdict business tables are the two concrete handoff ledgers;
there is no generic outbox table. `published_at_ms IS NOT NULL` means confirmed,
a marker-null row at or below the 30-minute relevance ceiling is pending, and a
strictly older row is expired. Repair scans use a 15-second minimum age, the
30-minute maximum age, and a batch limit. They publish first and CAS the marker
after confirmation. Marker failure therefore causes a safe duplicate on the
next turn. Feed page, counts, filters, detail, and telemetry use the same
code-owned ceiling; expired rows remain auditable but are never shown as
pending or republished.

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
the separate frame ledger, and since #510 it is the whole of what the Trading
Signal lane reads. The Quote planner unions recent live OI symbols into
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
v6 is `sha256(identity_version, item_id, fact_id, event_kind)` for every route.
Migration `0336` deletes all pre-v6 Events; current Admission contains no
legacy collision or rekey branch.
`tracefold.news.events.titles`
extracts the first content block (skipping URL-only, label-only, `reply/quote:`
lines and pinned wire source labels/suffixes; exchange names and `@handles`
are subjects and stay — `@Krakenfx launches ...` keeps `Krakenfx`),
`tracefold.news.events.identity`
normalizes for comparison, `tracefold.news.events.tokens` + `minhash` produce the
band keys stored in `news_event_bands`. Admission prepares fact units, Gate output and MinHash outside PostgreSQL,
then one short transaction owns the Item/Event assignment. It commits before the evidence rows are loaded,
serialized and hashed; a compare-and-append transaction installs that prepared snapshot before any Event is
published to RabbitMQ. A crash between those steps is safe because the redelivered Item assignment is idempotent
and the snapshot append is content-addressed. Fingerprints of at most two tokens never
share an Event. `event_kind` fences every dedupe candidate lookup and namespaces
non-News Event identity. Current cross-Item exact/artifact/near joins require
the same source-contract reason. Pre-genesis Events were physically deleted;
the current writer has no repair, translation, or join path for them.

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
floor and the 12 h dedupe-family window. A hit joins the existing Event as an ordinary
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
Ordinary and deterministic recovery keep `admission=recovery`, while
unsupported contracts retain the named `unsupported_market_contract`
admission. Event identity v6 namespaces every FactUnit by route, so different
contracts for one provider record cannot merge by arrival order. There is no
source registry, queue, worker, ID-only routing, or pre-v6 identity bridge.
Migration `0336` deletes the historical classifications and every Event marked
by `0330`; a later provider redelivery creates a new current Event through the
single v6 writer.

Gate and storyline (`tracefold.news.events.gate`, `tracefold.news.events.storyline`) are pure
functions and keep no Strategy name table of their own: grounded assets are the
provider's grade B+/A/A+ coin tags plus any literal `$TICKER` cashtag (the
provider already resolved Bitcoin -> BTC, Home Depot -> HD); `CL`/`XYZ-CL` is
grounded only in energy context and a short stop-list drops English-word tags.
The Gate keeps no word list of its own either (#509 PR-2): the energy context
for `CL`, the `macro_lexicon` fact behind `asset_class=macro`, and the
queue-order subset are the `gate.energy_context` / `gate.macro` /
`gate.queue_high` flags on the storyline registry rows the text matched, read
through `events.gate.gate_lexicon_flags`. The v5 regexes that used to sit in
`gate.py` are deleted, so "energy" and "macro" mean the same thing to the Gate
and to the storyline key instead of two lists disagreeing about `iranian`, 沙特,
`barrels` and every central bank outside the Fed. Adding a word is a registry
row; `gate.queue_high` rows are a subset of the `gate.macro` ones.
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
phrases always; weak ones only without a grounded asset), or an unscored or
under-80 market frame (#126). The `news.gate` low-signal switch was
deleted in #504: it defaulted off, was never turned on, and produced zero
admissions in the whole retained history. A `listing` frame takes the
`listing_deterministic` admission, which is admitted and judged like a
candidate (#72). A member that
joins a suppressed Event with stronger evidence (score >= 80, an A/A+ grounded
tag, or a different source) re-gates it in place and it publishes once.
`queue_priority` is `high` (AMQP priority 5) for score >= 90, watchlist hits,
listing frames, or a registry hit flagged `gate.queue_high` (the rates topic and
the Fed). It is a broker scheduling hint only: it may be persisted and measured,
but cannot enter a Predictor, `decide()`, ReaderCard or reader-facing importance
UI.

The storyline key is composed from a **code-owned registry**, not from an
ordered pattern list (#509). `tracefold/news/events/storyline_registry.json`
(`news_storyline_registry_v1`) holds `conflict` / `actor` / `geo` / `topic`
entries, each with a Chinese label, literal aliases per script and the optional
Gate flags above; `latin` aliases match on word boundaries and every other
script matches as a substring, both over NFKC-normalized, case-folded text, and
the longest alias at a position wins. An alias belongs to exactly one entry, so matching yields a *set* of
positioned hits with no priority rule of its own. Structure is enforced at load:
unique aliases, no structural regex syntax (a literal `.` is fine and escaped —
`u.s.` needs it), already-normalized surface forms, `members` that name entries
which exist, and no aliases at all on a `conflict` row. An entry may set `standalone: false`, which means "match
me, but never be the key on your own": the hit still counts toward a conflict's
`members` and the entry still owns its aliases, but it is skipped when the
`actor`/`geo`/`topic` steps pick a winner. `us` is the case that needs it — a US
dateline is not a storyline for this reader, and letting `美国` / `washington` /
`u.s.` open a bucket put CPI, jobless claims and housing starts into one hourly
budget. The key is then composed by one fixed rank —
1. `asset:<SYM>` (a verdict primary the Gate grounded, scope not macro),
2. `conflict:<id>` (an active conflict whose `members` the text names — a
conflict owns no aliases of its own, so `hormuz`, `lebanon` and `mideast` are
`geo` rows that keep their coverage if the war is ever set inactive),
3. `actor:<id>`, 4. `geo:<id>`, 5. `topic:<id>`, 6. the model's own
symbol-shaped primary, 7. a grounded tag the text actually names (#100), 8.
`none` — with earliest mention as the tie-break inside a rank. Shuffling the
registry cannot move a key, and adding a storyline is one row plus one
assertion rather than a reordering of everything above it. The symbol shape
accepts one exchange suffix (`02015.HK`, `DTE.DE`). `none` replaces the old
`macro:<dedupe_family>` fallback: the dedupe family is a column on the Event
row, not a storyline, and policy v13's budget exempts `none` exactly.

The preliminary key (status bar and told retrieval before Triage) walks that
rank with its first step removed: registry first, then an A/A+ or cashtag
strong tag, then `none`. A provider tag names an *affected* asset until Triage
names a primary, so letting it win before Triage keyed "Iran attacked another
ship outside the Strait of Hormuz" as `asset:BTC` on the strength of a BTC tag,
and the told ledger's exact-storyline tier then answered a war card with
Bitcoin cards. A B+ tag never opens a preliminary storyline. The final key is
computed after Triage from the verdict's grounded primaries and scope — where
the asset is back on top, because the model has now named its subject against
the Gate's grounding — written back to `news_events`, and used by duplicate
comparison, operator grouping, and advisory locking. `STORYLINE_REGISTRY_SHA256` (the
registry file's bytes) is written into every verdict trace as an audit field.
It is deliberately not part of `policy_sha256` and opens no learning epoch:
maintaining the registry is data maintenance, not a policy change.

Registry changes are a hard cut with no data migration. `news_events.storyline_key`
keeps whatever string the row was written with, so historical rows stay their own
audit truth; nothing reads or translates the retired `theme:` / `macro:` formats.
The one visible consequence is bounded and one-directional: the budget counts
only delivered cards inside `storyline_budget_window_s` (1 h), so for the first
hour after a deploy that changes key formats those rows still carry the old
format, match no new key, and are not counted. The `recent_seen_rows` ledger the
similarity check reads is 4 h and is unaffected — it compares headlines, not
keys. The budget therefore errs toward releasing a card rather than withholding
one, for one hour, once.

Triage is a deep semantic-judgment **Module**. Its only hot-path generation
**Interface** is `SemanticJudge.judge(TriageContext) -> SemanticJudgment`; the
consumer does not know Predictor instructions, output schemas, model
routing, retry state, or artifact layout. That **Interface** lives at the
semantic-judgment **Seam**, and `RoutedSemanticJudge` is the production
**Adapter** there. It wraps one `NativeNewsProgram(dspy.Module)` and explicit
primary/fallback model slots. Recorded-arm replay is an evaluator-side composition seam,
not a second production generation Interface: the default evaluation path
re-executes the real arm-scoped native Program with every Predictor call
answered by `RecordedLM` from the run's content-addressed recordings. The Program
still enters through `judge(TriageContext)`. A missing recording makes the
evaluation `incomplete` without falling through to a live provider; a request
or identity mismatch is a miss, never live I/O. This shape gives
the hot-path caller **Leverage** (one call owns graph execution, validation,
fallback and audit) while keeping replay authority outside production
generation. Its **Depth** is the amount of behavior hidden behind the single
hot-path `judge()` method, not the number of internal Predictor calls.

Inside the Module, the fixed Program graph is
`EventSemantics -> deterministic SemanticNormalizer -> ReaderCard ->
deterministic VerdictAssembler`.
`EventSemantics` judges novelty, grounded entities, direction, scope,
magnitude and audience without writing reader copy, and emits one nested typed
`TradeRelevanceV1`: impact breadth, tradability, surprise, development delta,
at most four canonical channels/affected markets, and the sole model delivery
intent `reader_value` (`escalate|realtime|background|none`). It has no separate
model-authored `decision` or `actionable` field. Issue #117 also makes it emit
the four model-owned axes of `news_taxonomy_v1`: at most three pinned IPTC
subject qcodes, event family, change state and assertion status. The assembler
derives the fifth axis, source authority, only from the exact structured
reporting-source identity; strategy/provenance routing IDs confer no authority
and the model cannot claim it. Taxonomy is persisted in `EditorialEnvelope.v2` but is not an
input to Gate, `decide()`, ReaderCard, Delivery or Trading.
For `new_fact` and `progression`, the normalizer discards any stray non-negative
`restates` index and records the raw and normalized values on the originating
call trace; a real `restatement` still requires a valid told-ledger index. It
also preserves raw relevance arrays in trace, then de-duplicates and sorts them
by code-owned enum order. The normalizer makes no provider call.
`ReaderCard` receives the original evidence plus an explicit
`ReaderCardSemanticView` containing only assets, direction,
magnitude, novelty/restates, scope, channels and affected markets. It produces
only `headline_zh` and `why_zh`; it cannot read reader intent, tradability,
surprise, development delta, taxonomy or ToldContext. The assembler makes no model call:
it projects the exact presentation-only `TriageVerdict`. Final action is absent
from the Verdict and belongs to `DecisionResult`. Splitting semantic judgment from copy creates internal
per-Predictor feedback, demonstration, routing and future fine-tuning seams;
it does not add a second product stage or a second card.

The only executable generation is `news_semantic_program_v9`. Issue #193
hard-cuts the artifact to one canonical JSON document; issue #306 keeps that
shape and changes what the instructions *are*, each becoming the complete
prompt for its Predictor rather than a bounded advisory appended to a rendered
stack, with the code-owned seed text in `tracefold/news/program/seed.py`. Issue
#314 removes the last field that was not a written instruction, and issue #501
adds the third instruction: the artifact
holds `schema_version` `news_program_strategy_artifact_v1` and one instruction
per Predictor (`event_semantics`, `taxonomy`, `reader_card`), and
`program_sha256` is the canonical hash of exactly those
three values. The stable root is
`63e5b438f7419e02621e419f3a3ad9860dfcc54bf2eea86c0896bcc04ebb4c64`.
Issue #117 changes the EventSemantics instruction and typed output while
preserving the same two-Predictor graph and exact two-call common-success path.

**Program identity has two halves, and they have two authors.**
`program_sha256` addresses the write-set a human or GEPA may edit.
`envelope_sha256` — `compute_execution_identity()` in
`tracefold/news/program/identity.py` — addresses everything the code decides
about a model call: exact DSPy/LiteLLM/transitive-GEPA versions, public Signature
dumps, actual `dspy.JSONAdapter` renders for the schema, JSON-object and
prompt-only capability paths, both typed output contracts, model-visible input
shapes, the four model slots, retry/fallback/error transitions, route deadline,
2/4/8 physical-call ceilings, token ceilings, normalization/assembly surface
and the breaker. It is computed from those values rather than declared
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

Everything else the Program needs — the three-Predictor graph, the typed schemas,
the normalizer, the assembler, the model route and the execution budget — is
code, and `envelope_sha256` is computed over what that code renders. It is one
hash over one golden render rather than twenty-odd component hashes that the
same package generated and verified in the same process; that was a self-proof,
not an attestation, and neither it nor this replaces exact image/CI evidence.

DSPy's public `JSONAdapter` renders every request from the same Signature.
Application configuration resolves and declares each self-hosted endpoint's
effective capability; the audited Program seam consumes that declaration and
does not infer capabilities from the model name. Schema-capable endpoints receive a JSON Schema
constraint, JSON-object endpoints receive that response format, and prompt-only
endpoints receive neither. Field descriptions remain model-visible on every
path. An unknown outer DSPy envelope sibling is filtered by JSONAdapter, while
unknown or missing fields inside the business Pydantic output fail closed. A
truncated response is terminal for that Predictor and never enters the adapter's
single format fallback.

There is one instruction text per Predictor and no Tracefold-owned prompt
renderer (#306 Phase 2): DSPy's public adapter renders the surrounding
Signature and inputs while injecting that instruction unchanged. Until
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
deleted rather than left empty. `NativeNewsProgram` constructs exactly three
named `dspy.Predict` objects — `event_semantics`, `taxonomy`, `reader_card`, in
that execution order — with empty demos, so there is no path by which a demo
can reach a provider. The taxonomy Predictor (#501) classifies the Event under
`news_taxonomy_v1` from evidence and Gate facts alone; its seed is rendered
from the codebook constants in `tracefold/news/taxonomy.py`, and the label set
travels in the typed `ModelTaxonomyV1` output schema rather than in prose. The
three run sequentially on purpose: the production slot is one llama.cpp server
where concurrency saves no wall clock, and recording call indices are assigned
in append order, so a parallel call would make record/replay nondeterministic.

DSPy owns Predictor execution, request rendering, structured-output parsing and
LiteLLM provider I/O. Tracefold's thin `AuditedConfiguredLM(dspy.BaseLM)` calls
the public typed `LMRequest -> LMResponse` contract and owns only safe request
identity, usage/cost and one terminal disposition per physical call. It provides
sync execution for `dspy.GEPA` and genuine async execution for production; it
does not construct HTTP, messages or `response_format`. Provider retry and cache
are disabled. JSONAdapter may perform one format fallback per Predictor, so a
common route uses exactly three calls, one route is capped at six, and a
complete primary-to-fallback judgment is capped at twelve.

The factory owns route topology, slot roles, token ceilings, deadlines and
breaker policy. The concrete model bound to each slot has a separate
secret-free `configured_endpoint_model_v3` identity over provider, model,
endpoint fingerprint, temperature behavior, structured-output mode and normalized LM kwargs.
That boundary makes provider execution semantics auditable without pretending
an endpoint change rewrote the Program graph. Every endpoint can explicitly omit
temperature, choose JSON Schema, JSON-object, or prompt-only JSON, and add guarded
OpenAI-compatible body fields. There is no Kimi URL/model special case. Known
provider defaults remain narrow (the `qwen*:thinking` alias's prompt-JSON
envelope, DeepSeek's JSON-object mode), while local and other models are
configured through the same request block.

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
three disjoint projections from that one material truth:

- `recent_seen_rows`: every receipt aged at most 4 h, newest first, cap 128;
- `targeted_told_rows`: receipts older than 4 h and at most 48 h, with up to 8
  exact `(family, comparison_fingerprint)` matches followed by up to 24
  canonical-asset overlaps. Exact matches win when one Event qualifies twice;
- `similar_told_rows`: up to 32 receipts aged at most 24 h whose normalized
  `comparison_title` is closest to the current Event's by pg_trgm
  `similarity()`, excluding rows the two bands above already selected. The band
  is bounded by that K, not by delivery volume: at 38 sent cards an hour the
  128-row recent ledger covers under 4 h on 79% of judgments, while the median
  same-event repeat arrives 211 min after its first card (#491).

Only the recent projection reaches deterministic `decide().seen`; the targeted
and similar projections are semantic evidence for the Program and cannot extend
a policy throttle. Telemetry requests `include_targeted=False`. Production
initial load and stale refresh, plus CandidateEvaluator seed and in-run receipt
replay, use the same pure `build_reader_history` boundary/cap/dedup rules; the
similarity band is re-ranked in Python with `trigram_similarity`, the twin of
pg_trgm's algorithm, so SQL and replay agree on the order.

`ToldLedgerSnapshot.select` is a pure, deterministic, candidate-conditioned
selector — not a Retriever service, Protocol or Adapter — that ranks the union
against *this* Event and shows the Program at most 16 rows. Its tiers are
targeted exact fact, exact storyline, shared instrument (canonical symbol
sets), same-fact title similarity at or above 0.15 over the Deduper's
normalized `comparison_title`, then the rest. Inside every tier the order is
the raw trigram similarity desc, sent time newest-first, then the stable Event
identity, so the same history always produces the same selection whatever
order the database returned it in. Character bigrams remain the primitive for
`decide()`'s Chinese headline comparison; they are not used for
`comparison_title`, where 4.6% of random English pairs cross 0.25 on bigrams
and 0.10% on word trigrams.
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
entry carries index `i`, age, final storyline key, concrete comparison title,
instrument symbols, magnitude, direction, `headline_zh` and `why_zh`; the Event id, sent time,
selection tier, similarity, history scope and retrieval reason stay audit-only.
`READER_HISTORY_SHA256` binds source truth/windows/caps/projection;
`TOLD_SELECTOR_SHA256` binds selection and the unchanged model-visible schema;
their composite `NEWS_RETRIEVAL_SHA256` is the arm's `retrieval_sha256`, so
either source or selector behavior changes the Program and bundle identities.

The three Predictors do not read the same input. `EventSemantics` receives the
model-safe Event evidence, grounded Gate facts and selected told context;
`taxonomy` receives the evidence and Gate facts only, because reader history is
novelty evidence and a classifier that could read it could be taught to label
by what was already sent; `ReaderCard` receives the evidence plus only
`ReaderCardSemanticView`.
`queue_priority`, provider score, Gate macro lexicon, queue lag and the
watchlist are excluded from both model-visible schemas; the watchlist remains a
code-owned objective policy guard. The boundary is the schema rather than a
prompt reminder: the card input forbids ToldContext and extras, so a card
payload or recorded demo carrying history or delivery intent is rejected at the
renderer. Novelty is `EventSemantics`' job; a copy step that can re-read old
cards can re-interpret them. All three Predictor instructions are English. `ReaderCard` has exactly two Chinese text outputs:
`headline_zh` (the card header — a complete headline that keeps the decisive
fact, not a stub) and `why_zh` (the one card sentence adding what the headline
does not say). `headline_zh` is the only Verdict reader title, while `audience`
(crypto / us_equity / macro / none) is an EventSemantics field. The verdict also carries
`novelty`
(`new_fact` / `progression` / `restatement`, judged against the told ledger)
and `restates` (the ledger index a restatement points at; -1 otherwise) —
the reader-facing memory Triage has (issue #61): dedup is byte/word-level,
novelty is the semantic last line against the same fact told again from
another outlet or under another storyline key. Magnitude remains a Program
output. Policy v11 owns ordinary model action from one atomic `ScoredJudgment`;
OI, liquidation and degraded judgments each carry their own typed
`DecisionResult`. A *grounded* restatement is handled first, and
the existing stale-source and content-similarity protections remain after
action selection. The action section is exactly:

1. deterministic listing objective guard, unless the model marked the frame
   `reader_value=none` (policy v13, #523: the admission is the provider's
   `engine_type=listing` tag, so it also carries marketing, trading-competition
   and operations notices; a `background` listing frame still pushes);
2. grounded-watchlist objective guard;
3. `reader_value=escalate` and `realtime_eligible` -> `escalate`;
4. `reader_value=realtime` and `realtime_eligible` -> `push`;
5. `background|none` -> `drop`;
6. every other combination -> `trade_relevance_inconsistent`.

OI and liquidation never enter this list: their typed judgments own one
code-derived result, and degraded handling owns one objective baseline result.

`realtime_eligible` requires magnitude >= 2; tradability `direct` or
`second_order`; non-empty channels and affected markets; and either a
`state_change`, or `material_detail` that is direct/unscheduled/material versus
expectation. Queue priority, provider score, macro lexicon and `scope=macro`
cannot select or rescue an action. A Gate-admitted `listing_deterministic` frame
(`listing_exempt_from_duplicate`) skips the restatement drop and similarity
throttle only when the matched card names none of its instruments, compared as
symbol sets rather than headline text; a re-issued notice for the same
instrument is still withheld. `news.policy` exposes six v13 knobs:
`restatement_drop`, `similarity_max`, `stale_source_max_age_s`,
`listing_exempt_from_duplicate`, `storyline_budget_window_s` and
`storyline_budget_max`.

Policy v13 has **no reader-global quota** (no hourly, two-hour or four-hour
cap on what the reader receives, and no operator mute) but it does have a
**per-storyline content budget** (#504, which withdraws policy v7's "no
storyline quota" decision): an ordinary `push` whose final storyline key
already has `storyline_budget_max` (2) delivered cards inside
`storyline_budget_window_s` (3600 s) is withheld as
`storyline:<key>:budget`. The ledger is the same `recent_seen_rows` the
similarity check reads (sent first deliveries, newest first, `settled_at_ms`
and the card's final key). Three exemptions, all content: an `escalate` that
survived corroboration; a bullish/bearish reversal against the newest
*directional* delivered card on that key — policy v13 (#523) reads past
neutral, unclear and direction-less cards to find it, because one neutral card
landing on a key otherwise hid a real reversal behind it, while those cards
still count toward the budget; and the `none` key, which is not a storyline
(the registry matched nothing) and is neither counted nor budgeted. Either knob at
0 disables the budget. Two more v12 rules run before it: an eligible
`escalate` whose code-owned `editorial.taxonomy.source_authority` is
`unknown` and whose Event has a single member is downgraded to `push` as
`trade_relevance_escalate_uncorroborated` (grounded assets are not
corroboration); and an eligible realtime `single_name` verdict that names no
primary asset drops as `single_name_without_instrument` (it checks only that a
primary exists, never the instrument universe, so a Hong Kong ticker passes).
Once the semantic conditions pass and no content rule withholds the card, the
delivery harness executes the decision; it only enforces explicit
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

The Program factory owns the execution contract. A successful ordinary-News
primary route under `news_semantic_program_v9` normally makes three serial
provider calls: EventSemantics, taxonomy, then the exact current ReaderCard.
The in-process normalizer and assembler make no provider request. DSPy's
JSONAdapter may make one formatting fallback independently for any Predictor,
so a route makes at most six calls. Provider errors do not trigger that fallback,
and `max_tokens` truncation is terminal without another format call. The
code-owned 20-second deadline
applies to the whole route, not to each call. If primary still fails, fallback
restarts the full Program with its own route deadline; the complete chain
therefore makes at most eight visible provider attempts. Client-side
cache and hidden provider retries are disabled so the trace count equals real
attempts. Missing or invalid `novelty` fails closed; the genesis deleted
pre-current Program traces. OI and liquidation use their typed
deterministic judgments and bypass the Program. An ordinary Program failure is
degraded, not silent: code-owned listing or grounded-watchlist objectives may
use the wire headline; every other failure drops as
`degraded_no_objective_guard`, even when the provider score or queue priority is
high or the text contains macro words. Three consecutive retryable primary-route
failures open the default 60-second in-process primary-route circuit, which
skips directly to fallback. Separately, the consumer owns the durable
whole-chain `triage_circuit_open` incident; an output failure
(`news_program_output_truncated` when a Predictor hit `max_tokens`, or a
typed Program output error on schema mismatch) is degraded but never
counts toward the circuit and records the failing Predictor, finish reason,
tokens and error code. After the Program returns the consumer decides and
persists in one transaction under a per-storyline advisory lock on the final
key (`repository.lock_storyline`; `pg_advisory_xact_lock('NEWS', hashtext(key))`),
re-checking the delivered-ledger revision inside the lock so two same-key Events in flight
cannot both send the same fact (the lock raises the lane's 250 ms
`lock_timeout` for that transaction only). The wide sent ledger is always
loaded and materialized without a transaction; the locked primitive revision
detects any card landing while the model was thinking and discards that stale
material before a write. Only the *selected* told context decides whether the judgment itself
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
provider calls total, while each execution independently retains the eight-call,
two-route ceiling. All work from both executions remains in audit and cost
telemetry even when the first result is superseded or the second fails.
`news_verdicts` atomically stores the current marker and origin, presentation
Verdict JSON, model editorial envelope when origin is model, judgment hash,
exact `runtime_manifest_sha`,
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
`verdict_sha256`, model `editorial_sha256`, `judgment_sha256`, and the final
storyline key). Exact record/replay and every scoring path validate one typed
`ScoredJudgment`; they never reconstruct editorial state from an independent
verdict dict. Exact replay binds
the request to the resolved runtime model identity; a recording mismatch or
miss fails rather than falling through to live I/O.

There is no second product model stage: one Event persists one
SemanticJudgment and one card (issue #57), produced by the three internal serial
Predictors above.
That changes the normal provider-call cost from one to three and expands the
latency and failure surface; the benefit is future per-Predictor optimization,
not a claim that the initial Program is already more accurate. `escalate`
stays a `decide()` outcome — a high-importance
push that rides the same `verdict.push` routing key at AMQP priority 5 and
wears a ⚡ card header — and never triggers another Program execution. The retired
Analyst lane (`q:news.deep`, the `verdict.escalate`/`verdict.deep` routing
keys, the evidence bundle and its `verify_verdict()` gate, follow-up cards)
left `stage='deep'` verdicts and `kind='followup'` deliveries as historical
rows that are never written again. An old `news.deep` queue left on a broker is
reported as topology drift like any other unexpected name; the runtime does not
know it and never deletes it.

Delivery (`tracefold.news.delivery`, `consumers.DelivererConsumer`) renders the
reader contract (`news_delivery_card_v11`): the header is `headline_zh` (⚡ when
the decision is escalate), falling back only to the original Event title when
the current headline sanitizes to empty. The first body line is `why_zh`, and the
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
The target is not part of `news_delivery_card_v11`: only the Telegram Adapter uses it to link the displayed ticker
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
a blank line between them. The adapter recognizes the code-owned `方向待定` metadata label and admits only
tokens matching the bounded ticker grammar into those asset blocks, so direction, magnitude, or arbitrary card
text cannot become a target. Impact and polarity share one direction row, such as `🧭 方向 明显利空`, while
novelty is a badge immediately below the title (`🆕 新事实` or `🔄 新进展`). A progression names the previous
headline immediately only when the optional post-delivery verifier is unavailable and an exact-fact retrieval or
a stored title-similarity score of at least `0.50` supports it. With the verifier configured, the first message
shows a one-line indented `关联确认中` child block without naming a parent. After the send receipt is durable,
one bounded structured Predictor compares the current Event with at most eight selected told-ledger candidates:
the common path is one physical call and JSONAdapter may spend one format fallback. It confirms only the same
concrete subject and event chain with a material new action, result, number, confirmation, reversal, or state
change; a shared topic, sector, ticker, country, or storyline bucket is insufficient. Price reads and this review
run concurrently and settle through one edit of the original Telegram message. A confirmation replaces the
pending child block with a nested `✅ 已确认关联` block that links the stored prior Telegram receipt as
`此前：<parent headline>` and shows the age calculated from the two actual push timestamps. Rejection,
unavailability, or a model confirmation without a sent, undeleted, same-target parent receipt changes the edited
message to `🆕 新事实` and removes the complete association child block; it never retains `🔄 新进展`,
exposes a failed-review explanation to readers, invents an unlinked parent, or derives age from candidate event
time. The exact
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
decision plane grows beside `decide()`. Policy v13 has
no reader-global quota; its only volume rule is the per-storyline content
budget (`storyline:<key>:budget`, #504) with the escalate, reversal and
`none` exemptions and the `throttled_by_key` observation described above.

Incidents and recovery: WSS transport/auth/protocol/idle failures, broker
backpressure/unavailability, and Triage circuit opens are rows in
`news_opennews_incidents`. The Receiver never uses process memory to decide
whether a durable broker incident exists: every classified broker failure
opens it and updates ingest state in one transaction; every confirmed live
publish unconditionally closes matching open broker incidents and updates
ingest state in one transaction. An actual close wakes Recovery. Reconnect does
the same for transport incidents, so a process restart cannot strand an older
row.

A Receiver that is killed rather than stopped reports nothing at all: the
Workers root closes business admission before it cancels the task, and a signal
kill never reaches application code. The next Receiver records that gap instead.
`news_ingest_state.connected` is written true only by a live connection and
false only by a reported disconnect, so finding it still true at startup means
the previous process died while connected; the successor opens a
`process_outage` interval starting at that row's last write — the last moment
the old process is known to have been running, refreshed at least once a minute
by the Janitor's snapshot — and its own connection closes it. Opening is
idempotent on the one-open-row-per-cause index, and the start is clamped to the
successor's own clock so a predecessor whose clock ran ahead cannot open an
interval that closes before it began.

Recovery scans on startup, explicit wakeup, and a 300-second fallback. It pages
the official Strategy history for closed pending intervals and publishes
stable-ID `raw.recovery.*` frames under one turn-wide 30-second / 60-provider-
call / 1,000-message budget. Typed provider, broker, or database faults leave
the incident pending; provider/broker and known-incident database errors record
their bounded code and use bounded in-process backoff; budget exhaustion also stays
pending and schedules another turn. Provider calls and confirmed broker
publishes each inherit the remaining turn deadline, so neither can overrun the
wall budget. An empty current Strategy list is a retryable configuration state,
not proof that historical data never existed. Only explicit no-history or
retention exhaustion may write unavailable/partial. Unknown exceptions leave
the runner and fail Workers. Recovery Admission persists facts and evidence
but is defensively barred from Triage and Delivery. Dead letters are
operator-visible through `tracefold news dlq inspect|replay|purge`.

News storage is split by meaning, not by a fragile table count. Material
evidence and current Event state remain in the ingestion/Event tables;
judgment, delivery and exact evidence snapshots are immutable observations;
reviews and learning artifacts form the cold learning plane. Read queries are
registered in `tracefold.news.storage.query_specs` for the query audit.

Learning loop (#112): `ReviewDesk` draws deterministic, version-homogeneous
tasks from sent, model-drop, Gate-suppressed, throttled, delivery-failed,
high-reaction and random strata. The operator sees the exact historical
evidence, verdict, policy trace and real sent receipt, then records a
multi-dimensional rubric (`should_push`, factuality, evidence sufficiency,
entity grounding, novelty, direction, magnitude, copy value, timeliness and
first bad owner). Current `news_review_v6` retains exact gold for the seven
TradeRelevance fields from v4 and adds exact taxonomy Gold plus draft/reviewer
provenance. One explicit ReviewDesk acceptance by an owner-authorized reviewer is sufficient taxonomy Gold; no taxonomy-specific
second reviewer or adjudication is required. A failed scored dimension without expected
gold is not scored. A judgment becomes training/eval truth only after a separate
acceptance receipt. An important fact missing before Event creation enters as
an immutable external-miss snapshot, rather than a fake Event id.

Issue #453 reuses that same accepted review and frozen development Dataset for
taxonomy optimization. `dataset.py` projects accepted four-axis taxonomy into
the existing episode, so the episode projection root covers Gold. The pure
`taxonomy_metric.py` helper compares it with Stable or candidate taxonomy and
the existing metric folds the result into `semantics_novelty`. Code-owned
source authority stays outside model target, score and feedback. No
taxonomy-specific Dataset, table, shadow Program,
registration, evaluator, or release lifecycle exists.

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
current-bundle / `news_review_v6` evidence, compares Stable with exactly one registered Prompt candidate,
and publishes release evidence. Validation/holdout replay
both arms sequentially because each arm's would-reach-reader ledger changes
later decisions. Predictor requests/responses are recorded per call and
content-addressed — retained as auditable forensic evidence — and the default
replay path answers each arm from those recordings, surfacing a request or
identity miss as an incomplete evaluation rather than falling through to live
I/O. A frozen dataset accepts Event cases only
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
Workers loop. `news learning run` reads a frozen development corpus once
and then holds task and reflection model endpoints plus a typed budget — no DB write, broker,
delivery, canary or promotion credential — and can emit only a bounded
`PromptPatchV1`. The patch contract carries all three Predictor instructions, but #501 permits only the
taxonomy instruction to change and requires EventSemantics and ReaderCard to remain byte-identical. The
graph, output schemas, execution budget, model slots and policy are code, covered by `envelope_sha256`,
and outside the write set.
The optimizer calls public `dspy.GEPA` exactly once with `instruction_proposer=None`,
`add_format_failure_as_feedback=False` (dspy 3.3.1 renders that feedback with a hard-coded ChatAdapter
that describes a request shape this JSONAdapter program never sends), and the existing
`NativeNewsProgram(base_strategy).taxonomy` Predict inside one learning-only wrapper as its student
with `num_threads=1`. The budget is DSPy's own: `--auto light|medium|heavy` or an explicit
`--max-metric-calls`, exactly one, passed through unchanged, with the resolved metric-call count recorded
in the optimizer receipt; there is no floor or preflight of Tracefold's own. Its code-owned six-example
reflection minibatch reuses GEPA's native knob: it is wider than the tie-prone default of three. The
reflection model is an operating requirement — strong, ≥128K context — recorded by the run receipt, not
checked by code. The wrapper converts an audited task-output truncation or a typed `ModelTaxonomyV1`
validation failure into one failed Prediction so DSPy keeps the trace batch aligned (#478); DSPy 3.3.1
otherwise re-raises the truncation or drops the invalid example, leaving GEPA indexing a shorter batch.
Every such failure scores the native `failure_score` of `0.0`: the v3 sentinel of `-(train_count + 1)`
dominated the Pareto front and left candidate zero with an aggregate below every real candidate. The
wrapper does not retry, parse, evaluate or select. Reflection truncation, transport/provider failure and
budget refusal remain run-terminal.
Admission is GEPA's own answer (#501 D4): the candidate at `best_idx` advances when its selection
aggregate is strictly above candidate zero's and its instruction is valid and bounded; otherwise the run
is `NO_OP`. There is no per-control replay, per-objective check or instruction growth budget at
selection — those are what the offline and holdout release gates already decide, and re-deciding them on
the selection set only made `ADVANCE` unreachable (the #456 rule required every Stable-correct control to
replay at exactly `1.0`, which the seed itself did not satisfy). A candidate that overfit the selection
set is caught by offline evaluation, at the cost of one evaluation; that is DSPy's standard division of
labour between selection and holdout.
Frozen examples carry only the rendered taxonomy evidence and accepted taxonomy Gold; the deterministic
metric returns the mean of subject set-F1 and exact family/state/assertion axes, and its feedback quotes
the codebook definition of the expected and predicted labels plus any precedence rule written for that
confusion. There is no component selector,
ReaderCard rollout, production composite, semantic judge, direct GEPA import, private DSPy API or
second evaluator. GEPA cannot accept a review,
register/deploy its output, move a stable pointer, or promote a candidate.
#202 deleted the container platform that used to surround it — image, launcher,
metered proxy sidecar, sandbox policy, tariff, build attestation — because it
proved *where* two strings came from, which was never what made them safe.
Automated optimizers may propose a Program candidate but cannot modify the
reader contract, rubric, accepted reviews, holdout, thresholds, stable bundle,
or production assignment.

One optimization produces one `news_prompt_candidate_v2`, and only when it ends
in `ADVANCE`. Every terminal state — `NO_OP`, `REJECTED`, `ADVANCE` — also
writes a complete `news_optimization_run_report_v4`, so a run that spent a
budget and shipped nothing is still readable. Issue #193 had already collapsed
the compile's evidence into a single `CompileRecordV1`; #202 removed the compile
itself, and with it the record, the sealed input bundle, the sidecar's per-call
ledger, the `CompilerBuildAttestation` and the tariff. Those documents proved
*where* two instructions were produced. Nothing downstream ever needed that:
public `dspy.GEPA` returns native Predict candidates, and `run_gepa` extracts
only GEPA's best candidate while refusing demos or any extra Predictor, then copies EventSemantics and
ReaderCard unchanged into the three-string patch contract. Rows written
under the old chain stay in `news_learning_artifacts`
as append-only audit and no longer parse, so they cannot be re-armed.

The embedded optimization usage v3 keeps physical task/reflection usage exact. If GEPA terminates before
returning its public result, the report records `metric_calls=null`; it does not turn completed-but-unknown
metric evaluations into zero or reconstruct a count from private optimizer state.

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

Each of the two optimizer roles — task and reflection (32k tokens) — is
one `ModelExecutionIdentity` carrying the complete secret-free execution
contract, in place of the `endpoint_sha256 -> model_sha256 -> binding_sha256`
chain and the role binding above it. Its one surviving digest is
`endpoint_fingerprint`, because the endpoint URL names the host a credential is
presented to and therefore may not be stored. Both are required before a budget is spent. The separate
diagnostic baseline and release evaluator retain their semantic judge; it is not an optimizer endpoint,
budget field, identity or receipt.

What bounds the offline job now is what it holds, not what surrounds it: a
frozen corpus read once through the shared application login, two model
endpoints, and a typed in-process budget whose per-call ceiling is also the rate
an unpriced call is charged at. It has no database writer call path, broker,
delivery, canary, or promotion authority; role separation is not that boundary.
If dynamic code generation ever becomes a candidate again, the sandbox threat
model is rebuilt with it under a new Issue rather than kept warm for it.

What GEPA is allowed to optimize is decided once, by `learning/objective.py`,
and every plane that needs the answer rebuilds the same plan from the same
frozen episodes: `news learning readiness`, `run_gepa` through the one offline
entry point, and `CandidateEvaluator` when it
re-projects a registered candidate's corpus. Under #501 a case is **included** when accepted four-axis
taxonomy Gold is valid and recorded Stable taxonomy exists; an owner column, a derived owner or a
taxonomy review dimension grants no optimizer authority and takes none away. Everything else is
an **excluded diagnostic** and never enters a reflective minibatch. `run_gepa` splits the included cases
after Objective Plan v4 elects one deterministic representative per connected fact cluster. Shadowed media
members remain frozen audit facts but add no optimizer weight.
The candidate's `optimization_objective_summary.v4` binds the plan schema and
representative ids/count/root; registration re-derives that population and refuses claims that do not
carry the current identity, while leaving their artifact bytes intact.
`news learning readiness --development SHA` publishes the plan with zero model
calls, and `run` rebuilds it and refuses on the same conditions before any
endpoint is touched. Its v4 report separately publishes `objective.compilable` and
`development_profile.ready`; it has no ambiguous top-level outcome. The population is every case with
valid accepted Gold and a replayable Stable answer (#501 D9) — `included`, with `stable_exact` recorded
as a diagnostic — because the #456 target/control rule (explicit-owner mismatches versus Stable-exact
controls) measured which batch drafted the label rather than the Program. #501 also deleted the
60/60 and 30/30 target/control floors and the 50-cluster calibration gate: GEPA needs Gold-bearing
samples, not a quota of Stable mistakes, and a small corpus ends in `NO_OP` on its own. Inter-drafter κ
is still computed at freeze time over every dual-labelled cluster and published beside the corpus
(`counts.calibration`, `dataset_calibration_receipt.v2`); it is reported, never gated, because the
holdout is the gate.

Whether a development corpus is *enough* is decided by coverage, never by the
calendar (#259). The release profile asks for independent connected fact
clusters by role — boundary, retention, negative, at least one safety — plus the
strata both split halves must carry, and the Objective Plan asks for Gold-bearing
clusters and a cluster-disjoint, time-ordered
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

`news learning run` (#453, #501) is the only way to generate a candidate: one command
writes zero-call readiness and invokes stock GEPA exactly once over the same
frozen corpus, exiting `0` only on `ADVANCE`. Candidate zero is the sole optimization baseline, and
GEPA's own `best_idx` is the admitted candidate when it is strictly above candidate zero with a valid
instruction. The only later baseline is
Stable on accepted examples that did not exist when the candidate was made,
produced by the release plane's holdout stage.

Metric v8 (`tracefold.news.production_action_trade_relevance_v8`) uses the one
version-bound production-action projection shared by baseline, failure-cluster
selection and CandidateEvaluator. Its candidate scalar weights 45% final
production action, 35% exact TradeRelevance dimensions, 10% semantics/novelty,
10% ReaderCard reviewer anchors and 10% the deterministic ReaderCard copy lint,
normalized over the components a case actually carries, with component
denominators/effective weight mass/gold coverage published. Listing/telemetry
are outside the relevance denominator; watchlist guard cases are policy evidence
and do not send action feedback to GEPA. The four model-owned taxonomy axes are
one subscore of the existing semantics/novelty component: subject-code set F1
plus exact event family, change state and assertion status. `source_authority`
is code-derived and absent from target, score and feedback.

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
retired: ReviewDesk does not render them, taxonomy has no price or delivery authority,
and they consumed the 30-day read budget without producing release evidence.
Coverage may span 30 days, while the operator discovery queue is explicitly
bounded to the most recent seven days; the market view rejects a larger window,
while the separate evidence-coverage view retains 30 days.

`tracefold news replay <hits.json>` remains the deterministic
provider-hits Deduper+Gate regression; `tracefold news why <event_id>` prints a
single production chain. The retired single-label evaluator, policy-only
corpus gate, label-copy UI and `news_event_labels` table no longer exist.

Before the #449 baseline squash, Git history began at
`20260818_0275_baseline` and carried the following hard-cut chronology. These
files and their role bootstrap are recovery evidence only; they are not part of
the current Alembic tree. `20260818_0276_review_49_hard_cut`
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
`program_factory_id` to nullable, and lets a deployment open its own epoch. The
append-only trigger remains the durable rewrite boundary. No migration appends
an epoch row after this one.

`20260827_0315` persists the #288 exact source-contract route and Event-kind
hard cut, trips open canary activations, and records the factory-v6 to
factory-v7 migration receipt. It neither rewrites nor appends the `program_v7`
epoch row: all earlier rows and bundles remain immutable audit history. Because
current acceptance is bound to the exact factory and Program bundle, prior
factory evidence is audit-only and the factory-v7 cohort starts with zero
eligible evidence.
`20260828_0316` and `20260828_0317` build the retired Capital/Intent execution
owner; [ADR 0002](adr/0002-trading-execution-owner-hard-cuts.md) records it.
`20260828_0318` starts the single-instruction Program-v8 evidence epoch.
`20260828_0319` starts the endpoint-capable-envelope Program-v9 evidence epoch.
`20260828_0320` adds the News catalogue's immutable listing-validity events, and
the retired owner's capability and replay ledgers with them.
`20260828_0321` lets the running deployment open its computed-identity evidence epoch.
`20260828_0322` adds the durable desired/edited/ambiguous lifecycle for in-place News delivery edits.
`20260828_0323` adds the durable deleting/deleted/ambiguous lifecycle and five-venue evidence for confirmed
untradeable single-name Telegram messages.
`20260828_0324` closes the PostgreSQL `NULL`-truth gap in both delivery lifecycle shape constraints and rejects
any preexisting partial edit or delete intent before replacing those constraints; #325 owns its evidence-preserving
repair and roll-forward plan.
`20260829_0325` replaces the retired Trading runner cluster with the single
deterministic lane; `0326`, `0327` and `0329` are the retired execution owner's
own cutovers, recorded in
[ADR 0002](adr/0002-trading-execution-owner-hard-cuts.md).
`20260829_0328` then restricts Review v5 taxonomy Gold to ordinary News and trips open canaries for the
Program v7/taxonomy-v1 epoch hard cut.
`20260831_0340` is the current-schema Alembic baseline and single root. The
operator-authorized #449 hard cut first advanced the supported live
database to the exact old terminal revision, then reused that identity with
`down_revision = None`. A fresh PostgreSQL 18 database creates the complete
current schema in one step; an already-stamped database does not replay the
baseline or rewrite business data. Earlier revisions and role bootstrap logic
live only in Git history and the pre-cut image. `20260901_0341` performs the
#433-C Signal hard cut after the baseline; additive `20260901_0342` adds the
append-only Trading notification delivery ledger; additive `20260901_0343` adds
the current execution Runtime projection and bounded recovery indexes; and
destructive `20260901_0344` restates the `news_verdicts` judgment CHECK for the
News open-interest push cut; `20260901_0345` removes the stale execution Runtime
constraint that rejected a safe transient pairing of independently observed
flatness and unexpected exposure, with readiness still failing closed on
unexpected exposure; additive `20260901_0346` lets a notification receipt
outlive its provider's message id and carry a four-hour result; and destructive
`20260901_0347` drops the twenty-two execution tables
`0341` had made read-only, together with the thirteen functions only their
triggers, defaults and CHECKs called; `20260902_0348` hard-cuts Runtime
readiness and adds the profile-keyed current control projection; and additive
`20260902_0349` adds the bounded Runtime-owned current account read
projection; destructive `20260903_0359` drops the `0342` notification delivery
ledger and the partial observation index that fed it, neither of which any
production writer ever reached; additive `20260902_0350` pins the
`pg_trgm` extension and admits the `title_similarity` retrieval reason into the
`news_verdicts` told trace CHECK for the reader-history title-similarity band
(#491); additive `20260902_0351` opens the judgment CHECK to program v9 and
admits blind review drafts (#501); additive `20260903_0352` opens the same
CHECK's model, OI and degraded branches to `news_triage_policy_v12` (#504); and
`20260903_0353` replaces
`trading_execution_string_array_valid`'s default-collation ordering with
`COLLATE "C"`, so the observation CHECK orders `native_identity_references` the
way `ExecutionObservationV1` sorts them and a fill that mixes upper-case Binance
identities with lower-case `tf...` client order ids is no longer rejected;
additive `20260903_0354` publishes each Runtime's executable `market_key`
catalogue on `trading_execution_runtime_state.routes`;
destructive `20260903_0355` drops the six dead `trading_cases`
columns and narrows the Case state and admission status/stage CHECKs to the
values a writer can reach, refusing to run while a stored row still holds a
retired one; destructive `20260903_0356` makes `account_slot` the execution
identity and drops the profile activation ledger with the Decision Plane
heartbeat (#520 PR-A); destructive `20260903_0357`
makes the contract the only validator — every JSON-shape CHECK and the four
`trading_*` functions behind them are gone, along with the unread
`payload_digest` / `alpha_contract_sha256` / `evidence_sha256` digests, the
`confirmation_identity` column and the five readiness booleans (#520 PR-C); and
additive `20260903_0358`, the current single head, opens the judgment CHECK's
model, OI and degraded branches to `news_triage_policy_v13` (#523).

Every new schema change is again a normal linear, immutable, forward-only
revision after the baseline. Exact-image replacement requires source, image,
and live database to share the current head. Downgrade of an irreversible cut
is a verified backup restore. See [Migrations](MIGRATIONS.md) for the authoring
and evidence contract.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.


## Trading core

`tracefold.trading` is the disabled-by-default Alpha/Signal capability. It is
one deep module with one business action:

```python
await signal_lane.advance()
```

The caller — always `tracefold.app` — owns polling, the stop event and process
lifecycle, and knows none of the admission order, underlying de-duplication,
bar cutoff, manifest construction, Case lease, Signal identity, or transaction
boundaries.

The Nautilus OI Runtime is the only execution owner. Three owners preceded it
and each was deleted rather than kept alongside;
[ADR 0002](adr/0002-trading-execution-owner-hard-cuts.md) records what they were
and which words an archived row may still carry.

### The domain language

One word, one meaning, shared by the writer and every read surface:

| Term | Meaning |
| --- | --- |
| **Source** | a persisted, citable provider-native OI market fact |
| **Admission** | the durable Gate answer taken *before* a Case exists |
| **Case** | a frozen candidate that passed live Admission and may run the Alpha policy |
| **Decision Plane** | process lifecycle: `DISABLED`, `STARTING`, `RUNNING`, or `FAULTED` |
| **Policy decision** | pure `long`, `no_trade`, or `not_run`; never execution permission |
| **NO_TRADE** | the policy ran to completion and declined |
| **BLOCKED** | a system fact or invariant stopped the decision completing safely |
| **SIGNAL_EMITTED** | the Case and exactly one engine-neutral Signal committed atomically |
| **Signal** | a finite-TTL Alpha conclusion with evidence identity; never an order or permission |
| **Command** | one `OperatorIntentV1`; recorded by an operator, never interpreted by the recorder |
| **Execution Observation** | append-only Runtime/Binance fact; never a second OMS |

`CaseState` is the whole `trading_cases.state` vocabulary — `PENDING`,
`RUNNING`, `NO_TRADE`, `SIGNAL_EMITTED`, `BLOCKED` — and
`trading_cases_state_check` admits exactly it. Admission's four statuses and
five stages are closed the same way, by
`trading_candidate_gate_status_check` and `trading_candidate_gate_stage_check`.
Each vocabulary has one owner: a narrowed CHECK, not a CHECK and a trigger
saying the same thing twice. Shape is owned the same way, one level up: the
Pydantic contract that produces a durable execution fact is the only thing that
validates its JSON. `20260903_0357` deleted the CHECKs that restated those rules
in SQL, because two statements of one rule can disagree and did — the collation
incident of 2026-09-02 (#510 PR-1). The database keeps what only it can know:
primary keys, foreign keys, NOT NULL, the enumerated value sets, the identity
regexes, the clock inequalities and the append-only triggers.

### The one live path

```text
bounded OI projection snapshot
  -> normalize source
  -> closed source-venue partition
  -> deterministic admission
  -> fetch closed provider-native bars (outside every transaction)
  -> one transaction: Case + CASE_CREATED admission row
  -> pure deterministic OI policy
  -> NO_TRADE on Case
     or one transaction: Case=SIGNAL_EMITTED + TradeSignalV1
  -> Runtime reads the unresolved Signal, sizes it, enters, protects, exits
  -> append-only Observations plus one current Runtime projection
```

**Editorial News does not trigger automatic Trading.** News stays a sibling
bounded context; the App seam maps one public OI projection into the lane and
nothing else. Neither package imports the other or reads the other's tables,
and RabbitMQ remains News-only. There is no strategy registry, no venue
priority, no cross-venue fallback, no execution exchange, queue, outbox, Redis,
second database, or in-memory correctness ledger.

### Admission

Admission owns source contract, supported source venue, freshness, the
liquidity floor, market context, same-underlying Case identity,
executable-market presence, and the per-turn freeze bound. It records one
`(source_key, gate_version, gate_config_digest)` decision in
`trading_candidate_gate_decisions` — the admission ledger — so the console can
explain why a Source did not become a Case. The scan re-reads a bounded overlap
and relies on durable source identity rather than an in-memory cursor. A
terminal row keeps its status, stage, reason, evidence and case link; only the
two evaluation counters move, so "the scanner re-read this source 40 times" and
"the answer changed" stay distinguishable.

Source venue chooses only the public bars used as evidence. It does not select
an execution route, and it is the whole of the evidence for Hyperliquid's
`hl.xyz` builder DEX. Nothing in Admission reads an upstream judge, Program,
policy or learning cohort.

Admission selects no venue, instrument, account or route, but it does read one
Runtime fact: the `market_key` catalogue each configured Runtime publishes on
`trading_execution_runtime_state.routes`. A market absent from every published
catalogue is `REJECTED/instrument_unmapped` at the eligibility stage, because
the lane freezes one Case per turn and spending it on a market no Runtime can
reach both wastes the turn and defers a market that can. When no catalogue is
published at all — execution disabled, or no Runtime started yet — there is
nothing to read and admission applies no routability rule.

Changing a threshold does not rewrite history: `gate_config_digest` is half the
key, so an edit starts a new row and the old one records what the old rule
decided. Retention is 90 days, purged in bounded batches by the same turn.

### The Case and its manifest

Cases freeze source identity, cutoff, the price window, a venue-neutral
`market_key`, and the exact policy identity, version, typed config and config
digest. `policy_checks` records every condition the policy executed —
threshold, operator, measured value, pass/fail — so a Case decided a week ago is
explained without today's configuration.

The `trading_manifest_v11` manifest names exactly one `primary_trigger`, one
`policy_id` / `policy_version` / exact typed `policy_config` /
`policy_config_digest`, a venue-neutral `market_key`, and a point-in-time
`contexts` object. `contexts.market` is the sole market truth and `contexts.oi`
is the provider's measured frame — the four numbers, its two clocks, its venue,
its source Item and the provider's own measurement contract — with no upstream
judgment, Program, policy or cohort identity on it. A restart re-runs the exact
policy identity the Case froze rather than comparing the Case with today's
thresholds; a Case naming a retired identity is `BLOCKED /
policy_identity_retired` and is never re-decided, and a Case frozen under an
earlier manifest version is `BLOCKED / manifest_invalid` on its next claim.

**Trigger and context are different types.** A trigger is the one persisted
fact that starts an evaluation and fixes its cutoff. Context may enrich that
evaluation only when it existed no later than the cutoff. Notification `sent`
is notification transport success, not a trigger; Alpha must not depend on a
notification channel being reachable. News push is the only such channel that
exists; #528 deleted the Trading one, which had never been enabled.

Production runs exactly one pure policy,
`source_native_oi_smart_money_long_v4`: deterministic, long-only, code-owned
thresholds, answering `long` or `no_trade` only. It cannot express a
permission, an execution environment or a venue. `long` produces a
`TradeSignalV1`; the Signal grants no execution authority.

### Runtime ownership

The Nautilus OI Runtime owns the account, the risk numbers, orders, protection,
exits and recovery. `tracefold.trading` owns none of them and holds no order
state. Paper and live run the same Strategy / Risk / OMS / reconciliation code
and differ only by account slot, credential namespace and Binance environment.

Canonical up/deploy/status derives the execution Compose profile from operator
config: disabled stops Nautilus, while paper or live starts exactly one Binance
USD-M TradingNode. **`account_slot` plus `mode` is the whole execution
identity.** A session advisory lock owns the account slot and is the only thing
that decides who may execute for it; `runtime_release`, `config_sha256`,
`image_digest` and `credential_fingerprint` ride on the durable projection and
on every Observation as evidence of what is running, never as a gate on whether
it may run. A restart after a code, image or risk-config change is a restart:
the Runtime does not need a new name, does not require a flat account, and does
not reset control state. `mode: disabled` is the switch that means "do not
trade".

**Private account truth has one owner.** The App root disables Nautilus's
duplicate startup reconciliation and requires one complete Binance position +
regular-order + Algo-order report before activation. It refreshes that report
every `reconciliation_interval_seconds`, and wakes immediately for
unknown order outcomes, protection ambiguity, unexpected exposure and pending
flatten. It is a proof, not a precondition: a Runtime starts while the account
holds a position and rebuilds ownership from durable facts. Only a successfully loaded empty triple can assert `account_flat=true`;
a provider, parse, account-scope or Cache projection error escapes without
advancing the reconciliation clock. Nautilus keeps its native in-flight,
missing-open-order and position consistency loops as ExecutionEngine mechanics,
not as flat authority. Every Nautilus 1.231 Binance private attribute the proof
needs is isolated in `nautilus_1231_binance_compat.py`; no reconciliation or
Strategy module reaches a private adapter member directly.

**One number owns account freshness.** `reconciliation_interval_seconds` is the
private-reconciliation period, and `account_stale_after_ns` and
`reconciliation_stale_after_ns` are two and three times it.
`market_stale_after_seconds` is its own operator number because the quote
stream, not the private scan, decides it. Day-start equity and intraday equity
are one function, `account_equity_usd` — USDT balance plus unrealized PnL at
current marks — so `daily_loss_limit` compares one definition with itself.

**The risk numbers are operator configuration.** `trading.execution.risk`
carries `risk_fraction_per_trade`, `max_risk_per_trade_usd`,
`max_total_risk_usd`, `max_positions`, `max_leverage`, `max_daily_loss_usd`,
`stop_distance_bps`, `reconciliation_interval_seconds` and
`market_stale_after_seconds`, each with a pydantic bound that states why it is
where it is. `tracefold config` prints them and they are inside `config_sha256`,
which lands on the durable projection and on the Runtime's start Observation, so
a deploy can be told apart from the one before it. The stop
distance stays a Runtime number: the Nautilus Strategy places and replaces the
stop, and neither the Case nor the Signal carries it.

**The event loop does no PostgreSQL.** The process holds two connections, not
three. The singleton session holds the account-slot advisory lock and is read
during the sequential startup sequence; from `bridge.start()` the bridge thread
is the only PostgreSQL caller the process has, owning the singleton heartbeat,
the durable recovery identities, the day-start baseline, the projection write,
the two input reads and the audit flush. Its
session carries a five-second `statement_timeout`, because reading Commands on
it is how an operator flattens and a statement that has not finished within one
reconciliation period is broken rather than slow. The trading event loop keeps
Binance, Nautilus and the in-memory picture, offers `RuntimeStateProjector` the
row it computed, and reads everything else from memory. A failing current-state
step logs its cause once and lets the `alive` heartbeat go stale, which is
already how every reader decides a Runtime is gone.

**Quote streams are opened per admitted entry.** `on_start` subscribes nothing:
subscribing all ~500 routed USDT perpetuals is what made Binance close the
market-data WebSocket with 1008 `Too many requests`, and every illiquid route it
opened fed `market_stale` refusals to a Runtime that holds at most one position.
`QuoteStreamCoordinator` opens one stream when an admission needs a mark, and
the entry waits for the first tick as redeliveries of an unresolved Signal —
bounded by `QUOTE_WARMUP_NS`, inside every Signal TTL, never as a blocked event
loop. Recovery opens a stream for each position it reclaims, a closed position
gives its stream back, and a refused admission's stream is closed by the pump
once its warm-up window is spent.

**Readiness is three facts, and only three.** `alive` means the process,
TradingNode, event loop and database session are up; the composition root's loop
owns it, because reaching the loop body is what proves all four. `execution_safe`
means this Runtime's picture of the account is current and undisputed: startup
reconciliation happened, the private scan behind it is still fresh, no exposure
it does not own appeared, and it still holds the account slot. `entries_armed`
adds only what an operator asked for. So the Runtime's own `entry_block_reason`
is exactly one of `startup_reconciliation_unproven`, `reconciliation_stale`,
`unexpected_exposure`, `singleton_lost`, `entries_paused` and `emergency_halted`,
and the probe is ready exactly when `alive && execution_safe`.

Everything an entry needs beyond that — equity, a quote, the day baseline, a
writable audit — is answered on the entry path against that request's own facts,
where a refusal names the request that failed rather than disarming the Runtime.
#520 PR-B deleted the five booleans that sat between: `singleton_ready` and
`portfolio_ready` were true whenever the process could run at all,
`control_plane_ready` gated entries on the input plane that is the only source of
entry requests, `audit_ready` refused exposure because the local copy of what
Binance already stores was unwritable, and `day_start_ready` refused it because a
baseline the Runtime can compute from current equity had not been written yet.

**Restart recovery reads durable facts, not Cache.** Nautilus Cache is process
memory and the Binance private proof carries only open orders and position risk,
so a restart while in a position has no filled entry market order to key off.
Recovery reads this account slot's durable `order` / `leg=entry` Observations inside
a seven-day window, regenerates the deterministic entry/stop/exit client order
ids from each identity, and claims an open position on that identity's routed
instrument with the same direction plus the resting orders whose client order id
equals its deterministic stop or exit id. Because that match is by instrument
and direction alone, an identity is admitted only while its own facts leave
exposure possible: its latest `position` fact must not be `closed` and its
latest entry-order fact must not be `canceled`, `rejected`, `denied` or
`expired`. Exposure no identity claims stays unowned — `execution_safe=false`,
`entries_armed=false` — and the account projection lists its instrument, side
and quantity with `owned=false`.

**Ownership constrains only new exposure.** `/flatten account` converges the
whole account slot: a deterministic reduce-only exit for every owned position, a
reduce-only market close bounded to three attempts for every unowned one, and a
cancel for every remaining resting order. `complete_from_reconciliation` still
requires the later Binance private flat proof.

**One closed operator-control grammar, two ingresses.** `tracefold trading
issue` on the host carries the local OS uid; `POST
/api/trading/execution/commands` carries the bootstrap `ws_token`. The Workers
probe serves `/healthz`, `/readyz` and `/metrics` and nothing else — #528
deleted `POST /telegram/control`, its secret header and its chat/user
allowlists, which no production deployment had ever enabled. `/pause`,
`/resume`, `/halt`, account-only `/flatten`, and short-lived `/long` / `/short`
map to `OperatorIntentV1`; the grammar contains no quantity, notional, leverage,
venue or order parameter. Each ingress appends the intent before replying and never
manufactures a disposition — only the Runtime may
append accepted, rejected or completed control Observations. Pause blocks only
new entries; halt is sticky, rejects resume, and does not mean flatten; flatten
pauses entries and does not complete until a later fresh reconciliation proves
flat. The console's only mutation, header-authenticated `POST
/api/trading/execution/commands`, reuses that same parser, TTL, confirmation,
idempotent request identity and Runtime consumer, and admits only
pause/resume/flatten. HTTP success proves persistence and nothing else.

**Control state belongs to the account slot and survives every deploy.**
`trading_execution_runtime_control_state` is keyed by `account_slot`; the
Runtime creates an unpaused row the first time it starts for a slot and nothing
but an accepted Command moves it afterwards. A pending Signal or Command is one
whose own `expires_at_ns` has not passed, which is why there is no activation
waterline: the read states the TTL the contract already carries.

Commands and Signals share one bounded Runtime input, with Commands admitted and
handled first; queue pressure evicts only volatile Signal admission, because
PostgreSQL replays an unresolved Signal and a dropped Command is gone. The
Strategy callback reaches only Cache, Portfolio and in-memory queues;
PostgreSQL polling, audit and Telegram I/O are background work.

**Timer callbacks are not on the event loop.** Measured against a real
`TradingNode` on the pinned `nautilus-trader` 1.231.0 in
`tests/integration/test_nautilus_live_clock_threads.py`: `on_start` and every
order/position callback run on the asyncio event-loop thread, while a
`LiveClock` timer callback runs on one Rust-owned thread that
`threading.enumerate()` does not list. `RuntimeExecutionState` is unlocked, so
`OiNautilusStrategy.on_timer` does nothing but hand its pump to the event loop
and every coordinator mutates that aggregate from a single thread.

`OiNautilusStrategy` owns only Nautilus start/stop, the bounded input timer,
control-command routing and native callback routing. One
`RuntimeExecutionState` aggregate holds execution, order, position and control
identity, and concrete `EntryCoordinator`, `ProtectionCoordinator`,
`ExitCoordinator` and `RecoveryCoordinator` owners implement the four lifecycle
algorithms against it. `RuntimeObservationWriter` alone translates native facts
and dispositions into `ExecutionObservationV1`. There is no Protocol, ABC,
registry, plugin, service locator, or future-Runtime interface behind that
split.

### Failure semantics

Expected business refusals are a closed typed vocabulary written durably.
Everything else — a PostgreSQL timeout, a serialization failure, a repository
bug — propagates out of `advance()` with its transaction rolled back, so the
Case stays claimable and the Source is not consumed by an infrastructure fault.
Case+Signal failure rolls back both rows; no partial handoff exists. The Signal
lane keeps no heartbeat row of its own: `/readyz` states whether the Workers
process is alive, and the newest `trading_cases.created_at_ms` is when the lane
last froze a Case.

**A verdict and a clock are different refusals.** On the entry path
`account_stale`, `market_stale`, `day_start_baseline_missing` and the two
account-not-yet-loaded refusals write no `signal_disposition` at all: they
release the in-process claim, the unresolved anti-join redelivers the Signal on
the next poll, and only `expires_at_ns` closes it with a terminal `expired`.
Every deterministic refusal — unmapped, busy, below minimum, any risk `deny` —
is terminal and single-shot.

**A durable append fails in two ways.** A connection or timeout error keeps its
batch at the head of the audit queue and retries it unchanged. An integrity
refusal — CHECK, unique, foreign key, NOT NULL — is a verdict no retry can
change, so the batch leaves the queue and one `audit_gap` observation with
`cause=audit_append_rejected` records how many events were lost, the first
`event_id`, and the count per `normalized_kind`. The sink stays unhealthy until
that gap is itself durable, and reports it as `audit_healthy=false` with
`audit_failure_reason` on the account projection; it does not disarm entries.
Binance holds the account's own order and fill history, so refusing to open a
position because the local copy of it is unwritable spends the risk of not
acting to protect a copy (#520 PR-B). A quarantined
`signal_disposition` or `control_disposition` still resolves its Signal or
Command, because the Runtime lost the audit fact, not the input. Only the
App-side writer knows psycopg; it translates `psycopg.errors.IntegrityError`
into the sink's own `AuditAppendRejected`.

The bridge cycle runs its Command read, Signal read and audit flush as three
independent steps in that order, and only a lost connection aborts the cycle, so
an unwritable ledger cannot stop an operator from flattening. A step that keeps
failing logs its cause once rather than once per cycle, and the failure needs no
readiness gate of its own: an entry request can only arrive through the Signal or
Command read, so a Runtime whose input reads are failing has nothing to admit.

Observation batches use one set-based insert inside a savepoint, so an identity
or unique-disposition conflict rolls back the whole batch rather than leaving a
committable prefix. Callers validate and canonicalize payloads before entering
their explicit transaction; the repository callback then performs only SQL,
locks and primitive row checks.

### Read projections

Current product reads are Source/Admission, Case/Alpha, TradeSignal,
ExecutionObservation, and the current execution Runtime projection — one HTTP
owner each. `trading_cases`, `trading_candidate_gate_decisions`, `trading_trade_signals`,
`trading_operator_intents`, `trading_execution_observations`,
`trading_execution_runtime_control_state` and
`trading_execution_runtime_state` are the whole Trading schema.

Append-only history and current state are separate rows on purpose.
`trading_execution_runtime_state` is the one generation-fenced current
projection: its `account_flat` and `reconciliation_observed_at_ns` are the only
account-freshness and flat proof, and no reader folds the observation window to
obtain one. A `steady` reconciliation that finds the same positions, regular
orders and Algo orders as the previous one appends no observation at all —
unchanged current state is what the projection is for. Any other trigger, and
any change to those three identity sets, still appends.

`RuntimeAccountProjector` reads only the sole Nautilus Cache/Portfolio plus the
`RuntimeExecutionState` aggregate, then stores one bounded replaceable JSON
projection carrying current equity, drawdown, aggregate fixed-stop risk,
position PnL, protection coverage and open/in-flight/unknown-order rows. It is
not an execution contract, durable ledger, reconciliation owner, risk gate, OMS
or alternate account truth.

Every product statistic is a bounded aggregation over durable rows. Signal
latency is computed from durable source, Case and Signal timestamps; execution
latency comes only from append-only Observations. No report reconstructs an
order or treats an HTTP response, process cache, model output or provider
response as alternate truth. Each console page's statement has one owner in
`tracefold/trading/storage/queries.py`, and the query-plan audit EXPLAINs that
builder's own output — unfiltered and filtered — rather than a copy of it.

`GET /api/trading/executions` is the desk table, and it is a fold rather than a
correlation: one row per entry identity in a 24-hour window, built by joining
that entry's own disposition, `order`, `fill`, `protection` and `position`
observations and deriving one `stage` word from the result. An entry identity is
whatever `oi_runtime/observations.py:correlation` stamps on those facts — a
Signal's `signal_id`, or the `command_id` of a `manual_entry` Command — so the
two windows are one `UNION ALL` and `source` says which a row is. Folding by
`signal_id` alone left the CLI manual entry, the one ingress that can prove the
whole chain, with no row at all (#528 PR-3). The console used to rebuild this in
the browser keyed on `command_id`, which a flatten close — carried under the
entry's identity, because that is whose exposure it closes — could never match,
so the flatten progress never advanced (#528 C). Command rows travel in the same
response and read their `control_disposition` alone; nothing attaches a venue
observation to a Command row, since a flatten converges the whole account slot
rather than one intent.

A `position` observation carries the whole outcome: quantity as it stood before
the close, the average entry, and on `closed` the venue's own `avg_px_close`,
`realized_pnl` and which of the three Runtime exits took it —
`stop_filled`, `flatten` or `unclaimed_flatten`. Before #528 a `closed` position
said `quantity: 0` and nothing else, so no reader could state how a trade ended.

HTTP, CLI and React command and observation views are read
projections: recorded, Runtime accepted, order accepted, fill, and account flat
are five distinct facts.

### OI research replay

The #459 Stage A corpus and replay are **not part of the service**. They live in
`notebooks/research/` with every other research script, and nothing under
`tracefold/` imports them: `oi_research_cli.py oi-corpus` seals a Binance
open-interest corpus, `oi_research_cli.py oi-replay` scores one pre-registered
rule over it on the symbols the rule's originating probe never saw, and the
provider walk that feeds them is `notebooks/research/open_interest_history.py`.

There is no `tracefold trading oi-corpus|oi-replay` command; #537 PR-1 deleted
the parser and the CLI handler with the modules, because a research script that
reads a local corpus off disk never needed a service seam. See
[the research workspace](../notebooks/README.md) for how it is run and what it
may touch: no database transaction, no receipt, no venue write, no execution
path.

### Runtime and cutover

A deployment with `execution.mode=disabled` requires no execution credential and
canonical lifecycle stops any stale Nautilus process. `make up`,
`make deploy-image` and `make status` always require PostgreSQL, migration,
Serve, Workers and Web. For `paper|live` they additionally require one healthy
Nautilus runtime whose profile, revision, image, config digest, credentials,
singleton, Portfolio, audit, startup reconciliation and heartbeat match the
durable current projection. Decision starts `STARTING`, advances to `RUNNING`
with a durable heartbeat, and a real schema, wiring, policy or generation fault
fails Workers startup or records `FAULTED` rather than becoming observer mode.

Rollback is allowed only with venue-proven flat and a schema-compatible image.
When exposure exists the only safe direction is roll-forward: the Runtime
retains sole authority until it protects or closes the position.

## Open-interest telemetry (#137)

OpenNews strategy `1019` (`OI Event Monitor`) pushes a fixed-format frame about
190 times a day: `{SYM} OI Rise {x}%, OI Value {y}, Whale Long Profit {z}%,
Whale/OI Ratio {w}%`. The shared classifier admits it as
`telemetry_deterministic` only when the complete normalized tuple is exactly
`1019 / OI Event Monitor / market / market`; `1019` alone has no routing
authority.

Those four numbers are the whole message, so Triage judges the frame by
arithmetic instead of spending two structured model calls re-reading them.
`tracefold.news.oi_signals` parses it and returns one typed `OiJudgment`
containing its presentation Verdict, parsed signal, rule and sole
`DecisionResult`. Since #458 there is one rule and one outcome: a parsed frame
is `stored` with a magnitude-0 directional presentation and a `drop` result, and
an unparseable one is `oi_parse_failed`, also `drop`.

The lane had a second rule until then — a strict `whale_oi_ratio_bps` threshold
and an opening-rank ceiling inside a rolling 4 h window — which decided whether
a reader was interrupted. It was removed rather than retuned, for two measured
reasons. Over 48 h it and Trading's Alpha policy selected disjoint sets: seven
frames were pushed to the reader that the capital lane had refused, and none of
the five it admitted. And #459 then checked the provider's own number against
Binance's open-interest history: the reported five-minute move is substantially
price rather than position, and entered at a price a taker can actually get
those frames returned −276 bps at 4 h against a +82 bps baseline. The reader-
facing card returns as the Signal card once #433-E powers on the Runtime; until
then this lane pushes nothing, and the frame table, the Trading candidate feed
and the audit trail are unchanged.

The typed arithmetic judgment is materialized outside PostgreSQL and follows the
same prepared Verdict write as ordinary News, preserving the shared receipts,
`event_outcome`, the feed, the counters and the audit trail on the single path
they were built for. Nothing is published to the delivery consumer, because
Triage publishes only on `push`.

The ordinary-News content check still does not run on this lane. Every telemetry
headline is one template: two cards about unrelated symbols score 0.33 against
the 0.25 threshold, and two frames for one symbol score 0.41, so the check would
collapse the lane into roughly one row per symbol per window and lose
observations the frame table exists to hold. A byte-identical repeat is still
collapsed upstream by the exact fingerprint. Listing frames keep the different
exemption they were given in #72, which is per instrument rather than blanket,
because two notices for the same instrument really are one fact.

The ledger row is an idempotent append keyed on `(event_id, metric_version)`;
the prepared verdict follows in its own short transaction. No advisory lock is
taken for it any more: the lock existed so "count this symbol's earlier eligible
frames -> decide -> insert" could not miss a concurrent sibling, and nothing is
counted now.

`news_oi_signals` is the frame ledger and nothing more: a derived read model with
one writer, idempotent by `event_id`, rebuildable by re-parsing the Item, and
cascade-deleted with it. It is also the entire News surface the Trading Signal
lane consumes (#510), joined only to the source Item for its ingest mode. `rank_in_window` was dropped from it in migration
`20260901_0344` with the rule that computed it. The following `20260901_0345`
is the current head. Two consequences of judging
these frames rather than suppressing them are deliberate and worth stating:
every 1019 frame carries a verdict, so `news.retention` keeps its Item for 365
days instead of purging at 30 (~70k small rows a year), and every frame is
readable on the OI monitor with its four measurements. The decision itself lives
in `news_verdicts` like every other decision.

Strategy provenance and parser success are separate contracts. Every live `oi_v1`
frame bypasses near-duplicate matching so provider format drift reaches Triage;
an unparseable frame fails closed without a model call and persists the named
`oi_parse_failed` rule/error. Its trace and structured warning carry strategy
id, OpenNews/provider source, a title SHA-256 rather than raw text, parser
version, source-classifier version and failure stage. Status exposes 24 h
received, parsed and parse-failed counts — there is no pushed count, because
there is no push — while ordinary OI prose keeps the normal Deduper and model
path.

Two places treat these Events specially and both are explicit: they are exempt
from near-duplicate matching (two frames for one symbol differ only in their
numbers, which is their entire content, and would otherwise merge), and they are
excluded from `news_review_task_source_v1` and the model-health denominators,
because an arithmetic judgment is not model output and rating one teaches the
optimizer nothing.
