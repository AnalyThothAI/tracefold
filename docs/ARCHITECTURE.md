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
Brief/Fed-document model state, the News Story-push state machine,
and queues on bounded code-owned clocks.
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
production React build. Migration, maintenance cutover, Serve, and Workers use
that exact image and build revision with different commands and credentials.
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
- news: idempotent OpenNews current facts in `news_items`;
- macro: revision-preserving `macro_series_facts`, `macro_release_facts`, and
  `macro_documents`.

Current read models are `token_radar_current_rows`, `token_profile_current`,
`market_tick_current`, `news_stories`, `news_story_members`, and the six stable
rows in `macro_module_current`. Each uses stable product/window/target identity,
has exactly one runtime writer, is rebuildable from facts, and writes zero
serving rows when its business payload is unchanged. `news_items` is a mixed
Article row: acquisition alone owns the canonical title, description, URL,
reporting origin, publication clock, and provider metadata, and initializes
required deterministic serving columns. Story consumes that canonical title
and persists only the full-window classification, importance, and activity
values. It never overwrites acquisition-owned facts.

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
`news_push_state` and `news_push_deliveries` are durable outbound control state:
they freeze no-backfill baselines, selected Story evidence, one optional
presentation-only Chinese title translation or explicit original fallback,
compact cards with provider coins, provider score, and an
optional original-link button, plus claims, retries, and explicit delivery
receipts. They
do not become a second Story read model or notification product.

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
  opennews.py       canonical OpenNews fact adapter
  classification.py deterministic keyword threat/category classifier
  identity.py       WorldMonitor-compatible 512-dimension title clustering
  ranking.py        first-stage factors and public Insights selector
  brief.py          public English L1/L2 composer and payload identities
  brief_store.py    singleton target/run/publication/LKG state machine
  repository.py     PostgreSQL current Item, Story, source-health, and Brief state
  interface.py      sole external News read interface
  runtime.py        bounded OpenNews live/recovery and native Brief candidate
  projection.py     complete 12-hour current WorldMonitor Story calculation
  story_store.py    current-only Story compare-and-publish store
  push.py           Story-qualified durable Feishu delivery

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
its deadline. A DB, model, CPU, cleanup, or otherwise unclassified future that
remains alive beyond that grace is fatal. Only an explicitly classified true
external provider seam may translate a finite-operation overrun into its
existing durable retry, degradation, or terminal policy; doing so never
releases the shared capability permit before the underlying future actually
finishes. Projection turns therefore have phase-native deadlines and no
aggregate fatal watchdog.

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

News is a direct PostgreSQL-backed adaptation of the public WorldMonitor chain
at commit `0e8785c43e6a693990a14181ae0a16066c15fc8c`, not an independent
editorial ontology or the personalized Digest Magazine pipeline:

```text
OpenNews source
  -> persistent WSS with bounded queue and REST gap recovery
  -> reports materialize one canonical headline/origin NewsItem fact
  -> provider annotations update bounded metadata on the current item
  -> complete 12-hour current WorldMonitor title clustering every 60 seconds
  -> coherent current-only Story + members
  -> public cluster evidence + importance/admissibility/recency selection
  -> at most eight server-ordered Top Stories with corroborated-lead reservation
  -> L1 structured synthesis/acceptance -> L2 single-headline fallback
  -> one whole immutable public Insights publication or whole LKG
  -> News-owned strict score>70 push state
     -> one bounded Chinese-title attempt or immediate original fallback
     -> signed or explicitly unsigned frozen Feishu card
  -> one flat global cursor Feed; category, severity, and reporting origin are facets
  -> /api/news/feed + /api/news/stories/{story_id}
     + /api/news/brief + /api/news/sources + /api/news/status
```

`NewsAcquisition` is the only writer of NewsItem Article facts and provider
metadata. The Story projection only owns the deterministic derived
classification, importance, and active-window columns on those rows. Acquisition
owns one persistent
authenticated WSS receiver, one 256-event in-memory queue, one
publisher, and one REST recovery loop under the same structured-concurrency
root. REST is gap-driven only: recovery reads sequential 100-item pages from
page 1, stops when it finds the persisted provider-record boundary, and never
reads more than 11 pages per attempt. A healthy continuous WSS session performs
no periodic REST calls, and persisted source state enforces at least five
minutes between attempts. Even a continuous 31-day reconnect storm is therefore
bounded to 98,208 calls, below the operator's 100,000-call monthly limit, while
a healthy socket makes none. Recovery closes a gap only when a page contains
the boundary and the persisted gap version is unchanged; exhausting the page
budget leaves News honestly degraded.
The stream socket does not consume a finite-operation permit; REST does.

