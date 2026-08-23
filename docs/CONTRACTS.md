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

Issue #160 also retires every policy-v9 action/priority knob:
`escalate_magnitude`, `min_push_magnitude`, `min_watchlist_magnitude`,
`unclear_push_min_magnitude`, `unclear_push_event_types`,
`high_priority_escalates`, `noise_veto_max_magnitude`,
`noise_veto_respects_gate_priority`, and `contested_push_min_magnitude`.
Remove them before deployment; the strict settings schema provides no alias.

Issue #129 also retires `news.triage.deadline_seconds`; the content-addressed
Program artifact owns the route deadline. Existing configs must remove the key
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
artifact-owned `max_tokens`; changing this endpoint changes only the secret-free
`reader_card.primary` runtime binding identity, not Program identity.
`llm.news_compiler_tariff` is an optional, secret-free contract used only by
the manual cold compiler. Its `tariff_id`, positive `input_token_overhead`, and
positive task/reflection/metric-judge input/output micro-USD-per-million-token rates are all
required together; a partial or zero-rate tariff fails configuration. It does
not affect Workers or hot-path model calls. `tracefold config` reports only
whether this tariff is configured and its non-secret ID. `learning compile`
also requires an explicit local `--compiler-image sha256:<64 hex>`; tags and
registry manifest references are rejected. Compiler protocol/receipt v3 derives
three sealed role identities from typed task, reflection and `metric_judge`
configurations. Reflection has an exact 32k-token ceiling. The judge binds its
model/endpoint, instruction/schema, JSONAdapter, timeout/token/temperature/LM
kwargs and cache/retry contract, and its calls, cost and explicit unavailable
failures are receipted separately.
`llm.news_triage_fallback` (`api_key`, `base_url`, `model`; all-or-nothing and
only valid next to a complete primary triple; issue #65) is a second direct
endpoint used only when the primary Triage call fails — timeout, transport
error, truncated or invalid output — or while the Program artifact's primary-
route breaker is open. `llm.news_reader_card_fallback` is an optional complete
ReaderCard endpoint for that same fallback route and is valid only when
`news_triage_fallback` is complete. When it is absent, the Reader fallback slot
is an explicit alias of the EventSemantics fallback slot; when it is present but
invalid, the whole fallback route is unavailable rather than silently using a
different backend. The shipped artifact owns that breaker plus each
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
runner. `trading.mode` is `paper | live_reviewed | live_bounded` and is
startup-owned — a prompt or a tool argument cannot change it, and paper never
reads the OpenTrade token. `trading.candidates.*` bounds what may become a case
(`max_age_seconds`, `news_lookback_seconds`, `oi_lookback_seconds`,
`symbol_cooldown_seconds`, `max_rank_in_window`, `min_oi_value_usd`,
`max_dspy_cases_per_day`); `trading.regime.*` is the OI/price band
(`lookback_seconds`, `min_price_move_bps`, `max_price_move_bps` — a band with no
ceiling is rejected at startup); `trading.policy.*` gates the pure mapping
(`allow_short` defaults to false, `live_min_surprise`, `live_max_price_in`,
`min_whale_long_profit_bps`); `trading.venues.*` is the static priority over
`binance` and `hyperliquid`; `trading.order.*` is every order parameter
(`fixed_notional_usd`, `leverage` fixed at 1, `fixed_stop_bps`,
`take_profit_bps`, `max_holding_seconds`, `max_spread_bps`,
`max_open_underlyings`, `max_orders_per_day`), so the worst-case daily envelope
is the multiplication `fixed_notional x fixed_stop_bps x max_orders_per_day`;
`trading.opentrade.*` is the provider contract (`base_url`, `token_file`,
`request_timeout_seconds`). A live mode without a configured OpenTrade contract
or an enabled venue fails at startup, not at the first order.
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
(the console routes `/`, `/app`, `/app/*`, `/news`, `/news/*`), and `/api/*`.
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
| News | `/api/news/feed`, `/api/news/events/{event_id}`, `/api/news/status`, `/api/news/quotes`, `/api/news/review`, `/api/news/review/tasks/{task_id}/evidence`, `/api/news/review/tasks/{task_id}/responses`, `/api/news/review/external-misses` | broker-driven Event feed, one Event with frozen evidence/verdict/delivery audit, four-layer status, bounded quotes, and ReviewDesk reads/writes |

The public API is exactly these routes plus `/healthz`, `/readyz`, and
`/metrics`. The retired GMGN-lane routes (`/ws`, `/api/recent`,
`/api/events/by-ids`, `/api/search`, `/api/search/inspect`, `/api/token-case`,
`/api/target-posts`, `/api/target-social-timeline`, `/api/live-market`,
`/api/token-images/*`, `/api/token-radar`, `/api/stocks-radar`) are not
registered and answer the ordinary `404`; there is no alias, redirect, or
feature flag.

### News

News is an operator-bound, Strategy-qualified Event surface. The public
surface is exactly six GET route templates and two ReviewDesk POST route
templates:

`priority` is not a reader contract: feed/detail/OpenAPI expose no field,
filter, sort or badge for it. The hard-renamed `queue_priority` exists only in
broker scheduling, storage/audit/measurement and explicit operator review
projections; there is no public alias.

- `GET /api/news/feed?family={family}&admission={admission}&decision={push|escalate|drop|throttled|degraded}&symbol={symbol}&q={query}&limit={limit}&cursor={cursor}&outcome={pushed|held|pending}&hours={0..168}`
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
  or absent = no bound). Unknown query parameters, invalid admission or
  decision values, malformed cursors, and the retired `priority`/`sort`
  parameters return 400; out-of-pattern `outcome`/`hours` return 422. Recovery
  Events are visible with `admission=recovery`. `filters` echoes every parameter incl. `outcome` and
  `hours` (never the wall-clock bound, so unchanged pages keep their ETag).
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
  rather than restating a ticker that answers to itself. `tracefold news why`
  prints the same `outcome` sentence and timeline. Unknown ids return 404.
