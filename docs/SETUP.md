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
The `llm` block contains only `api_key` and `base_url`. It supplies transport
credentials to the optional `macro_research` and `news_ai_publish` workers.
Each worker's model, request timeout, token budget, statement timeout,
lease/retry policy, attempt limit, cadence, and enabled state lives only in its
own `workers.yaml` section; there is no third model-policy source. The request
timeout bounds one provider transport call. If an AI worker is enabled without
both credential fields, it reports `unavailable: llm_not_configured` and makes
no model call.

News correctness does not depend on the model. The production defaults are a
5-second `news_story_project` interval, a 30-second `news_brief_plan` interval,
120,000ms ordinary debounce, 10,000ms verified-critical debounce, and a
60-second `news_ai_publish` interval. Source refresh intervals remain
source-specific in `config.yaml`. Change these only with the News SLOs in
`OPERATIONS.md`; decreasing debounce does not repair identity or evidence
quality.

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

Macro freshness is normally owned by the `macro_sync` worker. Docker/runtime
always invokes the installed `macrodata` package entrypoint with the current
Python interpreter. It does not probe `PATH`, inspect console-script shebangs,
run `uv run macrodata`, or depend on a host-local macrodata checkout.
The worker reads the formal `workers.macro_sync.bundle_names` list; the default
set is `macro-core`, `macro-calendar-core`, `treasury-auction-core`, and
`fed-text-core`.
Provide a FRED API key either as `providers.macrodata.fred_api_key` in the
operator-owned `~/.tracefold/config.yaml`, or through the environment /
deployment secret manager named by `providers.macrodata.fred_api_key_env`
(default `FINANCE_FRED_API_KEY`). `uv run tracefold config` and macro sync
diagnostics report only whether a key is configured, never the key value. Tune
`workers.macro_sync.macrodata_timeout_seconds` to bound the provider subprocess;
a stuck macrodata child process is killed at that boundary and recorded as a
source-health failure.

For an operator-triggered repair of one bounded window, use the same sync
service as the worker:

```bash
uv run tracefold macro sync --bundle macro-core --start YYYY-MM-DD --end YYYY-MM-DD
uv run tracefold macro status
```

A good macro status has a recent `latest_sync_run`,
`facts_max_observed_at` near the expected upstream date, no expired running
sync window, and a bounded due/retry backlog. The `macrodata_cli` block must
show the expected package version and
`required_bundle_series_available=true`; otherwise the runtime is using an old
packaged `macrodata-cli` bundle and sync cannot import all required source
series. The installed macrodata runtime must also expose history commands for
the configured event bundles before the default worker cadence can refresh
official-event evidence. FRED public CSV timeouts or a missing optional FRED
API key are source-health gaps; they are not frontend defects.

After `uv run tracefold db migrate`, the database contains
`macro_research_runs`, immutable `macro_research_publications`, and the
LangGraph PostgreSQL checkpoint tables. Runtime startup does not create or
upgrade those tables. Enable `workers.macro_research` only after the migration
is current. A healthy completed-session run transitions
`pending -> running -> published`; transient model/tool failures transition to
`retryable`, and exhausted attempts to `failed`. The authenticated
`GET /api/macro/evidence/{view_id}` live read queries bounded persisted
`macro_observations`; `/macro` and its six detail routes never trigger a
provider, model, or write. `GET /api/macro/research` and `/macro/research`
remain persisted-only and never trigger the model.

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
