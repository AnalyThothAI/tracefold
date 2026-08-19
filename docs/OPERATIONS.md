# Operations

This document owns runtime configuration, worker/reliability invariants,
diagnosis, and safe repair boundaries.

## Runtime truth

The only operator-owned Tracefold application configuration is
`~/.tracefold/config.yaml`: deployment/domain choices, role-specific
PostgreSQL references, Macro source-family switches, credentials, API/auth,
models, and storage. Worker topology, cadence, deadlines, batches, leases,
retry policy, timeouts, and resource budgets are code-owned.

Confirm the active paths with `uv run tracefold config`. Never infer live state
from fixtures, examples, `.env`, generated docs, or a new CLI process. Report
paths, redacted configured booleans, source names, error classes, and command
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
| `/api/status` | `{measured_at_ms, runtime}`: database probe plus the Workers heartbeat row | bounded control read |
| `/api/news/status` | four-layer News state (`ingest`, `broker`, `pipeline`, `delivery`) plus `control` | bounded News reads |
| `make status` | PostgreSQL, migration, Serve, Workers, readiness, and console | fail-closed lifecycle check |
| `make macro-acceptance` | exact Macro contracts, current health, coverage, and ETag revalidation | persisted product-read acceptance |
| `tracefold ops ...` | explicit on-demand queue diagnosis and resolution | command-specific |

Source degradation and a missing Fed document analysis do not make the HTTP
process unready. `/api/status` has no provider block; it fails closed only on
a stale Workers heartbeat or a database/schema mismatch. Use `tracefold ops
queue-inspect` for exact on-demand queue detail; its owners are
`macro_document_analysis` (`macro_document_analysis_jobs`) and
`macro_projection` (`macro_module_frontiers`). Domain freshness and native
model-job state remain visible through `/api/news/status`, `macro status`, and
the Macro reads.

## Worker ownership

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
It wires one root `TaskGroup`; its due loops and dispositions are private
implementation details. Configuration cannot invent workers, owners, resource
lanes, or concurrency. An unknown child exception is a process failure, not an
individual-worker degraded state. The typed recurring business-DB overrun
below is the one resource-specific local recovery rule.

```text
tracefold serve
  -> read-only pool max 7 (6 ordinary + 1 control) -> HTTP/static

tracefold workers
  -> one singleton advisory lock and runtime_id
  -> one DB pool min 1 / max 8 / max_waiting 3
     (1 singleton lock + 2 business + 4 News lane + 1 control)
  -> one pinned singleton session / business DB executor 2 / News DB lane 4 /
     control DB executor 1
  -> finite external-operation executor 3 / synchronous model adapter 1
  -> spawn-only Pebble ProcessPool 1 for Macro
  -> tasks: workers-probe; when News is enabled, one RabbitMQ robust connection
     and the News consumer tasks (news-receiver, news-recovery, news-deduper,
     news-triage, news-deliverer, news-janitor); one durable-due
     task per Macro acquisition clock (macro_intraday_market, macro_settlements,
     macro_economic_releases, macro_official_state, macro_official_documents);
     projection-edf (Macro frontier coordinator); model-arbiter (Fed document
     analysis); workers-control
```

Every acquisition/projection/model task uses a short claim transaction,
bounded load plus provider/compute/model work with no database connection, and
a short compare-and-set publication transaction. The stateless EDF
coordinator polls typed Macro candidates and runs one
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

Serve owns a read-only pool of seven with ordinary/control admission `6/1`,
50 ms permit wait, 250 ms checkout, one-second statement timeout, JIT off,
parallel gather off, and 8 MiB work memory. Workers owns the exact pool/lane
topology above. Finite provider/filesystem operations share the three-slot
external capability; the OpenNews WSS socket remains a long-lived async root
child outside it. Only the owning source seam may map an outer
finite-operation overrun into its existing durable failure policy. A typed
recurring business-DB overrun remains local to its natural loop; its occupied
permit remains bound to the native future and the loop retries on its normal
cadence. Control-DB, model, CPU, cleanup, and unclassified overruns remain
process-fatal. Classification uses the typed physical capability carried by
the exception, never an operation-name or error-string prefix. A caller timeout
never releases a resource permit before the underlying future actually
completes; three stuck source futures therefore exhaust the shared external
capability even though the root heartbeat can remain healthy. Diagnose that
state from the resource-active/admission metrics and domain status. If an
underlying thread never returns, process exit is the only universal release
authority.

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