- `GET /api/news/status` returns `state` (`ready`, `warming`, `degraded`,
  `unavailable`), the Workers state, `health` (four thresholded items
  `ingest`/`broker`/`model`/`delivery` with `level` `ok|warn|bad|off`,
  `summary_zh`, `detail_zh`, and `overall`; thresholds are code-owned, see
  `docs/OPERATIONS.md`), `funnel_24h` (`received`, `candidates`, `triaged`,
  `tagged`, `grounded`, `decided_push`, `delivered`, plus
  `received_1h`/`delivered_1h`),
  `reasons_24h` (`stage` `gate|drop|throttle|push|degraded|ungrounded`, raw
  `key`, `label_zh`, `count`, sorted by count), and four layers: `ingest` (WSS
  connected, last frame/publish, error, open incidents, token configured; no
  Strategy IDs/counts), `broker`
  (configured, connected, per-queue message/consumer counts when observed,
  error code), `pipeline` (events and candidates per hour/day, Triage counts,
  degraded counts incl. `triage_degraded_by_code_24h`, decided pushes,
  throttled, Triage p50/p95, queue lag p95, the Triage model name, and the
  named 24 h maps `suppressed_by_reason`, `dropped_by_rule`,
  `throttled_by_key`, `pushed_by_rule`, `duplicates_withheld_24h`
  (`all` is the current content-only path; historical rows may retain the old
  `throttled` scope), plus `tagged_24h`,
  `grounded_24h` and
  the top-ten `ungrounded_by_symbol_24h`), and `delivery` (sent/terminal
  counts, last error, end-to-end p95, availability), plus
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
  `rates|liquidity|risk_premium|energy_supply|commodity_supply|commodity_demand|regulation|exchange_access|earnings_cashflow|positioning_flow|security_incident`;
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
and per-Predictor request/input/signature/instruction/demo/upstream/output,
finish reason, latency, token and cost identity. A told-only re-ask may restore
the complete `first_judgment`; evidence-changing re-asks may not reuse it.
`triage` is the only current stage. Current versions are
`news_title_norm_v2`, `news_gate_v5`, `news_storyline_v3`,
`news_semantic_program_v4` (or `news_oi_signal_v1` for deterministic telemetry),
`news_triage_policy_v10`, `news_delivery_card_v10`, artifact envelope v2,
factory `tracefold.news.semantic_program.factory_v4`, and epoch `program_v6`.
The exact Program identity is its content SHA, not the display version alone.

