# Setup

> **Scope.** Owns install, dev-loop, and deployment commands for both the Python service and the `web/` frontend. Runtime invariants live in `OPERATIONS.md`.

## Python service

```bash
uv sync
uv run pytest
uv run ruff check .
uv run python -m compileall src tests
```

Bring up the service:

```bash
uv run tracefold init      # create config.yaml + workers.yaml
uv run tracefold serve     # run collector + API in one ASGI worker
uv run tracefold db migrate
```

`init` writes `~/.tracefold/config.yaml` for application and
provider settings, plus `~/.tracefold/workers.yaml` for worker
runtime knobs. Existing deployments from before the worker-runtime hard
cut must create `workers.yaml` before starting the service; rerun
`uv run tracefold init --force` only when you intentionally
want to rewrite the default config files.

For real data, edit the operator-owned files in `~/.tracefold/`
instead of adding repository-local `.env` files or editing generated examples.
`config.yaml` must point at the live PostgreSQL store and contain the provider
credentials/endpoints needed by the enabled data lanes, including GMGN OpenAPI
for exact token profiles and OKX provider settings for discovery, market data,
or DEX WebSocket lanes when those workers are enabled. Keep secrets out of
terminal output, docs, tests, and commits.
The `llm` block owns operator credentials: `api_key` plus `base_url` for the
current OpenAI-compatible provider, and optional `openrouter_api_key` and
`groq_api_key` for News provider fallback. Endpoints, model names, request
timeouts, token budgets, cadence, and enabled state live in the owning worker
section of `workers.yaml`; there is no environment-variable credential path.
An enabled AI worker with no configured provider reports an explicit
unavailable state and makes no model call.

News correctness does not depend on the model. The production defaults are a
120-second deterministic `news_pipeline` interval and a 600-second
`news_world_brief` interval with one 60-second total provider budget. There is
no item-level AI worker. Source refresh intervals remain source-specific in
`config.yaml`.
Changing cadence does not repair source admission, Story identity, or Brief
fingerprint errors.

Use `uv run tracefold config` to inspect both config paths and the effective
worker settings. Inspect the running process through authenticated
`/api/status`; a new CLI process cannot report the state of an already-running
scheduler.

Useful live-data smoke checks:

```bash
uv run tracefold config
uv run tracefold ops refresh-asset-profiles --limit 5
uv run tracefold ops rebuild-token-profiles --limit 500
uv run tracefold ops repair-token-profile-images --limit 500
uv run tracefold ops mirror-token-images --limit 50
uv run tracefold ops rebuild-token-profiles --limit 500
uv run tracefold asset-flow --window 1h --scope all --limit 20
```

The first command confirms the real config paths. The profile refresh command
exercises the GMGN exact-token profile lane that feeds `asset_profiles.logo_url`
for DEX token icon source URLs. `rebuild-token-profiles` admits exact profile
and evidence logo sources into `token_image_source_dirty_targets`; the repair
command re-enqueues already-current rows whose icons were stuck before source
admission existed. The mirror command copies eligible provider images into
`~/.tracefold/cache/token-images`, and the final rebuild projects
`token_profile_current.logo_url` to local `/api/token-images/{image_id}` paths
or `NULL`. Provider blocks, rate limits, unsupported image types, and missing
mirror rows should surface as explicit diagnostic results or fallback marks,
not as fake public profile facts.

Macro live-data debugging starts the same way: first run
`uv run tracefold config` and confirm `config_path` /
`workers_config_path` point at `~/.tracefold/`. Report only paths,
booleans, and diagnostic command status; do not paste WebSocket tokens, API
keys, provider passwords, or full config payloads into docs or chat.

Macro acquisition uses free, keyless sources first: FRED public CSV and BLS
Public Data API for official series/releases, Federal Reserve RSS for official
policy documents, CFTC TFF Futures Only for rates/credit/cross-asset positioning,
Cboe CFE settlement files for VIX futures, Binance public spot klines for BTC,
and explicitly labelled Nasdaq public previous-close history for the remaining
cross-asset prices.
`providers.macro_sources` can disable the entire family or FRED, Cboe, CFTC,
and Nasdaq public history independently and owns only request timeout/user-agent transport
settings. It does not own dataset membership, formulas, freshness, or
scheduling. Licensed CME rates futures prices/curves remain an explicit
`unavailable` dataset until an authorized provider is configured; the service
never fills that gap with a fake proxy.

Four automatic acquisition workers own distinct clocks:
`macro_settlements`, `macro_economic_releases`, `macro_official_state`, and
`macro_official_documents`. `macro_backfill` is
disabled by default and processes only operator-created bounded targets.
`macro_projection` rebuilds six stable current rows from persisted facts;
`macro_document_analysis` writes immutable evidence-bound FOMC/speech analyses
and is disabled by default;
`macro_judgment` seals the 08:50 New York Evidence Pack and daily decision.

