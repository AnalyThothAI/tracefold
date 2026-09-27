# Operations

[Handbook](README.md) · [Setup](SETUP.md) · [Architecture](ARCHITECTURE.md) · [Migrations](MIGRATIONS.md)

This runbook covers the running EventUpdate application and its separate execution
process. Module guides explain the algorithms; this page explains what to inspect
and which operational action changes state. Commands below are examples, not a
request to run maintenance, spend model credits or operate an account.

## 1. Start with read-only evidence

```bash
make status-app
make status
make logs
uv run tracefold config
docker compose exec -T workers tracefold news bus-check
docker compose exec -T workers tracefold db audit
docker compose exec -T analysis tracefold trading status
docker compose exec -T analysis tracefold trading cases --limit 20
```

`config` prints redacted configuration. The standard config uses Compose-network
addresses, so database/broker commands run inside their application container.
Do not copy raw config, token files or secret URLs into an incident report.
`bus-check` can declare the expected topology idempotently; it is diagnostic but
not a claim that no broker metadata could change.

| Question | Evidence to inspect | Not established by it |
| --- | --- | --- |
| Are required application roles healthy? | `make status-app`, service logs and readiness | News freshness, model quality or delivered cards |
| Is a capability usable? | Named capability state and error | Merely seeing its task constructed |
| Was a source processed? | Item revision, wanted/done input, semantic attempt and adopted head | Old verdict counts or a healthy HTTP process |
| Was the reader notified? | Plan, intent, frozen body and actual receipt | A completed plan or pending queue row |
| Was a source analyzed for trading? | Public update/amendment, target selection, Case attempt and decision | A bullish card or a notification threshold |
| Did an account execute? | Runtime, signed venue evidence and attributed fills | A recorded command or published Signal |

Serve and the console are read-only. Browser bootstrap does not grant command,
review acceptance, model publication or account authority.

## 2. Application and execution lifecycles

`make up` is the supported application deployment, not a command to run from an
arbitrary feature worktree. Its [Makefile](../Makefile) preflight checks the tools,
repository/source identity and required successful main-push CI evidence. It
initializes operator files, builds the app image, starts infrastructure, applies
broker policy, waits for migration exit zero, then starts Serve, Workers and
Analysis. A failed migration leaves these application roles stopped.

```bash
make up
make status-app
make logs
```

Nautilus has its own image and explicit lifecycle:

```bash
make runtime-status
make runtime-logs
# Explicit execution lifecycle actions, not part of ordinary application work:
make runtime-build
make runtime-up
make runtime-restart
make runtime-down
```

`make up` does not restart Nautilus. `make down` stops it before the remaining
stack and preserves volumes/configuration. Stopping a process does **not** prove
that its venue position is flat. A running account must be assessed before a
maintenance window; use [Execution](modules/execution.md) and [Security](SECURITY.md).
Do not delete a volume or reset operator config to diagnose startup failures.

A schema-changing deployment must coordinate all affected writers, including
Analysis and the account owner. The runtime/migration check is not an optional
shortcut to disable. Follow [Migrations](MIGRATIONS.md); a green unit test is not
permission to migrate under active exposure.

### Same-schema image replacement

`make deploy-image IMAGE_ID=sha256:<full-local-image-id>` is the supported narrow
replacement path. It requires a local complete image ID, the approved clean main
checkout and matching source/image/database schema. The target validates the
active config and recreates the application roles without building or downgrading
the database. It does not replace the independent execution image. Read its
failure before taking another action; an old tag is not a valid schema rollback.

When explicitly authorized to merge, give the squash commit an explicit summary.
Concatenated branch history can accidentally retain a CI-suppression instruction.
Confirm the resulting main SHA has a successful **push-triggered** CI run before
deployment. A PR-head result or manually dispatched run does not substitute for
that exact main-push evidence. [Testing](TESTING.md) owns the required lanes.

## 3. News: identify the failed version before retrying

```bash
docker compose exec -T workers tracefold news why EVENT_ID
docker compose exec -T workers tracefold news retry-work --help
```

