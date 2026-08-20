# Public Contracts

Tracefold exposes one configuration contract, one HTTP service, and one CLI. This document records stable behavior; generated OpenAPI is authoritative for exact HTTP fields.

There are no compatibility aliases for retired products, tables, worker names, routes, or response fields. A behavior change updates source, tests, generated contracts, and this document in the same change.

## Runtime configuration

The active operator-owned application file is
`~/.tracefold/config.yaml`. It contains deployment/domain choices,
PostgreSQL role DSNs and password-file references, credentials, API bind/auth,
and News settings. Worker topology,
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
(`host`, `port`), `storage.postgres`, `llm`, and `news`, each a typed nested
model with `extra=forbid`. `ws_token` is the HTTP
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
OpenAI-compatible configuration (DeepSeek, or a LAN llama.cpp / vLLM server).
They are all absent or all present; a partial triple fails validation, and
Tracefold never supplies an implicit endpoint or model. `qwen*` models are
called with `chat_template_kwargs.enable_thinking=false` (code-owned): Qwen3
otherwise spends the Triage token budget on reasoning before the tool call.
`llm.news_triage_fallback` (`api_key`, `base_url`, `model`; all-or-nothing and
only valid next to a complete primary triple; issue #65) is a second direct
endpoint used only when the primary Triage call fails — timeout, transport
error, truncated or invalid output — or while the primary breaker
(`news.triage.circuit_failures` consecutive primary failures open it for
`news.triage.circuit_open_seconds`) is open. Each link gets its own
`news.triage.deadline_seconds`; `news_verdicts.model` records the model that
answered and the trace carries `model_fallback_from` (the primary's error code
or `primary_circuit_open`) or, when both links failed, `primary_error`.
`/api/news/status.pipeline` and `tracefold config` report both
`triage_model` and `triage_fallback_model`. The card's Chinese text is the Triage verdict's
`headline_zh` and `why_zh` (with `title_zh` for the console); no other model
or provider produces copy. Model execution policy, timeouts, token budgets,
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
Worker topology, News broker topology and consumer set, and all resource
capacities are code-owned. Configuration cannot add another worker or derived
product lane.

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
(the console routes `/`, `/app`, `/app/*`, `/news`, `/news/*`), and `/api/*`.
There is no WebSocket endpoint.

- `/healthz` is process liveness.
- `/readyz` combines a lightweight PostgreSQL liveness check with the cached startup schema/composition result. It does not inspect providers, queues, or business freshness.
- `/api/bootstrap` returns `{ws_token}` so the served console can authenticate; every other `/api/*` route requires that token as an HTTP bearer token (`Authorization: Bearer <ws_token>`; routes that allow it also accept a `token` query parameter). A missing or wrong token is `401`.
- `/api/status` is exactly `{measured_at_ms, runtime}`. `runtime` combines the database probe (schema revision match) with the Workers heartbeat row and fails closed on stale heartbeats; there is no provider block.
- Read endpoints do not call providers, execute models, or mutate facts.

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
  `counts` (`total`, `pushed`, `held`, `pending`) reports how the request's
  filters and window split across the three outcome groups — the same
  predicates the `outcome` filter uses, so the three sum to `total` — and is
  therefore unchanged by `outcome` itself. It is present on the first page
  only; a request carrying a `cursor` reports `counts: null` and the caller
  reuses what the first page returned.
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
`news_triage_prompt_v8`, `news_triage_policy_v3`, and `news_delivery_card_v9`.
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

The Alembic chain is `20260818_0275` (the root baseline: it executes
`current_schema_20260818_0275.sql` plus `runtime_roles.sql`) followed by
`20260818_0276_review_49_hard_cut` (drops the retired News title table, the
DEX discovery/token-profile/token-image tables, and the unused LangGraph
`checkpoint_*` tables), `20260818_0277_gmgn_lane_removal` (drops the
social evidence, token identity/registry, DEX/CEX market, live broadcast,
provider circuit, and News market-mark tables), and
`20260819_0278_macro_lane_removal` (drops the ten `macro_*` tables, the four
general market observation tables, `queue_terminal_events`, and the
`reject_macro_fact_mutation()` trigger function). A database at an earlier
revision upgrades with `tracefold db migrate`; a fresh database runs the
baseline and the three hard cuts. The resulting schema is exactly 13 tables.
News owns exactly eleven:
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
- News: `news bus-check|control|label|eval|replay-decisions|replay|dlq|why`;
- maintenance: `ops validate-projections`.

There is no `recent` or `search` command and no market rebuild/sync/reconcile
maintenance command. Mutating maintenance commands require an explicit
execution flag where the parser offers a dry-run mode. They operate from
persisted facts and stable target keys. A rebuild does not create an alternate
generation/run identity or make a provider response the source of truth.

`validate-projections` is a strict Serve-role read. It does not acquire the
maintenance lock, so operators can inspect the running singleton without
interrupting it.

`db audit` reports the migration revision, row `counts` for the eleven
`news_*` tables, and `news_schema` exactness over those same tables.
`db query-audit` covers `/readyz`, `/api/status`, and `/api/news/*`;
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

The `ops` family is exactly `validate-projections`. It constructs only the
dependencies required by the named domain operation and invokes that bounded
operation directly; there is no generic one-shot worker adapter or free-form
result object. It checks the bounded News singletons and delivery-state
invariants against persisted facts and writes nothing.

## Contract change discipline

For a public contract change:

1. change the owning domain/application behavior;
2. add a behavior or contract test;
3. update Pydantic/OpenAPI/frontend types when the HTTP shape changes;
4. update this document and the relevant domain architecture map;
5. remove the old name/path instead of adding an alias or dual read/write.

Historical dated audits explain why a hard cut happened; they are not a second runtime specification.
