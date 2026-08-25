# Operations

This document owns runtime configuration, worker/reliability invariants,
diagnosis, and safe repair boundaries.

## Runtime truth

The only operator-owned Tracefold application configuration is
`~/.tracefold/config.yaml`: deployment/domain choices, role-specific
PostgreSQL references, credentials, API/auth,
models, and storage. Worker topology, cadence, deadlines, batches, leases,
retry policy, timeouts, and resource budgets are code-owned.

Confirm the active paths with `uv run tracefold config`. Never infer live state
from fixtures, examples, `.env`, generated docs, or a new CLI process. Report
paths, redacted configured booleans, source names, error classes, and command
results; never secret values.

### Paper Trading activation (#213)

Paper mode uses real persisted News/OI/liquidation facts and real public venue
bars, then writes simulated entries and exits to the production Trading ledger.
It never sends an order to an exchange. The browser is therefore showing real
production pipeline data and durable paper results, not fixture rows; paper
fills still do **not** prove spread, precision, partial-fill, liquidation, or
profitability behavior.

Before enabling it, from the clean primary checkout:

1. run `uv run tracefold config` and confirm the reported path is
   `~/.tracefold/config.yaml`, `trading.mode=paper`, and no redacted config
   error is present;
2. deploy the exact reviewed image and migration `20260826_0310`, then require
   `make status` to pass;
3. set only `trading.enabled=true`, leaving the mode `paper`, and run
   `make up` followed by `make status` again;
4. require `uv run tracefold trading status` to report `enabled=true`,
   `mode=paper`, and control `RUNNING`; inspect `/api/trading/status` and
   `/api/trading/orders` without writing through HTTP;
5. wait for an eligible production trigger. Do not seed a production Case or
   Order to make the page non-empty. `oi_momentum_v1` and
   `news_oi_alignment_v1` are the only capital strategies in paper.

The first enabled worker turn durably registers both liquidation strategy
identities; any older frame is refused from the holdout. Every later live
Strategy 2000 frame should create two shadow evaluation rows and zero
cases/orders. After the one-hour horizon plus the next 5-minute close, status
should move fully measured cohorts from evaluated to completed. The report must show all six
horizons; 5s/30s/1m and funding currently appear as named missing data because
the source is 5-minute trade-price closes. Cohorts are separated by strategy,
venue and liquidity bucket and include bootstrap, MFE/MAE and cost assumptions.
Event-study v3 owns those research exit/cost assumptions as constants; editing
the capital Order configuration cannot rewrite a pending evaluation.
Their promotion gate must remain false with source/coverage/cost reasons;
changing the YAML cannot
override the code-owned `shadow` permission. To stop new paper cases without
losing audit history, restore `trading.enabled=false` and redeploy. Existing
paper orders remain ledger facts and should be allowed to reconcile/close under
the reviewed rollback procedure rather than being deleted.

Before deploying #129, remove the retired
`news.triage.deadline_seconds` key from the operator config. The typed schema
rejects it; each route deadline is code-owned by the Program factory.

Before deploying #160, remove every retired policy-v9 action/priority key from
`news.policy`: `escalate_magnitude`, `min_push_magnitude`,
`min_watchlist_magnitude`, `unclear_push_min_magnitude`,
`unclear_push_event_types`, `high_priority_escalates`,
`noise_veto_max_magnitude`, `noise_veto_respects_gate_priority`, and
`contested_push_min_magnitude`. Run `uv run tracefold config` against
`~/.tracefold/config.yaml` and report only its redacted result. There is no
compatibility alias.

Issue #185 PR-C2 implements the reviewed OpenTrade write lifecycle but does not
make configuration equivalent to provider readiness. A configured
`live_reviewed` lane must use one venue, a non-empty owner-private regular
non-symlink token file (normally mode `0600`, at most 16 KiB),
one explicit `trading.live_symbol`, notional at most 10 USD, one open
underlying and one order per day. `trading status` shows
`execution_backend=opentrade_reviewed`, `live_mode_supported=true`,
`live_ready=false`, and `live_readiness=not_proven`: the CLI cannot infer the
Workers process's account/metadata/position-mode/inventory receipt. The database
accepts an approval only during the 60 s window from order creation. A valid
approval near the end of that window remains due for one 30 s reconcile cadence,
and the runner still requires a fresh no-drift preflight immediately before the
write; rejection there is expected fail-closed behavior and must record zero
provider attempts. An `APPROVED` row without the C2 approval marker has no
provable approval instant and rejects without a write after upgrade.
`live_bounded` is rejected at settings
validation and again at composition. If a partially filled tracked entry still
appears in provider open orders, reconciliation enters manual review and sends
no position close: the remaining entry could otherwise fill after the filled
slice was closed. Cancel or otherwise settle the entry at the venue, then let a
fresh provider read prove it terminal before resolving the manual row.
Provider redirects are ambiguous writes even when their body says rejection;
they never release the daily attempt charge or active-underlying slot.
A live ticker must identify the exact exchange and provider symbol selected by
metadata and carry a millisecond timestamp within the 10-second preflight
freshness window. Missing, cross-market, stale, seconds-unit or future ticker
facts disable that attempt before sizing or any provider write.
A provider rejection with explicit zero fill also remains ambiguous when the
same snapshot contains any current open order or correlated position history
that cannot be fully attributed to the tracked lifecycle.
Same-symbol opening trades from the provider's lookback are scoped to the
current order's frozen instrument/preflight time. Only a valid timestamp proven
inside the composite snapshot's seven-day millisecond bounds and earlier beyond
the tolerated 30-second provider clock skew is ignored. Missing timestamps stay
conflicting; malformed, seconds-unit, too-old or future values invalidate the
observation.
The same rule applies to unmatched reverse-side or reduce-only trades: a current
closing trade means the preceding position quantity may already be stale, so
reconciliation enters manual review and sends no close. Only closing history
proven earlier than the lifecycle cutoff is ignored.
When a lost entry response reaches manual review with no remote identity, use
`tracefold trading resolve <order-id> open --remote-order-id <provider-id>` only
after confirming that exact order and position at the venue. The command
persists the identity before protection/max-holding reconciliation resumes. A
known provider identity is immutable: supplying a different ID rejects the
resolution and leaves the order in manual review.

