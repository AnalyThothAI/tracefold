# Public Contracts

Tracefold exposes one configuration contract, one HTTP/WebSocket service, and one CLI. This document records stable behavior; generated OpenAPI is authoritative for exact HTTP fields.

There are no compatibility aliases for retired products, tables, worker names, routes, or response fields. A behavior change updates source, tests, generated contracts, and this document in the same change.

## Runtime configuration

The active operator-owned application file is
`~/.tracefold/config.yaml`. It contains deployment/domain choices,
PostgreSQL role DSNs and password-file references, providers, credentials,
API, and public WebSocket settings. Worker topology, cadence, deadlines,
resource limits, batches, leases, retries, and model reservations are
code-owned and are not configuration fields.

Repository examples, fixtures, `.env` files, and generated docs are not runtime configuration. `uv run tracefold config` reports the effective paths and redacted settings. Unknown settings or worker keys fail validation.

The optional `~/.tracefold/rsshub.env` file belongs only to the Compose RSSHub
sidecar and is never read or reported by the Tracefold application. Its
absence is valid: Compose still starts, while the ordinary WallStEngine source
reports acquisition failure through News health until RSSHub can fetch it.

The configuration schema uses typed nested models directly
(`storage.postgres`, `api`, `llm`, `gmgn`, `providers.*`, and `upstream`).
Root-level `postgres_*`, `api_*`, provider, LLM, and upstream forwarding
aliases are not part of the configuration contract.

`workers.yaml`, top-level `handles`, top-level `notifications`, and
`news.sources` are retired inputs. Any equivalent retired key fails
validation; there is no alias, merge, or generated-source fallback.

`llm` contains operator-owned provider credentials: `api_key` and `base_url`
for the current OpenAI-compatible provider plus optional
`openrouter_api_key` and `groq_api_key` for the News fallback chain. They are
consumed only by enabled AI workers. Model execution policy, timeouts, token
budgets, cadence, leases, retries, and reservations are code-owned.
Environment variables are not a credential contract.

`src/tracefold/app/worker_manifest.py` owns the worker inventory and
writer/queue declarations. The current keys are:

```text
collector
market_tick_stream
market_tick_poll
event_anchor_capture
resolution_refresh
macro_intraday_market
macro_settlements
macro_economic_releases
macro_official_state
macro_official_documents
news_ingest
asset_profile_refresh
token_image_mirror
steady_projection_coordinator
model_generation_coordinator
```

Factories, runtime status, and this manifest use these exact names.
Configuration cannot add another worker or derived product lane. Macro
backfill is maintenance-only and is absent from the steady manifest.

## HTTP

The service exposes `/healthz`, `/readyz`, `/metrics`, `/ws`, static frontend assets, and `/api/*`.

- `/healthz` is process liveness.
- `/readyz` combines a lightweight PostgreSQL liveness check with the cached startup schema/composition result. It does not inspect providers, queues, or business freshness.
- `/api/status` combines the serve startup/schema snapshot with persisted
  `worker_runtime_status`; stale worker heartbeats fail closed as unavailable.
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
| Macro | `/api/macro/overview`, six typed module routes, `/api/macro/research` | persisted six-module current rows, immutable Evidence Pack/Thesis, independent review, Live Delta, and Outcome Replay |
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

The code-owned inventory contains 73 physical sources and 73 memberships.
It retains every crypto source, the focused US finance/government/politics
set, global event/security/energy/crisis coverage, and the explicit Nikkei
Asia, SCMP, Xinhua, and Al Jazeera regional exceptions. General regional feeds
are disabled and cannot remain active Stories or Brief candidates after the
next deterministic rebuild. Trump Truth Social is a tier-1 first-party source
under the ordinary Story and Brief rules.
WallStEngine is an ordinary English tier-4 Finance source acquired from the
internal RSSHub sidecar. Classification reads its RSS title only; quote text
in the description does not affect category. Source membership does not force
an economic category or change Story, ranking, corroboration, or Brief rules.
External relay fallback is allowed only for code-owned public HTTPS feed URLs.
HTTP, localhost, single-label container hosts, link-local, loopback, private,
and other non-public destinations are direct-only and never leave the Compose
network through the relay.

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
intended 08:50 New York requested session, the immutable Thesis when available,
server-owned twelve-asset fact/outlook/recovery rows, scoped Live Delta,
Outcome Replay, six typed module availability summaries, three independent
data-quality axes, backfill execution state, and typed reasons. It never
includes a prior-publication fallback. HTTP loading, stale cache, disabled
query, and error are frontend transport states rather than Thesis states.
Each route returns exactly one matching schema:

- overview: `macro_overview_v8`
- current research: `macro_thesis_detail_v4`
- explicit archive: `macro_thesis_archive_detail_v2`
- `macro_rates_fed_v6`
- `macro_economy_inflation_v5`
- `macro_liquidity_funding_v5`
- `macro_credit_v7`
- `macro_volatility_v7`
- `macro_cross_asset_v7`

