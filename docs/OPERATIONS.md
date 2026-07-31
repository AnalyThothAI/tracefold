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
| `/readyz` | DB liveness plus cached startup schema/composition | no queue inspection |
| `/api/status` | serve snapshot plus persisted worker status | bounded control read |
| `tracefold ops ...` | explicit on-demand diagnosis and repair | command-specific |

Queue backlog, optional provider degradation, and an Agent-authored Macro
evidence gap do not make the HTTP process unready. Research run and publication
state remain visible through their own API and operator diagnostics.

## Worker ownership

`src/tracefold/app/worker_manifest.py` is the exact 15-unit executable
inventory. The units are logical lifecycle/clock owners inside one workers
process, not separate containers or OS processes. Configuration cannot invent
names or owners.

```text
tracefold serve
  -> read-only pool -> HTTP/static/shared persisted-live WebSocket poller

tracefold workers
  -> one singleton advisory lock and runtime_id
  -> realtime DB executor 1 / background DB executor 1
  -> provider executor 3 / model executor 1
  -> Pebble spawn ProcessPool 1
  -> acquisition clocks + one EDF projection coordinator
  -> one model-generation coordinator
```

Every acquisition/projection/model task uses a short claim transaction,
bounded load plus provider/compute/model work with no database connection, and
a short compare-and-set publication transaction. The stateless EDF
coordinator polls typed Radar, Macro, News, and Profile candidates and runs one
eligible semantic shard at a time, ordered by the real freshness deadline.
After a productive or failed shard turn it repolls immediately; only an idle
turn waits on the 50 ms polling cadence. This preserves single-shard resource
ownership without spending a fixed sleep budget while a typed frontier is
backlogged.
`deadline_at_ms` is not a not-before time. Material changes are eligible
immediately; `next_attempt_at_ms` delays only a scheduled recheck or retry.
The eligibility expression has a bounded partial index on every projection
frontier, so an idle poll does not scan future News expiry rows. There is no
generic scheduler, database wake plane, startup rebuild, phased load shifting,
or configurable concurrency.

The exact steady units are `collector`, `market_tick_stream`,
`market_tick_poll`, `event_anchor_capture`, `resolution_refresh`,
`macro_intraday_market`, `macro_settlements`,
`macro_economic_releases`, `macro_official_state`,
`macro_official_documents`, `news_ingest`, `asset_profile_refresh`,
`token_image_mirror`, `steady_projection_coordinator`, and
`model_generation_coordinator`.

Serve owns a read-only pool of eight with ordinary/search/control admission
`6/1/1`, 50 ms permit wait, 250 ms checkout, one-second statement timeout,
JIT off, parallel gather off, and 8 MiB work memory. Workers owns one pool of
twelve, the four explicit executors above, one spawn-only Pebble CPU child,
and a process-wide ProviderGovernor (`global=3`, `per-host=2`, GMGN Profile,
Binance Profile, and image lanes each `1`). A provider-wide failure opens
durable circuit state and consumes no target attempt.

One semantic shard is capped at 10,000 input rows/4 MiB and 1 MiB output.
Claim, compute, publish, and full-turn hard timeouts are respectively 500 ms,
2 s, 1 s, and 5 s. Overflow is split deterministically or quarantined as
`shard_oversized`; it is never sampled or truncated.

Radar uses two stable shard granularities inside the same EDF coordinator.
A `target × window` shard recomputes and persists only that target feature;
a `RankSet × window × venue` shard reads the compact feature set once, ranks
it, hydrates only selected identities, and atomically publishes the serving
closure. Target changes coalesce into the stable RankSet row. If a RankSet is
already running, the latest input is held in its pending fields and promoted
back to `dirty` after the claimed snapshot publishes, so sustained input
cannot invalidate every publication attempt. There is no extra Radar worker,
queue, or wake plane.

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
  `worker_queue_terminal_events`.
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
  -> target x window feature frontier -> compact target feature
  -> coalesced RankSet x window x venue frontier -> atomic rank closure
  -> token_radar_current_rows
