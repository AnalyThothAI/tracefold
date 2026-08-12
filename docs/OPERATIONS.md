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
resource lanes, or concurrency. A child exception is a process failure, not an
individual-worker degraded state.

```text
tracefold serve
  -> read-only pool -> HTTP/static/shared persisted-live WebSocket poller

tracefold workers
  -> one singleton advisory lock and runtime_id
  -> one DB pool min 1 / max 4 / max_waiting 3
  -> one pinned singleton session / business DB executor 2 / control DB executor 1
  -> finite external-operation executor 3 / synchronous model adapter 1
  -> spawn-only Pebble ProcessPool 1 for Token Radar / Profile / Macro
  -> when News is enabled, spawn-only Pebble ProcessPool 1 for News Story
  -> acquisition clocks + fixed-period News Story and Token Radar writers
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
finite-operation overrun into its existing durable failure policy. Database,
model, CPU, cleanup, and unclassified overruns remain process-fatal. A caller
timeout never releases a resource permit before the underlying future actually
completes; three stuck provider futures therefore exhaust the shared external
capability even though the root heartbeat can remain healthy. Diagnose that
state from the resource-active/admission metrics and domain status. If an
underlying thread never returns, process exit is the only release authority.

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
bounded completion grace before treating an unfinished future as fatal.

Token Radar is one fixed-period 30-second reducer turn outside the EDF
coordinator. One turn streams only the typed Event and resolution-revision
fields needed by the reducer from the bounded twelve-hour source-time horizon
and closed action/provenance set. The load SQL follows the partial source-time
covering source-time index and covering resolution index in deterministic
replay order. The source index carries only the narrow Event fields plus a
fixed-width, non-security duplicate-text fingerprint; it never includes or
transfers the wide Event text columns. A `20001` sentinel and incremental byte
count enforce the 20,000-row and 16 MiB envelopes before full materialization.
These limits cover a representative live twelve-hour observation of
approximately 11,975 rows and 8.62 MiB before the fingerprint optimization,
without turning the envelope into a dynamic window. The reducer uses the first
eight hours to seed adjacent prior/current state at `t-4h`, replays the final
four-hour transition to `t`,
then compares prior `(t-8h, t-4h)` with current `[t-4h, t]`. It replays
fact-availability changes, opens only on a positive full-Gate false-to-true
transition, applies the four-hour episode TTL/suppression rule, and selects Top
50 by qualification time.
A production-distribution, session-local index rehearsal copied 50,941 matching
source Events into temporary tables and produced 11,502 material revisions /
7,476,901 canonical bytes (7.13 MiB) through the final v4 query and exact
fingerprint-index shape. The real repository load, including server-cursor
transfer, typed construction, and byte accounting, took 4.09 seconds on the
first read and 3.73--3.79 seconds warm. This supports the 9.0-second load cap but
does not replace the sealed post-deployment interval acceptance.
A second bounded query batch-reads identity/profile, exact trigger anchor,
current price, and recent market-cap presentation for only that selected set;
query count does not grow with Item count. The complete turn must finish within
a 12.0-second hard budget, with measured P95 at or below eight seconds. The base
phase caps are 9.0 seconds for load, 2.5 seconds for compute, and 0.25 seconds
each for presentation and publication. A phase gets the smaller of its absolute
cap and the remaining whole-turn time after later fixed phases are reserved.
Earlier unused time remains whole-turn slack; it never lets a later phase exceed
its cap, and the whole turn never exceeds 12.0 seconds. The complete public v4
snapshot is
capped at fifty Items and 96 KiB uncompressed.
Overflow or timeout fails the turn without sampling, truncation, partial
publication, or widening the source interval; the last-good singleton remains
unchanged. There is no Radar dirty frontier, claim, lease, episode table,
frontier table, history table, rejected-candidate or Gate-audit store,
quarantine, rank closure, feature cache, or second publication-state machine.

The reducer calculates outside its short publication transaction. Publication
locks one stable singleton and writes the complete payload only when its
business fingerprint changes. Before the first successful v4 sample, the public
state is `unavailable`. A complete sample publishes `current`; a known
non-streaming source or bounded projection failure publishes one generic
`stale` transition while retaining the complete LKG payload. Repeated identical
stale observations write zero serving rows, and the next complete sample
restores `current`. Restart and catch-up consist only of the next bounded
replay/presentation sample. There is no Stocks route, writer, query family, or
read model. The retained `us_equity_symbols` catalog is only a token-identity
collision guard and owns no runtime Stocks loop.

Profile and Macro projection claims retain their 30-second lease envelopes.
The fixed-period News Story writer retains its 25-second operation budget. The
long News compute runs in its own one-process lane; it cannot consume the
admission permit used by the twelve-second Token Radar turn or the short
Profile/Macro projections. Both lanes remain serial and code-owned. The
high-churn `events`
table uses a one-percent/10,000-row auto-analyze threshold so the 24-hour
Search planner does not choose a recency scan from stale time-distribution
statistics.

`/metrics` exposes low-cardinality worker transaction duration, periodic Radar
turn duration/outcome/input/output counts, unchanged/publication counts, and
hard-budget misses. Frontier-backed domains additionally expose projection
source/candidate/written counts, queue depth, oldest-due delay, and cumulative
deadline misses. A real acceptance interval requires zero Radar hard-budget
misses and no growth in any remaining durable backlog. Use these amplification
and latency signals with PostgreSQL activity/lock evidence; CPU alone is not a
root-cause claim.

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
  -> one atomic token_radar_current v4 current/LKG snapshot
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
OpenNews WSS + newest-first bounded REST overlap
  -> primary low-latency 12-hour canonical facts + annotation merge

WorldMonitor full/en + INTEL RSS catalog
  -> opt in with news.rss_enabled: true (default false)
  -> claim one due source -> conditional fetch -> first-five snapshot
  -> 96-hour breadth/corroboration facts

RSS membership expansion -> score before category Top 20
  -> physical union with OpenNews
  -> every 60 seconds WorldMonitor clustering/classification/importance
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

Every 10 seconds, current Story with max persisted OpenNews provider score > 70
  -> require non-empty provider assets and exclude CL-family-only sets
  -> News-owned durable push candidate
  -> suppress first-enable/pre-baseline/stale (>15 minute) evidence
  -> otherwise freeze one highest-score Item
  -> one Chinese-title attempt (7.5 s request, 8 s total) or immediate original fallback
  -> freeze bilingual/original presentation exactly once
  -> compact selected-Item `关联资产`/score body + optional original-link button
  -> signed or explicitly unsigned Feishu card through finite-operation adapter
  -> explicit success or bounded durable retry/terminal state
```

