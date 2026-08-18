# Architecture

Tracefold is one Python codebase/image with two mutually exclusive runtime
composition roots, one CLI, one React console, and one PostgreSQL database.
The architecture remains Kappa/CQRS: append-oriented material facts are the
only business truth; deterministic current views and bounded immutable model
publications are derived state.

## Data flow

```text
providers / public streams
  -> tracefold workers
  -> PostgreSQL material facts + bounded projection state + native model state
  -> single-writer read models or immutable publications
  -> tracefold serve
  -> HTTP / persisted-live WebSocket / React
```

`tracefold serve` initializes only public HTTP/static/WebSocket, read
repositories, and serve telemetry. `tracefold workers` initializes ingestion,
acquisition, the bounded external/model capabilities, one short-projection CPU
lane, singleton runtime status, the RabbitMQ-driven News consumers when News is
enabled, and one EDF projection coordinator for the remaining frontier-backed
domains. Market and Macro workers recover exclusively by re-reading PostgreSQL
facts, typed Macro/Profile frontiers, native Fed-document model state, and
queues on bounded code-owned clocks; News consumers recover by re-consuming
durable broker queues plus database idempotency keys.
There is no
database wake plane or in-memory correctness dependency. Provider raw frames
remain inputs until normalized and persisted as material facts.

The deployment composition has four required boundaries: PostgreSQL, one
successful migration job, Serve, and Workers. `make up` is only their
fail-closed lifecycle orchestrator; it does not merge the two runtime roots.
On an empty PostgreSQL volume, the image's `initdb` hook creates the
non-login owner plus least-privilege Serve, Workers, and migrate roles from
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
PostgreSQL container. Serve owns the static console and public HTTP/WebSocket
boundary; Workers exposes only its loopback operational boundary. Image
construction and Compose startup do not become alternate configuration
sources: `tracefold init` remains the single generated-default authority and
`~/.tracefold/config.yaml` remains the single live application config.

The projection coordinator executes exactly one semantic shard at a time.
A productive turn yields cooperatively and immediately rereads every typed
frontier head so a real backlog can converge. Failed and idle turns wait on the
fixed 250 ms code-owned cadence to prevent contention from becoming a hot loop;
that cadence is scheduling policy, not a correctness authority.

## Truth, control state, and derived state

Material facts include:

- evidence: `raw_frames`, `events`, `event_entities`;
- identity: `token_evidence`, `token_intents`,
  `token_intent_lookup_keys`, `token_intent_resolutions`,
  `registry_assets`, `asset_identity_evidence`, `asset_identity_current`;
- market: `market_ticks`, `enriched_events`, `market_observations`,
  `market_settlements`, and `market_position_facts`;
- news: canonical provider Item facts admitted by the operator's OpenNews
  Strategy allowlist in `news_items` (provenance union, provider metadata,
  raw first line, first ingest mode);
- macro: revision-preserving `macro_series_facts`, `macro_release_facts`, and
  `macro_documents`.

Current read models are `token_profile_current`,
`market_tick_current`, `news_events` (plus `news_event_members`,
`news_event_bands`, `news_event_assets`), and the six stable rows in
`macro_module_current`. Each uses stable product/window/target identity, has
exactly one runtime writer, is rebuildable from facts, and writes zero serving
rows when its business payload is unchanged. `news_events` is rebuildable by
replaying `news_items` through the Deduper (`tracefold news replay` performs
the same computation in memory). OpenNews's raw `coins` annotation remains
source evidence in `news_items.provider_metadata`; the Gate derives the bounded
`grounded_assets` from it and the read API exposes both.

OpenNews connection state in `news_ingest_state`, explicit incident intervals
in `news_opennews_incidents`, News control state (`news_control_state`),
queues, leases, retries, native model runs/jobs, and terminal events are
control state. Typed Macro and Profile
frontiers store stable
domain/shard identity, input fingerprint, earliest deadline, lease, failure,
and publication checkpoints. `first_dirty_at_ms` records the causal change,
`deadline_at_ms` is the freshness SLA, and `next_attempt_at_ms` is only an
eligibility clock for a scheduled recheck or retry. An eligible shard may run
before its deadline; the deadline is never a start gate. Profile refresh heat tiers, retry attempts, provider circuits, and terminal reasons are
likewise queue policy, not profile facts.
`news_verdicts` (Triage and Analyst decisions bound to a policy version) and
rows in `macro_document_analyses` are derived model outputs bound to frozen
evidence; they are not material facts. `news_title_presentations` is durable
presentation state keyed by the comparison fingerprint. `news_deliveries` is
the one-attempt outbound ledger keyed by `(event_id, kind)`; there is no retry,
lease, or backfill. `news_event_market_marks` and `news_event_labels` are the
learning-plane truth for evaluating decisions.