`ProgramArtifact v2` is the only executable semantic configuration. It is a
canonical, content-addressed, state-only JSON pair (`manifest.json` and
`state.json`) carried in the application image and selected by the code-owned
registry. Its `QualityKernelRef` binds the factory/topology, Signatures,
renderer, validator/normalizer/assembler, Adapter, execution and dependency
identities. Ordered code-owned RulePacks are authoritative; per-Predictor
LearnedStrategy is bounded advisory text; the typed DemoBank separates
model-visible input/output from provenance; and four logical model slots plus
token caps are explicit. Rendered instructions are derived bytes, never a
second editable truth. Loading fails closed on an
unknown hash/version/factory/lock, a path or symlink violation, extra file, or
unsafe or secret-bearing state. The lock digest is carried by the package and must match the
source `uv.lock`; it is not discovered by walking outside an installed wheel.
Demo evidence is accepted only in the exact model-visible input schema, which
excludes Event/fact audit ids, provenance, endpoints and credentials. The
optimizer can emit only a typed patch to the two LearnedStrategy instructions;
GEPA writes no demos, so DemoBank records and references remain empty. The
trusted side reconstructs the final Artifact from the exact
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
graph, so primary plus fallback is at most six. The artifact's 20-second
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
(0, disabled): the deterministic open-interest lane's thresholds (#137).
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
restatement guard, the action order is deterministic listing/telemetry,
grounded watchlist, eligible `reader_value=escalate`, eligible
`reader_value=realtime`, background/none, then
`trade_relevance_inconsistent`; the retained stale-source and same-fact checks
run after action selection. There is no runtime reader quota. Retired quota and v9
action/priority keys
are rejected as unknown configuration instead of being silently carried
forward. `news.retention` keys are `raw_days` (30) and
`judged_days` (365, >= `raw_days`): an Item behind an Event that carries a
verdict or accepted review is evidence and outlives the raw tier.

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
then-current release chain. `20260822_0295` preserves v1-v3 and appends the
`program_v5` epoch with factory v3 on the artifact-v2 envelope. `20260823_0301`
hard-renames `news_events.priority` to `queue_priority`, appends atomic
editorial/scored/runtime-manifest identity to verdicts, trips prior canaries,
and starts `program_v6` with factory/executable v4 and policy v10. Every earlier
row and review version is audit-only for the current compiler and release chain. A database
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
- `GET /api/news/review` is the ReviewDesk read surface. Query fields are
  `view=queue|coverage|proposals|market`, `mode=event|pairwise`, exact
  `cohort`, `stratum`, `event`, `status`, `hours`, `limit`, and opaque
  `cursor`. Queue tasks are deterministic and carry `task_id` plus an ETag-like
  `task_version`; coverage separates received, replayable, reviewed, accepted,
  external-miss and holdout-ready counts. `view=market` is explicitly
  non-causal, defaults to the latest homogeneous
  Program/policy/runtime-model cohort in
  the requested window, uses mature denominators, hides the unreliable v1
  event taxonomy, leaves the retired direction/magnitude/event-type price
  rankings empty, and clusters similar withheld Events from at most the latest
  seven days into one fact row. Market view rejects `hours > 168`; the separate
  evidence-coverage view retains the 720-hour option.
