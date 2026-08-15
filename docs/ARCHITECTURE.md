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
lane, an isolated News Story CPU lane when News is enabled, singleton runtime
status, dirty-triggered News Story and after-completion Token Radar writers, and one
EDF projection coordinator for the remaining frontier-backed domains. The
News Story load/full-publication work uses the fixed heavy-operation permit
before the unchanged two-slot business executor. Token Radar uses ordinary
business-database admission and an isolated CPU process turn, without a Radar
permit, service deadline, executor, or configuration surface. Workers
recover exclusively by re-reading PostgreSQL facts, typed Macro/Profile frontiers, native News
Brief/Fed-document model state, the News Item Push ledger,
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
- news: canonical current Article facts admitted by the operator's exact
  OpenNews Strategy allowlist and, when explicitly enabled, the code-owned
  public RSS breadth/corroboration catalog in `news_items`;
- macro: revision-preserving `macro_series_facts`, `macro_release_facts`, and
  `macro_documents`.

Current read models are `token_radar_current`, `token_profile_current`,
`market_tick_current`, `news_stories`, `news_story_members`, and the six stable
rows in `macro_module_current`. Each uses stable product/window/target identity,
has exactly one runtime writer, is rebuildable from facts, and writes zero
serving rows when its business payload is unchanged. `news_items` is a mixed
Article row: acquisition alone owns the canonical title, description, URL,
reporting origin, publication clock, and provider metadata, and initializes
required deterministic serving columns. Story consumes that canonical title
and persists only the full-window classification, importance, and activity
values. It never overwrites acquisition-owned facts.
OpenNews's raw `coins` annotation remains source evidence in `news_items`; the
public News read adapter exposes that bounded annotation as generic `assets`
because provider symbols can represent crypto, equities, or commodities. It
does not classify, validate, or correct those labels. Each accepted provider
event also retains a bounded, deterministic, sorted-unique union of the matching
Strategy ID, name, source type, and observed engine type. It never persists the
full Strategy definition or metrics payload.

Source connection and Strategy-history state in `news_sources`, explicit
OpenNews incident intervals in `news_opennews_incidents`, queues, leases,
retries, native model runs/jobs, and terminal events are control state. Typed Macro and Profile
frontiers store stable
domain/shard identity, input fingerprint, earliest deadline, lease, failure,
and publication checkpoints. `first_dirty_at_ms` records the causal change,
`deadline_at_ms` is the freshness SLA, and `next_attempt_at_ms` is only an
eligibility clock for a scheduled recheck or retry. An eligible shard may run
before its deadline; the deadline is never a start gate. Token Radar has no
durable source-edge row, dirty frontier, claim, lease, quarantine, failure, or
source-stream state: its fixed-period writer rereads a bounded fact window and
publishes only complete successful snapshots. Profile refresh heat tiers, retry attempts, provider circuits, and terminal reasons are
likewise queue policy, not profile facts.
The sealed `news_brief_current.served_payload` and rows in
`macro_document_analyses` are derived model outputs bound to frozen evidence;
they are not material facts.
`news_push_state` and `news_push_deliveries` are durable outbound control state:
they freeze a no-backfill delivery epoch and one immutable `news_item_push_v1`
snapshot for each eligible first-live OpenNews Item. A private turn may freeze
one Chinese-title or original-title presentation decision, fence one Feishu
attempt, and persist a sanitized sent or terminal outcome. There is no retry,
lease, rendered-card persistence, Story identity, or public notification
product in this ledger.

## Package map

