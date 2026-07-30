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
uv run tracefold init      # create config.yaml and four role password files
uv run tracefold serve     # read-only HTTP/static/WebSocket runtime
uv run tracefold workers   # ingestion/projection/provider/model runtime
```

`init` writes `~/.tracefold/config.yaml` plus separate bootstrap, serve,
workers, and migrate PostgreSQL password files with mode `0600`.
`workers.yaml` no longer exists. Worker topology and all safety/resource
budgets are code-owned. Rerun `uv run tracefold init --force` only when you
intentionally want to replace these operator-owned files.

For real data, edit the operator-owned files in `~/.tracefold/`
instead of adding repository-local `.env` files or editing generated examples.
`config.yaml` must point at the live PostgreSQL store and contain the provider
credentials/endpoints needed by the enabled data lanes, including GMGN OpenAPI
for exact token profiles and OKX provider settings for discovery, market data,
or DEX WebSocket lanes when those workers are enabled. Keep secrets out of
terminal output, docs, tests, and commits.
The `llm` block owns operator credentials: `api_key` plus `base_url` for the
current OpenAI-compatible provider, and optional `openrouter_api_key` and
`groq_api_key` for News provider fallback. Worker timeouts, token budgets,
cadence, and resource limits are code-owned; there is no environment-variable
credential path.
An enabled AI worker with no configured provider reports an explicit
unavailable state and makes no model call.

News correctness does not depend on the model. `news_ingest` owns source
claim/fetch/persist, the EDF coordinator owns deterministic Story projection,
and the single-capacity model coordinator owns World Brief. There is no
item-level AI worker.
Changing cadence does not repair source admission, Story identity, or Brief
fingerprint errors.

Use `uv run tracefold config` to inspect the active config path and redacted
enablement. Inspect serve through authenticated `/api/status` and workers
through its internal health/readiness/metrics surface.

Useful live-data smoke checks:

```bash
uv run tracefold config
uv run tracefold ops refresh-asset-profiles --limit 5
uv run tracefold ops rebuild-token-profiles --limit 500
uv run tracefold ops repair-token-profile-images --limit 500
uv run tracefold ops mirror-token-images --limit 50
uv run tracefold ops rebuild-token-profiles --limit 500
uv run tracefold asset-flow --window 1h --limit 20
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
`uv run tracefold config` and confirm `config_path` points at
`~/.tracefold/config.yaml`. Report only paths,
booleans, and diagnostic command status; do not paste WebSocket tokens, API
keys, provider passwords, or full config payloads into docs or chat.

Macro acquisition uses free, keyless sources first: Treasury XML for the
current nominal/real curve; FRED public CSV for official history; BLS Public
Data API and BEA public current-release pages for scheduled release facts;
Federal Reserve official pages for policy documents; CFTC TFF Futures Only for
rates/credit/cross-asset positioning; Cboe CFE settlement files for VIX
futures; Binance public spot klines for the completed UTC BTC settlement; and
the pinned `yfinance` wrapper over Yahoo Finance for best-effort five-minute
ETFs, BTC, VIX, and major futures plus five-year daily continuous-futures
proxies. Nasdaq public ETF history supplies the separate five-year daily ETF
lane.
`providers.macro_sources` can disable the entire family or FRED, Cboe, CFTC,
Nasdaq daily, and yfinance independently and owns only request timeout/user-agent transport
settings. It does not own dataset membership, formulas, freshness, or
scheduling. Capabilities that require unavailable paid data are not part of the
current product contract and are not filled with a fake proxy.

Five automatic acquisition workers own distinct clocks:
`macro_intraday_market`, `macro_settlements`, `macro_economic_releases`,
`macro_official_state`, and
`macro_official_documents`. `macro_backfill` exists only in the maintenance
runtime and processes operator-created bounded targets.
The EDF projection coordinator rebuilds only affected module-local current
rows from the static dataset/calculation/module dependency graph. The model
coordinator writes immutable evidence-bound FOMC/speech analyses and seals the
08:50 New York Evidence Pack, compiles one bounded
`MacroResearchInputV1`, performs exactly one native structured model invocation
per durable attempt through the Thin DeepAgent profile, and publishes at most
one immutable v2 Thesis for the session.

For an operator-triggered repair of one bounded dataset window:

```bash
uv run tracefold macro backfill --dataset fred.dgs10 --start YYYY-MM-DD --end YYYY-MM-DD
uv run tracefold macro status
```

For the code-owned five-year history policy:

```bash
uv run tracefold macro backfill-professional
uv run tracefold macro status
```

Treasury, FOMC, speech, completed BTC settlement, fixed ETF Nasdaq daily, and
Yahoo continuous-futures daily datasets use the most recent five years.
Yahoo intraday acquisition requests one month of five-minute bars initially
and the rolling day thereafter. Older deep history is optional enrichment.
No optional backfill state blocks Coverage, Current Health, projection,
Evidence Pack, or Thesis; History Depth remains explicit and affected
conclusions can become `no_call`. Credit and WTI retain longer reliable public history where one
bounded source response makes that history cheap and material to percentile
context.

Enable `macro_backfill` until the desired five-year targets are `current`;
incomplete targets remain visible without blocking the workbench. Then
enable `macro_document_analysis` until its durable queue has no open or failed
jobs. Open or failed analyses remain explicit gaps and do not suppress a daily
publication.

