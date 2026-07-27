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
They are consumed only when `macro_research` or `news_ai_publish` is enabled.
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
macro_market_intraday
macro_settlements
macro_economic_releases
macro_official_state
macro_official_documents
macro_backfill
macro_projection
macro_judgment
token_image_mirror
token_profile_current
news_ingest
news_story_project
news_brief_plan
news_ai_publish
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
| Macro | `/api/macro/overview`, six typed module routes, `/api/macro/research` | persisted six-module current rows, immutable daily judgment/Evidence Pack, and Evidence-Pack-bound DeepAgents research |
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

`/api/news/stories` serves the Story Feed with bounded cursor pagination,
`view=latest|priority`, and `q`, `source`, and `evidence_posture` filters.
`latest` is the default and orders by last material-evidence time; `priority`
orders by deterministic Priority and then material time. Cursors are
view-bound and fail closed with `news_story_cursor_view_mismatch` when reused
across views.
`/api/news/stories/{story_id}` serves Story Detail with Article/Revision/
Observation provenance, member semantics, identity decisions, material events,
and current/history Story analysis. A user can explicitly request analysis
through `POST /api/news/stories/{story_id}/analysis-requests`; this schedules
work but never calls a model in the request.

`/api/news/brief` returns one composite with:

- `active_selection`: the deterministic current Activation and required Story
  cards;
- nullable `analysis`: only an immutable Publication attached to that
  Activation;
- `analysis_status`: `unavailable`, `pending`, `available`, `failed`, or
  `reused`;
- optional `previous_publication`, always historical rather than current;
- optional `pending_proposal` and bounded `latest_failure`.

Activation time, evidence cutoff, Publication time, and cache attachment are
separate fields. Pending or failed AI never replaces `active_selection` with an
older Publication. `/api/news/brief/history` returns immutable Publications,
including valid late completions that were ineligible to attach.
Changing any qualified model/prompt/workflow/schema/locale field immediately
withdraws an incompatible current attachment. Reads then expose the
deterministic `active_selection` with `analysis_status=pending|failed` until the
new Publication completes or an exact cached Publication is reattached.

Article identity is publisher-artifact scoped and deterministic. Story
membership is versioned, candidate-channel audited, proof-ladder,
constraint-first, and conflict-aware. Source quality,
reporting origin, syndication, independent corroboration, corrections, and
conflict remain separate fields. AI publications are Chinese,
evidence-referenced, fail-closed, and content-addressed by evidence plus the
actual model/prompt/workflow/schema/locale contract. Provider/network/model
calls never occur on read endpoints.

`/api/status` includes a structured `news` health object with independent
`source`, `material`, `brief`, `public`, and `ai` layers. Each breach carries
an exact reason code, measured value or lag, threshold, and bounded details.
Business-invariant failures such as `planner_active_mismatch`,
`active_publication_mismatch`, `story_projection_lag`, or
`public_active_contract_mismatch` can degrade global readiness without making
deterministic Story reads unavailable.

There is no `/api/news` compatibility collection, legacy item/fact detail
route, News WebSocket payload, webhook, or public provider adapter.

Search inspection and Token Case likewise return resolver, identity, current
Radar, market, timeline, and source-post facts only. Removed derived prose and
admission fields are absent, not nullable.

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
judgment cutoff, overall readiness, and compact research state. Each module
returns one stable `macro_module_v1` payload containing current state, largest
changes, versioned features, bounded charts, contradictions, falsifiers, next
checkpoints, dataset quality, evidence gaps, and raw fact references.
Readiness is exactly `ready`, `degraded`, or `blocked`. These reads use
`macro_module_current` and immutable judgment rows only; they never call a
provider/model, advance a target, rebuild a projection, or synthesize a
fallback.

The Dataset and Calculation Registries are code-owned public semantics, not
runtime configuration. Provider config may only enable the free source
families. A dataset's owner, fact family, source/adapter, acquisition clock,
freshness, trust tier, criticality, module membership, and formula identity do
not come from YAML. General cross-asset observations and settlements are Market
facts; macroeconomic series, release events, and official documents are Macro
facts. The legacy generic evidence route, window parameter, bundle/sync
surface, `macro_observations`, and unclassified facts do not exist.

On every U.S. trading session at 08:50 `America/New_York`,
`macro_judgment` seals one cutoff-bounded `macro_evidence_pack_v1` and publishes
one immutable `macro_daily_judgment_v1` when no critical module is blocked.
The judgment fixes growth, inflation, policy, liquidity, credit, and volatility
states plus one-week/one-month directions for SPY, TLT, HYG, DXY, GLD, USO,
BTC, and VIX. It exposes conflicts, invalidation conditions, confidence,
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
- Macro: `macro backfill|retry-research|status`;
- read models: `recent`, `search`, `asset-flow`, `account-alerts`, `notification-deliveries`;
- maintenance: `ops ...` for explicit repair, rebuild, queue inspection/resolution, and diagnostics.

Mutating maintenance commands require an explicit execution flag where the parser offers a dry-run mode. They operate from persisted facts and stable target keys. A rebuild does not create an alternate generation/run identity or make a provider response the source of truth.

`ops rebuild-market-current --execute` is the bounded, cursor-based repair for
reconstructing `market_tick_current` from persisted `market_ticks`.
`ops rebuild-news-stories --execute` is the destructive, explicit Identity-v2
replay from preserved ArticleRevision facts; it invokes the same sequential
projection seam as `news_story_project` and does not preserve or redirect old
Story IDs. Normal recovery remains bounded PostgreSQL catch-up across the four
News workers, with no compatibility CLI or alternate clustering path. Token
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
