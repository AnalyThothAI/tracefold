# Public Contracts

Tracefold exposes one configuration contract, one HTTP service, and one CLI. This document records stable behavior; generated OpenAPI is authoritative for exact HTTP fields.

There are no compatibility aliases for retired products, tables, worker names, routes, or response fields. A behavior change updates source, tests, generated contracts, and this document in the same change.

## Runtime configuration

The active operator-owned application file is
`~/.tracefold/config.yaml`. It contains deployment/domain choices,
PostgreSQL role DSNs and password-file references, the Macro source-family
switches, credentials, API bind/auth, and News settings. Worker topology,
cadence, deadlines, resource limits, batches, leases, retries, and model
reservations are code-owned and are not configuration fields.

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
existing database passwords. The generated config has a new API bearer token
(`ws_token`) but no live provider/model/webhook credential, `news.push.enabled`
is false, and `news.broker.url` points at the compose RabbitMQ service.

The configuration schema is exactly the top-level keys `ws_token`, `api`
(`host`, `port`), `storage.postgres`, `llm`, `providers.macro_sources`, and
`news`, each a typed nested model with `extra=forbid`. `ws_token` is the HTTP
API bearer token; the key name is kept so operator configs need not churn.
Root-level `postgres_*`, `api_*`, provider, and LLM forwarding aliases are not
part of the configuration contract.