- `GET /api/news/review/tasks/{task_id}/evidence` requires `If-Match` and
  returns only the evidence scoped to that task: immutable source/fact
  snapshot, exact Program/policy/runtime-model cohort, input/told ledger,
  policy trace, real sent
  receipt, and verifier flags. Market reactions are hidden until a judgment is
  accepted so they cannot anchor `should_push` review.
- `POST /api/news/review/tasks/{task_id}/responses` requires `If-Match`, a
  UUID `Idempotency-Key`, and a bounded rubric or blind-pairwise body. It
  appends the judgment and its acceptance receipt atomically, never overwrites
  history, and returns the next deterministic task. Blind pairwise critical
  errors are side-qualified (`A:` or `B:`) and limited to the code-owned
  factual/entity/direction/key-fact/duplicate/injection safety enum; the sealed
  arm mapping converts a candidate-only critical error into release evidence.
  `POST
  /api/news/review/external-misses` applies the same receipt contract to an
  immutable fact the pipeline never turned into an Event. These are the only
  News HTTP writes. They use the existing Serve connection in an explicit
  read-write transaction; PostgreSQL permits that role to INSERT only the two
  append-only review fact tables and still denies all News/control rewrites.
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

`validate-projections` is a strict Serve-role read. It does not acquire the
maintenance lock, so operators can inspect the running singleton without
interrupting it.

`db audit` reports the migration revision, row `counts` for every table in the
code-owned `NEWS_TABLES` contract, `news_schema` exactness over that same set,
and the runtime-role contract including a role-authentic Workers evidence
append without rewrite access (current at migration `20260823_0301`). Since
#104 it also reports `trading_schema` over the code-owned `TRADING_TABLES`
contract; the two registries stay separate so "exactly these tables" remains a
per-capability claim.
`db query-audit` covers bounded reads for `/readyz`, `/api/status`, and every
News GET; the two ReviewDesk POST paths are explicitly catalogued as write
routes rather than falsely EXPLAINed as reads. `/healthz`, `/metrics`, and
`/api/bootstrap` are declared no-SQL routes.

`news bus-check` connects, declares the topology idempotently, and prints
per-queue message/consumer counts.
`news review queue|evidence|submit|external-miss` is the CLI form of the same
ReviewDesk contract as HTTP; submissions require the task version and an
idempotency key.

`news learning baseline --from-ms N --to-ms N [--mode
recorded|compile_live|runtime_live] [--action-source recorded|policy]
[--max-model-cases N] [--all-cohorts] [--semantic-judge MODEL] [--limit N]
[--out FILE]` scores the
stable Program over accepted reviews and returns one content-addressed
`tracefold.news.program_baseline_report.v2`. It is read-only — no dataset,
sandbox, tariff or container, no write to any table, and the only database
contact is one `serve` connection that closes before the first model call.

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
`tracefold.news.development_compile_episode.v3`; a dataset frozen under v2
carries no policy and is refused at validation instead of failing inside every
metric call.