```text
tracefold.market
  capture/       provider-neutral evidence ingestion
  identity/      token and asset identity resolution
  pricing/       append-only market facts and current prices
  profiles/      source-backed token profiles and image state
  radar/         bounded change reducers and compact current snapshots
  views/         persisted market read queries

tracefold.news
  sources.py        pinned WorldMonitor public RSS catalog and OpenNews identity
  opennews.py       canonical OpenNews fact adapter
  classification.py deterministic keyword threat/category classifier
  identity.py       WorldMonitor-compatible 512-dimension title clustering
  ranking.py        first-stage factors and public Insights selector
  brief.py          public English L1/L2 composer and payload identities
  brief_store.py    UTC half-hour frozen-slot/current-LKG singleton
  repository.py     PostgreSQL current Item, Story, source-health, and Brief state
  runtime.py        bounded RSS/OpenNews acquisition and native Brief candidate
  projection.py     public population, Story, and selection calculation
  story_store.py    current-only Story compare-and-publish store
  push.py           Item-scoped durable one-attempt Feishu delivery

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
- one RSS source snapshot or one accepted OpenNews Strategy trigger plus its
  canonical current NewsItem fact and provenance union;
- one coherent Story/member/selection closure plus every newly admitted v2
  Push outbox row;
- one fenced half-hour Brief slot completion against its frozen selection and
  one whole served current/LKG payload;
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
deadlines and no aggregate fatal watchdog. Token Radar has no application-level
phase or whole-turn deadline; native PostgreSQL statement safety remains in
force.

Projection claim leases cover the complete legal phase envelope: Profile and
Macro use 30 seconds each. The after-completion Token Radar loop and fixed-period
News Story writer have no frontier lease.

## Product flows

### Market and Token Radar

`market_tick_current` is transactionally maintained from append-only
`market_ticks`; it has no projection worker or dirty queue. Explicit bounded
fact replay rebuilds it. The bounded market poll reserves its first batch slots
for the current Radar target keys and fills the remainder by 24-hour activity.
This is acquisition scheduling only: market presentation facts do not affect
Radar admission or order, so the dependency cannot feed a market result back
into queue membership. It does not create a 24-hour Radar window; the sole
Radar product remains the fixed four-hour causal comparison.

```text
events + intent resolution revisions
  -> bounded twelve-hour source-time/revision evidence read
  -> seed adjacent four-hour prior/current state at t-4h from the first eight hours
  -> replay availability-ordered changes over the final four-hour transition to t
  -> boundaries/removals may close; positive additions may open only on Gate false -> true
  -> four-hour episode TTL and suppression until a later positive false -> true crossing
  -> qualified_at descending + stable target-key ties -> Top 50
  -> bounded selected-target identity and market-presentation hydration
  -> atomic token_radar_current v5 singleton (maximum 50 Items / 96 KiB)
  -> Token Radar -> focused Token Case evidence
