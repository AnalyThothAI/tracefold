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
Worker topology and all safety/resource budgets are code-owned. Rerun
`uv run tracefold init --force` only when you
intentionally want to replace these operator-owned files.

For real data, edit the operator-owned files in `~/.tracefold/`
instead of adding repository-local `.env` files or editing generated examples.
`config.yaml` must point at the live PostgreSQL store and contain the provider
credentials/endpoints needed by the enabled data lanes, including GMGN OpenAPI
for exact token profiles and OKX provider settings for discovery, market data,
or DEX WebSocket lanes when those paths are enabled. Keep secrets out of
terminal output, docs, tests, and commits.
The `llm` block owns operator credentials: `api_key` plus `base_url` for the
current OpenAI-compatible provider, and optional `openrouter_api_key` and
`groq_api_key` for News provider fallback. Worker timeouts, token budgets,
cadence, and resource limits are code-owned; there is no environment-variable
credential path.
Set `news.opennews_token` to enable the production News WSS and REST recovery
lane. `tracefold config` reports only whether it is configured; it never prints
the token. When it is absent, News reports `opennews_token_missing`.
Set `news.push.enabled: true` only after adding a newly rotated Feishu webhook
URL and signing secret under `news.push`; both are required and diagnostics
report configured booleans only. The push translator reuses the existing
DeepSeek-compatible `llm.api_key` and `llm.base_url` and fixes the translation
model to `deepseek-v4-flash`; no second model credential is configured.
If that credential is absent, title translation is marked unavailable without
a model call and the frozen card still carries the original headline.

News correctness does not depend on the model. The OpenNews WSS receiver, REST
recovery, and publisher share one acquisition module and sole NewsItem writer.
A healthy WSS connection never polls REST periodically; one bounded REST page
is requested only after initial connection, reconnect, or queue overflow, with
a persisted five-minute minimum interval between attempts.
A fixed 60-second writer owns the complete current 96-hour Story projection,
and the single-capacity native-state model arbiter owns World Brief and the
title-only push translator. Push is a separate News-owned delivery state
machine: initial enablement suppresses the current eligible baseline, later
strict score-greater-than-70 crossings freeze one highest-scored Item and send
one signed Feishu card with durable at-least-once retries. It is not a generic
Notifications product or item-level analysis path.
Changing cadence does not repair source admission, Story identity, or Brief
fingerprint errors.

Use `uv run tracefold config` to inspect the active config path and redacted
enablement. Inspect serve through authenticated `/api/status` and workers
through its internal health/readiness/metrics surface.

Useful live-data smoke checks:

```bash
uv run tracefold config
uv run tracefold ops refresh-asset-profiles --limit 5
uv run tracefold ops mirror-token-images --limit 50
uv run tracefold asset-flow --window 1h --limit 20
```

The first command confirms the real config paths. The profile refresh command
exercises the GMGN exact-token profile path that feeds `asset_profiles.logo_url`
for DEX token icon source URLs. Profile publication admits exact profile and
evidence logo sources into `token_image_source_dirty_targets`; the mirror
command processes the durable image target without a retired rebuild/repair
alias. Refreshing a profile can re-admit an image target whose prior source is
stale. The mirror command copies eligible provider images into
`~/.tracefold/cache/token-images`; fenced Profile publication projects
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
the bounded Yahoo Chart JSON endpoint for best-effort five-minute
ETFs, BTC, VIX, and major futures plus five-year daily continuous-futures
proxies. Nasdaq public ETF history supplies the separate five-year daily ETF
lane.
`providers.macro_sources` can disable the entire family or FRED, Cboe, CFTC,
Nasdaq daily, and Yahoo Chart independently and owns only request timeout/user-agent transport
settings. It does not own dataset membership, formulas, freshness, or
scheduling. Capabilities that require unavailable paid data are not part of the
current product contract and are not filled with a fake proxy.