## Package map

```text
tracefold.market
  capture/       provider-neutral evidence ingestion
  identity/      token and asset identity resolution
  pricing/       append-only market facts and current prices
  profiles/      source-backed token profiles and image state
  views/         persisted market read queries

tracefold.news
  opennews.py         canonical OpenNews frame adapter (raw_text, provenance)
  bus.py              broker envelope, routing keys, Publisher/Consumer protocols
  titles.py           content-block title extraction + pinned prefix/suffix tables
  exact_atom_identity.py comparison normalization, event family, windows
  tokens.py / minhash.py  comparison tokens, MinHash 32x4 band keys
  gate.py / storyline.py  deterministic admission, priority, grounded assets, storyline keys
  events.py           the Deduper transaction (admit_item)
  triage_rules.py     decide() post-rules, throttle, fail-closed fallback
  analyst_rules.py    verify_verdict() evidence gate
  agents/             Triage structured call, Analyst deepagents harness, tools, prompts
  translation.py / delivery.py / control.py  waterfall, cards, control commands
  consumers.py        Receiver, Recovery, Deduper, Triage, Analyst, Translator, Deliverer, Janitor, Control
  repository.py / query_specs.py  news_* access and audited reads
  eval/               market marks, offline evaluation, replay

tracefold.macro
  registry.py    code-owned Dataset Registry and six-module membership
  acquisition.py clock-driven claim, provider-I/O, fact and cursor flow
  calculations.py versioned calculation registry and transparent features
  projection.py  six current decision modules
  fed_analysis.py evidence-bound FOMC/speech analysis contract

tracefold.integrations
  provider and external-system adapters, including DeepAgents

tracefold.platform
  config, PostgreSQL/Alembic, telemetry, paths, and bounded resource primitives

tracefold.app
  composition, repositories/providers, the sole worker root, HTTP/WS, and CLI
```

The three business package roots are their public Python interfaces:
`tracefold.market`, `tracefold.news`, and `tracefold.macro`. Consumers outside
an owning package import from the root only. Internal subpackages may change
without creating a repository-wide import graph.

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
macro -> market + platform
market -> platform
platform -> Python / third-party libraries only
```

Business packages never import `tracefold.app` or provider integrations.
Transport adapters do not own business rules. The Workers root and its private
TaskGroup loops live in `tracefold.app.workers`; platform exposes only bounded
resource/projection/model candidate contracts. Queue state machines and
read-model behavior stay with their business owner. These rules are executable in
`tests/architecture/test_backend_boundaries.py`.

A Provider is an integration adapter, not a product layer, registry, or second
source of truth. Each adapter translates one upstream transport and error model
into a business-package protocol. App composition decides which configured
adapter owns each durable queue; disabled adapters own no work, and startup
starts a bounded drain of their obsolete nonterminal profile queue rows, which
continues through ordinary Worker turns. Operational status is derived from
PostgreSQL facts, circuits, and queues rather than cached provider objects or
live probes. Expected provider failures stay inside the owning bounded loop; an
unhandled child exception is deliberately a Workers-root failure and the
container restarts the single process.

SQL ownership follows the same boundary: Market owns the event, token, asset,
profile, price, collector, general cross-asset observation, and
settlement tables; News owns `news_*`; Macro owns `macro_*`. Platform owns
Alembic, checkpoint, and generic terminal-evidence tables. Macro imports Market only through
`tracefold.market`, has no live or hidden dependency on News, and never
duplicates general market facts into Macro storage. The architecture gate
checks SQL table references against the generated current schema.

## Transaction ownership

Application services and workers own transaction scope. Repository writes use
the supplied connection and never expose commit switches or open hidden
transactions.

Important atomic units are:

- fact persistence, identity resolution, market capture, and downstream dirty
  target creation;
- one Macro acquisition completion: normalized fact insert, cursor advance, and
  compare-and-set target completion;
- current read-model write plus acknowledgement of the exact claim;
- one accepted OpenNews frame: NewsItem upsert with provenance union plus its
  Event assignment (new Event, bands, assets, or membership);
- one Triage/Analyst verdict insert; one delivery begin or settle;
- immutable Fed document analysis plus completion of its exact native job;
- retry or terminal transition plus mutation of its source queue row.

Provider, model, subprocess, filesystem, and network I/O occurs outside
database transactions.

Each Worker database session owns exactly one bounded PostgreSQL transaction.
It installs its statement and transaction limits as transaction-local settings
in one setup round trip, so PostgreSQL is the native deadline authority for all
SQL in that session. Transaction exit restores the connection automatically;
there is no session reset round trip. Awaiting DB, CPU, finite-operation, and
model work adds only a bounded completion grace so the native result wins at
its deadline. If an asyncio wrapper callback is delayed, an already-completed
native future is consumed directly. A typed recurring business-DB future that
remains alive beyond the grace is a local loop failure: its permit stays bound
to native completion and the loop retries on its natural cadence. Control-DB,
model, CPU, cleanup, and otherwise unclassified overruns remain fatal. This
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
finishes. Frontier-backed projection turns therefore have phase-native
deadlines and no aggregate fatal watchdog.

Projection claim leases cover the complete legal phase envelope: Profile and
Macro use 30 seconds each. News consumers have no frontier lease; the broker's
single-active-consumer and per-message ack are their fences.

## Product flows

### Market

`market_tick_current` is transactionally maintained from append-only
`market_ticks`; it has no projection worker or dirty queue. Explicit bounded
fact replay rebuilds it. The bounded market poll selects active targets by
24-hour intent activity in stable order; there is no product-driven priority
input, so no derived read model can feed a market result back into acquisition
scheduling.

```text
events + intent resolution revisions + identity/profile facts + market ticks
  -> Search (bounded lexical/substring fact reads)
  -> Token Case (target dossier: profile, timeline, posts, live market)
  -> /api/live-market (one durable market_tick_current row per target)
