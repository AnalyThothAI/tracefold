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
and bootstrap/Serve/Workers/migrate password files. The operator directory is
mode `0700`; config and password files are `0600`. A normal rerun preserves
existing config and password contents while repairing permissions.
`tracefold init --force` replaces only `config.yaml`; it does not rotate
existing database passwords. The generated config has a new API bearer token
(`ws_token`) but no live provider/model/webhook credential, `news.push.enabled`
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
Tracefold never supplies an implicit endpoint or model. `qwen*` models are
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
Internal `ReaderCard.v2` outputs only `headline_zh`
and `why_zh`; no other model or provider produces copy. Raw persisted
`TriageVerdict.title_zh` is always `""` for the current Program. Feed and
summary read models may expose the non-empty derived display value returned by
`models.display_title`; that is not Reader output. Model execution policy,
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
(`enabled`, `feishu_webhook_url`,
optional `feishu_signing_secret`, `min_interval_seconds`), and
`news.watchlist[]` (`{symbol, market_type}`) are the only News knobs.
`news.triage.concurrency` (default 4) is the real consumer width of its queue.
Lexicons, prefix tables, LSH geometry, the code-owned Program registry, and
policy versions are image state. `tracefold config` exposes only redacted booleans, counts,
model names, and watchlist symbols; it never prints the token, broker URL,
keys, or webhook.

Push delivery is available only when `news.push.enabled` is true and the
webhook is a valid Feishu HTTPS custom-bot v2 URL; otherwise Serve and
Workers still start and deliveries settle `terminal/delivery_unavailable`.