## Operator lifecycle

The canonical complete-product lifecycle is:

```bash
make up
make status
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

### Exact-image rollback with the current database schema

An image rollback is a runtime replacement, not an Alembic downgrade. The #160
physical `priority -> queue_priority` rename means the pre-0301 image cannot run
against the current schema. Before the first v6/v10 deployment, build, retain
and drill the reviewed new-schema/v5-behaviour rollback image from the clean
primary checkout:

The same release hard-cuts the News-to-Trading input contract. New projections
expose only post-epoch v10 judgments and new cases freeze the current
`trading_manifest_*` version. Any undecided case frozen under an earlier one is
blocked as `news_generation_retired` before model or order work; existing
prepared orders remain owned by reconciliation and are never rewritten by the
News migration. The manifest version is a constant in the deployed image, not a
schema fact: #213 moves it to `trading_manifest_v4`, so the *image* retires
every case frozen under v3 and rolling the image back retires everything frozen
since. Schema `0309` is a separate thing — it adds the two upstream stage stamps
and coalesces any undecided cases that share an underlying down to one (recorded
with the same `news_generation_retired` reason the runner would write, since the
version bump retires them all anyway) before creating the partial unique index
that keeps one undecided case per underlying from then on. A case that already
authored an order is never the one coalesced away. `0309` has no downgrade: the
coalescing cannot be told apart afterwards from any other block. Both statements
are no-ops on a deployment where Trading has been disabled. Schema `0310`
renames `case_kind` to `trigger_kind`, freezes strategy identity on every new
case, and creates the typed liquidation fact, exact strategy-registration and
shadow-evaluation ledgers. It
has no downgrade because those new material facts cannot be represented in the
old schema. A rollback must therefore be a reviewed image that understands
schema `0310`, never an Alembic downgrade.

```bash
cd ~/Documents/Code/tracefold
make build-news-rollback-image
```

The target verifies a rollback binary that understands schema 0301 while
reproducing v5 behaviour, then checks its source revision, exact behavior-
profile identity and image label. It does not deploy or mutate PostgreSQL.
Record the full `sha256:` image ID it prints; that ID, not its local tag, is the
only accepted rollback input. The artifact is deployment safety only: it is not
in the v7 factory registry, artifact loader or normal runtime image and cannot
be used for canary or daily execution. The production image contains exactly
factory v6 / executable v5; there is no runtime flag or dual loader. A failed build
or drill fails the release gate.

The #175 reader-history rollout uses the same-schema previous runtime image as
its rollback target; it never downgrades migrations `0302`/`0303`. During the first 24 h,
manually review every newly held `restatement` whose trace reports
`reader_history.targeted_count > 0`. `tracefold news why <event_id>` must name
the prior sent time, headline, and retrieval reason. If any such hold is really
a `progression` or `new_fact`, replace the runtime with the recorded previous
image digest immediately and retain the verdict/trace as regression evidence.
The #190 canonical-number hard cut follows the same one-image rule: rollback is
the recorded previous same-schema image, never the removed Program registry
root, and any attempted NaN/Infinity identity must remain a hard failure.
The #193 strategy-artifact cut follows it too, with one extra operator step:
migration `0304` trips every armed or active canary, because its candidate is a
two-file artifact the new image cannot load. Expect `canary status` to report
`tripped` with reason `program_strategy_artifact_v1_hard_cut` after the
upgrade; a candidate has to be recompiled against the new root and re-armed
from the start of the promotion ladder. Rolling back to the previous
same-schema image does not un-trip it, and `0304` has no downgrade.
Migration `0305` trips them a second time for the same reason on the compile
side: a candidate registered against the retired receipt chain names a
`compile_receipt` row the new image cannot validate, so it cannot be evaluated.
Expect reason `compile_record_v1_hard_cut` there, recompile, and re-arm. Old
`compile_receipt` rows stay readable as audit history; they are simply not
release evidence any more. `0305` has no downgrade either.

For the #160 drill and rollback, use the full image ID printed by
`build-news-rollback-image` directly. After both sides of a later deployment
already use schema 0301, the most recent recorded previous runtime image may
also be used. From the primary checkout on `main`, verify the chosen ID locally;
the SQL below is only the later same-schema lookup:

```bash
cd ~/Documents/Code/tracefold
docker compose exec -T postgres sh -eu -c '
  PGPASSWORD="$(cat /run/secrets/postgres_serve_password)"
  PGOPTIONS="-c default_transaction_read_only=on"
  export PGPASSWORD PGOPTIONS
  exec psql -X -v ON_ERROR_STOP=1 -U tracefold_serve -d tracefold -c "$1"