`--mode recorded` makes no provider call; `--all-cohorts` drops release-plane
eligibility — for the seed sent ledger too, not only the cases — so a retired
arm's corpus is measured against the ledger `decide()` would actually have read.
`--semantic-judge MODEL` scores free-text retention anchors by meaning instead of
byte equality (#148) through the same `CardEquivalenceJudge` contract that the
hermetic compiler wires through its separate `metric_judge` role. Judge failure is explicit unavailable, enters the affected
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
v2/v3 rows remain readable audit history but cannot enter v6 metric/GEPA/release
evidence. Listing/telemetry do not enter relevance gold; grounded-watchlist
cases are separated as policy evidence. `gold_coverage` reports how much of each
component is actually scored.

`news learning freeze` seals accepted reviews into a content-addressed
development or future temporal validation dataset. Every current dataset is in
the deployment-time `program_v6` epoch and accepts only `news_review_v4`;
every earlier Prompt/Program/review cohort is audit-only and cannot enter a
dataset, metric-v4 denominator or DemoBank.
`learning compile --development SHA --artifact-root DIR --out FILE
--max-metric-calls N --max-task-model-calls N --max-reflection-model-calls N
--max-metric-judge-model-calls N --max-cost-microusd N [--seed N]` is the
manual cold DSPy GEPA workflow. A trusted exporter recomputes the
development dataset and ordered episode roots, then launches an isolated runner
without DB/holdout/application credentials. The runner is bounded by the
declared per-role calls, combined cost, seed and resource policy and can emit
only a typed `ProgramPatchV2` for the two LearnedStrategy instructions; DemoBank
stays empty under GEPA. A trusted
applier validates the complete receipt chain and builds an unaccepted
content-addressed Artifact from the exact stable root. The runner cannot read
holdout, register, accept, deploy or promote its output.

`learning propose` seals exactly one candidate variable (`program` or
`policy`) against a development dataset. `learning evaluate` runs the
development/offline or validation/holdout release gate; validation calls both
arms sequentially and `--live-program` can append exact per-Predictor
recordings. Its mutually exclusive `--verify-recordings` mode is limited to
offline/holdout Program candidates: it loads the exact existing run corpus,
re-executes both real arm-scoped Program graphs with no live provider fallback,
and seals the matching corpus/observation roots into the evaluation report. A
missing corpus or recording produces an `incomplete`/`UNKNOWN` evaluation with
no live fallback; an identity or tamper mismatch fails closed.
`learning shadow --live-program` cold-runs the candidate over the closed
validation window and seals the observations; an existing sealed shadow
observation manifest can be replayed instead.
`learning canary arm|status|hold|resume|trip|close` owns the durable one-arm
rollout. A candidate may advance only when the prior
stage has a sealed PASS; a tool or optimizer may propose but cannot accept,
deploy or promote. Canary selector `news_canary_selector_v2` includes queue-high Events, excludes
recovery/listing/telemetry lanes, and validates selector, eligibility profile,
rolling profile and runtime-manifest identity at startup, resume and assignment;
drift trips the activation. `news replay <hits.json> [--gate-policy config|open|strict]` runs
Deduper+Gate over saved provider hits without broker or model and lists every
Event with admission, grounded assets, and preliminary storyline. `news why
<event_id>` prints the Event's chain (item, gate, triage, decide, delivery)
and a one-line `outcome`. `news dlq inspect|replay|purge [--limit]`
peeks, republishes, or purges `news.dead`.

The `trading` family is read-mostly, and deliberately has no command that
places, amends or cancels an order. `trading status` reports mode, control
state, the day's counters and the day's funnel plus the worst-case daily
envelope; `trading cases [--state] [--limit]` and `trading show <case-id>` read
the case, its order and its deduplicated remote observations. The three writes
are narrow: `trading blacklist list|add|remove` owns the canonical deny-list
(one row per underlying — `CL` blocks `CL` and `XYZ-CL`; a read failure blocks
every symbol), `trading control running|close-only|paused` sets the runtime
control (`CLOSE_ONLY` and `PAUSED` still permit reconciliation and the
deterministic safety close), and `trading approve|reject <order-id> --digest`
settles one order bound to its exact frozen payload digest, idempotent by state
so a second approval of an already-approved order changes nothing.

Trading's News projection contract is `program_v6` / policy v10 only.
`trading_manifest_v2` freezes the learning epoch, lane-specific Program
version and SHA, policy version, editorial origin and SHA, scored-judgment SHA,
and runtime-manifest SHA. Older v1 cases remain readable audit rows but cannot
advance: an undecided case is terminalized as
`BLOCKED/no_trade/news_generation_retired`; an already prepared order is not
rewritten and remains owned by the reconciliation state machine.

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
