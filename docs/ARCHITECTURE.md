# Architecture

Tracefold is one Python codebase/image with two mutually exclusive runtime
composition roots, one CLI, one React console, and one PostgreSQL database.
The architecture remains Kappa/CQRS: append-oriented material facts are the
only business truth; deterministic current views and immutable research
publications are derived state.

## Data flow

```text
providers / public streams
  -> tracefold workers
  -> PostgreSQL material facts + typed projection frontiers + native model state
  -> single-writer read models or immutable publications
  -> tracefold serve
  -> HTTP / persisted-live WebSocket / React
```

`tracefold serve` initializes only public HTTP/static/WebSocket, read
repositories, and serve telemetry. `tracefold workers` initializes ingestion,
acquisition, the bounded external/model/CPU capabilities, singleton runtime
status, and one EDF projection
coordinator. Workers recover exclusively by re-reading PostgreSQL typed
projection frontiers, native News/Thesis/Document model state, and queues on
bounded code-owned clocks. Startup performs no full
rebuild, backlog-clear loop, backfill, or phased load shifting. There is no
database wake plane or in-memory correctness dependency. Provider raw frames
remain inputs until normalized and persisted as material facts.

The projection coordinator executes exactly one semantic shard at a time.
Productive turns repoll the typed frontier heads immediately, while an idle
turn waits on the bounded polling cadence; backlog throughput therefore does
not depend on an artificial per-shard sleep.

## Truth, control state, and derived state

Material facts include:

- evidence: `raw_frames`, `events`, `event_entities`;
- identity: `token_evidence`, `token_intents`,
  `token_intent_lookup_keys`, `token_intent_resolutions`,
  `registry_assets`, `asset_identity_evidence`, `asset_identity_current`;
- market: `market_ticks`, `enriched_events`, `market_observations`,
  `market_settlements`, and `market_position_facts`;
- news: immutable acquisition observations in `news_feed_observations` and
  idempotent normalized current `news_items`;
- macro: revision-preserving `macro_series_facts`, `macro_release_facts`, and
  `macro_documents`.

Current read models are `token_radar_current_rows`, `token_profile_current`,
`market_tick_current`, deterministic `news_items`, `news_stories`, and
`news_story_members`, plus the six stable rows in `macro_module_current`.
Each uses stable product/window/target identity, has exactly one runtime
writer, is rebuildable from facts, and writes zero serving rows when its
business payload is unchanged.

Source configuration/fetch health in `news_sources`, queues, leases, retries,
fetch attempts, sync runs, terminal events, and agent checkpoints are control
or audit state. Typed Radar, Macro, News, and Profile frontiers store stable
domain/shard identity, input fingerprint, earliest deadline, lease, failure,
and publication checkpoints. `first_dirty_at_ms` records the causal change,
`deadline_at_ms` is the freshness SLA, and `next_attempt_at_ms` is only an
eligibility clock for a scheduled recheck or retry. An eligible shard may run
before its deadline; the deadline is never a start gate. Persisted News
identity features and similarity edges and Radar source edges are
deterministic rebuildable state, not alternate business truth. Profile refresh
heat tiers, retry attempts, provider circuits, and terminal reasons are
likewise queue policy, not profile facts.
`macro_thesis_publications` and `news_brief_publications` are immutable
derived research keyed by frozen evidence; they are not material facts.

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
  sources.py        WorldMonitor-derived catalog plus code-owned RSS sources
  classification.py deterministic keyword threat/category classifier
  identity.py       WorldMonitor-compatible 512-dimension title clustering
  ranking.py        55/20/15/10 importance and Top-8 selection
  brief.py          Chinese Brief fingerprint and citation index lock
  repository.py     PostgreSQL observation, item, Story, and Brief state
  interface.py      sole external News read interface
  runtime.py        bounded source acquisition and native Brief candidate
  projection.py     incremental identity/scoring reducer

