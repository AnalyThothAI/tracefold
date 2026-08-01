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
acquisition, the bounded external/model/CPU capabilities, singleton runtime
status, one fixed-period News Story writer, and one EDF projection
coordinator for the frontier-backed domains. Workers recover exclusively by
re-reading PostgreSQL facts, typed Radar/Macro/Profile frontiers, native News
Brief/Fed-document model state, and queues on bounded code-owned clocks.
There is no
database wake plane or in-memory correctness dependency. Provider raw frames
remain inputs until normalized and persisted as material facts.

The projection coordinator executes exactly one semantic shard at a time.
Every productive or failed turn waits on the fixed 250 ms bounded repoll
cadence before reading the typed frontier heads again. Idle polling is likewise
bounded by a code-owned cadence. These waits prevent a successful backlog from
becoming a hot loop; they are scheduling policy, not a correctness authority.

## Truth, control state, and derived state

Material facts include:

- evidence: `raw_frames`, `events`, `event_entities`;
- identity: `token_evidence`, `token_intents`,
  `token_intent_lookup_keys`, `token_intent_resolutions`,
  `registry_assets`, `asset_identity_evidence`, `asset_identity_current`;
- market: `market_ticks`, `enriched_events`, `market_observations`,
  `market_settlements`, and `market_position_facts`;
- news: idempotent OpenNews current facts in `news_items`;
- macro: revision-preserving `macro_series_facts`, `macro_release_facts`, and
  `macro_documents`.

Current read models are `token_radar_current_rows`, `token_profile_current`,
`market_tick_current`, deterministic `news_items`, `news_stories`, and
`news_story_members`, plus the six stable rows in `macro_module_current`.
Each uses stable product/window/target identity, has exactly one runtime
writer, is rebuildable from facts, and writes zero serving rows when its
business payload is unchanged.

Source connection health in `news_sources`, queues, leases, retries, native
model runs/jobs, and terminal events are control state. Typed Radar, Macro, and
Profile frontiers store stable
domain/shard identity, input fingerprint, earliest deadline, lease, failure,
and publication checkpoints. `first_dirty_at_ms` records the causal change,
`deadline_at_ms` is the freshness SLA, and `next_attempt_at_ms` is only an
eligibility clock for a scheduled recheck or retry. An eligible shard may run
before its deadline; the deadline is never a start gate. Radar source edges are
deterministic rebuildable state, not alternate business truth. Profile refresh
heat tiers, retry attempts, provider circuits, and terminal reasons are
likewise queue policy, not profile facts.
`news_brief_publications` and `macro_document_analyses` are immutable derived
model outputs keyed by frozen evidence; they are not material facts.

## Package map

```text
tracefold.market
  capture/       provider-neutral evidence ingestion
  identity/      token and asset identity resolution
  pricing/       append-only market facts and current prices
  profiles/      source-backed token profiles and image state
  radar/         transparent factor projection
  views/         persisted market read queries

tracefold.news
  sources.py        one code-owned OpenNews source identity
  classification.py deterministic keyword threat/category classifier
  identity.py       WorldMonitor-compatible 512-dimension title clustering
  ranking.py        55/20/15/10 importance and Top-8 selection
  brief.py          Chinese Brief fingerprint and citation index lock
  repository.py     PostgreSQL current Item, Story, source-health, and Brief state
  interface.py      sole external News read interface
  runtime.py        bounded OpenNews live/recovery and native Brief candidate
  projection.py     complete 96-hour WorldMonitor Story calculation
  story_store.py    current-only Story compare-and-publish store

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

SQL ownership follows the same boundary: Market owns the event, token, asset,
profile, price, Radar, collector, general cross-asset observation, and
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
- OpenNews current-item persistence plus deterministic Story
  membership/projection;
- immutable World Brief publication plus completion of its exact
  evidence/model/version attempt;
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
its deadline; a future that remains alive beyond that grace is fatal. Projection
turns therefore have phase-native deadlines and no aggregate fatal watchdog.

Projection claim leases cover the complete legal phase envelope: Radar uses
45 seconds, while Profile and Macro use 30 seconds each. The fixed-period News
Story writer has no frontier lease and retains its 25-second operation budget.

## Product flows

### Market and Token Radar

`market_tick_current` is transactionally maintained from append-only
`market_ticks`; it has no projection worker or dirty queue. Explicit bounded
fact replay rebuilds it.

```text
events + intents + resolutions + market facts
  -> stable Radar source edges
  -> claim up to 4 target frontiers for one window x venue
  -> compact scalar feature updates
  -> one complete Top-N rank
  -> hydrate wide JSON only for selected identities
  -> token_radar_current_rows + publication state
  -> Radar, Search, Token Case
