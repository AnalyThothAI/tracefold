# Public Contracts

Tracefold exposes one configuration contract, one HTTP/WebSocket service, and one CLI. This document records stable behavior; generated OpenAPI is authoritative for exact HTTP fields.

There are no compatibility aliases for retired products, tables, worker names, routes, or response fields. A behavior change updates source, tests, generated contracts, and this document in the same change.

## Runtime configuration

The active operator-owned files are:

- `~/.tracefold/config.yaml` for application, PostgreSQL, providers, credentials, API, and public WebSocket settings.
- `~/.tracefold/workers.yaml` for worker enablement, cadence, and batch/lease/timeout settings.

Repository examples, fixtures, `.env` files, and generated docs are not runtime configuration. `uv run tracefold config` reports the effective paths and redacted settings. Unknown settings or worker keys fail validation.

The configuration schema uses typed nested models directly
(`storage.postgres`, `api`, `llm`, `gmgn`, `providers.*`, and `upstream`).
Root-level `postgres_*`, `api_*`, provider, LLM, and upstream forwarding
aliases are not part of the configuration contract.

### Watchlist and Notifications config cutover

Before starting this hard cut, edit the active operator-owned files rather than
copying repository examples over them:

1. Remove top-level `handles`, top-level `notifications`, and `news.sources`
   from `~/.tracefold/config.yaml`.
2. Remove top-level `notification_rule`, top-level
   `notification_delivery`, and `token_radar_projection.scopes` from
   `~/.tracefold/workers.yaml`.
3. Run `uv run tracefold config` and confirm only the reported paths and
   redacted configuration. Any retired key must fail validation; there is no
   alias, merge, or generated-source fallback.
4. Stop older workers, run `uv run tracefold db migrate`, and then start the
   current service so no process can write a retired persistence contract
   during the irreversible migration.

`llm` contains operator-owned provider credentials: `api_key` and `base_url`
for the current OpenAI-compatible provider plus optional
`openrouter_api_key` and `groq_api_key` for the News fallback chain. They are
consumed only by enabled AI workers. Model, endpoint, request timeout, token,
cadence, lease, and retry settings remain typed under the owning worker in
`workers.yaml`. News World Brief attempts configured local Ollama, the current
OpenAI-compatible provider, OpenRouter, then Groq once each under one
60-second chain budget. Environment variables are not a credential contract.

`src/tracefold/app/worker_manifest.py` owns the worker inventory and
writer/queue declarations. The current keys are:

```text
collector
market_tick_stream
market_tick_poll
event_anchor_backfill
resolution_refresh
asset_profile_refresh
token_radar_projection
macro_settlements
macro_economic_releases
macro_official_state
macro_official_documents
macro_backfill
macro_document_analysis
macro_projection
macro_judgment
token_image_mirror
token_profile_current
news_pipeline
news_world_brief
macro_research
```

`workers.yaml`, `WorkersSettings`, factories, status output, and this manifest
must use these exact names. Configuration cannot add another worker or derived
product lane.

## HTTP

The service exposes `/healthz`, `/readyz`, `/metrics`, `/ws`, static frontend assets, and `/api/*`.

- `/healthz` is process liveness.
- `/readyz` combines a lightweight PostgreSQL liveness check with the cached startup schema/composition result. It does not inspect providers, queues, or business freshness.
- `/api/status` captures one typed in-memory runtime snapshot for worker status,
  collector details, provider connections, and startup/schema state. It
  performs no SQL.
- Read endpoints do not call providers, execute models, mutate facts, or rebuild projections.

Status contains no model configuration, model policy, capacity counters,
prompt state, or model-derived business status.

API responses use a typed envelope:

```json
{"ok": true, "data": {}}
```

Errors use `ok: false` with a stable error code. Pydantic response models generate `docs/generated/openapi.json` and `web/src/lib/types/openapi.ts`; frontend code consumes those generated types.

### Endpoint families

| Family | Routes | Source of data |
|---|---|---|
| Bootstrap/status | `/api/bootstrap`, `/api/status` | runtime composition and worker status |
| Events | `/api/recent`, `/api/events/by-ids` | persisted event/evidence facts |
| Search/case | `/api/search`, `/api/search/inspect`, `/api/token-case`, `/api/target-posts`, `/api/target-social-timeline` | Evidence, identity facts, and current Token Radar rows |
| Radar/market | `/api/token-radar`, `/api/stocks-radar`, `/api/live-market` | stable PostgreSQL current read models |
| Macro | `/api/macro/overview`, six typed module routes, `/api/macro/research` | persisted six-module current rows, immutable daily judgment/Evidence Pack, and Evidence-Pack-bound DeepAgents research |
| News | `/api/news/feed`, `/api/news/stories/{story_id}`, `/api/news/brief`, `/api/news/sources`, `/api/news/status` | deterministic Story read model, NewsItem members, immutable Chinese Brief, source fetch state, and derived News health |
| Images | `/api/token-images/{image_id}` | ready mirrored assets under the operator cache root |

