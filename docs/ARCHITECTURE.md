# Architecture

Tracefold is one Python codebase/image with two mutually exclusive runtime
composition roots, one CLI, one React console, and one PostgreSQL database.
It has exactly one business capability, News V3. The architecture
remains Kappa/CQRS: append-oriented material facts are the only business
truth; deterministic current views and bounded immutable model publications
are derived state.

## Data flow

```text
OpenNews Strategy WSS
  -> tracefold workers (RabbitMQ is the News transport plane)
  -> PostgreSQL material facts
  -> single-writer read models
  -> tracefold serve
  -> HTTP / React
```

Beside that hot path runs one strictly bounded cold plane, the Price Review
plane (#88): two polling loops in Workers read their own work from PostgreSQL,
call public venue REST with no database connection held, and write two derived
read models — latest-only current quotes and versioned Event Reactions. It is
not a market lane: no tick history, no socket, no OI, no order book, and no
price reaches the Gate, Triage, `decide()` or a delivery card. Its failure is
local by construction; News ingestion, judgment, delivery and readiness do not
depend on it.

`tracefold serve` initializes only public HTTP/static, read repositories, and
serve telemetry. `tracefold workers` initializes the bounded external
capability, singleton runtime status, and the RabbitMQ-driven News consumers
when News is enabled. News consumers recover by re-consuming durable broker
queues plus database idempotency keys. There is no database wake plane, no
projection/EDF coordinator, no CPU-process lane, and no in-memory correctness
dependency. Provider raw frames remain inputs until normalized and persisted
as material facts.

The deployment composition has four required boundaries: PostgreSQL, one
successful migration job, Serve, and Workers. `make up` is only their
fail-closed lifecycle orchestrator; it does not merge the two runtime roots.
On an empty PostgreSQL volume, the image's `initdb` hook creates the
non-login owner plus least-privilege Serve, Review, Workers, and migrate roles from
separate password files, then revokes the bootstrap login before the migration
job runs. That hook is never replayed against a non-empty cluster. Repeated
startup therefore preserves the database and operator-owned credentials, while
an unknown existing schema or missing role fails instead of being implicitly
hard-cut.

The same project-scoped application image contains the Python service and a
production React build. Migration, Serve, and Workers use that exact image and
build revision with different commands and credentials.
`make up` builds the image once and recreates only migration, Serve, and
Workers; it starts PostgreSQL when absent but does not recreate a running
PostgreSQL container. Serve owns the static console and public HTTP
boundary; Workers exposes only its loopback operational boundary. Image
construction and Compose startup do not become alternate configuration
sources: `tracefold init` remains the single generated-default authority and
`~/.tracefold/config.yaml` remains the single live application config.

## Truth, control state, and derived state

Material facts include:

- news: canonical provider Item facts admitted by the operator's OpenNews
  Strategy allowlist in `news_items` (provenance union, provider metadata,
  raw first line, first ingest mode).

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
replaying `news_items` through the Deduper (`tracefold news replay` performs
the same computation in memory). OpenNews's raw `coins` annotation remains
source evidence in `news_items.provider_metadata`; the Gate derives the bounded
`grounded_assets` from it and the read API exposes both.

OpenNews connection state in `news_ingest_state`, explicit incident intervals
in `news_opennews_incidents`, News control state (`news_control_state`),
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
`news_learning_retention_state` makes the bounded 90/365-day cold purge and
its current backlog/error observable; the database function pins the current
and previous distinct stable release chains. The schema currently has 24
`news_*` base tables and four security-barrier
review views; the exact list is executable in
`tests/integration/test_news_v3_pipeline.py`, not repeated as a hand-maintained
table-count contract.

## Package map

```text
tracefold.news
  opennews.py         canonical OpenNews frame adapter (raw_text, provenance)
  bus.py              broker envelope, routing keys, error classes, Publisher/Consumer protocols
  titles.py           content-block title extraction + pinned prefix/suffix tables
  exact_atom_identity.py comparison normalization, event family, windows
  tokens.py / minhash.py  comparison tokens, MinHash 32x4 band keys
  gate.py / storyline.py  deterministic admission, priority, grounded assets, storyline keys
  pricing.py          Price Review domain: source order, quote/candle normalization, reaction_v1 metric
  price_loops.py      the two cold loops (QuoteSnapshotLoop, EventReactionLoop) and their one-slot DB lane
  price_repository.py quote snapshots, Event Reactions, and the bounded review aggregates
  facts.py            atomic fact units and immutable Event evidence snapshots
  review.py           ReviewDesk queues, evidence views, rubrics, acceptance receipts
  candidate_evaluator.py content-addressed datasets and stable/candidate evaluation workflow
  canary.py           deterministic one-arm assignment and durable trip/close control
  events.py           the Deduper transaction (admit_item)
  triage_rules.py     decide() post-rules (DecidePolicy), throttle, fail-closed fallback
  agents/             the Triage structured call and its byte-frozen prompt
  delivery.py / control.py  cards, control commands
  consumers.py        Receiver, Recovery, Deduper, Triage, Deliverer, Janitor
  repository.py / query_specs.py  news_* access and audited reads
  eval/               provider-hits Deduper+Gate replay only

tracefold.integrations
  provider and external-system adapters: OpenNews, RabbitMQ, Feishu

tracefold.platform
  config, PostgreSQL/Alembic, telemetry, paths,
  bounded resource primitives, docker host translation

tracefold.app
  composition, repositories, the worker root package (`app/workers/`), HTTP, and CLI
```

The business package root is its public Python interface: `tracefold.news`.
Consumers outside the owning package import from the root only. Internal
subpackages may change without creating a repository-wide import graph.

The application composition root and concrete provider adapters are private
implementation collaborators, not product consumers. Where one of them must
construct a repository, schedule an internal worker, or reuse the exact pinned
parser/composer implementation behind a public protocol, its package-private
import is enumerated exactly by the architecture harness. Those named seams are
not re-exported, compatibility interfaces, or available to feature callers;
all public models and protocols still come from the package root.

The dependency direction is:

```text
app -> integrations + business packages + platform
integrations -> business package interfaces + platform
news -> platform
platform -> Python / third-party libraries only
```

Business packages never import `tracefold.app`, provider integrations, or each
other. Transport adapters do not own business rules. The Workers root and its
private TaskGroup loops live in `tracefold.app.workers`; platform exposes only
bounded resource contracts. Queue state machines and
read-model behavior stay with their business owner. These rules are executable
in `tests/architecture/test_backend_boundaries.py`.

A Provider is an integration adapter, not a product layer, registry, or second
source of truth. Each adapter translates one upstream transport and error model
into a business-package protocol. The adapters are OpenNews (the authenticated
Strategy WSS plus the official Strategy list/hits endpoints), RabbitMQ
(`aio-pika`), and Feishu (the custom-bot webhook). No provider owns a durable
queue. Expected provider failures stay inside the owning bounded
loop; an unhandled child exception is deliberately a Workers-root failure and
the container restarts the single process.

SQL ownership follows the same boundary: News owns `news_*`; platform owns
Alembic and `workers_runtime`. News makes no cross-domain read: its single
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
- one Triage verdict insert; one delivery begin or settle.

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
`news-deliverer`, `news-janitor`), the bounded cold loops
(`news-instruments`, and with venues enabled `news-quotes`,
`news-reactions`), and `workers-control` (singleton lock, heartbeat, runtime
row). There is no acquisition clock, projection coordinator, model arbiter,
stream ingester, identity backfill, or universe sync task. The three cold
loops poll public catalogues and prices on code-owned cadences and admit their
database work through a separate one-slot lane, never the four News hot-path
slots.

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
       -> exact fingerprint / MinHash 32x4 LSH near-duplicate + strong-fact veto
       -> Event new|member (family window) -> Gate (provider-graded grounded_assets,
          macro/energy lexicon, PR-template veto, low-signal switch) -> preliminary
          storyline key; a stronger later member re-gates a suppressed Event
       -> publish event.<family>.<priority> only for admission=candidate
  -> q:news.triage [prefetch = news.triage.concurrency, handled concurrently] Triage:
       one structured call (frozen system prompt, <event> -> <gate> -> <event_status> status bar,
       one bounded retry for a fast retryable model failure) -> final storyline key from the
       verdict (written back) -> decide() policy -> verdict row (title_zh, audience, prompt sha,
       input sha, preliminary + final status snapshots, named rule) -> publish verdict.push
       (an escalate rides the same routing key at AMQP priority 5; there is no second model call)
  -> q:news.deliver [single-active-consumer] Deliverer: begin(sending) -> one Feishu attempt
       -> settle sent|terminal; paused -> terminal/delivery_paused; crash between send and ack
       -> ambiguous_after_crash
  -> news.retry (one 30 s TTL lane -> back to x:news): TransientError counted (3 attempts),
     DeferError uncounted; x:news.dlx -> q:news.dead for permanent/exhausted/crashed messages
  -> Janitor: outbox catch-up, band expiry, 30/365-day Item retention,
              bounded learning-evidence retention on the one-slot cold lane,
     broker depth snapshot
  -> Serve: /api/news/feed, /api/news/events/{event_id}, /api/news/status
