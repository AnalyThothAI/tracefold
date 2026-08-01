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

## Health and status

| Surface | Meaning | SQL/queue inspection |
|---|---|---|
| `/healthz` | process liveness | none |
| Serve `/readyz` | DB liveness plus cached startup schema/composition | no queue inspection |
| Workers `/readyz` | root running, singleton session healthy, and latest O(1) heartbeat persisted within 15 s | no queue inspection |
| `/api/status` | serve snapshot plus persisted worker status | bounded control read |
| `tracefold ops ...` | explicit on-demand diagnosis and repair | command-specific |

Queue backlog, optional provider degradation, and an Agent-authored Macro
evidence gap do not make the HTTP process unready. Research run and publication
state remain visible through their own API and operator diagnostics.

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
  -> acquisition clocks + one EDF projection coordinator
  -> one serial native-state model arbiter
```

Every acquisition/projection/model task uses a short claim transaction,
bounded load plus provider/compute/model work with no database connection, and
a short compare-and-set publication transaction. The stateless EDF
coordinator polls typed Radar, Macro, News, and Profile candidates and runs one
eligible semantic shard at a time, ordered by the real freshness deadline.
After every productive, failed, or idle shard turn it waits on the fixed 250 ms
bounded polling cadence. Other due loops and the model arbiter also wait 250 ms
after productive work and retain their bounded code-owned idle cadence. This
preserves single-shard resource ownership and prevents a productive backlog
from becoming a hot poll loop.
`deadline_at_ms` is not a not-before time. Material changes are eligible
immediately; `next_attempt_at_ms` delays only a scheduled recheck or retry.
The eligibility expression has a bounded partial index on every projection
frontier, so an idle poll does not scan future News expiry rows. There is no
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
30 seconds each, and News is 60 seconds.
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
source claim -> News acquisition turn -> one bounded provider conversation
  -> receipt/observation/item
  -> typed identity frontiers + persisted features/similarity edges
  -> bounded affected component Story/member/alias closure
  -> at most 64 stable hourly score-bucket frontiers
  -> set-based item/Story score publication
  -> /api/news/feed + /api/news/stories/{story_id}

Top-8 changed Story fingerprint
  -> native News Brief candidate
  -> serial model arbiter
  -> one serial model adapter
  -> validated Chinese immutable publication + current pointer
  -> /api/news/brief
```

The News acquisition loop claims one due source in a short transaction, performs provider
I/O outside the database through the three-slot finite-operation capability, and closes
the source in a short transaction. One source failure records a
failed receipt, increments its failure count, and cannot block another source.
A successful response retains ETag and Last-Modified; a `304` still permits
the deterministic 96-hour expiry/recluster pass. Direct transport/403/429/5xx/
HTML/non-RSS failure can use the configured relay only for a code-owned public
HTTPS source URL; the winning path and bounded diagnostics are persisted
without secrets. HTTP, localhost, Docker service names, link-local, loopback,
private, and other non-public destinations never use the relay. The internal
6551NEWS and WallStEngine RSSHub sources therefore record failures directly.

The News acquisition publication is the only NewsItem writer. The EDF projection domain is the
only Story, membership, alias, feature, and edge writer. Restart re-reads typed
frontiers; it never performs a full-window steady rebuild. Unchanged
component closures write zero serving rows. Identity work admits at most four
candidate-pair blocks of at most 4,096 pairs each; edge calculation and the
final component merge run under the bounded CPU capability, not on the event
loop. The one-hour scoring epoch is one global clock partitioned into at most
64 stable score buckets, not one timer per Story. A bucket loads only its
current members, computes outside the database, and publishes changed
item/Story score fields with set-based writes inside one short transaction.
This prevents an hourly Story-count fanout while preserving the 60-second
public Story deadline.

The native Brief candidate exits before any model call when fewer than three
Stories, fewer than two physical sources, or an unchanged ordered Story
fingerprint is observed. On provider or validation failure it records the
failed run and keeps the last-known-good current pointer.

Diagnose News in this order:

1. `/api/news/sources`: enabled count, due source, last success, HTTP status,
   failure count, conditional-fetch validators;
2. `news_source_fetches`: one receipt for the source attempt, duration, parsed
   entry count, admitted/updated/observation counts, and bounded gate counts
   (`per_feed_cap`, `missing_title`, `missing_http_url`, `missing_date`,
   `future_date`, `stale_age`, and `duplicate`);
3. `news_feed_observations`: raw entry exists even when rejected;
4. `news_items`: admitted source identity and content fingerprint;
5. `news_story_members` and `news_stories`: membership closure, stable
   Story ID, state fingerprint, physical-source count, score factors;
