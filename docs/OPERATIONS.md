# Operations

This document owns runtime configuration, worker/reliability invariants,
diagnosis, and safe repair boundaries.

## Runtime truth

The only operator-owned Tracefold application configuration is
`~/.tracefold/config.yaml`: deployment/domain choices, role-specific
PostgreSQL references, providers, credentials, API/auth, models, and storage.
Worker topology, cadence, deadlines, batches, leases, retry policy, timeouts,
and resource budgets are code-owned.

Confirm the active paths with `uv run tracefold config`. Never infer live state
from fixtures, examples, `.env`, generated docs, or a new CLI process. Report
paths, redacted configured booleans, provider names, error classes, and command
results; never secret values.

## Operator lifecycle

The canonical complete-product lifecycle is:

```bash
make up
make status
make macro-acceptance
make logs
make down
```

`make up` preflights Git, `uv`, Docker, Compose, `curl`, and daemon access, runs
idempotent initialization, builds one shared Python/React image, starts
PostgreSQL when absent, requires the one-shot migration to succeed, starts
Serve and Workers, and then runs the same fail-closed status gate. Rerunning it
recreates only migration, Serve, and Workers; it does not recreate a running
PostgreSQL container. On failure, use `make logs`. Operator config, four
password files, and named-volume data remain in place. `make down` stops
containers without deleting that volume.

Fresh PostgreSQL role bootstrap belongs only to the image's `initdb` phase. It
creates a non-login owner plus the separate Serve, Workers, and migrate roles
from their mode-`0600` password files, revokes the bootstrap login, and only
then permits migration. It is not a periodic reconciler and will not mutate an
unknown non-empty cluster. Such a cluster must already satisfy the role/schema
contract; startup never repairs an unknown role/schema boundary.

`make status` prints Compose state and returns non-zero unless PostgreSQL,
migration, Serve, Workers, the Serve and Workers readiness endpoints, and the
HTML console all pass. It must not be replaced by a liveness-only `curl` or a
Compose command whose exit status ignores an unhealthy Worker.
`make macro-acceptance` is the separate product-data gate for the overview and
six Macro reads; it is not part of `/readyz`.

## Health and status

| Surface | Meaning | SQL/queue inspection |
|---|---|---|
| `/healthz` | process liveness | none |
| Serve `/readyz` | DB liveness plus cached startup schema/composition | no queue inspection |
| Workers `/readyz` | root running, singleton session healthy, and latest O(1) heartbeat persisted within 15 s | no queue inspection |
| `/api/status` | separate runtime truth and persisted Provider operations | bounded control plus ordinary read |
| `make status` | PostgreSQL, migration, Serve, Workers, readiness, and console | fail-closed lifecycle check |
| `make macro-acceptance` | exact Macro contracts, current health, coverage, and ETag revalidation | persisted product-read acceptance |
| `tracefold ops ...` | explicit on-demand diagnosis and repair | command-specific |

Provider degradation and a missing Fed document analysis do not make the HTTP
process unready. `/api/status.providers` derives configured ownership,
continuous-source freshness, durable circuit state, and queue backlog from
PostgreSQL; it never probes an upstream. Only adapters with a durable
operational signal appear there, and backlog is an indexed existence signal;
use `tracefold ops queue-inspect` for exact on-demand queue detail. Domain
freshness and native model-job state remain visible through their own API and
operator diagnostics.

## Worker ownership

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
It wires one root `TaskGroup`; its due/periodic loops and dispositions are
private implementation details. Configuration cannot invent workers, owners,
resource lanes, or concurrency. An unknown child exception is a process
failure, not an individual-worker degraded state. The typed recurring
business-DB overrun below is the one resource-specific local recovery rule.

```text
tracefold serve
  -> read-only pool -> HTTP/static/shared persisted-live WebSocket poller

tracefold workers
  -> one singleton advisory lock and runtime_id
  -> one DB pool min 1 / max 4 / max_waiting 3
  -> one pinned singleton session / business DB executor 2 / control DB executor 1
  -> one heavy-DB admission slot for News Story load/full-publication work;
     Radar uses ordinary business-DB admission
  -> finite external-operation executor 3 / synchronous model adapter 1
  -> spawn-only Pebble ProcessPool 1 for Token Radar / Profile / Macro
  -> when News is enabled, spawn-only Pebble ProcessPool 1 for News Story
  -> acquisition clocks + fixed-period News Story and after-completion Token Radar writers
     + one EDF projection coordinator for Macro/Profile
  -> one serial native-state model arbiter
```

Every acquisition/projection/model task uses a short claim transaction,
bounded load plus provider/compute/model work with no database connection, and
a short compare-and-set publication transaction. The stateless EDF
coordinator polls typed Macro and Profile candidates and runs one
eligible semantic shard at a time, ordered by the real freshness deadline.
After every productive shard turn it yields cooperatively and immediately
rereads every typed frontier head. Failed and idle shard turns wait on the fixed
250 ms bounded polling cadence. Other due loops and the model arbiter retain
their bounded code-owned productive and idle cadences. This preserves
single-shard resource ownership while allowing a real projection backlog to
converge.
`deadline_at_ms` is not a not-before time. Material changes are eligible
immediately; `next_attempt_at_ms` delays only a scheduled recheck or retry.
The eligibility expression has a bounded partial index on every projection
frontier. There is no
generic scheduler, database wake plane, startup rebuild, phased load shifting,
or configurable concurrency.

The control child distinguishes the pinned singleton session from its pooled
heartbeat write. Loss of the pinned advisory-lock session remains immediately
fatal. A precise transient PostgreSQL admission, timeout, pool-checkout, or
connection error from the idempotent heartbeat write is retried after 250 ms;
after 15 seconds the stale heartbeat makes readiness false without killing the
root, and recovery restores readiness. Invariant failures and an unfinished
native control future remain process-fatal. This retry does not apply to
general control writes whose commit outcome could be ambiguous.

Resolution performs one external lookup per durable turn and reprocesses at
most 10 affected intents in each publication transaction. Larger closures use
the persisted keyset continuation and repoll after the fixed 250 ms cadence
without refetching the provider result.

Serve owns a read-only pool of eight with ordinary/search/control admission
`6/1/1`, 50 ms permit wait, 250 ms checkout, one-second statement timeout,
JIT off, parallel gather off, and 8 MiB work memory. Workers owns the exact
pool/lane topology above. Finite provider/filesystem operations share the
three-slot external capability; stream sockets remain long-lived async root
children outside it. A provider-wide failure opens durable circuit state and
consumes no target attempt. Only the owning provider seam may map an outer
finite-operation overrun into its existing durable failure policy. A typed
recurring business-DB overrun remains local to its natural loop; its occupied
permit remains bound to the native future and the loop retries on its normal
cadence. Control-DB, model, CPU, cleanup, and unclassified overruns remain
process-fatal. Classification uses the typed physical capability carried by
the exception, never an operation-name or error-string prefix. A caller timeout
never releases a resource permit before the underlying future actually
completes; three stuck provider futures therefore exhaust the shared external
capability even though the root heartbeat can remain healthy. Diagnose that
state from the resource-active/admission metrics and domain status. If an
underlying thread never returns, process exit is the only universal release
authority.