```

Search and Token Case read their owning facts directly; there is no
model-derived, scored, or ranked product layer between them and the persisted
facts. There is no Token Radar or Stocks product, route, public read model,
writer, or worker task; historical migrations `20260810_0249` through
`20260814_0269` shaped the former Radar singleton and `20260818_0274` removed
it together with its Radar-only Event/resolution covering indexes and the
generated `events.token_radar_text_fingerprint` column. `us_equity_symbols`
remains only an identity-collision guard for token resolution and does not
constitute a Stocks surface. General cross-asset Market facts and the six Macro
modules remain unchanged.

Profile refresh targets use `hot`, `warm`, and `cold` queue tiers; missing and
error outcomes back off exponentially to a bounded terminal state, and only a
new evidence fingerprint reactivates that target. Profile eligibility and
invalidation come from identity/profile facts and Profile-owned policy.

### News

News V3 is a broker-driven Event pipeline. RabbitMQ is the only transport,
buffer, retry, concurrency, and dead-letter plane; PostgreSQL holds facts,
decisions, and audit; every write is idempotent by key. The Story/Brief/RSS/
pinned-WorldMonitor lane is retired.

```text
OpenNews account Strategies (news.opennews_strategy_ids; validated at startup)
  -> authenticated persistent WSS; server pushes strategy.triggered; no app subscribe frame
  -> Receiver publishes each accepted frame to x:news with publisher confirms
     (routing key raw.opennews.<strategy_id>; recovery frames use raw.recovery.<strategy_id>)
  -> q:news.raw [single-active-consumer] Deduper:
       Item upsert (provenance union) -> content-block title + pinned normalization
       -> exact fingerprint / MinHash 32x4 LSH near-duplicate + strong-fact veto
       -> Event new|member (family window) -> Gate (engine_type, asset_class,
          grounded_assets, macro lexicon, PR-template) -> storyline key
       -> publish event.<family>.<priority> only for admission=candidate
  -> q:news.triage [prefetch N] Triage: one structured call (frozen system prompt,
       <event> -> <gate> -> <event_status> status bar) -> decide() rules -> verdict row
       -> publish verdict.push (+ verdict.escalate)
  -> q:news.translate (verdict.push) Translator: DeepL -> DeepSeek -> original, keyed by fingerprint
  -> q:news.deep (verdict.escalate) Analyst: minimal deepagents harness, 7 read-only tools,
       verify_verdict() -> deep verdict -> publish verdict.deep only after the first card was sent
  -> q:news.deliver [single-active-consumer] Deliverer: begin(sending) -> one Feishu attempt
       -> settle sent|terminal; crash between send and ack -> ambiguous_after_crash
  -> x:news.control (fanout) pause/resume/mute_theme/mute_symbol/drain -> news_control_state
  -> retry lanes news.retry.{5s,30s,120s} (TTL -> back to x:news) for transient errors;
     x:news.dlx -> q:news.dead for permanent/exhausted messages
  -> Janitor: band expiry, 30-day retention, market marks (t0/+5m/+30m/+4h)
  -> Serve: /api/news/feed, /api/news/events/{event_id}, /api/news/status