News acquisition always registers OpenNews. `news.rss_enabled` defaults to
`false`; only `true` registers the exact pinned catalog: 179 physical HTTPS RSS
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

The primary low-latency OpenNews lane owns one persistent WSS receiver outside
the finite-operation capability, a 256-event queue, and one bounded REST
overlap loop. Initial connection and reconnect each trigger one overlap. Queue
overflow ends the current stream, then its reconnect follows that same path.
REST starts at the newest page and reads at most eleven 100-item pages. It stops
at the provider's last page, the first provider record persisted before the
attempt, or an Item older than 12 hours, and upserts the terminating overlap row.
The source row records the bounded attempt/outcome/rejection counts, but there
is no gap flag, boundary, version, or historical backfill state.
`/api/news/sources` lists OpenNews first, then enabled RSS schedule/claim outcomes. This
inventory order is operational only and does not change Story tier or ranking.

Acquisition is the only writer of NewsItem Article facts and provider metadata.
RSS fact identity is source URL identity plus GUID, canonical URL, or the
deterministic title/publication-time fallback. OpenNews identity is its provider
record ID. An OpenNews report uses the first non-empty logical plaintext block
as the bounded title; reporting origin uses Twitter author before `newsType`,
URL host, and `opennews`. Linkless reports remain valid. Story owns only
deterministic derived columns on the same row.