A good macro status reports the generated Alembic head, bounded acquisition target
states, recent source and reconciliation receipts, all six module rows, and the
current-session v2 Thesis/Live Delta/Outcome Replay states. It also reports the
read-only frozen-corpus readiness: nine distinct real sessions remain the
long-horizon comparison target, while `blocks_deployment=false` makes clear that
collection is not a schema-migration gate. Every available real v3 Evidence Pack
must still compile into the bounded current Research Input before deployment.
Historical v1 rows never satisfy current status. Diagnose a missing value by
concept ID and source role through its target, receipt, fact family, and three
module quality axes. A public-source timeout, weekend settlement lag, or
delayed Yahoo proxy is a visible quality state; it is not a frontend defect.

After `uv run tracefold db migrate`, the database contains
typed Market/Macro fact tables, acquisition targets/receipts, six module rows,
immutable Evidence Packs, Thesis runs/reviews/publications, Live Delta, Outcome
Replay, append-only Research Inputs, and the retained historical review/checkpoint
tables.
`20260728_0210` remains the compact current-schema baseline and
the generated Alembic head is the required hard-cut version. A new empty database applies the
baseline and head without replaying retired runtime tables, compatibility
columns, historical backfills, or intermediate contracts. A database stamped
at `20260728_0210` migrates forward once; retired Judgment/Research tables and
paid-data placeholders are dropped rather than archived.
Enable the Macro workers only after the migration is current.

A healthy Thesis run transitions
`pending -> running -> published`; transient provider/model failures transition to
`retryable`, exhausted attempts to `failed`, invalid models to `config_error`,
and one of the four publication gates may produce terminal `not_published`.
Unsupported
configuration reaches `config_error` with `attempt_count=0`. The overview, six typed module
reads, and `/api/macro/research` are persisted-only and never trigger a
provider, model, target advance, projection rebuild, or write.

The Thin profile has no Reviewer invocation, tool loop, subagent, workspace, or
checkpoint write. Historical v1 Reviewer rows remain immutable audit records;
the migration removes only retired Macro Thesis checkpoint control rows.

The full CLI surface is documented by `uv run tracefold --help`.
Treat that output as the source of truth — do not enumerate commands
here. A snapshot lives at `generated/cli-help.md`.

## Docker Compose

### First Issue #32 hard cut

Normal `migrate` uses `tracefold_migrate`, so an existing deployment must
provision the new roles through the explicit maintenance profile first.
Do not run these steps until the old combined runtime is stopped and a
recoverable PostgreSQL volume snapshot has been verified.

```bash
uv run tracefold init
docker compose up -d postgres
docker compose --profile maintenance run --rm cutover \
  tracefold db hard-cut \
  --bootstrap-dsn postgresql://tracefold_app@postgres:5432/tracefold \
  --bootstrap-password-file /run/secrets/postgres_password \
  --snapshot-confirmed \
  --execute
docker compose up -d
```

An operator who explicitly accepts that there is no automatic rollback point
may use `--snapshot-waived` instead of `--snapshot-confirmed`; the result
records that irreversible waiver. The hard-cut command acquires the exclusive maintenance gate, refuses visible
Tracefold runtime sessions, migrates to head, provisions role passwords,
rebuilds and audits Radar/News/Macro/current Profile, and only then changes
`tracefold_app` to `NOLOGIN`. It never takes the snapshot itself. A failure
before finalization remains in maintenance for fix-forward or snapshot
restore. Because the legacy bootstrap superuser login is deliberately revoked,
cluster-owner recovery afterward requires local/container PostgreSQL
administration rather than the old network credential.

```bash
export GITHUB_TOKEN="$(gh auth token)"  # required when GitHub dependencies are private
make docker-check
make docker-up
make docker-status
make docker-logs
make docker-down
```

Bind-mounts only the role-appropriate files from host `~/.tracefold/`.
Serve receives only its SELECT credential; workers receives only its DML
credential; the migrate credential is absent from both steady containers.
PostgreSQL data is pinned to the `tracefold-postgres` named volume.

Normal Compose starts PostgreSQL, the one-shot migration service, separate
serve/workers runtimes, and RSSHub. PostgreSQL and RSSHub use
version-and-digest-pinned upstream images. RSSHub has no host port, uses memory
cache only, and is not an application-startup dependency.

For the free WallStEngine transport, an operator may create
`~/.tracefold/rsshub.env` with RSSHub's `TWITTER_AUTH_TOKEN` value and restrict
the file to the operator account. The file is optional, sidecar-only, and must
not be copied into `config.yaml`, repository files, templates, or shell
wrappers. When it is absent, Compose still starts and News records
WallStEngine as an ordinary degraded source.

`make docker-check` verifies the Docker CLI, the Compose plugin, and daemon
access before the build starts. If it reports that the Docker daemon is not
reachable, start Docker Desktop or grant the current terminal access to the
Docker socket before rerunning `make docker-up`.

The official PostgreSQL 18 Bookworm image preloads `pg_stat_statements` with
query IDs enabled. Use `tracefold db health`, supported audit/query-audit and
status/metrics surfaces, the SQL in `OPERATIONS.md`, and `docker compose logs`
for diagnosis. Compose has no custom PostgreSQL build, auxiliary observability
services, host log mount, or HTML-report path.

## Frontend (`web/`)

```bash
cd web
npm install
npm run dev          # vite dev server with API proxy
npm run build        # production bundle
npm run preview      # serve the build locally
```

See `FRONTEND.md` for architecture and component conventions.