```

Ownership: `tracefold.integrations.rabbitmq` is the only module that imports
`aio_pika`; `tracefold.news.bus` owns the envelope, routing keys, and
Publisher/Consumer protocols. `tracefold.news.consumers` holds the eight
consumers wired by `app/workers.py::_wire_news_pipeline`; they run as asyncio
tasks in the single Workers process but coordinate only through the broker and
PostgreSQL keys, so they can be scaled out without code changes.

Identity: `news_items.item_id = sha256(source_id, params.id)`;
`news_events.event_id` is the leader item id. `tracefold.news.titles`
extracts the first content block (skipping URL-only, label-only, `reply/quote:`
lines and pinned wire prefixes/suffixes), `tracefold.news.exact_atom_identity`
normalizes for comparison, `tracefold.news.tokens` + `minhash` produce the
band keys stored in `news_event_bands`, and `tracefold.news.events.admit_item`
is the single Deduper transaction. Fingerprints of at most two tokens never
share an Event.

Gate and storyline (`tracefold.news.gate`, `tracefold.news.storyline`) are pure
functions with pinned lexicons: grounded assets are provider grade A/A+ tags
whose symbol (or match text) appears in the title or A+ tags on non-CL assets;
`CL`/`XYZ-CL` is grounded only in energy context; macro-lexicon items pass to
Triage even without assets; ungrounded non-macro items and law-firm template
notices are suppressed; `listing` frames are deterministic. Priority is
`high` (AMQP priority 5) for score >= 90, watchlist hits, or rate/yield macro.

Triage (`tracefold.news.agents.triage_model`, `tracefold.news.triage_rules`)
never retrieves: the Deduper computes `event_status` (storyline window facts)
and the consumer passes it last in the human message. `decide()` owns the
final decision: noise -> drop; magnitude 3 -> escalate; high priority + push ->
escalate; unclear direction -> drop; magnitude >= 2 and actionable -> push;
watchlist primary and magnitude >= 1 -> push; storyline window-max throttle
(2 h push / 4 h escalate); hourly cap; control mutes. Model failure is
fail-closed (`rule_baseline`: watchlist primary or score >= 90 with grounded
assets) and three consecutive failures open a 60-second circuit that also opens
a `triage_circuit_open` incident. `news_verdicts` stores `model_decision`,
`rule_baseline_decision`, `final_decision`, `override_rule`, `throttled_by`,
`degraded`, and a trace with latency and cached tokens.

Analyst (`tracefold.news.agents.analyst`, `tools`, `analyst_rules`) uses
`create_deep_agent` with seven read-only tools (event card, members, find
events, prior verdicts, market reaction, macro state, watchlist), no subagents,
no filesystem/todo/sandbox tools, no checkpointer, and `ToolStrategy` terminal
output. Tool returns are bounded (2 s, 4 KB, clamped echoes) and register
evidence ids; `verify_verdict()` rejects unknown evidence, market numbers that
differ from tool output, disagreement without revision, and magnitude without
evidence. Deep verdicts only produce follow-up cards after the first delivery
was sent. `NEWS_ANALYST.md` is code-owned domain memory concatenated into the
system prompt.

Delivery (`tracefold.news.delivery`, `consumers.DelivererConsumer`) renders
code facts (original title, link, assets, direction, magnitude, sources) as
the card body and sanitizes AI copy (URLs fall back to the original title).
There is no retry: `news_deliveries(event_id, kind)` is inserted as `sending`
before the single HTTP call and settled `sent`/`terminal`; interrupted rows are
terminalized at startup. Recovery items, suppressed events, and paused/muted
control state never deliver; the hourly cap lets only escalates through.

Incidents and recovery: WSS transport/auth/protocol/idle failures, broker
backpressure/unavailability, and Triage circuit opens are rows in
`news_opennews_incidents`; reconnect closes transport incidents and requests
recovery, which pages the official Strategy hits endpoints for the closed
interval and publishes `raw.recovery.*` frames (`admission=recovery`, never
delivered).

Storage is exactly thirteen tables: `news_ingest_state`,
`news_opennews_incidents`, `news_items`, `news_events`, `news_event_members`,
`news_event_bands`, `news_event_assets`, `news_verdicts`,
`news_title_presentations`, `news_deliveries`, `news_control_state`,
`news_event_market_marks`, `news_event_labels`. Migration
`20260818_0275_news_v3_event_bus_hard_cut` drops the eleven legacy Story/
Brief/Push/Title tables and is irreversible. Read queries are registered in
`tracefold.news.query_specs` for the query audit.

Learning plane: `news_event_market_marks` capture t0/+5m/+30m/+4h price and OI
for candidate events with CEX targets; `tracefold news eval` reports
precision@push, missed-mover rate, and direction accuracy over stored verdicts;
`tracefold news replay <hits.json>` replays provider hits through Deduper+Gate
without a model or broker; `tests/fixtures/news_v3_hits_sample.json` is the
golden replay corpus.

### Macro

```text
code-owned Dataset Registry + Coverage Manifest
  -> one of six clock families
  -> macro_acquisition_targets claim
  -> free official / exchange / disclosed proxy adapter
  -> typed append-only Market or Macro fact + target cursor/current state
  -> static dataset -> calculation -> module dependency graph
  -> typed affected-module frontier
  -> one EDF module-local reducer
  -> one stable macro_module_current closure
  -> persisted-only overview and module reads