The fixed-period projection is the only Story/member/Brief-selection writer. It
expands the RSS facts in pinned category-major membership order and calculates
identity, classification, corroboration, and importance before selecting the
stable Top 20 membership rows per category. It then deduplicates those rows to
one physical RSS union, appends every current OpenNews Item, and calculates the
physical Story closure. Public selection retains duplicate category
memberships, so `source_count` can exceed the distinct-origin
`unique_source_count`, without duplicating physical Story members. Calculation
runs outside the database and publishes one coherent captured closure. New
facts wait for the next 60-second turn, a load-time fingerprint prevents older
snapshots from overwriting newer ones, and unchanged inputs write zero serving
rows. The operation is bounded by 10,000 input rows, 8 MiB, and a 25-second CPU
deadline. Do not repair capacity failures with sampling or dynamic windows;
diagnose `news_story_input_row_cap`, `news_story_input_byte_cap`, or
`news_story_operation_timeout` against the RSS 96-hour and OpenNews 12-hour
populations.

The push reconciler independently scans persisted current Story evidence every
10 seconds. It performs no clustering or provider call and writes zero delivery
rows for already-seen Stories or already-ledgered selected Items, including
when cluster membership changes the current Story ID. A newly ingested report must still
enter a Story through the 60-second projection before it can qualify; a later
`news.ai_update` on an existing Story no longer waits for another Story rebuild.
Push is a live alert rather than a recovery replay: the selected Item must be
newer than the enablement baseline, at most 15 minutes old, and carry at least
one non-empty provider asset symbol. A normalized set contained entirely in
`{CL, XYZ-CL}` is also excluded. These exclusions write no delivery candidate;
mixed sets such as `{CL, BTC}` continue normally, and News reads remain
unchanged. A stale Item that only becomes scored through REST overlap is frozen
as suppressed and performs no outbound request. Each frozen retry checks the same deadline again in the
finite-operation pre-submit phase; an aged retry is atomically suppressed and
never reaches Feishu.
The selected-Item ledger lookup is index-backed. It is intentionally non-unique
so historical duplicate audit rows remain intact; candidate insertion still
writes zero new rows for any already-ledgered selected Item.

`published_at_ms` is the provider's article clock, not the score-eligibility
clock. OpenNews can publish a report first and later attach `aiRating` and its
raw `coins` asset annotation through `news.ai_update`; the Story may therefore
already exist while push remains ineligible. `provider_score_updated_at_ms` changes only when the
current numeric score fact changes. Story projection writes cannot move it, and
candidate creation freezes it as `threshold_observed_at_ms`. Therefore
`threshold -> sent|suppressed|terminal` is the local end-to-end interval from
the selected high-score fact, including Story waiting. Rows backfilled by the
0244 migration are historical approximations; validate the 90-second P95 SLO
only on clean post-migration observations. The status rollup therefore includes
only v2 frozen sent/terminal envelopes; a current waiting delivery whose
score-fact clock exceeds 120 seconds is
an immediate stalled signal. Raw WSS receipt history is still not
retained, so `published -> threshold` cannot separate provider emission delay
from transport or recovery.

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
projection never waits for an RSS sweep: a primary OpenNews fact can enter the
next fixed 60-second Story turn before any RSS attempt. An empty Top Story
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

Enabled push requires a valid Feishu webhook but not a signing secret. With a
secret, each request carries the Feishu timestamp/signature pair; without one,
the request deliberately carries neither. A signed request that fails is never
retried unsigned. Optional translation reuses the same configured direct
DeepSeek `llm.api_key`, `llm.base_url`, and `llm.news_brief_model` triple as the
Brief's middle provider; there is no independent `news.push.translation`
endpoint, key, engine, inferred default, or fallback provider. It remains an outbound
presentation adapter and does not occupy the serial model arbiter. It sends
only the selected title, makes one provider attempt under a code-owned
7.5-second request timeout and 8-second total budget, and has no durable
translation retry. After finite-operation admission, it commits a durable
dispatch fence immediately before executor submission. Recovery of a fenced
but unfrozen row never resubmits translation: it freezes an interrupted
original-title fallback and either delivers or age-suppresses that envelope. A
valid Chinese result becomes the header and the original
stays visible; Chinese input bypasses the endpoint. Timeout, invalid output,
changed numeric or asset-symbol anchors, and titles over 500 graphemes immediately
freeze the original fallback and continue delivery. The compact body shows its
provider-order, case-insensitively deduplicated asset symbols under `关联资产`
and its provider score,
plus one
`查看原文` button when a canonical HTTP(S) Item URL exists. A missing URL omits
the button. No summary, signal, grade,
Story score, source, or publication time is rendered. Status and logs expose
only configured booleans and sanitized error codes, never the webhook or
signing secret. In `tracefold config`, `translation_enabled` combines Push
enablement with the direct DeepSeek triple, while `translation_configured`
reports that triple's availability; neither field describes a second
translation configuration.
The ledger names `pending_translation` and `translation_status` also encode the
one-time preparation phase. Provider-bound rows move from `pending` through the
durable `attempted` dispatch fence to `translated` or `unavailable`; Chinese and
other pre-call outcomes can freeze directly as `not_needed` or `unavailable`.
Frozen retries never re-enter preparation. The fence supplies the attempt
clock; a normal provider outcome also freezes duration. An ambiguous crash
after fencing is conservatively counted as an interrupted failure with no
duration so at-most-once dispatch is preserved. The rolling 24-hour success
ratio target is at least 95%, latency P95 is at most three seconds, and the hard
request path stays within eight seconds. Title-too-long, freshness-budget, and
admission failures before the fence do not count as provider attempts.

