# Public Contracts

Tracefold exposes one configuration contract, one HTTP/WebSocket service, and one CLI. This document records stable behavior; generated OpenAPI is authoritative for exact HTTP fields.

There are no compatibility aliases for retired products, tables, worker names, routes, or response fields. A behavior change updates source, tests, generated contracts, and this document in the same change.

## Runtime configuration

The active operator-owned files are:

- `~/.tracefold/config.yaml` for application, PostgreSQL, providers, credentials, notifications, API, and public WebSocket settings.
- `~/.tracefold/workers.yaml` for worker enablement, cadence, and batch/lease/timeout settings.

Repository examples, fixtures, `.env` files, and generated docs are not runtime configuration. `uv run tracefold config` reports the effective paths and redacted settings. Unknown settings or worker keys fail validation.

The configuration schema uses typed nested models directly
(`storage.postgres`, `api`, `llm`, `gmgn`, `providers.*`, and `upstream`).
Root-level `postgres_*`, `api_*`, provider, LLM, and upstream forwarding
aliases are not part of the configuration contract.

`llm` contains only shared provider credentials (`api_key` and `base_url`).
They are consumed only when `macro_research` or `news_analysis` is enabled.
Model, request timeout, token, cadence, lease, and retry settings remain typed
under the owning worker in `workers.yaml`. The request timeout applies to one
provider transport call; there is no generic model-policy or capacity surface.

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
macro_sync
token_image_mirror
token_profile_current
news_ingest
news_analysis
macro_research
notification_rule
notification_delivery
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
| Watchlist | `/api/watchlist/handles/overview`, `/api/watchlist/handle/{handle}/overview`, `/api/watchlist/handle/{handle}/timeline` | Evidence queries; no separate Watchlist domain |
| Search/case | `/api/search`, `/api/search/inspect`, `/api/token-case`, `/api/target-posts`, `/api/target-social-timeline` | Evidence, identity facts, and current Token Radar rows |
| Radar/market | `/api/token-radar`, `/api/stocks-radar`, `/api/live-market` | stable PostgreSQL current read models |
| Macro | `/api/macro/evidence/{view_id}`, `/api/macro/research` | bounded persisted `macro_observations` views; persisted completed-session DeepAgents research and durable run state |
| News | `/api/news/stories`, `/api/news/stories/{story_id}`, `/api/news/sources` | deterministic Story read model, Article evidence, immutable current-evidence analysis, and source fetch state |
| Notifications | account alerts, notification list with embedded summary, delivery audit, and read commands under `/api` | notification facts and external-delivery ledger |
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

### News

`/api/news/stories` is the sole News collection read. It supports bounded
cursor pagination and `q`, `source`, and `verification_status` filters.
`/api/news/stories/{story_id}` returns the frozen Story projection, every member
Article, provenance, deterministic membership audit, and the immutable
DeepSeek analysis for the current evidence set when available.

Article identity is source-scoped and deterministic. Story membership is
deterministic, versioned, and conflict-aware. `source_count` counts acquisition
feeds; `independent_origin_count` counts verified original publisher domains,
so aggregator repetition does not become corroboration. `6551News` is an
authoritative trusted aggregator but its Article provenance remains
`attributed` unless an external origin URL is extracted. Analyses are Chinese,
evidence-referenced, and versioned by evidence hash, model, prompt, workflow,
and schema. Provider/network/model calls never occur on read endpoints.

There is no `/api/news`, item/fact detail route, source-status compatibility
route, write route, News WebSocket payload, webhook, or public provider adapter.

Search inspection and Token Case likewise return resolver, identity, current
Radar, market, timeline, and source-post facts only. Removed derived prose and
admission fields are absent, not nullable.

### Macro

Macro exposes one parameterized live-fact read family and one research read:

```text
/api/macro/evidence/{view_id}?window=30d|90d|1y|5y
/api/macro/research
```

`view_id` is `dashboard`, `overview`, `rates-inflation`, `growth-labor`,
`liquidity-funding`, `credit`, or `cross-asset`. The live endpoint queries
bounded persisted `macro_observations` directly; it does not call a provider or
model, write a projection, resume research, or synthesize prose. The dashboard
returns six category previews plus bounded uncatalogued latest facts. Detail
views return the complete 108-concept presentation subset for that category,
with row-local missing states, source-native history, observation/source time,
received time, provenance, and transparent calculations. The catalog is
display metadata, never an Agent evidence allowlist.