The anonymous GMGN direct WebSocket treats `upstream.reconnect_delay` as its
initial retry delay and doubles consecutive connection failures to a code-owned
60-second cap. The first distinct failure logs at error level; repeated
identical failures log only at power-of-two checkpoints, while the first live
frame resets the backoff. This limits local retry and log pressure but does not
claim to repair an upstream TLS or network-path outage.

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

News Story load/full-publication work uses one additional code-owned heavy-DB
permit before the two ordinary business permits.
Heavy admission waits up to 16 seconds for the previous bounded native
transaction, then uses the unchanged one-second business admission. Waiting for
the heavy permit consumes no business slot, so the two measured heavy phases do
not fill both slots together. Radar load, presentation, and changed-only
publication use ordinary business admission. The permit follows the native
future after wrapper timeout or cancellation. This is one physical bulkhead over
the existing pool and executor, not a product lane, priority scheduler,
configurable topology, or reserved PostgreSQL connection; all non-heavy work
can still use both business slots.

Token Radar is one after-completion loop outside the EDF coordinator. Workers
run one turn immediately, then wait 30 seconds after that turn completes before
starting the next. There is no catch-up burst, overlap, startup reconcile, or
database wake plane.

One turn performs exactly four steps: load typed Event and resolution-revision
facts, reduce on the isolated CPU process, batch-read presentation facts for the
selected targets, and publish the complete singleton. The load is a bounded,
deterministically ordered twelve-hour causal replay. A `20001` sentinel and
incremental byte count retain the 20,000-row and 16 MiB input envelopes. The
reducer uses the first eight hours to seed state at `t-4h`, replays the final
four-hour transition, compares prior `(t-8h, t-4h)` with current `[t-4h, t]`,
opens only on a positive complete-qualification false-to-true transition,
applies negative close and four-hour episode suppression, and selects the
server-ordered Top 50. Market facts remain presentation-only and must be fresh
within five minutes where present.

Database steps use the shared native statement and transaction deadlines; they
have no Radar phase budgets or whole-turn service deadline. CPU reduction runs
without a Radar service timeout in its isolated one-process capability. Normal
cancellation propagates. Any other turn failure is logged with its safe error
class, leaves the last successful singleton unchanged, and waits for the next
natural cycle. Input overflow still fails closed: it never samples, truncates,
widens the source interval, or partially publishes.

Publication locks one stable singleton and writes the exact complete
`token_radar_snapshot_v5` packet only when its canonical snapshot fingerprint
changes. The public packet has exactly `schema_version`,
`social_evidence_as_of_ms`, `eligible_total`, and `items`; there is no public or
durable availability/stale/failure state. The initial singleton is a valid
empty v5 packet, and `updated_at` changes only on a changed successful
publication. Radar owns no dirty frontier, claim, lease, attempt state,
ruleset/input fingerprint, episode/history/Gate-audit table, product-specific
heavy-DB permit, or product-specific telemetry/status command. There is no
Stocks route, writer, query family, or read model. The retained
`us_equity_symbols` catalog is only a token-identity collision guard and owns no
runtime Stocks loop.

Profile and Macro projection claims retain their 30-second lease envelopes.
The fixed-period News Story writer retains its 25-second operation budget. The
long News compute runs in its own one-process lane; it cannot consume the
isolated CPU process used by Token Radar and the short Profile/Macro
projections. Both lanes remain serial and code-owned. The
high-churn `events`
table uses a one-percent/10,000-row auto-analyze threshold so the 24-hour
Search planner does not choose a recency scan from stale time-distribution
statistics.

`/metrics` exposes low-cardinality worker transaction and shared capability
resource signals. Frontier-backed domains additionally expose projection
source/candidate/written counts, queue depth, oldest-due delay, and cumulative
deadline misses. Token Radar has no product-specific metrics contract or
production-duration acceptance gate. Use shared resource and PostgreSQL
activity/lock evidence for diagnosis; CPU alone is not a root-cause claim.

## Durable queue and transaction rules

- PostgreSQL facts/control rows are the only recovery source.
- Claims are bounded and leased with `SKIP LOCKED` or compare-and-set.
- Queue identity is the stable product target, not an event or attempt.
- Success writes the current model and acknowledges the exact claim in one
  application-owned transaction.
- Retry clears the lease and schedules a bounded future attempt.
- Exhaustion preserves the source snapshot in
  `queue_terminal_events`.
- Workers re-read durable work on bounded intervals; there is no wake plane.
- Provider/network/subprocess/filesystem I/O occurs outside DB transactions.
- Current rows use stable keys and skip unchanged payload writes.
- Asset profile targets carry `hot`/`warm`/`cold` priority, exponential
  missing/error backoff, and an explicit terminal reason. Rank-only churn does
  not reset retries; a new evidence fingerprint reactivates the target.

## First checks

For missing or stale live data:

1. run `uv run tracefold config`;
2. check `/healthz` and `/readyz`;
3. inspect authenticated `/api/status`;
4. run `uv run tracefold ops queue-inspect --status active`;
5. inspect unresolved terminal events;
6. trace one stable target from fact -> dirty target -> current row -> API.

| Symptom | Inspect first |
|---|---|
| no API row | current key and publication state |
| idle worker with expected work | durable target plus due/lease fields |
| stale row after a run | fact watermark, payload hash, zero-write comparison |
| growing queue | claim size, lease expiry, retry budget, terminal events |
| repeated provider failure | provider status and deterministic terminal policy |
| readiness 503 | DB liveness and startup schema/composition |
| status degraded, readiness 200 | expected runtime/product separation |

## Domain traces

Token Radar:

```text
event -> intent -> resolution revisions
  -> bounded twelve-hour source-time/revision read
  -> seed at t-4h from eight hours + availability-ordered four-hour transition to t
  -> negative close + positive false-to-true qualification + four-hour suppression
  -> Top 50 -> one bounded selected-target identity/market presentation read
  -> one atomic exact token_radar_current v5 snapshot
```

Market current is maintained transactionally with `market_ticks`; it has no
projection worker or dirty queue. Repair uses bounded
`tracefold ops rebuild-market-current --execute` fact replay.
The normal poll rereads the database-ordered most recently active 100 market
targets from the fixed 24-hour fact window every 35 seconds. Consecutive turns
intentionally refresh the same still-hot targets so their five-minute Radar
presentation facts remain fresh; there is no cross-turn exclusion cursor or
24-hour round-robin sweep. Current Radar targets occupy the first batch slots;
the remaining capacity is filled by 24-hour activity order. This priority only
keeps presentation facts fresh and never changes Radar admission or rank. The
OKX path uses the bounded batch
`/api/v6/dex/market/price-info` contract, so price, market capitalization,
liquidity, and holder facts share one request and observation clock rather than
an N+1 enrichment loop. Radar nevertheless publishes independent price and
market-cap clocks because the selected persisted facts may come from different
observations; neither market value is an admission fact.

News:

```text
OpenNews account Strategy WSS
  -> authenticate, then send zero application subscription frames
  -> server automatically pushes strategy.triggered
  -> exact configured multi-Strategy allowlist
  -> operator-bound 12-hour canonical facts + provenance union
  -> disconnect/overflow/outage records a typed incident
  -> official Strategy list/hits performs bounded recovery; no Search

WorldMonitor full/en + INTEL RSS catalog
  -> opt in with news.rss_enabled: true (default false)
  -> claim one due source -> conditional fetch -> first-five snapshot
  -> 96-hour breadth/corroboration facts

RSS membership expansion -> score before category Top 20
  -> physical union with OpenNews
  -> 1-second dirty debounce + 5-minute safety pass
  -> WorldMonitor clustering/classification/importance
  -> compare-and-publish current Story/member closure
  -> public UTF-16 title-length gate + same-kernel seed clustering
  -> public cluster score/admission/recency selector
  -> singleton server-ordered Top Story selection snapshot
  -> /api/news/feed + /api/news/stories/{story_id}

UTC half-hour slot
  -> freeze current public selection + 120-second fenced claim
  -> native News Brief candidate
  -> serial model arbiter
  -> Ollama -> configured direct DeepSeek -> optional Groq L1 waterfall
  -> shared-budget corroboration-gated L2 fallback
  -> whole sealed English Insights payload or whole LKG
  -> /api/news/brief

First-inserted live OpenNews Item transaction
  -> persist one immutable NewsItem fact
  -> when delivery is available and the observation is post-epoch, insert one
     `news_item_push_v1` outbox row keyed by `(source_id, source_item_key)`
  -> commit Item and outbox together; recovery, RSS, pre-epoch, and disabled or
     unavailable delivery create no Push work and are never backfilled

Independent Item Push turn
  -> inspect one pending row
  -> attempt title-only Chinese translation asynchronously once within 1.5 s, or use original
  -> commit the immutable presentation and `sending` fence
  -> make one signed or explicitly unsigned Feishu attempt within 7.5 s
  -> settle once as `sent` or `terminal`; no retry, lease, or reaper

Independent Story/Brief projection
  -> may merge several pushed Items, fail, or rebuild without changing Push
  -> exposes no Push field and never creates, updates, or suppresses Push rows
```

News acquisition always registers OpenNews. A configured token while News is
enabled requires a non-empty duplicate-free `news.opennews_strategy_ids` set;
otherwise startup fails closed. `tracefold config` reports only token/ID-set
configured booleans and the ID count. `news.rss_enabled` defaults to `false`;
only `true` registers the exact pinned catalog: 179 physical HTTPS RSS
feeds, 183 category memberships, 178 reporting-source names, and 17 categories.
When disabled, reconciliation disables old RSS rows and releases their claims;
the acquisition clock performs Article expiry but no RSS network request.
When enabled, one due turn claims at most one source for 60 seconds, sends ETag
and Last-Modified validators, and considers only the first five RSS/Atom wire
entries before validation. A retained entry requires a title and parseable date
no more than one hour in the future; its link is optional and retained only
when HTTP(S). If all first-five entries lack a title, the turn is a parse
failure rather than a successful empty refresh. A successful accepted set
atomically replaces that source's snapshot. `304` and unchanged snapshots write
no NewsItem facts. Failure keeps the last successful snapshot until the 96-hour
floor expires. Automatic redirects are disabled: the reader follows at most two
redirects explicitly and sends each request only after the hop is public HTTPS
and every resolved address is globally routable.

The operator-bound OpenNews lane owns one persistent WSS receiver outside the
finite-operation capability, one independent PostgreSQL publisher, one
independent status publisher, and one 256-event queue. The client authenticates
with the configured token, then sends no application subscribe/request: never
`news.subscribe`, `news.unsubscribe`, `strategy.subscribe`, or
`strategy.triggered`. Literal provider ping/pong and RFC control heartbeat remain
allowed. The server automatically pushes the account owner's Strategy
notifications. Admission requires `method=strategy.triggered`, object
`params` and `params.strategy`, a non-empty provider event `params.id`, and a
nested Strategy ID in the exact configured allowlist. Configured and wire IDs
are trimmed opaque strings; names are diagnosis context only. Matching NEWS,
MARKET, meme, and listing events are accepted regardless of `engineType`.
Database admission failure never closes the socket: the publisher retries the
same pending batch and observation clock. Overflow drops only the excess frame,
records `buffer_overflow`, and leaves current WSS connected.

There is no ordinary OpenNews Search overlap. Disconnect, queue overflow, and
unexpected process outage open typed incidents; planned shutdown is distinct.
Planned intervals are excluded from network-failure counts, but they still close
at the next live connection and recover from official Strategy history because
the provider may publish events while Tracefold is intentionally offline.
Reconnect closes the current transport interval, then official authenticated
`/open/strategy_list` verifies the configured set and bounded
`/open/strategy_hits` pages recover each interval with overlap. Exact fact
identity makes recovery idempotent. Complete retention marks `recovered`;
retention shortfall or endpoint/provider failure remains `partial` or
`unavailable`. Recovery facts can enter Story/Brief but never Push.
`/api/news/sources` lists OpenNews first with current connection, last accepted
configured trigger, configured count/boolean, bounded observed names/types,
last connect/disconnect, Strategy-history status, and incident state, then enabled RSS
schedule/claim outcomes. It never lists the configured ID values. This inventory
order is operational only and does not change Story tier or ranking.

The current cutover allowlist is exactly `1018` (News Score > 70) and `1019` (OI
Event Monitor); redacted diagnostics must report configured count `2`. Listing
and Delisting Announcements and Storage News exist provider-side but are
deliberately excluded, so their IDs are not a cutover prerequisite. Enabling an
additional provider Strategy does not admit it until an explicit reviewed
Tracefold configuration change; disabling or deleting an allowlisted Strategy
stops new triggers and is an external admission-policy change.

Acquisition is the only writer of NewsItem Article facts and provider metadata.
RSS fact identity is source URL identity plus GUID, canonical URL, or the
deterministic title/publication-time fallback. OpenNews identity is provider
event `params.id`; nested `params.strategy.id` is admission provenance. An
OpenNews report uses the first non-empty logical plaintext block as the bounded
title; reporting origin prefers the underlying `source`, then URL host and
`opennews`. `newsType=strategy` and the wrapper Strategy name never become the
origin, and linkless MARKET/OI reports remain valid. Exact replay writes nothing;
the same event under multiple configured Strategies merges a deterministic
sorted-unique ID/name/source-type/engine-type provenance union into one Item.
Conflicting wrappers select one complete deterministic provider payload by
numeric score, non-empty assets, then canonical payload; fields are not mixed
across wrappers.
Different event IDs remain different facts. The full Strategy definition and
metrics payload are discarded. Story owns only deterministic derived columns on
the same row.