6. `/api/news/feed`: flat global keyset order, filters, facets, and cursor;
7. `news_brief_runs`, `news_brief_current`, and
   `news_brief_publications`: candidate fingerprint, lease/run state, current
   publication, last error, and immutable history;
8. `/api/news/brief`: ETag and truthful public state;
9. `/api/news/status`: warming/ready/degraded derived health.

News health has three layers:

| Layer | Healthy evidence | Degradation signal |
|---|---|---|
| ingest | every source has a terminal first attempt, no current failures, and at least 80% succeeded in the last hour | no sources, incomplete first coverage, any current source failure, low recent coverage, or material polling backlog |
| story | active admitted items close into coherent active Story aggregates | missing/duplicate ownership, aggregate mismatch, or no active Stories |
| brief | current valid publication matches the current Top-8 fingerprint, or insufficient material is explicit | no publication, expired/failed run, mismatched fingerprint, or stale last-known-good |

The HTTP service remains ready when News is degraded; the structured News
health object names the affected layer. Facts and Story cards never wait for
the model.

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

The `20260728_0210` baseline plus irreversible migrations through the
generated Alembic head contain the current Macro fact, coverage, module,
ResearchInput, Thesis v2, Live Delta v2, Outcome Replay v2, and Fed evidence
contracts. Current reads have one v2 path. Existing v1 publications remain
byte-for-byte immutable and are available only through explicit archive reads.

```text
clock-specific target claim -> provider I/O -> typed fact + source receipt + cursor
  -> typed affected-module frontier -> EDF module-local projection
  -> stable macro_module_current row
  -> 08:50 New York macro_evidence_pack_v3
  -> deterministic bounded macro_research_input_v1
  -> one Thin DeepAgent graph invocation -> exactly one model invocation
  -> four publication gates -> immutable macro_thesis_v2
  -> immutable macro_live_delta_v2 and macro_outcome_replay_v2 snapshots
```

The five explicit Macro due loops claim only their own clock family from
`macro_acquisition_targets` with `SKIP LOCKED`. `macro backfill` and
`macro backfill-professional` enqueue and synchronously drain only their
explicit bounded targets before returning. The professional command applies
the code-owned Treasury/Fed/credit/WTI/BTC/CFTC/ETF/futures history policy.
Treasury curves, FOMC materials, policy speeches, completed BTC
settlements, fixed ETF Nasdaq daily datasets, and Yahoo futures daily
continuous proxies use a trailing five-year backfill window. Yahoo intraday
targets request one month initially and one rolling day thereafter. Credit and
WTI may retain longer reliable public history because their bounded
single-source histories are inexpensive and materially improve regime context.
Every loop claims one target/page per bounded turn. Daily settlements catch up
through the fixed 250 ms productive repoll and never expand the semantic turn
into a legacy batch.
Declared required windows remain observable in reader-facing History Depth but
are non-blocking outside the feature or claim that needs them. Optional maximum
public history and its execution state remain in the audit appendix and cannot
lower Current Health or enter Thesis prose.
Five-year targets have a lower numeric queue priority than optional deep
history, so a large enrichment crawl cannot sit ahead of current five-year
coverage. Before creating a new target, the professional command
promotes an already-current target whose durable cursor covers the requested
window; a four-day boundary change must not trigger a duplicate multi-year
fetch.
Provider I/O happens after claim commit. One
completion transaction appends normalized facts and
`macro_source_receipts`, advances the cursor, and compare-and-set completes the
target. An unchanged replay writes zero fact rows and a receipt; a changed
value appends a revision. One failed source cannot head-of-line block another
clock or target.

Diagnose a missing metric in this order: Registry `concept_id` and
`source_role`, acquisition target state/lease, last receipt/error, persisted
fact clocks (reference/published/received), reconciliation receipt, module
Coverage/Current Health/History Depth, then current-row hash. Do not repair
source or calculation errors with a frontend fallback. Paid-only capabilities
are not product rows. `untrusted_proxy` Nasdaq/Yahoo rows remain explicitly
labelled. A closed or maintenance market with its last expected bar is current,
while a healthy source can still serve degraded data and a failed source can
leave a current last expected bar.

FOMC/Board/Reserve Bank full-text facts feed
`macro_document_analysis_jobs`. The worker waits to schedule a speech until an
effective-dated role match exists or the completed FOMC history backfill proves
that the speech date has roster coverage. Each claim performs model I/O outside
the write transaction, validates exact excerpts against the frozen body, then
atomically inserts the immutable analysis and completes the job. Open or
exhausted jobs remain visible in the affected evidence scope; they do not
become a global publication gate. Restart reclaims expired leases without
duplicating analysis identity.

