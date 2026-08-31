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

### Trading no-key Decision/Capital operation (#350)

Execution credentials are optional for deployment. The supported observer state is:

```text
Decision RUNNING
Capital PAUSED
BINANCE_USDM.credentials unconfigured
HYPERLIQUID_PERP.credentials unconfigured
```

Run `uv run tracefold config` to inspect only redacted per-binding credential
states and resolved paths. Missing files are `unconfigured`; partial,
malformed, insecure, or unreadable inputs are `invalid`. Never print or copy a
credential. `make up`, migration, Serve, Workers, Web, and the public venue
catalog must remain green without either binding credential.

`trading.enabled` controls only the Decision Plane. Workers always project both
closed bindings and refresh the credential-free Binance USD-M and Hyperliquid
perp catalogs. Verify with:

```text
make up
make status
uv run tracefold trading status
```

The status must show Decision lifecycle/heartbeat, Capital control, and both
bindings' redacted credential/runtime/account/catalog/heartbeat/reason facts.
A provider catalog failure marks only that binding stale/error and preserves
last-known-good. Serve reads these PostgreSQL facts and never secret files or a
provider client.

Every Workers start reprojects credentials and forces Capital `PAUSED`. Do not
set `RUNNING` to test activation: before #360, even configured/ready facts end
in `promotion_authority_unavailable` and zero new Intent. A pure-policy LONG
must remain LONG with independent `capital_disposition=blocked`; NO_TRADE has
Capital `not_applicable`.

If Decision is enabled and schema, wiring, policy, or News-generation
composition is invalid, Workers must fail startup/readiness or record Decision
`FAULTED`. Do not classify an arbitrary exception as legal no-key observer
mode.

#### Production V3 evidence clock (#377)

Keep Capital `PAUSED` while building discovery and future-holdout evidence.
Use one content-addressed artifact root and record every returned full digest
and receipt; never edit an artifact in place or reuse a future window after a
protocol change. The operator sequence is:

```text
trading evidence capture --partition discovery ...
trading evidence drain --capture ... --max-horizon-ms ... --finalization-lag-ms ... --cost-model ...
trading evidence corpus-seal --capture ... --drain ...
trading evidence candidate-register --file ...
trading evidence capture --partition future ... --candidate ... --candidate-receipt ...  # repeat each locked interval
trading evidence drain --capture ... --candidate ... --candidate-receipt ...
trading evidence future-unblind --capture ... --drain ... --candidate ... --candidate-receipt ...
```

Capture and drain are not one command: the first cannot see outcome bars or
funding. Future capture is periodic, not one post-window query. Run it at every
candidate-locked `capture_interval_ms`. Each call appends only the next contiguous
batch and reports blind collection health: collector/Workers generation, expected and
missing source mass, `capture_lag_ms`, late/catalog counts, bar/funding continuity, and
artifact integrity. PostgreSQL rejects a gap, overlap, wrong binding, late call
beyond `maximum_capture_lag_ms`, or changed candidate. The last batch seals the one
`FUTURE_CAPTURE_SEALED` only after PostgreSQL recomputes the full interval chain,
health digest, and canonical incident set; later source-population variants cannot replace it. A missed
deadline fails closed and requires a new preregistered future protocol, not a backfill.
The second refuses all future provider I/O before the locked drain cutoff or under a
different candidate receipt/cost/horizon. The first successful future drain
transaction appends `FUTURE_DRAIN_SEALED`; a second drain cannot replace it, and
unblind accepts only its exact capture and drain artifacts. During the blind period inspect collection
health only: batch continuity, schedule lag, raw source counts, late availability,
catalog coverage, and provider availability. Do not calculate candidate PnL, cohort profitability, hurdle
distance, or extend/stop the window based on results. Provider outage,
source-wide missingness, catalog reset/delist, correction, missing bars or
funding, protection drift, and clock violations use the locked incident map;
they are never adjudicated after unblind.

All public evidence transitions use PostgreSQL `clock_timestamp`; the caller's wall
clock is not preregistration evidence. Candidate registration re-runs the code-owned
finite selector over the sealed corpus and refuses a hand-authored terminal or eligible
population. The News handoff fields are the source Item/venue/availability/learning
epoch frozen when the OI ledger row was inserted, not reconstructed from current Event
or deployment state.

Before any human grant review, run `trading evidence verify --receipt SHA` on
the exact `PROMOTE` result. Promotion then follows #376's separate immutable
risk-policy -> grant -> arm path and still requires the explicit bounded
canary approval. A canary passes only after the durable Intent reaches
authoritative `CLOSED_FLAT` and risk settlement; profit is irrelevant and
`MANUAL_REVIEW` is not a pass. Keep the exact canary Intent ids. For the
required restart drill, stop the Nautilus generation only after the Intent is
filled and natively protected, start the same frozen release again, let it
query-first reconcile and close, and bind both immutable runtime ids plus the
final `flat_verified_at_ms` in the restart receipt. A service restart without
those two PostgreSQL start facts is not restart evidence.

Freeze the release candidate before final observation. Its artifact names the
exact tag/commit/tree, OCI image, migration head, committed OpenAPI, built web
tree, Workers/Serve revisions, Nautilus wheel/source, execution contracts,
per-binding catalog/capability/account identities, evidence/grant/risk
receipts, and one exact seven-day window with nonzero minimum activity. With the exact
approved Workers and Serve processes already running, execute
`trading evidence release-register --file FILE` before the window starts. The database
records its own registration time and binds both current runtime ids, start times,
revisions, and image digests; restarting either process invalidates the window. Run
`trading evidence verify --release FILE` only after its drain cutoff. The tag
must be an annotated signature-verifiable Git tag resolving to the declared
commit and tree; the release file must enumerate the exact canary Intent set
and one protected-to-recovered Nautilus restart receipt. A green
CI run or a mathematically conserved empty window is not acceptance. At window
start, the preregistered Workers and Serve generations must already report the
declared commit and image; the Workers durable heartbeat and a final authenticated
Serve observation must cover the end, and every Admission plus Intent authority chain in
the window must name that same release. Deploying another release invalidates
the window. At
rollback, pause Capital, revoke/expire every grant, reconcile every enabled
venue flat, drain active risk/Intent obligations, then run
`trading evidence verify --rollback FILE`. Keep the observer/Decision path
running past the rollback receipt; do not revive a terminal Intent, submit a
replacement entry, or infer a flat account from local
state. The rollback receipt names and re-hashes the exact release-candidate
artifact, covers its exact binding/grant scope, and cannot predate that release
window's drain cutoff. The v2 rollback receipt also names new Workers and Serve
runtime ids; both must differ from the registered release generations and must
start at or after rollback. Every enabled binding must then report `ready`,
`reconciled_flat`, and a query-first reconciliation heartbeat no earlier than
both rollback and the new Workers start before zero-refill and
observer-continuity checks can pass.

These commands implement the clock and verification mechanism. Until the
calendar window, human approval, venue receipts, fixed seven-day accounting,
and rollback receipts actually exist, #377 remains open and no production
terminal may be claimed.

On a fresh database, `tracefold init` and `make up` create the Nautilus
password and role with the other runtime roles. Restores land in a fresh
PostgreSQL cluster carrying that same current role contract.

#### Retired pre-#350 execution cutover record — do not execute

The remaining PR 1/PR 2 Demo activation notes in this subsection describe the
immutable pre-#350 Intent/replay era. The current CLI rejects
`trading refresh-capabilities`, deployment does not start Nautilus, and these
steps cannot authorize a new Intent. They remain only as recovery context for
existing durable rows.

The one-time PR 2 cutover from the PR 1 dark slice is:

1. Set Trading control to `PAUSED`. Do not stop Nautilus; it must retain
   authority over any already-fenced lifecycle.
