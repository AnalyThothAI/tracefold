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
make logs
make down
```

`make up` preflights `uv`, Docker, Compose, `curl`, and daemon access, runs
idempotent initialization, builds the Python/React image, starts PostgreSQL,
requires the one-shot migration to succeed, starts Serve and Workers, and then
runs the same fail-closed status gate. On failure, use `make logs`; rerunning
`make up` preserves operator config, four password files, and named-volume data.
`make down` stops containers without deleting that volume.

Fresh PostgreSQL role bootstrap belongs only to the image's `initdb` phase. It
creates a non-login owner plus the separate Serve, Workers, and migrate roles
from their mode-`0600` password files, revokes the bootstrap login, and only
then permits migration. It is not a periodic reconciler and will not mutate an
unknown non-empty cluster. Such a cluster must already satisfy the role/schema
contract or use an independently authorized maintenance cutover.

`make status` prints Compose state and returns non-zero unless PostgreSQL,
migration, Serve, Workers, the Serve and Workers readiness endpoints, and the
HTML console all pass. It must not be replaced by a liveness-only `curl` or a
Compose command whose exit status ignores an unhealthy Worker.

## Health and status

| Surface | Meaning | SQL/queue inspection |
|---|---|---|
| `/healthz` | process liveness | none |
| Serve `/readyz` | DB liveness plus cached startup schema/composition | no queue inspection |
| Workers `/readyz` | root running, singleton session healthy, and latest O(1) heartbeat persisted within 15 s | no queue inspection |
| `/api/status` | serve snapshot plus persisted worker status | bounded control read |
| `make status` | PostgreSQL, migration, Serve, Workers, readiness, and console | fail-closed lifecycle check |
| `tracefold ops ...` | explicit on-demand diagnosis and repair | command-specific |

Queue backlog, optional provider degradation, and a missing Fed document
analysis do not make the HTTP process unready. Domain freshness and native
model-job state remain visible through their own API and operator diagnostics.

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
  -> spawn-only Pebble ProcessPool 1
  -> acquisition clocks + one fixed-period News Story writer
     + one EDF projection coordinator
  -> one serial native-state model arbiter
```

Every acquisition/projection/model task uses a short claim transaction,
bounded load plus provider/compute/model work with no database connection, and
a short compare-and-set publication transaction. The stateless EDF
coordinator polls typed Radar, Macro, and Profile candidates and runs one
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
consumes no target attempt. A caller timeout never releases a resource permit
before the underlying future actually completes.

Each Worker DB session is exactly one bounded transaction. One transaction-local
setup statement installs the application name, statement/transaction deadlines,
JIT, parallel-gather, and work-memory policy for that transaction. PostgreSQL
restores those settings when the transaction exits, so pooling needs no reset
round trip. Every SQL statement and multi-statement repository operation is
therefore covered by the native database deadline; the async caller adds only a
bounded completion grace before treating an unfinished future as fatal.

One Radar semantic shard claims at most four targets and is capped at 10,000
input rows/4 MiB and 1 MiB output. Claim, load, CPU, and publication phases use
their native DB or CPU deadlines plus bounded completion grace. There is no
aggregate fatal projection-turn watchdog: five seconds is a soft Radar latency
observation only. Overflow is split deterministically or quarantined as
`shard_oversized`; it is never sampled or truncated. Projection claim leases
cover their legal phase envelopes: Radar is 45 seconds, Profile and Macro are
30 seconds each. The fixed-period News Story writer has no frontier lease and
retains its 25-second operation budget.
The explicit maintenance rebuild keeps the existing 120-second maintenance
transaction budget; it does not relax steady phase deadlines.

Radar is one stable `window × venue` shard inside the EDF coordinator. One turn
claims at most four due target frontiers for that shard, recomputes their compact
features, ranks the complete compact population once, hydrates only selected
identities, and atomically publishes the serving closure. Each target frontier
stores the latest input fingerprint/version and the snapshot claimed by the
running turn. Completion becomes clean only when latest still equals claimed;
otherwise the target returns to dirty with the earliest retained deadline.
There is no rank frontier, intermediate publication queue, extra worker, or
wake plane.

One `window × venue` publication remains atomic while an oversized ordered
row set is split into deterministic write batches no larger than 1 MiB. A
single row that exceeds the envelope is quarantined. The high-churn `events`
table uses a one-percent/10,000-row auto-analyze threshold so the 24-hour
Search planner does not choose a recency scan from stale time-distribution
statistics.

`/metrics` exposes low-cardinality worker transaction duration, projection
source/candidate/hydrated/written row counts, change-driven cache hit/miss,
queue depth, oldest-due delay, and the cumulative per-domain projection
deadline-miss counter. A real acceptance interval requires a zero counter
delta as well as zero sampled unresolved misses. Use these amplification and
latency signals with PostgreSQL activity/lock evidence; CPU alone is not a
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
event -> intent -> resolution -> stable Radar source edges
  -> claim up to 4 target frontiers for one window x venue
  -> compact feature updates -> one atomic rank closure
  -> token_radar_current_rows