`gmgn.*`, `upstream.*`, `providers.binance.*`, `providers.okx`,
`api.heartbeat_interval`, `api.replay_limit`, top-level `handles`, top-level
`notifications`, `news.sources`, `news.rss_enabled`,
`news.title_presentation`, `news.translation`, `news.budget`,
`llm.news_brief_model`, and — with the Analyst lane (issue #57) —
`news.analyst.*` and `llm.news_analyst_model` are retired inputs. Any
equivalent retired key fails validation; there is no alias, merge, or
generated-source fallback. Remove them from an existing operator config before
upgrading.

`llm.api_key`, `llm.base_url`, and `llm.news_triage_model` are one direct
DeepSeek-compatible configuration. They are all absent or all present; a
partial triple fails validation, and Tracefold never supplies an implicit
endpoint or model. `llm.macro_document_analysis_enabled` and
`llm.macro_document_analysis_model` admit the optional Fed document analysis
on the same gateway. The card's Chinese text is the Triage verdict's
`headline_zh` and `why_zh` (with `title_zh` for the console); no other model
or provider produces copy. Model execution policy, timeouts, token budgets,
cadence, retries, and reservations are code-owned. Environment variables are
not a credential contract.

`providers.macro_sources` (`enabled`, `fred_enabled`, `cboe_enabled`,
`cftc_enabled`, `nasdaq_daily_enabled`, `yfinance_enabled`, `user_agent`)
enables the free Macro source families and identifies the client; it owns no
dataset membership, formula, freshness, or schedule.

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
run; `news.broker.name_prefix` prefixes every exchange and queue name and
`news.broker.connect_timeout_seconds` bounds the connect. `news.triage.*`
(`deadline_seconds`, `concurrency`, `circuit_failures`,
`circuit_open_seconds`), `news.push.*` (`enabled`, `feishu_webhook_url`,
optional `feishu_signing_secret`, `min_interval_seconds`, `hourly_cap`), and
`news.watchlist[]` (`{symbol, market_type}`) are the only News knobs.
`news.triage.concurrency` (default 4) is the real consumer width of its queue.
Lexicons, prefix tables, LSH geometry, prompt texts, and policy versions are
code constants. `tracefold config` exposes only redacted booleans, counts,
model names, and watchlist symbols; it never prints the token, broker URL,
keys, or webhook.

Push delivery is available only when `news.push.enabled` is true and the
webhook is a valid Feishu HTTPS custom-bot v2 URL; otherwise Serve and
Workers still start and deliveries settle `terminal/delivery_unavailable`.

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
Worker topology, News broker topology and consumer set, private due loops, the
projection EDF, the serial native-state model arbiter, and all resource
capacities are code-owned. Configuration cannot add another worker or derived
product lane. Explicit Macro backfill is a synchronous CLI maintenance action
and is absent from the steady Workers root.

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

The service exposes `/healthz`, `/readyz`, `/metrics`, static frontend assets
(the console routes `/`, `/app`, `/app/*`, `/news`, `/news/*`, `/macro`,
`/macro/<module>`), and `/api/*`. There is no WebSocket endpoint.

- `/healthz` is process liveness.
- `/readyz` combines a lightweight PostgreSQL liveness check with the cached startup schema/composition result. It does not inspect providers, queues, or business freshness.
- `/api/bootstrap` returns `{ws_token}` so the served console can authenticate; every other `/api/*` route requires that token as an HTTP bearer token (`Authorization: Bearer <ws_token>`; routes that allow it also accept a `token` query parameter). A missing or wrong token is `401`.
- `/api/status` is exactly `{measured_at_ms, runtime}`. `runtime` combines the database probe (schema revision match) with the Workers heartbeat row and fails closed on stale heartbeats; there is no provider block.
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
| Bootstrap/status | `/api/bootstrap`, `/api/status` | Serve configuration, database probe, and the Workers runtime row |
| News | `/api/news/feed`, `/api/news/events/{event_id}`, `/api/news/status` | broker-driven Event feed, one Event with members/verdicts/deliveries/labels, and four-layer News status |
| Macro | `/api/macro/overview` and six typed module routes | persisted six-module current rows built from Macro facts and Fed document analysis |

The public API is exactly these routes plus `/healthz`, `/readyz`, and
`/metrics`. The retired GMGN-lane routes (`/ws`, `/api/recent`,
`/api/events/by-ids`, `/api/search`, `/api/search/inspect`, `/api/token-case`,
`/api/target-posts`, `/api/target-social-timeline`, `/api/live-market`,
`/api/token-images/*`, `/api/token-radar`, `/api/stocks-radar`) are not
registered and answer the ordinary `404`; there is no alias, redirect, or
feature flag.

### News

News is an operator-bound, Strategy-qualified Event surface. The public
surface is exactly three read-only routes:

- `GET /api/news/feed?family={family}&admission={admission}&priority={high|normal}&decision={push|escalate|drop|throttled|degraded}&symbol={symbol}&q={query}&sort={latest|priority}&limit={limit}&cursor={cursor}&outcome={pushed|held|pending}&hours={0..168}`
  returns Events newest first (or high priority first) with the leader title,
  the Triage `title_zh` when a verdict carries one, admission, priority,
  asset class, grounded assets, watchlist hits, storyline key, context line,
  **one `outcome`** (`kind` from the stable enum `held_recovery`, `held_gate`,
  `queued_publish`, `queued_triage`, `dropped`, `throttled`,
  `degraded_dropped`, `pending_delivery`, `delivered`, `delivery_failed`;
  reader copy `text_zh` and `reason_zh`; `group` = `pushed|held|pending`),
  the latest Triage summary (final decision, override rule, throttle reason,
  degraded flag, error code, direction, magnitude, event type, scope,
  `headline_zh`, `title_zh`, `why_zh`, plus Chinese `direction_zh`,
  `magnitude_zh`, `event_type_zh`), and the first delivery state with its
  error code. `outcome` is the feed's task-tab filter (its SQL mirrors the
  outcome groups); `hours` bounds `opened_at_ms` to the last N hours (`0`
  or absent = no bound). Unknown query parameters, invalid admission or
  decision values, and malformed cursors return 400; out-of-pattern
  `priority`/`outcome`/`hours` return 422. Recovery Events are visible with
  `admission=recovery`. `filters` echoes every parameter incl. `outcome` and
  `hours` (never the wall-clock bound, so unchanged pages keep their ETag).