OpenNews provider record ID is the current-fact identity. Reconnect and
REST/WSS overlap therefore update or no-op the same row. A report becomes one
fact: the first non-empty logical plaintext block is the title (maximum 500
characters), a cleaned explicit description or remaining blocks become the
bounded description, and reporting origin resolves from a Twitter author,
then `newsType`, URL host, or `opennews`. Linkless dispatches remain valid.
`news.ai_update`
updates only the bounded provider-source label, `score`, `signal`, `grade`, and
coin metadata on that row. OpenNews translation frames, `strategy.triggered`,
non-news engine events, and invalid or
stale reports are discarded rather than retained as audit history. Provider
metadata is descriptive and cannot affect identity, classification, Story,
importance, ordering, or Brief. It may qualify an already projected Story for
the separate outbound push state machine. Linkless OpenNews dispatches remain
valid current facts. Article admission retains valid OpenNews facts for up to
96 hours; the current Story closure uses the newest complete 12-hour window
only.

The sole Story writer loads all enabled NewsItems in the current 12-hour
window, carries only fields consumed by the calculation, normalizes canonical
titles deterministically in the CPU phase, and publishes that captured complete
snapshot under a load-time publication-order compare-and-set. Facts arriving
during calculation remain for the next 60-second turn; they do not invalidate
the coherent snapshot already calculated. If a newer captured snapshot has
already published, the older result is rejected as `superseded_snapshot` and
cannot overwrite it. The writer runs every 60 seconds and is bounded by
10,000 rows, 8 MiB input, and a 25-second runtime CPU budget. A narrow SQL
count/byte preflight rejects an oversized corpus before wide rows are fetched;
the exact encoded guard is checked again before calculation and publication. An unchanged input
writes zero serving rows; a superseded snapshot writes nothing. There are no News
frontiers, identity-feature rows, similarity-edge rows, aliases, or
membership history. WorldMonitor's 96-hour freshness floor is safe behind its
per-feed item bound; applying it unchanged to the aggregated OpenNews firehose
is not. The fixed 12-hour closure is a measured capacity boundary, not sampling
or adaptive truncation: every admitted Item inside it participates.

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
full-article extraction, browser clustering, revision product, per-Story AI
analysis, or localized-title state. Outbound Push may independently freeze a
Chinese presentation copy of its selected OpenNews Item headline, but that
delivery-local adapter adds no model-derived NewsItem or Story state.

Threat level and category use WorldMonitor's deterministic keyword classifier,
including exclusions and historical downgrade. There is no item-level AI
classifier or cache. Importance uses WorldMonitor's 55% severity, 20% source
tier, 15% corroboration, and 10% recency, followed by the narrow
diplomacy/flashpoint and entity-corroboration boosts. Corroboration and public
`source_count` count distinct reporting origins, so repeated OpenNews delivery
of one provider record counts once. Because Tracefold persists this otherwise
request-time score, recency uses the equivalent one-hour healthy-cache epoch;
unchanged input within an epoch writes zero serving rows. The API exposes the
scoring item and factor breakdown. Global clustering precedes search,
filtering, sorting, and keyset pagination; category, severity, reporting
origin, source, and strict provider-score qualification are facets or filters,
never clustering buckets or browser-side caps.

The Story transaction is also the only public selection writer. It treats each
current Story as one public `seed-insights` cluster, derives member titles,
distinct origins, entity corroboration, source tier, public category/threat,
second-stage importance, admissibility, and 16-hour effective-recency rank,
then selects at most eight. One primary reporting origin can occupy at most
three slots. If the normal Top Stories contain no eligible corroborated lead,
the highest-ranked eligible candidate is reserved and the result is restored
to public rank order. Drop counts distinguish admissibility, source-cap, and
overflow effects. This is one global public selection: there is no profile,
preference, embedding, topic grouping, entity veto, `(source, category)` quota,
client-side ISQ reorder, or promised topic/multi-source diversity.

