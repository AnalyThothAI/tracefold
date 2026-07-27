# Operations

This document owns runtime configuration, worker/reliability invariants,
diagnosis, and safe repair boundaries.

## Runtime truth

Real configuration is operator-owned:

- `~/.tracefold/config.yaml`: application, PostgreSQL, providers, credentials,
  storage, and notifications;
- `~/.tracefold/workers.yaml`: enabled state, cadence, batch, lease, retry, and
  timeout settings.

Confirm the active paths with `uv run tracefold config`. Never infer live state
from fixtures, examples, `.env`, generated docs, or a new CLI process. Report
paths, redacted configured booleans, provider names, error classes, and command
results; never secret values.

## Health and status

| Surface | Meaning | SQL/queue inspection |
|---|---|---|
| `/healthz` | process liveness | none |
| `/readyz` | DB liveness plus cached startup schema/composition | no queue inspection |
| `/api/status` | authenticated typed in-memory runtime snapshot | none |
| `tracefold ops ...` | explicit on-demand diagnosis and repair | command-specific |

Queue backlog, optional provider degradation, and an Agent-authored Macro
evidence gap do not make the HTTP process unready. Research run and publication
state remain visible through their own API and operator diagnostics.

## Worker ownership

`src/tracefold/app/worker_manifest.py` is the executable inventory for
worker names, start order, queue tables, and worker-owned stable read-model
identities. `worker_factories()` is the only callable composition registry.
Configuration may disable workers but cannot invent names or owners.

Every long-running worker is a `WorkerBase` subclass:

```text
WorkerScheduler
  -> run_once()
  -> WorkerResult + duration telemetry
  -> bounded interval catch-up / backoff
```

The scheduler owns start, stop, and status. One iteration runs at a time.
Provider, DB, subprocess, and network boundaries own their explicit timeouts.

## Durable queue and transaction rules

- PostgreSQL facts/control rows are the only recovery source.
- Claims are bounded and leased with `SKIP LOCKED` or compare-and-set.
- Queue identity is the stable product target, not an event or attempt.
- Success writes the current model and acknowledges the exact claim in one
  application-owned transaction.
- Retry clears the lease and schedules a bounded future attempt.
- Exhaustion preserves the source snapshot in
  `worker_queue_terminal_events`.
- Workers re-read durable work on bounded intervals; there is no wake plane.
- Provider/network/subprocess/filesystem I/O occurs outside DB transactions.
- Current rows use stable keys and skip unchanged payload writes.

External delivery follows claim -> close transaction -> I/O -> CAS complete or
retry. It requires a durable delivery ledger and stable dedup identity.

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
| duplicate external action | dedup key and CAS delivery state |
| readiness 503 | DB liveness and startup schema/composition |
| status degraded, readiness 200 | expected runtime/product separation |

## Domain traces

Token Radar:

```text
event -> intent -> resolution -> token_radar_dirty_targets
  -> factor edges/features -> token_radar_current_rows -> publication
```

Market current is maintained transactionally with `market_ticks`; it has no
projection worker or dirty queue. Repair uses bounded
`tracefold ops rebuild-market-current --execute` fact replay.

News:

```text
117 physical sources / 120 logical memberships
  -> NewsPipelineWorker
  -> FetchReceipt + immutable FeedObservation + NewsItem
  -> full active-window cluster + Story/member/alias projection
  -> /api/news/feed + /api/news/stories/{story_id}

Top-8 changed Story fingerprint
  -> NewsWorldBriefWorker
  -> provider chain under one 60s budget
  -> validated Chinese immutable publication + current pointer
  -> /api/news/brief
```

`news_pipeline` runs every 120 seconds by default. It claims due sources in
one short transaction, performs up to 16 fetches concurrently outside the
database, and closes each source independently. One source failure records a
failed receipt, increments its failure count, and cannot block another source.
A successful response retains ETag and Last-Modified; a `304` still permits
the deterministic 96-hour expiry/recluster pass. Direct transport/403/429/5xx/
HTML/non-RSS failure can use the configured relay only for an enabled source
URL; the winning path and bounded diagnostics are persisted without secrets.

The same worker is the only NewsItem, Story, membership, and alias writer.
There is no News dirty queue or repair command: restart recovery is the normal
full-window projection. Unchanged items, membership, and Story fingerprints
write zero serving rows. The hourly persisted recency epoch prevents a
two-minute worker tick from masquerading as a Story revision.

`news_world_brief` runs every 600 seconds by default. It exits before any
model call when fewer than three Stories, fewer than two physical sources, or
an unchanged ordered Story fingerprint is observed. On provider or validation
failure it records the failed run and keeps the last-known-good current
pointer.

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

#### News WorldMonitor hard-cut runbook

Migration `20260727_0205` is intentionally destructive. It drops every
existing `news_*` table and creates the exact eleven-table WorldMonitor-backed
schema. The operator accepted no backup, no downgrade, no ID redirect, no
dual writer, and no compatibility read.

1. Stop the service so no older News worker can write during the cut.
2. Confirm the intended checkout and that Alembic currently ends at
   `20260727_0204`.
3. Remove every retired News worker key, configure `news.relay` when relay
   fallback is required, and reject every old source field.
4. Run `uv run tracefold db migrate` and verify head `20260727_0205`.
5. Start exactly `news_pipeline` and `news_world_brief` with the rest of the
   service.
6. Verify exactly eleven `news_*` tables, 117 synchronized physical sources,
   120 memberships, a terminal attempt for every source, fresh receipts and
   observations, non-empty NewsItems and Stories, membership closure, the five
   HTTP routes, ETag `304`, a truthful Brief state, and zero old routes.