' sh \
  "SELECT payload->>'previous_image_digest' AS image_id
     FROM news_learning_artifacts
    WHERE kind = 'deployment_receipt'
      AND payload->>'action' = 'runtime_deploy'
      AND NULLIF(payload->>'previous_image_digest', '') IS NOT NULL
    ORDER BY created_at_ms DESC, artifact_sha DESC
    LIMIT 1"
docker image inspect --format '{{.Id}}' sha256:REPLACE_WITH_64_LOWERCASE_HEX
make deploy-image IMAGE_ID=sha256:REPLACE_WITH_64_LOWERCASE_HEX
```

Copy the literal `sha256:` ID into both commands; do not substitute a tag,
short ID, `name@sha256:` registry reference, or shell-discovered latest image.
The target does not pull. It requires a deployment-clean primary checkout at an
exact local `origin/main` commit; tracked/staged changes, `.env`, untracked or
ignored Compose overrides, and untracked or ignored Alembic revisions fail
closed, while unrelated untracked research files do not affect the rollback
boundary. Inherited `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`, `COMPOSE_ENV_FILES`,
and `COMPOSE_PROFILES` are refused; the target then pins Compose to the repository's
absolute `compose.yaml` and the `tracefold` project. `IMAGE_ID` must arrive on the
Make command line. The target re-inspects the local image to the
same full ID, runs `latest_migration_version()` inside both the current source
and target image, and reads the live database's `alembic_version` through the
read-only Serve role. All three heads must match before anything stops. It also
mounts the active operator config through the migration service and silently
runs the target image's redacted `tracefold config` parser. A missing image,
dirty/stale checkout, head mismatch, or config incompatibility is therefore a
no-op failure. `make up` and `make deploy-image` share one kernel-held deployment
lock from before initialization/mutation through the final status and success
output; a concurrent lifecycle command is refused, and process exit releases the
lock without stale-lock cleanup. Their `make -n` forms only print the plan.

After those checks it sets both Compose's app image and
`TRACEFOLD_IMAGE_DIGEST` to the inspected ID, stops Workers and Serve, and
recreates `migrate`, `serve`, and `workers` with `--no-build`. Compose waits for
the one-shot migration and both runtimes. Before reporting success, the target
requires all three recreated containers' Docker image IDs to equal the requested
ID, requires Workers `/readyz.image_digest` to equal it, and uses read-only SQL
to require that the latest `active_agent`, linked `runtime_deploy` receipt, and
runtime manifest all carry the same identity. It then runs the canonical
`make status` gate. PostgreSQL and RabbitMQ are not replaced.

The receipt's 24-hour rollback window is audit intent, not a promise that local
Docker garbage collection retained the image. If the ID is absent, recover the
trusted image into the local store through the operator's image-distribution
process first; never weaken the target to a mutable tag or bypass the schema
check. If recreation fails after the services were stopped, inspect
`make logs`, then either correct the exact-image problem or use normal
`make up` to rebuild and redeploy the current source.

## Health and status

| Surface | Meaning | SQL/queue inspection |
|---|---|---|
| `/healthz` | process liveness | none |
| Serve `/readyz` | DB liveness plus cached startup schema/composition | no queue inspection |
| Workers `/readyz` | root running, singleton session healthy, latest O(1) heartbeat persisted within 15 s, and (when News is enabled) the runtime manifest plus linked active/deployment receipt committed, plus the `runtime_revision` / `image_digest` this process can prove | no queue inspection |
| `/api/status` | `{measured_at_ms, runtime}`: database probe plus the Workers heartbeat row | bounded control read |
| `/api/news/status` | four-layer News state (`ingest`, `broker`, `pipeline`, `delivery`) plus `control` | bounded News reads |
| `make status` | PostgreSQL, migration, Serve, Workers, readiness, and console | fail-closed lifecycle check |
| `tracefold ops validate-projections` | bounded News singleton and delivery-state invariants | strict Serve-role read |

Source degradation does not make the HTTP process unready. `/api/status` has no
provider block; it fails closed only on a stale Workers heartbeat or a
database/schema mismatch. There is no durable worker queue left to inspect:
News backlog lives in RabbitMQ and is reported by `/api/news/status.broker`
and `tracefold news bus-check`.

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
  -> finite external-operation executor 3
  -> tasks: workers-probe; when News is enabled, one RabbitMQ robust connection
     and the News consumer tasks (news-receiver, news-recovery, news-deduper,
     news-triage, news-deliverer, news-janitor); the cold loops
     (news-instruments, and with venues enabled news-quotes, news-reactions);
     when Trading is enabled, trading-candidate and trading-reconcile;
     workers-control
```

The five cold loops (#75 instruments, #88 quotes and Event Reactions, #104's
two Trading runners) admit their database work through the one-slot
heavy-business lane, never the four News hot-path slots, so neither a price
backlog nor a Trading backlog can starve a live Event. Their
provider calls are bounded (quotes: one batch per source per turn — the #109 day
read replaces that turn's price read rather than adding a call — concurrency 4, a 10 s
turn deadline, 20 s cadence, never overlapping; reactions: at most 32 merged
candle requests per 60 s turn, concurrency 4) and none of them holds a database
connection while calling out.

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

Serve owns one pool of seven with ordinary/control admission `6/1`,
50 ms permit wait, 250 ms checkout, one-second statement timeout, JIT off,
parallel gather off, and 8 MiB work memory. Connections and ordinary requests
default to read-only; the two authenticated ReviewDesk POST routes explicitly
open a read-write transaction whose role grants permit INSERT only on the two
append-only review fact tables. Workers owns the exact pool/lane topology
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