The live News schema is exactly nine tables: `news_sources`, `news_items`,
`news_stories`, `news_story_members`, `news_projection_summary`,
`news_brief_selection_current`, `news_brief_current`, `news_push_state`, and
`news_push_deliveries`.

Diagnose News in this order:

1. `/api/news/sources`: primary OpenNews live/overlap outcome first, then any
   enabled RSS breadth/corroboration schedule, claim, and outcome counts;
2. `news_items`: source-local identity, source position, canonical
   headline/description/origin, bounded metadata, and content fingerprint;
3. `news_story_members` and `news_stories`: current membership closure,
   full-SHA Story ID, state fingerprint, reporting-origin count, score factors;
4. `news_brief_selection_current`: singleton projection revision, complete
   Top Story evidence, selection fingerprints/statistics, and server order;
5. `/api/news/feed`: flat global keyset order, server filters/search, and
   complete filter-bound cursor;
6. `news_brief_current`: UTC half-hour slot, frozen `active_selection`, fenced
   lease, bounded attempt/outcome/pointer telemetry, and whole
   current/LKG `served_payload`;
7. `/api/news/brief`: ETag and truthful public state;
8. `news_push_state` and `news_push_deliveries`: baseline, frozen evidence,
   lease, attempt, retry/terminal state, and explicit receipt;
9. `/api/news/status`: warming/ready/degraded layer health plus
   `live`/`recovering`/`stalled`, RSS/OpenNews ingest evidence, Story last
   success, Brief slot/current/LKG state, and separate rolling Push-translation
   evidence.

News health has four layers:

| Layer | Healthy evidence | Degradation signal |
|---|---|---|
| ingest | primary OpenNews has a successful fact/overlap, a live WSS connection, and no unresolved error; RSS reports explicit enablement and, when enabled, breadth/corroboration counters | missing OpenNews is degraded, no primary success yet is warming, and a disconnected or errored primary is degraded; enabled empty/warming/unavailable/partially failed RSS is reported as corroboration evidence without masking or overriding primary readiness; disabled RSS adds no degradation reason |
| story | the selected RSS/OpenNews population closes into coherent current Story aggregates | missing/duplicate ownership, aggregate mismatch, projection failure, Workers failure, or no current Stories yet |
| brief | the current half-hour slot completed with healthy output | no payload is `warming`; degraded output or a whole last-known-good fallback is `degraded`, with bounded slot reason/next due visible |
| push | disabled, or webhook-configured with initialized baseline, no retry/terminal delivery, and healthy rolling translation/delivery SLOs | missing required webhook, uninitialized enabled state, any retry/terminal delivery, an SLO breach, or a wait over 120 seconds |

The operator state is `stalled` when the persisted Workers runtime is no longer
fresh/running or a waiting Push delivery's score-fact clock exceeds 120 seconds.
Any non-ready layer that is not stalled is `recovering`, including a missing,
warming, disconnected, or errored OpenNews primary, no Story/Brief output yet,
pending/retry/terminal delivery, configuration errors, Brief failure, and
rolling SLO breaches; otherwise the state is `live`. A missing
`workers_runtime` row does not manufacture a stall for read-only/test contexts.