```

Token Radar is a change-first research queue, not a score, trading action,
market screener, security audit, or operational monitor. Every 30 seconds its
sole writer streams at most 20,000 typed Event/resolution-revision rows and 16
MiB from a twelve-hour source-time horizon, then runs one deterministic reducer.
The evidence load selects only
reducer fields in stable replay order against a source-time covering index and
the covering resolution index. The material read selects the STORED generated
`events.token_radar_text_fingerprint` column with the exact ASCII-lower,
whitespace-normalization, and MD5 semantics of its fixed-width, non-security
duplicate-text fingerprint. The partial source-time index INCLUDEs that column,
so vacuum-visible history can remain an Index Only read without fetching or
transferring the wide Event text payload. A turn always executes load, CPU
reduction, presentation, then publish. Failures log bounded structured detail
and the independent scheduler retries after its next natural 30-second wait;
they do not write serving state or stop Workers. Source event time defines current `[t-4h, t]`
and prior
`(t-8h, t-4h)` windows. Replay starts at `t-4h`: the first eight hours of the
source horizon seed the adjacent prior/current state at that boundary, and the
last four hours are the replay transition to `t`. This causal reconstruction
does not create a third public comparison window. A representative live
twelve-hour raw-text read measured approximately 11,975 rows and 8.62 MiB;
that observation motivated both the fixed-width fingerprint load and the
increase of the code-owned input envelope to 20,000 rows and 16 MiB from the
retired v3 envelope. A fact becomes available at the later of the Event
creation clock
and its eligible resolution creation clock;
`evidence_available_at_ms = max(event.created_at_ms,
eligible_resolution.created_at_ms)`, and that clock determines `qualified_at`.
Received time is used only for the live boundary:
`0 <= received_at_ms - source_event_at_ms <= 120000` and
`0 <= resolution.created_at_ms - received_at_ms <= 120000`. Resolution revisions are
replayed directly: a timely retarget removes the old binding and may add the
new one, a late retarget only removes, and a same-target refresh preserves an
already timely binding. There is no second availability ledger.

The fixed four-hour period is product semantics, not configuration. The retired
one-hour period made short sparse bursts and boundary movement too dominant and
expired an episode before a slower operator investigation could finish; a
twenty-four-hour comparison would mix old attention into `why_now` and weaken
the false-to-true causal interpretation. Selectable periods would also require
independent replay, Gate, episode-TTL, and serving identity per period rather
than a harmless frontend filter. Tracefold therefore owns exactly one four-hour
Radar product; longer-horizon fact inspection remains in Search and Token Case.

The code-owned Gate remains four equal-weight Boolean rules: minimum attention
delta, minimum independent authors, maximum duplicate-text share, and maximum
time to the required author. At one effective millisecond, window boundaries
and binding removals run first and can only close a case; positive additions
then apply atomically and can qualify only when the complete Gate changes from
false to true. When multiple additions share that millisecond, the stable
Event/intent/resolution/target fact-key order chooses the representative
trigger. A qualified episode leaves immediately when the Gate becomes false and
expires four hours after qualification. Expiry while the Gate remains true
suppresses the target until a later false state and new positive false-to-true
crossing. This episode is reconstructed on each bounded replay;
there is no episode, frontier, rejected-candidate, Gate-audit, or history table.
Authors have equal weight. Within the admitted provenance/action set, action,
follower, and provider labels cannot affect the Gate or order; neither can
composite scores, fuzzy similarity, model output, or market facts.

Items are ordered by `qualified_at` descending with stable target-key ties.
Only after Top-50 selection does one bounded batch read load canonical identity,
the exact trigger price anchor, optional profile presentation, current price,
and recent market capitalization. The recent positive market-cap lookup makes
at most one target-index LATERAL probe for each selected market key rather than
a global recent-tick scan; it never performs one query per Item. Market
values and their independent observation clocks are nullable presentation
facts and cannot change membership or order. The compact public Item contains
the causal trigger Event ID, trigger source-event time, qualification time,
current/prior four-hour mention change, actual independent-author/text counts,
propagation time, duplicate share, and that presentation packet. It contains
no rank, decision, score, per-rule evaluation history, rejected candidates,
source-event list, window, venue, pagination, archive, or user-adjustable
parameter. The Radar Module exposes only its one-turn `sample` interface to
Workers; evidence, presentation, publication, and serving operations remain
private PostgreSQL Adapter seams.

Publication locks the one stable product row and replaces the complete compact
v5 payload atomically. The singleton contains only its key, complete payload,
snapshot fingerprint, and changed-success timestamp. A business-identical
payload writes zero serving rows. Load, reduction, presentation, or publication
failure leaves the last successful snapshot untouched; no public state,
failure counter, stale reason, attempt clock, or Radar telemetry is persisted.
The hard-cut initial value is a valid empty v5 snapshot. Restart recovery is the
next bounded PostgreSQL replay. Search and Token Case
read their owning facts directly and never use Radar current state as evidence
authority. Radar v5 changes no WebSocket route, message, or subscription
behavior. There is no Stocks product, route, public read model, or writer.
`us_equity_symbols` remains only an identity-collision guard for token
resolution and does not constitute a Stocks surface. General cross-asset Market
facts and the six Macro modules remain unchanged.

Historical migration `20260810_0249` was the irreversible Radar serving hard
cut. It removes the six retired Radar projection tables and their terminal rows,
removes the temporary replay-only columns installed by `20260810_0248`, and
creates the one empty compact singleton. Material Events, intents, resolutions,
identities, and market facts are unchanged. At that revision the writer rebuilt
from its then-current two-hour fact window; there was no history import, dual
read/write, compatibility adapter, staging runtime, or legacy fallback.

Historical migration `20260810_0250`, directly after `0249`, was the v2 product
hard cut. It reset the v1 singleton to one empty
`token_radar_snapshot_v2`, installed the fifty-Item schema invariant, and
dropped the three Stocks-only derived tables
`stock_attention_target_features`, `stocks_radar_current_rows`, and
`stocks_radar_publication_state`. It preserved all material Events, intents,
resolutions, identities, profile facts, and market facts, including the
`us_equity_symbols` collision guard. No v1 or Stocks compatibility interface
was retained.

Historical migration `20260811_0254` superseded that serving contract with one
irreversible v3 hard cut. It reset only the rebuildable singleton to initial
`unavailable`, added the bounded source-time evidence index and v3 basic-shape
constraints, and preserved Events, intents, all resolution revisions,
identities, profiles, and market facts. At that revision the first successful
Worker sample reconstructed causal state from a bounded three-hour horizon for
one-hour current/prior windows and a one-hour episode TTL. No v2 trigger was
imported, and no dual reader/writer, episode/frontier/history table, Gate audit,
or compatibility path was installed.

Historical migration `20260812_0255` was the irreversible v4 hard cut. It reset only the
rebuildable singleton to initial `unavailable` with
`token_radar_snapshot_v4`, rebuilds the bounded source-time index as the narrow
fingerprint covering index, installs the covering resolution index for the
optimized bounded load, and preserves all material Events, intents,
resolution revisions, identities, profile facts, and market facts. The v4
runtime has exactly one fixed four-hour causal product and no window query or
control. Its first successful sample reconstructs state from the twelve-hour
fact horizon by seeding at `t-4h` and replaying the final four-hour transition;
it imports no v3 trigger or LKG payload. There is no dual reader/writer,
feature flag, compatibility adapter, staging runtime, or history import.

Migration `20260814_0269` is the irreversible v5 KISS hard cut. It preserves
all material facts and replaces the rebuildable v4 singleton with the exact
empty v5 payload plus its canonical snapshot fingerprint. The complete public
root has exactly `schema_version`, `social_evidence_as_of_ms`,
`eligible_total`, and `items`. It removes ruleset/input/state
fingerprints, attempt/failure state, workload counts, evaluation/state-change
clocks, and creation metadata. There is no public state envelope,
compatibility reader, or dual write.

Profile refresh targets use `hot`, `warm`, and `cold` queue tiers; missing and
error outcomes back off exponentially to a bounded terminal state, and only a
new evidence fingerprint reactivates that target. Profile eligibility and
invalidation come from identity/profile facts and Profile-owned policy; the
Radar writer only batch-reads already published `token_profile_current` rows
for optional name/icon presentation. It never calls a profile provider, performs
per-Item hydration, or drives Profile lifecycle.

### News

News is a direct PostgreSQL-backed adaptation of the public WorldMonitor chain
at commit `0e8785c43e6a693990a14181ae0a16066c15fc8c`, not an independent
editorial ontology or the personalized Digest Magazine pipeline:

```text
OpenNews account Strategies
  -> authenticated persistent WSS; server automatically sends strategy.triggered
  -> no application subscribe/request; literal ping/pong and RFC control heartbeat only
  -> exact configured multi-Strategy allowlist
  -> first live Item insert + same-transaction Item Push outbox when available
     -> one best-effort Chinese-title attempt or immediate original fallback
     -> one signed or explicitly unsigned Feishu attempt; sent or terminal
  -> operator-bound, Strategy-qualified 12-hour current facts
  -> disconnect/overflow/outage records an explicit incident interval
  -> official Strategy list/hits recovery; no ordinary-news Search