```

Market current is maintained transactionally with `market_ticks`; it has no
projection worker or dirty queue. Repair uses bounded
`tracefold ops rebuild-market-current --execute` fact replay.

News:

```text
OpenNews WSS + bounded REST recovery
  -> report/annotation merge into current NewsItem
  -> every 60 seconds load complete enabled 96-hour item window
  -> WorldMonitor clustering + classification + importance
  -> compare-and-publish current Story/member/facet/Brief selection closure
  -> /api/news/feed + /api/news/stories/{story_id}

Top-8 changed Story fingerprint
  -> native News Brief candidate
  -> serial model arbiter
  -> one serial model adapter
  -> validated Chinese immutable publication + current pointer
  -> /api/news/brief

New current Story with max OpenNews provider score > 70
  -> News-owned durable push candidate
  -> first-enable baseline suppression or one frozen highest-score Item
  -> optional deepseek-v4-flash headline translation through model adapter
  -> signed or explicitly unsigned Feishu card through finite-operation adapter
  -> explicit success or bounded durable retry/terminal state
```

News acquisition owns one persistent OpenNews WSS receiver that runs
outside the finite-operation capability, feeds a 256-event queue, and reconnects
after expected transport failures. REST recovery uses a finite-operation slot
only after the initial WSS connection, reconnect, or overflow, with page 1 and
limit 100. A healthy continuous connection performs no periodic REST call.
Persisted source state enforces a five-minute minimum interval between attempts;
reconnects during that interval coalesce behind one delayed recovery. This caps
the 30-day reconnect-storm maximum at 8,640 requests. A page that does not
contain the persisted provider-record boundary leaves `gap_unclosed=true`.
Gap closure uses the persisted gap version as a compare-and-set fence, so a
concurrent disconnect or buffer overflow cannot be overwritten as healthy.
`/api/news/sources` shows the live connection,
last recovery, current error, and whether a gap remains unclosed. There is no
second polling or acquisition-history lane.

The acquisition publication is the only NewsItem writer. The fixed-period
Story projection is the only Story/member/facet/Brief-selection writer. It
re-reads the complete current 96-hour window every 60 seconds, calculates
outside the database, rejects stale snapshots, and atomically publishes the
current closure. Unchanged inputs write zero serving rows. The operation is
bounded by 10,000 rows, 4 MiB, and 25 seconds. There are no News frontiers,
identity features, similarity edges, aliases, archive rows, or membership
history.

The native Brief candidate exits before any model call when fewer than three
Stories, fewer than two reporting origins, or an unchanged ordered Story
fingerprint is observed. On provider or validation failure it records the
failed run and keeps the last-known-good current pointer.

Enabled push requires a valid Feishu webhook but not a signing secret. With a
secret, each request carries the Feishu timestamp/signature pair; without one,
the request deliberately carries neither. A signed request that fails is never
retried unsigned. Missing model credentials or a translation error still
freezes and sends the marked original-headline card. Status and logs expose
only configured booleans and sanitized error codes, never the webhook or
signing secret.

Diagnose News in this order:

1. `/api/news/sources`: OpenNews live/recovery/gap and current error;
2. `news_items`: provider identity, bounded metadata, and content fingerprint;
3. `news_story_members` and `news_stories`: current membership closure,
   full-SHA Story ID, state fingerprint, reporting-origin count, score factors;
4. `/api/news/feed`: flat global keyset order, filters, facets, and cursor;
5. `news_brief_runs`, `news_brief_current`, and
   `news_brief_publications`: candidate fingerprint, lease/run state, current
   publication, last error, and immutable history;
6. `/api/news/brief`: ETag and truthful public state;
7. `news_push_state` and `news_push_deliveries`: baseline, frozen evidence,
   lease, attempt, retry/terminal state, and explicit receipt;
8. `/api/news/status`: warming/ready/degraded derived health.

News health has four layers:

| Layer | Healthy evidence | Degradation signal |
|---|---|---|
| ingest | OpenNews is connected, its recovery gap is closed, and at least one item has succeeded | source not configured, no item yet, disconnected stream, open gap, or current provider error |
| story | current 96-hour admitted items close into coherent current Story aggregates | missing/duplicate ownership, aggregate mismatch, projection failure, or no current Stories |
| brief | current valid publication matches the current Top-8 fingerprint, or insufficient material is explicit | no publication, expired/failed run, mismatched fingerprint, or stale last-known-good |
| push | disabled, or webhook-configured with initialized baseline and no terminal delivery | missing required webhook, uninitialized enabled state, retry backlog beyond policy, or terminal delivery error |

The HTTP service remains ready when News is degraded; the structured News
health object names the affected layer. Facts and Story cards never wait for
the model or outbound delivery.

#### Operator-authorized Issue #33 maintenance hard cut

The active production cut is system-wide, not a separate News migration. The
owner explicitly authorized a no-backup, no-snapshot hard cut and fix-forward
recovery boundary:

1. stop the old combined runtime;
2. record the exact pre-cut revision/schema/count boundary without copying or
   snapshotting production data;
3. run the explicit maintenance-profile `tracefold db hard-cut` command from
   `SETUP.md`;
4. require the role, semantic, queue, history-cleanup, and invariant audits to
   pass before the legacy login is revoked;
5. start serve and workers only after the command reports `cutover_ready`;
6. keep writers stopped and repair forward while still in maintenance if any
   gate fails.

Old and new writers never coexist. There is no dual read/write, compatibility
alias, automatic fallback, rolling mixed version, restore proof, or waiver. An
acceptance bundle must record the operator-authorized fix-forward boundary
directly; it must not substitute a waived or placeholder proof.

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
immutable analysis and completes the job. Restart reclaims an expired lease
without duplicating analysis identity. A failed analysis affects only the
document-dependent module evidence; it is not a global runtime gate.

The Macro projection domain maps changed datasets through the static
dataset/calculation/module dependency graph. One EDF turn loads only the
affected module's bounded history, computes outside the database, rechecks the
input fingerprint, and publishes that module in one short transaction.
Unchanged payloads write zero serving rows. The overview and six module routes
read only `macro_module_current`; they never call a provider/model or repair
state.

`uv run tracefold macro status` reports acquisition target counts/statuses,
each module's current health, history depth, fact cutoff and update time, and
Fed document-analysis job counts. The command performs no provider call and no
write.

Migrations `20260801_0235` and `20260801_0236` are irreversible. They delete
the retired News acquisition history and Macro publication, per-attempt, and
stored intermediate history while preserving current NewsItems, Macro facts,
targets, document analyses, and six module rows.
Migration `20260801_0237` persists the bounded OpenNews recovery boundary.
Migration `20260801_0238` creates only `news_push_state` and
`news_push_deliveries`; it performs no backfill and no outbound send.

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

Hot paths claim narrow stable keys and hydrate wide JSONB only after selection.
Partial indexes must match the real due/status predicate. An idle worker must
not scan broad facts merely to prove that no work is due. Current models remain
bounded by stable product keys; a latest-generation pointer is not a retention
policy.

Projection sessions disable PostgreSQL parallel gather and JIT and use 16 MB
`work_mem`; foreground ingestion and API sessions keep PostgreSQL defaults.
This prevents two bounded background iterations from multiplying into all
available PostgreSQL CPU workers while preserving the source-to-current
priority of Token Radar. The News current-item and asset-identity profile hot
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

For an ordinary future migration or production cutover without a separately
recorded operator hard-cut authorization:

1. stop writers or establish a maintenance boundary;
2. take and verify a PostgreSQL backup;
3. record Alembic head and non-empty fact/read-model counts;
4. apply migrations with bounded lock and statement timeouts;
5. verify the same fact identities and expected counts;
6. start one writer per current model, then verify readiness, queue movement,
   and unchanged-projection zero-write behavior;
7. retain the backup until the new runtime passes smoke checks.

## Issue #33 Workers Runtime V2 acceptance and sealing

Controlled offline workload, isolated startup/recovery, and the real continuous
30-minute run are independent gates. Tests or a healthy Compose stack cannot
substitute for the real run. This operator-authorized cut uses no backup, volume
snapshot, or restore path: a failed gate keeps writers stopped and is repaired
forward in maintenance. That is the declared cutover contract, not a waivable
proof. Print the deliberately non-passing `evidence.json` template from the
current code:

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
operator and independent review. The collector also requires all six
Prometheus families for projection deadline misses, projection soft-SLO
overruns, projection transitions, resource-active gauges, resource-admission
duration, and resource-service duration. A missing required family fails
closed instead of becoming an implicit zero.

Admission and service evidence are independent. Each requires complete
`capability`, `operation`, and `outcome` labels plus matching cumulative
`_count`/`_sum` series. Every exact series must remain present and monotonic
between samples, and each class must show a real positive count delta during
the 30-minute interval. Active capability gauges must remain within their
code-owned caps. A Radar whole turn over five seconds increments the per-domain
soft-SLO counter and is sealed as latency evidence, but that increment alone
does not fail the gate; it is not a fatal turn deadline. Counter regression or
an invalid soft-SLO metric shape still fails collection.

More generally, the collector fails closed on a dirty or changing checkout,
revision/runtime/PID/container changes, restart or OOM, stale readiness,
container memory at or above 2 GiB, PostgreSQL connection/lock/transaction
violations, resource-cap violations, deadline/quarantine evidence, malformed
field shapes, negative or non-finite (`NaN`/`Infinity`) measurements, any
cumulative-counter regression, or a non-converging durable Frontier backlog.
The transaction ceiling follows the longest active steady publication budget
(currently the eight-second News Story publication). Database-wide temporary
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
interval deltas, and recorded Radar soft-SLO counters from the bound JSONL. It
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
