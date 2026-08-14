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
credential, leaves `news.opennews_strategy_ids` empty, and leaves News push
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

The product process is usable without optional live credentials, but affected
lanes report explicit degradation or unavailable evidence:

- absent `news.opennews_token` produces `opennews_token_missing` for the
  operator-bound OpenNews Strategy lane. When a token is configured while News
  is enabled, `news.opennews_strategy_ids` must be a non-empty duplicate-free
  set or configuration fails closed. RSS defaults off; operators may explicitly
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

`tracefold config` reports the effective file paths, configured booleans, and
`opennews_strategy_count`; it never prints provider tokens, Strategy-ID values,
webhook URLs, signing secrets, or model keys.

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
  opennews_strategy_ids:
    - "1018" # News Score > 70
    - "1019" # OI Event Monitor
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

The current cutover configures exactly `1018` (News Score > 70) and `1019` (OI
Event Monitor), and `tracefold config` therefore reports a redacted Strategy
count of `2`. Listing and Delisting Announcements and Storage News exist
provider-side but are deliberately excluded. Provider-side enablement never
silently edits Tracefold configuration; adding any Strategy later requires an
explicit config change, while the account page remains the definition authority.

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
last successful snapshot until the 96-hour floor. The OpenNews Strategy receiver
and publisher share the same acquisition module. It authenticates one WSS and
sends zero application subscription frames; the server automatically pushes the
account owner's `strategy.triggered` notifications. Tracefold admits only nested
Strategy IDs in the exact configured allowlist, including configured NEWS and
MARKET/OI events regardless of `engineType`. It does not send
`news.subscribe`, `strategy.subscribe`, or `strategy.triggered`, does not call
ordinary `/open/news_search` for recovery, and does not use private webpage APIs.
A disconnect, queue overflow, or process outage creates a typed incident.
Reconnect restores current WSS independently; the official authenticated
Strategy list/hits endpoints perform bounded idempotent incident recovery, while
partial provider retention stays visible. Search is never used for recovery.
RSS provides public breadth and independent corroboration; Strategy admission
does not change the deterministic Story/Brief algorithms.
A dirty-triggered writer owns the membership-expanded RSS Top-20-per-category
plus OpenNews physical Story/selection projection. It coalesces accepted-fact
bursts for one second and retains a five-minute safety pass,
and the single-capacity native-state model arbiter owns World Brief. On the
first post-migration start, the Story writer can publish current
Strategy-admitted OpenNews facts before any RSS attempt. An empty Top Story
selection is not claimable and does
not complete or overwrite the current Brief slot; a later dirty Story turn in
the same half hour makes a non-empty selection eligible.
Push is a separate News-owned delivery state machine. Initial enablement records
one no-backfill epoch. The Story writer atomically inserts one outbox row for
each Story containing a live fact first observed after that epoch. Scoreless,
assetless, linkless, and CL-labelled Stories remain eligible; recovery-first and
pre-enablement facts never backfill. The delivery worker sends one optionally
signed Feishu card containing the representative Story headline plus a compact
body with any available `关联资产`, provider score, and optional
original-link button, with durable at-least-once retries. It is not a
generic Notifications product or item-level analysis path.
An accepted Strategy trigger may appear outside explicit Focus when it lacks a
score above 70, while still creating Story and Push. The due worker never scans
Stories to discover candidates; it only claims existing transactional outbox
rows.
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
rather than archived. Migrations `20260801_0235` and `20260801_0236`
irreversibly delete retired News acquisition history and Macro publication,
per-attempt, and stored intermediate history while preserving current items,
facts, targets, document analyses, and module rows.
Historical migration `20260801_0237` added an OpenNews recovery boundary;
historical News migration `20260809_0247` replaced it with bounded ordinary-news
overlap, installed public RSS source scheduling, and hard-cut Brief persistence
to two singleton tables. Neither recovery shape is current: Strategy-only
migration `20260813_0265` removes ordinary-news REST overlap and records unknown coverage
without replay.
Stop Serve and Workers before applying `20260813_0265`. It deactivates
legacy full-corpus OpenNews Items, clears Story/member/selection for the sole
normal writer to rebuild after startup, clears
incompatible Brief current/LKG state, cancels obsolete pending/retry Push work,
preserves sent-delivery audit plus Push baseline/dedup evidence, and resets old
REST-recovery telemetry. Start only the new writer after the exact non-empty
allowlist is configured; never overlap old and new acquisition.
Stop Serve and Workers before applying `20260813_0266`. It adds
`news_opennews_incidents`, replaces legacy coverage columns with official
Strategy-history status, and marks every OpenNews fact's immutable first ingest
mode. It removes the Push eligibility clocks/cursor/ring, renames the baseline
to the enablement epoch, terminalizes incompatible unsent v1 rows, and installs
the live-only v2 Story outbox contract. After restart, current WSS state and
latency are independent of incident recovery.
Token Radar migration `20260810_0249` removed the retired Radar projection
tables and temporary replay-only schema, then installed the compact singleton.
It preserved material Events, intents, resolutions, identities, and market
facts. Historical migration `20260810_0250` reset that v1 singleton to one empty
`token_radar_snapshot_v2`, installed the fixed Top-50/96-KiB contract, and dropped
the three Stocks-only derived tables `stock_attention_target_features`,
`stocks_radar_current_rows`, and `stocks_radar_publication_state`. It preserved
all material facts and retains `us_equity_symbols` only as an internal
token-identity collision guard; there is no Stocks product or route.
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
Historical migration `20260811_0254` hard-cut only the rebuildable Radar
singleton to `token_radar_snapshot_v3`, initial `unavailable` state, and the
bounded source-time index/basic serving constraints. It preserved every
material Event, intent, resolution revision, identity/profile fact, and market
fact; its v3 runtime replayed a three-hour horizon for one-hour current/prior
windows and a one-hour episode TTL.
Migration `20260812_0255` hard-cuts only that rebuildable singleton to
`token_radar_snapshot_v4` and initial `unavailable` state, and installs the
bounded source-time index rebuilt as the narrow fingerprint covering index,
plus the covering resolution index used by the optimized load SQL. It again
preserves every material fact. This is now historical.
Migration `20260814_0269` is the current irreversible v5 KISS hard cut. It
preserves every material fact, replaces only the rebuildable Radar singleton
with one valid empty `token_radar_snapshot_v5` packet and canonical snapshot
fingerprint, and removes all attempt/failure/state/ruleset/input/workload
columns. Stop Serve and Workers while an existing database crosses this
revision; after migration, start only the v5 runtime. A fresh database migrates
directly to head.
Migration `20260813_0256` adds deterministic `facet_facts` to the existing
rebuildable `news_stories` read model, backfills it from current Story
membership, and invalidates the Story input fingerprint for one normal writer
rebuild. It adds no News table and preserves every Item, Story identity,
membership, Brief, and Push ledger row. Stop Serve and Workers while crossing
the revision so no old writer can publish the pre-column shape.
Migration `20260813_0257` adds narrow covering and partial-expression indexes
for the membership-first numeric News score read and bounded Push-health
reads. It extends the existing Push singleton with transactionally maintained
lifetime counts/latest event clocks and adds typed delivery telemetry for a
capped 24-hour SLO sample. It adds no table, second writer, or `active` filter,
and it does not relax Serve deadlines. Stop Serve and Workers while crossing
the revision, then start both runtimes.
Migration `20260813_0258` adds the News Push reconcile cursor plus the
provider score/assets eligibility clock, and widens two existing market-read
indexes in place. Stop Serve and Workers while crossing this revision and keep
them stopped through the 0259 invariant repair.
Migration `20260813_0259` repairs the eligibility clock of any numeric-score
Item written during a mixed-version 0258 cutover and then enforces that clock
as a database invariant. Migration `20260813_0260` adds the durable 25-second
Push reconcile-ring clock. Migration `20260813_0261` replaces Radar's old
expression index with a STORED generated text fingerprint and narrow covering
index while preserving all facts/current payload. Keep Serve and Workers
stopped while crossing these revisions, then restore only the new single
writer, observe one bounded cursor wrap, and verify Push latency plus Workers
heartbeat stability.
`20260801_0238` historically adds the News push baseline/delivery ledger. Push remains
disabled after migration until the Feishu webhook and push switch are
explicitly configured; signing remains optional. Current migration 0266 uses a
no-backfill enablement epoch and admits every live Story observed after it,
without score, asset, or age gates.
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
role-repair mechanism. Normal startup consists of PostgreSQL, the one-shot
migration service, and separate Serve/Workers runtimes. `make status` returns
non-zero for a failed/missing migration, stopped or unhealthy required
container, failed Serve or Workers readiness endpoint, or missing HTML console.
It intentionally does not make business-data freshness part of readiness. Run
`make macro-acceptance` after deployment to validate the overview and six
current modules; use `make logs` for the bounded startup evidence named by a
failure.