For an operator-triggered repair of one bounded dataset window:

```bash
uv run tracefold macro backfill --dataset fred.dgs10 --start YYYY-MM-DD --end YYYY-MM-DD
uv run tracefold macro status
```

For the one code-owned professional history policy required by the v2 hard cut:

```bash
uv run tracefold macro backfill-professional
uv run tracefold macro status
```

The readiness-critical Treasury, FOMC, speech, ETF, and BTC window is the most
recent five years. Older deep history is optional enrichment and does not block
Coverage, projection, or Daily Judgment. Credit and WTI retain longer reliable
public history where one bounded source response makes that history cheap and
material to percentile context.

Enable `macro_backfill` until every readiness-required professional target is
`current`; optional deep-history targets may continue without blocking the
workbench. Then
enable `macro_document_analysis` until its durable queue has no open or failed
jobs. Projection and judgment intentionally remain blocked while required
history or document analysis is incomplete.

A good macro status reports Alembic `20260727_0206`, bounded acquisition target
states, recent source receipts, all six module rows, and the latest daily
judgment/research states. Diagnose a missing value by dataset ID through its
target, last receipt, fact family, and module gap. A public-source timeout,
weekend settlement lag, unavailable licensed CME dataset, or delayed Nasdaq
public history is a visible quality state; it is not a frontend defect.

After `uv run tracefold db migrate`, the database contains
typed Market/Macro fact tables, acquisition targets/receipts, six module rows,
immutable Evidence Packs and daily judgments, `macro_research_runs`, immutable
`macro_research_publications`, and the LangGraph PostgreSQL checkpoint tables.
Migration `20260727_0200` irreversibly drops legacy Macro tables and data
without migration or backup. Runtime startup does not create or upgrade those
tables. Migration `20260727_0201` removes the unusable Stooq lane and invalidates
all derived Macro state before the Nasdaq/Cboe source correction rebuild.
Migration `20260727_0202` removes still-open Binance daily candles and the two
FRED liquidity series ingested with incorrect units, resets those targets, and
invalidates derived Macro state before a clean rebuild.
Migration `20260727_0203` removes the redundant intraday clock and rebuilds the
Binance dataset as a UTC daily close under the settlement worker.
Migration `20260727_0206` archives v1 Macro publication tables, hard-cuts the
active lane to v2 Evidence Pack/judgment/research schemas, adds Fed role and
immutable document-analysis storage, and requires six module-specific payloads.
Enable the Macro workers only after the migration is current.

A healthy completed-session research run transitions
`pending -> running -> published`; transient model/tool failures transition to
`retryable`, and exhausted attempts to `failed`. The overview, six typed module
reads, and `/api/macro/research` are persisted-only and never trigger a
provider, model, target advance, projection rebuild, or write.

The enabled worker creates per-scope native DeepAgents calculation directories
under `~/.tracefold/macro-agent-workspaces/`. Docker Compose already mounts the
operator app home, so `execute` scratch files survive app-container restarts;
checkpoint-backed files and large tool results remain in PostgreSQL.

The full CLI surface is documented by `uv run tracefold --help`.
Treat that output as the source of truth — do not enumerate commands
here. A snapshot lives at `generated/cli-help.md`.

## Docker Compose

```bash
export GITHUB_TOKEN="$(gh auth token)"  # required when GitHub dependencies are private
make docker-check
make docker-up
make docker-status
make docker-logs
make docker-down
```

Bind-mounts host `~/.tracefold/` into the container, including
both `config.yaml` and `workers.yaml`; PostgreSQL data is pinned to the
`tracefold-postgres` named volume.

`make docker-check` verifies the Docker CLI, the Compose plugin, and daemon
access before the build starts. If it reports that the Docker daemon is not
reachable, start Docker Desktop or grant the current terminal access to the
Docker socket before rerunning `make docker-up`.

PostgreSQL observability is part of the compose runtime. The PostgreSQL image
loads `pg_stat_statements`, PoWA, `pg_stat_kcache`, `pg_qualstats`, and
`pg_wait_sampling`; slow logs are mounted under
`~/.tracefold/postgres-logs`.

```bash
./scripts/pgbadger_report.sh
./scripts/powa_configure.sh
```

`pgbadger_report.sh` writes
`~/.tracefold/reports/pgbadger/pgbadger-latest.html`.
`powa_configure.sh` configures the local PoWA GUCs and server row with bounded
retention, takes snapshots, and prints only non-secret server metadata plus
current/history row counts.

## Frontend (`web/`)

```bash
cd web
npm install
npm run dev          # vite dev server with API proxy
npm run build        # production bundle
npm run preview      # serve the build locally
```

See `FRONTEND.md` for architecture and component conventions.