- `GET /api/news/events/{event_id}` returns one Event, its `outcome`, a
  `timeline` (ordered steps `received` → `gate` → `triage` → `decide` →
  `delivery`, each with `title_zh`, `at_ms`, `summary_zh`, and the raw
  `facts` it was built from), its member Items (title, URL, origin,
  publication time, match kind, Jaccard estimate, provenance, description),
  every Triage verdict (model decision, rule baseline, final decision,
  override rule, throttle reason, verdict payload, model, prompt version,
  degraded flag, trace), deliveries, and operator labels (`label_version`,
  `source`, label payload, created time). `tracefold news why` prints the same
  `outcome` sentence and timeline. Unknown ids return 404.
- `GET /api/news/status` returns `state` (`ready`, `warming`, `degraded`,
  `unavailable`), the Workers state, `health` (four thresholded items
  `ingest`/`broker`/`model`/`delivery` with `level` `ok|warn|bad|off`,
  `summary_zh`, `detail_zh`, and `overall`; thresholds are code-owned, see
  `docs/OPERATIONS.md`), `funnel_24h` (`received`, `candidates`, `triaged`,
  `decided_push`, `delivered`, plus `received_1h`/`delivered_1h`),
  `reasons_24h` (`stage` `gate|drop|throttle|push|degraded`, raw `key`,
  `label_zh`, `count`, sorted by count), and four layers: `ingest` (WSS
  connected, last frame/publish, error, configured and provider-enabled
  Strategy IDs, strategy warnings, open incidents, token configured), `broker`
  (configured, connected, per-queue message/consumer counts when observed,
  error code), `pipeline` (events and candidates per hour/day, Triage counts,
  degraded counts incl. `triage_degraded_by_code_24h`, decided pushes,
  throttled, Triage p50/p95, queue lag p95, the Triage model name, and the
  named 24 h maps `suppressed_by_reason`, `dropped_by_rule`,
  `throttled_by_key`, `pushed_by_rule`), and `delivery` (sent/terminal
  counts, last error, end-to-end p95, availability, hourly cap), plus
  `control` (paused, mutes) and the watchlist symbols.