2. Update `~/.tracefold/config.yaml` to the current contract before running the
   new CLI or image: remove the retired `mode`, `live_symbol`, `account_ref`,
   `venues`, `opentrade` and `nautilus.accept_intents` keys, and — since #331 —
   `trading.regime.*`, `trading.policy.*`, `trading.candidates.news_lookback_seconds`,
   `trading.candidates.oi_lookback_seconds`, `trading.candidates.max_dspy_cases_per_day`
   and `llm.trading_decision_model`. Those six are gone because a capital
   threshold in a YAML file is a rule with no version and no frozen evidence;
   the policy owns its own numbers and freezes them onto every Case. Preserve
   `trading.enabled`, the two `trading.candidates.*` filters (`max_age_seconds`,
   `min_oi_value_usd`; `symbol_cooldown_seconds` and `max_rank_in_window` were
   retired by #348),
   `trading.order.fixed_notional_usd` (`0 < value <= 10`) and the two Nautilus
   Demo secret-file paths. Run `uv run tracefold config`; every model is
   `extra="forbid"`, so a retired key left in place fails Serve and Workers at
   settings load rather than being ignored.
3. Confirm the current Nautilus `/readyz` is green. Its startup reconciliation
   must have proved the exact Demo account/instrument configuration,
   authoritative venue flat, and no unexpected exposure.
4. Run `make trading-hard-cut-preflight` from the clean primary checkout. It
   fails unless control is `PAUSED`, exactly one Nautilus Compose replica
   exists, readiness proves venue flat, legacy `PENDING/RUNNING` Cases are
   zero, nonterminal Intents are zero, and legacy active/unknown Orders are
   zero.
5. Deploy the exact reviewed image at the current Alembic head (`20260831_0339`
   at this release). Both
   `make up` and `make db-migrate` detect the PR 1 head and automatically repeat
   the full preflight before migration or service shutdown; migration `0317`
   repeats the three database drain predicates in its transaction before
   revoking the legacy writer. The later `0327` cut requires PAUSED and no
   undecided Case but deliberately preserves a nonterminal Intent as a recovery
   obligation, projected as `recovery_blocked_credentials_missing` when its
   binding has no credential. The `0328` → `0329` automatic preflight is
   deliberately database-only: it requires PAUSED and zero nonterminal Intents,
   so a no-key deployment can drain that recovery obligation without inventing
   a Nautilus readiness requirement. `0329` repeats those two predicates before
   installing Intent-level Q1/fence/Q2 evidence. `0330` then hard-cuts the
   current News contract; upgrading that head to Trading Production V3 `0331`
   repeats the full venue-flat/Nautilus preflight. Upgrading `0331` to Trading
   capital authority `0332` also repeats that preflight. `0333` is the
   subsequent additive News Verdict-handoff partial index and has no capital
   cutover predicate. `0334` adds the Trading evidence clock and requires Capital
   `PAUSED` with no pre-existing unbound promotion grant, but does not require a
   recovery-only Nautilus process. `0335` adds the News open-incident uniqueness
   index; its preflight fails if two incidents of one cause class are already
   open, which an operator resolves by closing the stale duplicate rather than by
   deleting evidence. `0336` is the separate News genesis described in the
   current migrations runbook below. Older execution-authority cutover routes
   retain the full
   venue-flat/Nautilus preflight above.
6. Run `make status`, then `uv run tracefold trading status`. Require one
   healthy Nautilus replica, `execution_authority=nautilus`,
   each configured binding's exact account generation, catalog, capability and
   execution binding; current
   heartbeat, `engine_ready=true`, and `unexpected_exposure=false`.
7. Set control to `RUNNING`. The capital lane can now atomically write a fresh
   Intent; there is no `accept_intents` flag or per-order approval.

For the first V2 capability activation, keep Trading `PAUSED` and require zero
nonterminal Intents. When no active snapshot exists, `make up` starts a bounded
zero-claim Nautilus process, waits for its fresh account-wide
`nautilus_bootstrap_account_zero_at_ms` proof while `/readyz` correctly remains
red, activates the first snapshot, stops that process, and recreates the normal
capability-governed Nautilus service. The proof remains valid for the bounded
provider load (at most five minutes) and activation clears it.

Once an active snapshot exists, `make up` and `make deploy-image` reuse it and
only recreate normal Nautilus. They do not refresh capability or write Trading
control; stale-but-valid is an accepted deployment state. A later replacement
is an explicit paused-window operation:

```text
uv run tracefold trading control paused
uv run tracefold trading refresh-capabilities
make up
uv run tracefold trading status
uv run tracefold trading control running
```

Before refresh, require zero nonterminal Intents and current Nautilus readiness
with no unexpected exposure; leave Nautilus running so that proof stays fresh.
The refresh command fails before provider I/O unless control is already
`PAUSED`, and it never pauses or resumes Trading itself. Record the new snapshot
digest/count and blacklist revision, and restore `RUNNING` only after recreated
Nautilus is ready and has revalidated every included instrument. A failed load
or activation leaves the old active snapshot unchanged and the operator-chosen
`PAUSED` control intact. If the old snapshot cannot load, keep Trading `PAUSED`
and use the bounded `tracefold nautilus run --bootstrap-zero-claims` recovery
process before retrying refresh; that process refuses active Intents, never
claims readiness, and never consumes an Intent.

Do not seed a production Case or Intent to make the console non-empty. A normal
source must produce the Case, and only a frozen long/non-shadow Case admitted by
the active snapshot and current blacklist may emit an Intent.

Current `make up` and `make deploy-image` idempotently create empty 0600
credential placeholders, do not require execution credentials, and do not start an execution adapter. `make status` reports adapters as
not required while Capital is PAUSED. The browser and HTTP surface expose
durable Decision/Capital/binding facts and independent Case policy/capital
attribution; existing Intent/Outcome history remains read-only.

If an Intent reaches `MANUAL_REVIEW`, an entry outcome is unknown, protection
cannot be proved, or flat cannot be proved:

- immediately set `PAUSED` or `CLOSE_ONLY` to block new entry fences;
- leave Nautilus running so query/protection/full-close reconciliation
  continues;
- do not start a legacy writer, submit a manual replacement through Tracefold,
  or mark the row closed from local inference;
- treat fresh targeted venue proof of zero position plus terminal/canceled
  owned legs as the only `CLOSED_FLAT` proof.

Rollback is permitted only while the venue is authoritatively flat and only to
an image compatible with the live schema. A non-flat incident must roll
forward: Nautilus remains the sole authority until exposure is protected or
closed.

The deterministic PostgreSQL acceptance lane is `make trading-smoke`; it
proves atomic Case/Intent handoff, fences, restart/outcome projection, and
role constraints, not venue behavior. The opt-in real Demo closure/restart
drill is `tests/live/test_nautilus_binance_demo.py`; use its isolated
database/config/container contract and exact committed image. Its terminal is
`DEMO_CLOSED_FLAT`, otherwise the run must preserve the recoverable state and
report failure or `MANUAL_REVIEW`.

## Operator lifecycle

The canonical complete-product lifecycle is:

```bash
make up
make status
make logs
make down
```

`make up` preflights Git, `uv`, Docker, Compose, `curl`, an authenticated GitHub
CLI, and daemon access, runs
idempotent initialization, builds one shared Python/React image, starts
PostgreSQL when absent, requires the one-shot migration to succeed, starts
Serve and Workers, and then runs the same fail-closed status gate. Execution
credentials do not participate in deployment readiness, and no Nautilus
process is recreated. It
does not recreate a running PostgreSQL container. On failure, use `make logs`. Operator config, five
password files, and named-volume data remain in place. `make down` stops
containers without deleting that volume.

Fresh PostgreSQL role bootstrap belongs only to the image's `initdb` phase. It
creates the ordinary direct-login migration owner plus the separate Serve,
Workers, and Nautilus roles from their mode-`0600` password files. The owner
reuses `postgres_migrate_password`; no second migrator role exists. Bootstrap
then revokes its own login and permits owner-direct migration. It is not a
periodic reconciler and will not mutate an unknown non-empty cluster. Such a
cluster must already satisfy the role/schema contract; startup never repairs an
unknown role/schema boundary.

`make status` prints Compose state and returns non-zero unless PostgreSQL,
migration, Serve, Workers, the Serve and Workers readiness endpoints, and the
HTML console all pass. Execution-adapter absence is expected at #350 and does
not weaken those required boundaries.
It must not be replaced by a liveness-only `curl` or a
Compose command whose exit status ignores an unhealthy Worker.

### Exact-image replacement with the current database schema

An image replacement is a runtime change, never an Alembic downgrade. Use only a
reviewed local image identified by its full `sha256:` ID. The current source, the
target image and the live database must report the same migration head.

From the deployment-clean primary checkout on `main`, resolve the latest
recorded previous image candidate, then inspect and deploy that exact ID:

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

Both `make up` and `make deploy-image` acquire the repository deployment lock,
then run `verify-main-ci` while that lock is held and before any deployment
mutation. The private implementation targets verify the inherited lock file
descriptor, so setting an environment flag or invoking them directly cannot
bypass either control. `make db-migrate` takes the same lock and runs the same
verifier (#373) because it applies working-tree Alembic revisions to the
production database without starting the application image.

Which entries are covered is no longer a list someone maintains:
`tests/deploy/test_main_ci_gate.py` derives the set from the Makefile by what each
recipe does. It classifies a recipe that runs `docker compose up`, `build` or
`run`, that applies Alembic revisions, or that runs the image directly, and it
asserts the derived set still contains the known entries so a derivation that
stopped matching cannot pass by finding nothing. The three read-only preflights
and the observe-or-stop targets (`down`, `status`, `logs`, the `*-shell` pair) are
not classified, because they change nothing.
The gate requires the primary checkout on `main`, a
clean source tree, `HEAD` equal to both the local and live remote `origin/main`,
and that exact SHA's latest `ci-gate` check to be completed and successful
under GitHub Actions integration id `15368`. It also refuses inherited Compose
topology variables and pins Compose to this checkout's `compose.yaml` and the
`tracefold` project. A pull-request result cannot authorize a squash commit,
an old green local ref cannot authorize deployment, an untrusted check with the
same name cannot authorize deployment, and missing GitHub status fails closed.

This verifies a real deployment boundary; it does not claim current merge
protection. As verified for #353 on 2026-08-30, the private repository's GitHub
Free organization cannot configure branch protection or Rulesets through the
available APIs. Until that platform constraint changes, the fixed CI workflow
is observable exact-SHA verification and `ci-gate` is deployment authorization,
not a GitHub-enforced pre-merge rule. A future platform change should require
this one stable check name rather than introduce another project-owned planner.

The target accepts no tag, short ID or registry reference. It never builds or pulls,
and it checks the checkout, Compose inputs, active config, three migration heads,
deployment lock, recreated container IDs, Workers readiness and durable deployment
receipt before reporting success. A recorded previous image digest is only a
candidate: local retention and schema compatibility are still required.

A schema-changing release cannot roll back to an older-schema image. Its Issue must
approve a current-schema recovery or roll-forward plan before migration. Historical
implementations remain in Git history and never become compatibility code in the
runtime.

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

## RabbitMQ durable-event plane (#400)

RabbitMQ 4.3 owns News retry. There is no application retry lane, scheduler or
attempt counter, and there is no `news.retry` queue or exchange. What the broker
does is configured by one policy per queue, generated from
`tracefold.news.broker_policy` into `docker/rabbitmq/definitions.json` and
imported by the one-shot `rabbitmq-policy` Compose service (`tracefold news
bus-policy apply`) before Workers starts. Provisioning proves the policy
documents it just imported — a policy is a name-pattern rule that exists before
any queue matches it, so this holds on a fresh broker volume with no topology
at all. The per-queue effective policy is Workers' and `news bus-check`'s
question: Workers verifies it at startup (waiting out the management statistics
interval that publishes a freshly declared queue's effective policy) and
refuses to consume on a mismatch.

| Setting | Value | Why this value |
|---|---|---|
| `delayed-retry-type` | `all` | Delays counted returns (`TransientError`) and uncounted ones (`DeferError`) alike, so a defer waits exactly as long as it did through the old TTL lane. |
| `delayed-retry-min` / `-max` | `30000` / `30000` | Frozen from the removed lane's TTL. A flat delay, not a backoff: changing it needs its own production evidence. |
| `delivery-limit` | `2` | Measured on 4.3.5: a quorum queue delivers `delivery-limit + 1` times, because the first delivery carries no `x-delivery-count`. Two keeps the frozen three total handler attempts. |
| `dead-letter-strategy` | `at-least-once` | A `news.dead` that is unavailable or full must hold the message on its source queue, not drop it. |
| `overflow` | `reject-publish` | At the bound the newest publish is rejected as `BrokerBackpressure`; the oldest message is never dropped. |
| `dead-letter-exchange` | `news.dlx` | Terminal deliveries only: decode failure, `PermanentError`, spent delivery limit. |
| `max-length-bytes` | see below | Bounded so the queue rejects before the node-wide memory alarm blocks every publisher. |

### How the byte bounds were measured

`max-length-bytes(q) = p99 envelope bytes x peak messages per minute x 10`,
rounded up to a power-of-two MiB and floored at 4 MiB. `news.dead` is terminal
evidence rather than arrival-driven, so it is sized as 8,192 dead letters
instead. Envelope sizes are the broker's own `message_bytes` (body plus AMQP
properties and headers); rates are the worst single minute in a seven-day
window.

| Queue | p99 envelope | Peak/min | Bound | Backlog that buys |
|---|---:|---:|---:|---|
| `news.raw` | 2,048 B | 2,882 | 64 MiB | ~11 min of the worst minute ever observed (a Recovery backfill, itself capped at 1,000 messages per 30 s run), or ~42 h at the p99 minute of 13/min |
| `news.triage` | 512 B | 111 | 4 MiB | ~8,192 Events: ~73 min at the worst minute, ~12 h at the p99 minute |
| `news.deliver` | 512 B | 7 | 4 MiB | ~8,192 push Verdicts |
| `news.dead` | 2,048 B | n/a | 16 MiB | ~8,192 dead letters an operator can still page through |

The four bounds total 88 MiB against a 768 MiB broker container whose default
`vm_memory_high_watermark` blocks publishers near 460 MiB. That ordering is the
point: a queue bound rejects one queue's publishes as a typed
`BrokerBackpressure`, which opens an incident and later replays through
Recovery, while the memory alarm blocks every publisher on the node with no
typed signal at all. Re-measure with `SELECT` over `news_items` /
`news_events` / `news_verdicts` per-minute counts and the management API's
`message_bytes / messages`, then edit `tracefold/news/broker_policy.py` and run
`uv run python scripts/regen_rabbitmq_definitions.py`.

### Signals

`/api/news/status.broker.queues` and `tracefold news bus-check` carry, per
queue: `messages`, `ready`, `unacked`, `delayed` (inside a native retry window),
`dead_letter_pending` (at-least-once dead letters the source queue is holding
because `news.dead` would not take them), `message_bytes` / `bytes_used_bps`
against the bound, `consumers`, and `policy_ok`. The broker health item turns
`bad` on policy drift, a queue with no consumer, any pending dead letter, or a
queue past 80% of its byte bound.

A blocked dead letter is never lost, but it is not instant either: RabbitMQ
retries the transfer roughly every three minutes, so `dead_letter_pending`
staying above zero for one tick is expected during a `news.dead` outage and
staying there for many is not.

### Deployment boundary

One RabbitMQ node, one durable volume. Process, channel and broker restart on
that persisted node are covered and tested. Node-level HA and survival of the
volume's destruction are not: a real three-node cluster would be a separate
infrastructure change, and nothing here should be read as claiming it.

The healthcheck runs `rabbitmq-diagnostics` as the `rabbitmq` user, never as
root. On 4.3 the server runs as `rabbitmq`, and a root-run CLI that reaches
`/var/lib/rabbitmq` before the node has written `.erlang.cookie` creates that
file owned by root with mode 0400 — after which the server cannot read its own
cookie and refuses to boot. A volume that already carries a correctly owned
cookie hides this completely, so it only appears on a fresh volume.

### Cutting over from the removed TTL retry lane

Run this once, from the primary checkout, when deploying the #400 image onto a
deployment that still has `news.retry`. It fails closed at every step. Two things
change on the broker: the policies appear, and the three business queues lose the
arguments the policy now owns. `news.dead` is untouched — its declaration is
unchanged, and it holds evidence.

`rabbitmqctl` runs as the `rabbitmq` user with `PATH` spelled out, because `su`
resets it on the Debian image and the CLI would not be found. Deleting a queue
goes through the management API, because `rabbitmqctl delete_queue --if-empty`
and the API's `?if-empty=true` are both rejected by quorum queues — emptiness is
something this runbook proves, not something the broker will check.

```bash
RMQ () { docker compose exec -T rabbitmq su -s /bin/sh rabbitmq -c \
  "PATH=/opt/rabbitmq/sbin:/opt/erlang/bin:/usr/local/bin:/usr/bin:/bin $1"; }
API () { curl -fsS -u "$USER:$PASS" "$@" http://127.0.0.1:15672/api/queues/%2F; }
```

1. Prove the broker: `RMQ 'rabbitmqctl version'` must report 4.3 or newer. Native
   delayed retry does not exist before 4.3, and an older broker would silently
   retry immediately.
2. Apply the policies while the old image is still running, from the checkout
   rather than from the container: `uv run tracefold news bus-policy apply`, then
   `uv run tracefold news bus-policy verify`. The deployed image predates the
   command, and `uv run` reaches the same broker because the compose host name is
   rewritten to its published loopback port. The command deliberately opens no
   AMQP connection and declares no topology, so it works while the queues still
   have their old shape. What it proves is the policy documents — every field of
   the checked-in entries, verbatim on the broker. A policy overrides a queue
   argument on 4.3 and applies the moment a matching queue exists, so the
   business queues are never argument-less and unconfigured at once; the
   per-queue confirmation that the policies actually govern the new queues is
   step 7, where Workers verifies the effective policy before consuming, and
   step 8's `bus-check`. Every deployment after this one re-applies the
   documents through the `rabbitmq-policy` Compose service.
3. Observe the old lane and let it drain: `API | jq '.[] | select(.name=="news.retry")'`.
   Record ready, unacked and the oldest message.
4. If anything in `news.retry` cannot drain deterministically, stop here. Do not
   purge it to make the migration proceed; the messages in it are business facts
   that have not been handled.
5. Stop the consumers so nothing can produce or redeclare:
   `docker compose stop -t 40 workers`. The OpenNews frames that arrive during
   this window are the ordinary deployment gap, and Recovery backfills them from
   official history afterwards.
6. Prove `news.raw`, `news.triage` and `news.deliver` each read zero for
   `messages`, `messages_ready`, `messages_unacknowledged`, `messages_dlx` and
   `consumers`, immediately before deleting them. Then delete them, so the new
   image can declare them without the arguments the policy now owns — keeping
   those arguments would work today and silently restore the old delivery limit,
   at-most-once dead lettering and message-count bound the moment the policy were
   removed:

   ```bash
   for q in news.raw news.triage news.deliver; do
     curl -fsS -u "$USER:$PASS" -X DELETE "http://127.0.0.1:15672/api/queues/%2F/$q"
   done
   ```

   Do not delete `news.dead`.
7. Deploy the hard-cut image (`make up`). Workers redeclares the three queues,
   verifies the effective policy and refuses to consume if it does not match.
   From this point no code path can publish to `news.retry`.
8. Wait at least one former TTL interval (30 s) and prove `messages_ready` and
   `messages_unacknowledged` on `news.retry` are both still zero. Then delete the
   old lane by hand — the application deliberately will not:

   ```bash
   curl -fsS -u "$USER:$PASS" -X DELETE http://127.0.0.1:15672/api/queues/%2F/news.retry
   curl -fsS -u "$USER:$PASS" -X DELETE http://127.0.0.1:15672/api/exchanges/%2F/news.retry
   ```

   `uv run tracefold news bus-check` must then report empty `drift` lists and
   `policy_ok` on every queue.
9. Cold-restart the stack (`docker compose restart rabbitmq workers`) and re-run
   `make status`, `uv run tracefold news bus-check`, and the open-incident check
   `SELECT cause_class, count(*) FROM news_opennews_incidents WHERE closed_at_ms
   IS NULL GROUP BY 1` — which the `0335` partial unique index now makes
   impossible to exceed one row per cause class.

   This step restarts both containers at once, which is **not** the graceful stop
   a deployment performs: RabbitMQ closes the AMQP connections underneath live
   handlers, Workers takes its fatal path, and the Receiver is cancelled without
   reporting a disconnect. Its receipt is therefore a `process_outage` interval
   opened by the next Workers process and closed when that process connects,
   recovered from official Strategy history like any other incident — never a
   `planned_shutdown`, which only a Receiver loop that exited on its own writes.
   Both are correct receipts for different events; a run that produces one must
   not be read as evidence for the other.

Rollback before step 6 may restore the previous image; the policies are additive
and the old image ignores them. After step 6 the queues carry the new shape and
after step 8 the old lane is gone, so rolling back would recreate an unconfigured
retry queue rather than the one that was deleted. Roll forward.

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
     news-triage, news-deliverer, news-janitor); the bounded polling loops
     (news-instruments, and with venues enabled news-quotes, news-reactions);
     when Trading is enabled, trading-capital-lane;
     workers-control
```

Quote plan/store uses an existing ordinary business permit. Event Reaction,
Janitor and #104's capital lane keep the one-slot heavy-business gate over
the same pool, so heavy work is serialized without blocking display quote
progress or consuming the four News hot-path slots. Quote provider calls are
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

Serve owns one pool of seven with ordinary/control admission `6/1`,
50 ms permit wait, 250 ms checkout, one-second statement timeout, JIT off,
parallel gather off, and 8 MiB work memory. Connections and ordinary requests
default to read-only, and since #256 the HTTP surface has no write route at
all: `tracefold news review submit` opens its own `tracefold_serve` connection
and one explicit read-write transaction, whose role grants permit INSERT only
on the two append-only review fact tables. Workers owns the exact pool/lane topology
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
admission `DeferError` before consuming; statement overruns and unknown faults
remain process-fatal.

News has no projection lease: the broker's single-active-consumer and
per-message ack are the fences.

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

The separate loopback Workers probe reports fatal News task reasons as
`news_consumer_fatal`, `news_receiver_fatal`, `news_broker_unavailable`, or
`news_recovery_fatal`. A classified live broker incident or Recovery transient
is recoverable work, not a crashed task: Workers readiness stays up while
`/api/news/status` names the open incident or closed-pending recovery state as
`reason=recovery_pending|recovery_transient`, retains the typed error code, and
remains degraded.

## Domain traces

News:

```text
OpenNews account Strategy WSS (whatever the account has enabled; no local allowlist)
  -> Receiver publishes each accepted frame to RabbitMQ (confirms)
  -> q:news.raw [SAC] Deduper: Item upsert -> exact source contract + event_kind
     -> title/identity -> same-kind Event new|member -> Gate -> storyline key
     -> publish event.<family>.<queue_priority> for admitted Events
        (unsupported_market persists and stops here)
  -> q:news.triage Triage (prefetch news.triage.concurrency):
     SemanticJudge.judge(TriageContext) -> EventSemantics.v2 + TradeRelevanceV1 -> SemanticNormalizer
     -> ReaderCard.v2
     -> deterministic assembler -> atomic SemanticJudgment/ScoredJudgment
     -> policy-v10 decide() -> news_verdicts (editorial + runtime manifest)
     -> verdict.push (an escalate rides the same key at AMQP priority 5)
  -> q:news.deliver [SAC] Deliverer: one configured-provider attempt per Event (kind first)
  -> RabbitMQ 4.3 native delayed retry inside each business queue: TransientError is a counted return
     (30 s delay, terminal after 3 total attempts), DeferError is an uncounted return (same delay);
     q:news.dead (at-least-once) for decode/PermanentError/exhausted-transient terminal cases
  -> Janitor: Event->Triage and push-Verdict->Delivery repair (15 s minimum age,
     30 min relevance ceiling, 50 rows/stage), band expiry, 30-day purge, broker snapshot
  -> /api/news/feed + /api/news/events/{event_id} + /api/news/status
```

`opennews_source_classifier_v1` is a pure step inside the existing Deduper, not
a registry, queue or worker. Diagnose every first-seen Strategy tuple retained
on the normalized Item by its `id`, `name`, `source_type` and `engine_type`,
plus the Event's durable
`event_kind`. A known id with a changed tuple is `source_contract_drift`; an
unbound scoreless market/wallet frame is `unsupported_market_contract`. Both
persist in nullable `news_events.source_contract_reason` and intentionally
produce no Triage message, model call or delivery. Recovery applies the same
classifier and, for a complete history tuple, the same strict parser without
writing the live OI rank fact; it never delivers. The official hits contract
omits `total` on an empty first page; the adapter normalizes only that exact
shape to zero. Other envelope, pagination, and hit failures remain closed and
persist as `opennews_history_payload_<reason>` rather than one broad payload
error. An accepted history hit must carry a normalized provider record ID and
published timestamp; Recovery indexes the raw row with the same ID normalizer
used by the canonical parser. Repeated rows for the same normalized provider
record ID coalesce to the first row in provider order. A history hit missing
`source_type` or another tuple field is named drift and must not be repaired by
guessing from Strategy id. Ordinary and deterministic recovery remains
`admission=recovery`, while a newly observed unsupported contract retains its
named `admission=unsupported_market_contract`. The hard cut does not rewrite a
verdict/delivery ledger before genesis. Migration `0336` then deletes that
entire pre-genesis ledger and requires all News queues to be empty, including
stale Event references, before it runs.
Migration `0336` deletes pre-cut deterministic rows that lacked durable typed
success evidence. Current Admission and Triage therefore see only the current
source contract. The OI signal row
used during migration is a derived read-model row, not an alternate material
truth.

Broker: RabbitMQ 4 (`rabbitmq:4-management` in compose; `news.broker.url` is
the AMQP URL, `news.broker.name_prefix` prefixes every exchange/queue).
`tracefold news bus-check` connects, declares the topology idempotently, and
prints per-queue message/consumer counts. Outside a container the compose
host names resolve to the published loopback ports (`postgres` ->
`127.0.0.1:${TRACEFOLD_POSTGRES_PORT:-56532}`, `rabbitmq` ->
`127.0.0.1:${TRACEFOLD_RABBITMQ_PORT:-5672}`), so the same `config.yaml`
serves `docker compose exec` and host-side CLI runs. Connection/setup faults may
reconnect before consuming a delivery. Once a message task owns a delivery,
broker, settlement, or unknown handler failure leaves the consumer scope and
makes Workers unready; it is never converted into a permanent data error.
While the broker is unreachable the Receiver keeps the WSS open, records every
failed interval as a durable `broker_unavailable` incident, and Recovery fills
the closed interval from official Strategy history. Queue overflow on
`news.raw` (`reject-publish` at 100k) opens recovery-eligible
`broker_backpressure`. A confirmed live publish always reconciles both broker
causes from PostgreSQL, including incidents created by a previous process.

Dead letters: `q:news.dead` receives permanently failed messages (schema
errors, explicit `PermanentError`, `TransientError` after 3 attempts, and
broker delivery-limit hits). Unclassified handler exceptions, retry-lane
publish failures, and ack/reject failures do not terminally settle the
delivery; they fail Workers instead. The queue is declared with delivery limit
1,000,000 so peeking never drops evidence. `tracefold news dlq inspect
[--limit N]` peeks without consuming, `tracefold news dlq replay [--limit N]`
republishes to the topic exchange with a fresh attempt counter, and
`tracefold news dlq purge` empties it; the management UI (`127.0.0.1:15672`)
and `bus-check` show the depth. Replay is the one that writes back into the
pipeline, so it proves the broker contract first: an effective policy that is
not the checked-in one, a management API that cannot be read, or any unexpected
name under the prefix and it exits non-zero having read no message. A dead
letter it cannot decode stops the batch — the message is returned to the queue
and named in the error, along with how many had already been replayed — because
`news.dead` is terminal and rejecting it would delete the only copy. Fix the
decode path or remove that message with `purge`, which is the only command that
destroys evidence. A growing DLQ with a healthy DB means a code bug, not load.
Purge only after the cause is fixed; recovered Items never deliver, so
re-driving old raw frames is safe.

Control: there is none. `news_control_state` and `tracefold news control` were
removed after the singleton never withheld a card: across the whole retained
history no verdict carried `override_rule = 'muted'` and no delivery settled as
`delivery_paused`, while both hot-path consumers read the row on every message.
To stop delivery, stop the Workers container; to stop a source, turn its
Strategy off in the OpenNews account (#126).

Model failure: Triage's sole Interface is
`SemanticJudge.judge(TriageContext) -> SemanticJudgment`. The production
Adapter runs the code-owned Program
`EventSemantics.v2 -> deterministic SemanticNormalizer -> ReaderCard.v2 ->
deterministic assembler`. The normalizer changes a stray non-negative
`restates` value on `new_fact`/`progression` to `-1`, records both values on the
EventSemantics trace, canonicalizes the nested `TradeRelevanceV1` sets, and
spends no provider call. ReaderCard.v2 produces only `headline_zh` and
`why_zh`; the assembled Verdict has no second title or action projection. Both
Predictor payloads exclude queue priority,
provider score, Gate macro lexicon, queue lag and watchlist; ReaderCard receives
only its reduced semantic view and never ToldContext or delivery intent. A
successful primary route makes exactly two serial provider calls. JSONAdapter
may make one format fallback per Predictor, so one route makes at most four
calls; provider errors and truncation do not spend a format fallback. The
code-owned 20-second deadline covers the whole route. If primary fails, a
configured `llm.news_triage_fallback` restarts the full Program with its own
deadline budget. Its ReaderCard slot explicitly aliases the same
endpoint unless a complete `llm.news_reader_card_fallback` endpoint is present;
one missing or invalid fallback slot disables fallback instead of mixing
routes. One Program execution's maximum is eight. The typed LM seam makes one
stock DSPy/LiteLLM call per physical invocation with no client cache or provider
retry, so every billable attempt is visible. There is still one persisted final semantic judgment and one card,
not a restored Analyst stage. Capacity planning must account for the normal
1 -> 2 call increase and serial latency. A stale-ledger re-ask is a second full
Program execution:
normally four calls total for that Event, with the same per-execution eight-call
ceiling and all superseded/failed work included in telemetry.

By default both Predictors use the Triage endpoint, but each has its own
Adapter and code-owned token cap. A complete `llm.news_reader_card`
endpoint moves only ReaderCard's primary slot. A complete
`llm.news_reader_card_fallback` independently moves the ReaderCard fallback
slot; otherwise that slot is an explicit alias of the EventSemantics fallback
slot. `tracefold config` and `/api/news/status.pipeline` expose the effective
model names and dedicated-Reader flags without exposing endpoints or
credentials.

A schema-capable Predictor may spend one stock JSONAdapter **format fallback**
only after a schema answer cannot parse. Provider timeouts, rate limits,
connection errors and other typed LM failures do not spend that format call;
they fail the route and may restart the complete Program on fallback. A
`max_tokens` truncation (`news_program_output_truncated`,
`finish_reason=length`) also does not retry. The code-owned primary-route breaker defaults to three retryable
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
are terminal, `bad` at 30 %. A `warn` or `bad` level from any enabled health
lane makes top-level `state` `degraded`. A closed incident with
`recovery_status=pending` keeps ingest at `warn` and is exposed under
`ingest.recovery` with its count, oldest opening time, latest typed error, and
bounded product-readiness `reason` (`recovery_pending` before a failed attempt,
`recovery_transient` after one);
the API cannot turn green merely because the live connection recovered. This
projection describes at most the next 20 incident rows, using the same bounded
batch statement as Recovery and query audit. The
API no longer reports a green `ready` state beside a failing health item. The
five visible Event-feed stages in `funnel_24h` use one cohort: Events opened
in the rolling 24 h window, tested for parsed/admitted/Triage/sent durable facts. The independent Triage and
delivery rolling ledgers remain throughput/health facts, so late work does not make a later funnel stage exceed
its intake cohort. `reasons_24h` (Chinese labels
over `suppressed_by_reason`, `dropped_by_rule`, `throttled_by_key`,
`pushed_by_rule`, `triage_degraded_by_code_24h`) say where the day went. Every
Event's `outcome` (feed, detail, `news why`) is the same twelve-kind conclusion:
`held_recovery`, `held_gate`, `expired_triage_handoff`,
`expired_delivery_handoff`, `queued_publish`, `queued_triage`, `dropped`,
`throttled`, `degraded_dropped`, `pending_delivery`, `delivered`,
`delivery_failed`. The two expired kinds are terminal `held` projections after
the 30-minute handoff ceiling; only the `queued_*` kinds are live backlog.

Diagnose News in this order:

1. `/api/news/status.state` and `ingest`: `connected`, `last_frame_at_ms`,
   `open_incidents`, and `recovery.pending_count/reason/last_error_code`. Which Strategies are feeding the pipeline is a question for
   the OpenNews dashboard, not for Tracefold.
2. For a market-contract frame, inspect its normalized four-field Strategy
   identity, `event_kind`, `source_contract_reason`, admission, classifier version and parser
   version. Do not retry a named unsupported contract into the model lane.
3. `tracefold news bus-check`: consumers attached to every queue (Deduper and
   Deliverer show exactly one), `news.dead` depth, each queue's `delayed`
   (native retry backlog), `dead_letter_pending` (at-least-once dead letters the
   source queue is still holding) and `bytes_used_bps`, plus `policy_ok` and a
   `drift` list of names the final topology does not contain. It exits non-zero
   on either policy drift or topology drift. `tracefold news dlq inspect` prints
   the dead-letter bodies with their broker `delivery_count`.
4. `pipeline`: `candidate_share_24h` (the Gate now admits nearly every ordinary News Item;
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
5. `delivery`: `sent_1h`, `terminal_24h`, `last_error_code`
   (`delivery_unavailable` = push disabled or the selected provider configuration unavailable;
   `ambiguous_after_crash` = a send whose ack was lost). Historical rows can
   still contain the retired `delivery_paused` and
   `hourly_cap_reached` error, but policy v7 never writes it.
   `delivery_available` is true only when the selected provider contract is
   complete and Workers is running. If push is explicitly enabled with an
   absent or insecure Telegram token file, Workers fails startup; inspect
   `workers_state` and Workers logs instead of treating the target as available.
6. `tracefold news review queue --view coverage --hours 168` first checks
   whether there is enough same-version production evidence and accepted
   review coverage to make a quality claim. Work the deterministic strata with
   `review queue`, inspect the exact frozen input using `review evidence`, and
   append a rubric with `review submit`; a fact that never became an Event uses
   `review external-miss`. Do not infer precision/recall from unlabeled rows or
   infer causality from the market tab.
   Before and after a Prompt or policy edit, run
   `news learning baseline` and name the mode you mean. Code around the
   instructions — the wire envelope, the output contract, the route budget — is
   not artifact state: editing it changes what every call is billed for while
   leaving `program_sha256` untouched, and what catches it is the computed
   `envelope_sha256` pin in
   `tests/contract/test_program_release_identity.py` (see
   `docs/ARCHITECTURE.md`). `--mode recorded` costs
   nothing and answers "is the metric still wired the way it was"; it makes no
   provider call, so it cannot see a Prompt change. `--mode compile_live` is the
   native Program GEPA optimizes on one endpoint. It has no fallback route, but
   disables the whole-route deadline and cross-case primary breaker that GEPA
   does not run; the endpoint keeps its per-call timeout and DSPy JSONAdapter's
   single format fallback per Predictor.
   `--mode runtime_live` is the configured production Program route and is the
   only mode whose failure rate resembles the reader's — it spends real provider
   calls on the same single-slot GPU that serves Triage, so both live modes
   require an explicit `--max-model-cases N`; expect exactly two physical calls
   on common success, at most four on one route and at most eight across a full
   primary/fallback judgment. Read both
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
   Freeze a post-epoch development dataset, then run one command:
   `learning run --development SHA --out DIR` with explicit metric, task,
   reflection and metric-judge call limits, a baseline corpus bound, a total and
   a per-call provider-cost limit, and a seed. It runs `readiness` (zero model
   calls), the standalone `compile_live` baseline over that exact corpus, and
   the one optimization, into that directory. It ends in `NO_OP`, `REJECTED` or
   `ADVANCE`; only `ADVANCE` writes `prompt_candidate.json`, and all three write
   a complete `optimization_report.json`. Each of the three roles is one
   `ModelExecutionIdentity`, and calls/cost/failures are accounted separately
   before they are summed. The standalone baseline in
   `baseline-compile-live.json` and the GEPA-seed score inside
   `optimization_report.json` are two different numbers; running the three legs
   in one process over one dataset SHA and one configured judge route is what
   keeps them comparable. The
   three legs stay callable one at a time for a partial re-run. Then `release
   register --candidate prompt_candidate.json` binds it to the active stable and that frozen dataset
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
   (expect a handful per day; a surge means same-key floods). Program v8 fails
   closed on missing `novelty` or taxonomy. Migration `0336` deletes pre-current
   trace diagnostics; they do not appear in the current status contract.
8. `tracefold news replay <hits.json> [--gate-policy open|strict]`: reproduce
   Deduper+Gate on a saved provider payload without broker or model.

The current evidence eligibility window starts at the deployment timestamp the
running deployment wrote into `news_learning_epochs` for its own bundle. Find it
with `WITH agent AS (SELECT stable_sha FROM news_review_active_agent_v1 ORDER BY
created_at_ms DESC LIMIT 1) SELECT e.epoch_id, e.starts_at_ms FROM
news_learning_epochs e JOIN agent ON agent.stable_sha = e.bundle_sha`. Take the
newest agent *before* the join, not after: joining the whole appointment history
and then taking one row reports the previous deployment's epoch when the current
agent has no row yet, which is exactly the case worth diagnosing. Only accepted `news_review_v6` rows from that
epoch, bound to that exact bundle, enter metric v6, GEPA or release evidence. Every earlier Prompt/Program
baseline remains readable audit history but cannot enter a dataset or release
stage. Do not
interpret a successful migration, a valid Program artifact, or the new
two-Predictor trace as proof of higher quality; that claim begins only after
post-epoch accepted reviews and future holdout/shadow/canary evidence exist.
Issue #117 deliberately lands the production persistence/read/UI seam before
those quality denominators exist, by operator sequencing decision. New ordinary
News judgments therefore carry taxonomy immediately, while taxonomy quality
stays `UNKNOWN`. Run `taxonomy-register` before opening the future holdout;
registration derives its exact Git/image/bundle identity from the active
Workers deployment receipt and computes the Shadow Program/model binding from
operator configuration. Run bounded `taxonomy-shadow --file CONTEXTS --limit N
--out RECEIPTS` batches next; each case records success, schema invalid,
provider failure, or budget/deadline failure, with one or two ordered replayable
physical attempts. Provider I/O is outside the database transaction, and the
command writes only append-only learning artifacts. `taxonomy-evaluate` then accepts only that
PostgreSQL-clock registration, database-verified current regression evidence,
accepted Review v6 Gold and replayable shadow artifacts, and still requires
complete observation/attempt/recording, development and post-registration
holdout denominators. Missing Shadow evidence is `UNKNOWN`, never an empty-set
PASS. Do not use taxonomy to alter Gate,
delivery or Trading, and do not describe schema deployment as a model-quality PASS.
The immediate cost is the normal 1 -> 2 provider-call increase. The intended
future benefit is per-Predictor feedback, demonstrations, routing and
fine-tuning without widening the consumer's `SemanticJudge.judge()` Interface.

Retention: unjudged `news_items`/`news_events` older than 30 days are purged by
the Janitor; judged evidence is retained under the configured 365-day tier and
bands expire with their family window. The same turn uses the existing
one-slot heavy DB admission for learning evidence: unreferenced model
recordings/cases become eligible after 90 days, report-referenced rows and
ordinary artifacts after 365 days, while current and previous distinct stable
release chains and an active canary remain pinned. Each table deletes at most
500 rows per turn; `/api/news/status.learning_retention` exposes the capped
remaining eligible count, last-turn deletes, oldest retained age and error.
Feed shows Events from the first frame after deployment; there is no backfill
of pre-V3 history.

### Why an OI frame produced no case (#264, #331)

`trading_candidate_gate_decisions` — the admission ledger — holds one row per
`(source_key, gate_version, gate_config_digest)` and answers this without a
replay. It replaced `trading_runtime_state.funnel`, a JSONB document reset on
the UTC day key that counted one entry per *re-read* of the same frame; #331
dropped the column, so this ledger is now the only answer and there is no
second number to disagree with it.

Start from `uv run tracefold trading status`:

- `candidate_counts_24h` / `candidate_counts_7d` — how many source frames the
  lane saw and what happened to them, by `DEFERRED | REJECTED | CASE_CREATED |
  EXPIRED` for current writers. Counted on the frame's own observation time, so a
  runner restart that re-reads a backlog cannot move yesterday's frames into
  today. `RESEARCH_ONLY` can still appear while the 90-day ledger retains rows
  written by the retired single-venue contract; current Hyperliquid frames use
  their native binding.
- `candidate_reasons_24h` — the same population by `stage:reason`. The stages run
  `source -> venue -> eligibility -> catalog -> market_context -> freeze`
  (`routing` still appears on rows written before #331), and the reason
  vocabulary is closed; anything outside it is a bug, not a new rule.
- `latest_source_at_ms` and `latest_gate_eligible_at_ms` sit on either side of
  admission. A recent source with no recent `CASE_CREATED` is an admission
  question; a recent Case with no `latest_intent_emitted_at_ms` is a strategy or
  atomic admission question. Compare `latest_entry_fenced_at_ms`,
  `latest_position_opened_at_ms`, and `latest_position_closed_at_ms` only after
  an Intent exists.

For one frame, `GET /api/trading/gate/{event_id}?lane=oi` returns the decision
with its `gate_evidence` — the measurement it failed on and the threshold it
failed against — or read the row directly:

```sql
SELECT status, stage, reason, retryable, attempt_count, evidence, case_id
  FROM trading_candidate_gate_decisions
 WHERE source_key = 'oi:<event_id>:oi_signal_v1'
 ORDER BY last_evaluated_at_ms DESC;
```

`attempt_count` is how many times the scanner re-read that source, not how many
times the answer changed: a terminal row keeps its status, stage, reason and
evidence, and only the two evaluation counters move. `DEFERRED` is the only
non-terminal state and means a later scan could genuinely answer differently
(`market_data_unavailable`, `capability_absent`, `underlying_busy`); the lane's own sweep
turns one `EXPIRED` with `trigger_stale` once the frame is past the trigger
budget, so an open row that never resolved reads as the clock's answer rather
than as pending work. Retention is 90 days, purged in bounded batches by the
same turn.

Two adjacent situations are *not* refusals and read as such:
`decision: null` on the HTTP surface means no row under any `gate_version` —
after a gate version bump that is the honest state — and a source whose case was
already created reports `CASE_CREATED` with the `case_id`, which is the link to
`trading_cases`.

Changing a threshold does not rewrite history: `gate_config_digest` is half the
key, so an edit starts a new row and the old one stays as the record of what the
old rule decided.

### OI BAR replay and attribution (#286)

`uv run tracefold trading replay-oi --days 7 --venues
binance.perp,hl.perp --fidelity bar_v1` freezes the bounded parsed OI source
population, active capability snapshot and canonical blacklist, then fetches
source-native public Binance/Hyperliquid OHLCV bars. It gives every selected
source one terminal decision, coverage reason or fresh Nautilus
`BacktestEngine` outcome and keeps capital admission separate from the Alpha
decision.

This is a cold audited research command, not a read-only diagnostic. Before the
Serve repeatable-read snapshot it uses one short Workers transaction to
materialize timed blacklist expiry under the canonical revision contract. It
then performs public market-data I/O outside database transactions, atomically
publishes a content-addressed artifact under `--out`, and uses one final short
Workers transaction to insert the immutable `trading_replay_runs` receipt.
Rerunning an identical spec verifies and reuses the existing artifact/receipt.

The replay process never mounts Binance Demo execution credentials and never
constructs an execution adapter or performs a provider order write. BAR-v1 is
reported as BAR-v1: funding and portfolio drawdown remain unavailable, and
missing source-native history receives a stable coverage reason rather than
fabricated data. `run_id` binds the source rows, market slices, capability,
blacklist payload, Gate, Strategy, regime, notional, execution policy, engine,
fees and fidelity identities.

### Price Review plane (#88)

`/api/news/status.price` is the first place to look:

- `sources[]` — one row per provider source with `received_age_ms`, optional
  `source_age_ms`, their maximum `effective_age_ms`, `freshness_basis`, raw
  timestamps and the worst `state` across that source's quotes. A source
  whose state has been `stale` for minutes is either rate-limited or blocked;
  the loop's last error names which (`venue_rate_limited`, `venue_blocked`,
  `venue_timeout`). One failing venue never clears another and never blanks a
  price: the previous row stays and simply ages. Current is stale above 45 s or
  when an applicable raw timestamp is more than 5 s in the future. Binance day
  reference is an independent post-store read: it is valid through 360 s and a
  failure or expiry removes only the percentage, never the current price.
- `reaction_partial_7d` / `reaction_complete_7d` / `reaction_unavailable_7d` —
  the Reaction backlog. A rising `partial` count with a flat `complete` count
  means the 4H leg is not landing; a rising `unavailable` count is a data
  question, not a health one, and `tracefold news review queue --view market`
  names the reason. (The HTTP route that used to answer this was removed with
  the ReviewDesk console in #256; the CLI reads the same projection.)

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
creates the direct-login `tracefold_owner`, `tracefold_serve`,
`tracefold_workers`, and `tracefold_nautilus` roles when run by the bootstrap
superuser, verifies the role contract, and applies the Serve read / Workers
write grants) followed by the linear revisions through the current
`20260831_0339` head. The #112 chain
adds ReviewDesk tables and grants the existing Serve role only their
append-only INSERT capability. It adds no login role or password. A live
database stamped at an earlier revision upgrades with `tracefold db migrate`;
a fresh database runs the same complete chain. Every revision is irreversible;
a downgrade is a backup restore. Stop Serve and Workers before applying a
chained revision
(each takes the maintenance gate advisory lock and refuses to run while
Workers hold the steady lock).

The normative authoring checklist, required evidence, and 0330–0332 object
authority/cost audit are in [the migration guide](MIGRATIONS.md). Published
revision files are immutable; a correction is a forward revision.

### News current-contract genesis (`0336`, one time)

This is the destructive cut required by #398, not an ordinary retention run.
Run it once, from the exact reviewed main SHA and image, with Serve, Workers and
Nautilus stopped. It is irreversible in Alembic: the only rollback is restoring
the verified pre-cut database snapshot and the matching broker snapshot before
starting the old image. It never converts, backfills, translates, dual-reads or
serves old News evidence.

The migration owns this complete disposition. It compares the live set of all
`public.news_%` tables, views, functions, triggers, sequences and foreign keys
with its explicit before/after inventories; an added, missing or externally
referenced object makes the transaction fail rather than widening it through
`CASCADE`. Its schema digest also seals definitions, columns, constraints and
indexes.

| Disposition | Exact owners |
| --- | --- |
| Empty and reset identity | `news_agent_assignments`, `news_agent_runtime_manifests`, `news_canary_activations`, `news_deliveries`, `news_event_assets`, `news_event_bands`, `news_event_evidence_snapshots`, `news_event_members`, `news_event_reactions`, `news_events`, `news_external_miss_snapshots`, `news_ingest_state`, `news_items`, `news_learning_artifacts`, `news_learning_cases`, `news_learning_epochs`, `news_learning_retention_state`, `news_model_recordings`, `news_oi_signals`, `news_opennews_incidents`, `news_reviews`, `news_verdicts` |
| Preserve rows and schema | `news_market_instrument_listing_events`, `news_market_instruments`, `news_market_liquidations`, `news_quote_snapshots`, `news_symbol_aliases` |
| Drop permanently | both `current_contract_archive_only` columns; `news_current_events_v1`; `ix_news_events_current_opened`; `news_current_event_archive_guard`; all three archive-check triggers |
| Recreate current-only | `news_review_task_source_v1`, `news_review_records_v1`, and the current verdict-evidence guard |
| Keep current objects | `news_review_active_agent_v1`, `news_review_external_source_v1`, `news_review_pairwise_tasks_v1`, current validation/append-only functions and triggers; the incident sequence is reset with its table |
| Preserve outside News evidence | every `trading_%` and Capital owner, every historical Alembic revision, and the complete Price/instrument rows named above |

Phase 0 is fail-closed:

1. Verify the branch is merged, the primary checkout is clean at that exact
   successful-main-CI SHA, and `uv run tracefold config` reports only the
   intended operator-owned paths and redacted configured state. Record the Git
   SHA, Alembic revision, image ID, runtime revision, target runtime-manifest
   SHA, `tracefold db audit`, and `tracefold news bus-check` output in the
   maintenance record.
2. Let Workers drain the four code-owned queues (`news.raw`, `news.triage`,
   `news.deliver`, `news.dead`, with the configured prefix). Save
   any dead-letter incident evidence, purge it with `tracefold news dlq purge`,
   then stop Serve, Workers and Nautilus. Query RabbitMQ after the stop and
   require `messages_ready=0` and `messages_unacknowledged=0` for all four
   queues. With every queue empty, both the dead-letter count and stale Event
   reference count are exactly zero.
3. Take a restorable full PostgreSQL snapshot with the operator's normal backup
   mechanism, restore it into an isolated database, and run `tracefold db
   audit` there. Compute the SHA-256 of the immutable snapshot file only after
   that restore succeeds. Also snapshot the RabbitMQ volume/topology if the
   backup policy requires a whole-stack rollback. Do not continue with an
   unverified or mutable snapshot.
4. Prebuild the exact main image with
   `TRACEFOLD_BUILD_REVISION=<40-hex-main-sha>`, inspect its full
   `sha256:<64-hex>` image ID, export it as `TRACEFOLD_IMAGE_DIGEST`, then run
   `make news-genesis-manifest`. Record `data.runtime_manifest_sha` from that
   read-only command as the expected target runtime-manifest SHA. The command
   computes it inside that same configured image from the active operator
   config, stable bundle, compiled candidate set, image ID and runtime revision.
   Do not use a tag or a value from another build.
5. Export one compact JSON value as
   `TRACEFOLD_NEWS_GENESIS_PREFLIGHT_JSON` with exactly these fields (no extra
   keys), then run `make up`. The Makefile rechecks exact main CI, owns the
   deployment lock, rebuilds or reuses the exact image, stops runtimes, and the
   `migrate` service receives the JSON and image identity. Before changing the
   database, `make up` independently computes the target manifest through the
   same read-only image command. It runs migration only after
   the broker policy import, reads every configured News queue after the
   runtimes stop, and rejects a missing queue, consumer, policy/topology drift,
   ready/unacked/delayed/dead-letter message, or a queue total that differs
   from the JSON:

   ```json
   {
     "mode": "maintenance_window",
     "tested_git_sha": "<40 lowercase hex>",
     "deployed_git_sha": "<same 40 lowercase hex>",
     "image_digest": "sha256:<64 lowercase hex>",
     "runtime_revision": "<same 40 lowercase hex>",
     "runtime_manifest_sha": "<64 lowercase hex>",
     "snapshot_sha256": "<64 lowercase hex>",
     "snapshot_verified": true,
     "queue_ready": 0,
     "queue_unacked": 0,
     "queue_dead_letter": 0,
     "queue_stale_reference_count": 0
   }
   ```

`0336` never infers freshness from mutable News or Trading rows. The migration
command recognizes a fresh install only when `alembic_version` did not exist
before the migration run; it still computes exact image/runtime identities and
requires the same live empty-broker observation, but records the canonical empty
snapshot digest because no pre-existing database state exists. Every existing
database requires the operator JSON above. `0336` rejects a missing field,
extra field, invalid identity, unverified snapshot, nonzero or unobserved queue
count, a Git mismatch, an image/runtime-manifest mismatch or schema-object
inventory drift before deleting anything.

After deployment, require Alembic head `20260831_0339`; zero rows in every cleared
owner except the single new `news_learning_artifacts(kind='epoch_reset')` row
and fresh singleton rows in `news_ingest_state` and
`news_learning_retention_state`;
unchanged counts in all five preserved owners and all `trading_%` owners; no
retired column/view/index/function/trigger; and no unvalidated `news_%`
constraint. Recompute the receipt address from canonical
`{kind: "epoch_reset", payload: ...}` and require it to equal `artifact_sha`.
The payload must bind the exact Git/image/runtime manifest, pre/post News schema
digests and counts, preserved counts, verified snapshot digest, the
content-addressed live broker observation, zero queue and stale-canary counts,
the full disposition, and
`rollback=verified_snapshot_restore_only`.

Start the exact image, then require Workers `/readyz` to publish the same target
runtime-manifest SHA. `make up` always compares it with the pre-migration target
for maintenance upgrades and fresh installs. Require all readiness endpoints
green, the first new Event to
complete the current evidence/verdict/delivery path, and a restart to preserve
that result without recovering any pre-genesis identifier. Open a new review,
dataset, candidate and canary epoch only from post-genesis evidence; the
migration receipt is not evidence of model quality. The subsequent `0337`
revision only grants Nautilus execution on `trading_canonical_jsonb(JSONB)`;
`0338` removes the retired global readiness fields; `0339` then hard-cuts the
migration identity without altering or reintroducing any News schema object.

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
`0315` is the #288 exact source-contract route and Event-kind hard cut. It
trips open canary activations and appends the factory-v6 to factory-v7 receipt,
but neither rewrites nor appends the `program_v7` epoch row. Earlier rows and
bundles remain immutable audit history; exact current-bundle acceptance makes
prior-factory evidence audit-only, so the factory-v7 cohort starts at zero.
`0316` adds the #283 immutable Trading Intent handoff and Nautilus execution
projection; on an existing volume, provision the Nautilus role before applying
it as described above.
`0318` is the #306 prompt-layer hard cut. It
appends `program_v8` for `factory_v8` and trips every armed or active canary.
Two byte changes land under that one identity migration, deliberately paid once
rather than twice: the sealed kernel / nine RulePacks / advisory / authority-seal
layering collapses into one seed instruction per Predictor, and the Program's
self-owned chat transport composes the request envelope DSPy's JSON adapter used
to compose. `program_v7` evidence — which closed with zero accepted candidates,
zero canary activations and two empty advisory instructions — becomes immutable
audit history. It adds no column and is irreversible.
`0319` is the #310 envelope hard cut and the current epoch boundary. It appends
`program_v9` for `factory_v9`, trips every armed or active canary, and re-issues
the stable root over unchanged seed texts. The self-owned transport's
structured-output constraint now follows the endpoint — `json_schema` where
supported, `json_object` with the same schema inlined into the system message
for DeepSeek-class endpoints — which moves fallback-route prompt bytes; the
first hours of the v8 cohort, a third of whose verdicts degraded against the
rejected format, become immutable audit history.
`0320` is the #286 TradeIntentV2 hard cut. It adds immutable capability/replay
ledgers plus the News catalogue's immutable listing-validity events, refuses a
warm migration, and never rewrites V1 history. Roll forward; there is no
downgrade to a second execution permission model.
`0321` is #314's computed-identity cut and the last epoch migration there will
be. It adds `bundle_sha` and `envelope_sha256` to `news_learning_epochs`, ties
`epoch_id` to `left(bundle_sha, 8)` by CHECK, relaxes `program_factory_id` to
nullable, and grants `tracefold_workers` INSERT so the startup barrier can open
the running bundle's epoch itself; UPDATE and DELETE stay revoked and the
append-only trigger stays. The artifact loses its `factory_id` field, which
re-issues the stable root over unchanged seed texts one last time, so the first
deployment after this migration opens a new `bundle_<sha8>` epoch and trips every
armed or active canary. After it, an identity migration is a code change plus a
re-pinned line in `tests/contract/test_program_release_identity.py`.
`0322` adds the durable News delivery edit-intent lifecycle and its stale-edit
index; it performs no provider call and requires no new credential or runtime
role. `0323` adds the receipt-bound deletion lifecycle and its stale-intent
index for authoritative five-venue single-name absence.
`0324` replaces both lifecycle shape constraints with two-valued predicates so
PostgreSQL `NULL` semantics cannot admit partial edit or delete intent. It fails
closed if an existing row violates either lifecycle before replacing the constraints.
Issue #325 owns the operator-approved recovery: keep the database at `0323`,
repair only the invalid lifecycle tuple from provider evidence, and then roll
forward to `0324`; never start an older-schema image after that migration commits.

Before applying 0326 remove `trading.candidates.symbol_cooldown_seconds` and
`trading.candidates.max_rank_in_window` from `~/.tracefold/config.yaml`; both
models are `extra="forbid"`, so either key left in place fails Serve and Workers
at settings load. 0326 also refuses to run while any Intent is nonterminal —
`daily_entry_fence_nonterminal_intent` — because it moves the execution policy
identity to `trade_intent_policy_v3` and removes the CHECK that pinned the old
digest; drain the lane (`uv run tracefold trading control paused`, wait for the open
thesis to close) before migrating. Verify after restart: `/api/trading/gate`
answers with a `trading_admission_v3` config block whose `config_digest` has
moved, and a second Case on a day that already entered is decided rather than
deferred.

Before applying 0325 remove `trading.regime.*`, `trading.policy.*`,
`trading.candidates.news_lookback_seconds`, `trading.candidates.oi_lookback_seconds`,
`trading.candidates.max_dspy_cases_per_day` and `llm.trading_decision_model`
from `~/.tracefold/config.yaml`; the settings schema rejects them and
Serve/Workers fail to start with them present. Verify after restart:
`uv run tracefold trading status` reports `control`, the active capability
digest, `engine_ready=true` and `unexpected_exposure=false`; `/api/trading/gate`
answers with a `trading_admission_v3` config block; and the first admitted
Binance OI frame reaches a Case whose `policy_checks` carry the thresholds it
was decided against.

Before applying 0278 remove `providers.macro_sources` and the
`llm.macro_document_analysis_*` keys from `~/.tracefold/config.yaml`; the
settings schema rejects them and Serve/Workers fail to start with them
present. Verify after restart: `tracefold db audit` reports
`migration_status` `ready`, current News table counts, `news_schema.exact`, and
`runtime_roles.ok`; `tracefold news bus-check` shows one consumer on
`news.raw` and `news.deliver`; `/api/news/status.state` becomes `ready` only
after the WSS, broker, model, delivery, and Workers health checks are all green;
`/api/macro/overview` answers `404`; and the first candidate
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

Raw retention selects stable `(observed_at_ms, item_id)` candidates and deletes
at most 500 rows per transaction, four transactions and three seconds per
Janitor turn. Each batch rechecks the 30-day raw and 365-day judged predicates
inside its `DELETE`; reaching a row/batch/time budget leaves backlog for the
next turn. Band expiry is likewise an ordered 500-row transaction. The raw
retention metrics report deleted rows, batches, wall time, capped backlog
sample, whether the sample hit its cap, and oldest eligible age. Each batch,
band expiry, and learning-retention call uses a separate cold transaction.

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
- Redeploying an earlier image re-appoints that bundle and re-enters its existing
  epoch rather than opening a new one, and the epoch keeps its original
  `starts_at_ms` because the table is append-only. Evidence produced by the
  intervening deployment is therefore inside the restored epoch's window for the
  readers that can only compare timestamps — external-miss eligibility above all,
  since an external miss carries no bundle to filter on. Freezing a dataset
  immediately after a rollback will carry those misses; if that matters, freeze a
  window that starts after the re-appointment.
- `news_learning_epochs` is append-only permanent audit truth, whether a
  migration or the startup barrier wrote the row. An epoch change alters
  eligibility, not retention: all earlier evidence remains auditable until the
  existing deterministic retention policy makes an otherwise-unpinned row
  eligible;
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
growth outside the 90/365-day envelope. The purge shares the one-slot heavy
admission with Event Reaction and Trading, never ordinary Quote admission or
the four-slot News hot lane.

### Backup, recovery, and restore drill

The packaged deployment tier is a single-host, operator-managed Compose
database: it provides no automatic failover, replica, backup scheduler, WAL
archive, or PITR service. The deployment owner must provide encrypted daily
backups and an additional verified backup immediately before every destructive
migration. The operating targets are RPO at most 24 hours and RTO at most four
hours; the pre-migration snapshot makes the migration cutover RPO the snapshot
time. These are targets only when the external backup owner monitors freshness
and restoreability. If a deployment requires a tighter RPO, its platform owner
must archive WAL and operate PITR outside Tracefold; the application neither
ships nor retains WAL.

The weekly scheduled diagnostics run an isolated production-image restore
test. Run the same entry manually with
`TRACEFOLD_TEST_POSTGRES_DSN=<dedicated-admin-dsn>` and
`TRACEFOLD_TEST_POSTGRES_MIGRATION_DSN=<direct-owner-dsn>` set, then run
`make postgres-restore-drill`. The owner DSN must use the existing migration
capability credential; it is never emitted in the result.
It creates uniquely named disposable source/target databases, seeds
representative News current/archive and Trading facts, uses the exact
PostgreSQL 18 Bookworm client image for custom-format dump/restore, migrates to
head directly as the ordinary owner, performs deep schema/role/identity audit
and bounded smoke, records head,
duration and identity counts, then drops both databases. It never reads or
writes the database named by the supplied DSN. This proves the mechanism, not
the freshness of a live operator backup.

Live restore/audit procedure:

1. Restore the PostgreSQL backup into an isolated database and migrate only to
   the image's recorded schema head; never set
   `tracefold.learning_retention_purge` or issue manual DELETEs.
2. Run `uv run tracefold db audit --deep`; confirm migration head, exact News table
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
scan, any temporary read/write blocks, or read/return amplification above the
budget declared by that production query. An empty development database proves only SQL and route coverage;
production-scale plans need a production-sized database. Each runtime owner
supplies the same bound statement builder used by its serving read; the App
layer only composes those specs with route coverage, so an audit-only SQL
approximation is not accepted.

`uv run tracefold db audit` is the online fast path. It uses catalog/statistics
row estimates plus O(1) migration, schema, role/grant, PostgreSQL-major,
extension, and key-session-setting checks. It reports the externally declared
container image identity without pretending PostgreSQL can attest that digest;
it does not issue exact `COUNT(*)`
against every business table. `uv run tracefold db audit --deep` adds those
exact counts and is reserved for offline migration/restore evidence.

Read/return amplification uses the root result-row count for hot page queries, and each query spec owns its
budget. The two bounded News search count specs use aggregate-input amplification because their production contract deliberately returns one
aggregate row after scanning the same 168-hour AssetSearch or TextSearch predicate as the first page; the
catalog rejects that basis for every other query.

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