### Token Radar v5 serving hard cut

For the one-time `0255` to `0269` transition, build the new image first, stop
Serve and Workers, run the ordinary one-shot migration service, and verify that
material fact counts and identities are unchanged before starting the new
runtime. The migration is transactional: failure leaves the v4 singleton in
place. After it succeeds, fix forward with the v5 code; no v4 packet reader,
dual writer, compatibility adapter, or imported LKG remains.

The v5 singleton always serves one complete exact packet containing only
`schema_version`, `social_evidence_as_of_ms`, `eligible_total`, and `items`.
Its initial value is an empty valid packet; there is no availability, stale, or
failure state. Workers run one immediate turn and then wait 30 seconds after
each completed turn. A turn loads the bounded twelve-hour causal replay,
reduces the unchanged four-hour current/prior semantics on the isolated CPU
process, batch-loads selected presentation facts, and publishes only when the
canonical snapshot fingerprint changes. Database operations use the shared
native deadlines; there is no Radar whole-turn or phase budget. A non-cancelled
failure leaves the last successful packet unchanged and retries only on the
next natural cycle.

Migration `20260813_0261` materializes the exact ASCII-lower,
whitespace-normalized MD5 duplicate-text fingerprint as a STORED Event column
and INCLUDEs it in the partial source-time index, allowing vacuum-visible
history to remain Index Only without fetching wide Event text. Presentation
uses at most one target-index LATERAL probe per selected market key for recent
positive market cap, never a global recent-tick scan.

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
