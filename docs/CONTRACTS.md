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
but no live provider/model/webhook credential, `news.push.enabled` is
false, and `news.broker.url` points at the compose RabbitMQ service.

The configuration schema uses typed nested models directly
(`storage.postgres`, `api`, `llm`, `gmgn`, `providers.*`, and `upstream`).
Root-level `postgres_*`, `api_*`, provider, LLM, and upstream forwarding
aliases are not part of the configuration contract.

Fresh configs subscribe GMGN to `sol`, `eth`, `base`, `bsc`, and `robinhood`.
The default OKX DEX discovery/quote set includes chain index `4663`, whose
canonical Tracefold chain ID is `robinhood`; no runtime alias or inferred
fallback supplies that mapping.

Top-level `handles`, top-level `notifications`, `news.sources`,
`news.rss_enabled`, `news.title_presentation`, and `llm.news_brief_model` are
retired inputs. Any equivalent retired key fails validation; there is no
alias, merge, or generated-source fallback.

`llm.api_key`, `llm.base_url`, and `llm.news_triage_model` are one direct
DeepSeek-compatible configuration. They are all absent or all present; a
partial triple fails validation, and Tracefold never supplies an implicit
endpoint or model. `llm.news_analyst_model` defaults to the triage model. News
title translation uses the ordered `news.translation.deepl_api_keys` first and
this direct slot as fallback. Model execution policy, timeouts, token budgets,
cadence, retries, and reservations are code-owned. Environment variables are
not a credential contract.

`news.opennews_token` is the operator-owned secret for the production News
source. `news.opennews_strategy_ids` is the operator-owned, duplicate-free
allowlist of account Strategy IDs; IDs are trimmed opaque strings and mutable
Strategy names are never admission keys. When News is enabled, a configured
token with an empty Strategy set fails closed. Workers compare the configured
list with the provider Strategy list at startup: configured-but-disabled and
enabled-but-unconfigured IDs become `strategy_warnings` in `/api/news/status`;
they never fail startup or recovery. The current operator set is `1018`,
`1352`, `1353`; `1019` is disabled provider-side and not configured.

`news.broker.url` (`amqp://` or `amqps://`, a secret) is required for News to
run; `news.broker.name_prefix` prefixes every exchange and queue name.
`news.gate.*`, `news.triage.*` (`deadline_seconds`, `concurrency`,
`circuit_failures`, `circuit_open_seconds`), `news.analyst.*` (`enabled`,
`deadline_seconds`, `max_steps` (must not equal 25), `concurrency`),
`news.push.*` (`enabled`, `feishu_webhook_url`, optional
`feishu_signing_secret`, `min_interval_seconds`, `hourly_cap`),
`news.budget.daily_model_cost_usd`, and `news.watchlist[]`
(`{symbol, market_type, weight}`) are the only News knobs. Lexicons, prefix
tables, LSH geometry, prompt texts, and policy versions are code constants.
`tracefold config` exposes only redacted booleans, counts, model names, and
watchlist symbols; it never prints the token, broker URL, keys, or webhook.

Push delivery is available only when `news.push.enabled` is true and the
webhook is a valid Feishu HTTPS custom-bot v2 URL; otherwise Serve and
Workers still start and deliveries settle `terminal/delivery_unavailable`.

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
Worker topology, News broker topology and consumer set, private due/periodic
loops, the projection EDF, the serial native-state model arbiter, and all
resource capacities are code-owned. Configuration cannot add another worker
or derived product lane. Explicit
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
- `/api/status` separates process/database/Workers runtime truth from Provider
  operations. `runtime` fails closed on stale worker heartbeats; `providers`
  reports configured ownership, durable circuit state, continuous-source
  freshness, and owned or unowned queue backlog without calling an upstream.
- Read endpoints do not call providers, execute models, mutate facts, or rebuild projections.

Status contains no provider/model credentials, base URLs, request policy,
capacity counters, prompt contents, or raw model responses. Code-owned
prompt/policy versions may accompany bounded verdict telemetry; they are
not operator configuration.

API responses use a typed envelope:

```json
{"ok": true, "data": {}}
```

Errors use `ok: false` with a stable error code. Pydantic response models generate `docs/generated/openapi.json` and `web/src/lib/types/openapi.ts`; frontend code consumes those generated types.

### Endpoint families