News consumers use a dedicated four-slot News DB lane
(`WorkerDatabase.run_news`: its own executor and gate, separate from the two
business slots) for short idempotent transactions; each message is one
transaction of a few milliseconds. `consume()` handles up to `prefetch`
messages concurrently with a per-message ack, so `news.triage.concurrency`
(default 4) is real concurrency and the only News concurrency knob;
single-active queues use prefetch 1. When the News lane cannot admit a message
the consumer raises `DeferError` and the message requeues uncounted through
the retry lane.

Macro projection claims retain their 30-second lease envelope.
News has no projection lease: the broker's single-active-consumer and
per-message ack are the fences.

`/metrics` exposes low-cardinality worker transaction and shared capability
resource signals. Frontier-backed domains additionally expose projection
source/candidate/written counts, queue depth, oldest-due delay, and cumulative
deadline misses. Use shared resource and PostgreSQL activity/lock evidence for
diagnosis; CPU alone is not a root-cause claim.

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

## First checks

For missing or stale live data:

1. run `uv run tracefold config`;
2. check `/healthz` and `/readyz`;
3. inspect authenticated `/api/status`, then `/api/news/status` or
   `uv run tracefold macro status`;
4. run `uv run tracefold ops queue-inspect --status active`;
5. inspect unresolved terminal events;
6. trace one stable target from fact -> frontier/queue row -> current row -> API.

| Symptom | Inspect first |
|---|---|
| no API row | current key and publication state |
| idle worker with expected work | durable target plus due/lease fields |
| stale row after a run | fact watermark, payload hash, zero-write comparison |
| growing queue | claim size, lease expiry, retry budget, terminal events |
| repeated source failure | target error state and deterministic terminal policy |
| readiness 503 | DB liveness and startup schema/composition |
| status degraded, readiness 200 | expected runtime/product separation |

## Domain traces

News:

```text
OpenNews account Strategy WSS (news.opennews_strategy_ids, validated at startup)
  -> Receiver publishes each accepted frame to RabbitMQ (confirms)
  -> q:news.raw [SAC] Deduper: Item upsert -> title/identity -> Event new|member
     -> Gate -> storyline key -> publish event.<family>.<priority> for candidates
  -> q:news.triage Triage (prefetch news.triage.concurrency): one structured
     DeepSeek call (headline_zh, why_zh, ...) + decide() -> news_verdicts
     -> verdict.push (an escalate rides the same key at AMQP priority 5)
  -> q:news.deliver [SAC] Deliverer: one Feishu attempt per Event (kind first);
     paused control settles terminal/delivery_paused
  -> tracefold news control -> news_control_state (read on every message)
  -> q:news.retry (30 s TTL) for TransientError/DeferError; q:news.dead for the rest
  -> Janitor: outbox catch-up (unpublished candidates older than 15 s), band
     expiry, 30-day purge, broker snapshot
  -> /api/news/feed + /api/news/events/{event_id} + /api/news/status
```

Broker: RabbitMQ 4 (`rabbitmq:4-management` in compose; `news.broker.url` is
the AMQP URL, `news.broker.name_prefix` prefixes every exchange/queue).
`tracefold news bus-check` connects, declares the topology idempotently, and
prints per-queue message/consumer counts. Outside a container the compose
host names resolve to the published loopback ports (`postgres` ->
`127.0.0.1:${TRACEFOLD_POSTGRES_PORT:-56532}`, `rabbitmq` ->
`127.0.0.1:${TRACEFOLD_RABBITMQ_PORT:-5672}`), so the same `config.yaml`
serves `docker compose exec` and host-side CLI runs. Consumers reconnect automatically
(robust connection); while the broker is unreachable the Receiver keeps the
WSS open, drops frames, and opens a `broker_unavailable` incident that
Recovery fills from the official Strategy hits after reconnect. Queue overflow
on `news.raw` (`reject-publish` at 100k) opens `broker_backpressure`.