The Macro projection domain maps changed datasets through the static
dataset/calculation/module dependency graph. One EDF turn loads only the
affected module's declared bounded history, computes outside the database,
rechecks the input fingerprint, and publishes that module plus its feature
frontier in one transaction. Unchanged payloads write zero serving rows.
The native Thesis candidate becomes due after 08:50 `America/New_York` on U.S.
trading days, creates
or re-reads one stable session run, and claims at most one due run per
iteration. Before model work it freezes the session, cutoff, pack identity,
six ordered module payloads, prior material delta, catalysts, twelve-asset
momentum, exact fact references, and the closed condition-candidate registry.
Only facts authoritative by the cutoff may enter. `MacroResearchInputV1` is
deterministic and capped at 64 exact refs, 32 conditions, and 64 KiB. Input
compilation failure becomes a stable pre-model `failed` run with
`attempt_count=0`.

The only production adapter is the Thin profile. Each durable attempt performs
one `create_deep_agent` graph invocation and exactly one provider-native
structured model invocation. The model receives no business tools, subagents,
filesystem, todo, task, execute, search, summarization, or checkpoint surface.
It returns one call/no-call mainline, one to three causal edges for a call, at
most one alternative, at most three tensions, sparse material module
assessments, and only material asset outlooks. Code owns exact citations,
closed condition predicates, stable IDs, and the twelve-asset fact
presentation.

After a provider-success envelope, rejection is exhausted by four gates:
time/identity, evidence closure, contract validity, and write safety.
Confidence, no-call, partial history, Reviewer absence, report length, and
offline scores are not runtime gates. Provider configuration/authentication,
timeout, refusal, or missing structured mapping are pre-draft run failures.
There is no Reviewer revision loop or attempt-internal retry loop. Macro Thesis
has no runtime workspace and the Thin profile writes no checkpoint rows.

Run states are `pending`, `running`, `retryable`, `failed`, `config_error`,
`not_published`, and `published`. Leases are owner-bound crash-recovery TTLs.
External/runtime failures retry within the code-owned budget; invalid model
configuration transitions before a claim and keeps `attempt_count=0`. The
session-keyed pack, ResearchInput, publication, Live Delta, and Outcome Replay
reject mutation. A repeated deterministic evaluation input writes zero rows;
a changed input appends a new ID derived from publication plus input hash.
Replaying a published session performs zero model calls and zero publication
writes.

Post-publication cycles update only deterministic Live Delta and Outcome Replay.
Live Delta evaluates declared Thesis conditions against new persisted facts;
event checkpoints have their own state and do not affect mainline validity.
Outcome Replay creates only declared 1W/1M checkpoints and includes only assets
with a corresponding material outlook. Recovery separately explains
publication-time versus current fact availability. None of these paths edits
or republishes the Thesis.

Before applying `20260729_0216` to production, compile every available real v3
Evidence Pack into the bounded current Research Input, then complete the normal
backup, migration, restart, PostgreSQL/API/browser, and current-session model
smoke checks. A pack that cannot compile is a deployment defect; the number of
elapsed sessions is not.

`macro_thin_profile_eval_v1` remains an offline quality corpus: six module
cases, three distinct mixed sessions, and three derived gap cases. Nine
distinct real sessions are its long-horizon selection target, not a migration
gate, and synthetic directional cases never fill the target. Baseline and
candidate use the same production model twice. Candidate
factual/citation/condition errors veto a human quality disposition; its worst
causal, counterevidence, and material-asset recall cannot regress, and at least
one of causal sufficiency, counterevidence recall, or duplicate-claim count
must improve.

`uv run tracefold macro status` reports collection as
`offline_evaluation.state=collecting`, the target and remaining real-session
counts, and `blocks_deployment=false`. It validates every available pack before
reporting collection. Once the target exists it either reports the 12 selected
case IDs or `selection_blocked` with the failed corpus rule. This status read
performs no provider call and no write.

## Operator actions and retention

Supported terminal actions are:

- retry: recreate the supported source transition and record reason/time;
- archive: preserve evidence but remove it from unresolved work;
- quarantine: preserve and mark evidence for investigation.

Retired queues have no retry path. Successful operational attempts may have
short retention; failed/terminal evidence and unresolved side effects are kept
longer. Current models retain one stable row per identity. Completed Macro
research publications are immutable history and are not pruned as queue state.

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
docker compose ps --all
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
priority of Token Radar. The News latest-fetch and asset-identity profile hot
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
count. `Lock` must remain zero. Other wait types are not interpreted as a
blanket failure; their interval maxima are sealed for operator and independent
review. The collector also requires all six Prometheus families for projection
deadline misses, projection soft-SLO overruns, projection transitions,
resource-active gauges, resource-admission duration, and resource-service
duration. A missing required family fails closed instead of becoming an
implicit zero.

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
cumulative-counter regression, or a non-converging four-domain backlog.
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