| Family | Routes | Source of data |
|---|---|---|
| Bootstrap/status | `/api/bootstrap`, `/api/status` | runtime composition, worker status, and persisted Provider operations |
| Events | `/api/recent`, `/api/events/by-ids` | persisted event/evidence facts |
| Search/case | `/api/search`, `/api/search/inspect`, `/api/token-case`, `/api/target-posts`, `/api/target-social-timeline` | Evidence, identity, profile, and market facts owned by those readers |
| Market | `/api/live-market` | stable PostgreSQL current read models |
| Macro | `/api/macro/overview` and six typed module routes | persisted six-module current rows built from Macro/Market facts and Fed document analysis |
| News | `/api/news/feed`, `/api/news/events/{event_id}`, `/api/news/status` | broker-driven Event feed, one Event with members/verdicts/deliveries/marks, and four-layer News status |
| Images | `/api/token-images/{image_id}` | ready mirrored assets under the operator cache root |

There is no CEX OI/detail product API. Generic exchange facts and provider adapters remain internal inputs to supported products.

### Live market

`GET /api/live-market?target_type=…&target_id=…` reads the durable
`market_tick_current` row for one `Asset` or `CexToken` target and returns one
`LiveMarketData` object (`status` is `live`, `stale`, or `missing`). It never
calls a provider. Both query parameters are required and are validated exactly.

`/api/token-radar` and `/api/stocks-radar` are removed and return `404`; there
is no compatibility alias, redirect, feature flag, or replacement Radar
contract. Search and Token Case are the only token research readers. The
retained US-equity identity catalog is an internal collision guard for token
resolution, not a public Stocks interface.

### News

News is an operator-bound, Strategy-qualified Event surface. The public
surface is exactly three read-only routes:

- `GET /api/news/feed?family={family}&admission={admission}&priority={high|normal}&decision={push|escalate|drop|throttled|degraded}&symbol={symbol}&q={query}&sort={latest|priority}&limit={limit}&cursor={cursor}`
  returns Events newest first (or high priority first) with the leader title,
  the shared display title when a presentation exists, admission, priority,
  asset class, grounded assets, watchlist hits, storyline key, context line,
  the latest Triage summary (final decision, override rule, throttle reason,
  degraded flag, direction, magnitude, event type, scope, `headline_zh`), and
  the first delivery state. Unknown query parameters, invalid admission or
  decision values, and malformed cursors return 400. Recovery Events are
  visible with `admission=recovery`.
- `GET /api/news/events/{event_id}` returns one Event, its member Items
  (title, URL, origin, publication time, match kind, Jaccard estimate,
  provenance, description), every Triage/Analyst verdict (model decision, rule
  baseline, final decision, override rule, throttle reason, verdict payload,
  model, prompt version, degraded flag, trace), deliveries, the shared
  presentation, and market marks. Unknown ids return 404.
- `GET /api/news/status` returns `state` (`ready`, `warming`, `degraded`,
  `unavailable`), the Workers state, and four layers: `ingest` (WSS connected,
  last frame/publish, error, configured and provider-enabled Strategy IDs,
  strategy warnings, open incidents, token configured), `broker` (configured,
  connected, per-queue message/consumer counts when observed, error code),
  `pipeline` (events and candidates per hour/day, Triage/Analyst counts,
  degraded counts, decided pushes, throttled, Triage p50/p95, model names), and
  `delivery` (sent/terminal counts, last error, end-to-end p95, availability,
  hourly cap), plus `control` (paused, mutes) and the watchlist symbols.

`/api/news/feed` and `/api/news/events/{event_id}` emit strong ETags and
honor `If-None-Match`; `/api/news/status` uses a weak ETag that ignores
`measured_at_ms`. All News routes require the operator token.

Item identity is `sha256(source_id, params.id)`; `params.strategy.id` is
provenance, not fact identity. Event identity is the leader Item id; Events
merge Items by exact comparison fingerprint or MinHash/LSH near-duplicate
(estimated Jaccard >= 0.55 with strong-fact compatibility) inside the family
window (market telemetry 2 h, disaster 6 h, filing 72 h, general 12 h).
Fingerprints of at most two tokens never share an Event.