Dead letters: `q:news.dead` receives permanently failed messages (schema
errors, missing events, `PermanentError`, handler crashes, `TransientError`
after 3 attempts, delivery-limit hits); it is declared with delivery limit
1,000,000 so peeking never drops evidence. `tracefold news dlq inspect
[--limit N]` peeks without consuming, `tracefold news dlq replay [--limit N]`
republishes to the topic exchange with a fresh attempt counter, and
`tracefold news dlq purge` empties it; the management UI (`127.0.0.1:15672`)
and `bus-check` show the depth. A growing DLQ with a healthy DB means a code
bug, not load. Purge only after the cause is fixed; recovered Items never
deliver, so re-driving old raw frames is safe.

Control: `tracefold news control <action> [--key K] [--ttl-minutes N]` writes
`news_control_state` directly through the Workers role; there is no broker
hop and consumers read the row on every message. `pause_delivery` makes
`decide()` drop new candidates (`override_rule` `muted`) and the Deliverer
settle already-decided verdicts as `terminal/delivery_paused` instead of
holding an unacked message; `resume_delivery` clears it. `mute_theme --key
<theme>` / `mute_symbol --key <SYM>` (`--ttl-minutes`, default 360) make
`decide()` drop matching events; `unmute --key <key>` removes a mute. State
is visible in `/api/news/status.control`.

Model failure: Triage timeouts/5xx produce degraded verdicts that are never
silent (the rule baseline still pushes watchlist primaries and provider score
>= 80 with a grounded asset; everything else drops with `degraded=true` and
is counted in `triage_degraded_24h`);
a retryable failure (timeout, rate limit, connection) gets one more attempt
inside `deadline_seconds`, and three consecutive transport failures open a
60-second circuit and a `triage_circuit_open` incident (closed again by the
next successful call). An *output* failure — the model answered but the tool
call was cut by `max_tokens` (`news_triage_output_truncated`,
`finish_reason=length`) or failed the schema (`news_triage_output_invalid`) —
is degraded the same way but never counts toward the circuit; its trace
carries `finish_reason`, `output_tokens`, and the `parsing_error` text, and
the worker logs one warning per Event. Triage output is capped at
`_TRIAGE_MAX_TOKENS` (700, code-owned): a full verdict is ~250-300 tokens, so
a rising `news_triage_output_truncated` count means the prompt grew and the
cap must follow. The verdict trace records `prompt_sha256`, `input_sha256`,
the `event_status` snapshot, `model_attempts`, and `model_failure_retryable`.
There is no second model
stage behind Triage — one Event gets one judgment and one card — so
`triage_24h` next to `triage_degraded_24h` in status is the first place to
look when pushes stop.