The Chinese vocabulary behind `outcome`, `*_zh`, and `label_zh` lives in
`tracefold.news.outcome` (admissions, `decide()` rules, throttle keys, error
codes, delivery errors, event types, directions, magnitudes, storyline
themes); a new rule or error code lands there in the same change, so no
surface renders a bare key.

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
`novelty` (`new_fact` / `progression` / `restatement`, judged against the told
ledger in the status bar; required in the tool schema, replayed as `new_fact`
for pre-v7 rows), `restates` (told-ledger index a restatement points at, -1
otherwise), `event_type`, `assets[{symbol, market_type?, role}]`, `direction`,
`scope`, `magnitude 0..3`, `actionable`, `confidence`, `decision` (model
intent), `audience`, `headline_zh` (the card header: a complete headline that
keeps the decisive number / condition / consequence clause, prompt target 15–45
characters, at most 60), `title_zh` (faithful Chinese title, console only, at
most 160 characters), `why_zh` (one plain sentence adding mechanism and who is
exposed, prompt target <= 70 characters, at most 140); the stored row adds
`model_decision`, `rule_baseline_decision`, `final_decision`, `override_rule`
(policy v3 adds `restatement` and `novel_bypass`), `throttled_by`
(`storyline:<key>`, `storyline:<key>:cap<N>`, `storyline:<key>:hard<N>`,
`hourly_cap`), `degraded`, `error_code`, `trace` (`prompt_sha256`,
`input_sha256`, `storyline_key_preliminary`, `status`, `status_final`,
`storyline_key`, `told[{i, event_id, at_ms, m, dir, headline_zh}]`,
`told_count`, `restates_event_id`, `reasked_after_told_change`,
`first_verdict`, `first_input_sha256`, `reask_failed`, `novelty_defaulted`,
model telemetry). `triage` is the only
stage written; the retired Analyst lane's `deep` rows survive as history
(issue #57). The current versions are `news_title_norm_v2`, `news_gate_v4`
(lexicon `news_gate_lexicon_v2`), `news_storyline_v2`,
`news_triage_prompt_v7`, `news_triage_policy_v3`, and `news_delivery_card_v8`.
`news.policy` keys are `escalate_magnitude`, `min_push_magnitude`,
`min_watchlist_magnitude`, `unclear_push_min_magnitude`,
`unclear_push_event_types`, `theme_cap_4h`, `storyline_throttle`,
`hourly_cap_enabled`, `restatement_drop`, `novel_min_magnitude`,
`theme_hard_cap_4h` (>= `theme_cap_4h`), `asset_hard_cap_2h`.

Delivery identity is `(event_id, kind)`; `first` is the only kind written —
one Event gets one card — and the retired lane's `followup` rows survive as
history. States are `sending`, `sent`, `terminal`. There is exactly one HTTP
attempt; a paused control settles `terminal/delivery_paused` immediately
instead of holding the message.

Broker contract (code-owned): topic exchange `news`, dead-letter exchange
`news.dlx`, fanout retry exchange `news.retry`, three quorum business queues —
`news.raw` (`raw.#`; single-active, `reject-publish` overflow at 100,000,
delivery limit 3), `news.triage` (`event.#`; delivery limit 3), and
`news.deliver` (`verdict.push`; single-active, delivery limit 1) — plus the
single retry lane `news.retry` (30 s TTL, dead-letters back to `news`) and
`news.dead` (delivery limit 1,000,000 so peeks never drop evidence); all names
take `news.broker.name_prefix`. Declaring the topology deletes the retired
Analyst queue `news.deep` (issue #57). Message bodies are `news_bus_v1` JSON
envelopes (`schema_version`, `kind`, `message_id`, `trace_id`, `occurred_at_ms`,
`payload`) with AMQP priority 0 or 5 and `x-news-attempt`/`x-news-trace`
headers. Consumer outcomes are typed: `TransientError` retries through the
lane and dead-letters after 3 attempts, `DeferError` requeues uncounted when
the News DB lane cannot admit the message, and `PermanentError` or a handler
crash dead-letters. Control is not a broker message: `tracefold news control
<pause_delivery|resume_delivery|mute_theme|mute_symbol|unmute> [--key
--ttl-minutes]` writes `news_control_state` and consumers read that row on
every message.

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
not come from YAML. General cross-asset observations, settlements, and
positioning are Macro's general market facts (`market_instruments`,
`market_observations`, `market_settlements`, `market_position_facts`);
macroeconomic series, release events, and official documents are `macro_*`
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

The Alembic chain is `20260818_0275` (the root baseline: it executes
`current_schema_20260818_0275.sql` plus `runtime_roles.sql`) followed by
`20260818_0276_review_49_hard_cut` (drops the retired News title table, the
DEX discovery/token-profile/token-image tables, and the unused LangGraph
`checkpoint_*` tables) and `20260818_0277_gmgn_lane_removal` (drops the
social evidence, token identity/registry, DEX/CEX market, live broadcast,
provider circuit, and News market-mark tables). A database at `20260818_0276`
upgrades with `tracefold db migrate`; a fresh database runs the baseline, 0276,
and 0277. The resulting schema is exactly 28 tables. News owns exactly eleven:
`news_ingest_state`, `news_opennews_incidents`, `news_items`, `news_events`,
`news_event_members`, `news_event_bands`, `news_event_assets`,
`news_verdicts`, `news_deliveries`, `news_control_state`,
`news_event_labels`. Migrations perform no provider, broker, or outbound call
and have no compatibility reader/writer; the Feed starts from the first frames
observed after deployment.

## CLI

`uv run tracefold --help` is the exact CLI source of truth. Stable top-level families are:

- service/config: `serve`, `workers`, `init`, `config`;
- database: `db migrate|health|audit|query-audit`;
- Macro: `macro backfill|backfill-professional|status`;
- News: `news bus-check|control|label|eval|replay-decisions|replay|dlq|why`;
- maintenance: `ops queue-inspect|queue-resolve|queue-resolve-bucket|validate-projections`.

There is no `recent` or `search` command and no market rebuild/sync/reconcile
maintenance command. Mutating maintenance commands require an explicit
execution flag where the parser offers a dry-run mode. They operate from
persisted facts and stable target keys. A rebuild does not create an alternate
generation/run identity or make a provider response the source of truth.

`queue-inspect` and `validate-projections` are strict Serve-role reads. They
do not acquire the maintenance lock, so operators can inspect the running
singleton without interrupting it. Queue resolution commands remain exclusive
maintenance operations.

`db audit` reports the migration revision, row `counts` for the Macro core
tables (`macro_series_facts`, `macro_release_facts`, `macro_documents`,
`macro_document_analyses`, `macro_module_current`, `market_instruments`,
`market_observations`, `market_settlements`, `market_position_facts`), and
`news_schema` exactness over the eleven `news_*` tables. `db query-audit`
covers `/readyz`, `/api/status`, `/api/news/*`, and `/api/macro/*`;
`/healthz`, `/metrics`, and `/api/bootstrap` are declared no-SQL routes.

`news bus-check` connects, declares the topology idempotently, and prints
per-queue message/consumer counts. `news control <action> [--key
--ttl-minutes]` writes `news_control_state` through the Workers role. `news
label <event_id> <good|noise|late|wrong_direction|dup|missed> [--note]`
inserts one `news_event_labels` row (`source` `human`, `label_version`
`news_label_v1`) on any Event, including Gate-suppressed, dropped, or
throttled ones. `news eval --hours --policy-version` scores every Event of the
window against operator labels only (`good`/`wrong_direction`/`late`/`missed`
count as moved, `noise`/`dup` as flat; an Event without a verdict counts as
`suppressed`): `precision_at_push`, `missed_rate`, `false_push_rate`,
`missed_movers_rate`, `suppressed_movers_rate`, `throttled_movers_rate`,
per-admission, `override_rule`, `throttled_by`, asset-class, audience, and
event-type confusion tables, and storyline statistics. `news
replay-decisions --hours [--escalate-magnitude --min-push-magnitude
--min-watchlist-magnitude --theme-cap-4h --theme-hard-cap-4h
--asset-hard-cap-2h --novel-min-magnitude --no-restatement-drop
--no-storyline-throttle --no-unclear-push]` re-runs `decide()` over stored
verdicts with a candidate `DecidePolicy` (unspecified values come from
`news.policy`) and no model call, reporting `restatement_drops` and
`novel_bypass` alongside the changed decisions. `news replay <hits.json> [--gate-policy config|open|strict]` runs
Deduper+Gate over saved provider hits without broker or model and lists every
Event with admission, grounded assets, and preliminary storyline. `news why
<event_id>` prints the Event's chain (item, gate, triage, decide, delivery)
and a one-line `outcome`. `news dlq inspect|replay|purge [--limit]`
peeks, republishes, or purges `news.dead`.

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

The `ops` family is exactly `queue-inspect`, `queue-resolve`,
`queue-resolve-bucket`, and `validate-projections`. Queue owners are
`macro_document_analysis` (`macro_document_analysis_jobs`) and
`macro_projection` (`macro_module_frontiers`). Each command constructs only the
dependencies required by the named domain operation and invokes that bounded
operation directly; there is no generic one-shot worker adapter or free-form
result object.

Queue resolution is auditable: retry mutates the source queue and resolves terminal evidence in one transaction; quarantine/archive resolves the terminal row without pretending the source work succeeded.

## Contract change discipline

For a public contract change:

1. change the owning domain/application behavior;
2. add a behavior or contract test;
3. update Pydantic/OpenAPI/frontend types when the HTTP shape changes;
4. update this document and the relevant domain architecture map;
5. remove the old name/path instead of adding an alias or dual read/write.

Historical dated audits explain why a hard cut happened; they are not a second runtime specification.
