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
make macro-acceptance  # verify all six Macro product reads after deployment
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
`0700`, `logs/` and `cache/`, one config with a locally generated WebSocket
token but no external credentials, and four independent PostgreSQL password
files:

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
static example or `.env` fallback. The generated default creates a local
WebSocket token but contains no external provider, model, OpenNews, or Feishu
credential, points `news.broker.url` at the compose RabbitMQ service, leaves
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

The product process is usable without optional live credentials, but affected
lanes report explicit degradation or unavailable evidence:

- absent `news.opennews_token` keeps the News Receiver idle (`ingest.connected`
  false, no incidents); when a token is configured while News is enabled,
  `news.opennews_strategy_ids` must be non-empty or configuration fails closed;
- absent or unreachable `news.broker.url` makes Workers fail startup while News
  is enabled (the broker is the News transport plane); disable News with
  `news.enabled: false` to run Market/Macro without RabbitMQ;
- absent GMGN/OKX credentials leave their authenticated profile, discovery, or
  market lanes unavailable, while configured keyless sources keep their own
  independent behavior;
- an absent direct DeepSeek triple (`llm.api_key`, `llm.base_url`,
  `llm.news_triage_model`) makes Triage fall back to fail-closed rules
  (`triage_degraded_24h` grows) and disables the Analyst;
- absent both DeepL keys and the direct DeepSeek triple makes card titles use
  the original text;
- News push remains off until `news.push.enabled: true` and a supported
  `news.push.feishu_webhook_url` are both configured.

`tracefold config` reports the effective file paths, configured booleans,
`opennews_strategy_count`, broker `url_configured`, model names, watchlist
symbols, and the DeepL key count; it never prints provider tokens, the broker
URL, webhook URLs, signing secrets, or model keys.

`news.push.feishu_signing_secret` is optional. When present, the Adapter adds
the Feishu timestamp and signature. When absent, it sends the same compact
interactive card unsigned, without `timestamp` or `sign`; the operator owns
that reduced-authentication choice. Configuration diagnostics report only
configured booleans. Feishu delivery has no model-credential dependency.
Title translation uses ordered DeepL keys first and the direct DeepSeek triple
second, only for Events that will be delivered; no provider call is retried.

An operator configuration for live News uses the existing generated fields;
do not add another secrets file or environment variable:

```yaml
llm:
  api_key: "<operator model secret>"
  base_url: "https://api.deepseek.com/v1"
  news_triage_model: "deepseek-v4-flash"
  news_analyst_model: "deepseek-v4-pro"

news:
  enabled: true
  opennews_token: "<operator secret>"
  opennews_strategy_ids:
    - "1018" # News Score > 70
    - "1352" # Storage News
    - "1353" # Listing and Delisting Announcements
  broker:
    url: "amqp://tracefold:<rabbitmq password>@rabbitmq:5672/"
  translation:
    deepl_api_keys:
      - "<DeepL key 1>"
  push:
    enabled: true
    feishu_webhook_url: "<Feishu v2 webhook>"
    feishu_signing_secret:
    hourly_cap: 20
  watchlist:
    - {symbol: BTC}
    - {symbol: NVDA}
```

Leave the signing field empty only when unsigned delivery is intentional. Do
not commit the populated operator config. Missing or invalid delivery
configuration is fail-soft: Serve and Workers still start and every decided
delivery settles `terminal/delivery_unavailable`.

The compose stack runs `rabbitmq:4-management` with the default user
`tracefold` and password `${TRACEFOLD_RABBITMQ_PASSWORD:-tracefold}`; ports
5672/15672 bind to `127.0.0.1`. The broker URL in `config.yaml` must match.
Set `news.enabled: false` to run Market/Macro without RabbitMQ.

Workers validate the configured Strategy allowlist against the provider
Strategy list at startup and expose `strategy_warnings` in `/api/news/status`;
provider-side enablement never silently edits Tracefold configuration.

Worker topology and all safety/resource budgets are code-owned. For real data,
`config.yaml` must contain the credentials/endpoints needed by each enabled
lane, including GMGN OpenAPI for exact token profiles and OKX provider settings
for discovery, market data, or DEX WebSocket paths. The `llm` block owns one
all-or-none direct DeepSeek triple (`api_key`, `base_url`,
`news_triage_model`) plus optional `news_analyst_model` and `groq_api_key`;
there is no environment-variable credential path or inferred URL/model.

The OpenNews Receiver authenticates one WSS and sends zero application
subscription frames; the server pushes the account owner's `strategy.triggered`
notifications and Tracefold publishes each accepted frame to RabbitMQ. A
disconnect, broker backpressure, or process outage creates a typed incident;
reconnect restores current WSS health and the official Strategy list/hits
endpoints perform bounded idempotent recovery (recovered Items never deliver).
Deduper, Triage, Analyst, Translator, and Deliverer are broker consumers; see
`docs/ARCHITECTURE.md` and `docs/OPERATIONS.md` for the pipeline and diagnosis.

Use `uv run tracefold config` to inspect the active config path and redacted
enablement. Inspect serve through authenticated `/api/status` and workers
through its internal health/readiness/metrics surface.

Useful live-data smoke checks:

```bash
uv run tracefold config
uv run tracefold ops refresh-asset-profiles --limit 5
uv run tracefold ops mirror-token-images --limit 50
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
Nasdaq daily, and Yahoo Chart independently and owns only provider admission
and user-agent identity. Request timeout and bounded-operation budgets are
code-owned. The retired `request_timeout_seconds` key is rejected; remove it
from an existing operator config before upgrading. Provider config does not own
dataset membership, formulas, freshness, or scheduling. Capabilities that
require unavailable paid data are not part of the current product contract and
are not filled with a fake proxy.

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
for ETFs and VIX. High-session futures and continuous BTC request five days
initially so one batch remains within the code-owned 5,000-fact budget. Every
Yahoo intraday dataset requests the rolling day thereafter. Older deep history
is optional enrichment.
No optional backfill state blocks Coverage, Current Health, or projection;
History Depth remains explicit. Credit and WTI retain longer reliable public
history where one bounded source response makes that history cheap and
material to percentile context.

Rerun the explicit backfill command after a retry deadline until the desired
targets are `current`; incomplete targets remain visible without blocking the
workbench. Open or failed document analyses remain explicit module-local gaps.

A good `macro status` reports bounded acquisition target counts/statuses, all
six current module rows with health/history/fact-cutoff clocks, Fed
document-analysis job counts, and the optional analysis runtime state. A
default `disabled` analysis worker is supporting evidence, not a Rates/Fed
outage. Diagnose a missing value by concept ID and source role through its
target current state/cursor, fact family, and three module quality axes. A
public-source timeout, weekend settlement lag, or delayed Yahoo proxy is a
visible quality state; it is not a frontend defect.

After `uv run tracefold db migrate`, the database contains
typed Market/Macro fact tables, acquisition targets, module frontiers, six
current module rows, official documents, document-analysis jobs, and immutable
document analyses.
`20260728_0210` remains the compact current-schema baseline and
the generated Alembic head is the required hard-cut version. A new empty database applies the
baseline and head without replaying retired runtime tables, compatibility
columns, historical backfills, or intermediate contracts. A database stamped
at `20260728_0210` migrates forward once; paid-data placeholders are dropped
rather than archived. Retired News migrations (`20260801_0234` .. `20260815_0273`) remain in the
alembic chain as history; `20260818_0275` is the current irreversible News V3
hard cut that drops every legacy News table and creates the thirteen V3 tables
(see `OPERATIONS.md`).
Historical migrations `20260810_0249` through `20260814_0269` shaped the
former Token Radar singleton; migration `20260818_0274` is the irreversible
Token Radar removal hard cut. It drops `token_radar_current`, the Radar-only
Event covering index and its generated `token_radar_text_fingerprint` column,
and the Radar-only resolution covering index. It preserves every material
Event, intent, resolution, identity, profile, and market fact; there is no
Radar table, route, worker, or compatibility read afterwards. Historical
migration `20260810_0250` also dropped the three Stocks-only derived tables
`stock_attention_target_features`, `stocks_radar_current_rows`, and
`stocks_radar_publication_state`; `us_equity_symbols` remains only as an
internal token-identity collision guard; there is no Stocks product or route.
Migration `20260811_0252` converts legacy acquisition-target states to the
reachable state machine, removes `invalid`, preserves all six current rows, and
clears only their rebuildable frontiers; it does not call a provider. Worker
startup reconstructs missing/version-mismatched frontiers from persisted
Dataset projection state, while matching clean frontiers remain zero-write.
Migration `20260811_0253` hard-cuts only the rebuildable Rates, Economy, and
Cross-Asset serving rows/frontiers to `macro_rates_fed_v8`,
`macro_economy_inflation_v6`, and `macro_cross_asset_v8`. Typed facts,
acquisition state, official documents, jobs, and immutable analyses are
preserved. Stop Serve and Workers, apply both migrations, then let the sole
Macro projection writer reconcile all six frontiers and rebuild the three
semantic-contract rows.
Enable the Macro workers only after the migration is current.

The overview and six typed module reads are persisted-only and never trigger a
provider, model, target advance, projection rebuild, or write. Retired Macro
routes return `404`; there is no compatibility alias.

The full CLI surface is documented by `uv run tracefold --help`.
Treat that output as the source of truth — do not enumerate commands
here. A snapshot lives at `generated/cli-help.md`.

## Container deployment

`make up`, `make status`, `make macro-acceptance`, `make logs`, and `make down`
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
It intentionally does not make business-data freshness part of readiness. Run
`make macro-acceptance` after deployment to validate the overview and six
current modules; use `make logs` for the bounded startup evidence named by a
failure.

### Token Radar removal hard cut

For the one-time transition across `20260818_0274`, build the new image first,
stop Serve and Workers, run the ordinary one-shot migration service, and verify
that material fact counts and identities are unchanged before starting the new
runtime. The migration is transactional and irreversible: it only drops
rebuildable Radar-derived schema. After it succeeds, fix forward; no Radar
route, worker task, projection CPU module, audit query, compatibility adapter,
or imported LKG remains. `GET /api/token-radar` returns `404`; `/api/live-market`
is unchanged.

Use the ordinary lifecycle after the transition:

```bash
make up
make status
make macro-acceptance
make logs
make down
```

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
npm run dev          # Vite console with API/WebSocket proxy to 127.0.0.1:8765
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
