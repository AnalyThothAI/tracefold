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
news_sources -> isolated RSS fetches -> news_articles
  -> single-writer Story membership/projection
  -> independent DeepSeek analysis publication
  -> /api/news/stories + /api/news/sources
```

`news_ingest` synchronizes the configured source catalog, claims due sources,
and commits one source at a time. A failed source records its own bounded error
and does not stop the remaining claimed sources. `news_analysis` claims frozen
Story evidence independently; retries and terminal failure do not roll back or
hide Article/Story state. Diagnose acquisition through `/api/news/sources` and
worker status. There is no legacy News repair, provider-item, page-projection,
or compatibility command.

Macro:

```text
sync window -> macro_observations
  -> bounded persisted-only live evidence read -> /macro + six detail pages
  -> completed-session macro_research_runs claim
  -> frozen-scope DeepAgents graph with durable PostgreSQL checkpoint
  -> immutable macro_research_publications row
```

`macro_sync` owns provider catch-up and fact persistence. One failed sync
window is isolated; the worker continues its bounded batch so an unhealthy
bundle cannot head-of-line block unrelated due windows.

The live evidence API reads `macro_observations` directly with bounded
per-series history and the existing concept/date index. It has no queue,
projection worker, snapshot table, or semantic readiness state. Diagnose a
missing metric from its exact concept/source/series facts and sync health;
never repair it by adding a page-level evidence gate.

`macro_research` waits for its configured settle delay, creates or re-reads one
stable completed-session run, and claims at most one due run per iteration.
The run freezes `session_date`, market cutoff, and seal time before model work.
All model and evidence-tool I/O occurs outside a database write transaction.
The Agent decides its research plan, evidence selection, subagent delegation,
counterevidence, gaps, review, and final Chinese narrative; there is no
application-owned semantic readiness or conclusion gate.

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
session-keyed publication rejects update and delete; replaying a published
session performs zero model calls and zero publication writes.

`uv run tracefold macro retry-research --session-date YYYY-MM-DD` is the only
manual recovery from `failed`. It atomically grants one immediately due
attempt, clears the old lease/error, and returns an auditable JSON receipt.
Missing, non-failed, or already-published sessions are explicit no-ops.

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