The five non-rates modules share identity, clocks, status, summary,
contradictions, falsifiers, checkpoints, and evidence lineage. Rates v6
deliberately has no generic `summary`, `top_changes`, contradiction, or
falsifier fields. Its `decision` contract is tenor-native: 2Y/10Y/30Y current
facts, actual baseline dates for 1D/1W/MTD/3M/past-30-day changes,
session-completeness state, 2s10s/10s30s summaries, same-day 10Y/30Y
nominal-real-Breakeven decomposition, window-qualified classifications, and
fact references. Treasury completed-session curves are decision-primary; FRED
single-tenor series are history/reconciliation only. Treasury cross-sections,
Fed events, credit ladders, and the ETF comparison matrix are explicit typed
fields, not generic chart arrays. Coverage is `complete` or `partial`; Current
Health is `current`, `degraded`, or `unavailable`; rates session completeness
is an independent `complete`, `unaligned`, or `incomplete` axis. History Depth
is `complete`, `partial`, `insufficient`, or `not_required`. Each Dataset
additionally exposes market state and source state. Optional history cannot
lower Current Health. Only declared required windows affect reader-facing
History Depth; optional maximum public history remains audit-only.
The Macro overview read always returns the intended Thesis session and
deterministic cutoff, even before publication. Its Thesis state is one of
`published`, `pending`, `running`, `retryable`, `failed`, `config_error`,
`not_published`, or `missing`. A `published` current state requires a v2
publication whose session exactly matches the resolved session. One missing or
schema-mismatched module produces a typed
unavailable slot and lowers Evidence Health rather than failing the entire
overview or dossier. These reads use `macro_module_current` and immutable
Thesis tables only; they never call a provider/model, advance a target, rebuild
a projection, or write fallback content.

The Dataset and Calculation Registries are code-owned public semantics, not
runtime configuration. Provider config may only enable the free source
families. A dataset's owner, fact family, source/adapter, acquisition clock,
freshness, trust tier, criticality, module membership, and formula identity do
not come from YAML. General cross-asset observations and settlements are Market
facts; macroeconomic series, release events, and official documents are Macro
facts. The legacy generic evidence route, window parameter, bundle/sync
surface, `macro_observations`, and unclassified facts do not exist.

Every Registry row has a stable `concept_id` and `source_role`.
`decision_primary` is authoritative for the current decision; `release`,
`history`, `intraday_proxy`, and `reconciliation_only` remain separately
labelled inputs. A
reconciliation receipt records comparisons; values from different identities
are never averaged or silently substituted. Release facts preserve actual,
expected, surprise, revision, reference date, optional source publication time,
and receipt time as separate fields.

Treasury owns the current nominal/real curve and FRED owns its history. BLS
owns CPI/labor release facts; BEA public release pages own GDP, PCE, and core
PCE release facts; FRED owns the matching history. The natural-change contract
is Dataset-specific: daily/weekly gaps are bounded and monthly/quarterly
comparisons require the exact calendar lag. A missing period yields a missing
change, never a mislabeled fallback.

The Cross-Asset payload always owns the fixed ETF basket SPY, QQQ, IWM, TLT,
IEF, LQD, HYG, UUP, GLD, and USO plus ES, NQ, RTY, ZB, ZN, GC, CL, and HG
major-futures rows and the Yahoo DXY index. ETF rows use Nasdaq public daily
history for five-year changes, normalization, and correlations, paired with
Yahoo Finance five-minute prices. Futures pair Yahoo five-minute prices with
Yahoo continuous-contract daily history. Both Yahoo lanes and Nasdaq public
history are explicitly `untrusted_proxy`; each row exposes separate history
and price Dataset IDs, its actual market timestamp, price kind, and source
lineage. A closed or maintenance market preserves the last expected bar as
`current`; staleness is measured against the market clock, never wall-clock
age alone. WTI is the separate official FRED/EIA `DCOILWTICO` benchmark. The Rates
payload exposes Treasury nominal and real maturity cross-sections for current,
1W, 1M, and 3M snapshots, matched breakevens, 2s10s/3m10s/5s30s histories,
and transparent curve-shape inputs. Paid CME probability gaps are not part of
the supported contract.

Volatility exclusively owns the official CFE VX settlement curve. A served
`market_settlement_v2` fact requires the official `Expiration Date`; schema
version and expiration participate in both fact hash and settlement identity.
Provable legacy raw rows receive a new append-only v2 revision while the v1 row
remains unchanged. Unprovable v1 rows stay audit-only and are never sorted by a
guessed contract-code expiry. Cross-Asset does not duplicate this curve.

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
evidence and do not block the current Thesis.