`/api/news/status.health` (and the console's status page) applies code-owned
thresholds from `tracefold.news.health`: ingest is `warn` after 10 min and
`bad` after 30 min without a frame, `bad` when disconnected or Workers are not
running; broker is `warn` at 50 and `bad` at 200 queued messages on a business
queue, `bad` when a business queue has no consumer, `warn` with dead letters;
model is `warn` at a 3 % and `bad` at a 10 % 24 h degraded share (the detail
names the error codes); delivery is `warn` when paused or 10 % of 24 h attempts
are terminal, `bad` at 30 %. `funnel_24h` and `reasons_24h` (Chinese labels
over `suppressed_by_reason`, `dropped_by_rule`, `throttled_by_key`,
`pushed_by_rule`, `triage_degraded_by_code_24h`) say where the day went. Every
Event's `outcome` (feed, detail, `news why`) is the same ten-kind conclusion:
`held_recovery`, `held_gate`, `queued_publish`, `queued_triage`, `dropped`,
`throttled`, `degraded_dropped`, `pending_delivery`, `delivered`,
`delivery_failed` — "no state" on the console means one of the two `queued_*`
kinds, and only those two are worth chasing as backlog.

Diagnose News in this order:

1. `/api/news/status.state` and `ingest`: `connected`, `last_frame_at_ms`,
   `strategy_warnings`, `open_incidents`.
2. `tracefold news bus-check`: consumers attached to every queue (Deduper and
   Deliverer show exactly one), `news.dead` depth, `news.retry` depth;
   `tracefold news dlq inspect` for the dead-letter bodies.
3. `pipeline`: `candidate_share_24h` (the Gate now admits nearly every Item;
   a share far below ~90% means the low-signal switch or a template flood),
   `suppressed_by_reason`, `dropped_by_rule`, `throttled_by_key`,
   `pushed_by_rule`, `labeled_missed_24h`, `triage_24h` vs
   `triage_degraded_24h`, `triage_p95_ms`, `queue_lag_p95_ms`,
   `throttled_24h`. For one Event, `tracefold news why <event_id>` prints
   raw first line -> normalized title -> gate facts -> triage verdict ->
   decide rule / throttle key -> storyline status snapshots -> delivery.
4. `delivery`: `sent_1h`, `terminal_24h`, `last_error_code`
   (`delivery_unavailable` = push disabled or webhook invalid;
   `delivery_paused` = control pause; `ambiguous_after_crash` = a send whose
   ack was lost; `hourly_cap_reached`).
5. `tracefold news eval --hours 168`: precision@push, the guardrail
   `missed_rate`/`false_push_rate`, and suppressed/missed/throttled movers from
   operator labels (`tracefold news label <event_id>
   <good|noise|late|wrong_direction|dup|missed>` on any Event;
   `good`/`wrong_direction`/`late`/`missed` count as moved, `noise`/`dup` as
   flat), with per-admission/`override_rule`/`throttled_by`/asset-class/
   audience/event-type confusion tables. `tracefold news replay-decisions
   --hours 168 --min-push-magnitude 2 --no-storyline-throttle ...` re-runs
   `decide()` with a candidate policy over the same stored verdicts; change
   `news.policy` only after the replay and the labels agree. The novelty knobs
   (`--theme-hard-cap-4h`, `--asset-hard-cap-2h`, `--novel-min-magnitude`,
   `--no-restatement-drop`) trade reader volume for coverage: on the
   2026-08-18 replay (issue #61) hard caps 4/2 gave 80 pushes, 5/3 88, 6/3 92
   against 68 without novelty, with 14 restatements dropped in every setting.
   `dropped_by_rule.restatement` in `/api/news/status.pipeline` counts the
   duplicates the reader was spared; `pipeline.reasked_24h` counts Events the
   model was asked twice because a card landed while it was thinking (expect
   a handful per day; a surge means same-key floods).
6. `tracefold news replay <hits.json> [--gate-policy open|strict]`: reproduce
   Deduper+Gate on a saved provider payload without broker or model.

Retention: `news_items`/`news_events` older than 30 days are purged by the
Janitor; bands expire with their family window. Feed shows Events from the
first frame after deployment; there is no backfill of pre-V3 history.

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

## Migrations

The Alembic chain is the `20260818_0275` baseline (root; it executes
`current_schema_20260818_0275.sql` and then `runtime_roles.sql`, which
creates the `tracefold_owner`, `tracefold_serve`, `tracefold_workers`, and
`tracefold_migrate` roles when run by the bootstrap superuser, verifies the
role contract, and applies the Serve read / Workers write grants) followed
by `20260818_0276_review_49_hard_cut` and `20260818_0277_gmgn_lane_removal`.
A live database stamped `20260818_0276` upgrades to 0277 with `tracefold db
migrate`; a fresh database runs the baseline, 0276, and 0277. All three are
irreversible; a downgrade is a backup restore. Stop Serve and Workers before
applying a chained revision (each takes the maintenance gate advisory lock
and refuses to run while Workers hold the steady lock).

0276 drops `news_title_presentations`, `token_discovery_results`,
`token_discovery_dirty_lookup_keys`, `asset_profiles`,
`asset_profile_refresh_targets`, `cex_token_profiles`, `token_image_assets`,
`token_image_source_dirty_targets`, `token_profile_current`,
`token_profile_projection_frontiers`, and the four unused `checkpoint_*`
tables, and deletes their `queue_terminal_events` rows.

0277 drops the whole GMGN lane in child-before-parent order:
`news_event_market_marks`, `asset_identity_current`,
`asset_identity_evidence`, `enriched_events`, `event_anchor_backfill_jobs`,
`market_tick_current`, `market_ticks` (with its default partition),
`price_feeds`, `cex_tokens`, `token_intent_lookup_keys`,
`token_intent_evidence`, `token_intent_resolutions`, `token_intents`,
`token_evidence`, `event_entities`, `events`, `raw_frames`,
`registry_assets`, `collector_pending_items`, `persisted_live_events`,
`us_equity_symbols`, and `provider_circuit_state`; it drops the
`forbid_market_fact_update()` function and deletes the
`queue_terminal_events` rows of `event_anchor_backfill_jobs` and
`collector_pending_items`. Macro's general market fact tables
(`market_instruments`, `market_observations`, `market_settlements`,
`market_position_facts`) stay. Neither revision performs a provider, broker,
or outbound call.

Before applying 0277 remove `gmgn`, `upstream`, `providers.binance`,
`api.heartbeat_interval`, and `api.replay_limit` from
`~/.tracefold/config.yaml`; the settings schema rejects them. Verify after
restart: `tracefold db audit` reports `migration_status` `ready`, `counts` for
the Macro core tables, and `news_schema.exact` for the eleven `news_*`
tables; `tracefold news bus-check` shows one consumer on `news.raw` and
`news.deliver`; `/api/news/status.state` becomes `ready` once the WSS
connects; `/api/macro/overview` lists six modules; and the first candidate
Event receives a Triage verdict within seconds.

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

`uv run tracefold db query-audit` verifies that every public HTTP route is
assigned to a bounded read-query family (`/readyz`, `/api/status`,
`/api/news/*`, `/api/macro/*`; `/healthz`, `/metrics`, and `/api/bootstrap`
are declared no-SQL) and checks that every query can be planned. `uv run
tracefold db query-audit --analyze` executes those read-only queries with JSON
`EXPLAIN (ANALYZE, BUFFERS)` and fails on an estimated large-table sequential
scan, any temporary read/write blocks, or read/return amplification above
20:1. An empty development database proves only SQL and route coverage;
production-scale plans need a production-sized database. Each runtime owner
supplies the same bound statement builder used by its serving read; the App
layer only composes those specs with route coverage, so an audit-only SQL
approximation is not accepted.

Read/return amplification uses the root result-row count for every hot query;
no News query uses aggregate-input amplification.

Use ad hoc `EXPLAIN (ANALYZE, BUFFERS)` only on a representative bounded
query. Since `ANALYZE` executes mutating SQL, wrap `INSERT`, `UPDATE`, `DELETE`,
or `MERGE` in `BEGIN` and `ROLLBACK`.

Frontier-backed hot paths claim narrow stable keys and hydrate wide JSONB only
after selection. Partial indexes must match the real due/status predicate. An
idle frontier worker must not scan broad facts merely to prove that no work is
due. Use one representative `EXPLAIN (ANALYZE, BUFFERS)` for a
bounded evidence path; do not create a second planner-assertion control
plane. Current models remain bounded by stable product keys; a
latest-generation pointer is not a retention policy.

Projection sessions disable PostgreSQL parallel gather and JIT and use 16 MB
`work_mem`; foreground ingestion and API sessions keep PostgreSQL defaults.
This prevents two bounded background iterations from multiplying into all
available PostgreSQL CPU workers while preserving deterministic source-to-
current work. The News feed hot paths use ordered composite indexes; do not
replace them with periodic broad scans.

Compose uses the official PostgreSQL 18 Bookworm image and preloads only
`pg_stat_statements` for query diagnosis. `compute_query_id` remains enabled.
Use Compose logs for container output and the supported Tracefold database
health, audit, query-audit, status, metrics, and `ops` commands for diagnosis
and repair. There is no repository `ops/` infrastructure tree, auxiliary
observability service, host log collector, or persistent diagnostic script.

For an ordinary migration or production cutover:

1. stop writers or establish a maintenance boundary;
2. take and verify a PostgreSQL backup;
3. record Alembic head and non-empty fact/read-model counts;
4. apply migrations with bounded lock and statement timeouts;
5. verify the same fact identities and expected counts;
6. start one writer per current model, then verify readiness, queue movement,
   and unchanged-projection zero-write behavior;
7. retain the backup until the new runtime passes smoke checks.