Use the actual Event ID in place of `EVENT_ID`. Inspect the wanted/done input
revisions, lease and attempt, last error, adopted content revision, notification
plan and exact intent. The [News guide](modules/news.md#5-work-progress-and-recovery)
explains the independent state dimensions.

| State | Expected behavior | Appropriate next step |
| --- | --- | --- |
| Models unavailable / no semantic agent | Wake can be acknowledged while durable work remains pending | Correct the named capability configuration; do not fabricate a verdict |
| Transient failure below budget | Existing durable backoff and Janitor wake own the next attempt | Check the error and due time before intervening |
| Final semantic attempt still holds a live lease | It is still owned work, not exhausted failure | Do not reclaim it or reset another worker's budget |
| Final attempt crashed and its lease expired | Janitor exposes exhausted work as failed | Fix the cause, then retry that exact input revision if authorized |
| Latest input failed, previous head exists | Last valid EventUpdate remains readable | Do not erase the head or describe it as successful latest processing |
| Notification planning exhausted | Current plan work is visibly failed independently of card state | Retry only the current failed content revision |
| Card generation exhausted before any send ledger exists | Dead unsent intent remains attributable | Retry its exact content revision and intent |
| A send exists with unknown/terminal outcome | Receipt history must be preserved | Investigate provider evidence; `retry-work` cannot reopen it |

The explicit commands are version-scoped:

```bash
# Failed semantic input: revision is the wanted input's integer revision.
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind semantic --revision INPUT_REVISION

# Exhausted notification planner: revision is the adopted content revision.
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind notification --revision CONTENT_REVISION

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
  -> one DB pool min 2 / max 8 / max_waiting 3
     (1 singleton lock + 2 business + 4 News lane + 1 control)
  -> one pinned singleton session / business DB executor 2 / News DB lane 4 /
     control DB executor 1
  -> finite external-operation executor 3
  -> tasks: workers-probe; when News is enabled, one RabbitMQ robust connection
     and the News consumer tasks (news-receiver, news-recovery, news-deduper,
     news-semantic, news-deliverer, news-janitor); the bounded polling loops
     (news-instruments, and with venues enabled news-quotes, news-reactions);
     workers-control
```

Quote plan/store and the wallet tape use ordinary
business permits. Event Reaction and the Janitor keep the one-slot
heavy-business gate over the same pool, so heavy work is serialized without
blocking display quote progress or consuming the four News hot-path slots. The
Quote provider calls are
bounded to 12 mandatory current source groups (concurrency 4, 10 s deadline)
plus at most two post-store Binance day reads; its 20 s cadence is start-based,
non-overlapping, and does not catch up. Reactions remain bounded to 32 merged
candle requests per 60 s turn with concurrency 4. None of these loops holds a
database connection while calling out.

Every News consumer turn is one short idempotent transaction; provider and
model work happens with no database connection held. There is no generic
scheduler, projection frontier, EDF coordinator, model arbiter, database wake
plane, startup rebuild, phased load shifting, or configurable concurrency
beyond `news.triage.concurrency`.

The control child distinguishes the pinned singleton session from its pooled
heartbeat write. Loss of the pinned advisory-lock session remains immediately
fatal. A precise transient PostgreSQL admission, timeout, pool-checkout, or
connection error from the idempotent heartbeat write is retried after 250 ms;
after 15 seconds the stale heartbeat makes readiness false without killing the
root, and recovery restores readiness. Invariant failures and an unfinished
native control future remain process-fatal. This retry does not apply to
general control writes whose commit outcome could be ambiguous.

Serve owns one read pool of seven with ordinary/control admission `6/1`,
50 ms permit wait, 250 ms checkout, two-second statement timeout, JIT off,
parallel gather off, and 8 MiB work memory. The statement budget accommodates
full-history feed counts measured at about 0.8–1.3 seconds over 35k events;
it does not change request admission or Workers budgets. Connections and ordinary requests
default to read-only. The sole authenticated Trading Command POST opens a
semaphore-bounded short-lived write connection outside that pool; every other
HTTP route remains read-only. `tracefold news review submit` opens a short-lived connection under the
same `tracefold` login and uses one ordinary short transaction. Database
append-only triggers and business constraints—not an internal role ACL—protect
the review facts. Workers owns the exact pool/lane topology
above. Finite provider/filesystem operations share the three-slot
external capability; the OpenNews WSS socket remains a long-lived async root
child outside it. Only the owning source seam may map an outer
finite-operation overrun into its existing durable failure policy. A typed
recurring business-DB overrun remains local to its natural loop; its occupied
permit remains bound to the native future and the loop retries on its normal
cadence. Control-DB, model, cleanup, and unclassified overruns remain
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

The measured transaction is the true outer scope: setup, the capability-limited
callback, and commit or rollback produce one duration/outcome observation.
Callbacks receive only their News/Price/Instrument/Trading repositories. They
do not receive a raw connection and do not run provider I/O, Pydantic, hashing,
canonicalization, compression, large Python work, or backoff while PostgreSQL
is idle in transaction.

News consumers use a dedicated four-slot News DB lane
(`WorkerDatabase.run_news`: its own executor and gate, separate from the two
business slots) for short idempotent transactions; each message is one
transaction of a few milliseconds. `consume()` handles up to `prefetch`
messages concurrently with a per-message ack, so `news.triage.concurrency`
(default 4) is real concurrency and the only News concurrency knob;
single-active queues use prefetch 1. When the News lane cannot admit a message
the consumer raises `DeferError` and the message requeues uncounted through
the retry lane. Delivery restart reconciliation likewise waits out a typed
admission `DeferError` before claiming; statement overruns and unknown faults
remain process-fatal.

News has no projection lease: the broker's single-active-consumer and
per-message ack are the fences on `news.raw` and `news.triage`, and on the
delivery lane it is the row the claim holds with `FOR UPDATE SKIP LOCKED`,
leased until its next due time (#598 D2).

`/metrics` exposes low-cardinality worker transaction and shared capability
resource signals. Use shared resource and PostgreSQL activity/lock evidence for
diagnosis; CPU alone is not a root-cause claim.

News Feed search adds
`tracefold_news_search_requests_total{mode="asset|text",result="nonzero|zero"}`
and `tracefold_news_search_duration_seconds{mode="asset|text"}`. They record
successful first-page requests only; cursor pages are excluded, while repeated
browser polling remains repeated operational load. These counters are not
distinct user-search or user-session analytics. Labels never carry the raw
query, symbol, resolved identity, route, or user-controlled text.

News durable-event boundaries add the following bounded metrics. `stage`,
`outcome`, `queue`, `reason_class`, `cause`, and `budget` are closed code-owned
sets; Event/message/incident/Strategy IDs are log fields, never labels.

```text
tracefold_news_handoff_pending{stage}
tracefold_news_handoff_oldest_age_seconds{stage}
tracefold_news_handoff_repair_total{stage,outcome}
tracefold_news_handoff_expired_total{stage}
tracefold_news_rabbitmq_consumer_fatal_total{queue,reason_class}
tracefold_news_rabbitmq_publish_failure_total{reason_class}
tracefold_news_opennews_incident_open{provider,cause}
tracefold_news_opennews_incident_oldest_age_seconds{provider,cause}
tracefold_news_opennews_recovery_turn_total{outcome}
tracefold_news_opennews_recovery_provider_calls_total
tracefold_news_opennews_recovery_published_messages_total
tracefold_news_opennews_recovery_budget_exhaustion_total{budget}
```

`handoff_expired_total` is a Gauge despite its compatibility name: expiry is a
current marker-plus-age projection, not a durable transition that can be
incremented once. Counting it on each Janitor scan would manufacture growth.
The pending and expired gauges are each capped at 1,000 rows per stage; their
partial-index scans are bounded even when retained expired audit facts grow.

## Durable state and transaction rules

- PostgreSQL facts/control rows plus the durable broker queues are the only
  recovery sources.
- Every News write is idempotent by key; the broker owns retry, buffering, and
  the dead-letter lane.
- Success writes the current model and acknowledges the exact message in one
  application-owned transaction.
- Provider/network/filesystem I/O occurs outside DB transactions.
- Current rows use stable keys and skip unchanged payload writes.

## First checks

For missing or stale live data:

1. run `uv run tracefold config`;
2. check `/healthz` and `/readyz`;
3. inspect authenticated `/api/status`, then `/api/news/status`;
4. run `docker compose exec -T workers tracefold news bus-check` for per-queue depths;
5. run `docker compose exec -T workers tracefold news why <event_id>` for one Event's whole chain;
6. trace one stable target from fact -> Event row -> API.

| Symptom | Inspect first |
|---|---|
| no API row | current key and publication state |
| idle worker with expected work | durable target plus due/lease fields |
| stale row after a run | fact watermark, payload hash, zero-write comparison |
| growing queue | claim size, lease expiry, retry budget, terminal events |
| repeated source failure | target error state and deterministic terminal policy |
| readiness 503 | DB liveness and startup schema/composition |
| status degraded, readiness 200 | expected runtime/product separation |

The separate loopback Workers probe answers two questions, not one. `ok` is
basic readiness: this process still owns PostgreSQL, its schema and its
singleton session. `capabilities` is a separate object keyed by capability name
-- `news_ingestion`, `news_editorial`, `news_delivery`, `news_instruments`,
`news_quotes`, `news_reactions`, `market_notifications` -- each with a `state` of
`running`, `faulted`, `unavailable` or `disabled` and the reason that put it
there. The same object is persisted on `workers_runtime.capabilities` and
republished on `/api/status` under `runtime.workers_runtime.capabilities`, and
the console prints it as the **Workers 能力** card on 流水线状态 (`/news/status`),
so an operator sees a stopped lane without opening the loopback probe
(#553 PR-3). A stale runtime row publishes no report: a process that stopped
answering is not evidence that its lanes are still running.

An unexpected program error in one *optional* business task stops that task,
records its capability `faulted` with the failure that stopped it, and leaves
every other task running. Nothing restarts it: recovery is an operator restart
after the fix, which is why a `faulted` capability is a page-worthy fact even
while readiness stays 200. Analysis has its own process and its status is
reported on `/api/trading/status`; Workers does not own its lifecycle. A push sender that cannot be constructed from the
current configuration reports `news_delivery` `unavailable` with the
configuration reason, the Deliverer settles those Events `delivery_unavailable`
rather than presenting them as sent, and `/api/news/status` reports
`delivery.delivery_available` false; correct the configuration and restart.

News reception, admission and retention -- `news-receiver`, `news-recovery`,
`news-deduper`, `news-janitor` -- are **not** optional. They are the information
entry every other capability reads, so a program error there still fails the
root and the container restart that has always healed it still happens, rather
than becoming a permanent ingestion outage behind a 200 readiness.

Shared foundation failures are unchanged and still fail the root: PostgreSQL
unavailable, a schema that is not the code's head, a lost singleton session, an
unfinished native control future, and a graceful deadline overrun. A PostgreSQL
failure raised while a capability is being composed is also not confined: it
says the database failed, not that one Program is wrong.

A classified live broker incident or Recovery transient
is recoverable work, not a crashed task: Workers readiness stays up while
`/api/news/status` names the open incident or closed-pending recovery state as
`reason=recovery_pending|recovery_transient`, retains the typed error code, and
remains degraded.

## Domain traces

### Editorial News EventUpdate (#706)

```text
OpenNews -> RabbitMQ news.raw -> admission -> Item / Event / evidence revision
           -> durable semantic work -> RabbitMQ news.triage
           -> NewsAgent extraction + judgments -> adopted EventUpdate
                  |                              |
                  v                              v
           public outbox                   notification work
           -> App relay                    -> claim-level plan
           -> Trading catalyst or          -> selected intent -> card -> sender
              source amendment             -> exact receipt / ambiguous state
```

These commands are writes. They refuse mismatched/nonfailed targets rather than
turning them into a new work item. Semantic retry preserves checkpoints and
immutable observations/adoptions. Card retry does not erase any existing send
ledger row, including `sending`, `sent`, `ambiguous` or `terminal`, and does not
reset an independently exhausted planner budget. A new prompt/model identity
does not automatically replay completed evidence or reset failures.

Failure/defer updates are revision-scoped: an older notification or card failure
must not consume or postpone a successor's work. Do not replace the supported
operation with manual SQL resetting every attempt counter.

## 4. Broker, OI and wallet diagnosis

RabbitMQ carries raw input and semantic wake work. The queue name `news.triage`
is retained, but the Workers task is `news-semantic`. Editorial notification work
and receipts are in PostgreSQL; there is no current `news.deliver` queue to repair.
The [broker policy](../tracefold/news/broker_policy.py) owns retry/dead-letter
semantics and topology; historical queue migration recipes are not normal operation.

```bash
docker compose exec -T workers tracefold news bus-check
docker compose exec -T workers tracefold news dlq inspect --limit 20
docker compose exec -T workers tracefold news wallets --hours 24 --queue-limit 10
```

DLQ inspection is different from replay or purge. Replay re-enters the pipeline
and may produce effects according to the recorded input; purge destroys evidence.
Neither is a routine harmless way to make a queue count zero. Diagnose the exact
source contract, schema or handler error first.

For [OI](modules/oi.md), follow Item parsing status → typed observation → notification
group/anchor → send outcome, and independently public outbox → Trading Case. A
provider-format change can stop parsing while original Items continue to arrive.
Do not route OI measurements through editorial deduplication or infer entry
eligibility from whether a follow-up card was suppressed.

For [wallets](modules/wallets.md), follow roster publication → complete receipt
prefix → fill/cash attribution → detector window → episode → send-time evidence.
The largest observed block is not proof of a continuous complete prefix. Price
sampling and a slow roster provider must not be confused with first-alert evidence.

## 5. Trading and account operations

```bash
docker compose exec -T analysis tracefold trading gate --limit 20
docker compose exec -T analysis tracefold trading signals --limit 20
docker compose exec -T analysis tracefold trading commands --limit 20
docker compose exec -T analysis tracefold trading observations --limit 20
```

A `source_update` is an amendment, not a missing Trigger: it creates no Case or
fresh TTL. A valid TRADE decision can remain unpublished. WATCH needs a new bounded
conditional analysis rather than immediately ordering on a bar crossing. See
[Trading](modules/trading.md) for the public source and state contracts.

The only local operator ingress is `tracefold trading issue`, authenticated by
OS identity. Inspect its grammar before use:

```bash
docker compose exec -T workers tracefold trading issue --help
```

A submitted request must have a stable request ID and caller-sealed nanosecond
timestamp, both preserved on retries. Recording intent is not Runtime acceptance
or a venue receipt. `/pause` prevents new entries; `/flatten account` requests
reduce-only closes and pauses entry, but acceptance is not proof of flatness.
`/halt` is sticky for that Runtime lifetime; do not assume `/resume` clears it.
Never resume or restart simply to remove a diagnostic warning.

A newly enabled account slot without prior control history is not guaranteed to
start paused. Review its actual activation/control contract and venue state before
starting execution. Unexpected or unclaimed exposure requires evidence, not automatic
flattening. Keep existing protection until the owning recovery path establishes
what the venue holds.

`trading diagnose` and `trading verify-execution` are distinct from ordinary
persisted reads: the latter reads signed historical venue evidence. It requires an
exact entry ID, account slot and environment; preview is the default and `--apply`
appends verified evidence. Use the authorized credential-bearing runtime context,
not a Workers container with missing Binance mounts. Inspect:

```bash
docker compose exec -T nautilus tracefold trading verify-execution --help
```

Incomplete fills, commissions or funding stay explicitly incomplete. Do not repair
an account result by importing an unrelated environment's history, inventing a
fill, changing a Plan's ownership or folding research returns into realized PnL.

## 6. Backup and restore

A named Docker volume is persistence, not a backup. Before a destructive migration,
coordinate writers and account state as required, record the source/image and
schema identity, and take an operator-owned dump. For the standard Compose database:

```bash
umask 077
mkdir -p "$HOME/.tracefold/backups"
backup="$HOME/.tracefold/backups/tracefold-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres sh -eu -c \
  'PGPASSWORD="$(cat /run/secrets/postgres_database_password)" exec pg_dump -U tracefold -d tracefold --format=custom' \
  > "$backup"
docker compose exec -T postgres pg_restore --list < "$backup"
```

Check command exit codes and retain the matching image/config recovery information
securely. A listable dump is only an initial integrity check. Prove restoration in
an isolated database using the matching schema/source and a permitted test identity;
never restore over the operating database as a test. No numeric RPO/RTO or automatic
PITR guarantee is implemented by these commands.

`make postgres-restore-drill` is the supported isolated drill; inspect its test
resource configuration in [Testing](TESTING.md) and
[restore_drill.py](../tracefold/platform/postgres/restore_drill.py). Restore verification checks database evidence and needs no model calls or
model-release operation. A rollback across a forward-only cut needs a restored backup
and matching image, not a migration stamp or downgraded code over the newer schema.

## 7. Database performance, retention and incident records

Start with `db health`, bounded `db audit` and process logs. Default audit counts
can be estimates; an exact/deep audit is a separately requested heavier operation.
`db query-audit --help` describes bounded query diagnosis. An analyzed query really
executes against its data even when it is read-only, so its load and timing must
be considered. Inspect query/index plans before copying old tuning constants.

[Maintenance](../tracefold/news/pipeline/maintenance.py), owner repositories and
migration constraints own retention. Source evidence, active lineage, immutable
adoptions, actual receipts and rebuildable caches have different lifetimes. Do
not bypass append-only protections or delete unrecognized tables from an old list.

Record the source/image identity, exact IDs/revisions, observed times, sanitized
errors, performed actions and their receipts in the incident or PR. Historical
incidents belong in Git/issue history, not appended indefinitely to this current
runbook. Unit tests and synthetic replays are not production-health evidence.