```

Market current is maintained transactionally with `market_ticks`; it has no
projection worker or dirty queue. Repair uses bounded
`tracefold ops rebuild-market-current --execute` fact replay.

News:

```text
source claim -> news_ingest -> one provider attempt -> receipt/observation/item
  -> typed identity frontiers + persisted features/similarity edges
  -> bounded affected component Story/member/alias closure
  -> at most 64 stable hourly score-bucket frontiers
  -> set-based item/Story score publication
  -> /api/news/feed + /api/news/stories/{story_id}

Top-8 changed Story fingerprint
  -> model_generation_coordinator
  -> one admitted model lane
  -> validated Chinese immutable publication + current pointer
  -> /api/news/brief
```

`news_ingest` claims one due source in a short transaction, performs provider
I/O outside the database under the process-wide ProviderGovernor, and closes
the source in a short transaction. One source failure records a
failed receipt, increments its failure count, and cannot block another source.
A successful response retains ETag and Last-Modified; a `304` still permits
the deterministic 96-hour expiry/recluster pass. Direct transport/403/429/5xx/
HTML/non-RSS failure can use the configured relay only for a code-owned public
HTTPS source URL; the winning path and bounded diagnostics are persisted
without secrets. HTTP, localhost, Docker service names, link-local, loopback,
private, and other non-public destinations never use the relay. The internal
6551NEWS and WallStEngine RSSHub sources therefore record failures directly.

`news_ingest` is the only NewsItem writer. The EDF projection domain is the
only Story, membership, alias, feature, and edge writer. Restart re-reads typed
frontiers; it never performs a full-window steady rebuild. Unchanged
component closures write zero serving rows. The one-hour scoring epoch is one
global clock partitioned into at most 64 stable score buckets, not one timer
per Story. A bucket loads only its current members, computes outside the
database, and publishes changed item/Story score fields with set-based writes
inside one short transaction. This prevents an hourly Story-count fanout while
preserving the 60-second public Story deadline.

The model coordinator exits before any Brief model call when fewer than three
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

#### Issue #32 maintenance hard cut

The authoritative hard cut is system-wide, not a separate News migration:

1. stop the old combined runtime;
2. take and verify a recoverable PostgreSQL volume snapshot;
3. run the explicit maintenance-profile `tracefold db hard-cut` command from
   `SETUP.md`;
4. require the role, semantic, queue, history-cleanup, and invariant audits to
   pass before the legacy login is revoked;
5. start serve and workers only after the command reports `cutover_ready`;
6. restore the snapshot or repair forward while still in maintenance if any
   gate fails.

Old and new writers never coexist. There is no dual read/write, compatibility
alias, automatic fallback, or rolling mixed version.

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
The model coordinator schedules Thesis after 08:50 `America/New_York` on U.S.
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
External/runtime failures retry within the configured budget; invalid model
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

For a migration or production cutover:

1. stop writers or establish a maintenance boundary;
2. take and verify a PostgreSQL backup;
3. record Alembic head and non-empty fact/read-model counts;
4. apply migrations with bounded lock and statement timeouts;
5. verify the same fact identities and expected counts;
6. start one writer per current model, then verify readiness, queue movement,
   and unchanged-projection zero-write behavior;
7. retain the backup until the new runtime passes smoke checks.

## Issue #32 production status at 2026-07-31

**Disposition: not accepted; keep Issue #32 open.**

This is a production status snapshot, not a replacement specification. GitHub
Issue #32 remains authoritative. The evidence cutoff is 2026-07-31
Asia/Shanghai, using the operator-owned Compose runtime, PostgreSQL, and
`~/.tracefold/` configuration. The deployed implementation was local `main` at
`816ff672` and Alembic `20260731_0231`. At the cutoff the local branch was 24
commits ahead of `origin/main`; no remote push had been performed.

The two role-specific composition roots, exact 15-unit logical Workers
topology, PostgreSQL singleton lock, role permissions, typed domain frontiers,
one stateless EDF coordinator, and code-owned resource contracts are deployed.
Serve, Workers, PostgreSQL, and RSSHub were healthy with zero restarts, and
`/readyz` reported the expected migration. A second Workers runtime failed fast
with exit code 3. These facts prove runtime readiness, not business freshness.

The latest quick hard cut began at 09:03 Asia/Shanghai. Serve and Workers were
stopped and replaced without overlapping writers; PostgreSQL and RSSHub
remained running. Conservative external readiness returned by approximately
09:05:15. The operator explicitly waived a database backup for this cut. That
waiver is not restore evidence and does not satisfy the snapshot step in the
Issue #32 cutover recipe.

### Closed findings

Migration `20260731_0231` removed the hourly per-Story score-frontier fan-out
and replaced it with exactly 64 stable `score-bucket:00` through
`score-bucket:63` frontiers. Production contained zero legacy
`score:<story_id>` rows. A controlled reconcile of all buckets completed in
12.111 seconds with no unresolved miss or quarantine. The semantic audit
covered 3,053 desired item rows and 2,396 active Stories with zero item and
Story mismatches.

The production-scale News benchmark covered approximately 2,397 active Stories
and 3,053 items, with at most 51 Stories and 301 items in one bucket:

| News score stage | p50 | p95 | max |
|---|---:|---:|---:|
| bounded load | 13.42 ms | 24.78 ms | 146.79 ms |
| pure compute | 4.17 ms | 7.70 ms | 28.14 ms |

A real publish path for 39 Stories and 41 items was also rehearsed inside a
rolled-back transaction; the rollback left serving state unchanged. The prior
2,236 independent hourly Story clocks are therefore closed at their cause,
without Redis, Kafka, Celery, or a general scheduler.

The production query audit covered every declared public query family with no
route gap, plan violation, or temporary-block violation. Lexical search
executed in 23.324 ms with 2.48:1 base-relation read amplification; substring
search executed in 54.325 ms with 9.20:1 amplification. Both are below the
20:1 gate. Role, schema, foreign-key, current-model, and publication-state
audits passed, with zero missing foreign keys.

Automated verification passed:

- full backend suite: 495 passed, 1 skipped, 17 warnings, and 7 subtests in
  251.13 seconds;
- targeted coordinator, cadence, telemetry, and Radar suite: 13 passed;
- `make check`: Ruff, mypy, frontend typecheck/lint/architecture, backend
  architecture/contracts, and Python compilation.

A full operational database audit is itself maintenance load. One production
audit scanned millions of rows and incremented Radar deadline misses by 83 and
Profile misses by 2 before recovery. Run it outside the formal steady interval;
do not hide it inside acceptance.

### Failed acceptance attempts

Raw evidence is retained at
`/Users/qinghuan/.tracefold/acceptance/issue-32-20260731T055500`:

| Attempt | Evidence | Disposition |
|---|---|---|
| 1 | `failed-attempt-1.ndjson`, 13 samples | Historical pre-closure run; not a passing 30-minute interval. |
| 2 | `failed-attempt-2.ndjson`, 7 samples | Failed when News exposed 2,236 hourly score frontiers and 1,181 unresolved misses; this produced the 64-bucket fix. |
| 3 | `failed-attempt-3.ndjson`, 7 samples | Formal run stopped fail-closed at 90 seconds after Radar misses increased from 304 to 307. |

Attempt 3 began with 50 eligible Radar shards. In 90 seconds the coordinator
completed 635 iterations, including 329 successful shards, with 68.52 seconds
of aggregate processing time and approximately 108 ms mean processing time.
Containers and sampled endpoints remained healthy and 15-second snapshots
showed no unresolved miss at their exact sampling instants. The cumulative
counter still increased by three, which independently fails the zero-miss
gate.

There is no `samples.ndjson`, complete `evidence.json`, independent reviewer
pass, or sealed hash manifest. The continuous 1,800-second gate is not met, the
bundle must not be sealed, and the issue must not close.

### Remaining root cause

The remaining failure is Radar invalidation fan-out and queueing, not a slow
individual feature calculation. A production benchmark over 50 recent
5-minute targets measured:

| Radar stage | p50 | p95 | max |
|---|---:|---:|---:|
| bounded data load | 11.229 ms | 20.581 ms | 32.943 ms |
| pure feature compute | 0.009 ms | 0.918 ms | 1.189 ms |

The targets loaded 1 row p50, 3 rows p95, and 8 rows max. High-frequency
read-only diagnosis instead caught one concrete late `5m/all` frontier for
`asset:solana:token:a8ETZcRb9MMJ3UAQMczyHYCMKvdSiL1XHyirGYupump`.
It was dirty and unclaimed, had no `market_tick_current` row, and was observed
11,436 ms past its deadline. Its exact-resolution source was GMGN event
`gmgn:twitter_monitor_basic:5df7594c-d179-4ef4-881e-232e48458f65`.

One material event creates four target-window frontiers and can subsequently
dirty global RankSets. Ten-second source buckets usually contained only 1–8
affected targets, but the multiplication across windows, rank publication, and
the other domain frontiers exceeded what the single-shard EDF path could always
close inside the 5-minute product's 10-second deadline.

Commit `816ff672` removed avoidable cadence sleep: productive and failed turns
now repoll immediately, and only an idle turn waits 50 ms. After deployment, a
preflight starting from zero misses still recorded three new Radar misses by
16 seconds and six by 30 seconds. The current Issue #32 assumption that one
Radar compute shard is exactly one `target × window` is therefore disproved at
production load.

Redis, Kafka, Celery, APScheduler, or a general PostgreSQL task library would
move or wake the same queue without reducing invalidation count or service
time. Splitting the 15 logical units into separate worker processes preserves
the fan-out while making shared PostgreSQL and CPU conservation harder.

The smallest evidence-supported next design is a bounded Radar
`window × venue` micro-batch reducer: keep a durable stable dirty-target set in
PostgreSQL; claim a fixed code-owned target batch for one window and venue;
recompute only those features; atomically rank and publish the complete
affected closure; and retain the earliest dirty deadline while work remains or
arrives. This keeps one PostgreSQL truth, one writer, stable keys, typed state,
bounded work, and the two-runtime topology.

That proposal changes an explicit Issue #32 shard clause. It is not yet an
authorized implementation and must be recorded as an issue amendment or a
focused blocker/successor rather than silently changing the acceptance target.
After that decision and implementation, start from zero unresolved misses, run
a new continuous 30-minute acceptance, obtain an independent reviewer pass,
seal the complete bundle, and only then close Issue #32.

## Issue #32 acceptance and sealing

Controlled offline workload, isolated startup/recovery, and the real continuous
30-minute run are independent gates. Tests or a healthy Compose stack cannot
substitute for the real run. Print the deliberately non-passing
`evidence.json` template from the current code:

```bash
uv run tracefold ops seal-worker-acceptance --template
```

After the operator-approved production cutover, fill a new external bundle
with measured evidence and an independent reviewer disposition. Seal only a
complete bundle:

```bash
uv run tracefold ops seal-worker-acceptance \
  --bundle /absolute/path/to/issue-32-evidence
```

The sealer requires all 15 steady paths, at least 1,800 seconds of real
continuous evidence, production query analysis without route gaps or plan
violations, resource/latency/shard/lane/queue/PostgreSQL evidence, semantic and
permission passes, runtime/model-reservation evidence, and reviewer pass. It
hashes every evidence file and refuses post-seal changes.