tracefold.macro
  registry.py    code-owned Dataset Registry and six-module membership
  acquisition.py clock-driven claim, provider-I/O, receipt, fact and cursor flow
  calculations.py versioned calculation registry and transparent features
  projection.py  six current decision modules
  thesis.py      sealed Evidence Pack, Thesis, Live Delta, and Outcome Replay
  thesis_service.py one 08:50 New York immutable DeepAgents publication lifecycle

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
- one Macro acquisition completion: normalized fact insert, append-only source
  receipt, cursor advance, and compare-and-set target completion;
- current read-model write plus acknowledgement of the exact claim;
- News observation/item persistence plus deterministic Story
  membership/projection;
- immutable World Brief publication plus completion of its exact
  evidence/model/version attempt;
- immutable Macro Evidence Pack, independent review, Thesis publication, Live
  Delta, or Outcome Replay plus the corresponding stable session transition;
- retry or terminal transition plus mutation of its source queue row.

Provider, model, subprocess, filesystem, and network I/O occurs outside
database transactions.

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
4 MiB materialized input, 1 MiB compact output, and a five-second whole turn.
It computes feature updates
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
73 physical RSS/RSSHub sources / 73 logical memberships
  -> conditional direct fetch, then public-HTTPS-only relay fallback
  -> ETag / Last-Modified, first five entries
  -> immutable FeedObservation before admission
  -> idempotent NewsItem
  -> input-fingerprint comparison
  -> full 96-hour WorldMonitor title clustering only when changed
  -> canonical alias union
  -> coherent persistent Story + members + aliases
  -> one flat global cursor Feed; category is a facet
  -> deterministic Top-8 Brief selection
  -> one Chinese World Brief publication
  -> /api/news/feed + /api/news/stories/{story_id}
     + /api/news/brief + /api/news/sources + /api/news/status