`trading.*` is the whole Trading surface (#104) and it is `enabled: false` by
default; a disabled Trading context constructs no program, no adapter and no
runner. `trading.mode` names `paper | live_reviewed | live_bounded` and is
startup-owned — a prompt or a tool argument cannot change it, and paper never
reads the OpenTrade token. The strategy set and its numbers are code-owned, not
configuration: `oi_smart_money_momentum_v1` freezes its measurement window,
OI-change, smart-money-ratio, profit and price-band thresholds into every Case
it decides, so changing one starts a new config digest rather than re-deciding
an existing Case. `trading.candidates.*` bounds what may become a case
(`max_age_seconds`, `news_lookback_seconds`, `oi_lookback_seconds`,
`symbol_cooldown_seconds`, `max_rank_in_window`, `min_oi_value_usd`,
`max_dspy_cases_per_day`) — `max_age_seconds` gates the **trigger** and the two
lookbacks gate the **counterpart** it may attach, which are separate windows
with separate meanings (#211); `trading.regime.*` is the OI/price band
(`lookback_seconds`, `min_price_move_bps`, `max_price_move_bps` — a band with no
ceiling is rejected at startup); `trading.policy.*` gates the pure mapping
(`allow_short` defaults to false, `live_min_surprise`, `live_max_price_in`,
`min_whale_long_profit_bps`); `trading.venues.*` is the operator's permission list over
`binance` and `hyperliquid` — it is a priority order only for a case with no OI
frame, because an OI-bearing case routes at the venue its own frame named and
refuses rather than substituting another (#211); `trading.order.*` is every order parameter
(`fixed_notional_usd`, `leverage` fixed at 1, `fixed_stop_bps`,
`take_profit_bps`, `max_holding_seconds`, `max_spread_bps`,
`max_open_underlyings`, `max_orders_per_day`), so
the nominal planned-stop daily envelope is the multiplication
`fixed_notional x fixed_stop_bps x max_orders_per_day`. The four numbers that
decide how an order *ends* are frozen onto its ledger row when the intent is
written — the absolute stop and take-profit prices, `max_holding_ms`, and the
taker fee `realized_bps` is charged at — so editing `fixed_stop_bps`,
`take_profit_bps` or `max_holding_seconds` changes the next order and never one
already written, in either mode (#209). The taker fee is code-owned, not a
configuration key: `trading.order.*` forbids extras and adding a
`taker_fee_bps` key is a startup rejection. The remaining keys are not exit
policy and do still gate an order after it is written —
`max_orders_per_day` and `max_open_underlyings` are re-counted where they are
spent, and `max_spread_bps` is re-checked at the live pre-submission preflight;
`trading.opentrade.*` is the provider contract (`base_url`, `token_file`,
`request_timeout_seconds`); `base_url` must be credential-free HTTPS. A live
mode without a configured OpenTrade contract
or an enabled venue fails at startup, not at the first order. Issue #185 PR-C2
supports only the narrow `live_reviewed` lifecycle: exactly one enabled venue,
one uppercase base symbol in `trading.live_symbol`,
`fixed_notional_usd <= 10`, `max_open_underlyings = 1`,
`max_orders_per_day = 1`, `take_profit_bps = 0`, and leverage 1. The token file is resolved relative
to the operator config directory (or as an absolute path), must be a non-empty
regular non-symlink file of at most 16 KiB with no group/other permission bits
(normally mode `0600`), and is read only by App composition. `live_bounded` fails
configuration and composition closed. A reviewed approval binds the exact
payload digest and is accepted only during the 60 seconds from order creation.
An approval accepted near the end of that window gets one 30-second reconcile
cadence to reach submission. Submission repeats preflight and terminally rejects
before any write if the account, external inventory,
execution-contract fingerprint, hedged mode, 1x leverage, margin mode, spread,
balance or 25 bps mark-drift bound no longer holds. The only provider writes are
one allowlisted market entry and bounded full-position closes using the latest
authoritative quantity; ACK never means fill or closed.
The approval transition stores a C2 marker with its timestamp; a pre-C2
`APPROVED` row without that marker fails closed because its approval time cannot
be proved.
`llm.trading_decision_model` selects the single `dspy.Predict` endpoint; without
it a News-bearing case settles as `no_trade / program_unconfigured` and an
open-interest case still decides, because that lane calls no model at all.

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
| Trading | `/api/trading/status`, `/api/trading/orders`, `/api/trading/events/{event_id}` | the capital lane's mandate and readiness, its source-admission ledger, its orders and the cases that authored none, and whether one News Event became a case — with the named reason when it did not. Reads only — there is no HTTP write anywhere on this surface |

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

- `GET /api/news/feed?family={family}&admission={admission}&decision={push|escalate|drop|throttled|degraded}&symbol={symbol}&q={query}&limit={limit}&cursor={cursor}&outcome={pushed|held|pending}&hours={0..168}&oi={pushed|withheld|parse_failed}&direction={bullish,bearish,neutral}&channel={news,oi}`
  returns Events newest first with the leader title,
  the derived display `title_zh` (`models.display_title`, normally the current
  verdict's `headline_zh`), admission,
  asset class, grounded assets, watchlist hits, storyline key, context line,
  **one `outcome`** (`kind` from the stable enum `held_recovery`, `held_gate`,
  `queued_publish`, `queued_triage`, `dropped`, `throttled`,
  `degraded_dropped`, `pending_delivery`, `delivered`, `delivery_failed`;
  reader copy `text_zh` and `reason_zh`; `group` = `pushed|held|pending`),
  the latest Triage summary (final decision, override rule, throttle reason,
  degraded flag, error code, direction, magnitude, event type, scope,
  `headline_zh`, derived display `title_zh`, `why_zh`, plus Chinese
  `direction_zh`,
  `magnitude_zh`, `event_type_zh`), and the first delivery state with its
  error code. `outcome` is the feed's task-tab filter (its SQL mirrors the
  outcome groups); `hours` bounds `opened_at_ms` to the last N hours (`0`
  or absent = no bound).

  `direction` and `channel` accept comma-separated, duplicate-free closed sets. `direction` filters the latest
  stored Triage verdict; `channel=oi` means `admission=telemetry_deterministic`, while `channel=news` means
  every other admission. Selecting both channels narrows nothing. The axes compose with every existing
  predicate before the count aggregate and cursor pagination.

  `q` matches the Event search document and leader title, the leader item's `reporting_origin`, and any attached
  asset's raw symbol, canonical `base_symbol`, instrument `venue`, or `venue_symbol`. Search is applied by the
  authoritative feed query before counts and cursor pagination; the browser does not maintain a second index.

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

  Unknown query parameters, invalid admission,
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
  admitted on) and beside it `assets[]` — the same tags resolved against the
  #75 instrument universe, one entry per *instrument named* (the provider ships
  both `CL` and `XYZ-CL` for one contract, and those resolve to a single
  entry), each `{symbol, base_symbol, venue, listed}`. Entries are keyed by the raw provider tag and `symbol` comes
  back normalized, so `UNITREE` and `XYZ-UNITREE` resolve to the same listed
  contract. `venue` is the preferred venue when a base trades on
  several (deepest first, HIP-3 builder DEXs last) so a chip is stable across
  polls, and is `null` with `listed: false` when the tag names nothing on any
  venue — which is how a reader tells `SPOT` on a Spot Gold headline from a
  real listing. Resolution is the same two steps the Gate takes (alias, then
  existence) and costs one batched query per response, never one per Event.
- `GET /api/news/events/{event_id}` returns one Event, its `outcome`, a
  `timeline` (ordered steps `received` → `gate` → `triage` → `decide` →
  `delivery`, each with `title_zh`, `at_ms`, `summary_zh`, and the raw
  `facts` it was built from), its member Items (title, URL, origin,
  publication time, match kind, Jaccard estimate, provenance, description),
  every Triage verdict (model decision, rule baseline, final decision,
  override rule, throttle reason, verdict payload, runtime model, Program
  version/SHA, degraded flag and trace; nullable `prompt_version` is Prompt-era
  audit history only), deliveries, and `normalization[]` — the alias
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
  `docs/OPERATIONS.md`), `funnel_24h` (`received`, `parsed`, `admitted`, `candidates`, `triaged`,
  `tagged`, `grounded`, `decided_push`, `delivered`, plus
  `received_1h`/`delivered_1h`),
  whose five Event-feed stages (`received`/`parsed`/`admitted`/`triaged`/`delivered`) all start from Events
  opened in the same rolling 24 h cohort and test those Events' durable stage facts,
  `reasons_24h` (`stage` `gate|drop|throttle|push|degraded|ungrounded`, raw
  `key`, `label_zh`, `count`, sorted by count), and four layers: `ingest` (WSS
  connected, last frame/publish, error, open incidents, token configured; no
  Strategy IDs/counts), `broker`
  (configured, connected, per-queue message/consumer counts when observed,
  error code), `pipeline` (events and candidates per hour/day, Triage counts,
  degraded counts incl. `triage_degraded_by_code_24h`, decided pushes,
  throttled, OI telemetry received/parsed/parse-failed/pushed counts, Triage
  p50/p95, queue lag p95, the Triage model name, the cohort fields
  `funnel_received_24h`/`funnel_parsed_24h`/`funnel_admitted_24h`/`funnel_triaged_24h`/
  `funnel_delivered_24h`, and the
  named 24 h maps `suppressed_by_reason`, `dropped_by_rule`,
  `throttled_by_key`, `pushed_by_rule`, `duplicates_withheld_24h`
  (`all` is the current content-only path; historical rows may retain the old
  `throttled` scope), plus `tagged_24h`,
  `grounded_24h` and
  the top-ten `ungrounded_by_symbol_24h`), `oi` (#207: the deterministic
  open-interest lane — `policy`, the `news.oi` thresholds as they are running;
  `by_rule_24h`, 24 h counts keyed on the judge's own gate names, which
  `pipeline.dropped_by_rule` cannot carry because `decide()` writes
  `override_rule = telemetry_deterministic` for every OI verdict whether it
  pushed or withheld; `window_occupancy`, per-symbol spent rank slots inside
  the live window measured with the judge's eligibility predicate, ending at
  `measured_at_ms` — the window's start is deliberately not a field because it
  would move on every read and churn the ETag; and `trade_floors`, the capital
  lane's own configured floors plus its `enabled`/`mode`, shown beside the News
  gates and never merged with them, read from platform configuration so this
  endpoint imports nothing from `tracefold.trading`),
  and `delivery` (sent/terminal
  counts, last error, end-to-end p50/p95, availability), plus
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
provenance, not fact identity. Event identity is the leader Item id; Events
merge Items by exact comparison fingerprint or MinHash/LSH near-duplicate
(estimated Jaccard >= 0.55 with strong-fact compatibility) inside the family
window (market telemetry 2 h, disaster 6 h, filing 72 h, general 12 h).
Fingerprints of at most two tokens never share an Event.

Verdict identity is `(event_id, stage, policy_version)`. `TriageVerdict` keeps
the reader/compatibility fields `novelty`, `restates`, `event_type`, `assets`,
`direction`, `scope`, `magnitude 0..3`, deterministically derived `actionable`,
`confidence`, assembler-projected `decision`, `audience`, `headline_zh`, the
empty `title_zh` sentinel, and `why_zh`. Model delivery intent has exactly one
owner: the sibling editorial envelope's `TradeRelevanceV1.reader_value`.

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

Empty channels/markets are valid only for contextual/none tradability with a
background/none reader value. The normalizer records the raw arrays, then
de-duplicates and orders them before exact gold, hashing and replay.
The assembler maps `reader_value` exactly: `escalate -> decision=escalate`,
`realtime -> decision=push`, and `background|none -> decision=drop`.
It derives `actionable=true` only for direct/second-order tradability with
non-empty channels and affected markets; neither compatibility field is a
second model opinion.

`SemanticJudgment` atomically carries verdict, an `EditorialEnvelope`, trace,
usage and runtime identities. The envelope is
`{editorial_contract_version=news_editorial_v1, editorial_origin,
relevance, editorial_sha256}`: model origin requires relevance;
`telemetry_deterministic` and `degraded_unavailable` require null relevance.
An admitted listing still runs the normal Program and uses `model` origin;
listing admission is an objective policy fact, not synthetic relevance.
`ScoredJudgment` is the only projection accepted by policy, baseline, compiler,
CandidateEvaluator and recording/replay. `news_verdicts` stores the verdict,
editorial envelope, `scored_judgment_sha256`, exact `runtime_manifest_sha`,
`model_decision`, `rule_baseline_decision`, `final_decision`, `override_rule`,
`throttled_by`, `degraded`, `error_code`, and trace in one transaction. Old
NULL-editorial rows remain audit-only.

The trace binds `verdict_sha256`, `editorial_sha256`, Program version/SHA,
runtime manifest/provider/model identity, every frozen `DecidePolicy` value,
input/told/seen/storyline hashes and snapshots, every initial/re-ask execution,
and per-Predictor request/input/upstream/output,
finish reason, latency, token and cost identity. The rendered instruction is
derived from `factory_id` plus the artifact's advisory, so the Program SHA
already commits to it and the call trace no longer repeats a signature,
instruction or demo digest. A told-only re-ask may restore
the complete `first_judgment`; evidence-changing re-asks may not reuse it.
`triage` is the only current stage. Current versions are
`news_title_norm_v2`, `news_gate_v5`, `news_storyline_v3`,
`news_semantic_program_v5` (or `news_oi_signal_v1` for deterministic OI),
`news_triage_policy_v10`, `news_delivery_card_v10`, artifact schema
`news_program_strategy_artifact_v1`, factory
`tracefold.news.program.factory_v6`, and epoch `program_v7`.
The exact Program identity is its content SHA, not the display version alone.

Strategy 2000 is a separate deterministic contract composed after Gate v5; it
does not silently widen that policy or editorial policy v10. Its release
identities are `news_liquidation_admission_v1`,
`news_liquidation_fact_v1`, `news_liquidation_policy_v1`,
`liquidation_parser_v1`, and `opennews_liquidation_source_v1`. The source
contract records provider-record and unresolved contract identity, position
side, quantity/notional/price semantics, completeness and throttle assumptions.
Its current `complete=false` is a material fact.

`ProgramStrategyArtifactV1` is the only executable semantic configuration, and
it is one canonical JSON document — `schema_version`, `factory_id`, the
`event_semantics_instruction` and `reader_card_instruction` advisories, and the
`program_sha256` over exactly those four values — carried in the application
image as `<program_sha256>.json` and selected by the code-owned registry. The
stable root is
`e54c8d69b9606b7306e0e829a09994dd525743b5c12ec9e549a7f67ef6a2ea06`.
That SHA is behavior identity only: it holds no parent lineage, optimization
cost, trajectory or teacher endpoint, so two runs that reach the same two
instructions produce the same Program. Lineage belongs to the candidate's
`ProposalReceipt`, and since #202 it is *derived* at registration by re-applying
the patch to the running stable rather than declared by the candidate.
The graph, schemas, ordered code-owned RulePacks, renderer, normalizer,
assembler, model route and execution budget are code, versioned by `factory_id`;
a semantic change to any of them is an explicit factory bump, not a cascade of
component hashes. Rendered instructions are derived bytes, never a
second editable truth, and they contain no identity hash and no demo section.
Loading fails closed on an unknown hash/version/factory, non-canonical or
duplicate-keyed JSON, a non-finite number, a path or symlink violation, a file
name that is not its own root, or unsafe or secret-bearing state.
The optimizer can emit only a typed patch carrying the two advisory
instructions; there is no DemoBank to write to, and a demo on a Predictor is
refused. The trusted side reconstructs the final Artifact from the exact
active stable root. Pickle, cloudpickle,
DSPy Flex state, dynamic Python/classes,
endpoints and credentials are not artifact formats. DSPy cache and hidden
provider retries are disabled; every provider attempt must appear in the trace.
There is no legacy Prompt runtime, dual stack, compatibility Adapter or
production operator-selected artifact path. Nullable Prompt-era fields remain
audit-only.
The current execution contract is `EventSemantics.v2 -> deterministic
SemanticNormalizer -> ReaderCard.v2 -> deterministic assembler`: normally two
serial calls because the normalizer and assembler make no provider request;
one fast retry is shared by the whole route, so at most three calls per route;
fallback restarts the full
graph, so primary plus fallback is at most six. The code-owned 20-second
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
Strategy 2000 never enters that order: after generic Gate v5 it is composed as
`liquidation_deterministic`, then its own v1 policy emits the parser's
direction-neutral push/drop. Parse failure is a deterministic drop.

Delivery identity is `(event_id, kind)`; `first` is the only kind written —
one Event gets one card — and the retired lane's `followup` rows survive as
history. States are `sending`, `sent`, `terminal`. There is exactly one HTTP
attempt; a delivery without a configured sender settles `terminal` immediately
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
crash dead-letters. There is no operator control plane: pause and mute were
removed with `news_control_state`, which had never withheld a card, so the only
things that can withhold one are `decide()` and duplicate evidence.

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
  (`rolling_24h|provider_day`), provider and receipt timestamps, `age_ms`, and
  one `state`: `fresh` (age <= 60 s, three collector turns), `stale`, `unavailable` (nothing quoted
  yet) or `unlisted` (no venue we poll lists it). A price is a positive decimal
  string or `null`; it is never `0`, and a failed venue leaves the previous row
  in place rather than blanking it. `change_pct` is `null` until the venue's day
  reference is known — always recomputed from the same response's price, never
  carried over from another turn (#109) — and `source_at_ms` is `null` for venues
  that publish no timestamp of their own (Hyperliquid always; `binance.spot`
  between day reads). Current quotes are deliberately **not** feed
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
- `tracefold trading replay-oi --days N` is a read-only report, not an endpoint:
  every parsed OI fact in the window driven through the production source stage,
  Candidate Gate and strategy, counted by stage and by rule, with the target
  template's cohort listed separately. It proposes no threshold and evaluates
  neither the price band nor any outcome, because it fetches no market data.
- `GET /api/trading/status` returns the capital lane's `budget` (fixed
  notional, fixed stop bps, max hold, `nominal_daily_stop_loss_usd`, the daily
  order ceiling and today's count), `readiness` (`enabled`, `mode`, `control`,
  `execution_backend`, `execution_configured`, `live_mode_supported`,
  `live_ready`, `live_readiness`, `venues`), `floors` (the capital lane's own
  thresholds, never the News gates) and `counts` (rolling 24 h groupings plus
  `cases_today_by_state`, exact `policy_allowed_today`, `closed_orders_today`,
  and the unbounded active-order count for the UTC `funnel_day_key`). `counts`
  also carries the durable admission ledger (#264): `candidate_counts_24h` /
  `candidate_counts_7d` by `DEFERRED | REJECTED | CASE_CREATED | EXPIRED`,
  `candidate_reasons_24h` / `candidate_reasons_7d` keyed `stage:reason` from a
  closed vocabulary, and six `latest_*_at_ms` milestones from source through
  admission, case, order, open and close. Those counts are keyed on when the
  *frame* was observed rather than on when the gate looked, so a runner that
  restarts and re-reads a backlog cannot move yesterday's frames into today, and
  unlike `funnel_today` they survive the UTC day roll. Same facts
  as CLI `trading status`, from the same reads.
  `live_ready` is never `true` from a read: a serve process cannot observe the
  Workers process's startup and canary result, so it reports `not_proven` rather
  than guessing (#185 P1-2). Money is an exact decimal string end to end.
- `GET /api/trading/orders?underlying={base|crypto:BASE}&state={active|closed|all}&day={YYYY-MM-DD}`
  returns `orders[]` — one economic intent each, with the case that authored it —
  and `cases_without_orders[]`, the cases that stopped before authoring one and
  the rule they stopped on. Both halves, because a `POLICY_REJECTED` case is
  where the capital floors bite and has no order to join through. An explicit
  `state` filter suppresses the second list: it is a question about orders.
  `underlying` accepts either spelling and the response carries both. When `day`
  is present, the order batch contains every active order plus closes
  whose authoritative `position_closed_at_ms` falls within that UTC day; this is
  how the workbench binds its daily ledger without reconstructing the interval
  from rows in the browser. `day` does not change the rolling
  `cases_without_orders` evidence list. **Neither
  `trading_orders.payload` nor `trading_cases.manifest` is ever returned**, and
  neither is `account_ref` or `remote_order_id`; the frozen provider request body
  and the frozen decision input do not reach a browser. `state` is the ledger's
  own string, returned verbatim: `ACKNOWLEDGED` is the venue answering, `OPEN` is
  the only state that has proven both a position and a native stop covering it,
  and a caller that collapses them asserts something the ledger does not.
- `GET /api/trading/events/{event_id}?lane=oi` answers whether one News Event
  became a case. `joinable` is the honest half: only the deterministic OI lane's
  source key (`oi:{event_id}:{metric_version}`) is reconstructible from an Event
  id, the model lane's is a content hash of an artifact and a fingerprint (#154),
  and for anything but `lane=oi` the answer is `joinable: false` — "this cannot
  be asked", which is a different fact from `case: null`, "it was asked and the
  answer is no". Joining by symbol and time instead would record a link the
  ledger does not have.
  Whether or not there is a case, the response carries the admission decision:
  `gate_status`, `gate_stage`, `gate_reason`, `gate_retryable`, `gate_version`,
  `gate_config_digest`, `gate_evidence` (the measurements the decision was taken
  on, plus the threshold it failed against), and the three evaluation counters.
  A `gate_status` of `null` means the lane has not evaluated this source under any
  gate version — an absence, not a refusal, and a different fact from a rejection.
- `/api/news/feed` and `/api/news/events/{event_id}` additionally carry the
  Event Reaction: the feed the compact event-level aggregate (median signed
  return of the Triage primaries that price, with `state`
  `pending|partial|complete|unavailable`), the detail every per-asset row with
  its pinned venue, raw closes, close timestamps, returns, metric version and
  unavailable reason. A current quote and an Event Reaction are different
  response types with different words; no field named simply `change` carries
  either meaning.
- `/api/news/status.price` reports per-source quote freshness (source key,
  target and quote counts, age, state) and the Reaction backlog
  (partial/complete/unavailable over 7 days) beside the pipeline's own health.

## CLI

`uv run tracefold --help` is the exact CLI source of truth. Stable top-level families are:

- service/config: `serve`, `workers`, `init`, `config`;
- database: `db migrate|health|audit|query-audit`;
- News: `news bus-check|control|instruments|review|learning|replay|why|dlq`;
- Trading: `trading status|cases|show|blacklist|approve|reject|resolve|control`;
- maintenance: `ops validate-projections`.

There is no `recent` or `search` command and no market rebuild/sync/reconcile
maintenance command. Mutating maintenance commands require an explicit
execution flag where the parser offers a dry-run mode. They operate from
persisted facts and stable target keys. A rebuild does not create an alternate
generation/run identity or make a provider response the source of truth.
`trading resolve <order-id> open` accepts `--remote-order-id`; it is required
when the manual row has no durable provider entry identity and is persisted
before automated live reconciliation resumes. It can fill a missing identity,
but cannot replace one; a supplied mismatch leaves the row in manual review.

`validate-projections` is a strict Serve-role read. It does not acquire the
maintenance lock, so operators can inspect the running singleton without
interrupting it.

`db audit` reports the migration revision, row `counts` for every table in the
code-owned `NEWS_TABLES` contract, `news_schema` exactness over that same set,
and the runtime-role contract including a role-authentic Workers evidence
append without rewrite access (current at migration `20260824_0302`). Since
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

`news learning baseline (--dataset SHA | --from-ms N --to-ms N [--all-cohorts])
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
compares two different corpora, and the receipt says `cohort_scope: current` or
`all` — discovery, never release evidence. `--dataset SHA` is the exact frozen
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
to measure what the optimizer measures — `DspyCompileProgram` on one task
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
| `compile_live` | `DspyCompileProgram` on one task endpoint | what baseline does GEPA optimize against? |
| `runtime_live` | the configured four-slot `DspyNewsSemanticProgram` | does the production Program route answer these cases? |

`compile_live` is exactly the graph GEPA maximizes and deliberately has no
fallback route, no fast retry, no per-route deadline and no circuit breaker, so
its failure rate is not the reader's. `runtime_live` is built by the same seam
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

`--all-cohorts` stays available to every mode, and `identity.cohort_scope` names
which population was read (`current` = the release-plane cohort, `all` = every
accepted review in the window). What #150 forbids is replaying today's policy
over a *stored retired verdict*, which is `--mode recorded --action-source
policy`; the handler rejects that pairing. A live mode has no stored verdict —
it generates one with today's Program and scores it under today's policy, and
only the evidence and the reviewer's labels are historical. Banning the
combination outright would make both live modes unrunnable rather than safer:
every accepted review this project holds belongs to a cohort that has since been
retired.

`--action-source` has exactly one valid value per mode and the handler rejects
the other: `recorded` outside `--mode recorded` short-circuits the policy replay,
so a live mode would generate a fresh verdict and score it against the action a
*different* verdict shipped, silently emptying the metric's heaviest component.
`--max-model-cases N` is required by both live modes and caps the corpus read —
`runtime_live` spends two to six sequential provider calls per case on the
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
`known_duplicate_leak`, `advisory_rejected`). A gated case keeps its resolved action and its per-dimension
outcomes: the zero enters every denominator rather than leaving it, or a
candidate with more hard failures could publish a higher per-dimension hit rate.
Metric `tracefold.news.production_action_trade_relevance_v4` weights 45% exact
final production action, 35% exact TradeRelevance dimensions, 10% existing
semantics/novelty and 10% ReaderCard. Reports expose each component's effective
denominator, effective weight mass, gold coverage and field count. The score is
identical with or without DSPy's `pred_name`; that argument filters feedback
only. EventSemantics receives relevance, semantics, novelty and its owned action
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
costs six calls, and counting zero made the receipt least accurate exactly where
the route was worst.
`report_sha256` covers the measurement with wall-clock latency excluded, so two
runs with identical predictions publish the same address; `latency_sha256`
addresses the timings separately. `identity.case_root_sha256` answers "the same
cases?" and `identity.corpus_sha256` answers "the same inputs?" — hashing ids
alone let one address describe two corpora, because any evidence edit that kept
the ids left the receipt untouched.
`compile_live` reports wall clock only, because in that mode `dspy.Evaluate`
owns the program call and per-case provider timing is not observable. Neither
receipt contains a credential or an endpoint URL.

Policy is frozen into each scored example rather than read from process-global
state: `policy_metric` carries the exact `policy_values` and `policy_sha256` of
the arm, the shared pure/version-bound `production_decision()` builds
`DecidePolicy(**policy_values)`, validates one `ScoredJudgment`, and returns the
complete `DecisionResult` (action, rule and throttle key). A missing or mismatched policy fails closed instead of
falling back to `DEFAULT_POLICY`. A report spanning two policies is refused
rather than labelled with one of them. `recorded` returns before policy replay,
so a retired cohort's shipped action stays reproducible after the policy it ran
under was replaced, and its `identity.policy_sha256` is an explicit `null` —
naming a policy the number does not depend on would be the same ambient-state
confusion in a different place. `identity.policy_source` says where the replayed
values came from: `active_arm_manifest` is the configured arm, which is the arm
that ran only for current-cohort episodes, so `--all-cohorts --action-source
policy` applies today's rules to a retired corpus by design. An episode with no
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

`--mode recorded` makes no provider call; `--all-cohorts` drops release-plane
eligibility — for the seed sent ledger too, not only the cases — so a retired
arm's corpus is measured against the ledger `decide()` would actually have read.
`--semantic-judge MODEL` scores free-text retention anchors by meaning instead of
byte equality (#148) through the same `CardEquivalenceJudge` contract that the
the optimization wires through its separate `metric_judge` role. Judge failure is explicit unavailable, enters the affected
free-text dimension as zero, and is counted/costed with no byte-equality fallback,
hidden retry or cache. Magnitude, direction, assets, novelty and every
TradeRelevance field stay exact; the strict byte-equality mean is reported
alongside as `scores.case_macro_answered_byte_equality`.
Reviews whose `evidence_version` has been superseded are not replayable and are
excluded, the same rule `_load_case` already enforced.

The recorded calibration is pinned to a checked-in corpus
(`tests/fixtures/news_baseline_calibration_v2.json` for metric v4), not to the live
database, so it proves metric wiring rather than tracking corpus growth. The v1
fixture remains frozen metric-v3 audit evidence. The
expected values are held only by `tests/news/test_news_baseline_calibration.py`;
no document restates them, because four copies of one number is how a receipt
starts disagreeing with itself. A live run over the same window will differ, by
design — the database keeps accepting reviews and superseding evidence. Every
string outside an explicit structural allowlist is redacted by an
equality-preserving map, which keeps every comparison the recorded metric makes
and is why the fixture is valid for `--mode recorded` only. The allowlist is the
design: a key nobody thought of is redacted rather than published.

Reviews are accepted under `news_review_v4`. Its exact optional `expected` gold
includes magnitude, direction, assets and the seven TradeRelevance fields
(`trade_impact_breadth`, `trade_tradability`, `trade_surprise`,
`trade_development_delta`, `trade_channels`, `trade_affected_markets`, and
`reader_value`). Accepted `novelty` and `should_push` are already their own
typed truth rather than duplicate `expected` fields. Every failed scored
dimension must have expected gold; otherwise it is not scored, with no
any-change fallback. Channels/markets canonicalize before exact comparison. Historical
v2/v3 rows remain readable audit history but cannot enter v7 metric/GEPA/release
evidence. Listing/telemetry do not enter relevance gold; grounded-watchlist
cases are separated as policy evidence. `gold_coverage` reports how much of each
component is actually scored.

`news learning snapshot|compare` (#193, flattened out of an `experiment` group
by #202) is the operator's research window, and it is not part of the release
plane. It reads the database once as `serve`, writes only into a run directory
the operator names, and can propose nothing:

| Command | Reads | Writes | Spends |
| --- | --- | --- | --- |
| `snapshot --hours N --limit N --out DIR` | one closed window through `CandidateEvaluator.baseline_episodes` | `DIR/manifest.json`, `DIR/cases/<case>.json` | nothing |
| `compare --run DIR --student MODEL [--teacher MODEL] --max-model-cases N [--resume]` | the frozen cases | `DIR/compare/<case>.json`, `DIR/report.json` | one live arm per named model |

The window ends at a ten-minute settlement grace rather than at `now`: the
outcome loop keeps writing prices for minutes after an Event opens, so a
snapshot taken to the current instant measures a corpus that changes underneath
the comparison. `run_sha256` is issued over the window, the parent Program, the
policy, the case counts and a root over the frozen case ids, so a run cannot
silently change what it measured. The manifest is
`tracefold.news.experiment_run_manifest.v2` and names the episode projection its
cases were frozen under; a run frozen under an older one is refused by name
(`news_experiment_run_projection_schema_stale`) rather than silently answering a
question its cases cannot support — a snapshot taken before the Objective Plan
existed carries no explicit owner, so every failure case in it would classify as
`owner_absent`. `case_sha256` is the evaluator's own case id,
which is what makes `--resume` a directory listing rather than a stored cursor.

`compare` scores three arms that are never averaged together — `recorded` is
what production shipped, `student` is the local route that will ship, `teacher`
is a larger reference — and each named model runs on the credentials of the
route that serves that role. A case with no accepted review is listed in
`unlabelled_case_ids` and scored by nobody; `failure_clusters` ranks by cluster
size times mean regression, so a broad small regression outranks one bad case
and an improvement never enters the queue.

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
`optimization/optimization_report.json`, `optimization/prompt_candidate.json` on
`ADVANCE`, and `run_summary.json`
(`tracefold.news.gepa_run_summary.v2`). Freezing with `--out
DIR/development.json` makes the same directory loadable by
`docs/research/news-gepa-frozen-run-evaluation.ipynb`.

`run_summary.json` is a projection over those artifacts and never a fourth
authority: it reads published fields, computes no score, re-derives no plan and
carries no news text, Prompt, case list or endpoint. It names three baselines so
none can be quoted as another —
`standalone_selection_score`
(`subsets.development_selection.case_macro_failure_as_zero`),
`gepa_seed_selection_score` (`trajectory.val_aggregate_scores[0]`, the seed
Program's score inside the run that proposed against it) and
`future_test_baseline`, which is always `null` here because only `release
evaluate --stage holdout` against a post-registration ValidationDataset can
produce one. `numeric_drift` is `seed - standalone`, published rather than
reconciled: two physical runs of one graph may differ, and a difference is not
by itself evidence that a dataset identity is wrong.

`dataset` carries the corpus counts and roots plus a `coverage` block forwarded
from readiness, so the numbers and the population behind them read together. The
block always has the same ten keys: a `gepa_readiness_report.v1` in an archived
run directory carried none of these counts, so every value is `null` — never `0`,
which would read as a measured corpus of nothing, and never an empty object,
which a consumer would fall off the end of.

`same_population` is a verdict over named `population_checks` — dataset SHA,
episode projection root, episode count, representative case root and counts,
both split roots, the metric receipt, the parent Program, the task model, and
the task endpoint. Every corpus check is three-way against readiness, so a run
explained as one corpus and measured as another is a `mismatch` even when the
two measured legs agree. The metric receipt is compared whole except for
`semantic_judge.execution.max_model_calls`, a spend bound that cannot change
what "better" means; both observed ceilings are printed in the row. A judge that
went `unavailable` is reported separately under `judge_availability`, because it
makes its leg a lower bound rather than a different population. The endpoint check compares each report against the
digest of the route this run composed rather than the two reports against each
other, because the baseline fingerprints it as `configured_endpoint_model_v1`
and the optimizer as `model_execution_identity.v1`. Any `mismatch` makes
`same_population` false and exits `2` with
`news_learning_run_population_identity_mismatch`; the summary is still written,
because a refused comparison is not a reason to withhold its evidence. A leg
that never ran is `not_comparable` and `same_population` is `null`, never
`true`. `next_action` is `future_test` on `ADVANCE`, `keep_stable` on `NO_OP`,
and on `REJECTED` it is `collect_more_gold` only when the terminal reasons say
the corpus cannot answer — an exhausted budget keeps Stable and says so in
`reasons`. A `false` `same_population` forces `keep_stable` whatever the
terminal: the file must not recommend a future test that rests on a `before`
number the same file just declined to vouch for. Exit `0` is `ADVANCE`; `1` is `NO_OP` or `REJECTED`, both complete
experiments.

`news learning draft-reviews --model MODEL --out FILE [--hours N] [--limit N]
[--include-reviewed] [--events-from DIR]` proposes `news_review_v4` rubrics for
a human to accept and writes a file, never a review. `--events-from` closes the
fast loop: it drafts exactly the unlabelled Events an experiment run froze, in
the run's own fixed order, one ReviewDesk query per Event, and reports
`requested_events` beside `tasks` so an Event whose desk task has been
superseded is visible rather than read as judged. Without it the command keeps
its queue-by-hours form, which is how a first corpus is grown before any run
exists. A batch refuses duplicate task identities before its first model call
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
the deployment-time `program_v7` epoch and accepts only `news_review_v4`;
every earlier Prompt/Program/review cohort is audit-only and cannot enter a
dataset or metric-v4 denominator.
The CLI is two groups, because there are two lifecycles (#202 §11 PR-E). `news
learning` freezes a corpus, explains what GEPA may optimize, scores the stable
Program and runs the one optimization — `readiness`, `baseline`, `run`,
`draft-reviews`, `snapshot`, `compare`, `optimize`, `freeze` — and none of them
can ship anything. `run` is the recommended composition of the first three;
the rest stay callable one at a time. `news release` admits a candidate and moves it: `register`,
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
recordings. Its mutually exclusive `--verify-recordings` mode is limited to
offline/holdout Program candidates: it loads the exact existing run corpus,
re-executes both real arm-scoped Program graphs with no live provider fallback,
and seals the matching corpus/observation roots into the evaluation report. A
missing corpus or recording produces an `incomplete`/`UNKNOWN` evaluation with
no live fallback; an identity or tamper mismatch fails closed.
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
peeks, republishes, or purges `news.dead`.

The `trading` family is read-mostly, and deliberately has no command that
places, amends or cancels an order. `trading status` reports mode, control
state, the day's counters and funnel, `stage_latency_ms` (p50/p95 and an
evidence count `n` for each pipeline stage from `source_observed` to
`position_opened`, keyed by stage and by nothing else),
capital cases grouped by `trigger_kind` and `strategy_id`, and liquidation
shadow evaluations grouped by strategy and rule. Each shadow cohort reports
`evaluated`, `completed`, holdout and coverage counts, source latency, nullable
duplicate rate, 5s/30s/1m/5m/15m/1h outcomes, deterministic bootstrap
intervals, MFE/MAE, stop/TP/max-holding results, fees/slippage/funding
availability and missing-data counts. Cohorts stay separate by strategy, venue,
and liquidity bucket. The current public bar source is 5-minute close-only, so
5s/30s/1m and funding are explicitly missing rather than synthesized. The
first close at or after the trigger is the forward-return origin; coverage
requires all supported horizons plus a measured terminal exit. Event-study v3
owns fixed research stop/TP/holding/fee/slippage constants, independent of
operator Order edits.
`liquidation_promotion_ready` is false with a named evidence reason; it is not a
configuration switch.
`nominal_daily_stop_loss_usd`, the configured `live_symbol`,
`execution_backend`, `execution_configured`, `live_mode_supported`,
`live_ready`, and `live_readiness`; `live_reviewed` reports
`opentrade_reviewed` support but still `live_ready=false/not_proven`, because a
separate CLI process cannot prove the Workers capability receipt. A
`live_bounded` configuration is rejected before the CLI can report status.
`trading cases [--state]
[--limit]` and `trading show <case-id>` read
the case, its order and its deduplicated remote observations. The three writes
are narrow: `trading blacklist list|add|remove` owns the canonical deny-list
(one row per underlying — `CL` blocks `CL` and `XYZ-CL`; a read failure blocks
every symbol), `trading control running|close-only|paused` sets the runtime
control (`CLOSE_ONLY` and `PAUSED` still permit reconciliation and the
deterministic safety close), and `trading approve|reject <order-id> --digest`
settles one order bound to its exact frozen payload digest, idempotent by state
so a second approval of an already-approved order changes nothing.

Trading consumes `news_trade_projection_v5`: separate editorial News,
deterministic OI, and typed liquidation rows. The liquidation row preserves
both `liquidated_position_side` and `forced_order_side`; callers must not infer
one by treating the other as a forecast. It also carries every source-contract
semantic named above and freezes `ingest_mode` in the normalized ledger, so
Item retention cannot erase live/recovery provenance. Recovery rows are audit
context and are not eligible triggers.

Trading's editorial News projection contract is `program_v7` / policy v10
only. `trading_manifest_v4` freezes the learning epoch, lane-specific Program
version and SHA, policy version, editorial origin and SHA, scored-judgment SHA,
and runtime-manifest SHA, plus the OI verdict's own persistence stamp (#211),
the single primary trigger, point-in-time contexts, and strategy ID, version,
the exact typed configuration values and their digest (#213). The serialized
manifest has one market fact at `contexts.market`; there is no serialized or
accessor alias named `market_context`. A pending Case reconstructs its strategy from that frozen
snapshot, so editing runtime thresholds affects only later Cases.
Cases frozen under any earlier manifest version remain readable audit rows but
cannot advance: an undecided case is terminalized as
`BLOCKED/no_trade/news_generation_retired`; an already prepared order is not
rewritten and remains owned by the reconciliation state machine.

The HTTP shape uses `trigger_kind`, never the retired `case_kind`. Every case
and order row carries `strategy_id` and `strategy_version`. `/api/trading/status`
adds `counts.cases_by_strategy`, `counts.shadow_by_strategy`,
`counts.shadow_by_rule`, `counts.shadow_cohorts`,
`counts.event_study_cohorts`,
`counts.liquidation_promotion_ready`, and
`counts.liquidation_promotion_reason`. The two liquidation shadow strategies
write only `trading_strategy_evaluations`; they cannot produce a case or order.
Event-study cohorts expose `duplicate_rate_bps=null` and the named missing fact
`source:duplicate_rate_unavailable` until the upstream source publishes a
durable duplicate/replay denominator; surviving ledger rows are not treated as
evidence of a zero duplicate rate.
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