The selection table is one singleton captured-current snapshot, not rank rows.
`selection_fingerprint` binds its projection revision/evaluation clock, every
ordered Top Story field and selection statistic, plus selector/identity
versions. `target_fingerprint` adds prompt, workflow, composer, schema, and
locale versions. `publication_id` hashes the complete sealed publication
payload—Top Stories, content kind, sources, source-age range, validation,
provenance, provider/model, and versions—but not database clocks, run telemetry,
or the ID itself. Unchanged selection and publication payloads write zero
serving rows.

L1 sends only the ordered primary headlines, primary sources, and distinct-
source counts. Its tolerant parser and citation-scoped composer are both the
provider acceptance gate and final publisher: a rejected response advances to
the next provider, while a missing or proper-noun-invalid per-Story line falls
back only to that Story's headline. Accepted L1 output is English, has one
index-locked line and source slot per Top Story, and retains an empty URL slot
instead of shifting citations. The code-owned waterfall is Ollama
`llama3.1:8b`, optional OpenRouter `deepseek/deepseek-v4-flash` with reasoning
disabled, then optional Groq `llama-3.3-70b-versatile`. The direct global
OpenAI-compatible provider is not a Brief provider.

L1 and the corroboration-gated single-headline L2 fallback share one 60-second
budget with a five-second guard and bounded provider retries/Retry-After. L2
accepts the first transport-valid minimum-length prose, then applies only its
proper-noun/headline fallback; it has no L1 composer, no Story lines, and at
most one valid source. L1 produces `quality=ok`; L2 or no-text produces
`quality=degraded`. Empty selection publishes nothing, and a non-empty
selection without an eligible lead makes no model call: it can advance a
complete no-text degraded snapshot only when no healthy LKG exists.

`news_brief_current` owns one complete current/LKG publication pointer plus the
active target and bounded run telemetry; it never stores a second Top Story
list. One mutable run per target uses `waiting_input`, `running`, `retry_wait`,
or `published`, with outcome and pointer action recorded separately. A new
target observes a 10-minute debounce, degraded current targets retry no more
than every 30 minutes, a fenced lease lasts 120 seconds, and failure telemetry
is capped without permanently terminalizing the target. A degraded publication
may advance only when no healthy publication exists. With a healthy LKG, a
degraded run preserves the entire older snapshot—Top Stories, brief, sources,
provenance, and clocks—without replacement or freshness refresh. The public
read state is exactly `unavailable`, `current`, `degraded`, or
`last_known_good`; it exposes one publication and bounded latest-run/target
telemetry, never publication history or mutable Story joins.

`NewsStoryPush` is a News-owned durable outbound worker, not a model candidate
or generic Notifications service. On first enablement it atomically suppresses
every currently eligible Story and records the initialized baseline without
network work. Later it
qualifies a Story only when the maximum numeric OpenNews provider score among
its current members is strictly greater than 70 and the selected Item was
published after the baseline and within the code-owned 15-minute live-alert
window. Older Items discovered through REST recovery or a later Story identity
are durably suppressed, never backfilled to Feishu. It chooses the
highest-scored member with deterministic publication-time and Item-ID ties, and
freezes that Story/Item evidence once. Its code-owned 10-second reconcile reads
only persisted current Story membership and NewsItem metadata; it neither rebuilds
Stories nor adds another acquisition path. A private durable-due loop claims
and delivers without occupying the serial model arbiter. For an unfrozen,
non-Chinese title, the Feishu Adapter makes at most one request through the
existing global OpenAI-compatible `llm.api_key`, effective `llm.base_url`, and
`llm.news_brief_model`; there is no second translation endpoint, credential,
or model configuration. The request timeout is 7.5 seconds inside an 8-second
total translation budget. Success freezes a Chinese header plus visible
original; any provider, validation, length, or timeout failure freezes and
sends the original in the same turn. Only the selected title enters the model
request. Its compact body renders the selected Item's valid
OpenNews coin symbols, preserving provider order and deduplicating by case,
plus the provider score and one original-link button when a canonical HTTP(S)
Item URL exists. Items without valid coins show `未提供`; a missing URL omits
the button.