```

The public Radar row is a transparent `factor_snapshot` built only from
persisted identity, social, and market facts. One bounded `window × venue`
micro-batch claims at most four target frontiers, within 10,000 source rows,
4 MiB materialized input, and 1 MiB compact output. Five seconds is a soft
whole-turn latency observation, never a fatal aggregate timeout; each DB and
CPU phase is governed by its native deadline plus the bounded completion grace.
The turn computes feature updates
and the complete compact-population rank outside write transactions, then
atomically publishes the closure and completes the exact claimed snapshots.
Each frontier retains its latest input fingerprint/version and earliest
deadline while a claimed snapshot runs; a changed latest input returns that
target to dirty after publication. There is no rank frontier or intermediate
publication state. Unchanged closures write zero serving rows.
Profile refresh targets use `hot`, `warm`, and `cold` queue tiers; missing and
error outcomes back off exponentially to a bounded terminal state, and only a
new evidence fingerprint reactivates that target. Radar rank, window,
watermark, and row-payload changes do not enter the Profile fingerprint:
Radar can dirty Profile only when the target enters or exits the deduplicated
serving-set union. Provider, image, identity, or Profile version changes own
the remaining Profile invalidations.

### News

News is a direct PostgreSQL-backed adaptation of WorldMonitor commit
`f73de5b7`, not an independent editorial ontology:

```text
OpenNews source
  -> persistent WSS with bounded queue and REST gap recovery
  -> reports materialize idempotent NewsItems
  -> provider annotations update bounded metadata on the current item
  -> complete 96-hour WorldMonitor title clustering every 60 seconds
  -> coherent current-only Story + members
  -> one flat global cursor Feed; category is a facet
  -> deterministic Top-8 Brief selection
  -> one Chinese World Brief publication
  -> /api/news/feed + /api/news/stories/{story_id}
     + /api/news/brief + /api/news/sources + /api/news/status