The HTTP service remains ready when News is degraded; the structured News
health object names the affected layer. Facts and Story cards never wait for
the model or outbound delivery. Each OpenNews overlap reads at most 11 REST
pages and stops at the provider's last page, stable existing current evidence,
or the 12-hour cutoff. An exhausted attempt records one bounded source outcome
and waits for the next connection event; it does not schedule another search
or create durable gap or historical-backfill state.

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

`uv run tracefold macro status` reports acquisition target counts/statuses,
each module's current health, history depth, fact cutoff and update time, Fed
document-analysis job counts, and whether the optional analysis worker
configuration is `disabled`, `unconfigured`, or active. Active means its
configuration admission conditions are satisfied, not that a worker process
heartbeat was observed. The command performs no provider call and no write.
`uv run tracefold config` exposes the same secret-free booleans.

Migrations `20260801_0235` and `20260801_0236` are irreversible. They delete
the retired News acquisition history and Macro publication, per-attempt, and
stored intermediate history while preserving current NewsItems, Macro facts,
targets, document analyses, and six module rows.
Historical migration `20260801_0237` introduced an OpenNews recovery boundary.
Migration `20260801_0238` creates only `news_push_state` and
`news_push_deliveries`; it performs no backfill and no outbound send.
Migration `20260807_0246` is the irreversible World Brief hard cut. It fails
closed if a retained OpenNews row cannot yield a canonical title, otherwise
canonicalizes only the code-owned OpenNews facts in bounded batches, leaves
disabled historical source facts unchanged, clears the rebuildable Story closure, drops
the retired Story-title table and incompatible Brief rows/schema, and installs
the singleton selection plus discriminated L1/L2/none publication state.
Current migration `20260809_0247` removes the historical gap state and facet
tables, installs public RSS scheduling/snapshot controls, hard-cuts Brief to
`news_brief_selection_current` and `news_brief_current`, clears rebuildable
Story/selection state, and deletes incompatible Push payload rows. The live
News boundary is exactly nine tables. Existing current-schema Push baselines,
selected-Item ledgers, pending/retry deliveries, receipts, and freshness fences
are preserved byte-for-byte; the migration performs no provider or outbound
call and creates no compatibility view. Cutover preflight must prove at most
100,000 `news-opennews` rows; otherwise that historical migration fails closed
with `news_world_brief_hard_cut_opennews_row_cap:<count>` before rewriting
facts. `20260809_0247` is irreversible and has no compatibility reader.

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
acceptance bundle.

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

## Token Radar v4 hard cut and acceptance

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

Migration `20260812_0255` is the current one-time transactional v4 Radar reset.
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

The independent performance bundle proves, on representative facts:

- causal replay opens only on a positive complete-Gate false-to-true crossing;
  source-time aging and removals never open or reorder a case, four-hour expiry
  suppresses a continuously true target, and a later false state plus positive
  crossing permits re-entry;
- reducer P95 at or below eight seconds, no turn above twelve seconds, no overflow,
  and a continuous 30-minute interval with zero hard-budget misses or growing
  remaining-domain backlog;
- `GET /api/token-radar` P95 at or below 100 ms for `200` and 50 ms for `304`;
- an uncompressed snapshot no larger than 96 KiB;
- no snapshot-update long task above 50 ms, no horizontal overflow, and the
  fiftieth Item remains reachable at every supported viewport;
- after the first response, unchanged snapshots return only `304`;
- committed fact to visible browser Item P95 at or below 60 seconds.

The maintained non-mock browser lane is
`uv run pytest -q tests/e2e/test_token_radar_browser_release.py`. It builds the
real React distribution, serves it from FastAPI over an isolated PostgreSQL
database, opens an initially unavailable Radar page, then persists three
independent resolved facts and runs one real Token Radar
`load`/`reduce`/`publish` sample.
The browser waits for its normal 30-second poll and proves the resulting Item
becomes visible within 60 seconds of fact persistence, exercises same-origin
icon/current-price/signal-change/market-cap rendering without frontend data
hydration, and records no update long task above 50 ms. The maintained
`web/tests/e2e/golden-paths/live-cold-load.spec.ts` browser lane separately
proves exact fifty-Item order and reachability, responsive containment, and an
API-mocked current-empty-to-fifty refresh with no long task above 50 ms. The
non-mock release lane proves unavailable-to-current publication; both test-only
harnesses use isolated state and never target an operator or production
database.

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
processing and terminal counters, last-run time, Radar row/byte gauges,
and resource active/admission/service series. A missing required family or
required Radar-labelled series fails closed instead of becoming an implicit
zero.