```

#### Price Review plane (#88)

Two cold loops beside the hot path, sharing one instrument-resolution strategy
and no state with it:

```text
recent live Events + watchlist -> exact-symbol-first resolution (alias only as fallback,
  reference tiers never candidates) -> unique Price Instruments deduplicated by
  (venue, venue_symbol, price_kind) -> grouped by provider source
  -> one batch REST request per source (Hyperliquid metaAndAssetCtxs /
     spotMetaAndAssetCtxs / one per HIP-3 dex; Binance ticker/price, or
     ticker/24hr on the one turn in 15 that refreshes its day reference, #109)
  -> one latest-only row per source in news_quote_snapshots
  -> GET /api/news/quotes (<=100 symbols, resolved server-side, fresh|stale|unavailable|unlisted)

due Event-assets (live Events, pushed and held alike) -> pinned or resolved instrument
  -> merged historical 5m candle ranges (<=32 requests/turn, concurrency 4)
  -> p0 = last closed candle at or before opened_at_ms; p1/p4 the same at +1H/+4H
  -> (pH/p0)-1 in integer basis points -> news_event_reactions (reaction_v1)
  -> Feed/Detail attachment + GET /api/news/review
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
行情 line rendered from a `fresh` (<= 60 s) Quote Snapshot, never read back by
any decision, and absent rather than approximated when no fresh value exists —
68.7% of a week's cards carried one.

The price and the day change are two questions on two cadences (#109). Binance
answers "what is it worth now" in 45.5 kB (`ticker/price`, whole USD-M market,
weight 2) and both questions in 270 kB (`ticker/24hr`, weight 42) — 92% of the
bigger payload is fields we never display, for symbols nobody asked about. So a
Binance source alternates: `ticker/24hr` every 300 s, `ticker/price` on the
turns between. The wide read **replaces** that turn's narrow read rather than
joining it, which is what keeps "one batch request per source per turn" literally
true and keeps an optional question off the mandatory one's deadline — a second
request inside the same `asyncio.wait_for` would cancel the whole gather on a
slow response and discard every price already fetched, on every venue.

What the wide read caches is the rolling window's `openPrice`, **not** the
percentage. `priceChangePercent` is `lastPrice/openPrice - 1`, and the numerator
is the number the next turn is about to replace — freezing the ratio for 300 s
while refreshing the price every 20 s would put a price and a percentage that
cannot be derived from each other side by side, most visibly in the minutes after
a push. Caching the denominator instead means the percentage is recomputed from
each turn's own price, and the only thing ageing is a 24 h window open, which
moves 0.023% per turn.

Nothing is cached for a read that failed or was cancelled, so a failed day read
leaves the source due: it writes nothing that turn, its previous row ages, and
the next turn asks again — the same stale-not-blank rule as any other venue
failure, and the reason a good percentage can never be overwritten with a blank.
A symbol that joins the working set triggers a wide read immediately instead of
waiting out the cadence, since the plan is ordered newest Event first and that
symbol is the card the operator is looking at; coverage records what the last
wide read *asked* for, so a symbol no venue lists cannot pin a source to the
expensive endpoint. `binance.spot` asks by name on both endpoints, with the
`symbols=` list dropped only on `ticker/24hr` past 100 symbols where the weight
tiers make the whole market cheaper. Hyperliquid never alternates — its single
request already carries `midPx` and `prevDayPx`.

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
| REST after #109 (price @ 20 s + 24hr @ 300 s) | — | **0.26 GB** |
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
REST plane it would replace at the accepted cadence — 0.26 GB/day for USD-M
after #109, not the 1.19 GB/day figure that motivated the question. If it is ever built it
must meet what the OpenNews receiver already meets — jittered reconnect with
resubscription, forced reconnect before the venue's connection lifetime,
ping/pong liveness, **a per-symbol staleness watchdog that degrades a
silent-but-connected socket to `stale` instead of freezing the last value**,
subscription diffing inside the venue's subscribe rate limit, one connection per
venue with isolated failure, no socket in Serve, and REST as the source of truth
on startup and after any gap.


Ownership: `tracefold.integrations.rabbitmq` is the only module that imports
`aio_pika`; `tracefold.news.bus` owns the envelope, routing keys, error classes,
and Publisher/Consumer protocols. `tracefold.news.consumers` holds the six
consumers wired by `tracefold.app.workers._wire_news_pipeline`; they run as
asyncio tasks in the single Workers process but coordinate only through the
broker and PostgreSQL keys, so they can be scaled out without code changes.
News consumers use their own four-slot database lane
(`WorkerDatabase.run_news`) so background backlog never starves a live Event;
a lane admission timeout is a `DeferError` (uncounted requeue), a statement
overrun is a `TransientError` (counted).

Identity: `news_items.item_id = sha256(source_id, params.id)`;
`news_events.event_id` is the leader item id. `tracefold.news.titles`
extracts the first content block (skipping URL-only, label-only, `reply/quote:`
lines and pinned wire source labels/suffixes; exchange names and `@handles`
are subjects and stay — `@Krakenfx launches ...` keeps `Krakenfx`),
`tracefold.news.exact_atom_identity`
normalizes for comparison, `tracefold.news.tokens` + `minhash` produce the
band keys stored in `news_event_bands`, and `tracefold.news.events.admit_item`
is the single Deduper transaction. Fingerprints of at most two tokens never
share an Event.

Gate and storyline (`tracefold.news.gate`, `tracefold.news.storyline`) are pure
functions and keep no name table of their own: grounded assets are the
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
The Gate does not decide relevance: every Item is a `candidate` unless it is a
recovery replay, a law-firm template notice (strong template phrases always;
weak ones only without a grounded asset), an under-80 market-telemetry frame,
or — behind `news.gate.suppress_low_signal`, default off — an ungrounded,
non-macro social post under 70. A `listing` frame takes the
`listing_deterministic` admission, which is admitted and judged like a
candidate (#72). A member that
joins a suppressed Event with stronger evidence (score >= 80, an A/A+ grounded
tag, or a different source) re-gates it in place and it publishes once.
Priority is `high` (AMQP priority 5) for score >= 90, watchlist hits, listing
frames, or rate/yield macro. The preliminary storyline key (status bar only)
is theme-first (`crypto_treasury`, `mideast_energy`, `rates`, `trade`,
`china_macro`, `metals`, `us_equity_macro`, `us_macro_data`), then the first
A/A+ or cashtag asset; the final key is computed after Triage from the
verdict's grounded primaries and scope, written back to `news_events`, and
used by duplicate comparison, operator grouping, advisory locking, and mute.

Triage (`tracefold.news.agents.triage_model`, `tracefold.news.triage_rules`)
never retrieves: the worker builds reader context from settled sent cards,
the consumer adds the **told ledger** — the cards the reader actually received
in the last 4 h (`repository.told_ledger`: newest push/escalate verdicts whose
first delivery has a durable `sent` receipt, no degraded fallbacks, plus the preliminary
storyline's own newest cards fetched separately; at most 12 entries in the
status bar, up to six same-storyline slots reserved, the rest the newest
cross-storyline cards, each with index `i`, age, magnitude, direction,
`headline_zh`) — and passes both last in the human message as the status
bar. The byte-frozen system
prompt is English (instructions) and every text field the verdict returns is
Chinese: `headline_zh` (the card header — a complete headline that keeps the
decisive fact, not a stub), `why_zh` (the one card sentence adding what the
headline does not say), a
console-only `title_zh` (the full Chinese title; empty means "same as
`headline_zh`" — prompt v9 asks for the sentinel because 85% of a live day's
verdicts repeated the headline verbatim there, ~13% of all output tokens, and
`models.display_title` fills it in for every console and API surface; the
Feishu card is the one deliberate exception, where an empty value has to stay
empty so the header can fall through to the wire title), and an `audience`
(crypto / us_equity / macro / none). The verdict also carries `novelty`
(`new_fact` / `progression` / `restatement`, judged against the told ledger)
and `restates` (the ledger index a restatement points at; -1 otherwise) —
the reader-facing memory Triage has (issue #61): dedup is byte/word-level,
novelty is the semantic last line against the same fact told again from
another outlet or under another storyline key. Magnitude and `actionable`
are calibrated in the prompt (its magnitude scale, the `actionable` definition
and the classification examples), never in code; prompt v9 files a listed
company's or token issuer's own product update at magnitude 2 and defines
`actionable` because the `model_push_actionable` branch of `decide()` requires
it (the other push paths do not check it). `decide()` owns the final
decision under a `DecidePolicy` whose defaults are the live policy and whose
values come from `news.policy`: mute -> drop; noise -> drop; a *grounded*
restatement (the model cites a ledger entry it was shown and the direction did
not flip against it; switch `restatement_drop`) -> drop (`restatement`);
magnitude >= 3 with a direction or macro scope -> escalate; high priority +
push -> escalate; model push/escalate intent, actionable, magnitude >=
`min_push_magnitude` (1) and a direction -> push (`model_push_actionable`);
unclear direction with a clear event type (product, listing, delisting,
regulation, hack, exploit, partnership, filing) at magnitude >= 2 -> push
(`unclear_but_clear_event`); other unclear -> drop; watchlist primary at
magnitude >= 1 -> push; else drop (`below_threshold`). Policy v7 deliberately
has **no hourly, two-hour, or four-hour reader quota**. Historical push counts
remain observable metrics, but they are not included in the model input and
cannot change `push`/`escalate` into `throttled`. Once the semantic conditions
pass, the delivery harness executes the decision; it only enforces explicit
pause/mute controls, idempotency, provider pacing, and real delivery receipts.

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
drops silently. A fast retryable
model failure (timeout, rate limit, connection) or an unusable answer that is
not a `max_tokens` truncation (empty tool call, missing field) earns one more
attempt inside the deadline, and once that budget is spent a verdict that is
complete except for `novelty` is accepted as `new_fact` (`novelty_defaulted`,
prompt-v5 quality) rather than dropped on rules; model failure is degraded,
not silent:
`rule_baseline` (watchlist primary, score >= 80 with a grounded asset, or —
since #81 — a high-priority Event or a deterministic exchange notice, which is
what a missile strike, a rate decision or a delisting looks like without a
ticker) still pushes on the wire headline, everything else drops with
`degraded=true`, and three
consecutive transport failures open a 60-second circuit that also opens a
`triage_circuit_open` incident (closed by the next success); an output failure
(`news_triage_output_truncated` when the tool call hit `max_tokens`,
`news_triage_output_invalid` on a schema mismatch) is degraded but never
counts toward the circuit and records `finish_reason`, `output_tokens`, and
`parsing_error` in the trace. After the model call the consumer decides and
persists in one transaction under a per-storyline advisory lock on the final
key (`repository.lock_storyline`; `pg_advisory_xact_lock('NEWS', hashtext(key))`),
re-reading the reader evidence inside the lock so two same-key Events in flight
cannot both send the same fact (the lock raises the lane's 250 ms
`lock_timeout` for that transaction only); when a card the model was not
shown has landed in the ledger by then (compared by event id, not by clock —
verdict rows carry their handler's start stamp), the consumer reloads sent
content evidence and control under a fresh stamp and asks the model once
more with the fresh ledger (`reasked_after_told_change`) instead of pushing a
restatement the reader just received; if that second call fails, the model's
first judgment is persisted (`reask_failed`), never the rule baseline. `news_verdicts` stores `model_decision`, `rule_baseline_decision`,
`final_decision`, `override_rule`, `throttled_by`, `degraded`, and a
replayable trace (latency, tokens, model attempts, prompt sha, input sha, the
preliminary storyline key, the preliminary and final status-bar snapshots,
the told ledger as shown with event ids, `told_count`, `restates_event_id`,
`first_verdict`/`first_input_sha256`/`reask_failed` when re-asked,
`novelty_defaulted`, the final storyline key).

There is no second model stage: one Event gets one structured judgment and one
card (issue #57). `escalate` stays a `decide()` outcome — a high-importance
push that rides the same `verdict.push` routing key at AMQP priority 5 and
wears a ⚡ card header — and never triggers another model call. The retired
Analyst lane (`q:news.deep`, the `verdict.escalate`/`verdict.deep` routing
keys, the evidence bundle and its `verify_verdict()` gate, follow-up cards)
left `stage='deep'` verdicts and `kind='followup'` deliveries as historical
rows that are never written again, and topology declaration deletes an old
`news.deep` queue at startup.

Delivery (`tracefold.news.delivery`, `consumers.DelivererConsumer`) renders the
reader contract (`news_delivery_card_v10`): the header is `headline_zh` (⚡ when
the decision is escalate; it falls back to `title_zh`, then the original
title), the first body line is `why_zh`, and the second is the facts in plain
words — direction label, `新进展` when the verdict's `novelty` is `progression`
(#113: 28.8% of a week's cards advanced a story the reader already had one for
and the card said nothing), magnitude label, the tickers the model called
primary and the Gate grounded, source（N 条报道）, and the leader item's
publication time in the reader's zone (UTC+8). The third body line is the
market's own number for those same tickers — `行情 CL $86.43 24h +2.30%（永续）`
— and it exists only when `PriceRepository.quotes_for_symbols` answered `fresh`:
a `stale`, `unavailable` or `unlisted` quote leaves no line, no placeholder and
no zero. The change window is named from `change_basis` rather than assumed
(`rolling_24h` -> `24h`, `provider_day` -> `日内`, unknown -> the price without a
percentage), `（永续）` marks each asset whose number comes from a proxy
market rather than its own — an equity/commodity/index on a Binance TradFi perp
or a Hyperliquid builder-DEX, 51.9% of a week's card assets. It is keyed on
`instrument_class`, not on the contract type: BTC also prices on a perpetual,
but for a crypto asset that *is* its own market, so it carries no mark. The mark
is repeated per asset rather than said once for the line, because a trailing
mark on a mixed line cannot say whether it covers the last asset or all of them.
The price/percentage formatting
mirrors the console's `web/src/features/news/model/newsPrice.ts` character for
character. The quotes are read in a separate short database session over
exactly `card_assets()`, so the two lines cannot name different assets, and any
price failure degrades to no line: delivery never depends on the price plane.
Then a 打开来源 button and a small `Tracefold · <event_id[:8]>`
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
retry: `news_deliveries(event_id, kind)` (`kind` is always `first`) is
inserted as `sending` before the single HTTP call and settled `sent`/`terminal`;
interrupted rows are terminalized at startup. Recovery items, suppressed
events, and muted storylines never deliver; a paused lane settles
`terminal/delivery_paused` instead of holding an unacked message. Policy v7 has
no hourly, two-hour, or four-hour reader quota: a push/escalate decision reaches
the Deliverer regardless of how many earlier cards were sent. Control state
(`news_control_state`) is written by
`tracefold news control` and read by Triage and the Deliverer on every message.

Incidents and recovery: WSS transport/auth/protocol/idle failures, broker
backpressure/unavailability, and Triage circuit opens are rows in
`news_opennews_incidents`; reconnect closes transport incidents and requests
recovery, which pages the official Strategy hits endpoints for the closed
interval and publishes `raw.recovery.*` frames (`admission=recovery`, never
delivered). Dead letters are operator-visible through `tracefold news dlq
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
first bad owner). A judgment becomes training/eval truth only after a separate
acceptance receipt. An important fact missing before Event creation enters as
an immutable external-miss snapshot, rather than a fake Event id.

`CandidateEvaluator` exposes one deep interface: freeze accepted evidence,
compare stable and one declared candidate variable, and publish release
evidence. Validation/holdout replay both arms sequentially because each arm's
would-reach-reader ledger changes later decisions. Model requests/responses are
recorded and content-addressed; replay mode must match those recordings exactly
or fail. A frozen dataset accepts Event cases only from the exact active Agent
bundle cohort and records every prompt/schema/retrieval/model/execution/policy
hash plus the reader-contract version; a mutable provider model alias is marked
as mutable rather than described as an immutable snapshot. Hidden validation
pre-registers at most 50 independent fact-cluster representatives before either
arm output is inspected, permits at most 100 human judgments, and returns
`UNKNOWN` when the batch remains unresolved. A candidate-only critical error
(unsupported fact, wrong entity/direction, missed key fact, severe repetition,
or injection obedience) is a release failure. Mean and peak delivery load are
reported for operator impact analysis but are not candidate-release quotas;
correctly recognizing many distinct facts cannot fail a release by count alone.
Automated optimizers may propose a candidate but cannot modify the
reader contract, rubric, accepted reviews, holdout, thresholds, stable bundle,
or production assignment.

Promotion is monotonic: development screen -> future temporal validation ->
blind pairwise review -> 24 h shadow -> deterministic 10% canary -> stable.
Every stage requires the prior sealed PASS. One Event is assigned to exactly
one production arm before the model call and runs exactly one model; recovery,
deterministic listing and high-priority fail-open traffic stay on stable. A
candidate artifact/schema fault trips the canary to stable, and activation,
assignment, deployment and rollback receipts remain auditable. The market view
is secondary discovery evidence only: it defaults to one exact
prompt/policy/model cohort, uses horizon-mature coverage denominators, clusters
similar withheld Events at fact grain, and never treats a 1 h/4 h move or a
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
Workers evidence-append grant/lock repair and role-authentic audit. No
chained revision has a downgrade. Earlier hard cuts live only in git history;
a fresh database and a database upgraded through the chain reach
byte-identical schemas.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