Verdict identity is `(event_id, stage, policy_version)`. `TriageVerdict` is
`event_type`, `assets[{symbol, market_type?, role}]`, `direction`, `scope`,
`magnitude 0..3`, `actionable`, `confidence`, `decision` (model intent),
`headline_zh`, `rationale`; the stored row adds `model_decision`,
`rule_baseline_decision`, `final_decision`, `override_rule`, `throttled_by`,
`degraded`, `error_code`, `trace`. `AnalystVerdict` is `agrees_with_triage`,
`revised_direction`, `revised_magnitude`, `novelty_assessment`,
`market_reaction[]`, `context_evidence[]`, `thesis_zh`, `risks_zh`,
`follow_up_needed`, `confidence`; it is stored only after `verify_verdict()`
or as `degraded`.

Delivery identity is `(event_id, kind)` with `kind` in `first`, `followup`;
states are `sending`, `sent`, `terminal`. There is exactly one HTTP attempt.

Broker contract (code-owned): topic exchange `news`, dead-letter exchange
`news.dlx`, fanout `news.control`, quorum queues `news.raw` (single-active,
`reject-publish` overflow), `news.triage`, `news.translate`, `news.deep`,
`news.deliver` (single-active, delivery limit 1), `news.dead`, and retry lanes
`news.retry.5s|30s|120s`; all names take `news.broker.name_prefix`. Message
bodies are `news_bus_v1` JSON envelopes (`kind`, `message_id`, `trace_id`,
`occurred_at_ms`, `payload`) with AMQP priority 0 or 5. Control payloads are
`{action: pause_delivery|resume_delivery|mute_theme|mute_symbol|unmute|drain,
key?, ttl_ms?}`.

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
- `macro_rates_fed_v8`
- `macro_economy_inflation_v6`
- `macro_liquidity_funding_v5`
- `macro_credit_v7`
- `macro_volatility_v7`
- `macro_cross_asset_v8`

The five non-rates modules share identity, clocks, status, summary,
contradictions, falsifiers, checkpoints, and evidence lineage. Rates v8
deliberately has no generic `summary`, `top_changes`, contradiction, or
falsifier fields. Its `decision` contract is tenor-native: 2Y/10Y/30Y current
facts, actual baseline dates for 1D/1W/MTD/3M/past-30-day changes,
session-completeness state, 2s10s/10s30s summaries, same-day 10Y/30Y
nominal-real-Breakeven decomposition, window-qualified classifications, and
fact references. It additionally exposes one revisioned official FOMC meeting
calendar and recent typed Treasury auction results. Bill discount rate,
investment rate, and high yield are three independent nullable fields; the
service never collapses them into one first-available value. Bid-to-cover,
offering amount, and indirect/direct/primary-dealer award shares remain
separate facts. Treasury
completed-session curves are decision-primary; FRED
single-tenor series are history/reconciliation only. Treasury cross-sections,
Fed events, credit ladders, and the ETF comparison matrix are explicit typed
fields, not generic chart arrays. Coverage is `complete` or `partial`; Current
Health is `current`, `degraded`, or `unavailable`; rates session completeness
is an independent `complete`, `unaligned`, or `incomplete` axis. History Depth
is `complete`, `partial`, `insufficient`, or `not_required`. Each Dataset
additionally exposes market state and source state. Optional history cannot
lower Current Health. Only declared required windows affect reader-facing
History Depth. One missing or schema-mismatched module produces a typed
unavailable slot without failing the other five. All seven fact payloads use
`macro_module_current` only; the Rates read additionally attaches the
secret-free optional-analysis runtime state from Serve configuration. Reads
never call a provider/model, advance a target, rebuild a projection, or write
fallback content.

Economy v6 adds one required `seasonal_adjustment` enum to every official
release observation. The value is Registry-owned source metadata
(`seasonally_adjusted`, `not_seasonally_adjusted`, or
`seasonally_adjusted_annual_rate`); it is never inferred from the number.
Cross-Asset v8 publishes pair facts for 30, 90, and 252 common daily-return
observations plus a server-owned `correlation_contract`. The browser uses its
default window, minimum common-observation count, supported windows, and
mirrored-matrix presentation rule; it does not invent a correlation default or
persist duplicate reverse/diagonal facts.

Each successful module representation has a weak semantic `ETag` and
`Cache-Control: private, no-cache`; an unchanged `If-None-Match` read returns an
empty `304`. The weak validator safely spans identity and gzip transfer
representations; responses above the transport threshold are gzip-compressed. The
overview remains a read-time snapshot because `read_at_ms` changes per read.

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
transparent curve-shape inputs, the official FOMC schedule snapshot, Treasury
auction-demand facts, and the shared SOFR fact. Paid CME probabilities are not
part of the supported contract and no probability proxy is synthesized.

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

