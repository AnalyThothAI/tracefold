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

Repository fixtures, `.env` files, and generated docs are not runtime
configuration. No static example is a second schema authority: the
`tracefold init` command generates the default directly from the typed settings
implementation.
`uv run tracefold config` reports the effective paths and redacted settings.
Unknown settings or worker keys fail validation.

`tracefold init` creates the operator directory, config, cache/log directories,
and bootstrap/Serve/Workers/migrate password files. The operator directory is
mode `0700`; config and password files are `0600`. A normal rerun preserves
existing config and password contents while repairing permissions.
`tracefold init --force` replaces only `config.yaml`; it does not rotate
existing database passwords. The generated config has a new WebSocket token
but no live provider/model/webhook credential, and `news.push.enabled` is
false.

The configuration schema uses typed nested models directly
(`storage.postgres`, `api`, `llm`, `gmgn`, `providers.*`, and `upstream`).
Root-level `postgres_*`, `api_*`, provider, LLM, and upstream forwarding
aliases are not part of the configuration contract.

Top-level `handles`, top-level `notifications`, and `news.sources` are retired
inputs. Any equivalent retired key fails
validation; there is no alias, merge, or generated-source fallback.

`llm` contains operator-owned provider credentials: `api_key` and `base_url`
for the current OpenAI-compatible provider plus optional
`openrouter_api_key` and `groq_api_key` for the News fallback chain. They are
consumed only by enabled AI workers. Model execution policy, timeouts, token
budgets, cadence, leases, retries, and reservations are code-owned.
Environment variables are not a credential contract.

`news.opennews_token` is the operator-owned secret for the production News
source. It is reported only as the redacted boolean
`news.opennews_token_configured`. When absent, News reports
`opennews_token_missing`; no substitute credential path is used.

`news.push` contains `enabled`, `feishu_webhook_url`, and
optional `feishu_signing_secret`. Enabling push requires the webhook, which
must be the supported Feishu HTTPS custom-bot v2 boundary. If a non-empty
signing secret is present, delivery includes the computed `timestamp` and
`sign`; if absent, delivery is unsigned and includes neither field.
Diagnostics expose only `feishu_webhook_url_configured` and
`feishu_signing_secret_configured`. Each frozen internal delivery envelope
contains the non-secret `auth_mode` (`signed` or `unsigned`) so a retry cannot
change modes; it never contains the webhook, secret, timestamp, or signature.
Threshold, translator model, cadence, deadlines, retries, and card policy are
code-owned. The Feishu JSON 2.0 card's only visible content is a plain-text
header title. A zero-width plain-text body element is retained solely because
the Feishu card protocol rejects an empty body.
When the selected highest-score Item has valid OpenNews coin symbols, the title
prefixes their provider order after case-insensitive deduplication, for example
`[NEAR · BTC] 中文标题`; otherwise it is only the translated headline when
available or the original headline. It has no visible body, subtitle, separate
metadata, or link button. Translation reuses
`llm.api_key` and `llm.base_url`; there is no second model credential or
Google-translation fallback.

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
Worker topology, private due/periodic loops, the projection EDF, the serial
native-state model arbiter, and all resource capacities are code-owned.
Configuration cannot add another worker or derived product lane. Explicit
Macro backfill is a synchronous CLI maintenance action and is absent from the
steady Workers root.

## Operator lifecycle

The fresh-clone operator contract is `make up`. It preflights `uv`, Docker,
Compose, `curl`, and daemon access; runs idempotent initialization; builds the
frontend and backend image; performs fresh-volume role bootstrap; runs the
one-shot migration; starts Serve and Workers; and waits for required health and
console boundaries. A repeated invocation preserves config, passwords, and
named-volume data.

`make status` fails non-zero when PostgreSQL, migration, Serve, Workers, either
runtime readiness endpoint, or console HTML is missing or unhealthy.
`make logs` follows the bounded startup services. `make down` stops the stack
without deleting the named PostgreSQL volume. These targets do not auto-hard-cut
an unknown non-empty database.

## HTTP

The service exposes `/healthz`, `/readyz`, `/metrics`, `/ws`, static frontend assets, and `/api/*`.