official FOMC / speech body + effective-dated role fact
  -> macro_document_analysis_jobs claim
  -> immutable evidence-bound document analysis
  -> institutional stance + officials communication distribution
```

The acquisition clock families are `intraday_market`, `daily_settlement`,
`scheduled_release`, `official_state`, `official_document`, and explicit
`backfill`. The five steady families are explicit private due loops over one
target table, not Worker objects or a uniform bundle poller. Claims use
`SKIP LOCKED`; provider I/O occurs outside database transactions; completion
atomically writes facts, cursor, and current target success/error state.
Unchanged source content writes zero fact rows. Revisions append a new fact and
never overwrite history.

Macro bounded history reads and the Calculation Registry's typed builder
contracts execute inside the six module payload builds, outside the write
transaction. There is no second generic feature-materialization engine. A
short compare-and-set write phase
publishes the stable module row and projection fingerprint. Unchanged module
payloads write zero serving rows.

The Dataset Registry fixes ownership, concept identity, source role, clock,
adapter, trust tier, freshness, criticality, and module membership in code.
Every concept has one primary current source and may have an explicitly labelled
official-history or proxy source. Dataset and source identities stay explicit
and are never blended. The Coverage Manifest contains only
capabilities that the supported free-data system can truthfully provide;
missing paid or unimplementable capabilities are deleted rather than displayed
as permanent product gaps. Operator config only enables source families;
cadence, lease, timeout, batch, and resource limits are code-owned.

OpenBB is not a runtime dependency or provider router. It does not supply data
or entitlement, and routing an existing source through it would add a second
provider/configuration/error layer without changing source identity. Macro uses
its direct, narrow adapters and never adds a provider waterfall around them.

The current nominal and real curves come from Treasury, with FRED as labelled
history. CPI and labor release facts come from BLS, while GDP, PCE, and core PCE
release facts come from BEA's public official release pages; the matching FRED
series are history only. Release timestamps are parsed from the official
release clock, never substituted with ingestion time.

Coverage (`complete`, `partial`), Current Health (`current`, `degraded`,
`unavailable`), and History Depth (`complete`, `partial`, `insufficient`,
`not_required`) are independent descriptive axes. Optional history cannot
degrade current-state health or reader-facing History Depth. Dataset rows also
expose market and source state;
closed and maintenance sessions do not age the last expected market bar against
wall time.

The six product modules are `rates_fed`, `economy_inflation`,
`liquidity_funding`, `credit`, `volatility`, and `cross_asset`. One code-owned
module descriptor fixes each ID, label, schema version, and builder key. Each
has one explicit typed payload (rates v8, economy v6, liquidity v5,
credit/volatility v7, and cross-asset v8), deterministic module-specific analysis, exact
market timestamps, natural publication cadence, source roles, importance
ranks with factor explanations, and evidence lineage. Release payloads keep expected, actual, surprise,
revision, publication time, and Registry-owned seasonal-adjustment semantics
distinct. ETF daily history is the Nasdaq public
five-year lane; Yahoo supplies ETF intraday prices and paired intraday/daily
continuous-contract futures proxies. No generic chart-array contract survives. The Calculation
Registry records every feature's inputs, formula version, windows, minimum
observations, units, gap policy, freshness, baseline, and output shape.
The Natural Change Calculation Registry separately fixes every Dataset's
cadence-native windows, minimum observations, formula, unit, revision/surprise
rules, bounded-gap policy, and output schema. Exact month/quarter lags cannot
fall back to an older available row while retaining the requested window label.
Treasury shape, matched breakevens, normalized asset returns, 30/90/252
common-daily-return correlations, credit ladder history, and funding
comparisons remain deterministic. Correlation pair facts are undirected and
exclude diagonals; the payload's presentation contract owns the default window
and mirrored unit-diagonal display rule. Credit exposes spread,
funding cost, bank supply, and borrower quality concurrently and never reduces
them to a score.

Rates v8 also carries the current official FOMC calendar snapshot and recent
Treasury auction-demand facts. The calendar adapter emits one immutable
revision across all meetings on the official page, so an official reschedule
replaces the prior snapshot in the read model without deleting its audit facts.
Auction bid-to-cover, Bill discount rate, investment rate, high yield, offering
amount, and bidder award shares enter the existing release-fact family from
Treasury Fiscal Data as distinct fields. The competitive
close is a scheduled clock; no result publication time is invented. SOFR has
one Registry owner and one fact identity while the dependency graph makes that
same fact available to both Rates and Liquidity.

Macro has no second judgment publication, daily narrative, or archive product.
The overview is a compact index over the six current module rows; each module
is a deterministic descriptive view over persisted facts.
One unavailable module affects only that module, and read requests never invoke
a provider, model, backfill, or projection.

Fed document analysis is the only model-derived Macro state. It receives one
bounded official body plus effective-dated role/prior-signal context, verifies
exact excerpts against that body, and inserts one immutable analysis for the
exact document/model/prompt identity. It feeds the descriptive `rates_fed`
module as a supporting capability and never gates official Rates/Fed current
health. Publication, completion of the native job, advancement of the derived
Dataset projection state, and dirtying the `rates_fed` frontier share one
transaction. A maintenance rebuild derives the same state from immutable
analyses and native jobs.

Migrations `20260801_0235` and `20260801_0236` are irreversible hard cuts: they
remove retired News acquisition and Macro derived/control history without
adding compatibility tables or readers.
Historical migration `20260801_0237` introduced a persisted OpenNews recovery
boundary, and historical migration `20260809_0247` replaced it with a bounded
12-hour ordinary-news overlap. Neither is the current acquisition contract:
`20260813_0265` removes ordinary-news REST overlap and records
unknown coverage without replay. Current migration `20260813_0266` supersedes
that status shape with explicit incidents and bounded official Strategy-hit
recovery; it still never uses ordinary News Search. `20260801_0238` adds the two News-owned
push-control tables with an
uninitialized baseline. Runtime initialization records that fence once, and
every bounded reconcile page suppresses selected evidence already persisted at
the fence without a network call.
Migration `20260810_0251` is the Rates v7 hard cut: it deletes the rebuildable
v6 `rates_fed` current/frontier rows and changes the database schema invariant
to v7. Typed Macro facts and immutable analyses are preserved and the sole
projection writer rebuilds the current row.
Migration `20260811_0252` converts unreachable legacy acquisition-target
states once, removes `invalid` from the live state machine, preserves the six
serving rows, and clears only their rebuildable frontiers. On startup the sole
projection writer reconciles those missing frontiers from persisted Dataset
projection state and republishes all six modules without provider I/O.
Migration
`20260811_0253` is the current semantic-contract hard cut: it invalidates only
the rebuildable Rates, Economy, and Cross-Asset current/frontier rows and
requires v8, v6, and v8 respectively. Material facts, targets, documents, and
analyses remain intact; there is no dual reader.

## Safety boundary

`events.raw_json` and `events.event_json` remain because historical events do
not yet have a proven one-to-one `raw_frames` source edge and locator. They may
be removed only after new writes persist the edge, historical coverage is
verified at 100%, and ambiguous payloads are exported as immutable evidence.
No runtime fallback path should be introduced meanwhile.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