Admission and service evidence are independent. Each requires complete
`capability`, `operation`, and `outcome` labels plus matching cumulative
`_count`/`_sum` series. Every exact series must remain present and monotonic
between samples, and each class must show a real positive count delta during
the 30-minute interval. Active capability gauges must remain within their
code-owned caps. Worker readiness/metrics and authenticated
`GET /api/token-radar` are resolved from the actual Compose-published Workers
and Serve endpoints, including custom host ports. Each API sample performs an
unconditional `200`, then a request with that exact ETag that must return
`304`. Serve must remain the same unrestarted container and exact image/revision
as Workers. Immediately before and after each API pair, the collector reads the
same PostgreSQL `token_radar_current` singleton. The canonical payload hash and
Item count returned by Serve must match either database observation; this
two-read bracket permits one normal publication race but rejects a different
database or stale unrelated LKG. The `200` ETag must itself be the canonical
payload hash. The collector seals both latencies, response bytes, Item count,
payload hash, and a hash of the matching ETag.

The sealed Radar interval requires 59–61 completed turns for the 30-second
clock, no more than one turn between adjacent ten-second samples, a last-run age
no greater than 35 seconds at every sample, and exact reconciliation of every
processing observation with one terminal status. It requires non-empty material
input on a counter-confirmed turn during the interval. Only samples whose
processing counter advances contribute workload gauges, and every interval turn
must have exactly one such observation. The bundle seals maximum
input/eligible/public rows and input/output bytes while enforcing 20,000 rows,
16 MiB input, fifty public Items, and 96 KiB output. It also requires no `failed`
or `stale_skipped` turns;
single-writer clock regressions may remain observable as a terminal status but
cannot count as release success. Processing histogram P95 must remain at or
below eight seconds, every observed turn at or below twelve seconds, with zero
Radar deadline-counter delta and the documented `200`/`304` API P95 limits. A
Token Radar turn above twelve seconds is a hard-budget miss, preserves LKG, and
fails both collection and release acceptance. Counter regression, unexpected
cadence, or an invalid duration/workload metric shape also fails collection.

More generally, the collector fails closed on a dirty or changing checkout,
revision/runtime/PID/container changes, restart or OOM, stale readiness,
container memory at or above 2 GiB, PostgreSQL connection/lock/transaction
violations, resource-cap violations, deadline/quarantine evidence, malformed
field shapes, negative or non-finite (`NaN`/`Infinity`) measurements, any
cumulative-counter regression, or a non-converging durable Frontier backlog.
The transaction ceiling follows the longest active steady transaction budget
(currently the nine-second Token Radar evidence load). Database-wide temporary
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
presence, per-series resource monotonicity, independent admission/service
interval deltas, and recorded Token Radar duration/hard-budget counters from
the bound JSONL. It
also requires semantic and permission passes, runtime/model-reservation
evidence, and reviewer pass. The declared commit must equal the current
checkout HEAD; the checkout must have no tracked or untracked changes, the
absolute operator config path must exist, and every raw artifact must bind the same
repository/session/cutoff, commit/migration, and redacted enablement. Every
passing proof and the independent review must bind an `artifact_path` plus its
actual SHA-256 to a regular JSON file inside the bundle. Raw proof files use
`workers_runtime_raw_evidence_v1`; they contain typed per-proof records rather
than bare `{ "ok": true }` assertions. The sealer independently checks all four
semantic-domain hash pairs, zero serving writes, all five migration states, the
operator-authorized no-backup/fix-forward boundary, and continuous runtime
samples with no gap over 15 seconds or runtime/process identity change. It
rejects restore, waiver, placeholder, and retired hand-filled production proof
records.
The `workers_runtime_independent_review_v1` artifact must identify the reviewer
and bind the exact path/hash set of every raw proof artifact, including both
production collection files. The sealer hashes every bundle file and refuses
post-seal changes.