The dirty-triggered projection is the only Story/member/Brief-selection writer. It
expands the RSS facts in pinned category-major membership order and calculates
identity, classification, corroboration, and importance before selecting the
stable Top 20 membership rows per category. It then deduplicates those rows to
one physical RSS union, appends every current OpenNews Item, and calculates the
physical Story closure. Public selection retains duplicate category
memberships, so `source_count` can exceed the distinct-origin
`unique_source_count`, without duplicating physical Story members. Calculation
runs outside the database and publishes one coherent captured closure. New
accepted facts set a local dirty event, bursts coalesce for one second, and a
five-minute safety pass covers a lost wake. A load-time fingerprint prevents older
snapshots from overwriting newer ones, and unchanged inputs write zero serving
rows. The operation is bounded by 10,000 input rows, 8 MiB, and a 25-second CPU
deadline. Do not repair capacity failures with sampling or dynamic windows;
diagnose `news_story_input_row_cap`, `news_story_input_byte_cap`, or
`news_story_operation_timeout` against the RSS 96-hour and OpenNews 12-hour
populations.

Push outbox creation is part of the first live OpenNews Item insert. The same
transaction reads the effective delivery state and enablement epoch, then
inserts at most one immutable source snapshot using the stable Item identity.
Score, assets, signal, grade, URL, and provider publication time are optional
presentation evidence, never gates. Recovery-first, RSS, pre-enablement,
disabled, and unavailable observations never backfill. A transaction failure
persists neither Item nor outbox row.

OpenNews Item/outbox insertion does not acquire the Story publication advisory
lock. A long or failed Story publish cannot hold a newly accepted live Item off
the Push ledger; its post-commit dirty signal drives a later Story pass.

The worker processes only existing Item rows. It never scans Stories or Items
to discover missed work. Translation precedes the durable delivery fence and
never holds a database transaction. Once fenced, Feishu is attempted exactly
once; any response, timeout, transport, render, auth, or size failure is
terminal. Startup converts an interrupted current-schema `sending` row to the
sanitized `news_item_push_interrupted_unknown` terminal outcome even when
delivery is currently unavailable. Pending rows survive restart. The ordinary
shared productive 250 ms repoll drains work; an empty Push turn waits one
second. There is no Push-specific pacing mechanism.

The selector turn keeps the complete captured Story closure, then applies the
pinned JavaScript UTF-16 `title.length > 10` gate and reclusters eligible Items
with the same identity kernel. It applies the public cluster score,
admissibility gate, 16-hour effective-recency ordering,
maximum-three primary-source cap, and corroborated-lead reservation, then seals
at most eight Top Stories plus drop telemetry. Unchanged canonical selection
replay writes zero serving rows. It has no personalization, embedding, topic
grouping, entity veto, client ISQ ordering, or source-category quota.

The native Brief candidate is driven only by UTC half-hour slots. It opens the
current slot first and never replays a chain of historical slots. Story
projection never waits for an RSS sweep: a Strategy-admitted OpenNews fact can enter the
next debounced Story turn before any RSS attempt. An empty Top Story
selection can leave the current slot due but is not claimable, makes no model
call, and cannot complete the slot or overwrite the served payload; a later
non-empty selection in the same half hour can still be frozen. There is no
in-memory wake or second writer. A claim freezes the current non-empty
selection exactly once in `news_brief_current` and holds a 120-second fenced
lease. A non-empty selection without an eligible lead also makes no model call.
L1 sends only ordered primary
headlines, primary sources, and distinct-source counts. Every response must
pass the publication composer; transport or composer failure advances through
Ollama `llama3.1:8b`, configured direct DeepSeek, then optional Groq
`llama-3.3-70b-versatile`. The DeepSeek slot uses the exact
`llm.base_url`/`llm.api_key`/`llm.news_brief_model` triple; a partial triple is
invalid and no URL or model is inferred.

L1 and the eligible-lead L2 single-headline fallback share one 60-second
provider budget with a five-second guard. L1 seals healthy English content
with index-locked lines and source slots. L2/no-text remains degraded, has no
Story lines, and exposes at most one link-valid source. Without a healthy LKG,
a complete degraded snapshot can advance so Top Stories remain useful. With a
healthy LKG, a degraded slot preserves the entire older served payload and all
of its clocks; only bounded slot telemetry changes. Finalization checks the
lease and frozen slot selection, never the later live selection. The two Brief
tables are the selection singleton and current/served singleton; there is no
run table, publication table, target identity, history, or generic queue
adapter. Claim/finalization transitions are short PostgreSQL transactions
around model I/O outside the transaction.

Push configuration is fail-soft. A requested but missing/invalid Feishu webhook,
or requested Push while News is disabled, leaves delivery unavailable and
publishes a sanitized reason; it does not prevent Serve or Workers startup.
With a signing secret, each request carries the Feishu timestamp/signature pair;
without one, the request deliberately carries neither. A signed request that
fails is never retried unsigned.

Optional translation reuses the configured direct DeepSeek `llm.api_key`,
`llm.base_url`, and `llm.news_brief_model` triple; there is no independent
`news.push.translation` endpoint, key, engine, inferred default, or fallback
provider. It is an outbound presentation adapter outside the serial model
arbiter and shared synchronous finite-operation threads. Each non-Chinese Item
title receives at most one cancellable asynchronous provider call under a
5-second absolute deadline. Chinese input bypasses the endpoint. Missing
configuration, timeout, invalid output, and titles over 500 graphemes use the
original immediately. Validation is structural only; acronyms, numbers, and
asset-like text are not semantically anchored. A non-empty bounded Chinese
result becomes the header and the original always remains visible. Translation outcomes are informational and
never degrade delivery health.

The compact card is rendered only from the immutable Item source snapshot and
frozen presentation. It shows reporting origin, provider publication time,
Strategy labels, available asset labels, optional score/signal/grade, and one
`查看原文` button when a canonical HTTP(S) Item URL exists. A missing URL omits
the button. No Story ID, Story score, cluster title, or derived summary is
rendered. `tracefold config`, status, and logs expose only requested/effective
booleans and sanitized reason/error codes, never the webhook, signing secret,
card, or title.

The live News schema is exactly ten tables: `news_sources`, `news_items`,
`news_stories`, `news_story_members`, `news_projection_summary`,
`news_brief_selection_current`, `news_brief_current`, `news_push_state`, and
`news_push_deliveries`, plus `news_opennews_incidents`.

Diagnose News in this order:

1. `/api/news/status?view=realtime`: shallow current WSS state and connect/disconnect
   clocks, then one-hour inbound and Story-visible P50/P95;
2. `/api/news/sources`: last accepted configured trigger, redacted Strategy
   count, official history status, and typed incidents, then any enabled RSS
   breadth/corroboration schedule, claim, and outcome counts;
3. `news_items`: source-local identity, immutable `first_ingest_mode`, source position, canonical
   headline/description/origin, bounded metadata, and content fingerprint;
4. `news_story_members` and `news_stories`: current membership closure,
   full-SHA Story ID, state fingerprint, reporting-origin count, deterministic
   source/origin `facet_facts`, and score factors;
5. `news_brief_selection_current`: singleton projection revision, complete
   Top Story evidence, selection fingerprints/statistics, and server order;
6. `/api/news/feed`: flat global keyset order, server filters/search, and
   complete filter-bound cursor;
