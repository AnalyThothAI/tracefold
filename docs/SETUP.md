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
make status  # fail closed unless the complete product is ready
make logs    # follow PostgreSQL, migration, Serve, and Workers logs
make down    # stop containers; preserve config, passwords, and database data
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
credential, and leaves News push disabled. Edit only the operator-owned
`~/.tracefold/config.yaml` to enable live capabilities. Keep secrets out of
terminal output, docs, tests, and commits.

The generated PostgreSQL DSNs are container-network addresses. The fresh-volume
bootstrap runs only during PostgreSQL `initdb`: it creates the non-login owner
plus Serve, Workers, and migrate roles, then revokes the temporary bootstrap
login before ordinary migration. It never attempts to reinterpret or hard-cut
an unknown non-empty volume. Existing deployments must already have the
least-privilege roles, or use their explicitly authorized maintenance/cutover
path; startup fails closed when the migrate role or schema contract is not
valid.

### Credential-dependent capabilities

The product process is usable without optional live credentials, but affected
lanes report explicit degradation or unavailable evidence:

- absent `news.opennews_token` produces `opennews_token_missing` for the
  primary low-latency OpenNews lane. RSS defaults off; operators may explicitly
  set `news.rss_enabled: true` to add the public breadth/corroboration catalog;
- absent GMGN/OKX credentials leave their authenticated profile, discovery, or
  market lanes unavailable, while configured keyless sources keep their own
  independent behavior;
- the public Brief always has its code-owned Ollama first provider and
  deterministic degraded Top Stories. An absent direct DeepSeek triple or
  optional Groq key can reduce synthesis availability but does not empty the
  public selection;
- absent the direct DeepSeek triple leaves optional News Push title translation
  unavailable; Push still sends the selected Item's original OpenNews headline;
- News push remains off until `news.push.enabled: true` and a supported
  `news.push.feishu_webhook_url` are both configured.

`tracefold config` reports the effective file paths and configured booleans;
it never prints provider tokens, webhook URLs, signing secrets, or model keys.

`news.push.feishu_signing_secret` is optional. When present, the Adapter adds
the Feishu timestamp and signature. When absent, it sends the same compact
interactive card unsigned, without `timestamp` or `sign`; the operator owns
that reduced-authentication choice. Configuration diagnostics report only
configured booleans. Feishu delivery has no model-credential dependency.
Optional translation reuses the direct DeepSeek `llm.api_key`, `llm.base_url`,
and `llm.news_brief_model`; it has no independent endpoint, key, model, or
provider fallback chain. That triple must be entirely present or entirely
absent. Translation makes one bounded attempt and sends the frozen original
immediately on failure.

An unsigned operator configuration uses the existing generated fields; do not
add another secrets file or environment variable:

```yaml
llm:
  api_key: "<operator model secret>"
  base_url: "https://api.deepseek.com/v1"
  news_brief_model: "deepseek-chat"

news:
  enabled: true
  rss_enabled: false
  opennews_token: "<operator secret>"
  push:
    enabled: true
    feishu_webhook_url: "<Feishu v2 webhook>"
    feishu_signing_secret:
```

Leave the signing field empty only when unsigned delivery is intentional. Do
not commit the populated operator config. With Push enabled, a configured
direct DeepSeek triple enables the one-attempt presentation translation; do
not add a `news.push.translation` block or duplicate the credential. The target
language, 7.5-second request timeout, 8-second total
budget, no-retry policy, and title limits are code-owned.

Worker topology and all safety/resource budgets are code-owned. For real data,
`config.yaml` must contain the credentials/endpoints needed by each enabled
lane, including GMGN OpenAPI for exact token profiles and OKX provider settings
for discovery, market data, or DEX WebSocket paths.
The `llm` block owns one all-or-none direct DeepSeek triple—`api_key`,
`base_url`, and `news_brief_model`—plus optional `groq_api_key`. The Brief order
is Ollama, configured direct DeepSeek, then Groq. Worker timeouts, token budgets,
cadence, and resource limits are code-owned; there is no environment-variable
credential path or inferred DeepSeek URL/model. Story Push reuses only that
direct triple for its outbound title-presentation adapter; it does not use
Ollama or Groq and does not enter the serial model arbiter.