```

The serving inventory is deliberately US-finance and global-event focused:
all crypto sources remain; US government/finance/politics, professional
technology/AI/layoff coverage, event-oriented security/intelligence, energy,
and crisis sources remain; Nikkei Asia, SCMP, Xinhua, and Al Jazeera are the
explicit regional exceptions. General local/regional news feeds are retired
from serving. Trump Truth Social is a tier-1 first-party source and enters the
ordinary Story/Brief lane without a separate corroboration gate.

The News acquisition due loop is the only NewsItem writer. Startup synchronizes the code-owned
source catalog, claims one due source, performs provider I/O outside
transactions under the global three-slot finite-operation capability, records one FetchReceipt per attempt,
and commits each source independently. Every parsed entry first becomes an immutable
`news_feed_observations` row. Missing title, non-HTTP URL, missing date, and
future time beyond one hour remain auditable rejected observations and never
become NewsItems. Valid entries older than 96 hours are persisted as
historical inactive NewsItems so acquisition loss stays distinguishable from
active-cluster eligibility.

The EDF News projection domain is the only Story/member/alias/feature/edge
writer. It runs ordered identity work before scoring, keeps candidate-pair
blocks at or below 4,096 and buckets at or below 250, and recomputes only
affected components and expiry closures. Pure calculation occurs outside the
write transaction. The CAS publication compares the input fingerprint again
and writes the complete affected component closure; unchanged input performs
zero serving writes.

The persisted one-hour scoring epoch is a single global clock expressed as at
most 64 stable `score-bucket` frontiers. Story IDs deterministically select a
bucket; timestamps never become serving or frontier identity. Each bucket
loads only current members, computes the same WorldMonitor factors outside the
database, and publishes changed item/Story score fields in set-based writes.
There is no per-Story timer fanout, scoring worker, or second scheduler.

WallStEngine is one ordinary tier-4 English source in the Finance membership.
Its fixed internal RSSHub user-timeline URL excludes replies and retweets,
keeps original and quote posts, and leaves quoted text in the RSS description
rather than the title. It uses the same five-entry cap, observations,
classification, Story source count, ranking, health, and Brief rules as every
other source. Internal HTTP/RSSHub URLs are direct-only; only code-owned public
HTTPS feed URLs may be sent to the external relay.

NewsItem identity is `(source_id, source_item_key)`, where the source item key
comes from GUID and canonical URL. Tracking parameters are removed. The
content fingerprint covers canonical URL, title, and description,
but deliberately excludes `pubDate`: a source timestamp drift produces a new
observation and zero NewsItem or Story writes.

Story identity is the Python port of WorldMonitor's
`shared/story-identity.js`: normalized titles, deterministic signed FNV-1a
512-dimensional vectors, uniform and boosted cosine channels, threshold
`0.615`, exact-duplicate union, high-containment rescue, 250-item candidate
buckets, and deterministic union-find. Persisted features and similarity edges
let the reducer close only affected components. Crossing the 250/251 boundary
dirties the complete bucket. Existing member ownership and seven-day title
aliases preserve a stable Story ID across representative changes; dropped
Stories are archived, not treated as current. There is no embedding,
translation, full-article extraction, browser clustering, revision product, or
per-Story AI analysis.

Threat level and category use WorldMonitor's deterministic keyword classifier,
including exclusions and historical downgrade. There is no item-level AI
classifier or cache. Importance uses WorldMonitor's 55% severity, 20% source
tier, 15% corroboration, and 10% recency, followed by the narrow
diplomacy/flashpoint and entity-corroboration boosts. Corroboration counts
distinct physical source IDs; logical memberships and parsed reporting-origin
metadata never increase it. Because Tracefold persists this otherwise
request-time score, recency uses the equivalent one-hour healthy-cache epoch;
unchanged input within an epoch writes zero serving rows. The API exposes the
scoring item and factor breakdown. Global clustering precedes filtering,
sorting, and keyset pagination; category is a facet, never a bucket or cap.

The native News Brief candidate selects at most eight Stories, caps one representative
physical source at three, and excludes opinion, feel-good, and ephemeral live
coverage. It requires at least three Stories from at least two physical
sources before making a model call.
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
run with an unexpired lease and heartbeat.
Typed Story frontiers are polled by the single EDF and productive work repolls
immediately. News Brief preserves the first-dirty 600-second debounce in native
domain state; once due, the serial model arbiter repolls after every completed
candidate. No per-Story or Brief worker timer is a correctness authority.

The complete live News storage boundary is exactly twelve tables:
`news_sources`, `news_source_memberships`, `news_source_fetches`,
`news_feed_observations`, `news_items`, `news_stories`,
`news_story_members`, `news_story_aliases`, `news_story_input_state`,
`news_brief_runs`,
`news_brief_publications`, and `news_brief_current`. The
`20260728_0210` current-schema baseline creates only this News model on an empty
database and has no downgrade or compatibility lane.

### Macro

```text
code-owned Dataset Registry + Coverage Manifest
  -> one of six clock families
  -> macro_acquisition_targets claim
  -> free official / exchange / disclosed proxy adapter
  -> typed append-only Market or Macro fact + source receipt + cursor
  -> static dataset -> calculation -> module dependency graph
  -> typed affected-module frontier
  -> one EDF module-local reducer
  -> one stable macro_module_current closure
  -> persisted-only overview and module reads

official FOMC / speech body + effective-dated role fact
  -> macro_document_analysis_jobs claim
  -> immutable evidence-bound document analysis
  -> institutional stance + officials communication distribution

08:50 America/New_York trading session
  -> cutoff-bounded six-module compilation
  -> immutable macro_evidence_pack_v3
  -> bounded immutable macro_research_input_v1
  -> one Thin DeepAgent graph / exactly one native structured model call
  -> time, evidence, contract, and write gates
  -> one immutable macro_thesis_v2 publication
  -> immutable Macro Live Delta v2 / Outcome Replay v2 snapshots
  -> current-only v2 reads and explicit immutable v1/v2 archive reads
