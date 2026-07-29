# Operations

This document owns runtime configuration, worker/reliability invariants,
diagnosis, and safe repair boundaries.

## Runtime truth

Real configuration is operator-owned:

- `~/.tracefold/config.yaml`: application, PostgreSQL, providers, credentials,
  and storage;
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
event -> intent -> resolution -> token_radar_dirty_targets
  -> factor edges/features -> token_radar_current_rows -> publication
```

Market current is maintained transactionally with `market_ticks`; it has no
projection worker or dirty queue. Repair uses bounded
`tracefold ops rebuild-market-current --execute` fact replay.

News:

```text
73 physical sources / 73 logical memberships
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
HTML/non-RSS failure can use the configured relay only for a code-owned public
HTTPS source URL; the winning path and bounded diagnostics are persisted
without secrets. HTTP, localhost, Docker service names, link-local, loopback,
private, and other non-public destinations never use the relay. The internal
6551NEWS and WallStEngine RSSHub sources therefore record failures directly.

The same worker is the only NewsItem, Story, membership, and alias writer.
There is no News dirty queue or repair command: restart recovery is the normal
full-window projection. Unchanged items, membership, and Story fingerprints
write zero serving rows. The hourly persisted recency epoch prevents a
two-minute worker tick from masquerading as a Story revision.

`news_world_brief` runs every 300 seconds by default. It exits before any
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

The `20260728_0210` current-schema baseline creates the exact eleven-table
WorldMonitor-backed News schema on an empty database. It contains no prior News
schema, ID redirect, dual writer, or compatibility read. An existing database
already stamped at the baseline is left intact.

1. Stop the service so no older News worker can write during the cut.
2. Confirm the intended checkout and current Alembic version.
3. Remove every retired News worker key, configure `news.relay` when relay
   fallback is required, and reject every old source field.
4. Run `uv run tracefold db migrate` and verify the repository's reported
   latest head.
5. Start exactly `news_pipeline` and `news_world_brief` with the rest of the
   service.
6. Verify exactly eleven `news_*` tables, 73 synchronized physical sources,
   73 memberships, a terminal attempt for every source, fresh receipts and
   observations, non-empty NewsItems and Stories, membership closure, the five
   HTTP routes, ETag `304`, a truthful Brief state, and zero old routes.
7. Leave the deployment stopped and repair forward if any acceptance check
   fails; there is no historical News restore path in this release.

Macro:

The `20260728_0210` baseline plus the irreversible `20260728_0212` hard-cut
and `20260728_0213` lifecycle-completion migrations
migration contains the current Macro fact, coverage, module, Thesis, Live
Delta, Outcome Replay, and Fed evidence contracts. It deletes the retired
Judgment/Research tables and paid-data placeholders; there is no archive or
compatibility read lane.

```text
clock-specific target claim -> provider I/O -> typed fact + source receipt + cursor
  -> macro_projection -> six macro_module_current rows
  -> 08:50 New York macro_evidence_pack_v3
  -> research graph -> independent reviewer bound to the draft hash
  -> at most one revision -> immutable macro_thesis_v1
  -> deterministic macro_live_delta_v1 and macro_outcome_replay_v1
```

The five automatic acquisition workers claim only their own clock family from
`macro_acquisition_targets` with `SKIP LOCKED`; `macro_backfill` claims only
explicit bounded backfills. `macro backfill-professional` enqueues the
code-owned Treasury/Fed/credit/WTI/BTC/CFTC/ETF/futures history policy in one
transaction. Treasury curves, FOMC materials, policy speeches, completed BTC
settlements, fixed ETF Nasdaq daily datasets, and Yahoo futures daily
continuous proxies use a trailing five-year backfill window. Yahoo intraday
targets request one month initially and one rolling day thereafter. Credit and
WTI may retain longer reliable public history because their bounded
single-source histories are inexpensive and materially improve regime context.
The daily-settlement worker defaults to a batch of 32 so one cold-start cycle
covers the complete automatic daily registry instead of spreading it across
multiple six-hour intervals.
Every backfill remains observable in History Depth but is non-blocking for
Current Health, module projection, Evidence Pack, and Thesis publication.
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
exhausted jobs are visible in the affected Dataset Current Health and can force
a Thesis claim to `no_call`. Restart reclaims expired leases without
duplicating analysis identity.