News consumers use a dedicated four-slot News DB lane
(`WorkerDatabase.run_news`: its own executor and gate, separate from the two
business slots) for short idempotent transactions; each message is one
transaction of a few milliseconds. `consume()` handles up to `prefetch`
messages concurrently with a per-message ack, so `news.triage.concurrency`
(default 4) is real concurrency and the only News concurrency knob;
single-active queues use prefetch 1. When the News lane cannot admit a message
the consumer raises `DeferError` and the message requeues uncounted through
the retry lane.

News has no projection lease: the broker's single-active-consumer and
per-message ack are the fences.

`/metrics` exposes low-cardinality worker transaction and shared capability
resource signals. Use shared resource and PostgreSQL activity/lock evidence for
diagnosis; CPU alone is not a root-cause claim.

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
4. run `uv run tracefold news bus-check` for per-queue depths;
5. run `uv run tracefold news why <event_id>` for one Event's whole chain;
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

## Domain traces

News:

```text
OpenNews account Strategy WSS (whatever the account has enabled; no local allowlist)
  -> Receiver publishes each accepted frame to RabbitMQ (confirms)
  -> q:news.raw [SAC] Deduper: Item upsert -> title/identity -> Event new|member
     -> Gate -> storyline key -> publish event.<family>.<queue_priority> for admitted Events
  -> q:news.triage Triage (prefetch news.triage.concurrency):
     SemanticJudge.judge(TriageContext) -> EventSemantics.v2 + TradeRelevanceV1 -> SemanticNormalizer
     -> ReaderCard.v2
     -> deterministic assembler -> atomic SemanticJudgment/ScoredJudgment
     -> policy-v10 decide() -> news_verdicts (editorial + runtime manifest)
     -> verdict.push (an escalate rides the same key at AMQP priority 5)
  -> q:news.deliver [SAC] Deliverer: one Feishu attempt per Event (kind first)
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

Control: there is none. `news_control_state` and `tracefold news control` were
removed after the singleton never withheld a card: across the whole retained
history no verdict carried `override_rule = 'muted'` and no delivery settled as
`delivery_paused`, while both hot-path consumers read the row on every message.
To stop delivery, stop the Workers container; to stop a source, turn its
Strategy off in the OpenNews account (#126).

Model failure: Triage's sole Interface is
`SemanticJudge.judge(TriageContext) -> SemanticJudgment`. The production
Adapter runs the code-owned DSPy Program
`EventSemantics.v2 -> deterministic SemanticNormalizer -> ReaderCard.v2 ->
deterministic assembler`. The normalizer changes a stray non-negative
`restates` value on `new_fact`/`progression` to `-1`, records both values on the
EventSemantics trace, canonicalizes the nested `TradeRelevanceV1` sets, and
spends no provider call or fast retry. ReaderCard.v2
produces only `headline_zh` and `why_zh`; the assembler retains public
`title_zh=""` as a
compatibility sentinel. Both Predictor payloads exclude queue priority,
provider score, Gate macro lexicon, queue lag and watchlist; ReaderCard receives
only its reduced semantic view and never ToldContext or delivery intent. A successful primary route normally makes two serial
provider calls. One fast retry is
shared across both Predictors, so one route makes at most three calls; the
code-owned 20-second deadline covers the whole route. If primary
fails, a configured `llm.news_triage_fallback` restarts the full graph with its
own retry/deadline budget. Its ReaderCard slot explicitly aliases the same
endpoint unless a complete `llm.news_reader_card_fallback` endpoint is present;
one missing or invalid fallback slot disables fallback instead of mixing
routes. One Program execution's maximum remains six. DSPy
cache and hidden provider retries are disabled, so every billable attempt is
visible. There is still one persisted final semantic judgment and one card,
not a restored Analyst stage. Capacity planning must account for the normal
1 -> 2 call increase and serial latency. A stale-ledger re-ask is a second full
Program execution:
normally four calls total for that Event, with the same per-execution six-call
ceiling and all superseded/failed work included in telemetry.

By default both Predictors use the Triage endpoint, but each has its own
Adapter and code-owned token cap. A complete `llm.news_reader_card`
endpoint moves only ReaderCard's primary slot. A complete
`llm.news_reader_card_fallback` independently moves the ReaderCard fallback
slot; otherwise that slot is an explicit alias of the EventSemantics fallback
slot. `tracefold config` and `/api/news/status.pipeline` expose the effective
model names and dedicated-Reader flags without exposing endpoints or
credentials.

A fast-retryable timeout, rate limit, connection error, or non-truncated
schema/output failure can spend the route's one retry. A `max_tokens`
truncation (`news_program_output_truncated`, `finish_reason=length`) does not
retry. The code-owned primary-route breaker defaults to three retryable
transport failures and 60 seconds; while open it routes directly to fallback.
Separately, the consumer's configured circuit opens a
`triage_circuit_open` incident after the whole primary+fallback chain fails
retryably for `news.triage.circuit_failures` consecutive Events, and remains
open for `news.triage.circuit_open_seconds`. Output failures do not count
toward either transport circuit. When fallback answers,
`news_verdicts.model` names its resolved runtime model, the trace
carries `model_fallback_from`, and the worker logs one warning per Event; only
a chain where both routes fail degrades, with `primary_error` retaining the
first route's code.

The degraded path is never silent: only deterministic listing/telemetry and a
grounded watchlist hit fail open. Score, macro words and queue priority cannot
rescue failure; everything else drops as `degraded_no_objective_guard` with `degraded=true` and
is counted in `triage_degraded_24h`. Each Program call records Predictor,
route/attempt, resolved provider/model identity, request/input/instruction/demo/
output hashes, validated output and deterministic normalizations, finish reason,
latency, input/output/cached/total tokens and
provider cost in microusd when the provider reports it. `program_executions`
preserves initial and stale-ledger re-ask executions, including
failed/superseded work, while
`program_trace` always names the execution whose verdict was persisted;
the told-only failure restores the complete `first_judgment`, never a detached
verdict, and an evidence-changing failure cannot reuse it. Each persisted row
binds verdict/editorial hashes and the exact runtime manifest. Top-level usage
aggregates all executions. A rising Program output-error count
means inspect the failing Predictor and its artifact token cap, not edit an
operator deadline—the route deadline and token budgets are artifact state.

`/api/news/status.health` (and the console's status page) applies code-owned
thresholds from `tracefold.news.health`: ingest is `warn` after 10 min and
`bad` after 30 min without a frame, `bad` when disconnected or Workers are not
running; broker is `warn` at 50 and `bad` at 200 queued messages on a business
queue, `bad` when a business queue has no consumer, `warn` with dead letters;
model is `warn` at a 3 % and `bad` at a 10 % 24 h degraded share (the detail
names the error codes); delivery is `warn` when 10 % of 24 h attempts
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
   `open_incidents`. Which Strategies are feeding the pipeline is a question for
   the OpenNews dashboard, not for Tracefold.
2. `tracefold news bus-check`: consumers attached to every queue (Deduper and
   Deliverer show exactly one), `news.dead` depth, `news.retry` depth;
   `tracefold news dlq inspect` for the dead-letter bodies.
3. `pipeline`: `candidate_share_24h` (the Gate now admits nearly every Item;
   a share far below ~90% means the low-signal switch or a template flood),
   `suppressed_by_reason`, `dropped_by_rule`, `throttled_by_key`,
   `pushed_by_rule`, `reviewed_should_push_24h`,
   `reviewed_external_miss_24h`, `triage_24h` vs
   `triage_degraded_24h`, `triage_p95_ms`, `queue_lag_p95_ms`,
   `throttled_24h`. For one Event, `tracefold news why <event_id>` prints
   raw first line -> normalized title -> gate facts -> triage verdict ->
   decide rule / throttle key -> storyline status snapshots -> delivery.
   For strategy 1019, compare `telemetry_received_24h`,
   `telemetry_parsed_24h`, `telemetry_parse_failed_24h`, and
   `telemetry_push_24h`; `dropped_by_rule.oi_parse_failed` is a provider parser
   contract fault, not ordinary model noise.
4. `delivery`: `sent_1h`, `terminal_24h`, `last_error_code`
   (`delivery_unavailable` = push disabled or webhook invalid;
   `ambiguous_after_crash` = a send whose ack was lost). Historical rows can
   still contain the retired `delivery_paused` and
   `hourly_cap_reached` error, but policy v7 never writes it.
5. `tracefold news review queue --view coverage --hours 168` first checks
   whether there is enough same-version production evidence and accepted
   review coverage to make a quality claim. Work the deterministic strata with
   `review queue`, inspect the exact frozen input using `review evidence`, and
   append a rubric with `review submit`; a fact that never became an Event uses
   `review external-miss`. Do not infer precision/recall from unlabeled rows or
   infer causality from the market tab.
   Before and after a Prompt, RulePack or policy edit, run
   `news learning baseline` and name the mode you mean. A RulePack body is code
   under `factory_id`, not artifact state: editing one changes the prompt bytes
   every call is billed for while leaving `program_sha256` untouched, so the
   edit is finished only when the factory is bumped and the stable root
   reissued in the same change. `--mode recorded` costs
   nothing and answers "is the metric still wired the way it was"; it makes no
   provider call, so it cannot see a Prompt change. `--mode compile_live` is the
   graph GEPA optimizes and has no fallback, retry, deadline or breaker.
   `--mode runtime_live` is the configured production Program route and is the
   only mode whose failure rate resembles the reader's — it spends real provider
   calls on the same single-slot GPU that serves Triage, so both live modes
   require an explicit `--max-model-cases N`; expect roughly two physical calls
   per case, and up to six for one that fails. Read both
   `scores.case_macro_answered` and `scores.case_macro_failure_as_zero`: the
   first is quality given an answer, the second counts every unanswered case as
   zero, and the gap between them is the availability of the route rather than
   the quality of the cards. When comparing two runs, compare
   `prediction_dimensions`; `review_label_distribution` is corpus metadata over
   every requested case and does not move when the model does. `hard_gates`
   says which gate zeroed a case, and a `metric_error:*` in `failures.by_code`
   is a defect in the corpus or the ruler, not a provider outage. Compare
   metric-v4 components as well: 45% final action, 35% exact TradeRelevance,
   10% semantics/novelty and 10% ReaderCard, each with its effective denominator,
   weight mass and gold coverage. A failed dimension without exact gold is not
   scored. `metric_judge` unavailability is a receipted failure-as-zero for its
   free-text field, not byte-equality fallback.
   receipts by `report_sha256`, which excludes wall-clock latency so two runs
   with the same predictions have the same address. The command is read-only —
   one `serve` connection that closes before the first model call, and no write, delivery,
   proposal, acceptance or promotion authority of any kind.
6. A release receipt may only claim an identity the deployment can prove. The
   image cannot hash itself at build time, so `make up` reads the digest of the
   image it just built and passes it in as `TRACEFOLD_IMAGE_DIGEST`; an absent
   or empty value is recorded as `unversioned`, never as an empty string.
   Before starting an evidence run, confirm Workers `/readyz` reports a real
   `image_digest` and `runtime_revision` — an `unversioned` deployment still
   serves News correctly but cannot close a promotion.
7. A change is a registered candidate, not an edited production artifact.
   Freeze a post-epoch development dataset, check it with `learning readiness`
   (zero model calls), then run `learning optimize --development SHA --out DIR`
   with explicit metric, task, reflection and metric-judge call limits, a total
   and a per-call provider-cost limit, and a seed. It ends in `NO_OP`,
   `REJECTED` or `ADVANCE`; only `ADVANCE` writes `prompt_candidate.json`, and
   all three write a complete `optimization_report.json`. Each of the three
   roles is one `ModelExecutionIdentity`, and calls/cost/failures are accounted
   separately before they are summed. Then `release register --candidate
   prompt_candidate.json` binds it to the active stable and that frozen dataset
   — re-applying the patch to derive the Program identity and re-deriving the
   #199 Objective Plan rather than trusting the candidate — and `learning
   evaluate` runs the gate. A patch a person wrote registers on identical terms:
   the generator is audit, never permission.
   Production promotion additionally requires a
   future temporal validation dataset, blind pairwise review, a sealed 24 h shadow
   observation, and then `release canary arm`; inspect with `canary status`
   and use `canary trip` immediately on a schema/artifact/quality guardrail
   breach. Selector `news_canary_selector_v2` includes queue-high Events, excludes recovery/listing/
   telemetry, and trips on selector, eligibility-profile, rolling-profile or
   runtime-manifest drift. One Event belongs to one arm and runs one assigned Program (normally
   two serial Predictor calls, plus only the traced retry/fallback budget). A
   canary is not an excuse to skip the earlier evidence stages: the evaluator rejects a
   holdout/shadow/canary request before any Program call unless the preceding
   stage has a sealed PASS. Validation fixes at most 50 independent cluster
   tasks before execution; 100 unresolved human judgments, an empty required
   set, or a common provider outage is `UNKNOWN`, while a candidate-only
   critical error is `FAIL`.
   `dropped_by_rule.restatement` in `/api/news/status.pipeline` counts the
   duplicates the reader was spared; `pipeline.reasked_24h` counts Events whose
   full Program was executed again because a card landed while it was thinking
   (expect a handful per day; a surge means same-key floods);
   `pipeline.novelty_defaulted_24h`
   counts verdicts the model returned without the `novelty` field (accepted as
   `new_fact` after the retry — a rising count means the schema stopped
   landing and novelty is silently off).
8. `tracefold news replay <hits.json> [--gate-policy open|strict]`: reproduce
   Deduper+Gate on a saved provider payload without broker or model.

The current evidence eligibility window starts at the deployment timestamp
stored in `news_learning_epochs(program_v7)`. Only accepted
`news_review_v4` rows from this epoch enter metric v4, GEPA or release evidence. Every earlier
Prompt/Program baseline remains readable audit history but cannot enter a
dataset or release stage. Do not
interpret a successful migration, a valid Program artifact, or the new
two-Predictor trace as proof of higher quality; that claim begins only after
post-epoch accepted reviews and future holdout/shadow/canary evidence exist.
The immediate cost is the normal 1 -> 2 provider-call increase. The intended
future benefit is per-Predictor feedback, demonstrations, routing and
fine-tuning without widening the consumer's `SemanticJudge.judge()` Interface.

Retention: unjudged `news_items`/`news_events` older than 30 days are purged by
the Janitor; judged evidence is retained under the configured 365-day tier and
bands expire with their family window. The same turn uses the existing
one-slot cold/heavy DB admission for learning evidence: unreferenced model
recordings/cases become eligible after 90 days, report-referenced rows and
ordinary artifacts after 365 days, while current and previous distinct stable
release chains and an active canary remain pinned. Each table deletes at most
500 rows per turn; `/api/news/status.learning_retention` exposes the capped
remaining eligible count, last-turn deletes, oldest retained age and error.
Feed shows Events from the first frame after deployment; there is no backfill
of pre-V3 history.

### Price Review plane (#88)

`/api/news/status.price` is the first place to look:

- `sources[]` — one row per provider source with `age_ms` and `state`. A source
  whose state has been `stale` for minutes is either rate-limited or blocked;
  the loop's last error names which (`venue_rate_limited`, `venue_blocked`,
  `venue_timeout`). One failing venue never clears another and never blanks a
  price: the previous row stays and simply ages. A Binance source reads the wider
  `ticker/24hr` on one turn in fifteen (#109); a failure there fails that whole
  turn for that source, which is why `state` and `age_ms` remain the one thing to
  read — there is no separate freshness for the percentage.
- `reaction_partial_7d` / `reaction_complete_7d` / `reaction_unavailable_7d` —
  the Reaction backlog. A rising `partial` count with a flat `complete` count
  means the 4H leg is not landing; a rising `unavailable` count is a data
  question, not a health one, and `/api/news/review` names the reason.

Read-only SQL for the same questions:

```sql
-- how old is each source's quote map, and how many quotes does it hold
SELECT source_key, target_count, jsonb_object_keys_count, received_at_ms
  FROM (SELECT source_key, target_count, received_at_ms,
               (SELECT count(*) FROM jsonb_object_keys(quotes)) AS jsonb_object_keys_count
          FROM news_quote_snapshots) s
 ORDER BY source_key;