7. `news_brief_current`: UTC half-hour slot, frozen `active_selection`, fenced
   lease, bounded attempt/outcome/pointer telemetry, and whole
   current/LKG `served_payload`;
8. `/api/news/brief`: ETag and truthful public state;
9. `news_push_state` and `news_push_deliveries`: effective availability,
   enablement epoch, immutable Item source/presentation snapshots, one delivery
   fence, and explicit sent/terminal outcome;
10. `/api/news/status`: warming/ready/degraded layer health plus
   `live`/`recovering`/`stalled`, RSS/OpenNews ingest evidence, Story last success, Brief
   slot/current/LKG state, current Item Push counts, delivery latency, and
   informational translation outcomes.

News health has four layers:

| Layer | Healthy evidence | Degradation signal |
|---|---|---|
| ingest | OpenNews has a live authenticated WSS, accepted configured Strategy facts when any have arrived, and no current transport error; RSS reports explicit enablement and, when enabled, breadth/corroboration counters | missing token/configuration is degraded, connected but never-seen is warming, and current disconnect/error is degraded; historical incident recovery is separate and does not turn a connected WSS red; enabled empty/warming/unavailable/partially failed RSS is corroboration evidence without masking or overriding OpenNews; disabled RSS adds no degradation reason |
| story | the selected RSS/OpenNews population closes into coherent current Story aggregates and the Workers runtime is healthy | missing/duplicate ownership, aggregate mismatch, projection failure, Workers failure, or no current Stories yet |
| brief | the current half-hour slot completed with healthy output | no payload is `warming`; degraded output or a whole last-known-good fallback is `degraded`, with bounded slot reason/next due visible |
| push | disabled, or effective delivery is synchronized with an initialized enablement epoch, no recent terminal outcome, and current-policy delivery P95 is at most 15 seconds | requested but unavailable, unsynchronized state, missing epoch, a recent terminal outcome, delivery P95 above 15 seconds, or bounded-sample overflow; translation never degrades Push |

The operator state is `stalled` only when the persisted Workers runtime is no
longer fresh/running.
Any non-ready layer that is not stalled is `recovering`, including a missing,
warming, disconnected, or errored OpenNews Strategy lane, no Story/Brief output yet,
pending/sending/terminal delivery, configuration errors, Brief failure, and
delivery SLO breaches; otherwise the state is `live`. A missing
`workers_runtime` row does not manufacture a stall for read-only/test contexts.
This legacy overall label means only that a product layer is non-ready;
incident recovery status is independent.

The HTTP service remains ready when News is degraded; the structured News
health object names the affected layer. Facts and Story cards never wait for
the model or outbound delivery. OpenNews makes only official Strategy list/hits
recovery calls. Advanced News Search is not used by runtime recovery.

Macro:

The current Macro runtime contains material facts, acquisition targets, six
deterministic module rows, and immutable Fed document analyses. It has no
second daily publication or historical product lane.

```text
clock-specific target claim -> provider I/O -> typed fact + target cursor/state
  -> typed affected-module frontier -> EDF module-local projection
  -> stable macro_module_current row

official FOMC/speech body + effective-dated role fact
  -> macro_document_analysis_jobs
  -> exact-excerpt-validated immutable document analysis
  -> rates-fed module input
```

The five explicit Macro due loops claim only their own clock family from
`macro_acquisition_targets` with `SKIP LOCKED`. `macro backfill` and
`macro backfill-professional` enqueue and synchronously drain only their
explicit bounded targets before returning. Every loop claims one target/page
per bounded turn; provider I/O happens after claim commit. Completion appends
changed facts, advances the durable cursor, and compare-and-set updates the
target's current success/error state. Unchanged replay writes zero fact rows.
One failed source cannot head-of-line block another clock or target.

The professional backfill applies the code-owned Treasury/Fed/credit/WTI/BTC/
CFTC/ETF/futures history policy. Required windows remain visible through
History Depth but do not lower Current Health. Optional deep history has lower
urgency than current coverage and cannot block a current module.

Diagnose a missing metric in this order: Registry `concept_id` and
`source_role`, acquisition target state/lease/cursor/current error, persisted
fact clocks (reference/published/received), module Coverage/Current Health/
History Depth, then the current-row hash. Do not mask source or calculation
errors with frontend fallback content. `untrusted_proxy` Nasdaq/Yahoo rows stay
labelled. Closed and maintenance market sessions are judged against the last
expected bar rather than wall time.

FOMC/Board/Reserve Bank full-text facts feed
`macro_document_analysis_jobs`. A speech waits for its effective-dated role
match. Each claim performs model I/O outside the write transaction, validates
exact excerpts against the frozen official body, then atomically inserts the
immutable analysis, completes the job, advances the derived Dataset state, and
dirties the `rates_fed` frontier. Restart reclaims an expired lease without
duplicating analysis identity. A failed or disabled analysis affects only the
supporting document evidence; official Rates/Fed Current Health stays
independent and Fed judgment fields remain `no_call`.

The Macro projection domain maps changed datasets through the static
dataset/calculation/module dependency graph. One EDF turn loads only the
affected module's bounded history, computes outside the database, rechecks the
input fingerprint, and publishes that module in one short transaction.
Unchanged payloads write zero serving rows. The overview and six module fact
payloads read only `macro_module_current`; Rates additionally exposes the
secret-free optional-analysis runtime state. They never call a provider/model
or repair state.

`uv run tracefold macro status` reports actionable steady acquisition and
explicit maintenance backfills separately, including active and expired claims.
A historical backfill state is
therefore never counted as live Worker backlog. The same response includes
each module's current health, history depth, fact cutoff and update time, Fed
document-analysis job counts, and whether the optional analysis worker
configuration is `disabled`, `unconfigured`, or active. Active means its
configuration admission conditions are satisfied, not that a worker process
heartbeat was observed. The command performs no provider call and no write.
Reconciliation reactivates an `unavailable` steady target on process startup
only when its adapter is currently enabled, so enabling a previously disabled
source requires no repair command while a still-disabled source creates no
restart burst. The
`uv run tracefold config` command exposes the same secret-free booleans.

Migrations `20260801_0235` and `20260801_0236` are irreversible. They delete
the retired News acquisition history and Macro publication, per-attempt, and
stored intermediate history while preserving current NewsItems, Macro facts,
targets, document analyses, and six module rows.
Historical migration `20260801_0237` introduced an OpenNews recovery boundary;
historical migration `20260809_0247` later replaced that state with a bounded
ordinary-news overlap. Neither is the current Strategy-only provider contract.
Migration `20260801_0238` creates only `news_push_state` and
`news_push_deliveries`; it performs no backfill and no outbound send.
Migration `20260807_0246` is the irreversible World Brief hard cut. It fails
closed if a retained OpenNews row cannot yield a canonical title, otherwise
canonicalizes only the code-owned OpenNews facts in bounded batches, leaves
disabled historical source facts unchanged, clears the rebuildable Story closure, drops
the retired Story-title table and incompatible Brief rows/schema, and installs
the singleton selection plus discriminated L1/L2/none publication state.
Historical migration `20260809_0247` removes the earlier gap state and facet
tables, installs public RSS scheduling/snapshot controls, hard-cuts Brief to
`news_brief_selection_current` and `news_brief_current`, clears rebuildable
Story/selection state, and deletes incompatible Push payload rows. The then-live
News boundary was exactly nine tables. Existing current-schema Push baselines,
selected-Item ledgers, pending/retry deliveries, receipts, and freshness fences
are preserved byte-for-byte; the migration performs no provider or outbound
call and creates no compatibility view. Cutover preflight must prove at most
100,000 `news-opennews` rows; otherwise that historical migration fails closed
with `news_world_brief_hard_cut_opennews_row_cap:<count>` before rewriting
facts. `20260809_0247` is irreversible and has no compatibility reader.