7. Leave the deployment stopped and repair forward if any acceptance check
   fails; there is no historical News restore path in this release.

Macro:

```text
clock-specific target claim -> provider I/O -> typed fact + source receipt + cursor
  -> macro_projection -> six macro_module_current rows
  -> 08:50 New York Evidence Pack -> immutable daily judgment
  -> completed-session macro_research_runs claim bound to that pack
  -> DeepAgents graph + reviewer with durable PostgreSQL checkpoint
  -> immutable macro_research_publications row
```

The five automatic acquisition workers claim only their own clock family from
`macro_acquisition_targets` with `SKIP LOCKED`; `macro_backfill` claims only
explicit bounded backfills. Provider I/O happens after claim commit. One
completion transaction appends normalized facts and
`macro_source_receipts`, advances the cursor, and compare-and-set completes the
target. An unchanged replay writes zero fact rows and a receipt; a changed
value appends a revision. One failed source cannot head-of-line block another
clock or target.

Diagnose a missing metric in this order: Dataset Registry identity, acquisition
target state/lease, last receipt/error, persisted fact clocks
(reference/published/received), module dataset state/gap, then current-row
hash. Do not repair source or calculation errors with a frontend fallback.
`unavailable` licensed futures and `untrusted_proxy` Nasdaq public cross-asset
history must stay
visible.

`macro_projection` deterministically recomputes the Calculation Registry and
six stable module rows; unchanged payloads write zero serving rows.
`macro_judgment` runs after 08:50 `America/New_York` on U.S. trading days,
compiles only facts received by that cutoff, and does nothing if any critical
module is blocked. Evidence Pack and daily judgment insertion are immutable
and replay-safe. They remain readable when DeepAgents is delayed or failed.

`macro_research` waits for its configured settle delay, creates or re-reads one
stable completed-session run, and claims at most one due run per iteration.
The run freezes `session_date`, Evidence Pack ID, pack cutoff, and seal time
before model work. The later completed-session schedule never widens the
Evidence Pack's morning fact cutoff.
All model and evidence-tool I/O occurs outside a database write transaction.
The Agent decides its research plan, evidence selection, subagent delegation,
counterevidence, gaps, and final Chinese narrative. A separate reviewer returns
`pass`, `revise`, or `block`; revise permits one corrected artifact pass and
block prevents publication without hiding the daily judgment or modules.

The production `AsyncPostgresSaver` is opened through an async context factory
for each graph invocation and uses the run's frozen scope ID as the stable
LangGraph `thread_id`. `checkpoints`, `checkpoint_blobs`, and
`checkpoint_writes` preserve resumable graph state across worker/process
restarts; `checkpoint_migrations` records the installed checkpointer schema.
These tables are runtime execution state, not Macro facts or a serving surface.
Alembic owns their DDL; application startup never runs checkpointer setup.
`~/.tracefold/macro-agent-workspaces/<scope>/` is the matching persistent
calculation workspace for native `execute`; it can be inspected or rebuilt
from frozen evidence and is not a publication source.

Run states are `pending`, `running`, `retryable`, `failed`, and `published`.
While a checkpointed Agent invocation is alive, the worker renews its
owner-bound lease every one-third of the configured lease duration. The lease
is therefore a crash-recovery TTL, not a whole-research runtime limit. If the
owner compare-and-set fails, that process cancels its local analysis and never
publishes or records failure as the stale owner. Expired leases are reclaimed
while attempts remain. External/runtime failures become `retryable`; exhaustion
becomes `failed` with a sanitized error.
Publication insertion and the transition to `published` are atomic. The
session-keyed publication rejects update and delete; it closes to the exact
Evidence Pack, cutoff, citations, and reviewer disposition. Replaying a
published session performs zero model calls and zero publication writes.

`uv run tracefold macro retry-research --session-date YYYY-MM-DD` is the only
manual recovery from `failed`. It atomically grants one immediately due
attempt, clears the old lease/error, and returns an auditable JSON receipt.
Missing, non-failed, or already-published sessions are explicit errors rather
than hidden state changes.

Notifications create/aggregate the notification and activate delivery rows in
one transaction. Sending happens later outside the transaction.

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

Use `EXPLAIN (ANALYZE, BUFFERS)` only on a representative bounded query. Since
`ANALYZE` executes mutating SQL, wrap `INSERT`, `UPDATE`, `DELETE`, or `MERGE`
in `BEGIN` and `ROLLBACK`.

Hot paths claim narrow stable keys and hydrate wide JSONB only after selection.
Partial indexes must match the real due/status predicate. An idle worker must
not scan broad facts merely to prove that no work is due. Current models remain
bounded by stable product keys; a latest-generation pointer is not a retention
policy.

Compose loads `pg_stat_statements`, PoWA, `pg_stat_kcache`, `pg_qualstats`, and
`pg_wait_sampling`. Use `./scripts/pgbadger_report.sh` for log history and
`./scripts/powa_configure.sh` for bounded PoWA snapshots. The read-only
`./scripts/runtime_performance_root_fix_check.sh` reports readiness, migration
head, top SQL, worker state, and relation-size lifecycle evidence without
resetting statistics or mutating queues.

For a migration or production cutover:

1. stop writers or establish a maintenance boundary;
2. take and verify a PostgreSQL backup;
3. record Alembic head and non-empty fact/read-model counts;
4. apply migrations with bounded lock and statement timeouts;
5. verify the same fact identities and expected counts;
6. start one writer per current model, then verify readiness, queue movement,
   and unchanged-projection zero-write behavior;
7. retain the backup until the new runtime passes smoke checks.