Document analysis is a supporting capability. Missing, disabled, or
unconfigured analysis cannot lower official Rates/Fed Current Health; the Fed
stance/distribution remains typed `no_call`. The Rates read adds a secret-free
`document_analysis_runtime` state (`disabled`, `unconfigured`, or `active`)
from Serve runtime configuration, while the persisted v7 module remains a
deterministic fact projection. `active`/`worker_active` means only that the
configuration admission conditions are satisfied (`enabled && configured`);
it is not a worker heartbeat or process-liveness claim. Successful immutable
analysis publication and its Dataset/frontier advancement are atomic.

Credit exposes IG/BBB/BB/B/CCC OAS, actual-sample history statistics, IG/HY
effective yields, deterministic comparisons with EFFR and 10Y Treasury, SLOOS
standards and demand for C&I/CRE/consumer, loan delinquency/charge-off facts,
and labelled ETF/CFTC confirmations. Four concurrent credit dimensions are
returned; no composite score exists. Paid TRACE/NAV and unavailable historical
ICE placeholders were deleted from the product contract.

Macro has no second judgment, historical-session, or archive contract. Retired
paths return the ordinary application `404`; there is no alias or fallback
publication.

Migration history for the retired Story/Brief/RSS/Item-Push/Title lane
(`20260801_0234` .. `20260815_0273`) is documented in the alembic version
files only. `20260818_0275_news_v3_event_bus_hard_cut` is the current
irreversible News migration: it drops `news_sources`, `news_stories`,
`news_story_members`, `news_projection_summary`,
`news_brief_selection_current`, `news_brief_current`, `news_push_state`,
`news_push_deliveries`, `news_item_title_presentations`, the legacy
`news_items`, and the legacy incident table, and creates the thirteen V3
tables. It performs no provider, broker, or outbound call and has no
compatibility reader/writer; the Feed starts from the first frames observed
after deployment.

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
`live_market_update`. Token Case may subscribe only its active target.

Worker progress is recovered by bounded database catch-up. Provider frames are never emitted as business facts before persistence.

## CLI

`uv run tracefold --help` is the exact CLI source of truth. Stable top-level families are:

- service/config: `serve`, `workers`, `init`, `config`;
- database: `db migrate|health|audit|query-audit`;
- Macro: `macro backfill|backfill-professional|status`;
- News: `news bus-check|control|eval|replay`;
- read models: `recent`, `search`;
- maintenance: `ops ...` for explicit repair, rebuild, queue inspection/resolution, and diagnostics.

Mutating maintenance commands require an explicit execution flag where the parser offers a dry-run mode. They operate from persisted facts and stable target keys. A rebuild does not create an alternate generation/run identity or make a provider response the source of truth.

`queue-inspect`, `validate-projections`, and
`audit-token-intent` are strict Serve-role reads.
They do not acquire the maintenance lock, so operators can inspect the running
singleton without interrupting it. Repair and rebuild commands remain
exclusive maintenance operations.

`ops collect-workers-runtime-acceptance --bundle <absolute-path>` is a
read-only production observer with a fixed 1,800-second interval, 181 samples,
10-second cadence, and 15-second maximum gap. It accepts exactly one new
directory outside the checkout and returns non-zero while preserving raw JSONL
if any continuity, identity, capacity, PostgreSQL, resource, or query-plan gate
fails. `ops seal-workers-runtime-acceptance` accepts that repository-owned
collection only after the other typed gates and independent review are bound.

`macro status` separates steady acquisition from explicit maintenance. The
steady summary reports actionable due work, future schedules, active and
expired claims, status counts, and current error-code counts; maintenance
reports every explicit backfill target and its claim state separately,
so a stopped or failed historical backfill is never presented as live Worker
backlog. It also reports each of the six module current rows with its health,
history depth, fact cutoff, and update time, Fed document-analysis job counts,
and the secret-free analysis runtime state (`enabled`, gateway `configured`,
configuration-derived `worker_active`, and model name). It invokes no
provider/model and writes nothing; `worker_active` is admission state, not
observed process liveness.

`ops rebuild-market-current --execute` is the bounded, cursor-based repair for
reconstructing `market_tick_current` from persisted `market_ticks`.
News steady state and explicit maintenance use the same complete current
12-hour WorldMonitor calculation from persisted Strategy-admitted and optional
RSS NewsItems.

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
