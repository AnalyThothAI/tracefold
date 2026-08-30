# Public Contracts

Tracefold exposes one configuration contract, one HTTP service, and one CLI. This document records stable behavior; generated OpenAPI is authoritative for exact HTTP fields.

There are no compatibility aliases for retired products, tables, worker names, routes, or response fields. A behavior change updates source, tests, generated contracts, and this document in the same change.

## Runtime configuration

The active operator-owned application file is
`~/.tracefold/config.yaml`. It contains deployment/domain choices,
PostgreSQL role DSNs and password-file references, credentials, API bind/auth,
and News and Trading settings. Worker topology,
cadence, deadlines, resource limits, batches, leases, retries, and model
reservations are code-owned and are not configuration fields.

Repository fixtures, `.env` files, and generated docs are not runtime
configuration. No static example is a second schema authority: the
`tracefold init` command generates the default directly from the typed config
model and loader implementation.
`uv run tracefold config` reports the effective paths and redacted settings.
Unknown settings or worker keys fail validation.

`tracefold init` creates the operator directory, config, cache/log directories,
an empty Telegram bot-token placeholder, and bootstrap/Serve/Workers/Nautilus/migrate password files. The operator directory is
mode `0700`; config, Telegram placeholder, and password files are `0600`. A normal rerun preserves
existing config and password contents while repairing permissions.
`tracefold init --force` replaces only `config.yaml`; it does not rotate
existing database passwords. The generated config has a new API bearer token
(`ws_token`) but no live provider/model/webhook/bot credential, `news.push.enabled`
is false, and `news.broker.url` points at the compose RabbitMQ service.

The configuration schema is exactly the top-level keys `ws_token`, `api`
(`host`, `port`), `storage.postgres`, `llm`, `news`, and `trading`, each a typed nested
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

Issue #160 also retires every policy-v9 action/priority knob:
`escalate_magnitude`, `min_push_magnitude`, `min_watchlist_magnitude`,
`unclear_push_min_magnitude`, `unclear_push_event_types`,
`high_priority_escalates`, `noise_veto_max_magnitude`,
`noise_veto_respects_gate_priority`, and `contested_push_min_magnitude`.
Remove them before deployment; the strict settings schema provides no alias.

Issue #129 also retires `news.triage.deadline_seconds`; the route deadline is
code-owned by the Program factory. Existing configs must remove the key
before startup.

