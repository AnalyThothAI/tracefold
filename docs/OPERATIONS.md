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

# Dead, unsent card only: both content revision and intent must match.
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind card --revision CONTENT_REVISION --intent INTENT_ID
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