Migration `20260813_0265`, the Strategy-only hard cut, is an offline
single-writer transition. Stop
Serve and Workers before applying it, and do not run an old `news.subscribe`
writer beside the new Strategy consumer. The cut must atomically:

1. deactivate legacy full-corpus OpenNews Items;
2. clear current Story/member/selection so the sole normal writer rebuilds it
   from remaining enabled facts after startup;
3. clear incompatible Brief current/LKG state;
4. cancel legacy pending/retry Push work that no longer has current eligibility;
5. preserve immutable sent-delivery audit plus Push baseline/dedup evidence;
6. remove/reset old REST-overlap telemetry and initialize truthful
   no-replay/unknown-coverage state.

After migration, configure exactly `1018` and `1019`, start only the new Workers
image, and verify secret-free configured count `2`, WSS connectivity,
last accepted configured trigger, and zero application subscription sends.
Do not call a private webpage Strategy endpoint or ordinary news search to
manufacture a recovered interval.

Migration `20260813_0266` is the historical Strategy/incident hard cut. It adds the
tenth News table, `news_opennews_incidents`; replaces legacy coverage flags with
typed cause/recovery state and official Strategy-history status; and persists
immutable `first_ingest_mode`. It removes Push's score/assets/freshness clocks,
cursor, and reconcile ring; renames baseline to enablement epoch; terminalizes
incompatible unsent v1 deliveries; and makes Story publication the atomic v2
outbox writer. After restart verify current WSS state, inbound/Story-visible
P50/P95, official history availability, and incident recovery independently.

Migration `20260814_0270` is the current offline News Item Push hard cut. Stop
Serve and Workers before applying it. It reuses `news_push_state` and
`news_push_deliveries`, requires an unambiguous legacy selected Item identity,
terminalizes all incompatible unsent Story-policy rows, preserves completed
legacy card audit, and removes Story identity, eligibility, retry, lease, and
translation-preparation columns. Current rows are keyed by Item and carry only
immutable source/presentation snapshots plus `pending`, `sending`, `sent`, or
`terminal`. There is no backfill, compatibility reader, dual writer, provider
call, or outbound send during migration. Restart first reconciles effective
availability and terminalizes interrupted `sending` rows, then only new live
OpenNews Item transactions may create `news_item_push_v1` work.

## Operator actions and retention

Supported terminal actions are:

- retry: recreate the supported source transition and record reason/time;
- archive: preserve evidence but remove it from unresolved work;
- quarantine: preserve and mark evidence for investigation.

Retired queues have no retry path. Successful operational attempts may have
short retention; failed/terminal evidence and unresolved side effects are kept
longer. Current models retain one stable row per identity.

Destructive migrations use bounded timeouts, transform data before constraints,
drop children before parents, avoid `CASCADE`/`IF EXISTS`, and preserve material
facts plus unresolved side-effect/terminal evidence.

Do not remove `events.raw_json` or `events.event_json` until every event has a
verified raw-frame edge and locator, historical coverage reaches 100%, and
ambiguous payloads are archived immutably.

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

`uv run tracefold db query-audit` verifies that every public HTTP/WS route is
assigned to a bounded read-query family and checks that every query can be
planned. `uv run tracefold db query-audit --analyze` executes those read-only
queries with JSON `EXPLAIN (ANALYZE, BUFFERS)` and fails on an estimated
large-table sequential scan, any temporary read/write blocks, or read/return
amplification above 20:1. An empty development database proves only SQL and
route coverage; production-scale output belongs in the real 30-minute
acceptance bundle. Each runtime owner supplies the same bound statement builder
used by its serving read; the App layer only composes those specs with route
coverage, so an audit-only SQL approximation is not accepted.

Read/return amplification uses the root result-row count by default. The
`news_feed_focus_facets` grouping query explicitly uses the largest actual
input-row count of its aggregate nodes instead, because its bounded public
result intentionally reduces many matching Story facets into a few counters.
That query-specific basis does not relax the 20:1 limit, temporary-block gate,
or large sequential-scan gate, and every other hot query keeps the default
result-row basis.

Use ad hoc `EXPLAIN (ANALYZE, BUFFERS)` only on a representative bounded
query. Since `ANALYZE` executes mutating SQL, wrap `INSERT`, `UPDATE`, `DELETE`,
or `MERGE` in `BEGIN` and `ROLLBACK`.

Frontier-backed hot paths claim narrow stable keys and hydrate wide JSONB only
after selection. Partial indexes must match the real due/status predicate. An
idle frontier worker must not scan broad facts merely to prove that no work is
due. Token Radar is the explicit exception: its code-owned 30-second reducer
performs one indexed, twelve-hour source-time/revision,
row/byte-capped evidence read followed by one Top-50 batch presentation read
and has no wake or due state. Both reads are bounded independently; there is no
per-Item profile or market query. Use one representative
`EXPLAIN (ANALYZE, BUFFERS)` for this
bounded evidence path; do not create a second acceptance or planner-assertion
control plane. Current models remain bounded by stable product keys; a
latest-generation pointer is not a retention policy.

Projection sessions disable PostgreSQL parallel gather and JIT and use 16 MB
`work_mem`; foreground ingestion and API sessions keep PostgreSQL defaults.
This prevents two bounded background iterations from multiplying into all
available PostgreSQL CPU workers while preserving deterministic source-to-
current work. The News current-item and asset-identity profile hot
paths use ordered composite indexes; do not replace them with periodic broad
scans.

Compose uses the official PostgreSQL 18 Bookworm image and preloads only
`pg_stat_statements` for query diagnosis. `compute_query_id` remains enabled.
Use Compose logs for container output and the supported Tracefold database
health, audit, query-audit, status, metrics, and `ops` commands for diagnosis
and repair. There is no repository `ops/` infrastructure tree, auxiliary
observability service, host log collector, or persistent diagnostic script.
The runtime hard-cut migration also resets the retired `powa.coalesce` and
`powa.frequency` `ALTER SYSTEM` entries before the official image takes over
the existing PostgreSQL volume.

For an ordinary migration or production cutover:

1. stop writers or establish a maintenance boundary;
2. take and verify a PostgreSQL backup;
3. record Alembic head and non-empty fact/read-model counts;
4. apply migrations with bounded lock and statement timeouts;
5. verify the same fact identities and expected counts;
6. start one writer per current model, then verify readiness, queue movement,
   and unchanged-projection zero-write behavior;
7. retain the backup until the new runtime passes smoke checks.