`macro_projection` deterministically recomputes the Calculation Registry and
six stable module rows; unchanged payloads write zero serving rows.
`macro_thesis` runs after 08:50 `America/New_York` on U.S. trading days, creates
or re-reads one stable session run, and claims at most one due run per
iteration. Before model work it freezes the session, cutoff, pack identity,
six ordered module payloads, prior Thesis, delta/catalyst packs, twelve-asset
momentum, and evidence references. Only facts received by the cutoff may enter.
All graph I/O occurs outside a database write transaction. The Evidence Pack
remains complete in PostgreSQL; one compact module decision view and its
allowed evidence refs are embedded as a frozen invocation input.

The research graph must return exactly one mainline, at most one alternative,
at most three tensions, six module roles, prior-Thesis changes, and twelve
condition-bound asset outlooks. A separately invoked Reviewer graph receives
the exact frozen Evidence Pack and exact draft hash. `revise` permits one
corrected draft and one final review; any second non-pass result leaves the
session terminal `not_published`. `pass` publication, Reviewer record, and run transition are
atomic. Missing evidence can produce `no_call`; schema, identity, cutoff,
citation, draft-hash, or Reviewer failure closes the lane. A model name ending
in `-terra`, `-sol`, or `codex` is a terminal `config_error`, not a retryable
provider failure.

Both the research and Reviewer graphs are bounded by
`macro_thesis.graph_recursion_limit` (default 48, allowed 8–128). Reaching the
bound is terminal `not_published` with `macro_thesis_agent_step_limit`; lease
heartbeats do not turn a tool or structured-output loop into an unbounded run.
The research invocation is a single graph: its exact-model DeepAgents harness
profile disables the general-purpose subagent and hides filesystem, todo,
`execute`, and `task` tools. A child graph therefore cannot escape the root
step budget.

The production `AsyncPostgresSaver` uses the run's frozen scope ID as the
stable LangGraph `thread_id`. Checkpoint tables are execution state, not Macro
facts or serving surfaces; Alembic owns their DDL. Macro Thesis has no runtime
workspace directory.

Run states are `pending`, `running`, `retryable`, `failed`, `config_error`,
`not_published`, and `published`. Leases are owner-bound crash-recovery TTLs.
External/runtime failures retry within the configured budget; invalid model
configuration transitions before a claim and keeps `attempt_count=0`. The
session-keyed pack, reviews, publication, and Outcome Replay reject mutation.
Live Delta is the single-writer stable publication-keyed current read model and
updates only when its deterministic input hash changes. Replaying a published
session performs zero model calls and zero publication writes.

Post-publication cycles update only deterministic Live Delta and Outcome Replay.
Live Delta evaluates declared Thesis conditions against new persisted facts;
Outcome Replay waits for each declared horizon and records evaluated or
insufficient outcomes. Neither path edits or republishes the Thesis.

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

Compose uses the official PostgreSQL 18 Bookworm image and preloads only
`pg_stat_statements` for query diagnosis. `compute_query_id` remains enabled.
Use Compose logs for container output and the supported Tracefold database
health, audit, query-audit, status, metrics, and `ops` commands for diagnosis
and repair. There is no repository `ops/` infrastructure tree, auxiliary
observability service, host log collector, or persistent diagnostic script.
The runtime hard-cut migration also resets the retired `powa.coalesce` and
`powa.frequency` `ALTER SYSTEM` entries before the official image takes over
the existing PostgreSQL volume.

For a migration or production cutover:

1. stop writers or establish a maintenance boundary;
2. take and verify a PostgreSQL backup;
3. record Alembic head and non-empty fact/read-model counts;
4. apply migrations with bounded lock and statement timeouts;
5. verify the same fact identities and expected counts;
6. start one writer per current model, then verify readiness, queue movement,
   and unchanged-projection zero-write behavior;
7. retain the backup until the new runtime passes smoke checks.