There is no CEX OI/detail product API. Generic exchange facts and provider adapters remain internal inputs to supported products.

### Token Radar

`/api/token-radar` serves `token_radar_current_rows` selected by stable
product/window keys. Each public row exposes `factor_snapshot` as the sole
target, market, attention, score, decision, and source-event payload; it does
not duplicate those sections at row level. Factor subjects use exactly
`target_type`, `target_id`, `symbol`, `target_market_type`, `chain`, `address`,
and `pricefeed_id`. The transparent factor families are `social_heat`,
`social_propagation`, and `timing_risk`. `gates`, `normalization`, and
`composite` use their producer-defined fixed fields, and decisions are exactly
`discard`, `watch`, or `high_alert`. The endpoint never falls back to
historical runs, source-event dirty rows, provider calls, identity aliases, or
alternate decision labels.

Radar, Stocks, Search, Asset Flow, and Token Case use one provider-neutral
population. Radar reads accept window and venue where declared by OpenAPI, but
no population `scope`; a retired `scope` query parameter is rejected with
`400 unsupported_query_param` instead of being ignored or aliased.

### News

The News public surface is exactly five read-only routes:

- `GET /api/news/feed?category={category}&level={level}&source_id={source_id}&sort={importance|latest}&limit={limit}&cursor={cursor}`
  returns one flat global Story page. `importance` is the default; page size
  defaults to 50 and is capped at 100. Both sorts use the same eligible Story
  population. Filters run before deterministic keyset ordering and
  pagination. The response includes Stories, facets, `next_cursor`, and
  `has_more`; the browser does not cluster, score, or reorder.
- `GET /api/news/stories/{story_id}` returns one persistent active or archived
  Story and its NewsItem evidence. It exposes representative/scoring item
  identity, title/source/time, classification, physical-source count,
  importance score, and the transparent factor breakdown. It contains no
  revisions or per-Story AI analysis.
- `GET /api/news/brief` returns one current Chinese World Brief, its truthful
  state, selected Story evidence, bounded immutable publication history, and
  latest run when present. Insufficient material makes no model call. A failed
  update preserves the last-known-good publication as `stale_fallback`.
- `GET /api/news/sources` returns the frozen physical-source registry,
  memberships, conditional-fetch health, and direct/relay diagnostics.
- `GET /api/news/status` derives warming/ready/degraded News health from
  PostgreSQL source, Story-invariant, and Brief state.

`/api/news/feed` and `/api/news/brief` emit an ETag, honor
`If-None-Match` with `304`, and use `Cache-Control: private, no-cache`.
Every read is PostgreSQL-only: it never fetches a source, calls a model,
reclusters, or repairs state.

NewsItem identity is source scoped. A changed source `pubDate` alone is an
acquisition observation, not a material revision. Story identity is the
persistent result of the WorldMonitor-compatible 96-hour title cluster;
existing memberships and aliases prevent a representative change from
inventing a new Story. Corroboration counts distinct physical source IDs, not
memberships, parsed publisher names, or same-source revisions. Keyword
classification and importance are deterministic and fully sufficient without
AI.

The Brief worker calls no model when fewer than three Stories or fewer than two
physical sources are available, or when its ordered Top-8 Story fingerprint is
unchanged. A new fingerprint permits one attempt per configured provider,
bounded by 60 seconds total. Publications are Chinese and citation-index
locked: line `[n]` always refers to selected Story `n`. Invalid lines are
repaired locally without shifting indexes. The current pointer changes only
after a complete valid publication transaction succeeds.

`/api/news/status` exposes three independent News health layers: `ingest`,
`story`, and `brief`. Deterministic Story cards remain readable while a Brief
is running, failed, insufficient, or stale.

There is no `/api/news/stories` collection, `view=latest|priority`, Brief
history route, analysis request route, item route, News WebSocket payload,
webhook, compatibility alias, or alternate clustering path.

### Macro

Macro exposes one overview, six typed decision-module reads, and one research
read:

```text
/api/macro/overview
/api/macro/rates-fed
/api/macro/economy-inflation
/api/macro/liquidity-funding
/api/macro/credit
/api/macro/volatility
/api/macro/cross-asset
/api/macro/research
```

Overview and module reads accept no query parameters. The overview returns the
latest immutable daily judgment, six module summaries, changes since the
judgment cutoff, three independent status families, and compact research state.
Each route returns exactly one matching schema:

- `macro_rates_fed_v2`
- `macro_economy_inflation_v2`
- `macro_liquidity_funding_v2`
- `macro_credit_v2`
- `macro_volatility_v2`
- `macro_cross_asset_v2`

Shared fields are limited to identity, clocks, status, summary, contradictions,
falsifiers, checkpoints, and evidence lineage. Treasury cross-sections, Fed
events, credit ladders, and the ETF comparison matrix are explicit typed
fields, not generic chart arrays. Coverage is `complete`, `partial`, or
`licensed_unavailable`; Data Health is `current`, `delayed`, `stale`, `invalid`,
`backfilling`, or `unavailable`; Judgment is `current`, `missing`, or `blocked`.
These reads use `macro_module_current` and immutable judgment rows only; they
never call a provider/model, advance a target, rebuild a projection, or
synthesize a fallback.

The Dataset and Calculation Registries are code-owned public semantics, not
runtime configuration. Provider config may only enable the free source
families. A dataset's owner, fact family, source/adapter, acquisition clock,
freshness, trust tier, criticality, module membership, and formula identity do
not come from YAML. General cross-asset observations and settlements are Market
facts; macroeconomic series, release events, and official documents are Macro
facts. The legacy generic evidence route, window parameter, bundle/sync
surface, `macro_observations`, and unclassified facts do not exist.

The Cross-Asset payload always owns the fixed ETF basket SPY, QQQ, IWM, TLT,
IEF, LQD, HYG, UUP, GLD, and USO. Nasdaq public history is an explicitly
`untrusted_proxy` source. WTI is the separate official FRED/EIA
`DCOILWTICO` benchmark; USO is never relabelled as spot or futures. The Rates
payload exposes Treasury nominal and real maturity cross-sections for current,
1W, 1M, and 3M snapshots, matched breakevens, 2s10s/3m10s/5s30s histories,
transparent curve-shape inputs, and explicit licensed-unavailable CME
probabilities.

FOMC statement, implementation, minutes, and SEP documents plus Board/Reserve
Bank speeches retain official full body text and source hashes. SEP PDF text is
extracted from the official PDF with bounded page/content limits. The
`macro_document_analysis` worker writes one immutable, model/prompt-versioned,
exact-evidence-bound analysis per source body after effective-dated role facts
are available. Institutional FOMC stance and the 90-day officials
communication distribution remain separate. Non-policy material is
`not_policy_signal`; no static official label or universal hawk/dove score
exists. The current immutable-analysis admission window is 550 days for FOMC
materials and 120 days for speeches. Older official bodies remain durable raw
evidence and do not block the current Daily Judgment.

Credit exposes IG/BBB/BB/B/CCC OAS, actual-sample history statistics, IG/HY
effective yields, deterministic comparisons with EFFR and 10Y Treasury, SLOOS
standards and demand for C&I/CRE/consumer, loan delinquency/charge-off facts,
and labelled ETF/CFTC confirmations. TRACE/NAV detail remains
`licensed_unavailable`. FRED's public ICE BofA series exposes only its current
three-year window, so older ICE history is also declared
`licensed_unavailable`, never inferred or silently presented as complete. Five
concurrent credit dimensions are returned; no composite score exists.

On every U.S. trading session at 08:50 `America/New_York`,
`macro_judgment` seals one cutoff-bounded `macro_evidence_pack_v2` and publishes
one immutable `macro_daily_judgment_v2` when no critical module is blocked and
required professional backfills/document analyses are complete.
The judgment fixes growth, inflation, policy, liquidity, credit, and volatility
states plus one-week/one-month directions for SPY, QQQ, IWM, TLT, IEF, LQD,
HYG, UUP, GLD, USO, BTC, and VIX. It exposes conflicts, invalidation conditions, confidence,
citations, gaps, and next checkpoints instead of a hidden score.

With no query, `GET /api/macro/research` targets the latest completed U.S.
regular session. Optional `session_date=YYYY-MM-DD` selects one explicit
session. The response is always persisted-only and returns state `current`,
`historical`, `generating`, `failed`, or `missing`, together with the requested
and current session dates. A generating or failed response may include the
durable run status, attempt counts, sanitized last error, and update time.