```

The acquisition clock families are `intraday_market`, `daily_settlement`,
`scheduled_release`, `official_state`, `official_document`, and explicit
`backfill`. The five steady families are explicit private due loops over one
target table, not Worker objects or a uniform
bundle poller. Claims use `SKIP LOCKED`; provider I/O occurs outside database
transactions; completion atomically writes facts, receipt, cursor, and target
state. Unchanged source content writes zero fact rows while every attempt
retains a receipt. Revisions append a new fact and never overwrite history.
Macro history reads, Calculation Registry execution, and all six module payload
builds occur outside the write transaction. A short compare-and-set write phase
publishes the stable feature/module rows and projection fingerprint; unchanged
facts perform zero history loads and zero calculations.

The Dataset Registry fixes ownership, concept identity, source role, clock,
adapter, trust tier, freshness, criticality, and module membership in code.
Every concept has one primary current source and may have an explicitly labelled
official-history or proxy source. Source identities are reconciled with a
persisted receipt and are never blended. The Coverage Manifest contains only
capabilities that the supported free-data system can truthfully provide;
missing paid or unimplementable capabilities are deleted rather than displayed
as permanent product gaps. Operator config only enables source families;
cadence, lease, timeout, batch, and resource limits are code-owned.

The current nominal and real curves come from Treasury, with FRED as labelled
history. CPI and labor release facts come from BLS, while GDP, PCE, and core PCE
release facts come from BEA's public official release pages; the matching FRED
series are history only. Release timestamps are parsed from the official
release clock, never substituted with receipt time.

Coverage (`complete`, `partial`), Current Health (`current`, `degraded`,
`unavailable`), and History Depth (`complete`, `partial`, `insufficient`,
`not_required`) are independent descriptive axes. Optional history cannot
degrade current-state health or reader-facing History Depth; it remains in the
audit appendix. Dataset rows also expose market and source state;
closed and maintenance sessions do not age the last expected market bar against
wall time.

The six product modules are `rates_fed`, `economy_inflation`,
`liquidity_funding`, `credit`, `volatility`, and `cross_asset`. Each has one
explicit typed payload (`rates/economy/liquidity` v5, credit/volatility/cross
asset v7), deterministic module-specific analysis, exact
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

The Thesis is the only product-level Macro judgment. A deterministic bounded
ResearchInput preserves typed structures, exact cutoff-frozen facts, gaps,
prior material delta, catalysts, all twelve asset momentum rows, and a closed
condition-candidate registry without choosing the conclusion. The Thin model
returns one call/no-call mainline, one to three causal edges for a call, at most
one alternative, at most three tensions, sparse module assessments, and only
material asset outlooks. The deterministic compiler closes citations,
conditions, hashes, and stable IDs. Momentum and conditional outlook remain
separate for exactly SPY, QQQ, IWM, TLT, IEF, LQD, HYG, UUP, GLD, USO, BTC,
and VIX; non-material assets keep facts and a short deterministic no-call
reason instead of model filler.

The production graph is one Thin `create_deep_agent` composition with exactly
one provider-native structured model invocation per durable attempt. Business
tools, subagents, filesystem, todo, task, execute, search, summarization, and
checkpoint writes are absent. Reviewer is not a production gate or invocation;
existing v1 review rows remain immutable archive audit. The only post-envelope
publication gates are time identity, evidence closure, contract validity, and
write safety.

Post-publication updates never mutate the Thesis. Macro Live Delta only reports
condition-bound strengthening, weakening, or invalidation against cited
evidence; its public read projection preserves mainline, alternative, tension,
and asset scopes, and only mainline bindings determine mainline validity.
Event checkpoints never affect mainline validity. Outcome Replay emits only
declared 1W/1M horizons and only corresponding material outlook assets.
Current Recovery separately compares publication-time and current canonical
fact availability without changing the Thesis hash. Current routes resolve one
session and never inject a prior publication; older v1/v2 Thesis is available
only by explicit archive selection. One unavailable module degrades only its
evidence scope. Read requests never invoke providers or the graph. Missing
evidence, no-call, partial history, confidence, report length, Reviewer
absence, and offline score are descriptive rather than extra publication
gates.

## Safety boundary

`events.raw_json` and `events.event_json` remain because historical events do
not yet have a proven one-to-one `raw_frames` source edge and locator. They may
be removed only after new writes persist the edge, historical coverage is
verified at 100%, and ambiguous payloads are exported as immutable evidence.
No runtime fallback path should be introduced meanwhile.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