## Token Radar hard-cut history and v5 verification

Historical migration `20260810_0249` removed the six pre-singleton Radar tables
and installed the v1 singleton. Historical migration `20260810_0250` reset it
to `token_radar_snapshot_v2` and dropped `stock_attention_target_features`,
`stocks_radar_current_rows`, and `stocks_radar_publication_state`. It preserved
material Events, intents, resolutions, identity/profile facts, market facts,
and the internal `us_equity_symbols` collision guard.

Historical migration `20260811_0254` was the one-time transactional v3 Radar
reset. It hard-cut the singleton to an empty `token_radar_snapshot_v3` business
payload and initial public `unavailable` state, added the bounded
source-time/action index and basic singleton/schema/shape/size constraints, and
preserved all material facts and identity/profile/market evidence. Its first
successful sample reconstructed causal state from a three-hour fact horizon
for one-hour current/prior windows and a one-hour episode TTL; it did not import
v2 trigger values.

Historical migration `20260812_0255` was the one-time transactional v4 Radar reset.
Stop Serve and Workers before applying it. It resets only the rebuildable
singleton to an empty `token_radar_snapshot_v4` business payload and initial
public `unavailable` state, rebuilds the bounded source-time index as the
narrow fingerprint covering index, installs the covering resolution index used
by the optimized bounded load SQL, and preserves all material facts and
identity/profile/market evidence. Verify fact counts and stable fact identities
before and after the migration, then start only the v4 runtime. Its first
successful sample reconstructs causal state from the twelve-hour fact horizon
by seeding at `t-4h` and replaying the final four-hour transition for the fixed
four-hour product; it does not import v3 trigger values or the v3 LKG. There is
no dual read/write, compatibility adapter, feature flag, v3
fallback, episode, frontier, or history table, Gate audit, Stocks route,
staging runtime, or history import.

Migration `20260814_0269` is the irreversible v5 KISS hard cut. Stop Serve and
Workers before applying it. It replaces only the rebuildable Radar singleton
with one valid empty `token_radar_snapshot_v5` packet, retains the stable
singleton key, adds its canonical snapshot fingerprint, and removes all
ruleset/input/state fingerprints, attempt/failure fields, workload counters,
evaluation/state-change clocks, and creation metadata. It preserves Events,
intents, every resolution revision, identity/profile facts, market facts, and
the v4 load indexes. After migration, start only the v5 runtime; there is no v4
reader/writer, compatibility payload, dual write, or imported LKG.

Verify v5 with deterministic reducer tests, database migration/integration
tests, the exact authenticated HTTP/ETag contract, and browser tests for Top-50
reachability, responsive containment, whole-card current-tab navigation,
isolated Copy/GMGN actions, exact Token Case trigger query, explicit return,
and session scroll restoration. Production wall-clock, P95, telemetry, and
long-running release bundles are not Token Radar acceptance gates.

Migration `20260813_0256` adds `news_stories.facet_facts` inside the
existing nine-table News boundary. Stop Serve and Workers before applying it.
The migration backfills exact source/origin dimensions from current Story
members, makes the column required, and clears only the Story projection input
fingerprint. The next normal Story turn recomputes the new state fingerprints;
material Items, Story IDs/memberships, Brief state, and Push ledgers remain
intact.

Migration `20260813_0257` installs narrow covering and
partial-expression indexes for membership-first numeric provider-score
qualification and bounded Push-health reads. It extends the existing
`news_push_state` singleton with exact lifetime status counts plus latest
sanitized sent/error events, backfills them from the ledger, and adds typed
translation clocks to each delivery. Repository transitions update the ledger
and singleton under one state-first transaction lock. The two 24-hour SLO
reads select at most 5,001 indexed rows and aggregate only a complete population
of at most 5,000; overflow is explicit and fail-closed. Feed probes only current
Story members, keeps dynamic score and published source/origin snapshot
semantics, and does not use `active`. No new table or second writer is added,
and the one-second Serve query deadline is unchanged.
The score index intentionally includes `provider_metadata`: PostgreSQL does
not otherwise treat the expression key alone as covering this predicate and
will still perform heap fetches even when the lateral subquery selects a
constant. Keep that INCLUDE as the measured expression-index visibility
workaround; assess its deployed `pg_relation_size` against `news_items` before
considering any broader covering payload.

Migration `20260813_0258` keeps the same tables and writers. It adds a
nullable durable cursor to `news_push_state` so push discovery reads at most
one fixed 1,000-Story primary-key page per reconcile transaction and resumes the
same scan after restart; reaching the end clears the cursor so later provider
annotations are reconsidered on the next cycle. The dedicated provider
score/assets eligibility clock is part of the first-enable fence on every page;
Story projection and unrelated provider metadata cannot move it, so a
future-skewed provider clock cannot turn baseline evidence into a notification.
It also widens the existing
`events(received_at_ms)` and current-resolution unique indexes with only the
columns already needed by the market-target read. This avoids historical
wide-heap reads while keeping append-hot heap visibility work bounded, without
changing current-resolution uniqueness or target-selection semantics.
Stop Serve and Workers before applying the index replacements, then verify the
0258 index definitions before restoring the single writer.
`20260813_0259` additionally repairs any numeric-score Item missing its
eligibility clock during a mixed-version cutover and enforces the clock as a
database invariant. Migration `20260813_0260` adds the same Push singleton's
durable 25-second reconcile-ring clock; an active cursor is backfilled with its
last durable update time. Migration `20260813_0261` replaces the Radar
expression index with the STORED generated fingerprint and narrow covering
index, preserving every fact/current payload with no dual path. Keep Serve and
Workers stopped through the current head. Migration `20260813_0264` keeps the source-time partial
covering read, sets the append-heavy Events vacuum policy, and restores the
visibility map after the 0261 heap rewrite. This avoids coupling Radar work to
unrelated global Intent volume. Verify the source-time index, Events reloptions,
and full heap visibility before restoring the single writer. Migration
`20260813_0265` then performs the irreversible OpenNews Strategy-only hard cut:
it deactivates legacy full-news facts, clears rebuildable Story/Brief state,
suppresses pre-cutover pending/retry Push work, preserves sent/terminal audit and
the durable baseline, and replaces overlap telemetry with connection,
accepted-trigger, and no-replay coverage truth. Historical `20260813_0266` replaces
that interim coverage shape with the incident ledger and official Strategy-hit
recovery, and replaces the Push scan with same-transaction live Story outbox
creation. Verify new live Stories create outbox rows without score/assets, while
recovery-only and pre-enablement Stories do not; then verify Push latency/SLO.
Current `20260814_0270` replaces that Story policy with same-transaction live
Item outbox creation and zero-retry settlement. Verify two distinct provider
Item IDs that later merge into one Story still create two independent Push
attempts; Story failure must not block Push, and Push failure must not change
Story/Feed/Brief.