- `/healthz` is process liveness.
- `/readyz` combines a lightweight PostgreSQL liveness check with the cached startup schema/composition result. It does not inspect providers, queues, or business freshness.
- `/api/status` combines the serve startup/schema snapshot with persisted
  singleton `workers_runtime`; stale worker heartbeats fail closed as unavailable.
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
| Macro | `/api/macro/overview` and six typed module routes | persisted six-module current rows built from Macro/Market facts and Fed document analysis |
| News | `/api/news/feed`, `/api/news/stories/{story_id}`, `/api/news/brief`, `/api/news/sources`, `/api/news/status` | OpenNews current items and source health, deterministic Story read model, and immutable Chinese Brief |
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
  `has_more`. Each Story carries nullable `provider_evidence`, selected by the
  backend from the member with the maximum numeric OpenNews provider score and
  deterministic publication-time/Item-ID ties. It binds that Item ID, URL, and
  bounded provider metadata together; the browser does not cluster, score,
  select the maximum, or reorder.
- `GET /api/news/stories/{story_id}` returns one current Story and its complete
  NewsItem evidence. It exposes representative/scoring item identity,
  title/reporting-origin/time, classification, reporting-origin count,
  importance score, and the transparent factor breakdown. Member and
  representative URLs are nullable for linkless dispatches. An expired Story
  ID returns not found; there is no archived Story contract, revision timeline,
  or per-Story AI analysis.
- `GET /api/news/brief` returns one current Chinese World Brief, its truthful
  state, selected Story evidence, bounded immutable publication history, and
  latest run when present. Insufficient material makes no model call. A failed
  update preserves the last-known-good publication as `stale_fallback`.
- `GET /api/news/sources` returns the one code-owned OpenNews source, its
  memberships, live connection, last recovery, current error, and unclosed-gap
  status.
- `GET /api/news/status` derives warming/ready/degraded News health from
  PostgreSQL source, Story-invariant, Brief, and outbound push state.

`/api/news/feed` and `/api/news/brief` emit an ETag, honor
`If-None-Match` with `304`, and use `Cache-Control: private, no-cache`.
Every read is PostgreSQL-only: it never fetches a source, calls a model,
reclusters, or repairs state.

NewsItem identity is `(source_id, provider_record_id)`. OpenNews reports upsert
the current title, description, link, reporting origin, publication time, and
bounded provider metadata (provider-source label, `score`, `signal`, `grade`,
and coin details).
Provider annotations merge metadata into the same current row; translations
and non-news messages are discarded. Provider metadata is descriptive and
does not affect Story identity, classification, importance, Feed ordering, or
Brief. A numeric provider score may qualify the already projected Story for
the separate outbound push state machine. Story identity is the full SHA-256
of the earliest normalized title in
the current WorldMonitor-compatible 96-hour cluster. Corroboration counts
distinct reporting origins, not acquisition paths, memberships, or repeated
observations. Keyword classification and importance are deterministic and
fully sufficient without AI.

The native Brief candidate calls no model when fewer than three Stories or fewer than two
reporting origins are available, or when its ordered Top-8 Story fingerprint is
unchanged. A new fingerprint permits one attempt per configured provider,
bounded by 60 seconds total. Publications are Chinese and citation-index
locked: line `[n]` always refers to selected Story `n`. Invalid lines are
repaired locally without shifting indexes. The current pointer changes only
after a complete valid publication transaction succeeds.

`/api/news/status` exposes four independent News health layers: `ingest`,
`story`, `brief`, and `push`. Push reports disabled/configured, baseline,
pending/retry/terminal counts, latest explicit delivery, and bounded sanitized
error evidence without exposing secrets or card content. Deterministic Story
cards remain readable while Brief or push is unavailable.

Production News uses one code-owned OpenNews WSS stream plus bounded,
gap-triggered REST recovery after initial connection, reconnect, or queue
overflow. A healthy WSS session makes no periodic REST call, and Workers
schedule no second acquisition lane. Persisted source state enforces a five-minute
minimum interval between recovery attempts. One recovery reads sequential
100-item pages from page 1, stops when it finds the persisted boundary, and is
capped at 11 pages; the 31-day theoretical ceiling is 98,208 REST calls. An
exhausted page budget keeps the gap open instead of claiming complete recovery.
Gap closure is fenced by the persisted gap version.

There is no `/api/news/stories` collection, `view=latest|priority`, Brief
history route, analysis request route, item route, News WebSocket payload,
inbound/public webhook route, compatibility alias, or alternate clustering
path. Feishu is a Workers-only outbound Adapter.

### Macro

Macro exposes one overview and six typed current-module reads:

```text
/api/macro/overview
/api/macro/rates-fed
/api/macro/economy-inflation
/api/macro/liquidity-funding
/api/macro/credit
/api/macro/volatility
/api/macro/cross-asset
```

These reads accept no query parameters. The overview is
`macro_overview_v9`: it returns read time, transport state, latest fact time,
six module availability summaries, and aggregate data quality. It is not a
daily narrative or historical-session product. Each module route returns its
matching persisted schema or `macro_module_unavailable_v1` with a typed reason:

