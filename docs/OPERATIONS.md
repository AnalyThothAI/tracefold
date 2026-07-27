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
configured feeds -> fetch receipts -> observations -> Article revisions
  -> Identity v2 candidate recall + proof-ladder Event Story projection
  -> Latest / Priority Story views
  -> Brief Selection -> Proposal -> Activation -> Active
  -> content-addressed immutable Brief / Story analysis publication
  -> /api/news/stories + /api/news/brief + /api/news/sources
```

`news_ingest` synchronizes the configured source catalog, claims due sources,
and commits one source at a time. A failed source records its own bounded error
and does not stop the remaining claimed sources. `news_story_project` is the
sole Identity-feature, Story membership/profile/material-event writer.
`news_brief_plan` alone writes Narrative grouping, Selection, Proposal,
Activation, and Active. Ordinary candidate transitions must remain stable for
120 seconds, verified critical additions for 10 seconds, and rectifications
activate on the next planner cycle. `news_ai_publish` only claims activated
work, validates immutable Publications, and attaches generated or exactly
reused analysis; it cannot advance Active. A qualified publication-contract
change supersedes the incompatible current attachment in the claim transaction,
then exposes pending/failed until generation finishes or an exact cache hit is
reattached. `news_ai_current_targets` is the durable intent fence used to reject
an older in-flight contract after rolling deploys or contract reversions.
Superseded and late Publications remain immutable history, not current state.

Provider retries and terminal failure never roll back or hide Article/Story
facts. Diagnose acquisition through `/api/news/sources`, deterministic Brief
state through `/api/news/brief`, and all five correctness layers through
`/api/status`. There is no legacy News repair, provider-item, page-projection,
current/fallback Brief, or compatibility command.

News health uses due work and persisted invariants rather than arbitrary
headline age:

| Layer | Target | Degraded | Failed |
|---|---:|---:|---:|
| Source fetch | each source refresh interval | 2 overdue cycles | 5 overdue cycles |
| Revision → Story | ≤15s | >30s | >120s |
| Story → public read | ≤30s | >60s | >180s |
| Ordinary Proposal → Activation | ≤150s | >180s | >300s |
| Verified critical → Activation | ≤40s | >60s | >120s |
| Rectification → Activation | next cycle, ≤30s | >45s | >90s |
| Activation → Brief read | ≤30s | >45s | >90s |
| AI queue | never blocks facts | >5m | terminal validation, exhausted attempts, or stuck lease |

`planner_active_mismatch` is degraded as soon as a Proposal is mature and
failed after more than two 30-second planner cycles. Pointer closure, exact
Selection bundle identity, Story counters/profile/material-hash closure, and
attached Publication synthesis identity have zero tolerance. Story/public and
Activation/Brief reads are PostgreSQL transaction-closed rather than queued
projections: a closed pointer reports zero visibility lag, while any closure
violation fails the layer instead of waiting out a freshness budget. Lane-
specific `proposal_activation_lag` reasons retain the ordinary, verified-
critical, and rectification SLO thresholds even though a mature unactivated
Proposal already raises the stricter `planner_active_mismatch`. Every health
reason returns its measured lag/value and threshold.

Material closure treats a FeedObservation as closed when it either introduced
an ArticleRevision directly or exactly matches an existing Revision of the
same publisher artifact. Repeated acquisition evidence is therefore retained
without being misreported as a missing material revision. Primary membership
is counted once per Article even when that Article has multiple revisions, and
equal-millisecond MaterialEvents use the projector's revision order.

#### News Identity v2 hard-cut runbook

Migration `20260727_0199` preserves Source Registry, FetchReceipt,
FeedObservation, Article, ArticleRevision, and Article content snapshots. It
destructively removes all old identity/Story/Brief/analysis derived products.
For this cutover the operator explicitly accepted an irreversible migration
without a backup. There is no backup-receipt table, compatibility path,
downgrade, or database restore point.

1. Stop all application workers so no 0198 writer can run during migration.
2. Record the Alembic head and non-empty material-fact counts.
3. Run `uv run tracefold db migrate`.
4. Verify that the Alembic head is `20260727_0202` and the preserved
   material-fact identities/counts are unchanged.
5. Run
   `uv run tracefold ops rebuild-news-stories --batch-size 100 --execute`.
   This replays every preserved ArticleRevision through the same sequential
   projector used by live catch-up.
6. Start the four News workers, then verify migration head, zero projection
   backlog, Story count/membership closure, Latest/Priority order, Active Brief,
   AI provenance, and all five News health layers. If validation fails, keep
   writers stopped and repair forward from the preserved material facts.

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