`llm.api_key`, `llm.base_url`, and `llm.news_triage_model` are one direct
OpenAI-compatible configuration (DeepSeek, or a LAN llama.cpp / vLLM server).
They are all absent or all present; a partial triple fails validation, and
Tracefold never supplies an implicit endpoint or model. Every primary, Reader,
fallback, and compiler endpoint also accepts a provider-neutral `request` block:
`send_temperature` (`true|false|null`), `temperature`, `structured_output`
(`auto|json_schema|json_object|prompt_json`), and bounded `extra_body` fields.
Transport-owned fields cannot be overridden. `auto` keeps known provider defaults;
there is no URL-specific or Kimi-for-Coding compatibility branch. `qwen*` models are
called with `chat_template_kwargs.enable_thinking=false` (code-owned): Qwen3
otherwise spends the Triage token budget on reasoning before the tool call.
`llm.news_reader_card` (`api_key`, `base_url`, `model`; all-or-nothing and only
valid next to a complete primary triple) optionally binds ReaderCard to a
different direct endpoint. When absent, ReaderCard inherits the Triage endpoint.
EventSemantics and ReaderCard still receive separate Adapters and their own
code-owned `max_tokens` (1,200 and 600); changing this endpoint changes only the
secret-free `reader_card.primary` runtime binding identity, not Program
identity.
`llm.news_compiler_tariff` is gone (#202 §6.2). It was the trusted worst-case
rate table the proxy sidecar reserved against, and the sidecar went with the
compiler platform; `learning optimize` charges an unpriced provider call at the
operator's declared `--max-call-cost-microusd` instead. `LlmConfig` forbids
unknown keys, so an operator YAML still carrying the block fails to load with
the key named — remove it before deploying this revision. Each of the three
optimizer roles —
task, reflection and `metric_judge` — is one `ModelExecutionIdentity` holding
the complete secret-free execution contract; its only digest is
`endpoint_fingerprint` over the canonical endpoint URL, which is fingerprinted
rather than stored because it names the host a credential is presented to.
Reflection has an exact 32k-token ceiling. The judge binds its
model/endpoint, instruction/schema, JSONAdapter, timeout/token/temperature/LM
kwargs and cache/retry contract, and its calls, cost and explicit unavailable
failures stay separate facts inside the compile record.
`llm.news_triage_fallback` (`api_key`, `base_url`, `model`; all-or-nothing and
only valid next to a complete primary triple; issue #65) is a second direct
endpoint used only when the primary Triage call fails — timeout, transport
error, truncated or invalid output — or while the code-owned primary-route
breaker is open. `llm.news_reader_card_fallback` is an optional complete
ReaderCard endpoint for that same fallback route and is valid only when
`news_triage_fallback` is complete. When it is absent, the Reader fallback slot
is an explicit alias of the EventSemantics fallback slot; when it is present but
invalid, the whole fallback route is unavailable rather than silently using a
different backend. The Program factory owns that breaker plus each
route's deadline and retry/call budget; `deadline_seconds` is not a
configuration field. The separate `news.triage.circuit_failures` /
`circuit_open_seconds` settings govern the consumer's whole-chain breaker after
both routes fail retryably. `news_verdicts.model` records the runtime model that
answered and the trace carries `model_fallback_from` (the primary's error code
or `primary_circuit_open`) or, when both routes failed, `primary_error`.
`/api/news/status.pipeline` and `tracefold config` report `triage_model`, the
effective `reader_card_model`, whether it is dedicated, and
`triage_fallback_model` plus the effective Reader fallback model/dedicated flag.
Internal `ReaderCard` outputs only `headline_zh`
and `why_zh`; no other model or provider produces copy. `headline_zh` is the
only Verdict/feed reader title; when no current judgment exists the Web falls
back to the Event's original `leader_title`. Model execution policy,
timeouts, token budgets,
cadence, retries, and reservations are code-owned. Environment variables are
not a credential contract.

`news.opennews_token` is the operator-owned secret for the production News
source, and it is the whole News source configuration. Which Strategies feed the
pipeline is decided in the OpenNews account (#126): Tracefold sends no
subscription frame, so the socket delivers what the account has enabled, the
Receiver filters nothing, and there is no `news.opennews_strategy_ids`. Adding
or removing a source is a provider dashboard switch — no config edit, no
restart. `/api/news/status` reports nothing about Strategies — Tracefold neither
chooses nor filters them, so a figure there would only restate the provider's
dashboard.

`news.broker.url` (`amqp://` or `amqps://`, a secret) is required for News to
run; `news.broker.name_prefix` prefixes every exchange and queue name and
`news.broker.connect_timeout_seconds` bounds the connect. `news.triage.*`
(`concurrency`, `circuit_failures`, `circuit_open_seconds`), `news.push.*`
(`enabled`, the mutually exclusive Feishu fields `feishu_webhook_url` and
optional `feishu_signing_secret`, or Telegram fields
`telegram_bot_token_file` and `telegram_chat_id`, plus
`min_interval_seconds`), and
`news.venues.*` (`enabled`, public-data switches `binance`, `hyperliquid`,
`okx`, `lighter`, `bitget`, reference-only `us_reference`, and `snapshot_period_hours`), and
`news.watchlist[]` (`{symbol, market_type}`) are the only News knobs.
`news.triage.concurrency` (default 4) is the real consumer width of its queue.
Lexicons, prefix tables, LSH geometry, the code-owned Program registry, and
policy versions are image state. `tracefold config` exposes only redacted booleans, counts,
model names, and watchlist symbols; it never prints the token, broker URL,
keys, or webhook.

Push delivery is available only when `news.push.enabled` is true, exactly one
provider is complete, and Workers is running. Feishu requires a valid HTTPS custom-bot v2 URL.
Telegram requires a secure bot-token file and one private channel Bot API ID
beginning with `-100`. Provider conflicts, invalid targets, and missing or
insecure credentials fail closed: Serve remains credential-free, while an
explicitly enabled invalid provider configuration makes Workers fail startup.
On Telegram, a reader ticker is clickable only when an official venue catalogue proves an exact contract.
Destinations are built from typed Binance, Hyperliquid, OKX, Lighter, or Bitget identities; untyped URLs and
inconsistent metadata remain plain text. A pushed `single_name` card with exactly one candidate ticker, or no
grounded ticker but a confident code-like title identity, is checked after the initial send against fresh catalogues
from all five venue families. Any exact match keeps the message and
is added through one in-place edit; its delivery prices use trade-first anchors and closed one-minute candles as
fallback. An absent result authorizes `deleteMessage` only when all five catalogues answered successfully and none
matched. A timeout, blocked endpoint, malformed catalogue, incomplete issuer identity, or any other partial result
retains the message. PostgreSQL records the complete five-venue evidence and reason before deletion, then settles
the exact receipt as `deleted` or `ambiguous`; startup and the stale-intent sweep terminalize inherited `deleting`
intents instead of retrying an uncertain destructive action. A settled deletion is excluded from the durable
reader-history ledger, so a removed untradeable issuer cannot suppress a later genuinely tradable listing. The
Telegram projection gives every asset its own block: the first line is `🎯 标的 BTC`, followed by separate
`新闻后 +1.10%`, `1h +0.80%，`, and `24h +3.20%` lines. Multiple assets repeat that complete block with a blank
line between them. Direction and magnitude remain presentation metadata: an unclear verdict renders `方向待定`,
and a positional tail token that does not match the code-owned ticker grammar fails closed rather than becoming a
trade target. The novelty badge sits directly
below the title: `🆕 新事实`, or `🔄 新进展` with the prior
headline immediately only when no post-delivery verifier is configured and an exact-fact retrieval or stored
title-similarity score of at least `0.50` supports it. With the verifier configured, an initial progression shows
an indented one-line `关联确认中` child block and never waits for another model call. The same message is later
edited to a compact child block: `✅ 已确认关联`, a clickable `此前：<parent headline>` quote bound to the
previous sent Telegram receipt, and its receipt-to-receipt age. A rejected, unavailable, or otherwise non-confirmed
final result changes the same message's novelty badge to `🆕 新事实` and removes the complete association child
block; the verifier's result and reason remain in the durable desired-card audit rather than appearing to readers.
A model confirmation without a sent, undeleted, same-target parent receipt follows the same `🆕 新事实` rule; it
never renders an unlinked parent or candidate/event-time age. The verifier considers at most eight already-delivered told-ledger candidates and its
structured result plus content-addressed verifier identity enters the durable desired card. A broad macro or sector
verdict with no code-verified ticker shows its scope and `暂无直接标的` instead of silently removing the target area
or inventing a trade. Telegram delivery is progressive: once the code-owned decision and provider pacing allow a
send, the first `sendMessage` contains the complete news facts immediately and labels all three market values
`计算中`; it performs no public price read first. The returned message ID and original send timestamp are settled
as `sent` before a background enrichment reads prices and, for a progression, verifies the claimed historical
relationship. Those operations run concurrently. That enrichment replaces the same Telegram message with
`editMessageText`; it never sends a second card. Before that provider mutation, PostgreSQL stores the desired card
as `pending_card` with `edit_state=editing`, bound to the same provider, message ID, original push timestamp, and
target digest. A confirmed edit promotes that card and canonical receipt under `edit_state=edited`. A crash,
timeout, invalid receipt, or settlement conflict after intent is recorded becomes `edit_state=ambiguous`; the
original `sent` outcome is never retracted or retried, and the ledger does not pretend to know which version TG
currently shows. Startup converts every inherited `editing` intent to the same explicit ambiguity. Feishu has no editable
capability and retains its single enriched send. Startup reconciliation must succeed before the delivery consumer
starts; while running, a 30-second sweep converts any edit still unsettled after 60 seconds to ambiguity and retries
after transient database failures.
Current-vs-anchor, fixed 1 h, and fixed 24 h returns come only from the
same request-time venue and contract: Binance is tried first, Hyperliquid second and OKX third. At each news,
push-minus-1H, push-minus-24H, and push anchor, the latest trade no later than the millisecond timestamp is used only when it is
at most 60 seconds old; otherwise the adapter falls back to the last closed one-minute candle within 90 seconds.
The calculation never mixes venues or contracts, needs no continuously collected tick history, and does not
write these presentation returns into `reaction_v1`. The 24 h value is calculated from the current and
push-minus-24H anchors on that same contract; a fresh same-contract `rolling_24h` snapshot is only a fallback when
the on-demand point path is unavailable. An unavailable value is labelled rather than replaced with another window.
Direction renders impact and polarity together on one line, such as `🧭 方向 明显利空`; novelty remains its
own line. The footer has no time heading: it lists news publication time, send-start time, and then the
normalized source words carrying the original HTTPS link, with no separate source button. Times use
whole-second precision in UTC+8; any missing
input is displayed as `暂无` without hiding known timestamps. The persisted reader card and Feishu payload are
unchanged; these values travel only in an ephemeral typed delivery presentation.
When push is disabled, both processes still start and a verdict that reaches a
delivery consumer settles `terminal/delivery_unavailable`.

`trading.*` is the whole Trading surface (#104, #331, #350) and is
`enabled: false` by default. `trading.enabled` controls only the Decision
Plane. Binding credential projection and the credential-free public venue
catalog remain active Workers responsibilities. The accepted keys are only:

- `enabled`;
- `candidates.*`: `max_age_seconds`, `min_oi_value_usd` — a freshness budget and
  a venue liquidity prior, never sizing and never Alpha. `symbol_cooldown_seconds`
  and `max_rank_in_window` were retired by #348: a per-symbol re-entry delay is
  what a lane needs when several positions can be open at once, and a rank ceiling
  is selectivity, which the policy already owns;
- `order.fixed_notional_usd`, the sole operator execution value, validated as
  `0 < value <= 10`;
- `bindings.binance_usdm.api_key_file` / `api_secret_file`;
- `bindings.hyperliquid_perp.private_key_file` / `account_address`.

Secret-file paths are resolved relative to the operator config directory
unless absolute. Missing files are the legal `unconfigured` state. Partial,
malformed, insecure, or unreadable inputs are `invalid`; neither state exposes
secret contents. A configured binding is still `stopped/unknown` until its
later adapter-owned read-only preflight exists (#356/#357).

There is no mode, live symbol, account, venue, OpenTrade, approval, backend
selector, or Intent-acceptance configuration, and since #331 no `regime.*`,
no `policy.*` and no model budget: a capital threshold in a YAML file is a rule
with no version and no frozen evidence. Unknown retired keys fail strict
settings validation. The capital lane's poll cadence is App-owned and the
Nautilus cadence is code-owned.

The one production policy, `source_native_oi_smart_money_long_v3`, is
code-owned and frozen onto every Case it decides, together with the per-check
evidence (`policy_checks`: threshold, operator, measured value, pass/fail).
It answers `long` or `no_trade` only; it cannot express a permission, an
execution environment or a venue. There is no Trading model call and no
`llm.trading_decision_model` key.

`VenueInstrumentCatalogSnapshotV1` is public instrument truth, collected
without credentials for the closed `BINANCE_USDM` and `HYPERLIQUID_PERP`
bindings. A content-addressed append-only snapshot conserves every provider
row, including inactive, duplicate, and unnormalizable rows with an exact
reason. Each binding owns an atomic active pointer plus
`ready|stale|error|missing`; a failed refresh retains last-known-good and does
not affect the other binding. Catalog presence is not execution permission.
#355 uniquely owns compilation of `catalog + ExecutionBinding` into an
execution capability partition.

Decision, Capital, and binding runtime are orthogonal durable facts. Decision
is `DISABLED|STARTING|RUNNING|FAULTED`; Capital control is
`PAUSED|CLOSE_ONLY|RUNNING`; each binding separately records redacted
credential fingerprint/state, runtime, account, catalog, heartbeat, and exact
reason. A Worker start or credential projection forces Capital to `PAUSED`, so
a restart or credential change never inherits an old activation.

Before #360, a pure-policy LONG is always persisted as
`policy_decision=long` plus `capital_disposition=blocked`; the exact earliest
reason is control, credentials, catalog, exposure, binding readiness, or
`promotion_authority_unavailable`. It is never rewritten as NO_TRADE and no
new Intent is created. #360 is the sole future owner of grant, arm, daily risk,
reservation, and Intent creation in one transaction.

`storage.postgres.nautilus_dsn` and `nautilus_password_file` retain the
least-privilege role for existing Intent recovery evidence. Serve is read-only.
Legacy `trading_orders` and
`trading_order_observations` are retained read-only for audit and have no
product or writer surface.

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
Worker topology, News broker topology and consumer set, and all resource
capacities are code-owned. Configuration cannot add another worker or derived
product lane.

## Operator lifecycle

The fresh-clone operator contract is `make up`. It preflights `uv`, Docker,
Compose, `curl`, an authenticated GitHub CLI, and daemon access; runs idempotent
initialization; builds the frontend and backend image; performs fresh-volume role bootstrap; runs the
one-shot migration; starts Serve and Workers; and waits for required health and
console boundaries. Execution credentials are not a deployment prerequisite,
and no execution adapter is started (#356/#357 pending). A repeated invocation preserves config, passwords, and
named-volume data, including across `make down`.

`make status` fails non-zero when PostgreSQL, migration, Serve, Workers, either
required runtime readiness endpoint, or console HTML is missing or unhealthy.
It reports execution adapters as not required while Capital is PAUSED.
`make logs` follows the bounded startup services. `make down` stops the stack
without deleting the named PostgreSQL volume. These targets do not auto-hard-cut
an unknown non-empty database.

`make deploy-image IMAGE_ID=sha256:<64 lowercase hex>` is the explicit
same-schema image redeployment/rollback contract. It accepts only a full local
image ID supplied on the Make command line from a deployment-clean primary
checkout whose `main` equals local `origin/main`; it refuses inherited Compose
stack selectors, `.env`, Compose overrides, and untracked or ignored Alembic
revisions. Before stopping Serve or Workers it requires source, image, and live
database Alembic heads to match and requires the target image to parse the
active config. It never builds, pulls, or downgrades. Success additionally
requires the recreated migration, Serve, and Workers containers, Workers
readiness identity, runtime manifest, and linked active/deployment receipt to
prove that exact image. `make up` and `make deploy-image` share one
process-lifetime deployment lock; concurrent mutation is refused and process
exit releases the lock.

## HTTP

The service exposes `/healthz`, `/readyz`, `/metrics`, static frontend assets
(the console routes `/`, `/app`, `/app/*`, `/news`, `/news/*`, `/trading`), and `/api/*`.
There is no WebSocket endpoint.

- `/healthz` is process liveness.
- `/readyz` combines a lightweight PostgreSQL liveness check with the cached startup schema/composition result. It does not inspect providers, queues, or business freshness.
- `/api/bootstrap` returns `{ws_token}` so the served console can authenticate; every other `/api/*` route requires that token as an HTTP bearer token (`Authorization: Bearer <ws_token>`; routes that allow it also accept a `token` query parameter). A missing or wrong token is `401`.
- `/api/status` is exactly `{measured_at_ms, runtime}`. `runtime` combines the database probe (schema revision match) with the Workers heartbeat row and fails closed on stale heartbeats; there is no provider block.
- Read endpoints do not call providers, execute models, or mutate facts.

Status contains no provider/model credentials, base URLs, request policy,
capacity counters, Program instructions/demonstrations, or raw model responses.
Code-owned Program/policy versions may accompany bounded verdict telemetry; they are
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
| News | `/api/news/feed`, `/api/news/events/{event_id}`, `/api/news/status`, `/api/news/quotes`, `/api/news/symbols/{base}` | broker-driven Event feed, one Event with frozen evidence/verdict/delivery audit, four-layer status, bounded quotes, and one symbol's identity |
| Trading | `/api/trading/status`, `/api/trading/cases`, `/api/trading/intents`, `/api/trading/gate`, `/api/trading/gate/{event_id}` | one owner per durable aggregate (#331): runtime readiness, frozen Case decisions, immutable Intents and their Outcomes, and the Source-admission ledger. Reads only — there is no HTTP write on this surface |

The public API is exactly these routes plus `/healthz`, `/readyz`, and
`/metrics`. The retired GMGN-lane routes (`/ws`, `/api/recent`,
`/api/events/by-ids`, `/api/search`, `/api/search/inspect`, `/api/token-case`,
`/api/target-posts`, `/api/target-social-timeline`, `/api/live-market`,
`/api/token-images/*`, `/api/token-radar`, `/api/stocks-radar`) are not
registered and answer the ordinary `404`; there is no alias, redirect, or
feature flag.

### News

News is an operator-bound, Strategy-qualified Event surface. The public surface
is exactly five GET route templates and no write route at all. The four
ReviewDesk routes — two reads and the only two HTTP writes this project ever
had — were removed with the console page they served (#256); `news review
queue|evidence|submit|external-miss` is now the whole ReviewDesk surface, and
it reaches `news_reviews` through its own Serve-role connection.

`priority` is not a reader contract: feed/detail/OpenAPI expose no field,
filter, sort or badge for it. The hard-renamed `queue_priority` exists only in
broker scheduling, storage/audit/measurement and explicit operator review
projections; there is no public alias.

Every normalized OpenNews frame is classified once by
`opennews_source_classifier_v1` from the exact
`strategy_id + strategy_name + source_type + engine_type` tuple. The closed
result is `news_v1|listing_v1|oi_v1|liquidation_v1|unsupported_market`, persisted
as `event_kind=news|listing|oi|liquidation|unsupported_market`. Ordinary enabled
News and listing still use Gate and the Program; only the exact 1019 OI and 2000
liquidation contracts select their strict parsers. The pinned tuples are
`1019 / OI Event Monitor / market / market` and
`2000 / 实时清算 / market / market`; `2026 / 聪明钱监控 / wallet / market` and
`2083 / Large-scale liquidation / market / market` are explicitly unsupported.
Known tuple drift and unbound scoreless market/wallet frames persist Item/Event
provenance with nullable Event field
`source_contract_reason=source_contract_drift|unsupported_market_contract`.
Migration `0336` deletes rows carrying the retired
`source_contract_unverified` value; it is not a current contract member.
Unsupported rows call no model and create no delivery.
A malformed OI/liquidation frame stores `source_contract_drift`;
a valid current-writer contract stores `null`. Recovery uses the same classifier
and, when provider history includes the complete tuple, the same strict parser;
it persists the result but does not write the live OI rank fact and never
delivers. An incomplete history tuple fails closed as drift. No Strategy id or
title alone selects a deterministic route.

- `GET /api/news/feed?event_family=...&change_state=...&assertion_status=...&source_authority=...&subject_code=...&final_decision=...&event_kind=...&admission=...&symbol=...&q=...&limit=...&cursor=...&outcome=...&hours=...&oi=...&direction=...`
  returns current Events newest first with the leader title, durable `event_kind`, nullable
  `source_contract_reason`, admission,
  asset class, grounded assets, watchlist hits, storyline key, context line,
  **one `outcome`** (`kind` from the stable enum `held_recovery`, `held_gate`,
  `expired_triage_handoff`, `expired_delivery_handoff`, `queued_publish`,
  `queued_triage`, `dropped`, `throttled`,
  `degraded_dropped`, `pending_delivery`, `delivered`, `delivery_failed`;
  reader copy `text_zh` and `reason_zh`; `group` = `pushed|held|pending`),
  the latest Triage summary (final decision, override rule, throttle reason,
  degraded flag, error code, direction, magnitude, scope, taxonomy, typed
  relevance, `headline_zh`, `why_zh`, and server-owned Chinese labels), and the first delivery state with its
  error code. `outcome` is the feed's task-tab filter (its SQL mirrors the
  outcome groups); `hours` bounds `opened_at_ms` to the last N hours (`0`
  or absent = no bound).

  Event-to-Triage and push-Verdict-to-Delivery use the same code-owned
  30-minute relevance ceiling. A marker-null handoff is pending at exactly the
  boundary and expired only when strictly older; a non-null marker remains
  published regardless of age. `expired_triage_handoff` and
  `expired_delivery_handoff` are `held`, never `pending`. Page rows, outcome
  filtering, and first-page counts share one request `as_of_ms`, so a row
  cannot be expired in the response but pending in its counts.

  `event_family`, `change_state`, `assertion_status`, `source_authority`,
  `subject_code`, `final_decision`, `event_kind`, and `direction` accept
  comma-separated, duplicate-free closed sets. Taxonomy axes filter the current
  model editorial object; `event_kind` filters the durable source/routing fact.
  Neither is inferred from the other. The axes compose with every existing predicate
  before the count aggregate and cursor pagination.

  `q` and `symbol` are mutually exclusive. `symbol` (maximum 32 characters) is
  always an exact asset-identity request. A normalized single-token `q` becomes
  asset mode only when the instrument catalogue resolves it exactly as a
  canonical symbol, alias, venue symbol, or one of the bounded pair spellings;
  one leading `$` is ignored for that identity lookup. Unknown tokens and every
  multi-word query are text mode. Asset mode expands the resolved identity to
  its durable canonical and alias Event spellings, then matches only exact
  `news_event_assets.symbol` values. It never falls back to title, origin,
  venue, substring, or wildcard matching. Text mode applies PostgreSQL
  `websearch_to_tsquery('simple', ...)` to the persisted Event search document;
  `%` and `_` are ordinary input, not SQL wildcards. Chinese input is routed
  consistently to text mode, but v1 makes no segmentation or recall guarantee.
  Search is applied by the authoritative feed query before counts and cursor
  pagination; the browser does not maintain a second index. The response always
  carries nullable `search` metadata with `mode`, `normalized_query`, and
  `resolved_symbols`; it is `null` when neither input was supplied.

  A `telemetry_deterministic` row additionally carries a nullable `oi` block
  (#207): `parsed`, `rule`, and — depending on which of the two shapes it is —
  the four measurements (`oi_change_bps`, `oi_value_usd`,
  `whale_long_profit_bps`, `whale_oi_ratio_bps`), `eligible_rank_in_window`,
  `rank_semantics`, the thresholds that frame ran under (`window_ms`,
  `max_rank_in_window`, `whale_oi_ratio_above_bps`, `oi_change_at_least_bps`),
  or the provider-contract failure (`parser_version`, `failure_stage`,
  `title_sha256`). Every field is `oi_judgment_trace()` /
  `oi_parse_failure()` output read back from the verdict trace, so no client
  ever re-runs `oi_signal_parser_v1` over `leader_title`. `strategy_id`,
  `provider` and `provider_source` are deliberately not exposed. The block is
  `null` on every other admission. `oi={pushed|withheld|parse_failed}` filters
  on the judged rule: `pushed` is the one qualifying rule, `withheld` the three
  threshold rules, `parse_failed` the unparseable frame. `decision` cannot
  express that split — a threshold withhold and a parse failure are both
  `drop` and both carry `override_rule = telemetry_deterministic`. The filter
  also constrains `admission = telemetry_deterministic`, which is the only lane
  that can write the key it reads: without it the predicate has to detoast
  `news_verdicts.trace` for every candidate row, and a rare rule with no early
  exit walked the whole retention into the serve role's 1 s statement timeout.

  Supplying both `q` and `symbol` returns 400
  `news_feed_search_conflict` before repository work. Unknown query parameters, invalid admission,
  decision, `oi`, `direction` or `channel` values, malformed cursors, and the retired `priority`/`sort`
  parameters return 400; out-of-pattern `outcome`/`hours` return 422. Recovery
  Events are visible with `admission=recovery`. `filters` echoes every parameter incl. `outcome`,
  `hours` (never the wall-clock bound, so unchanged pages keep their ETag), `oi`, `direction`, and `channel`.
  `counts` (`total`, `pushed`, `held`, `pending`) reports how the request's
  filters and window split across the three outcome groups — the same
  predicates the `outcome` filter uses, so the three sum to `total` — and is
  therefore unchanged by `outcome` itself. It is present on the first page
  only; a request carrying a `cursor` reports `counts: null` and the caller
  reuses what the first page returned. `counts` is part of the ETag basis and
  tracks the whole window rather than the page, so the first page revalidates
  when anything in the window moves — an Event ageing out at the tail, a
  delivery settling on a later page — even when its own events are unchanged.
  Later pages keep the stricter stability.

  Every Event carries `grounded_assets` (the raw provider coin tags the Gate
  admitted on) and beside it `assets[]` — the durable `news_event_assets`
  ledger resolved against the #75 instrument universe. That ledger includes
  Gate-grounded tags and deterministic-judge primaries, so an OI Event can have
  `grounded_assets=[]` and a listed BTR entry in `assets[]`. Each entry is
  `{symbol, base_symbol, venue, listed}`; duplicate spellings such as `CL` and
  `XYZ-CL` resolve to one instrument. `symbol` is normalized, so `UNITREE` and
  `XYZ-UNITREE` resolve to the same listed contract. `venue` is preferred when a base trades on
  several (deepest first, HIP-3 builder DEXs last) so a chip is stable across
  polls, and is `null` with `listed: false` when the tag names nothing on any
  venue — which is how a reader tells `SPOT` on a Spot Gold headline from a
  real listing. Each response reads all Event assets in one bounded batch and
  resolves all symbols in one instrument batch, never one query per Event.
- `GET /api/news/events/{event_id}` returns one Event, its durable `event_kind`, nullable
  `source_contract_reason`, its `outcome`, a
  `timeline` (ordered steps `received` → `gate` → `triage` → `decide` →
  `delivery`, each with `title_zh`, `at_ms`, `summary_zh`, and the raw
  `facts` it was built from), its member Items (title, URL, origin,
  publication time, match kind, Jaccard estimate, provenance, description),
  every Triage verdict (model decision, rule baseline, final decision,
  override rule, throttle reason, verdict payload, runtime model, Program
  version/SHA, degraded flag and trace; nullable `prompt_version` is Prompt-era
  audit history only), deliveries (including confirmed `card`, nullable `pending_card`, canonical receipt,
  `edit_state`, bounded edit error, and edit attempt/settlement timestamps), and `normalization[]` — the alias
  groups this Event's assets fall into (`base_symbol`, every `alias` that
  resolves into it including the base itself, and the alias `sources`). Only
  the code-owned seed aliases count (`source = 'seed'`, reconciled from
  `ALIAS_SEEDS` on every snapshot): the venue-derived rows (`XYZ-{base}`,
  `dex:SYMBOL`) are mechanical and would fire the block on every commodity
  Event. Only groups that actually collapse more than one name are sent, so the surface
  explains a surprise (SKHY / SKHX / SKHYNIX share one storyline bucket)
  rather than restating a ticker that answers to itself. For a grounded
  restatement, the decide step includes the prior Event id, sent timestamp,
  headline, history scope, and retrieval reason (`recent`,
  `exact_fingerprint`, or `canonical_asset_overlap`). `tracefold news why`
  prints the same `outcome` sentence and timeline. Unknown ids return 404.
- `GET /api/news/status` returns `state` (`ready`, `warming`, `degraded`,
  `unavailable`), the Workers state, `health` (four thresholded items
  `ingest`/`broker`/`model`/`delivery` with `level` `ok|warn|bad|off`,
  `summary_zh`, `detail_zh`, and `overall`; thresholds are code-owned, see
  `docs/OPERATIONS.md`), `funnel_24h` (`received`, `admitted`, `candidates`, `triaged`,
  `tagged`, `grounded`, `decided_push`, `delivered`, plus
  `received_1h`/`delivered_1h`),
  whose four Event-feed stages (`received`/`admitted`/`triaged`/`delivered`) all start from Events
  opened in the same rolling 24 h cohort and test those Events' durable stage facts,
  `reasons_24h` (`stage` `gate|drop|throttle|push|degraded|ungrounded`, raw
  `key`, `label_zh`, `count`, sorted by count), and four layers: `ingest` (WSS
  connected, last frame/publish, error, open incidents, closed-pending
  `recovery` summary with `pending_count`, `oldest_opened_at_ms`, and
  `last_error_code`, plus `reason` (`recovery_pending|recovery_transient|null`),
  token configured; no
  Strategy IDs/counts), `broker`
  (configured, connected, per-queue message/consumer counts when observed,
  error code), `pipeline` (events and candidates per hour/day, Triage counts,
  `source_classifier_version`, and `source_contracts_24h` keyed by the five
  closed families. Each family counts the same Event cohort opened in the last
  24 h: `received`; `parsed` (durably verified OI/liquidation parses, all
  supported News/listing Events, and zero for unsupported);
  `parse_failed` (`source_contract_reason=source_contract_drift`); `unsupported`; and
  `verdict` (any Triage verdict for the Event). It also returns degraded counts
  incl. `triage_degraded_by_code_24h`, decided pushes,
  throttled, OI telemetry received/parsed/parse-failed/pushed counts, Triage
  p50/p95, queue lag p95, the Triage model name, the cohort fields
  `funnel_received_24h`/`funnel_admitted_24h`/`funnel_triaged_24h`/
  `funnel_delivered_24h`, and the
  named 24 h maps `suppressed_by_reason`, `dropped_by_rule`,
  `throttled_by_key`, `pushed_by_rule`, `duplicates_withheld_24h`
  (`all` is the current content-only path), plus `tagged_24h`,
  `grounded_24h` and
  the top-ten `ungrounded_by_symbol_24h`), `oi` (#207: the deterministic
  open-interest lane — `policy`, the `news.oi` thresholds as they are running;
  `by_rule_24h`, 24 h OI-scoped counts keyed on the typed judgment's own gate
  names; `window_occupancy`, per-symbol spent rank slots inside
  the live window measured with the judge's eligibility predicate, ending at
  `measured_at_ms` — the window's start is deliberately not a field because it
  would move on every read and churn the ETag. There is no `trade_floors`
  (#331): News republished the capital lane's thresholds here, which invited a
  console to compare a Case frozen last week against a floor edited yesterday.
  Admission's own configuration is published by `/api/trading/gate`, and the
  thresholds that decided a Case travel with that Case),
  and `delivery` (sent/terminal
  counts, last error, end-to-end p50/p95, availability; availability requires
  both a complete declared provider and a running Workers runtime), plus
  the watchlist symbols, and `instruments` (the
  #75 universe summary: trading/delisted counts, base symbols, venues, last
  snapshot time, per-venue and per-class counts, `dangling_aliases`, and
  `reference_symbols`). Every figure but the last two counts contracts on
  venues we poll; `reference_symbols` is the separate US listed-symbol
  directory (#91), which tells the Gate a ticker is a stock and is tradeable
  nowhere, so it is kept out of `trading`, `by_venue`, `by_class` and the
  `符号落表` funnel.

  `funnel_24h.grounded` and `ungrounded_by_symbol_24h` are folded in the route
  from two halves neither repository reaches across for: News reports which
  tags each Event carried (`news_event_assets`), the instrument universe
  reports which of them name something listed. An Event counts as grounded
  when *any* of its tags resolves — the same condition the Gate admits on, so
  the console's funnel and the Gate cannot drift apart. The per-symbol tally
  is deliberately per-symbol rather than per-Event: the operator question is
  which provider tag keeps failing, and one bad tag can cost dozens of Events.
  `tagged_24h` counts the Events that offered at least one tag, and is the only
  population `grounded_24h` may be compared against — an Event carrying no coin
  tag never appears in either.
  The count is not clamped against the window's Event total — a funnel segment
  wider than the one above it is a visible bug, and a silently clamped one is
  an invisible one.

The Chinese vocabulary behind `outcome`, `*_zh`, and `label_zh` lives in
`tracefold.news.outcome` (admissions, `decide()` rules, throttle keys, error
codes, delivery errors, event types, directions, magnitudes, storyline
themes); a new rule or error code lands there in the same change, so no
surface renders a bare key.

`/api/news/feed` and `/api/news/events/{event_id}` emit strong ETags and
honor `If-None-Match`; `/api/news/status` uses a weak ETag that ignores
`measured_at_ms`. All News routes require the operator token.

Item identity is `sha256(source_id, params.id)`; `params.strategy.id` is
provenance, not fact identity. Event identity v6 is
`sha256(identity_version,item_id,fact_id,event_kind)` for every route.
Pre-genesis identities were deleted and there is no pre-v6 collision or rekey
bridge. Events
merge different Items only within the same `event_kind` and current
source-contract reason, by exact comparison fingerprint or MinHash/LSH
near-duplicate (estimated Jaccard >= 0.55 with strong-fact compatibility)
inside the dedupe-family window (market telemetry 2 h, disaster 6 h, filing 72 h,
general 12 h). Only post-genesis Events enter exact, artifact, or near-match
candidate sets; current drift and success cohorts never cross.
Fingerprints of at most two tokens never share an Event.

Verdict identity is `(event_id, stage, policy_version)`. Current rows also carry
`judgment_contract_version=news_judgment_v2` and an exact
`judgment_origin=model|oi|liquidation|degraded`. `TriageVerdict` is a
presentation-only atom: `novelty`, `restates`, `assets`, `direction`, `scope`,
`magnitude 0..3`, `confidence`, `audience`, `headline_zh`, and `why_zh`.
Model delivery intent has exactly one owner: the model editorial envelope's
`TradeRelevanceV1.reader_value`; final action has exactly one owner in the
origin-matched `DecisionResult`.

`TradeRelevanceV1` is the nested output of `EventSemantics.v2`:

- `impact_breadth`: `none|single_instrument|sector|regional|cross_asset|global_systemic`;
- `tradability`: `direct|second_order|contextual|none`;
- `surprise`: `unscheduled|material_vs_expectation|in_line|unknown`;
- `development_delta`: `state_change|material_detail|color_only|scheduled`;
- at most four unique `channels`, canonicalized in the code-owned order
  `rates|liquidity|risk_premium|energy_supply|commodity_supply|commodity_demand|regulation|exchange_access|product_progress|earnings_cashflow|positioning_flow|security_incident`;
  `product_progress` (#173) is a first-party confirmed product, protocol, or
  market capability reaching a verifiable new state, or a first-party
  active-use/economic adoption metric reaching a new quantified step — never
  brand marketing, a roadmap, or a cumulative address/account total;
- at most four unique `affected_markets`, canonicalized in the code-owned order
  `crypto_broad|us_equity_broad|rates|fx|energy|metals|single_asset`;
- `reader_value`: `escalate|realtime|background|none`.

The sibling `news_taxonomy_v1` is a fact projection, not delivery intent:

- `subject_codes`: zero to three qcodes from the 35-node IPTC Media Topics
  subset pinned at upstream version `2026-01-05` and codebook SHA
  `6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac`;
- `event_family`: one of the 13 event families defined in
  [`docs/NEWS_TAXONOMY.md`](NEWS_TAXONOMY.md);
- `change_state`: `announced|scheduled|effective|reported|updated|delayed|cancelled|recalled|unknown`;
- `assertion_status`: `confirmed|claimed|rumor|conflicted|unknown`;
- `source_authority`: `regulatory_filing|issuer_first_party|reputable_secondary|unknown`,
  derived by code from structured source/provenance and absent from model output.

`other` and `unknown` are valid abstentions. Unknown qcodes, more than three
qcodes, and a pinned parent together with one of its pinned descendants fail
schema validation. `NewsTaxonomyV1` is the only model semantic classification.

Empty channels/markets are valid only for contextual/none tradability with a
background/none reader value. The normalizer records the raw arrays, then
de-duplicates and orders them before exact gold, hashing and replay.
The ordinary policy reads `reader_value` with the current presentation facts
and objective guards to issue one `DecisionResult`; no action copy is stored in
the Verdict.

`SemanticJudgment` atomically carries verdict, an `EditorialEnvelope`, trace,
usage and runtime identities. The envelope is
`{editorial_contract_version=news_editorial_v2, editorial_origin=model,
relevance, taxonomy, editorial_sha256}` and exists only for model origin.
An admitted listing still runs the normal Program and uses `model` origin;
listing admission is an objective policy fact, not synthetic relevance.
`ScoredJudgment` is the model projection accepted by policy, baseline, compiler,
CandidateEvaluator and recording/replay. OI, liquidation and degraded lanes use
their own typed judgments. `news_verdicts` stores the current marker/origin,
verdict, model editorial when applicable, judgment hash, exact `runtime_manifest_sha`,
`rule_baseline_decision`, `final_decision`, `override_rule`,
`throttled_by`, `degraded`, `error_code`, and trace in one transaction. Rows
without the current marker cannot exist after the `0336` genesis, and every
ordinary reader requires the current marker.

The trace binds `verdict_sha256`, `editorial_sha256`, Program version/SHA,
runtime manifest/provider/model identity, every frozen `DecidePolicy` value,
input/told/seen/storyline hashes and snapshots, every initial/re-ask execution,
and per-Predictor request/input/upstream/output,
finish reason, latency, token and cost identity, plus `envelope_sha256`: the
computed identity of everything the code decided about the call. What the model
was sent is the artifact's instruction unchanged, so the Program SHA already
commits to it and the call trace no longer repeats a signature, instruction or
demo digest. A told-only re-ask may restore
the complete `first_judgment`; evidence-changing re-asks may not reuse it.
`triage` is the only current stage. Current versions are
`news_title_norm_v2`, `news_gate_v5`, `news_storyline_v3`,
`news_event_evidence_v3`, `news_judgment_v2`,
`news_semantic_program_v8` (or `news_oi_signal_v2` /
`news_liquidation_fact_v2` for deterministic structured lanes),
`news_triage_policy_v11`, `news_delivery_card_v11`, artifact schema
`news_program_strategy_artifact_v1`, and source classifier
`opennews_source_classifier_v1`. The epoch is the running bundle's
(`bundle_<sha8>`) and is not a declared version. The exact Program identity is
its content SHA plus `envelope_sha256`, not the display version alone; see
`docs/ARCHITECTURE.md` for the identity model.

The normalized tuple `2000 / 实时清算 / market / market` is a separate
deterministic contract composed after Gate v5; strategy id `2000` alone has no
routing authority and does not silently widen that policy or editorial policy
v11. Its release identities are `news_liquidation_admission_v1`,
`news_liquidation_fact_v2`, `news_liquidation_policy_v2`,
`liquidation_parser_v1`, `opennews_liquidation_source_v1`, and
`opennews_source_classifier_v1`. The source
contract records provider-record and unresolved contract identity, position
side, quantity/notional/price semantics, completeness and throttle assumptions.
Its current `complete=false` is a material fact.

`ProgramStrategyArtifactV1` is the only executable semantic configuration, and
it is one canonical JSON document — `schema_version`, the
`event_semantics_instruction` and `reader_card_instruction` texts, and the
`program_sha256` over exactly those three values — carried in the application
image as `<program_sha256>.json` and selected by the code-owned registry. Since
#306 Phase 2 each instruction is the complete prompt for its Predictor rather
than an advisory appended to a rendered stack, and the reviewed seed text lives
in `tracefold/news/program/seed.py`; #314 removed the `factory_id` field, since
code identity is computed rather than declared. The stable root is
`2857303530b684323ded02df055a83575261eb0c46e5a44671e8d2ee1a18ac71`.
That SHA is behavior identity only: it holds no parent lineage, optimization
cost, trajectory or teacher endpoint, so two runs that reach the same two
instructions produce the same Program. Lineage belongs to the candidate's
`ProposalReceipt`, and since #202 it is *derived* at registration by re-applying
the patch to the running stable rather than declared by the candidate.
The graph, schemas, normalizer, assembler, model route and execution budget are
code, and `envelope_sha256` is computed over what that code renders; a semantic
change to any of them moves that hash by construction, which one contract test
pins. See `docs/ARCHITECTURE.md` for the identity model. Rendered instructions are derived bytes, never a
second editable truth, and they contain no identity hash and no demo section.
Loading fails closed on an unknown hash or schema version, non-canonical or
duplicate-keyed JSON, a non-finite number, a path or symlink violation, a file
name that is not its own root, or unsafe or secret-bearing state.
The optimizer can emit only a typed patch carrying the two Predictor
instructions; there is no DemoBank to write to. The trusted side reconstructs
the final Artifact from the exact active stable root. Pickle, cloudpickle,
dynamic Python/classes, endpoints and credentials are not artifact formats.
One typed `AuditedConfiguredLM` invocation is one stock DSPy/LiteLLM provider
call, with no client cache or provider retry. JSONAdapter may make one additional
format call per Predictor, and every physical invocation appears in the trace.
There is no legacy Prompt runtime, dual stack, compatibility Adapter or
production operator-selected artifact path. Nullable Prompt-era fields remain
audit-only.
The current execution contract is `EventSemantics.v2 -> deterministic
SemanticNormalizer -> ReaderCard.v2 -> deterministic assembler`: normally two
serial calls because the normalizer and assembler make no provider request;
JSONAdapter may make one format fallback per Predictor, so at most four calls
per route; fallback restarts the full Program, so primary plus fallback is at
most eight. The code-owned 20-second
deadline covers one whole route. One Event still persists one final
SemanticJudgment and one card; this is not a restored Analyst stage. A stale-
ledger re-ask is a separate execution with the same ceiling (normally another
two calls), and both executions remain in the verdict audit.
The `EventSemantics.v2` model-visible projection excludes queue priority,
provider score, Gate macro lexicon, queue lag and watchlist. ReaderCard receives
only the explicit `ReaderCardSemanticView`; it cannot read ToldContext,
`reader_value`, tradability, surprise or development delta.
`news.oi` keys are `window_ms` (4 h), `max_rank_in_window` (2),
`whale_oi_ratio_above_bps` (8000, exceeded not met) and `oi_change_at_least_bps`
(0, disabled): the deterministic open-interest lane's thresholds (#137). Rank
counts only earlier rows that satisfy both thresholds; parsed rows that fail a
threshold remain auditable without consuming rank. Status exposes
`telemetry_received_24h`, `telemetry_parsed_24h`,
`telemetry_parse_failed_24h`, and `telemetry_push_24h`; parser-contract failures
also appear under `dropped_by_rule.oi_parse_failed` and never call a model.
`news.policy` has exactly four v10 keys: `restatement_drop` (true),
`similarity_max` (0.25), `listing_exempt_from_duplicate` (true), and
`stale_source_max_age_s` (43200 = 12 h; #154: an x/twitter artifact already older
than this when the provider pushed it is a replay, withheld as
`stale_source_artifact`; `escalate` is exempt and 0 disables the rule).
Trade-relevance eligibility and objective-guard ordering are code-owned, not
operator thresholds. `direct_surface` requires direct/second-order tradability
and non-empty channels/markets. `material_change` requires `state_change`, or
`material_detail` plus direct tradability or an unscheduled/material surprise;
`realtime_eligible` requires both and magnitude >= 2. After the grounded-
restatement guard, the generic v10 action order is deterministic
listing/telemetry,
grounded watchlist, eligible `reader_value=escalate`, eligible
`reader_value=realtime`, background/none, then
`trade_relevance_inconsistent`; the retained stale-source and same-fact checks
run after action selection. There is no runtime reader quota. Retired quota and v9
action/priority keys
are rejected as unknown configuration instead of being silently carried
forward. `news.retention` keys are `raw_days` (30) and
`judged_days` (365, >= `raw_days`): an Item behind an Event that carries a
verdict or accepted review is evidence and outlives the raw tier.
The exact Strategy 2000 source contract never enters that order: after generic Gate v5 it is composed as
`liquidation_deterministic`, then its own v1 policy emits the parser's
direction-neutral push/drop. Parse failure is a deterministic drop.

Delivery identity is `(event_id, kind)`; `first` is the only kind written —
one Event gets one card — and the retired lane's `followup` rows survive as
history. States are `sending`, `sent`, `terminal`. Telegram target/permission
preflight completes before `sending`; after that row there is exactly one
initial delivery HTTP attempt. A successful Telegram initial attempt may be followed by a durable-intent in-place
edit of that same receipt-bound message; an edit is neither a second delivery nor permission to retry the
initial send. A delivery without a configured sender or whose
preflight fails settles `terminal` immediately instead of holding the message.

Broker contract: topic exchange `news`, dead-letter exchange `news.dlx`, three
quorum business queues — `news.raw` (`raw.#`; single-active), `news.triage`
(`event.#`) and `news.deliver` (`verdict.push`; single-active) — and `news.dead`
(delivery limit 1,000,000 so nothing can lose terminal evidence by returning it). All names take
`news.broker.name_prefix`. Declaring the topology declares exactly those names
and deletes nothing else: any other name under the prefix — the retired Analyst
queue `news.deep` (issue #57), the removed retry lane `news.retry` (issue #400),
another deployment's queue — is reported by `tracefold news bus-check` as
topology drift for an operator to act on by hand.

Queue arguments carry only what a policy cannot express: the queue type,
single-active consumption and the dead-letter queue's evidence-preserving delivery limit.
Retry, dead lettering and resource bounds are one RabbitMQ policy per queue,
generated from `tracefold.news.broker_policy` into
`docker/rabbitmq/definitions.json`: `delayed-retry-type=all` with
`delayed-retry-min=delayed-retry-max=30000`, `delivery-limit=2` (RabbitMQ 4.3
delivers `delivery-limit + 1` times, so three total handler attempts),
`dead-letter-strategy=at-least-once`, `dead-letter-exchange=news.dlx`,
`overflow=reject-publish`, and a measured `max-length-bytes` per queue (64 MiB
`news.raw`, 4 MiB `news.triage`, 4 MiB `news.deliver`, 16 MiB `news.dead`).
`tracefold news bus-policy apply|verify` is the only writer; Workers verifies
the effective policy at startup and refuses to consume on a mismatch.

All three business queues share one delivery limit, so `news.deliver` goes from
the delivery limit of 1 it declared before #400 to the same 2 as the others.
That is not a weakening of the external-delivery fence, because the fence was
never the queue's: `begin_delivery` returns `new` exactly once per
`(event_id, kind)`, and a redelivery settles `ambiguous_after_crash` instead of
sending a second card. What the old limit of 1 actually bought was dead-lettering
a crashed delivery one attempt sooner.

Message bodies are `news_bus_v1` JSON envelopes (`schema_version`, `kind`,
`message_id`, `trace_id`, `occurred_at_ms`, `payload`) with AMQP priority 0 or 5
and an `x-news-trace` header. `BusMessage.attempt` is derived from the broker's
`x-delivery-count` (absent on a first delivery) and is never written by a
publisher. Consumer outcomes are typed and each maps to exactly one AMQP
settlement: success acks, `TransientError` is a counted `reject(requeue=true)`
that the broker delays and finally dead-letters, `DeferError` is an uncounted
`nack(requeue=true)` for when the News DB lane cannot admit the message, and
`PermanentError` or a decode failure is `reject(requeue=false)`; an
unclassified handler exception settles nothing and fails the consumer. There is
no operator control plane: pause and mute were removed with
`news_control_state`, which had never withheld a card, so the only things that
can withhold one are `decide()` and duplicate evidence.

The Alembic chain is `20260818_0275` (the root baseline: it executes
`current_schema_20260818_0275.sql` plus `runtime_roles.sql`) followed by
`20260818_0276_review_49_hard_cut` (drops the retired News title table, the
DEX discovery/token-profile/token-image tables, and the unused LangGraph
`checkpoint_*` tables), `20260818_0277_gmgn_lane_removal` (drops the
social evidence, token identity/registry, DEX/CEX market, live broadcast,
provider circuit, and News market-mark tables), and
`20260819_0278_macro_lane_removal` (drops the ten `macro_*` tables, the four
general market observation tables, `queue_terminal_events`, and the
`reject_macro_fact_mutation()` trigger function). Revisions `0279` through
`0283` add listing admission, instruments and Price Review. The #112 chain is
`0284` through `0290`: immutable fact/evidence snapshots, ReviewDesk v2 and
verified label-v1 removal, content-addressed learning artifacts/recordings,
durable canary control, bounded 90/365-day learning retention, and a
role-authentic Workers evidence-append grant repair. `0291` removes the local
Strategy allowlist. The irreversible `20260822_0292` hard cut adds Program
identity to verdicts, per-Predictor call identity/usage/cost to recordings, and
the append-only deployment-time `program_v1` epoch. `20260822_0293` preserves
that history and appends the corrected `program_v2` epoch; Prompt-era and
`program_v1` learning rows are audit-only and promotion-ineligible.
`20260822_0294` preserves both rows and appends the expert-quality `program_v3`
epoch; Prompt-era, `program_v1`, and `program_v2` rows are audit-only for the
then-current release chain. `20260822_0295` preserves v1-v3 and appends
`program_v4` with factory v2; `20260822_0298` preserves v1-v4 and appends
`program_v5` with factory v3 on the artifact-v2 envelope. `20260823_0301`
hard-renames `news_events.priority` to `queue_priority`, appends atomic
editorial/scored/runtime-manifest identity to verdicts, trips prior canaries,
and starts `program_v6` with factory/executable v4 and policy v10. Every earlier
row and review version is audit-only for the current compiler and release chain.
`20260824_0302` adds the reverse `(event_id, symbol)` read index for bounded
reader-history asset projection; it changes no fact, table, Program, or policy.
`20260824_0303` preserves those rows and appends the #162 `program_v7` epoch
with factory/executable v5 after the Program/Learning package split; v6 remains
immutable audit evidence and is promotion-ineligible.
Issue #175's following code hard cut keeps factory/executable v5, epoch
`program_v7`, and policy v10, but replaces the sole stable Program artifact and
bundle because the composite reader-history/selector retrieval identity and
RulePack text changed. Earlier bundles remain immutable audit history and are
not executable by the new image. Issue #190 reissues that sole bundle again,
still inside v7, because the package-owned canonical identity primitive now
rejects NaN/Infinity.
`20260824_0304` carries Issue #193's strategy-artifact hard cut into the
database. It adds and drops no column — the artifact is image-carried JSON, not
a table — and does exactly two things: it trips every armed or active canary,
whose candidate the new image cannot load, and records one migration receipt in
the append-only learning ledger. `program_v7` is deliberately not re-opened:
accepted `news_review_v4` truth stays eligible, and the epoch row keeps naming
the factory, schema and baseline root it was opened with. The sole stable root
re-issues a third time inside v7, now as the 262-byte
`news_program_strategy_artifact_v1` document under factory v6.
`20260825_0305` carries the compile-record half of the same issue. It adds
`compile_record` to the `news_learning_artifacts` kind constraint and leaves
`compile_receipt` in it, so retired rows stay readable audit history, and it
trips every armed or active canary: a candidate registered against the old
receipt chain names a document the new image cannot validate, so it can no
longer be evaluated. `program_v7` is again not re-opened.
`20260827_0315` carries Issue #288's exact source-contract route and Event-kind
hard cut. It trips open canary activations and appends the factory-v6 to
factory-v7 receipt without rewriting or appending the `program_v7` epoch row.
Accepted review labels remain immutable truth, while exact current-bundle
eligibility makes prior-factory judgments audit-only and starts the factory-v7
cohort at zero.
`20260828_0316` then adds the #283 immutable `trading_intents` handoff and its
least-privilege Workers, Serve, and Nautilus grants.
`20260828_0317` performs the atomic authority cut: it refuses unresolved legacy
Cases, nonterminal Intents, or active/unknown legacy Orders, admits
`INTENT_EMITTED`, and revokes legacy execution mutations from Workers. It has no
downgrade because restoring a second writer is not a safe rollback.
`20260828_0318` is #306's prompt-layer hard cut: it appends the `program_v8`
epoch for `factory_v8`, trips every armed or active canary, adds no column, and
is irreversible. Two byte changes land under that one identity migration —
the kernel/RulePack/advisory/seal layering collapsing into one seed instruction
per Predictor, and the Program's self-owned chat transport composing the request
envelope — deliberately paid once rather than twice.
`20260828_0319` is #310's envelope hard cut: it appends the `program_v9` epoch
for `factory_v9`, trips every armed or active canary, adds no column, and is
irreversible. The structured-output constraint now follows the endpoint —
`json_schema` where supported, `json_object` with the same schema inlined into
the system message where not — which moves fallback-route prompt bytes while
leaving both seed texts unchanged.
`20260828_0320` adds append-only execution-capability snapshots and replay
receipts, the active capability/blacklist revisions, the distinct bootstrap
account-zero proof, TradeIntentV2, and immutable News instrument-listing
validity events used by source-time replay. It requires `PAUSED` with no
nonterminal Intent, rejects every new V1 insert, and has no downgrade.
`20260828_0321` is #314's computed-identity cut and the last epoch migration
there will be: `news_learning_epochs` gains `bundle_sha` and `envelope_sha256`,
`epoch_id` is tied to `left(bundle_sha, 8)` by CHECK, `program_factory_id`
becomes nullable, and `tracefold_workers` gains INSERT so the running deployment
opens its own epoch at the startup barrier. UPDATE and DELETE stay revoked and
the append-only trigger stays.
`20260828_0322` adds the News delivery edit-intent columns and stale-intent
index used to distinguish a desired, confirmed, or ambiguous in-place Telegram
update without changing the initial `sent` state.
`20260828_0323` adds the receipt-bound deletion intent, five-venue evidence,
reason, settlement timestamps, and stale-intent index used only after
authoritative single-name tradeability absence.
`20260828_0324` makes the edit and delete lifecycle shape checks two-valued and
refuses to advance if any existing delivery row has a partial lifecycle shape.
A database
at an earlier revision upgrades with `tracefold db migrate`; a fresh database
runs the complete chain. The exact
News base-table set plus four security-barrier review views is asserted by
the schema integration test instead of a duplicated prose allowlist. Migrations
perform no provider, broker, model, or outbound call and have no compatibility
reader/writer.

- `GET /api/news/quotes?symbols={comma-separated}` returns one result per
  requested symbol, in request order, for at most 100 deduplicated symbols
  (`news_quotes_symbols_too_many` / `news_quotes_symbol_invalid` otherwise).
  Each result carries the requested symbol, the exact resolved symbol/base, the
  venue and venue symbol, instrument class, quote asset, price, `price_kind`
  (`last|mark|mid`), optional `change_pct` with the `change_basis` it came from
  (`rolling_24h|provider_day`), `source_at_ms`, `received_at_ms`,
  `received_age_ms`, optional `source_age_ms`, `effective_age_ms`, and
  `freshness_basis` (`source_and_received|received_only`). It also carries the
  independent `reference_at_ms` / `reference_age_ms` clock. `age_ms` does not
  exist. `state` is `fresh` when every applicable raw clock is no more than
  5,000 ms in the future and effective age is <=45,000 ms; otherwise a result
  with a price is `stale`. `unavailable` (nothing quoted yet) and `unlisted`
  (no venue we poll lists it) carry null timestamps, ages, basis and reference.
  A price is a positive decimal
  string or `null`; it is never `0`, and a failed venue leaves the previous row
  in place rather than blanking it. `change_pct` is `null` until the venue's day
  reference is known and becomes `null` again above 360,000 ms or beyond the
  future-skew bound. Reference expiry removes only the percentage: current
  price, basis and timestamps stay. Binance refreshes the reference only after
  a successful current store and persists it on the next natural turn;
  Hyperliquid's native reference shares the current receipt time. Current
  quotes are deliberately **not** feed
  fields — a price that changed must not invalidate the Feed ETag or re-run its
  count query every three seconds.
- `GET /api/news/symbols/{base}` returns what one `base_symbol` *is* (#207
  PR-W1): `known`, `tradeable`, `venues`, the `contracts` it names, and the
  operator-alias `normalization` group when one collapses more than the base
  itself. Identity only — the token page's Events, price and rank window each
  keep their own endpoint, so nothing here is a second answer to a question one
  of them already answers. `base` is normalized (uppercased, `XYZ-` stripped)
  and must match `[A-Z0-9._-]{1,24}` or the request is
  `news_symbol_invalid`. `known` and `tradeable` are different answers: `known`
  says some venue we poll lists the name, `tradeable` excludes the reference
  tier, because a `us.listed` contract proves a ticker exists and not that
  anyone can trade it (#91). A reference-only contract is returned with
  `reference_only: true` rather than filtered out. A base no venue lists is
  `known: false` with empty lists and **200**, not 404: every asset chip on the
  console links here, including tags that resolved to nothing, and that answer
  is what a reader following one came for. `underlying_key` is deliberately
  absent — `crypto:{BASE}` is a Trading identity owned by
  `tracefold.trading.contracts`, and a News route must not assert it.
- Workers refresh both public venue catalogs without credentials. Each refresh
  appends a content-addressed snapshot and atomically moves only that binding's
  pointer. Provider error marks that binding `error` when no snapshot exists or
  `stale` while retaining last-known-good; it never empties the catalog or
  changes the other binding. There is no operator refresh command and no
  execution authorization in this operation.
- `tracefold trading replay-oi --days 7 --strategy
  source_native_oi_smart_money_long_v3 --venues binance.perp,hl.perp --fidelity bar_v1`
  gives every bounded source fact one terminal source-native BAR outcome. It
  reports decision, independent capital admission, execution/coverage,
  gross/fees/net-ex-funding, MFE/MAE, and explicit fidelity limitations. The
  response names an immutable artifact and PostgreSQL receipt by deterministic
  `run_id`; funding and portfolio drawdown remain `null`.
**One HTTP owner per durable aggregate (#331, #350).** Nothing crosses: a Case
carries frozen evidence plus independent Policy and Capital attribution; an
Intent carries its lifecycle and a `case_id` back-reference; status carries
orthogonal durable runtime facts and bounded totals.

- `GET /api/trading/status` — `decision`, `capital`, and the two closed
  `bindings`. Decision exposes state/heartbeat/reason; Capital exposes
  control/blacklist revision; each binding exposes only durable redacted
  credential/runtime/account/catalog/heartbeat/reason facts. Serve reads no
  secret file and constructs no provider client.
  `policy` names the identity a *new* Case would be frozen under and is never
  applied to an existing one. `counts` is a bounded aggregation over durable
  rows — no funnel, no per-poll counter. Money is an exact decimal string.
- `GET /api/trading/cases?underlying={base|crypto:BASE}&state={open|no_trade|blocked|emitted}`
  — the Case/Decision aggregate. Each Case carries its raw `state`, its terminal
  `policy_decision` / `policy_reason`, independent
  `capital_disposition` / `capital_reason`, the frozen `policy_config` and
  `policy_checks` (check, operator, threshold, measured, passed) it was decided
  on, the frame's own measurements, and a nullable `intent_id` link. It is the
  replacement for the retired `intents.cases_without_intents`, and the two
  never coexisted. Bounded to 100 rows; the response says whether it is complete.
- `GET /api/trading/intents?underlying={base|crypto:BASE}&state={active|closed|all}&day={YYYY-MM-DD}`
  — the Intent/Outcome aggregate. `active` means
  `PENDING | IN_FLIGHT | OPEN_PROTECTED | MANUAL_REVIEW`; `closed` means
  `TERMINAL`. A day filter keeps every active row and selects terminal rows by
  authoritative `closed_at_ms` within that UTC day. The response carries
  immutable Demo identity/policy values, execution state/phase/outcome,
  authoritative quantity/price/protection/flat timestamps, and PnL/fees when
  known. It never returns a Case list, a Case manifest, a provider payload, an
  account identity, or a legacy Order/observation.
- `GET /api/trading/gate` — the Source/Admission aggregate over a bounded
  24-hour window: the admission `config` its rows were filed under, then per
  Source the status, stage, named reason, retryability, version/config digest,
  frozen evidence, timestamps, attempt count, `research_only`, and the linked
  `case_id` when one exists. It publishes no Case state and no execution state.
- `GET /api/trading/gate/{event_id}?lane=oi` — one Source's admission answer.
  `joinable` is the honest half: only the deterministic OI lane's source key
  (`oi:{event_id}:{metric_version}`) is reconstructible from an Event id, the
  model lane's is a content hash of an artifact and a fingerprint (#154), and
  for anything but `lane=oi` the answer is `joinable: false` — "this cannot be
  asked", which is a different fact from `decision: null`, "it was asked and the
  lane has no row". Joining by symbol and time instead would record a link the
  ledger does not have.
- `/api/news/feed` and `/api/news/events/{event_id}` additionally carry the
  Event Reaction: the feed the compact event-level aggregate (median signed
  return of the Triage primaries that price, with `state`
  `pending|partial|complete|unavailable`), the detail every per-asset row with
  its pinned venue, raw closes, close timestamps, returns, metric version and
  unavailable reason. A current quote and an Event Reaction are different
  response types with different words; no field named simply `change` carries
  either meaning.
- `/api/news/status.price` reports per-source quote freshness (source key,
  target and quote counts, receipt/source/effective ages, freshness basis and
  worst state across that source's applicable quotes) and the Reaction backlog
  (partial/complete/unavailable over 7 days) beside the pipeline's own health.

## CLI

`uv run tracefold --help` is the exact CLI source of truth. Stable top-level families are:

- service/config: `serve`, `workers`, `nautilus run`, `init`, `config`;
- database: `db migrate|health|audit|query-audit`;
- News: `news bus-check|control|instruments|review|learning|replay|why|dlq`;
- Trading: `trading status|cases|show|replay-oi|blacklist|control`;
- maintenance: `ops validate-projections`.

There is no `recent` or `search` command and no market rebuild/sync/reconcile
maintenance command. Mutating maintenance commands require an explicit
execution flag where the parser offers a dry-run mode. They operate from
persisted facts and stable target keys. A rebuild does not create an alternate
generation/run identity or make a provider response the source of truth.
There is no CLI command that creates, approves, rejects, resolves, submits,
amends, or cancels an execution. Nautilus owns execution from the durable
Intent contract.

`validate-projections` is a strict Serve-role read. It does not acquire the
maintenance lock, so operators can inspect the running singleton without
interrupting it.

`db audit` reports the migration revision, catalog row estimates for every table in the
code-owned `NEWS_TABLES` contract, `news_schema` exactness over that same set,
and the runtime-role contract including a role-authentic Workers evidence
append without rewrite access (current at migration `20260830_0337`). `db audit --deep`
adds exact table counts for offline migration or restore evidence. Since
#104 it also reports `trading_schema` over the code-owned `TRADING_TABLES`
contract; the two registries stay separate so "exactly these tables" remains a
per-capability claim.
`db query-audit` covers bounded reads for `/readyz`, `/api/status`, and every
News GET. The catalogued write-route set is empty since #256 — the audit still
asks for it, so a write route added by accident fails the contract rather than
slipping in unnoticed. `/healthz`, `/metrics`, and `/api/bootstrap` are declared
no-SQL routes.

`news bus-check` connects, declares the topology idempotently, and prints
per-queue message/consumer counts.
`news review queue|evidence|submit|external-miss` is the whole ReviewDesk
contract since #256; submissions require the task version and an idempotency
key, and open one explicit read-write transaction under `tracefold_serve`,
which PostgreSQL permits to INSERT only the two append-only review fact tables
and still denies every News/control rewrite.

`news learning baseline (--dataset SHA | --from-ms N --to-ms N)
[--mode recorded|compile_live|runtime_live] [--action-source recorded|policy]
[--max-model-cases N] [--semantic-judge MODEL] [--limit N]
[--out FILE]` scores the
stable Program over accepted reviews and returns one content-addressed
`tracefold.news.program_baseline_report.v3`. It is read-only — no dataset write,
sandbox, tariff or container, no write to any table, and the only database
contact is one `serve` connection that closes before the first model call.

The two corpus forms are mutually exclusive because a run can only measure one
of them (#199 §5). `--from-ms/--to-ms` is a moving window anchored to the clock:
the population changes underneath it, so a before/after taken across two of them
compares two different corpora, and the receipt says `cohort_scope: current` —
discovery, never release evidence. `--dataset SHA` is the exact frozen
development dataset a trusted compile would seal. It re-projects the sealed
corpus once, builds the same `GepaObjectivePlan` readiness and `run_gepa` build,
and scores **only** `target + control`: excluded diagnostics are counted and
named in the report's `objective` section and never enter a denominator, because
a retrieval miss averaged into the "before" number is movement a candidate can
be credited for without repairing anything. `subsets` publishes three separate
numbers — `train`, `development_selection` (the formal *before* value a Candidate
is picked on) and `optimizer_union` (a diagnostic) — and `identity` carries the
dataset SHA and the `episode_projection_root_sha256` that a candidate's
`ProposalReceipt` records and `CandidateEvaluator` re-derives, so readiness,
this baseline, the registration and the release gate can be checked against each
other. `--max-model-cases` must cover the whole optimizer corpus
(`news_program_baseline_dataset_requires_full_corpus_budget:N`): a truncated run
would publish split roots describing cases it never scored. A blocked plan is
refused outright (`news_program_baseline_dataset_objective_blocked:<reasons>`)
rather than published with an empty `subsets` block that reads as a measured
zero — `readiness` explains the same blockers for free.

The equivalence judge gets no admission ceiling of its own here, and #253 tried
the other way first: a judge that reaches its ceiling does not raise, it returns
`unavailable`, `retains()` reads that as not retained, a failed
`factual_fidelity` arms the `factual_contradiction` hard gate, and the case
scores zero. An under-sized ceiling would therefore publish a depressed baseline
that reads as a measurement. `--max-model-cases` pins the corpus and so pins the
judge's work, which is the bound that exists.

`--dataset` runs `--mode compile_live` and nothing else
(`news_program_baseline_dataset_requires_compile_live`), and requires
`--semantic-judge` on the configured reflection route
(`news_program_baseline_dataset_requires_semantic_judge`,
`..._requires_compiler_reflection_judge`). `subsets.development_selection` is
published as the formal *before* value a Candidate is picked against, so it has
to measure what the optimizer measures — the production graph on one task
endpoint, judged by the ruler `run_gepa` refuses to run without. `recorded`
scores the action that actually shipped while the Objective Plan classifies under
a replayed `decide()`, so the two disagree on any case whose ledger state
differed at ingest and the report would call a case a control and zero it in the
same document; `runtime_live` measures the four-slot production route with retry,
fallback, deadline and circuit, which is a reliability question and not
comparable to a candidate selected on the cold graph; and `bind_metric(None)`
compares free-text retention byte-for-byte and fires `factual_contradiction` on
every failed `factual_fidelity`. All three remain available in the moving-window
form, which names itself discovery. For the same
reason the retrieval receipt in a dataset-bound report is computed over the
**whole** sealed export rather than the scored subset: the plan excludes exactly
the cases `_retrieval_receipt` counts as misses, so a receipt over
`target + control` would report a recall biased toward 1.0 by construction.

The three modes answer three different questions and are never interchangeable
(#150 removed the single ambiguous `live`, with no alias):

| Mode | Executes | Question |
| --- | --- | --- |
| `recorded` | the persisted `ScoredJudgment` against the complete `DecisionResult` that shipped | is metric wiring reproducible over history? |
| `compile_live` | the production native DSPy Program on one task endpoint, no fallback slot | what baseline does GEPA optimize against? |
| `runtime_live` | the configured four-slot native DSPy Program | does the production Program route answer these cases? |

`compile_live` is exactly the native Program GEPA maximizes and deliberately has
no fallback route. It disables the production whole-route deadline and
cross-case primary breaker that GEPA does not run, while retaining the task
endpoint's per-call timeout and DSPy JSONAdapter's single format fallback per
Predictor, so its failure rate is not the reader's. `runtime_live` is built by the same seam
the Workers use — a dedicated ReaderCard binding is honoured rather than
silently aliased to the EventSemantics primary — and runs cases sequentially in
`(opened_at_ms, case_id)` order so circuit state is a property of the run rather
than of scheduling. It is a **Program-route** replay only: `execution_scope`
names what it still excludes (the consumer transaction, the advisory lock,
stale-evidence re-ask, the degraded wire-card fallback, the broker and
delivery).

Both live modes verify every example's frozen policy *before the first provider
call* and refuse the whole run with
`news_program_baseline_policy_unusable:<case>:<reason>` — a corpus that cannot
verify its own policy is a pure function of the input, and discovering it after
two Predictor calls per case turns it into "the route did not answer".

Every mode is current-cohort only. The moving-window repository requires the
exact current judgment contract and active epoch; there is no flag that widens
it to retired rows. A live mode generates a verdict with the current Program
and scores it under the current policy. `--mode recorded --action-source
policy` remains invalid because recorded mode measures the action that actually
shipped, not a replay under another action source.

`--action-source` has exactly one valid value per mode and the handler rejects
the other: `recorded` outside `--mode recorded` short-circuits the policy replay,
so a live mode would generate a fresh verdict and score it against the action a
*different* verdict shipped, silently emptying the metric's heaviest component.
`--max-model-cases N` is required by both live modes and caps the corpus read —
`runtime_live` spends two to eight sequential provider calls per case on the
endpoints that also serve production Triage, and every other model-spending
command in this plane makes its budget mandatory.

The report has no single ambiguous `score`. A provider failure is an outcome,
not an absence, so it is published twice: `scores.case_macro_answered` is
quality given an answer and `scores.case_macro_failure_as_zero` is the
end-to-end lower bound, with the same pair at connected-fact-cluster grain
(one cluster, one vote) plus a deterministic bootstrap interval on the release
evaluator's seed/replicate convention. `population` carries
requested/answered/failure counts, `failures.by_code` keeps each error code
separate — a raise inside the metric is a `metric_error:*` code and never a
provider failure, because one is a defect in the ruler and the other is route
availability — and `action_confusion` splits agreement by `must_push`,
`should_push`, `must_hold` and `should_hold`. `hard_gates.by_gate` names which
gate zeroed each case (`must_push_miss`, `must_hold_send`,
`background_realtime_send`, `factual_contradiction_unchanged`,
`ungrounded_primary_asset`, `schema_invalid`, `relevance_inconsistent`,
`known_duplicate_leak`, `advisory_rejected`, `card_lint_url`,
`card_lint_self_description`). A gated case keeps its resolved action and its per-dimension
outcomes: the zero enters every denominator rather than leaving it, or a
candidate with more hard failures could publish a higher per-dimension hit rate.
Metric `tracefold.news.production_action_trade_relevance_v6` weights 45% exact
final production action, 35% exact TradeRelevance dimensions, 10% existing
semantics/novelty, 10% ReaderCard reviewer anchors and 10% the deterministic
ReaderCard copy lint, normalized over the components a case carries. The lint
publishes eight scored checks (`headline_language`, `headline_length`,
`headline_number_count`, `banned_filler`, `meta_opening`, `why_length`,
`why_single_sentence`, `no_emoji`) and its two gates in the metric receipt under
`card_lint`, tables included. `headline_number_count` compares how many
decision-relevant numbers the headline carries against how many the source
stated, never which: a faithful rendering restates `$1.5B` as `15亿美元` and
`5.50%` as `5.5%`, so a literal-identity test would fail exactly the conversions
the card contract asks for and teach the optimizer to copy ASCII digits instead. Reports expose each component's effective
denominator, effective weight mass, gold coverage and field count. The score is
identical with or without `pred_name`; that argument filters feedback only. EventSemantics receives relevance, semantics, novelty and its owned action
feedback; ReaderCard receives headline/why/factual feedback and action feedback
only for a headline-caused duplicate. Reviewer correction prose reaches a
Predictor only when it has an owned failed dimension; it is not broadcast
unconditionally. Listing/telemetry are outside the relevance denominator, and a grounded-
watchlist objective guard is policy evidence rather than action feedback for a
Predictor.
A run where nothing answered publishes a null `case_macro_answered` and the full
failure breakdown rather than refusing — "the route answered nothing" is a
result, and it used to be the only one that produced no receipt at all. `review_label_distribution` is
corpus metadata (what reviewers labelled) over every requested case, grouped by
the stage each label describes — `event_semantics`, `reader_card`, `delivery`
(where `timeliness` lives, scored by nobody because the verdict has no such
field) and `not_scored` for a rubric dimension nobody has placed yet. That grouping is deliberately not called "owner":
`review._OWNER_BY_DIMENSION` owns that word for a different question (who is to
blame), under which `asset_grounding` is a Gate defect. `prediction_dimensions`
is what this candidate did — exact-gold hit/miss, accepted-retention hit/miss
including #148 semantic-equivalence decisions, and not-scored-without-gold — and
moves when predictions move. `runtime_live` adds `route` (primary/fallback,
`unanswered_n`, `retry_count`, call and physical-call counts, input/output
tokens, known provider cost and `cost_unknown_n` rather than a fabricated zero)
and `latency_ms` p50/p95/max over answered cases, with `p95_with_failures` /
`max_with_failures` beside them — a route that exhausts the chain is the slowest
case there is, and hiding it would understate the tail an operator bounds the
run against. A case that failed still reports what it spent, read
from the `SemanticJudgeError` partial trace: a route that exhausts the chain
can cost eight calls, and counting zero made the receipt least accurate exactly where
the route was worst.
`report_sha256` covers the measurement with wall-clock latency excluded, so two
runs with identical predictions publish the same address; `latency_sha256`
addresses the timings separately. `identity.case_root_sha256` answers "the same
cases?" and `identity.corpus_sha256` answers "the same inputs?" — hashing ids
alone let one address describe two corpora, because any evidence edit that kept
the ids left the receipt untouched.
Both live modes report the same route and latency facts and execute the same
native Module. What separates them is both the endpoint binding and availability
controls: `compile_live` disables the whole-route deadline and cross-case breaker
that only `runtime_live` runs. `execution_scope` says so. Neither receipt contains a
credential or an endpoint URL.

Policy is frozen into each scored example rather than read from process-global
state: `policy_metric` carries the exact `policy_values` and `policy_sha256` of
the arm, the shared pure/version-bound `production_decision()` builds
`DecidePolicy(**policy_values)`, validates one `ScoredJudgment`, and returns the
complete `DecisionResult` (action, rule and throttle key). A missing or mismatched policy fails closed instead of
falling back to `DEFAULT_POLICY`. A report spanning two policies is refused
rather than labelled with one of them. `recorded` returns before policy replay,
so its `identity.policy_sha256` is an explicit `null` —
naming a policy the number does not depend on would be the same ambient-state
confusion in a different place. `identity.policy_source` says where the replayed
values came from: `active_arm_manifest` is the configured current arm, and only
current-cohort episodes are eligible. An episode with no
complete recorded `DecisionResult` is refused in `recorded` mode rather than quietly falling through
to a policy replay. The sealed compile projection is
`tracefold.news.development_compile_episode.v4`. The projection is recomputed
from `news_reviews` on every read, so a *dataset* is never stale; what the field
refuses is a **compile record** written under an older projection — v2 carried no
policy and would have raised inside every metric call, and v3 could not say
whether a human wrote `first_bad_owner` or ReviewDesk derived it, which is
exactly the difference between a Prompt-owned target and somebody else's defect.
A record naming an older projection fails
`news_learning_program_compile_record_invalid` rather than being re-read under
rules it was not produced under.

`--mode recorded` makes no provider call and reads only exact current-contract
episodes from the active epoch.
`--semantic-judge MODEL` scores free-text retention anchors by meaning instead of
byte equality (#148) through the same `CardEquivalenceJudge` contract that the
the optimization wires through its separate `metric_judge` role. Judge failure is explicit unavailable, enters the affected
free-text dimension as zero, and is counted/costed with no byte-equality fallback,
hidden retry or cache. Magnitude, direction, assets, novelty and every
TradeRelevance field stay exact; the strict byte-equality mean is reported
alongside as `scores.case_macro_answered_byte_equality`.
Reviews whose `evidence_version` has been superseded are not replayable and are
excluded, the same rule `_load_case` already enforced.

The `0336` genesis removed the retired replay fixture. Current metric evidence
comes only from exact `news_judgment_v2` rows created in the post-genesis active
epoch; no repository fixture provides a legacy recorded-mode input.

Reviews are accepted under `news_review_v6`. Its exact Gold includes the five
taxonomy axes; its optional `expected` block continues to cover magnitude,
direction, assets and the seven TradeRelevance fields
(`trade_impact_breadth`, `trade_tradability`, `trade_surprise`,
`trade_development_delta`, `trade_channels`, `trade_affected_markets`, and
`reader_value`). Accepted `novelty` and `should_push` are already their own
typed truth rather than duplicate `expected` fields. Every failed scored
dimension must have expected gold; otherwise it is not scored, with no
any-change fallback. Channels/markets canonicalize before exact comparison. Historical
v2-v4 rows remain readable audit history but cannot enter current metric/GEPA/release
evidence. Listing/telemetry do not enter relevance gold; grounded-watchlist
cases are separated as policy evidence. `gold_coverage` reports how much of each
component is actually scored.

`news learning snapshot|compare` — the #193 research window of frozen run
directories and student/teacher arm comparison — was deleted in #343 together
with the `tracefold.news.learning.experiment` package; `optimize` over a frozen
development dataset is the only research entry.

`news learning optimize --development SHA --out DIR --max-metric-calls N
--max-task-model-calls N --max-reflection-model-calls N
--max-metric-judge-model-calls N --max-cost-microusd N
--max-call-cost-microusd N [--max-wall-clock-seconds N] [--seed N]` (#202) is
the one optimization entry point in the repository. It reads the frozen
development corpus once as `serve` and then holds three model endpoints and a
typed budget — no database write credential, no broker, no delivery, no canary,
no promotion, no Docker, no compiler image, no sandbox, no proxy sidecar, no
tariff. The task LM is the configured production Program route rather than a
command-line model, because a number optimized against a different route
predicts nothing about production.

It ends in exactly one of three terminal states, and every one of them writes
`DIR/optimization_report.json` (`news_optimization_run_report_v1`):

| Outcome | Meaning | Candidate | Exit |
| --- | --- | --- | --- |
| `NO_OP` | the optimizer kept the seed; no verifiable improvement | none | 1 |
| `REJECTED` | the run violated a quality, safety or budget bound, or the Objective Plan refused the corpus before any call | none | 1 |
| `ADVANCE` | one bounded Prompt patch, still with no production authority | `DIR/prompt_candidate.json` | 0 |

A `REJECTED` caused by the Objective Plan costs nothing: the plan is built
before any endpoint is touched, which is the same answer `readiness` gives with
zero model calls. The per-call cost ceiling is also the rate an unpriced
provider call is charged at — neither endpoint this project runs on returns a
price litellm can resolve — so over-charging stops a run early rather than late.

`news learning run --development SHA --out DIR --max-baseline-model-cases N
--max-metric-calls N --max-task-model-calls N --max-reflection-model-calls N
--max-metric-judge-model-calls N --max-cost-microusd N
--max-call-cost-microusd N [--max-wall-clock-seconds N] [--seed N]` (#253) is
the one recommended GEPA path. It composes `readiness`, `baseline --dataset
--mode compile_live` and `optimize` into one directory and defines no second
Objective Plan, Metric, split, budget or optimizer; the budget flags are the
underlying commands' own, because inventing defaults for them would be a second
budget. They divide the way those commands do: every ceiling except
`--max-baseline-model-cases` bounds the optimization leg, and the standalone
baseline is bounded only by its corpus, which `--max-baseline-model-cases` must
cover exactly. It registers, accepts, promotes and deploys nothing.

It takes no `--semantic-judge`: the equivalence judge is
`llm.news_compiler_reflection`, the route `optimize` cannot be told to leave, so
the two legs cannot be handed two different rulers. It refuses a
`--max-baseline-model-cases` below the optimizer corpus
(`news_learning_run_baseline_budget_below_corpus:M<N`) using readiness's own
free count, before any provider call. A corpus readiness reports as
`insufficient` skips the baseline — which refuses a blocked plan anyway — and
goes straight to `optimize`, whose `REJECTED` for that corpus costs nothing.

The directory holds `readiness.json`, `baseline-compile-live.json`,
`optimization/optimization_report.json`, and
`optimization/prompt_candidate.json` on `ADVANCE`. Freezing with `--out
DIR/development.json` makes the same directory loadable by
`notebooks/news-gepa-frozen-run-evaluation.ipynb`.

The separate `run_summary.json` comparability projection
(`tracefold.news.gepa_run_summary.v2`) was deleted in #343: with the three legs
composed in one process over one dataset SHA and one judge route derived from
`llm.news_compiler_reflection`, same-population is true by construction, so no
fourth document re-checks the other three. The three baselines keep their own
homes and their own meanings — the standalone number in
`baseline-compile-live.json`
(`subsets.development_selection.case_macro_failure_as_zero`), the GEPA seed
number in `optimization/optimization_report.json`
(`trajectory.val_aggregate_scores[0]`, the seed Program's score inside the run
that proposed against it), and the future test number only ever from `release
evaluate --stage holdout` against a post-registration ValidationDataset. Two
physical runs of one graph may still differ in the last digits; a difference is
not by itself evidence that a dataset identity is wrong. Exit `0` is `ADVANCE`;
`1` is `NO_OP` or `REJECTED`, both complete answers.

`news learning draft-reviews --model MODEL --out FILE [--hours N] [--limit N]
[--include-reviewed]` proposes `news_review_v6` rubrics for
a human to accept and writes a file, never a review. It drafts from the
ReviewDesk queue over the `--hours` look-back window — the `--events-from` form
that drafted the Events a #193 experiment run had frozen went with that loop in
#343. A batch refuses duplicate task identities before its first model call
and reports `tasks` beside `unique_tasks`; one ReviewDesk task can therefore
consume at most one drafting call.

`news learning readiness --development SHA [--out FILE]` explains one frozen
development dataset before anyone spends a provider call on it: it re-projects
the sealed corpus, builds the one `GepaObjectivePlan`, and reports
`target / control / excluded` with a reason for every exclusion, the explicit vs
derived owner distribution, exact-gold coverage by dimension, the train and
development-selection halves of the honest split with their case and cluster
roots, the required strata, retrieval verifiability, and a per-metric-call task
and judge envelope computed from the corpus. Objective Plan v2 elects exactly
one optimizer representative per connected fact cluster before the split:
target before control, then target-dimension count, safety status,
newer Event and stable case id. Other members remain frozen diagnostics with a
`cluster_representative_shadowed:*` reason. Split receipt v2 proves the resulting
one-case-per-cluster invariant; if election removes a target, control or required stratum from either half,
readiness remains fail-closed. It makes no task, reflection or
judge call and writes nothing. `outcome` is `ready` or `insufficient`; the exit
code stays `0` for an `insufficient` report, because refusing to optimize a
corpus that cannot support it is a result rather than a failure. `insufficient`
means exactly that — a corpus that cannot support an optimization, including the
one blocking reason that has no episodes behind it,
`dataset_agent_cohort_mismatch` — and it is reported in the same shape as a
`ready` report, every section present. A wrong argument is still an error: a
validation-role SHA, a bumped epoch or drifted evidence raise their own codes
and a non-zero exit rather than being dressed up as insufficiency. Readiness is an
explanation in advance, not a bypass — `run_gepa` rebuilds the same plan and
refuses on the same conditions, and `CandidateEvaluator` rebuilds it again from
the frozen dataset and requires the candidate's declared failure clusters,
target dimensions and split roots to equal it exactly.

Optimizer candidates publish `optimization_objective_summary.v2`, including
the Objective Plan schema plus the representative case ids, count and root. Registration
re-derives and compares that population. A
candidate that declares split roots with an older or missing plan identity is
registration-ineligible; its append-only artifact remains historical evidence.

The report is `tracefold.news.gepa_readiness_report.v2`. v2 adds a `coverage`
block carrying the frozen dataset's own sealed counts — `case_n`,
`independent_cluster_n`, `boundary_cluster_n`, `retention_cluster_n`,
`negative_cluster_n`, `safety_cluster_n`, `stratum_n`, `eligible_event_n`,
`natural_day_n`, `window_duration_hours` — republished verbatim rather than
re-tallied, and present with `null` values on the one path that cannot project a
corpus at all. The last two are diagnostics and are read by no gate (#259):
`natural_day_n` is how many distinct UTC dates the accepted cases opened on and
`window_duration_hours` is the length of the frozen window, so the pair says how
concentrated the corpus is and the two may disagree freely — a 72 h freeze whose
reviews all landed in one afternoon reads `1` and `72.0`.
`release evaluate --stage offline|holdout` decides
development evidence on the cluster-role, stratum and safety counts alone.
Out-of-time generalization remains the Future Holdout's alone — `validation`
still requires a window strictly after candidate registration, ≥ 24 h, ≥ 200
eligible Events and ≥ 30 primary clusters. Removing the day gate moves
`TRUSTED_ROOT_SHA`, and the profile is named `news_learning_release_v2` so one
readable name cannot stand for two sets of gates; a v1 dataset or candidate is
audit history and a new experiment re-freezes.

`news learning freeze` seals accepted reviews into a content-addressed
development or future temporal validation dataset. Every current dataset is in
the running bundle's runtime-owned epoch and accepts only `news_review_v6`;
every earlier Prompt/Program/review cohort is audit-only and cannot enter a
dataset or metric-v4 denominator.
The CLI is two groups, because there are two lifecycles (#202 §11 PR-E). `news
learning` freezes a corpus, explains what GEPA may optimize, scores the stable
Program and runs the one optimization — `readiness`, `baseline`, `run`,
`draft-reviews`, `taxonomy-register`, `taxonomy-evaluate`, `optimize`, `freeze` — and none of them can ship anything.
`taxonomy-register` seals the exact tested code, taxonomy shadow Program/model
binding and active production identities at a PostgreSQL-clock timestamp before
the future holdout opens. `tested_git_sha` is derived from the current
content-addressed Workers deployment receipt, never accepted as operator input;
an unversioned image/revision or mismatched active bundle is rejected.
`taxonomy-evaluate --file CASES --out REPORT` seals a cluster-deduplicated
`TaxonomyEvaluationReportV1` and writes it through the existing append-only
learning artifact ledger. The command accepts only database-verified Review v6
Gold and exact replayable `shadow_observation` artifacts under that durable
registration; it reports every preregistered denominator and gate. Insufficient
development or future holdout evidence forces `UNKNOWN`. The four existing
regression gates are re-read from PostgreSQL release evidence and must bind one
exact current candidate, dataset and metric; a file cannot declare their
outcomes.
`run` is the recommended composition of `readiness`, `baseline` and
`optimize`; the rest stay callable one at a time. `news release` admits a
candidate and moves it: `register`,
`evaluate`, `shadow`, `canary`. The split is what an operator reads off
`--help`, and it is the same boundary the packages carry: `news.learning`
never imports `news.release`.

`release register --development SHA --candidate FILE --artifact-root DIR
[--hypothesis TEXT] --out FILE` (#202) binds one `news_prompt_candidate_v1` to
the active stable Program and a frozen development dataset. Whatever wrote the
two instructions — `learning optimize`, a research run, or a person — enters
here on identical terms, because the generator is audit, not permission. The
command re-applies the patch to the running stable to derive the candidate's
Program identity, re-projects the corpus and re-derives the #199 Objective Plan
rather than trusting the candidate's own `objective_summary`, and refuses a
candidate whose declared projection root, Objective Plan schema, representative
optimizer population identity, or split disagrees. These checks run before any
candidate artifact is written. It stores the
candidate under kind `prompt_candidate` keyed by its own `candidate_sha256`, and
the `ProposalReceipt` carries that root plus
`development_episode_projection_root_sha256` — the registrar's own projection,
which is what makes a review edited between generation and evaluation visible.

There is one candidate kind. `CandidateManifest.target` (`program | policy`) is
gone: a policy change is a configuration release with its own gradual-rollout
capability, and dressing it as a learning candidate gave it an Objective Plan, a
development dataset and a blind pairwise stage for a change no optimizer
proposed and no metric scored. Rows registered under the old contract stay in
`news_learning_artifacts` as append-only audit and no longer parse, so they
cannot be re-armed (migration `20260825_0307` trips anything still open).

`release evaluate` runs the
development/offline or validation/holdout release gate; validation calls both
arms sequentially and `--live-program` can append exact per-Predictor
recordings. Those recordings persist in `news_model_recordings` as
content-addressed forensic evidence, and without `--live-program` the gate
replays each arm from them; a missing recording produces an `incomplete`
evaluation with no live fallback. The separate strict re-execution verification
pass (`--verify-recordings`) was deleted in #343.
`release shadow --live-program` cold-runs the candidate over the closed
validation window and seals the observations; an existing sealed shadow
observation manifest can be replayed instead.
`release canary arm|status|hold|resume|trip|close` owns the durable one-arm
rollout. A candidate may advance only when the prior
stage has a sealed PASS; a tool or optimizer may propose but cannot accept,
deploy or promote. Canary selector `news_canary_selector_v2` includes queue-high Events, excludes
recovery/listing/OI-telemetry/liquidation lanes, and validates selector, eligibility profile,
rolling profile and runtime-manifest identity at startup, resume and assignment;
drift trips the activation. `news replay <hits.json> [--gate-policy config|open|strict]` runs
Deduper+Gate over saved provider hits without broker or model and lists every
Event with admission, grounded assets, and preliminary storyline. `news why
<event_id>` prints the Event's chain (item, gate, triage, decide, delivery)
and a one-line `outcome`. `news dlq inspect|replay|purge [--limit]`
peeks, republishes, or purges `news.dead`. `replay` verifies the effective
policy and the topology first and exits non-zero without reading a message if
either is unknown or drifted; a dead letter it cannot decode is returned to the
queue and ends the batch with a non-zero exit naming the message, the decode
code and the number already replayed. `purge` is the only command that removes
evidence.

The `trading` family is read-mostly and has no provider-execution write command.
`trading status` reports Decision state/heartbeat, Capital control, both
bindings' durable redacted facts, target notional, durable admission counts,
stage latency, Case policy/capital reasons, and Intent counts. Unknown outcome
evidence remains unknown; the CLI does not infer readiness, flat, protection,
PnL, or fees.

`trading cases [--state] [--limit]` lists the Case ledger.
`trading show <case-id>` returns exactly `case`, optional immutable `intent`,
and optional current `outcome`. Operator control writes are
`trading blacklist add|remove`, which manages the canonical deny-list, and
`trading control running|close-only|paused`. `PAUSED` and `CLOSE_ONLY`
block new economic Intent; existing recovery obligations remain durable.
The retired `trading refresh-capabilities` command is rejected. `trading
replay-oi --days N` fetches public source-native BAR data,
publishes one content-addressed artifact, materializes timed blacklist expiry
through a short Workers transaction, and inserts one immutable replay receipt;
it has no execution credentials and performs no provider order write.

Trading consumes `news_trade_projection_v10`: exact current
`news_judgment_v2` OI rows plus the public instrument catalogue. Editorial News
and liquidation do not cross this capital seam. OI rows freeze `ingest_mode`,
so Item retention cannot erase live/recovery provenance; recovery rows are not
eligible triggers.

`trading_manifest_v9` freezes the learning epoch, OI Program v2 version and
SHA, policy v11, judgment contract/origin/SHA, runtime-manifest SHA, and the OI
verdict's own persistence stamp (#211),
the single primary trigger, point-in-time contexts, and strategy ID, version,
the exact typed configuration values and their digest (#213), and the public
venue catalog digest used to resolve its instrument. The serialized
manifest has one market fact at `contexts.market`; there is no serialized or
accessor alias named `market_context`. A pending Case reconstructs its strategy from that frozen
snapshot, so editing runtime thresholds affects only later Cases.
Cases frozen under any earlier manifest version remain readable audit rows but
cannot advance: an undecided case is terminalized as
`BLOCKED/not_run/source_generation_retired`; an already emitted Intent is not
rewritten and remains owned by Nautilus reconciliation.

The HTTP shape uses `trigger_kind`, never the retired `case_kind`. Every Case
and Intent projection carries `policy_id` and `policy_version` — the read model
is where the storage columns `strategy_id` / `strategy_version` meet the
product word, rather than a rename migration over 228 historical rows. The
shadow strategy ledgers and their status projections are gone with their
writers (#331, migration `20260829_0325`).
The typed liquidation ledger is not cascade-owned by `news_items`; raw Item
retention cannot delete the normalized replay fact.

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