Five explicit acquisition due loops own distinct clocks:
`macro_intraday_market`, `macro_settlements`, `macro_economic_releases`,
`macro_official_state`, and `macro_official_documents`. The two backfill
commands synchronously process their explicit bounded targets and then exit;
backfill is not a steady loop or automatic clock.
The EDF projection coordinator rebuilds only affected module-local current
rows from the static dataset/calculation/module dependency graph. The serial
native-state model arbiter writes only immutable, exact-evidence-bound
FOMC/speech analyses.

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
No optional backfill state blocks Coverage, Current Health, or projection;
History Depth remains explicit. Credit and WTI retain longer reliable public
history where one bounded source response makes that history cheap and
material to percentile context.

Rerun the explicit backfill command after a retry deadline until the desired
targets are `current`; incomplete targets remain visible without blocking the
workbench. Open or failed document analyses remain explicit module-local gaps.

A good `macro status` reports bounded acquisition target counts/statuses, all
six current module rows with health/history/fact-cutoff clocks, and Fed
document-analysis job counts. Diagnose a missing value by concept ID and source
role through its target current state/cursor, fact family, and three module
quality axes. A public-source timeout, weekend settlement lag, or delayed Yahoo
proxy is a visible quality state; it is not a frontend defect.

After `uv run tracefold db migrate`, the database contains
typed Market/Macro fact tables, acquisition targets, module frontiers, six
current module rows, official documents, document-analysis jobs, and immutable
document analyses.
`20260728_0210` remains the compact current-schema baseline and
the generated Alembic head is the required hard-cut version. A new empty database applies the
baseline and head without replaying retired runtime tables, compatibility
columns, historical backfills, or intermediate contracts. A database stamped
at `20260728_0210` migrates forward once; paid-data placeholders are dropped
rather than archived. Migrations `20260801_0235` and `20260801_0236`
irreversibly delete retired News acquisition history and Macro publication,
per-attempt, and stored intermediate history while preserving current items,
facts, targets, document analyses, and module rows.
Migration `20260801_0237` adds the persisted OpenNews recovery boundary and
`20260801_0238` adds the News push baseline/delivery ledger. Push remains
disabled after migration until the signed Feishu settings are explicitly
enabled; the first enabled reconcile records a no-backfill baseline.
Enable the Macro workers only after the migration is current.

The overview and six typed module reads are persisted-only and never trigger a
provider, model, target advance, projection rebuild, or write. Retired Macro
routes return `404`; there is no compatibility alias.

The full CLI surface is documented by `uv run tracefold --help`.
Treat that output as the source of truth — do not enumerate commands
here. A snapshot lives at `generated/cli-help.md`.

## Docker Compose

### Authorized Issue #33 in-place hard cut

Normal `migrate` uses `tracefold_migrate`, so an existing deployment must
provision the new roles through the explicit maintenance profile first. For
the current operator-authorized Issue #33 cut, stop the old Workers before
entering this maintenance path. The cut runs in place without a backup,
snapshot, or restore drill and fixes forward on failure.

```bash
uv run tracefold init
docker compose up -d postgres
docker compose --profile maintenance run --rm cutover \
  tracefold db hard-cut \
  --bootstrap-dsn postgresql://tracefold_app@postgres:5432/tracefold \
  --bootstrap-password-file /run/secrets/postgres_password \
  --execute
docker compose up -d
```

The hard-cut command acquires the exclusive maintenance gate, refuses visible
Tracefold runtime sessions, migrates to head, provisions role passwords,
rebuilds and audits Radar/News/Macro/current Profile, and only then changes
`tracefold_app` to `NOLOGIN`. A failure remains in maintenance for fix-forward
on the current database; there is no restore path for this authorized cut.
Because the legacy bootstrap superuser login is deliberately revoked,
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

Normal Compose starts PostgreSQL, the one-shot migration service, and separate
serve/workers runtimes. Production News is OpenNews-only.

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