With no query, `GET /api/macro/research` targets the latest completed U.S.
regular session. Optional `session_date=YYYY-MM-DD` selects one explicit
session. The response is always persisted-only and returns state `current`,
`historical`, `generating`, `failed`, or `missing`, together with the requested
and current session dates. A generating or failed response may include the
durable run status, attempt counts, sanitized last error, and update time.

An available publication contains its schema version, session and market
cutoff, agent-authored title and Chinese executive summary, one authoritative
dynamically ordered list of Markdown sections, explicit evidence gaps,
citations, reviewer notes, sanitized audit, and publication time. A flat
Markdown export is mechanically derived from the same sections and is not a
second API narrative. Citations carry stable
IDs, material `source_ref` values, source labels, observation/publication time,
URL when available, and lineage. The envelope does not prescribe fixed
sections, asset lanes, direction, confidence, score, forecast horizon,
readiness, or a trading conclusion.

The service verifies session/cutoff identity and citation closure before
publication. It does not reject content through language, coverage, readiness,
direction, confidence, or other semantic policy rules. Those judgments belong
to DeepAgents. The read endpoint does not invoke a model or provider, search
facts, resume a graph, run a repair, or synthesize a fallback publication.
Missing remains a typed successful read state rather than an older publication
relabelled as current. Unmatched Macro API paths return the ordinary
application `404` response.

### Notifications

Notifications are durable facts. `GET /api/notifications` is the sole
list/read-summary query and returns both `items` and `summary`. Read commands
update persisted read state. Only watched-account activity and watched-account
token-alert rules produce candidates. The unique `dedup_key` is the sole dedup
authority; its rule-defined occurrence bucket enforces cooldown. External
delivery uses `notification_deliveries` as an auditable side-effect ledger with
compare-and-set state transitions; API responses never infer successful
delivery from a provider call alone.

### Token images

`/api/token-images/{image_id}` accepts only the persisted lowercase SHA-256 URL identity. Only `ready` assets whose relative path resolves under `~/.tracefold/cache/token-images` are served. Missing rows/files, malformed IDs, absolute paths, and traversal attempts return `404`. Provider URLs are never accepted as a proxy input.

## WebSocket

Clients connect to `/ws`, authenticate, then subscribe:

```json
{"type":"auth","token":"..."}
{"type":"subscribe","handles":[],"cas":[{"ca":"0x...","chain":"eip155:1"}],"symbols":[],"market_targets":[],"notifications":false,"replay":100}
```

Authentication accepts exactly `type` and a string `token`. Subscription keys and value shapes are exact: `handles` and `symbols` are string arrays; `cas` contains `{ca, chain?}` objects; `market_targets` contains `{target_type, target_id}` objects; `notifications` is boolean; and `replay` is an integer. Retired `ca`/`tokens` keys, scalar CA values, `address` aliases, extra target keys, and coercible string/number booleans are rejected as `invalid_subscription`. The total filter count and replay count are bounded. Replay is a PostgreSQL read-side query with batched hydration, not one query per event or filter. Push message families are `event`, `notification`, and `live_market_update`.

Worker progress is recovered by bounded database catch-up. Provider frames are never emitted as business facts before persistence.

## CLI

`uv run tracefold --help` is the exact CLI source of truth. Stable top-level families are:

- service/config: `serve`, `init`, `config`;
- database: `db migrate|health|audit|query-audit`;
- Macro: `macro import-bundle|sync|retry-research|status`;
- read models: `recent`, `search`, `asset-flow`, `account-alerts`, `notification-deliveries`;
- maintenance: `ops ...` for explicit repair, rebuild, queue inspection/resolution, and diagnostics.

Mutating maintenance commands require an explicit execution flag where the parser offers a dry-run mode. They operate from persisted facts and stable target keys. A rebuild does not create an alternate generation/run identity or make a provider response the source of truth.

`ops rebuild-market-current --execute` is the bounded, cursor-based repair for
reconstructing `market_tick_current` from persisted `market_ticks`. News has no
public maintenance or compatibility CLI: its two workers recover from
`news_sources`, `news_articles`, and immutable analysis-attempt state. Token
Radar contract and distribution checks use `projection-status`,
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
