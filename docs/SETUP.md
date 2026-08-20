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
`news.broker.url` at the compose RabbitMQ service, leaves
`news.opennews_strategy_ids` empty, and leaves News push disabled. Edit only the operator-owned
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
`llm.news_triage_fallback` triple), the RabbitMQ URL (`news.broker.url`), the
Feishu webhook and optional signing secret (`news.push.*`), and the PostgreSQL
role password files.

The product process is usable without optional live credentials, but affected
lanes report explicit degradation or unavailable evidence:

- absent `news.opennews_token` keeps the News Receiver idle (`ingest.connected`
  false, no incidents); when a token is configured while News is enabled,
  `news.opennews_strategy_ids` must be non-empty or configuration fails closed;
- absent or unreachable `news.broker.url` makes Workers fail startup while News
  is enabled (the broker is the News transport plane);
- an absent direct DeepSeek triple (`llm.api_key`, `llm.base_url`,
  `llm.news_triage_model`) makes Triage fall back to fail-closed rules
  (`triage_degraded_24h` grows); a degraded verdict carries no Chinese text, so
  the feed and card fall back to the original title;
- News push remains off until `news.push.enabled: true` and a supported
  `news.push.feishu_webhook_url` are both configured.

`tracefold config` reports the effective file paths, configured booleans,
`opennews_strategy_count`, broker `url_configured`, model names, and watchlist
symbols; it never prints provider tokens, the broker URL, webhook URLs,
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

news:
  enabled: true
  opennews_token: "<operator secret>"
  opennews_strategy_ids:
    - "1018" # News Score > 70
    - "1352" # Storage News
    - "1353" # Listing and Delisting Announcements
  broker:
    url: "amqp://tracefold:<rabbitmq password>@rabbitmq:5672/"
  push:
    enabled: true
    feishu_webhook_url: "<Feishu v2 webhook>"
    feishu_signing_secret:
    hourly_cap: 30
  policy:                     # decide() thresholds and switches (all optional; these are the defaults)
    min_push_magnitude: 1
    min_watchlist_magnitude: 1
    escalate_magnitude: 3
    unclear_push_min_magnitude: 2
    unclear_push_event_types: [product, listing, delisting, regulation, hack, exploit, partnership, filing]
    theme_cap_4h: 3
    storyline_throttle: true
    hourly_cap_enabled: true
    restatement_drop: true      # a restatement of a card the reader already received never pushes
    similarity_max: 0.25        # a throttled card is released when it resembles the reader's window less than this
    distinct_hard_cap_4h: 18    # flood ceiling: pushes per theme / 4 h whatever they say (>= theme_cap_4h)
    distinct_asset_cap_2h: 6    # flood ceiling: pushes per asset / 2 h
    high_priority_escalates: false  # true = the Gate's AMQP priority also earns the ⚡ header (pre-v4, #77)
  retention:
    raw_days: 30                # an Item nobody judged is storage
    judged_days: 365            # an Item behind a judged or labelled Event is the corpus every replay reads
  gate:
    suppress_low_signal: false  # true = drop ungrounded, non-macro social posts under score 70 without a model call
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
the model; exchange listing/delisting frames are admitted and judged like any
candidate), Triage is the semantic filter, and
`decide()` applies these thresholds. Change them after `tracefold news
replay-decisions` and operator labels agree; `tracefold config` prints the
effective values.

Leave the signing field empty only when unsigned delivery is intentional. Do
not commit the populated operator config. Missing or invalid delivery
configuration is fail-soft: Serve and Workers still start and every decided
delivery settles `terminal/delivery_unavailable`.

The compose stack runs `rabbitmq:4-management` with the default user
`tracefold` and password `${TRACEFOLD_RABBITMQ_PASSWORD:-tracefold}`; ports
5672/15672 bind to `127.0.0.1`. The broker URL in `config.yaml` must match.
Setting `news.enabled: false` leaves Workers with only the probe and control
children and needs no RabbitMQ.

Workers validate the configured Strategy allowlist against the provider
Strategy list at startup and expose `strategy_warnings` in `/api/news/status`;
provider-side enablement never silently edits Tracefold configuration.

Worker topology and all safety/resource budgets are code-owned. For real data,
`config.yaml` must contain only the News credentials above; the `llm` block
owns one all-or-none direct DeepSeek triple (`api_key`, `base_url`,
`news_triage_model`); there is no environment-variable credential path or
inferred URL/model. Configs written before the GMGN lane removal must drop the
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
message/consumer counts. `db audit` confirms the migration head, the eleven
`news_*` row counts, and that the schema holds exactly those tables. Source
blocks, rate limits, and missing rows surface as explicit diagnostic results,
not as fake facts.

Live-data debugging starts the same way: first run `uv run tracefold config`
and confirm `config_path` points at `~/.tracefold/config.yaml`. Report only
paths, booleans, and diagnostic command status; do not paste the API token,
model keys, provider passwords, or full config payloads into docs or chat.

After `uv run tracefold db migrate`, the database contains exactly 13 tables:
the eleven `news_*` tables plus the platform tables `alembic_version` and
`workers_runtime`.
The Alembic chain is the `20260818_0275` current-schema baseline (root; it
executes `current_schema_20260818_0275.sql` and `runtime_roles.sql`) followed
by `20260818_0276_review_49_hard_cut`, `20260818_0277_gmgn_lane_removal`, and
`20260819_0278_macro_lane_removal`. A new empty database applies all four
without replaying retired runtime tables, compatibility columns, historical
backfills, or intermediate contracts. A database stamped at an earlier
revision migrates forward with `tracefold db migrate`; 0278 drops the whole
Macro lane and `queue_terminal_events` and is irreversible (see
`OPERATIONS.md`). Stop Serve and Workers before applying it, remove the
retired `providers.macro_sources` and `llm.macro_document_analysis_*` config
keys, and start the workers only after the migration is current.

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
and set the three DSNs in `~/.tracefold/config.yaml` to a database reachable
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