```

`NewsAcquisition` is the only NewsItem writer. It owns one persistent
authenticated WSS receiver, one 256-event in-memory queue, one
publisher, and one REST recovery loop under the same structured-concurrency
root. REST is gap-driven only: one bounded page (page 1, limit 100) after a
successful initial WSS connection, reconnect, or queue overflow. A healthy
continuous WSS session performs no periodic REST calls, and persisted source
state enforces at least five minutes between attempts. Even a continuous
reconnect storm is therefore bounded to 8,640 calls in 30 days, while a healthy
socket makes none. One page closes a gap only when it contains the persisted
provider-record boundary and the persisted gap version is unchanged; otherwise
News remains honestly degraded.
The stream socket does not consume a finite-operation permit; REST does.

OpenNews provider record ID is the current-fact identity. Reconnect and
REST/WSS overlap therefore update or no-op the same row. `news.ai_update`
updates only the bounded provider-source label, `score`, `signal`, `grade`, and
coin metadata on that row. Translations, `strategy.triggered`, non-news engine
events, and invalid or
stale reports are discarded rather than retained as audit history. Provider
metadata is descriptive and cannot affect identity, classification, Story, or
Brief. Linkless OpenNews dispatches remain valid current facts.

The sole Story writer loads all enabled NewsItems in the current 96-hour
window, computes outside the database, and compares the input fingerprint
again under the publication lock. It runs every 60 seconds and is bounded by
10,000 rows, 4 MiB input, and a 25-second operation budget. An unchanged input
writes zero serving rows; a stale snapshot writes nothing. There are no News
frontiers, identity-feature rows, similarity-edge rows, aliases, or
membership history.

NewsItem identity is `(source_id, provider_record_id)`. The existing `item_id`
is retained when this hard cut migrates an already known provider record.
Tracking parameters are removed from article links. Provider annotations
update only bounded metadata and never replace report content.

Story identity is the Python port of WorldMonitor's
`shared/story-identity.js`: normalized titles, deterministic signed FNV-1a
512-dimensional vectors, uniform and boosted cosine channels, threshold
`0.615`, exact-duplicate union, high-containment rescue, 250-item candidate
buckets, and deterministic union-find. A Story ID is the full SHA-256 of the
earliest normalized title in that current cluster. It may change when the
earliest item expires; the previous Story is removed and its detail route
returns not found. There is no archived Story product, embedding,
translation, full-article extraction, browser clustering, revision product, or
per-Story AI analysis.

Threat level and category use WorldMonitor's deterministic keyword classifier,
including exclusions and historical downgrade. There is no item-level AI
classifier or cache. Importance uses WorldMonitor's 55% severity, 20% source
tier, 15% corroboration, and 10% recency, followed by the narrow
diplomacy/flashpoint and entity-corroboration boosts. Corroboration and public
`source_count` count distinct reporting origins, so repeated OpenNews delivery
of one provider record counts once. Because Tracefold persists this otherwise
request-time score, recency uses the equivalent one-hour healthy-cache epoch;
unchanged input within an epoch writes zero serving rows. The API exposes the
scoring item and factor breakdown. Global clustering precedes filtering,
sorting, and keyset pagination; category is a facet, never a bucket or cap.

The native News Brief candidate selects at most eight Stories, caps one
representative reporting origin at three, and excludes opinion, feel-good,
and ephemeral live coverage. It requires at least three Stories from at least
two reporting origins before making a model call.
Its fingerprint binds the ordered Story state and the prompt/workflow/schema/
locale contract. An unchanged fingerprint makes no model call and no write.
The provider chain gets one bounded attempt per configured provider under one
60-second total budget. Output is Chinese, each line is index-locked to its
selected Story, malformed lines receive a deterministic local fallback, and an
empty set or provider failure preserves the last-known-good publication.
Publication history is immutable; failed runs cannot replace last-known-good.
`news_brief_current` is the single current pointer and the read contract
exposes `unavailable`, `insufficient_material`, `running`, `ready`,
`stale_fallback`, or `failed` honestly. `running` requires a current database
run with an unexpired lease and heartbeat. News Brief preserves the first-dirty
600-second debounce in native domain state; once due, the serial model arbiter
waits on the fixed 250 ms bounded cadence after every completed candidate. No
per-Story or Brief worker timer is a correctness authority.

The complete live News storage boundary is exactly twelve tables:
`news_sources`, `news_source_memberships`, `news_items`, `news_stories`,
`news_story_members`, `news_projection_summary`,
`news_story_facet_counts`, `news_source_facet_counts`,
`news_brief_selection_current`,
`news_brief_runs`,
`news_brief_publications`, and `news_brief_current`. The
`20260801_0234` removes incremental Story machinery. The current hard cut has
no downgrade or compatibility lane.

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

Macro history reads, Calculation Registry execution, and all six module payload
builds occur outside the write transaction. A short compare-and-set write phase
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
`liquidity_funding`, `credit`, `volatility`, and `cross_asset`. Each has one
explicit typed payload (rates v6, economy/liquidity v5, and
credit/volatility/cross-asset v7), deterministic module-specific analysis, exact
market timestamps, natural publication cadence, source roles, importance
ranks with factor explanations, and evidence lineage. Release payloads keep expected, actual, surprise,
revision, and publication time distinct. ETF daily history is the Nasdaq public
five-year lane; Yahoo supplies ETF intraday prices and paired intraday/daily
continuous-contract futures proxies. No generic chart-array contract survives. The Calculation
Registry records every feature's inputs, formula version, windows, minimum
observations, units, gap policy, freshness, baseline, and output shape.
The Natural Change Calculation Registry separately fixes every Dataset's
cadence-native windows, minimum observations, formula, unit, revision/surprise
rules, bounded-gap policy, and output schema. Exact month/quarter lags cannot
fall back to an older available row while retaining the requested window label.
Treasury shape, matched breakevens, normalized asset returns, credit ladder
history, and funding comparisons remain deterministic. Credit exposes spread,
funding cost, bank supply, and borrower quality concurrently and never reduces
them to a score.

Macro has no second judgment publication, daily narrative, or archive product.
The overview is a compact index over the six current module rows; each module
is a deterministic descriptive view over persisted facts.
One unavailable module affects only that module, and read requests never invoke
a provider, model, backfill, or projection.

Fed document analysis is the only model-derived Macro state. It receives one
bounded official body plus effective-dated role/prior-signal context, verifies
exact excerpts against that body, and inserts one immutable analysis for the
exact document/model/prompt identity. It feeds the descriptive `rates_fed`
module and is never a publication gate for the other five modules.

Migrations `20260801_0235` and `20260801_0236` are irreversible hard cuts: they
remove retired News acquisition and Macro derived/control history without
adding compatibility tables or readers.

## Safety boundary

`events.raw_json` and `events.event_json` remain because historical events do
not yet have a proven one-to-one `raw_frames` source edge and locator. They may
be removed only after new writes persist the edge, historical coverage is
verified at 100%, and ambiguous payloads are exported as immutable evidence.
No runtime fallback path should be introduced meanwhile.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