-- the oldest Event-asset still waiting for a horizon
SELECT min(a.opened_at_ms) AS oldest_due
  FROM news_event_assets a
  JOIN news_events e ON e.event_id = a.event_id AND e.ingest_mode = 'live'
  LEFT JOIN news_event_reactions r
    ON r.event_id = a.event_id AND r.symbol = a.symbol AND r.metric_version = 'reaction_v1'
 WHERE a.opened_at_ms <= (EXTRACT(EPOCH FROM now()) * 1000)::bigint - 3600000
   AND (r.state IS NULL OR r.state IN ('pending', 'partial'));

-- why Events could not be priced, by named reason
SELECT unavailable_reason, count(*) FROM news_event_reactions
 WHERE state = 'unavailable' GROUP BY 1 ORDER BY 2 DESC;
```

Nothing here is on the delivery path. If both venues are unavailable the price
plane reports degraded coverage and the News feed, status, Triage, Delivery,
readiness and shutdown are unaffected. There is no operator knob: cadence,
caps, concurrency, freshness limit, metric version, candle interval and gap
tolerance are code-owned; `news.venues.binance` / `news.venues.hyperliquid` /
`news.venues.enabled` are the only switches, shared with the instrument
snapshot.

## Migrations

The Alembic chain is the `20260818_0275` baseline (root; it executes
`current_schema_20260818_0275.sql` and then `runtime_roles.sql`, which
creates the `tracefold_owner`, `tracefold_serve`, `tracefold_workers`, and
`tracefold_migrate` roles when run by the bootstrap superuser, verifies the
role contract, and applies the Serve read / Workers write grants) followed by
the linear revisions through `20260823_0299_news_source_artifact_id`. The #112 chain
adds ReviewDesk tables and grants the existing Serve role only their
append-only INSERT capability. It adds no login role or password. A live
database stamped at an earlier revision upgrades with `tracefold db migrate`;
a fresh database runs the same complete chain. Every revision is irreversible;
a downgrade is a backup restore. Stop Serve and Workers before applying a
chained revision
(each takes the maintenance gate advisory lock and refuses to run while
Workers hold the steady lock).

An existing volume at 0283 needs no new password or offline role bootstrap.
Before its first 0284–0295 upgrade, take a restorable volume backup, stop Serve
and Workers, run the normal migration, then deploy the matching image. The
migration owns the narrow ReviewDesk grants; the existing Serve credential is
unchanged.

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
`collector_pending_items`.

0278 drops the whole Macro lane in child-before-parent order:
`macro_document_analysis_jobs`, `macro_document_analyses`, `macro_documents`,
`macro_fed_official_role_facts`, `macro_release_facts`, `macro_series_facts`,
`macro_module_current`, `macro_module_frontiers`,
`macro_dataset_projection_states`, `macro_acquisition_targets`,
`market_position_facts`, `market_settlements`, `market_observations`,
`market_instruments`, and `queue_terminal_events` (whose only writers were the
Macro repository and the projection frontier); it also drops the
`reject_macro_fact_mutation()` function. No revision performs a provider,
broker, or outbound call.

0279–0283 add listing admission, the instrument universe, legacy label-v1 and
Price Review. 0284 freezes fact/evidence versions. 0285 verifies legacy-label
migration, hard-deletes `news_event_labels`, creates append-only ReviewDesk
evidence and the security-barrier task view, and grants Serve only INSERT on
the two review fact tables in addition to its read access. 0286 adds content-addressed datasets,
candidate/evaluation/deployment artifacts, pairwise cases and exact model
recordings. 0287 adds durable canary activations, one assignment per Event and
runtime manifests. 0288 adds the bounded retention function, cold-Janitor
state, and indexes used by its ordered batches. 0289 reasserts the exact
Workers `SELECT`/`INSERT` evidence-snapshot grant and revokes rewrite access;
`db audit` now verifies that role contract so a missing runtime grant fails the
rollout check before a live Event discovers it. 0290 removes an ineffective
`FOR SHARE` from the append read: PostgreSQL otherwise requires UPDATE for the
locking SELECT even though the immutable table rejects UPDATE. Migration never
calls the model or derives a release PASS. 0291 removes the local OpenNews
Strategy allowlist. 0292 adds Program version/SHA to verdicts, Predictor/call/
attempt/route usage and cost fields to model recordings, and the append-only
`news_learning_epochs` row whose database deployment timestamp starts
`program_v1`; its explicit disposition makes all Prompt-era learning evidence
audit-only and promotion-ineligible. `0293` preserves that row and appends
`program_v2` after correcting the semantic fast-retry state machine and
hardening the restatement sentinel, making `program_v1` evidence audit-only as
well. `0294` preserves both prior Program epochs and appends `program_v3` for
the expert quality baseline and semantic normalization, making `program_v2`
evidence audit-only for its release decisions. `0295` preserves v1-v3 and
appends `program_v4` for the D-generation ownership hard cut; `0298` preserves
v1-v4 and appends `program_v5` for candidate-conditioned ToldContext, making
every earlier cohort audit-only for current release decisions. `0301`
hard-renames persisted `priority` to `queue_priority`, adds
atomic editorial/scored/runtime-manifest judgment identity, trips old canaries,
and starts `program_v6` for factory/executable v4 and policy v10. None of these migrations
deletes history or claims a release PASS.
`0303` preserves that history and appends `program_v7` for factory/executable
v5 after the #162 Program/Learning package split; v6 evidence remains audit-only.
`0304` is the #193 strategy-artifact hard cut: it adds no column, trips every
armed or active canary whose candidate the new image cannot load, and appends
one migration receipt to `news_learning_artifacts`. It leaves `program_v7`
open on purpose — the artifact serialization and the Program root changed, the
evidence did not — so accepted `news_review_v4` rows stay eligible and the
epoch row goes on naming what the epoch was opened with. It is irreversible.
`0305` is the #193 compile-record hard cut: it adds `compile_record` to the
learning-artifact kind constraint while keeping `compile_receipt` in it, and
trips every armed or active canary whose candidate was registered against the
retired receipt chain. It leaves `program_v7` open for the same reason and is
irreversible as well.

Before applying 0278 remove `providers.macro_sources` and the
`llm.macro_document_analysis_*` keys from `~/.tracefold/config.yaml`; the
settings schema rejects them and Serve/Workers fail to start with them
present. Verify after restart: `tracefold db audit` reports
`migration_status` `ready`, current News table counts, `news_schema.exact`, and
`runtime_roles.ok`; `tracefold news bus-check` shows one consumer on
`news.raw` and `news.deliver`; `/api/news/status.state` becomes `ready` once
the WSS connects; `/api/macro/overview` answers `404`; and the first candidate
Event receives a Triage verdict within seconds.

## Operator actions and retention

There is no durable worker queue and therefore no terminal-evidence retry,
archive, or quarantine action. Failed News messages retry through the broker's
30 s lane three times and then dead-letter to `news.dead`, which
`tracefold news dlq inspect|replay|purge` owns. Current models retain one
stable row per identity.

News retention has two tiers (`news.retention`, issue #81). The Janitor deletes
`news_items` older than `raw_days` (30), which cascades to Event-owned verdict,
delivery, member, asset, band and evidence snapshots. An Item behind an Event
that carries a verdict or an accepted ReviewDesk judgment is evaluation
evidence and survives to `judged_days` (365). Accepted reviews, external-miss
snapshots, sealed datasets/evaluations/model recordings, canary assignments
and deployment/rollback receipts are append-only audit evidence; a retention
change must preserve every foreign-key dependency and the ability to replay a
sealed dataset. Narrowing the evidence window silently destroys the only
ground truth the system has.

`news_market_liquidations` is a separate immutable normalized replay ledger,
not an Item-owned child. Item retention may remove its provider envelope but
must leave the typed liquidation fact, source identity and frozen live/recovery
ingest provenance intact.

Learning evidence follows #118's separate deterministic policy:

- an unreferenced `news_model_recordings` or `news_learning_cases` row is kept
  for at least 90 days;
- a run named by an evaluation report/release receipt and every ordinary
  learning artifact is kept for at least 365 days;
- the newest manifest for each of the current and previous distinct stable
  bundles, plus an armed/active canary, pins its candidate, datasets, reports,
  observations, per-case rows and exact model recordings regardless of age;
- `news_learning_epochs` is append-only permanent audit truth. The current
  `program_v7` reset changes eligibility, not retention: all earlier evidence
  remains auditable until the existing deterministic
  retention policy makes an otherwise-unpinned row eligible;
- `active_agent`, deployment and rollback receipts are permanent audit truth;
- every purge call deletes at most 500 recordings, 500 cases and 500 artifacts.
  Eligible counters are capped at 501: `501` means “at least one more full
  batch”, not an exact global count. A purge error is recorded but does not
  change News readiness or stop ingest/delivery.

Capacity assumption for V1: request and response JSON are each capped at
64 KiB, so a worst-case 200-case, two-arm, three-trial run is under roughly
150 MiB of payload before PostgreSQL/index overhead; typical one-trial runs
are much smaller. Operators should alert on a non-zero eligible count that
does not fall across Janitor turns, any `last_error_code`, or persistent table
growth outside the 90/365-day envelope. The purge shares the one-slot cold
admission with Price Review, never the four-slot News hot lane.

Restore/audit procedure:

1. Restore the PostgreSQL backup into an isolated database and migrate only to
   the image's recorded schema head; never set
   `tracefold.learning_retention_purge` or issue manual DELETEs.
2. Run `uv run tracefold db audit`; confirm migration head, exact News table
   set and role grants. Read `news_learning_retention_state` and retain its
   pre-restore snapshot for comparison.
3. Select the latest manifest for each distinct `stable_bundle_sha`; for the
   newest two bundles, verify candidate → release evidence → report → dataset,
   `news_learning_cases` and `news_model_recordings` references are present.
   Recompute content hashes through CandidateEvaluator/record-replay; do not
   accept row counts alone as proof.
4. Verify every deployment/rollback receipt from the backup still exists and
   compare counts plus oldest ages before allowing Workers to start. If any
   pinned link is missing, keep the restored system offline and restore an
   earlier backup; the irreversible migration's rollback is backup restore.

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
`/api/news/*`; `/healthz`, `/metrics`, and `/api/bootstrap`
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
idle worker must not scan broad facts merely to prove that no work is
due. Use one representative `EXPLAIN (ANALYZE, BUFFERS)` for a
bounded evidence path; do not create a second planner-assertion control
plane. Current models remain bounded by stable product keys; a
latest-generation pointer is not a retention policy.

Worker sessions disable PostgreSQL parallel gather and JIT and use 16 MB
`work_mem`; API sessions keep PostgreSQL defaults. This prevents bounded
background iterations from multiplying into all available PostgreSQL CPU
workers. The News feed hot paths use ordered composite indexes; do not replace
them with periodic broad scans.

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
6. start one writer per current model, then verify readiness, broker queue
   movement, and unchanged-payload zero-write behavior;
7. retain the backup until the new runtime passes smoke checks.