Translation preparation and Feishu delivery both run outside the database
transaction through the finite-operation capability. After preparation, the
worker rechecks the 15-minute source window before freezing. Every later retry
receives only the frozen compact card and never retranslates. When the
optional signing secret is present, it adds the Feishu timestamp/signature
pair; otherwise it sends an explicitly unsigned request with neither field. It
never downgrades a failed signed request into an unsigned retry. The frozen
envelope persists only the non-secret `auth_mode`; timestamp, signature,
secret, and webhook remain runtime-only. A retry with a different configured
mode terminates before
network submission. Explicit Feishu `code == 0` is the only success. Transport
failures, timeouts, HTTP 429, and 5xx are bounded
retries; deterministic configuration, signing, authentication, and card errors
are terminal. The semantics are durable at-least-once: an ambiguous response
loss may duplicate externally, while a confirmed success is never resent.
The delivery key remains current `story_id`, while a selected Item already
present anywhere in the durable ledger cannot qualify again after cluster
membership changes produce a new Story ID. No historical Story alias or
membership-overlap identity is introduced. A non-unique selected-Item lookup
index keeps that permanent-ledger check bounded while retaining existing audit
rows.
The existing ledger column/status names `translation_status` and
`pending_translation` encode payload preparation without adding another queue:
new candidates use `pending`; after resource admission, translation dispatch
first commits `attempted` immediately before executor submission, then freezes
as `translated` or `unavailable`. Chinese/pre-call fallback can move directly
to `not_needed` or `unavailable`; stale rows use `not_requested`. Recovery of
an unfrozen `attempted` row conservatively freezes an interrupted original-title
fallback and never submits the model again. Provider failure never creates a
durable translation retry.

The current numeric provider-score value has its own
`provider_score_updated_at_ms` fact clock. Story writes cannot move that clock;
candidate creation freezes it as `threshold_observed_at_ms`, so the local
high-score-fact-to-terminal latency includes Story waiting. Translation v2
uses the durable dispatch fence as its attempt clock and freezes elapsed
milliseconds after a normal provider outcome. A crash in the tiny interval
between that fence and executor submission is deliberately counted as an
interrupted failure—the conservative cost of guaranteeing at-most-one provider
dispatch. `/api/news/status` derives the rolling 24-hour attempt success ratio,
P95 latency, and bounded failure-code counts from those existing delivery
envelopes; pre-call title-length or admission degradation is excluded.
The companion clean-v2 delivery rollup targets P95 at or below 90 seconds.
Every delivery attempt rechecks the 15-minute Article deadline immediately
before external submission; an aged frozen retry becomes suppressed without a
network call. Current waiting work whose score-fact clock is older than 120
seconds is a stalled operating state, even when a retry is scheduled for the
future.

The complete live News storage boundary is exactly thirteen tables:
`news_sources`, `news_items`, `news_stories`,
`news_story_members`, `news_projection_summary`,
`news_story_facet_counts`, `news_source_facet_counts`,
`news_brief_selection_current`,
`news_brief_runs`,
`news_brief_publications`, `news_brief_current`,
`news_push_state`, and `news_push_deliveries`. The
`20260801_0234` migration removes incremental Story machinery,
`20260801_0237` persists the bounded OpenNews recovery boundary, and
`20260801_0238` adds the durable push baseline and delivery ledger.
`20260806_0243` removes RSS execution/scheduling and source-membership schema
while retaining disabled historical source identity; `20260806_0244` adds
dedicated provider-score and Story-success clocks. `20260807_0246` canonicalizes
retained OpenNews facts, removes the retired reading-layer title table and
incompatible Brief state, rebuilds Story state, and installs the singleton selection plus
discriminated L1/L2/none publication contract without altering the two Push
tables. The current hard cut has no downgrade or compatibility lane.

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
Migration `20260801_0237` makes OpenNews recovery bounded and durable;
`20260801_0238` adds the two News-owned push-control tables with an
uninitialized baseline, so the first enabled reconcile suppresses the current
eligible set without a network call.

## Safety boundary

`events.raw_json` and `events.event_json` remain because historical events do
not yet have a proven one-to-one `raw_frames` source edge and locator. They may
be removed only after new writes persist the edge, historical coverage is
verified at 100%, and ambiguous payloads are exported as immutable evidence.
No runtime fallback path should be introduced meanwhile.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