Migration `20260810_0251` is the historical Rates v7 hard cut. Migration
`20260811_0252` converts legacy steady `stale`/`invalid` acquisition targets to
the reachable state machine, removes `invalid` from the database constraint,
preserves all six current serving rows, and clears their rebuildable frontiers.
Worker startup recreates missing or version-mismatched frontiers from persisted
Dataset projection state and republishes them without provider I/O; already
matching clean frontiers remain zero-write.
Migration `20260811_0253` deletes only the rebuildable Rates, Economy, and
Cross-Asset current/frontier rows and requires `macro_rates_fed_v8`,
`macro_economy_inflation_v6`, and `macro_cross_asset_v8`. It preserves all typed
Macro/Market facts, acquisition state, documents, jobs, and immutable analyses.
Restart the sole Macro projection writer to reconcile all six frontiers and
rebuild the three semantic-contract rows; there is
no old-schema reader or compatibility path.

Container readiness intentionally remains infrastructure-only. After every
deployment, run `make macro-acceptance` separately. It validates the overview
and all six exact response contracts, requires complete coverage and current
health, and proves weak semantic ETag `304` revalidation. A failed report is a product
acceptance failure even when Serve, Workers, PostgreSQL, and `/readyz` are
healthy.

## Issue #33 Workers Runtime V2 acceptance and sealing

Controlled offline workload, isolated startup/recovery, and the real continuous
30-minute run are independent gates. Tests or a healthy Compose stack cannot
substitute for the real run. Print the deliberately non-passing `evidence.json`
template from the current code:

```bash
uv run tracefold ops seal-workers-runtime-acceptance --template
```

Before the cutover, migration `20260731_0233` must pass its terminal-owner
preflight. It performs these one-time, operator-authorized production evidence
mappings encoded in that irreversible migration; the specification-defined
historical coordinator and model owners retain their source- and
candidate-specific migration rules. No runtime alias or dual read remains after
the migration. Any other non-canonical owner aborts the whole migration;
operators must resolve that provenance instead of guessing an alias. After the
operator-approved production cutover, fill a new external bundle with measured
evidence. The production collector bypasses the maintenance lock because it is
read-only and must observe the running singleton. It uses a fixed 1,800-second
interval with 181 samples at 10-second cadence, rejects any sample gap over 15
seconds, and writes only to a new absolute directory outside the checkout:

```bash
uv run tracefold ops collect-workers-runtime-acceptance \
  --bundle /absolute/path/to/issue-33-runtime-evidence
```

The first sample embeds the complete result of
`PostgresQueryAudit.run(analyze=True)`. Validation binds the exact public-route
coverage and no-SQL route sets, requires every declared hot query, executes its
read-only plan, and rejects any route gap, missing plan/metric, large-table
sequential scan, temporary-block use, or other query-audit violation. This is
the production query-plan gate; a separately asserted query-analysis boolean
cannot replace it.

Every sample records the complete observed Worker `wait_event_type`
distribution and requires its counts to reconcile to the Worker connection
count. A sampled `Lock` wait is measured from PostgreSQL's ungranted-lock
`waitstart`, must remain within the code-owned 250 ms database lock budget, and
is sealed with its count and maximum duration for review. Other wait types are
not interpreted as a blanket failure; their interval maxima are sealed for
operator and independent review. The collector also requires the complete
low-cardinality Prometheus contract: projection deadlines/transitions,
processing and terminal counters, last-run time, and resource
active/admission/service series. A missing required family fails closed instead
of becoming an implicit zero. Token Radar product metrics are deliberately not
part of this collector.

Admission and service evidence are independent. Each requires complete
`capability`, `operation`, and `outcome` labels plus matching cumulative
`_count`/`_sum` series. Every exact series must remain present and monotonic
between samples, and each class must show a real positive count delta during
the 30-minute interval. Active capability gauges must remain within their
code-owned caps. Worker readiness and metrics are resolved from the actual
Compose-published Workers endpoint, including a custom host port. Token Radar
cadence, payload, HTTP latency, and singleton state are outside this historical
Workers Runtime V2 production collector and are verified by deterministic,
integration, HTTP, and browser tests instead.

More generally, the collector fails closed on a dirty or changing checkout,
revision/runtime/PID/container changes, restart or OOM, stale readiness,
container memory at or above 2 GiB, PostgreSQL connection/lock/transaction
violations, resource-cap violations, deadline/quarantine evidence, malformed
field shapes, negative or non-finite (`NaN`/`Infinity`) measurements, any
cumulative-counter regression, or a non-converging durable Frontier backlog.
The transaction ceiling follows the longest active steady transaction budget
(currently the News Story publication budget). Database-wide temporary
file counters are recorded for review but are not attributed to Workers; the
first-sample analyzed query audit remains the fail-closed temp-plan gate.
Arrival and completion counters are emitted only after the PostgreSQL
transaction containing the frontier transition commits; rollback emits
nothing. The 30-minute duration is the final `collector_elapsed_seconds` from
the collector's monotonic clock, while wall-clock timestamps continue to prove
heartbeat freshness and the maximum sample gap. A failed collection retains
its raw samples and returns non-zero.

Add the measured collection, the offline/startup artifacts, the operator
authorization record, and an independent reviewer disposition to the complete
bundle. Seal only that complete bundle:

```bash
uv run tracefold ops seal-workers-runtime-acceptance \
  --bundle /absolute/path/to/issue-33-evidence
```

The real gate accepts one `production_collection` proof plus the separate
`public_semantic_diff`; it has no hand-filled runtime, process, PostgreSQL,
resource, or capacity proofs. The production proof must point exactly at
`workers-runtime-collection.json`, whose `samples_sha256` binds the accompanying
`workers-runtime-samples.jsonl`. The sealer rereads all 181 JSONL rows,
revalidates every sample, and recomputes duration, deadline misses, quarantine,
capacity, runtime/process/container identity, restart/readiness, PostgreSQL,
and resource limits. The real gate's declared duration, miss/quarantine counts,
and capacity rows must equal that recomputed summary exactly; editing either
the collection summary or any raw sample fails sealing.

The sealer revalidates the embedded first-sample production query audit and
recomputes its summary, every sampled wait distribution, required metric-family
presence, per-series resource monotonicity, and independent admission/service
interval deltas from the bound JSONL. It
also requires semantic and permission passes, runtime/model-reservation
evidence, and reviewer pass. The declared commit must equal the current
checkout HEAD; the checkout must have no tracked or untracked changes, the
absolute operator config path must exist, and every raw artifact must bind the same
repository/session/cutoff, commit/migration, and redacted enablement. Every
passing proof and the independent review must bind an `artifact_path` plus its
actual SHA-256 to a regular JSON file inside the bundle. Raw proof files use
`workers_runtime_raw_evidence_v1`; they contain typed per-proof records rather
than bare `{ "ok": true }` assertions. The sealer independently checks all three
semantic-domain hash pairs, zero serving writes, all five migration states, the
operator-authorized no-backup/fix-forward boundary, and continuous runtime
samples with no gap over 15 seconds or runtime/process identity change. It
rejects restore, waiver, placeholder, and retired hand-filled production proof
records.
The `workers_runtime_independent_review_v1` artifact must identify the reviewer
and bind the exact path/hash set of every raw proof artifact, including both
production collection files. The sealer hashes every bundle file and refuses
post-seal changes.