WorldMonitor full/en + INTEL RSS catalog
  -> disabled by default; enable only with news.rss_enabled: true
  -> 179 physical feeds / 183 category memberships
  -> first five entries per feed before validation
  -> 96-hour breadth and corroboration snapshots

RSS category membership expansion
  -> deterministic score before per-category Top 20
  -> physical union with OpenNews
  -> coherent current-only Story + members after a 1-second dirty debounce
     with a 5-minute safety pass
  -> public UTF-16 title-length gate + same-kernel seed clustering
  -> public cluster evidence + importance/admissibility/recency selection
  -> at most eight server-ordered Top Stories with corroborated-lead reservation
  -> UTC half-hour slot freezes the current selection
  -> Ollama -> configured direct DeepSeek -> Groq L1 waterfall
  -> shared-budget L2 single-headline fallback
  -> one whole sealed public Insights payload or whole LKG
  -> one flat global cursor Feed with server-side filters
  -> /api/news/feed + /api/news/stories/{story_id}
     + /api/news/brief + /api/news/sources + /api/news/status
```

`NewsAcquisition` is the only writer of NewsItem Article facts and provider
metadata. The Story projection owns only deterministic derived classification,
importance, and active-window columns on those rows. RSS source identity is the
HTTPS feed URL. The pinned catalog is exactly WorldMonitor
`full/en + INTEL_SOURCES`: 179 physical feeds, 183 category memberships, 178
reporting-source names, and 17 categories. `news.rss_enabled` is its only
runtime switch and defaults to `false`; disabled reconciliation removes those
sources from the active inventory and releases any prior claim. The acquisition
clock still expires old Article facts but cannot claim or fetch RSS. When
enabled, a bounded RSS turn claims one due
source, conditionally fetches it, and takes the first five RSS/Atom entries
before validation. A retained entry requires a non-empty title and a parseable
date no more than one hour in the future; its link is optional but, when kept,
must be HTTP(S). A first-five window with no titled entry is a parse failure,
not a successful empty snapshot. The successful accepted set atomically
replaces that source's current snapshot. `304` and unchanged accepted snapshots
write no NewsItem facts. Fetch failure preserves the last successful snapshot
until the 96-hour floor expires. Initial and bounded redirect hops are checked
as public HTTPS and must resolve only to globally routable addresses before the
Adapter sends them.

OpenNews is an operator-bound, Strategy-qualified low-latency acquisition lane.
Its persistent authenticated WSS receiver feeds one bounded queue. After the
protocol handshake Tracefold sends no `news.subscribe`, `news.unsubscribe`,
`strategy.subscribe`, `strategy.triggered`, or other application request. It
may answer a provider literal `ping` with literal `pong` and use RFC WebSocket
control-frame heartbeat; neither is a subscription. The provider automatically
sends the account owner's `strategy.triggered` notifications. Admission requires a non-empty `params.id` plus a nested
`params.strategy.id` in the exact configured `news.opennews_strategy_ids` set.
Configured and wire IDs are trimmed opaque strings and mutable Strategy names
are never admission keys. An allowlisted NEWS, MARKET, meme, or listing frame is
accepted regardless of `engineType`; raw `news.update`, `news.ai_update`,
acknowledgements, malformed frames, and unconfigured Strategies are ignored.
The current cutover allowlist is exactly `1018` (News Score > 70) and `1019` (OI
Event Monitor). Provider-side Listing/Storage Strategies are outside this input
population unless a future explicit configuration change admits them.
The account page remains the Strategy-definition authority, and Tracefold does
not reproduce its score, OI, keyword, symbol, venue, or time-window rules.

`params.id` remains the source-local material fact identity;
`params.strategy.id` is provenance, never fact identity. Repeated delivery of
the same provider event writes one NewsItem. Exact replay writes nothing, while
the same event observed under multiple configured Strategies merges a
deterministic sorted-unique provenance union without last-write erasure.
If wrappers for one provider event disagree, one complete wrapper wins by a
deterministic provider-evidence order (numeric score, non-empty assets, then
canonical payload); fields from different wrappers are never spliced together.
Different provider event IDs remain distinct facts even when their text is
similar. Linkless MARKET/OI reports are valid, and neither `newsType=strategy`
nor the wrapper Strategy name becomes the reporting origin. Provider score,
signal, grade, assets, source, title, timestamp, and link remain bounded
descriptive metadata. Strategy admission changes the material input population;
it does not change Story identity, importance, ordering, or Brief. Numeric score
remains only an optional explicit Feed focus filter and display label.

The WSS receiver and PostgreSQL publisher are independent tasks joined by one
bounded in-memory queue. Database admission/backpressure retains the connected
socket and retries the same pending batch with its original observation clock;
only overflow opens a `buffer_overflow` incident, and overflow itself does not
falsely mark WSS disconnected. Transport close, authentication/protocol/idle
failure, unexpected process restart, overflow, and planned shutdown have
separate incident causes. Reconnect proves current connectivity and closes the
transport interval but does not by itself assert historical completeness.

Closed incidents, including planned deploy intervals, are recovered only through the provider's official
authenticated `/open/strategy_list` and `/open/strategy_hits` endpoints. The
list call verifies the exact configured Strategies are enabled; history is
page-bounded, overlap-safe, filtered to the incident interval, and persisted as
`first_ingest_mode=recovery`. Fact identity makes overlap idempotent. Complete
retention marks the incident recovered; a missing endpoint, retention boundary,
or provider failure remains explicit as `unavailable` or `partial`. Recovered
facts may enter Story/Brief but can never create outbound Push. OpenNews Search
is not a recovery or parity authority.

The sole Story writer loads active RSS Items from the 96-hour feed window and
OpenNews Items from the 12-hour Strategy-qualified window. It expands RSS facts
in the pinned category-major membership order, runs the WorldMonitor identity,
classification, corroboration, and importance kernel before any category cap,
then retains the stable top 20 membership rows per category. It forms one
physical union of those capped RSS Items plus every current OpenNews Item and
calculates the materialized Story/member closure from that union. Duplicate
RSS category memberships therefore count in public selector evidence without
duplicating physical Story membership. Accepted facts set one process-local
dirty event. The sole writer coalesces bursts for one second, while one
five-minute safety pass covers a lost local wake. A load-time compare-and-set prevents an older
snapshot from overwriting a newer publication. The turn is bounded by 10,000
rows, 8 MiB input, and a 25-second CPU budget; unchanged or superseded input
writes zero serving rows. Its isolated serial CPU process prevents this long
calculation from blocking Token Radar, Profile, or Macro CPU admission. There
are no News frontiers, similarity-edge rows, aliases, membership history, or
sampled/adaptive population path.

NewsItem identity is `(source_id, source_item_key)`. RSS prefers a non-empty
GUID, then the canonical URL, then a deterministic title/publication-time key;
OpenNews uses `params.id` as its source item key. Tracking parameters are
removed from article links. Repeated configured Strategy frames merge only
bounded OpenNews metadata and the Strategy provenance union; they never replace
one fact with one Item per Strategy.

Story identity is the Python port of WorldMonitor's
`shared/story-identity.js`: normalized titles, deterministic signed FNV-1a
512-dimensional vectors, uniform and boosted cosine channels, threshold
`0.615`, exact-duplicate union, high-containment rescue, 250-item candidate
buckets, and deterministic union-find. A Story ID is the full SHA-256 of the
shared `normalizeStoryText` value for the pinned earliest anchor in that
current component; an untrackable component uses its per-Item sentinel. It may
change when the earliest item expires; the previous Story is removed and its
detail route returns not found. The separate `canonical_key` records the
caller-owned public `titleHash` used by the digest first stage. Distinct lexical
components may share that value, but never membership or selector grouping.
There is no archived Story product, embedding, full-article extraction,
browser clustering, revision product, per-Story AI analysis, or localized-title
state. Outbound Push may independently freeze a Chinese presentation copy of
its selected OpenNews Item headline, but that delivery-local adapter adds no
model-derived NewsItem or Story state.

Threat level and category use WorldMonitor's deterministic keyword classifier,
including exclusions and historical downgrade. There is no item-level AI
classifier or cache. Importance uses WorldMonitor's 55% severity, 20% source
tier, 15% corroboration, and 10% recency, followed by the narrow
diplomacy/flashpoint and entity-corroboration boosts. Materialized Story
`source_count` counts distinct reporting origins. Public candidate
`source_count` retains membership-expanded evidence, while
`unique_source_count` counts distinct reporting origins; repeated category
membership can therefore make the first greater than the second. Repeated
OpenNews delivery of one provider record still counts once. Persisted recency
uses the equivalent one-hour cache epoch, so unchanged input within an epoch
writes zero serving rows. The API exposes the scoring item and factor
breakdown. Global clustering precedes search, filtering, sorting, and keyset
pagination.

The Story transaction is also the only public selection writer. Materialized
Stories retain complete ownership, while the public `seed-insights` stage first
drops titles whose JavaScript UTF-16 length is at most ten and then reruns the
same pinned clustering kernel over eligible Items. Removing a short bridge can
split one complete Story into multiple public candidates; each candidate maps
back to its containing Story ID. The selector derives member titles, distinct
origins, entity corroboration, source tier, public category/threat, second-stage
importance, admissibility, and 16-hour effective-recency rank, then selects at
most eight. One primary reporting origin can occupy at most three slots. If the
normal Top Stories contain no eligible corroborated lead, the highest-ranked
eligible candidate is reserved and the result is restored to public rank order.
Drop counts distinguish admissibility, source-cap, and overflow effects. This
is one global public selection: there is no profile, preference, embedding,
topic grouping, entity veto, `(source, category)` quota, client-side ISQ
reorder, or promised topic/multi-source diversity.

The selection table is one singleton captured-current snapshot, not rank rows.
`selection_fingerprint` binds its projection revision/evaluation clock, every
ordered Top Story field and selection statistic, plus selector/identity
versions. A Brief slot freezes that complete selection JSON once.
`publication_id` hashes the complete sealed served payload—slot, Top Stories,
content kind, sources, source-age range, validation, provenance,
provider/model, and versions—but not its own ID. There is no target identity,
publication-history table, or request-time Story join.

L1 sends only the ordered primary headlines, primary sources, and distinct-
source counts. Its tolerant parser and citation-scoped composer are both the
provider acceptance gate and final publisher: a rejected response advances to
the next provider, while a missing or proper-noun-invalid per-Story line falls
back only to that Story's headline. Accepted L1 output is English, has one
index-locked line and source slot per Top Story, and retains an empty URL slot
instead of shifting citations. The code-owned waterfall is Ollama
`llama3.1:8b`, the configured direct DeepSeek endpoint/key/model, then optional
Groq `llama-3.3-70b-versatile`. The direct slot exists only when
`llm.base_url`, `llm.api_key`, and `llm.news_brief_model` are all present;
partial configuration is invalid and no endpoint or model is inferred.

L1 and the corroboration-gated single-headline L2 fallback share one 60-second
budget with a five-second guard and bounded provider retries/Retry-After. L2
accepts the first transport-valid minimum-length prose, then applies only its
proper-noun/headline fallback; it has no L1 composer, no Story lines, and at
most one valid source. L1 produces `quality=ok`; L2 or no-text produces
`quality=degraded`. Empty selection publishes nothing, and a non-empty
selection without an eligible lead makes no model call: it can advance a
complete no-text degraded snapshot only when no healthy LKG exists.

Brief persistence is exactly two singleton tables:
`news_brief_selection_current` and `news_brief_current`. Slots are aligned to
UTC half hours. Story publication never waits for an RSS catalog sweep, so a
new OpenNews fact can enter the debounced Story turn immediately. An empty
Top Story selection may open the current slot but is not claimable, makes no
model call, and never completes or overwrites the served payload; a later
non-empty selection in the same half hour remains claimable. At most the newest
eligible slot is considered after restart, so older missed slots cannot
manufacture catch-up churn. A claim freezes the current non-empty selection in
`active_selection` and holds a 120-second fenced lease. Finalization checks
that fence and publishes only against the frozen selection, never a later live
selection. The same row owns bounded slot telemetry and the whole
`served_payload`. Healthy output advances it; degraded output advances only
when no healthy payload exists, otherwise it preserves the complete healthy
LKG without mixing Top Stories, prose, sources, or clocks. The public state is
exactly `unavailable`, `current`, `degraded`, or `last_known_good`.

`NewsItemPush` is a News-owned durable outbound capability, not a model
candidate or generic Notifications service. The sole OpenNews Item writer
creates one outbox row in the same short transaction that first inserts an
eligible live Item. Identity is the deterministic `item_id` over
`(source_id, source_item_key)`; OpenNews uses `params.id`. Strategy overlap
therefore creates one alert, while distinct provider IDs remain distinct even
when Story later clusters them together. Recovery-first, pre-epoch, RSS, and
delivery-unavailable Items never create work or backfill.

The OpenNews Item writer uses its source-local transaction fence and does not
acquire the Story publication advisory lock. A Story publish may therefore
finish against its captured prior closure while a new Item/outbox commits; the
post-commit dirty signal schedules the next deterministic closure. This keeps
Story publication time and failure outside Item Push admission.

One private `NewsItemPush.turn()` peeks FIFO pending work, optionally translates
the title outside a transaction under a three-second total bound, conditionally
fences `pending -> sending` with the minimal presentation snapshot, renders the
Feishu card, performs at most one request, and settles `sent` or `terminal`.
Translation failure always falls back to the original title, which remains
visible. Feishu `code == 0` is the only success. There is no delivery retry,
backoff, lease, reaper, or exactly-once provider claim. A pre-fence crash leaves
pending work; a post-fence or ambiguous interruption is terminalized at startup
and never resent. Story projection never reads, writes, configures, or joins
Push state, and Feed/detail expose no Push field.

The complete live News storage boundary is exactly ten tables:
`news_sources`, `news_items`, `news_stories`,
`news_story_members`, `news_projection_summary`,
`news_brief_selection_current`,
`news_brief_current`,
`news_push_state`, `news_push_deliveries`, and
`news_opennews_incidents`. The
`20260801_0234` migration removes incremental Story machinery,
`20260801_0238` adds the durable push baseline and delivery ledger.
`20260806_0244` adds dedicated provider-score and Story-success clocks.
`20260807_0246` is historical input to the current cut.
`20260809_0247` installs public RSS/OpenNews source controls, removes the
persisted OpenNews gap columns and facet tables, replaces the former Brief
run/publication tables with the two-singleton slot design, clears rebuildable
Story/selection state, and deletes incompatible Push payload rows. It is
irreversible and has no runtime compatibility lane.
`20260813_0265` is the Strategy-only hard cut. It keeps the then nine-table ownership
boundary but deactivates legacy full-corpus OpenNews Items, clears and normally
rebuilds the Story/member/selection closure through the sole normal writer,
clears incompatible Brief current/LKG state,
cancels legacy pending/retry Push work, preserves immutable sent-delivery audit
plus baseline/dedup evidence, and replaces obsolete REST-recovery telemetry with
truthful unknown-coverage state. Old and new acquisition writers never overlap.
`20260813_0256` keeps that historical nine-table boundary and adds deterministic,
rebuildable source/origin `facet_facts` to each `news_stories` row. The sole
Story writer derives them from the same complete membership closure before the
Story fingerprint; Feed facets expand this bounded Story-local dimension
instead of rereading every member Item. Provider-score qualification remains a
dynamic membership-bounded read, including the short interval after an Item
expires and before the next atomic Story replacement. Source/origin filters
and facets bind to that same published Story snapshot, so an Item correction
cannot mix old facet counts with a new filter identity before replacement.
`20260813_0257` through `20260813_0260` are historical Story Push migrations;
their eligibility clocks, scan cursor, and reconcile ring are removed by the
current Item Push hard cut.
`20260813_0266` adds the tenth
News table for typed OpenNews incidents, replaces legacy coverage flags with
official Strategy-history status, and records immutable live-versus-recovery
ingest origin. It removes provider-score/assets/freshness Push gates and the
cursor/reconcile ring, renames the baseline to an enablement epoch, terminalizes
incompatible unsent v1 deliveries, and historically made Story publication the
v2 outbox writer. The WSS receiver, database publisher, and status publisher run
independently; current WSS state and one-hour inbound/Story-visible latency are
separate from historical incident recovery.
`20260814_0270` supersedes that Story Push boundary and hard-cuts the two
existing Push tables in place to Item identity. It preserves completed legacy
audit and old rendered payloads as audit-only data, terminalizes incompatible
unsent work, removes Story/retry/lease fields, resets enablement, and adds no
table.
`20260813_0261` replaces Radar's expression index with the STORED generated
fingerprint and its narrow covering index, preserving all facts and the current
payload with no dual path.

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