News correctness does not depend on the model. The code-owned public
WorldMonitor catalog contributes 179 physical RSS feeds and 183 category
memberships when `news.rss_enabled: true`; the switch defaults to `false`.
Disabled startup reconciliation removes RSS from the active source inventory
and releases prior claims, while the acquisition clock still expires old facts
without making RSS requests. When enabled, one bounded turn conditionally fetches at most one due feed and
atomically replaces its accepted first-five snapshot; failures preserve the
last successful snapshot until the 96-hour floor. The primary OpenNews WSS
receiver, newest-first 12-hour REST overlap, and publisher share the same
acquisition module. REST is requested after
initial connection, reconnect, or queue overflow, reads at most 11 sequential
pages, and stops at the first existing provider record or the 12-hour cutoff;
there is no persisted gap state. RSS provides public breadth and independent
corroboration; acquisition role does not change deterministic Story ranking.
A fixed 60-second writer owns the membership-expanded RSS Top-20-per-category
plus OpenNews physical Story/selection projection,
and the single-capacity native-state model arbiter owns World Brief. On the
first post-migration start, the Story writer can publish current OpenNews facts
before any RSS attempt. An empty Top Story selection is not claimable and does
not complete or overwrite the current Brief slot; normal 60-second PostgreSQL
polling makes a later non-empty selection in the same half hour eligible, with
no wake service or compatibility path.
Push is a separate News-owned delivery state machine with a code-owned 10-second
persisted-evidence reconcile: initial
enablement suppresses the current eligible baseline, later strict
score-greater-than-70 crossings freeze one highest-scored Item and send one
optionally signed Feishu card containing the selected Item's original OpenNews
headline plus a compact body with its coins, provider score, and optional
original-link button, with durable at-least-once retries. It is not a
generic Notifications product or item-level analysis path.
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
Historical migration `20260801_0237` added an OpenNews recovery boundary;
current migration `20260809_0247` removes that state, installs public RSS
source scheduling, and hard-cuts Brief persistence to two singleton tables.
`20260801_0238` adds the News push baseline/delivery ledger. Push remains
disabled after migration until the Feishu webhook and push switch are
explicitly configured; signing remains optional. The first enabled reconcile
records a no-backfill baseline; the code-owned 15-minute live-alert window also
prevents later REST overlap from sending stale articles.
Enable the Macro workers only after the migration is current.

The overview and six typed module reads are persisted-only and never trigger a
provider, model, target advance, projection rebuild, or write. Retired Macro
routes return `404`; there is no compatibility alias.

The full CLI surface is documented by `uv run tracefold --help`.
Treat that output as the source of truth — do not enumerate commands
here. A snapshot lives at `generated/cli-help.md`.

## Container deployment

`make up`, `make status`, `make logs`, and `make down` are the supported
operator lifecycle. `make up` passes an existing `GITHUB_TOKEN` into the image
build as a BuildKit secret; when unset, it uses `gh auth token` if available.
Public dependencies need neither. The token is not stored in an image layer or
application config.

Compose bind-mounts only role-appropriate files from `~/.tracefold/`. Serve
receives only its SELECT credential; Workers receives only its DML credential;
the migrate credential is absent from both steady containers. PostgreSQL data
is pinned to the `tracefold-postgres` named volume, and `make down` does not
delete it.

Fresh-volume bootstrap is an `initdb` hook, not a steady service or a generic
role-repair mechanism. Normal startup consists of PostgreSQL, the one-shot
migration service, and separate Serve/Workers runtimes. `make status` returns
non-zero for a failed/missing migration, stopped or unhealthy required
container, failed Serve or Workers readiness endpoint, or missing HTML console.
Use `make logs` for the bounded startup evidence named by a failure.

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

After a successful one-time cutover, use the ordinary lifecycle again:

```bash
make up
make status
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
