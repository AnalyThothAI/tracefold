# Setup

> **Scope.** Owns install, dev-loop, and deployment commands for both the Python service and the `web/` frontend. Runtime invariants live in `OPERATIONS.md`.

## Complete operator startup

Install Git, Make, [uv](https://docs.astral.sh/uv/), Docker with the Compose
plugin, and `curl`; start the Docker daemon. From a fresh clone, run:

```bash
make up
```

This is the canonical startup path. It preflights Git, `uv`, Docker, Compose,
`curl`, and daemon access; idempotently initializes the operator directory;
builds one application image containing the React console and Python service;
initializes PostgreSQL and its least-privilege roles on a fresh named volume;
migrates to the current Alembic head; starts Serve and Workers; and waits for
PostgreSQL, migration, both runtime readiness boundaries, and an HTML console.
Any failed boundary makes the command return non-zero and directs the operator
to `make logs`.

```bash
make status            # fail closed on infrastructure/runtime readiness
make logs              # follow PostgreSQL, migration, Serve, and Workers logs
make down              # stop containers; preserve config, passwords, and database data
```

The console is available at `http://127.0.0.1:8765/`. PostgreSQL, public HTTP,
and Workers metrics/readiness are bound to loopback by default. A second
`make up` rebuilds the shared application image and deliberately recreates only
the migration, Serve, and Workers containers so edits to the bind-mounted
operator config take effect. An already running PostgreSQL container is not
recreated; the operator files and named-volume data remain in place.

### Upgrading across a removed config key

`NewsSettings` and the other config models are `extra="forbid"`, so a key that a
release deletes does not become inert — it fails startup. When release notes
retire a key, remove it from `~/.tracefold/config.yaml` **between** `git pull`
and `make up`, or Serve and Workers refuse to start with `extra_forbidden`.

`news.opennews_strategy_ids` is retired in #126. Which Strategies feed News is
now decided in the OpenNews account, so delete the key and its list:

```bash
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path.home() / ".tracefold" / "config.yaml"
s = p.read_text()
p.with_suffix(".yaml.bak").write_text(s)
print(p.write_text(re.sub(r"^  opennews_strategy_ids:\n(?:  - .*\n)*", "", s, flags=re.M)))
EOF
uv run tracefold config   # confirm it parses before make up
```

`news.triage.deadline_seconds` is retired in #129. Remove that line from an
existing `news.triage` mapping before `make up`; keep `concurrency` and the
optional whole-chain `circuit_failures` / `circuit_open_seconds`. The Program
artifact now owns its primary and fallback route deadline, so carrying the old
key fails `extra="forbid"` rather than silently overriding the artifact.

### Initialization semantics

`make up` runs `tracefold init`. The command creates `~/.tracefold/` with mode
`0700`, `logs/` and `cache/`, one config with a locally generated API bearer
token (`ws_token`) but no external credentials, and four independent
PostgreSQL password files:

```text
postgres_password
postgres_serve_password
postgres_workers_password
postgres_migrate_password
```

The config and all password files are mode `0600`. Ordinary `tracefold init`
never overwrites an existing config, never rotates an existing password, and
repairs the required permissions on every run. `tracefold init --force`
replaces only `config.yaml` with a newly generated default; it still preserves
all existing PostgreSQL passwords. Back up intentional config changes before
using `--force`.

`tracefold init` is the sole default-config authority. There is no maintained
static example or `.env` fallback. The generated default creates a local API
token but contains no model, OpenNews, or Feishu credential, points
`news.broker.url` at the compose RabbitMQ service, and leaves News push
disabled. Edit only the operator-owned
`~/.tracefold/config.yaml` to enable live capabilities. Keep secrets out of
terminal output, docs, tests, and commits.

The generated PostgreSQL DSNs are container-network addresses. The fresh-volume
bootstrap runs only during PostgreSQL `initdb`: it creates the non-login owner
plus Serve, Workers, and migrate roles, then revokes the temporary bootstrap
login before ordinary migration. It never attempts to reinterpret or hard-cut
an unknown non-empty volume. Existing deployments must already have the
least-privilege roles; startup fails closed when the migrate role or schema
contract is not valid.

### Credential-dependent capabilities

The credentials a live deployment can hold are exactly: the OpenNews token
(`news.opennews_token`), the direct model triple (`llm.api_key`,
`llm.base_url`, `llm.news_triage_model`, plus the optional
`llm.news_reader_card`, `llm.news_triage_fallback`, and
`llm.news_reader_card_fallback` triples), the RabbitMQ URL (`news.broker.url`), the
Feishu webhook and optional signing secret (`news.push.*`), and the PostgreSQL
role password files.

The product process is usable without optional live credentials, but affected
lanes report explicit degradation or unavailable evidence:

- absent `news.opennews_token` keeps the News Receiver idle (`ingest.connected`
  false, no incidents); a configured token is the whole News source setup —
  which Strategies push is decided in the OpenNews account (#126);
- absent or unreachable `news.broker.url` makes Workers fail startup while News
  is enabled (the broker is the News transport plane);
- an absent direct model triple (`llm.api_key`, `llm.base_url`,
  `llm.news_triage_model`) leaves the semantic Program unconfigured and makes
  Triage fall back to fail-closed rules
  (`triage_degraded_24h` grows); a degraded verdict carries no Chinese text, so
  the feed and card fall back to the original title;
- News push remains off until `news.push.enabled: true` and a supported
  `news.push.feishu_webhook_url` are both configured.

`tracefold config` reports the effective file paths, configured booleans,
broker `url_configured`, model names, and watchlist symbols; it never prints
Strategy IDs/counts, provider tokens, the broker URL, webhook URLs,
signing secrets, or model keys.

`news.push.feishu_signing_secret` is optional. When present, the Adapter adds
the Feishu timestamp and signature. When absent, it sends the same compact
interactive card unsigned, without `timestamp` or `sign`; the operator owns
that reduced-authentication choice. Configuration diagnostics report only
configured booleans. Feishu delivery has no model-credential dependency; the
card header is the Triage verdict's `headline_zh` (the original title when
Triage is degraded) and the body is `why_zh` plus the code-owned facts line.

An operator configuration for live News uses the existing generated fields;
do not add another secrets file or environment variable:

```yaml
llm:
  api_key: "<operator model secret>"
  base_url: "https://api.deepseek.com/v1"
  news_triage_model: "deepseek-v4-flash"
  # Optional: omit this complete triple to run ReaderCard on the Triage endpoint.
  news_reader_card:
    api_key: "<reader model secret>"
    base_url: "https://reader.example/v1"
    model: "reader-model"
  # Optional all-or-none fallback route.
  news_triage_fallback:
    api_key: "<event fallback secret>"
    base_url: "https://event-fallback.example/v1"
    model: "event-fallback-model"
  # Optional: requires news_triage_fallback; omit to alias its endpoint explicitly.
  news_reader_card_fallback:
    api_key: "<reader fallback secret>"
    base_url: "https://reader-fallback.example/v1"
    model: "reader-fallback-model"
  # Optional cold-compiler contract. All values are required together and do
  # not affect production News calls. Rates are micro-USD per million tokens.
  news_compiler_tariff:
    tariff_id: "provider-contract-2026-08"
    input_token_overhead: 1024
    task_input_microusd_per_million: 300000
    task_output_microusd_per_million: 1200000
    reflection_input_microusd_per_million: 300000
    reflection_output_microusd_per_million: 1200000

news:
  enabled: true
  # Which Strategies feed the pipeline is set in the OpenNews account, not here.
  opennews_token: "<operator secret>"
  broker:
    url: "amqp://tracefold:<rabbitmq password>@rabbitmq:5672/"
  push:
    enabled: true
    feishu_webhook_url: "<Feishu v2 webhook>"
    feishu_signing_secret:
  policy:                     # decide() thresholds and switches (all optional; these are the defaults)
    min_push_magnitude: 1
    min_watchlist_magnitude: 1
    escalate_magnitude: 3
    unclear_push_min_magnitude: 2
    unclear_push_event_types: [product, listing, delisting, regulation, hack, exploit, partnership, filing]
    restatement_drop: true      # a restatement of a card the reader already received never pushes
    similarity_max: 0.25        # ordinary pushes above this sent-ledger similarity are same-fact duplicates
    high_priority_escalates: false  # true = the Gate's AMQP priority also earns the ⚡ header (pre-v4, #77)
    noise_veto_max_magnitude: 1     # `noise` drops on its own only at or below this magnitude (policy v8)
    noise_veto_respects_gate_priority: true   # false = a `noise` label may drop a Gate high-priority Event
    contested_push_min_magnitude: 2 # Gate high priority beats a model hold at this magnitude; 0 disables
    listing_exempt_from_duplicate: true  # exchange listing frames are duplicates only per instrument
  retention:
    raw_days: 30                # an Item nobody judged is storage
    judged_days: 365            # an Item behind a verdict or accepted review is retained as learning evidence
  gate:
    suppress_low_signal: false  # true = drop ungrounded, non-macro social posts under score 70 before Program execution
  venues:                       # instrument-universe snapshot; public catalogues, no credentials
    enabled: true
    binance: true
    hyperliquid: true
    us_reference: true          # US listed-symbol directory (#91): tells the Gate a ticker is a stock, not tradeable here
    snapshot_period_hours: 6.0
  watchlist:
    - {symbol: BTC}
    - {symbol: ETH}
    - {symbol: SOL}
    - {symbol: NVDA}
    - {symbol: TSLA}
    - {symbol: COIN}
```

`news.policy` and `news.gate` are the operator's recall/precision knobs: the
Gate admits nearly every Item (only recovery replays, law-firm templates,
and — behind `suppress_low_signal` — low-score ungrounded social posts skip
Program execution; exchange listing/delisting frames are admitted and judged like any
candidate), Triage is the semantic filter, and
`decide()` applies these thresholds. Semantic generation is the code-owned
`EventSemantics -> deterministic SemanticNormalizer -> ReaderCard.v2 ->
deterministic assembler` Program behind
`SemanticJudge.judge(TriageContext)`. A normal judgment makes two serial
provider calls; the Program artifact owns the route deadline and retry/call
budget, so `deadline_seconds` is not an operator setting. Changes are
exact-one-variable `program` or `policy` candidates:
record accepted cases with `tracefold news review`, freeze development and
future validation windows with `tracefold news learning freeze`, then run the
offline, holdout, shadow and canary gates under `tracefold news learning`.
The optional `learning compile` workflow seals accepted development, runs
bounded DSPy GEPA without DB/holdout/application credentials, and accepts only
a typed LearnedStrategy/Demo patch. It requires the complete trusted tariff
above, an exact local `--compiler-image sha256:<64 hex>`, explicit
metric/model/total-cost and resource limits, plus a seed; it cannot accept,
deploy or promote. Migration
`0292` records the initial `program_v1`
epoch; migration `0293` preserves it and starts the corrected `program_v2`
epoch; migration `0294` preserves both prior rows and starts the expert-quality
`program_v3` epoch; migration `0295` preserves v1-v3 and starts D-generation
`program_v5`. Every earlier cohort remains audit-only, and quality evidence
restarts from zero at the `program_v5`
deployment. The hard cut itself
does not prove a quality uplift; it
creates future per-Predictor feedback, demo, routing and fine-tuning leverage
at the immediate cost of the normal call count increasing from one to two.
`tracefold config` prints the effective values. Policy v7 has no 1 h/2 h/4 h
reader-count veto: every distinct fact that passes the semantic contract moves
to delivery; the sent-reader ledger remains only for same-fact suppression.

Leave the signing field empty only when unsigned delivery is intentional. Do
not commit the populated operator config. Missing or invalid delivery
configuration is fail-soft: Serve and Workers still start and every decided
delivery settles `terminal/delivery_unavailable`.

The compose stack runs `rabbitmq:4-management` with the default user
`tracefold` and password `${TRACEFOLD_RABBITMQ_PASSWORD:-tracefold}`; ports
5672/15672 bind to `127.0.0.1`. The broker URL in `config.yaml` must match.
Setting `news.enabled: false` leaves Workers with only the probe and control
children and needs no RabbitMQ.

There is no local allowlist to keep in step (#126): enabling a Strategy in the
OpenNews dashboard starts feeding the pipeline, disabling it stops, and
`/api/news/status` reports nothing about Strategies because Tracefold neither
chooses nor filters them.

Worker topology and all safety/resource budgets are code-owned. For real data,
`config.yaml` must contain only the News credentials above; the `llm` block
owns one all-or-none direct Triage triple (`api_key`, `base_url`,
`news_triage_model`) and may own one all-or-none `news_reader_card` endpoint;
an absent Reader endpoint inherits Triage. The optional fallback route has an
all-or-none `news_triage_fallback` endpoint and may add an all-or-none
`news_reader_card_fallback`; absent Reader fallback is an explicit alias of the
EventSemantics fallback endpoint. There is no environment-variable
credential path or inferred URL/model. Configs written before the GMGN lane removal must drop the
`gmgn`, `upstream`, `providers.binance`, `api.heartbeat_interval`, and
`api.replay_limit` keys, and configs written before the Analyst lane removal
(#57) must drop `news.analyst.*` and `llm.news_analyst_model`; the schema
rejects them.

The OpenNews Receiver authenticates one WSS and sends zero application
subscription frames; the server pushes the account owner's `strategy.triggered`
notifications and Tracefold publishes each accepted frame to RabbitMQ. A
disconnect, broker backpressure, or process outage creates a typed incident;
reconnect restores current WSS health and the official Strategy list/hits
endpoints perform bounded idempotent recovery (recovered Items never deliver).
Deduper, Triage, and Deliverer are broker consumers; see `docs/ARCHITECTURE.md`
and `docs/OPERATIONS.md` for the pipeline and diagnosis.

Use `uv run tracefold config` to inspect the active config path and redacted
enablement. Inspect serve through authenticated `/api/status` and workers
through its internal health/readiness/metrics surface.

Useful live-data smoke checks:

```bash
uv run tracefold config
uv run tracefold news bus-check
uv run tracefold db audit
```

The first command confirms the real config paths. `news bus-check` proves the
broker URL, declares the News topology idempotently, and prints per-queue
message/consumer counts. `db audit` confirms the migration head, every current
News table count, and that the schema holds exactly the declared table set. Source
blocks, rate limits, and missing rows surface as explicit diagnostic results,
not as fake facts.

Live-data debugging starts the same way: first run `uv run tracefold config`
and confirm `config_path` points at `~/.tracefold/config.yaml`. Report only
paths, booleans, and diagnostic command status; do not paste the API token,
model keys, provider passwords, or full config payloads into docs or chat.

The Alembic chain starts at the `20260818_0275` current-schema baseline and is
linear through `20260822_0299_news_liquidation_shadow`. A new empty database applies
the complete chain without replaying retired runtime tables. A database
stamped at an earlier revision migrates forward with `tracefold db migrate`;
all revisions are irreversible (see `OPERATIONS.md`). Stop Serve and Workers
before applying them and start Workers only after the migration is current.
An existing 0283 volume uses the backup/stop/migrate/redeploy sequence
documented in `OPERATIONS.md`; #112/#129 add no login role or password.

Retired routes return `404`; there is no compatibility alias.

The full CLI surface is documented by `uv run tracefold --help`.
Treat that output as the source of truth — do not enumerate commands
here. A snapshot lives at `generated/cli-help.md`.

## Container deployment

`make up`, `make status`, `make logs`, and `make down`
are the supported operator lifecycle. `make up` passes an existing
`GITHUB_TOKEN` into the image
build as a BuildKit secret; when unset, it uses `gh auth token` if available.
Public dependencies need neither. The token is not stored in an image layer or
application config.

Compose bind-mounts only role-appropriate files from `~/.tracefold/`. Serve
receives only its SELECT credential; Workers receives only its DML credential;
the migrate credential is absent from both steady containers. PostgreSQL data
is pinned to the `tracefold-postgres` named volume, and `make down` does not
delete it.

Fresh-volume bootstrap is an `initdb` hook, not a steady service or a generic
role-repair mechanism. Normal startup consists of PostgreSQL, RabbitMQ
(`rabbitmq:4-management`, data on the `tracefold-rabbitmq` volume, AMQP and
management ports bound to `127.0.0.1`), the one-shot migration service, and
separate Serve/Workers runtimes; Workers waits for the broker health check. `make status` returns
non-zero for a failed/missing migration, stopped or unhealthy required
container, failed Serve or Workers readiness endpoint, or missing HTML console.
It intentionally does not make business-data freshness part of readiness. Use
`make logs` for the bounded startup evidence named by a failure.

The preflight verifies `uv`, the Docker CLI, Compose plugin, `curl`, and daemon
access before a build starts. If the daemon is unavailable, start Docker
Desktop or grant this shell access to the Docker socket, then rerun `make up`.

`make deploy-image IMAGE_ID=sha256:<64 lowercase hex>` is the narrow
database-compatible image rollback/redeployment path. Run it only from the
primary checkout on `main`, and pass the full ID of an image already present in
the local Docker image store; tags, short IDs, registry digest references, and
an `IMAGE_ID` inherited only from the environment are refused. The checkout
must have no tracked/staged changes, must equal local `origin/main`, and must
have no `.env`, untracked Compose override, or untracked Alembic revision; an
unrelated untracked research artifact is not a deployment input. The target
inspects the local ID and requires the image, source, and live database Alembic
heads to match (therefore rejecting every image on a different schema head
mismatch), then validates that the target can parse the active config without
printing it. It injects that exact ID as `TRACEFOLD_IMAGE_DIGEST`, stops Serve
and Workers, and recreates migration, Serve, and Workers with `--no-build`.
Before running `make status`, it verifies every recreated container image,
Workers readiness identity, and the linked active/runtime-deployment receipt.
It never downgrades PostgreSQL. See `OPERATIONS.md` for the receipt lookup and
rollback runbook. Normal `make up` still builds and deploys the current checkout
and ignores this exact-image override.

The official PostgreSQL 18 Bookworm image preloads `pg_stat_statements` with
query IDs enabled. Use `tracefold db health`, supported audit/query-audit and
status/metrics surfaces, the SQL in `OPERATIONS.md`, and `docker compose logs`
for diagnosis. Compose has no custom PostgreSQL build, auxiliary observability
services, host log mount, or HTML-report path.

## Explicit development loops

The container workflow is the fresh-clone onboarding path. It is not equivalent
to starting only `tracefold serve`: the complete product also requires a
current PostgreSQL schema, one Workers runtime, and a built or proxied console.

For frontend-only development, keep the complete stack running and start Vite
against its loopback API:

```bash
make up
cd web
npm ci
npm run dev          # Vite console with API proxy to 127.0.0.1:8765
```

For an intentional host-process backend loop, first provision PostgreSQL roles
and set the four role DSNs in `~/.tracefold/config.yaml` to a database reachable
from the host. This is for development against an already prepared database;
it does not bootstrap a blank cluster. Then use separate terminals:

```bash
# one-time dependency/schema preparation
uv sync
cd web && npm ci && cd ..
uv run tracefold db migrate

# terminal 1
uv run tracefold serve

# terminal 2
uv run tracefold workers

# terminal 3
cd web && npm run dev
```

Developer checks remain separate from startup:

```bash
uv run pytest
uv run ruff check .
uv run python -m compileall src tests
cd web && npm run typecheck && npm run lint
```

Other frontend commands are:

```bash
cd web
npm run build        # production bundle
npm run preview      # serve the build locally
```

See `FRONTEND.md` for architecture and component conventions.