An available publication is bound to the same immutable Evidence Pack used by
the session judgment. It contains the Evidence Pack ID, schema version, session
and market cutoff, agent-authored title and Chinese executive summary, one
authoritative dynamically ordered list of Markdown sections, explicit evidence
gaps, citations, reviewer disposition (`pass`, `revise`, or `block`), reviewer
notes, sanitized audit, and publication time. A flat Markdown export is
mechanically derived from the same sections and is not a second API narrative.
Citations carry stable
IDs, material `source_ref` values, source labels, observation/publication time,
URL when available, and lineage. The envelope does not prescribe fixed
sections, asset lanes, direction, confidence, score, forecast horizon,
readiness, or a trading conclusion.

The service verifies Evidence Pack/session/cutoff identity, citation closure,
and reviewer disposition before publication. These are contract checks, not a
second semantic gate. The read endpoint does not invoke a model or provider,
search facts, resume a graph, run a repair, or synthesize a fallback
publication.
Missing remains a typed successful read state rather than an older publication
relabelled as current. Unmatched Macro API paths return the ordinary
application `404` response.

### Token images

`/api/token-images/{image_id}` accepts only the persisted lowercase SHA-256 URL identity. Only `ready` assets whose relative path resolves under `~/.tracefold/cache/token-images` are served. Missing rows/files, malformed IDs, absolute paths, and traversal attempts return `404`. Provider URLs are never accepted as a proxy input.

## WebSocket

Clients connect to `/ws`, authenticate, then subscribe:

```json
{"type":"auth","token":"..."}
{"type":"subscribe","cas":[{"ca":"0x...","chain":"eip155:1"}],"symbols":[],"market_targets":[],"replay":100}
```

Authentication accepts exactly `type` and a string `token`. Subscription keys
and value shapes are exact: `symbols` is a string array; `cas` contains
`{ca, chain?}` objects; `market_targets` contains `{target_type, target_id}`
objects; and `replay` is an integer. Retired `handles`, `notifications`,
`ca`, and `tokens` keys, scalar CA values, `address` aliases, extra target
keys, and coercible string/number values are rejected as
`invalid_subscription`. The total filter count and replay count are bounded.
Replay is a PostgreSQL read-side query with batched hydration, not one query
per event or filter. Event replay and event pushes require at least one `cas`
or `symbols` filter; an empty event filter returns and broadcasts no events.
`market_targets` remains an independent subscription for
`live_market_update` pushes. Push message families are `event` and
`live_market_update`.

Worker progress is recovered by bounded database catch-up. Provider frames are never emitted as business facts before persistence.

## CLI

`uv run tracefold --help` is the exact CLI source of truth. Stable top-level families are:

- service/config: `serve`, `init`, `config`;
- database: `db migrate|health|audit|query-audit`;
- Macro: `macro backfill|retry-research|status`;
- read models: `recent`, `search`, `asset-flow`;
- maintenance: `ops ...` for explicit repair, rebuild, queue inspection/resolution, and diagnostics.

Mutating maintenance commands require an explicit execution flag where the parser offers a dry-run mode. They operate from persisted facts and stable target keys. A rebuild does not create an alternate generation/run identity or make a provider response the source of truth.

`ops rebuild-market-current --execute` is the bounded, cursor-based repair for
reconstructing `market_tick_current` from persisted `market_ticks`.
News has no repair/rebuild CLI: normal recovery is a full 96-hour PostgreSQL
recluster by `news_pipeline` on each bounded catch-up cycle. Token Radar
contract and distribution checks use `projection-status`,
`validate-projections`, and `factor-diagnostics`; the CLI does not carry a
second copy of the factor contract.

One-shot worker commands call the same application composition and `WorkerBase` lifecycle as the service. Their `data` object reports `worker_name`, `processed`, `failed`, `dead`, `skipped`, and `notes`; commands that enqueue repair targets first also include `preparation`. The CLI does not construct workers or own provider/database cleanup.

Queue resolution is auditable: retry mutates the source queue and resolves terminal evidence in one transaction; quarantine/archive resolves the terminal row without pretending the source work succeeded.

## Contract change discipline

For a public contract change:

1. change the owning domain/application behavior;
2. add a behavior or contract test;
3. update Pydantic/OpenAPI/frontend types when the HTTP shape changes;
4. update this document and the relevant domain architecture map;
5. remove the old name/path instead of adding an alias or dual read/write.

Historical dated audits explain why a hard cut happened; they are not a second runtime specification.