- overview: `macro_overview_v9`
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
History Depth. One missing or schema-mismatched module produces a typed
unavailable slot without failing the other five. All seven reads use
`macro_module_current` only; they never call a provider/model, advance a
target, rebuild a projection, or write fallback content.

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
labelled inputs. Values from different identities are never averaged or
silently substituted. Release facts preserve actual, expected, surprise,
revision, reference date, optional source publication time, and ingestion time
as separate fields.

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
`macro_document_analysis` native candidate writes one immutable, model/prompt-versioned,
exact-evidence-bound analysis per source body after effective-dated role facts
are available. Institutional FOMC stance and the 90-day officials
communication distribution remain separate. Non-policy material is
`not_policy_signal`; no static official label or universal hawk/dove score
exists. The current immutable-analysis admission window is 550 days for FOMC
materials and 120 days for speeches. Older official bodies remain durable raw
evidence but do not block current module reads.

Credit exposes IG/BBB/BB/B/CCC OAS, actual-sample history statistics, IG/HY
effective yields, deterministic comparisons with EFFR and 10Y Treasury, SLOOS
standards and demand for C&I/CRE/consumer, loan delinquency/charge-off facts,
and labelled ETF/CFTC confirmations. Four concurrent credit dimensions are
returned; no composite score exists. Paid TRACE/NAV and unavailable historical
ICE placeholders were deleted from the product contract.

Macro has no second judgment, historical-session, or archive contract. Retired
paths return the ordinary application `404`; there is no alias or fallback
publication.

Migrations `20260801_0235` and `20260801_0236` are irreversible: they remove
retired News acquisition and Macro derived/control history while preserving
current items, material facts, acquisition targets, Fed document analysis, and
the six module rows.
`20260801_0237` adds the durable OpenNews recovery boundary, and
`20260801_0238` adds the News push baseline and delivery ledger. Applying either
migration does not send a message; delivery begins only after an explicit
webhook-backed push configuration and the first enabled reconcile establishes
its baseline. Signing is optional.

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

`db hard-cut --execute` is the current operator-authorized in-place migration
path. It requires the legacy bootstrap DSN/password file, refuses active
Tracefold runtime sessions, and has no snapshot-confirmation or restore flag.
Failure stays inside the maintenance boundary and is repaired forward on the
current database.

`ops collect-workers-runtime-acceptance --bundle <absolute-path>` is a
read-only production observer with a fixed 1,800-second interval, 181 samples,
10-second cadence, and 15-second maximum gap. It accepts exactly one new
directory outside the checkout and returns non-zero while preserving raw JSONL
if any continuity, identity, capacity, PostgreSQL, resource, or query-plan gate
fails. `ops seal-workers-runtime-acceptance` accepts that repository-owned
collection only after the other typed gates and independent review are bound.

`macro status` reports the bounded acquisition target count/statuses, each of
the six module current rows with its health, history depth, fact cutoff, and
update time, plus Fed document-analysis job counts. It is a PostgreSQL-only
diagnostic: it invokes no provider/model and writes nothing.

`ops rebuild-market-current --execute` is the bounded, cursor-based repair for
reconstructing `market_tick_current` from persisted `market_ticks`.
News steady state and explicit maintenance use the same complete current
96-hour WorldMonitor calculation from persisted NewsItems. Token Radar
contract and distribution checks use `projection-status`,
`validate-projections`, and `factor-diagnostics`; the CLI does not carry a
second copy of the factor contract.

One-shot maintenance commands construct only the dependencies required by the
named domain operation and invoke that bounded operation directly. The
application adapter owns provider/database cleanup and returns exactly
`operation`, `processed`, `failed`, `terminal`, `skipped`, and `preparation`.
`operation` is `resolution_refresh`, `asset_profile_refresh`, or
`token_image_mirror`; counters are non-negative integers and `preparation` is
an object or null. There is no generic result object, free-form notes, or
retired `dead`/`worker_name` field.

Queue resolution is auditable: retry mutates the source queue and resolves terminal evidence in one transaction; quarantine/archive resolves the terminal row without pretending the source work succeeded.

## Contract change discipline

For a public contract change:

1. change the owning domain/application behavior;
2. add a behavior or contract test;
3. update Pydantic/OpenAPI/frontend types when the HTTP shape changes;
4. update this document and the relevant domain architecture map;
5. remove the old name/path instead of adding an alias or dual read/write.

Historical dated audits explain why a hard cut happened; they are not a second runtime specification.