Credit exposes IG/BBB/BB/B/CCC OAS, actual-sample history statistics, IG/HY
effective yields, deterministic comparisons with EFFR and 10Y Treasury, SLOOS
standards and demand for C&I/CRE/consumer, loan delinquency/charge-off facts,
and labelled ETF/CFTC confirmations. Four concurrent credit dimensions are
returned; no composite score exists. Paid TRACE/NAV and unavailable historical
ICE placeholders were deleted from the product contract.

On every U.S. trading session at 08:50 `America/New_York`,
`macro_thesis` seals one cutoff-bounded `macro_evidence_pack_v3` and projects
one immutable `macro_research_input_v1`. Each module contributes at most three
driver candidates, two material changes, two counter-signals, six exact refs,
and four conditions; the global input is capped at 64 exact refs, 32
conditions, and 64 KiB. Input compilation failure is a stable pre-model
`failed` run.

The only production execution path is `macro_thesis_thin_v1`: one
`create_deep_agent` graph invocation and exactly one provider-native structured
model invocation per durable attempt. It has no business tools, subagents,
filesystem, todo, task, execute, search, summarization, checkpoint write,
Reviewer, or revision loop. Provider configuration/authentication, timeout,
refusal, and missing structured mapping are pre-draft run errors.

A provider-success envelope is publishable only through the closed four-gate
set: time identity, evidence closure, contract validity, and write safety.
Their parseable primary order is time, evidence, contract, then write.
Confidence, no-call, history partial, best-effort gaps, report length, Reviewer
absence, and offline evaluation are not additional runtime gates.

The immutable `macro_thesis_v2` contains one call/no-call mainline, one to
three causal edges for a call, at most one alternative, at most three tensions,
sparse material module assessments, sparse material asset outlooks, compiled
citations/conditions, and all twelve deterministic frozen asset snapshots in
stable order. Non-material assets retain canonical momentum/current facts and
a short read-projection no-call reason; the model does not generate 12×2
filler. Existing `macro_thesis_v1` bytes/hashes remain available only through
explicit archive reads.

`macro_live_delta_v2` is an immutable post-publication snapshot whose ID binds
publication and deterministic current-fact input hash. It keeps mainline,
alternative, tension, and asset scopes separate; only mainline metric
conditions determine mainline validity, while event checkpoints use a separate
state. `macro_outcome_replay_v2` uses the same append-only identity rule,
emits only declared 1W/1M horizons (`1w_to_1m` expands to both), and includes
only assets with a corresponding material outlook. Current Recovery is an
independent rebuildable projection. None of these changes the Thesis hash.

With no query, `GET /api/macro/research` targets the current intended 08:50
U.S. trading session and never relabels the previous publication. Optional
`session_date=YYYY-MM-DD` selects one explicit session. This is the same Thesis
product as the overview. Without a date it returns
`macro_thesis_detail_v4` and only a matching current v2 Thesis. With a date it
returns `macro_thesis_archive_detail_v2`, whose state is `historical` or
`missing` and whose Thesis is discriminated as original v1 or v2. Archive
payload/hash identity is unchanged; current Recovery is outside the immutable
payload. Current history items use `macro_publication_history_item_v2`.

The read endpoint does not invoke a model or provider, search facts, resume a
graph, run a repair, or synthesize a fallback publication. Missing remains a
typed successful state and never carries an older Thesis. Unmatched Macro API
paths return the ordinary application `404` response.

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

- service/config: `serve`, `workers`, `init`, `config`;
- database: `db migrate|hard-cut|health|audit|query-audit`;
- Macro: `macro backfill|backfill-professional|status`;
- read models: `recent`, `search`, `asset-flow`;
- maintenance: `ops ...` for explicit repair, rebuild, queue inspection/resolution, and diagnostics.

Mutating maintenance commands require an explicit execution flag where the parser offers a dry-run mode. They operate from persisted facts and stable target keys. A rebuild does not create an alternate generation/run identity or make a provider response the source of truth.

`macro status` resolves the current publication session first and never selects
the latest historical run as a substitute. Its Thesis summary exposes only the
current v2 state and identity; a same-session v1 row is `not_published`.
`offline_evaluation` is a read-only projection over immutable Evidence Packs:
it validates available packs, reports advisory progress toward the nine-real-
session quality corpus or the selected 12 cases, and explicitly returns
`blocks_deployment=false`. It never invokes a model, writes an evaluation row,
or becomes a daily publication or schema-migration gate.

`ops rebuild-market-current --execute` is the bounded, cursor-based repair for
reconstructing `market_tick_current` from persisted `market_ticks`.
News steady recovery re-reads typed identity and stable hourly score-bucket
frontiers and recomputes only affected components or score partitions. The
system-wide maintenance hard cut rebuilds News from persisted items through
the same incremental reducer. Token Radar
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
