# Public Contracts

Tracefold exposes one configuration contract, one HTTP service, and one CLI. This document records stable behavior; generated OpenAPI is authoritative for exact HTTP fields.

There are no compatibility aliases for retired products, tables, worker names, routes, or response fields. A behavior change updates source, tests, generated contracts, and this document in the same change.

## Runtime configuration

The active operator-owned application file is
`~/.tracefold/config.yaml`. It contains deployment/domain choices,
one PostgreSQL DSN and password-file reference, credentials, API bind/auth,
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
an empty Telegram bot-token placeholder, and bootstrap/application PostgreSQL password files. The operator directory is
mode `0700`; config, Telegram placeholder, and password files are `0600`. A normal rerun preserves
existing config and password contents while repairing permissions.
`tracefold init --force` replaces only `config.yaml`; it does not rotate
existing database passwords. The generated config has a new API bearer token
(`ws_token`) but no live provider/model/webhook/bot credential, `news.push.enabled`
is false, and `news.broker.url` points at the compose RabbitMQ service.

Normal `tracefold init` changes no config content. The one-shot #433-C
(`trading.order` / `trading.bindings`) and #449 (multi-login PostgreSQL) shape
rewrites it used to perform, and the `config_migrated` / `config_backup_path` /
`config_backup_paths` result fields that reported them, are deleted (#589). A
config still holding a retired key is refused by `Settings` validation, which
names the key, rather than being rewritten. A non-regular config path still
fails with `config_path_not_regular_file` before anything is written.

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

Issue #129 retired `news.triage.deadline_seconds`; the News Agent
stage and provider-call budgets are code-owned. #706 also retires
`llm.news_compiler_reflection` and the entire `news.policy` mapping.
`LlmConfig` and News settings forbid unknown fields, so remove these
keys from existing operator YAML before the matching image starts.

`llm.api_key`, `llm.base_url` and `llm.news_triage_model` form one
all-or-none direct generative route. A partial triple fails validation.
`llm.news_reader_card` may provide a separate complete card endpoint;
otherwise selected cards use the Triage route. The optional complete
`llm.news_triage_fallback` route supplies generated extraction and
judgment fallback; `llm.news_reader_card_fallback` may provide a
separate card fallback and otherwise aliases the Triage fallback.
Each endpoint's `request` block controls temperature, structured-output
mode and bounded `extra_body`; transport-owned or credential-shaped keys
are refused. The route's secret-free identity includes its endpoint,
requested model and request semantics, while keys never enter the digest.

`llm.news_judgment` is an optional, independent all-or-none
`api_key`/`base_url`/`model` System One route for News Jev judgments.
It neither reads `llm.trading_semantics` nor becomes a chat generator.
Without it, the same domain judgment contract runs on the generative
News route. `tracefold config` and `/api/news/status` report redacted
availability, model names and the current News program identity; they
never expose API keys or provider URLs. The absence of a complete
generative route leaves semantic work durable and pending, not a
fabricated no-news verdict.

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
`telegram_bot_token_file`, `telegram_chat_id` and the optional
`telegram_proxy_url`, plus
`min_interval_seconds`), and
`news.venues.*` (`enabled`, public-data switches `binance`, `hyperliquid`,
`okx`, `lighter`, `bitget`, reference-only `us_reference`, and `snapshot_period_hours`), and
`news.watchlist[]` (`{symbol}`) are the only News knobs.
`news.triage.concurrency` (default 4) is the real consumer width of its queue.
Lexicons, prefix tables, LSH geometry, the code-owned Program registry, and
policy versions are image state. `tracefold config` exposes only redacted booleans, counts,
model names, and watchlist symbols; it never prints the token, broker URL,
keys, webhook, or proxy URL.

Push delivery is available only when `news.push.enabled` is true, exactly one
provider is complete, and Workers is running. Feishu requires a valid HTTPS custom-bot v2 URL.
Telegram requires a secure bot-token file and one channel Bot API ID; the adapter
is the single owner of what a valid target is, and configuration reads the
operator's number without a second copy of that rule (#562 `5 rows 1 and 8).
Provider conflicts, invalid targets, and missing or insecure credentials fail
closed on the capability they describe: Serve remains credential-free, and an
explicitly enabled invalid provider configuration leaves Workers running with
`news_delivery` reported `unavailable` and its reason, while reception,
admission, triage and the market loop carry on.
The editorial sender transmits the frozen body for one selected
EventUpdate intent. A successful initial send records the actual provider
message and body before optional presentation enrichment. On Telegram,
typed venue-catalogue evidence may enrich a ticker link, tradeability
and anchored price context through an in-place edit of that same message.
Incomplete catalogue reads do not claim an asset is untradeable. The
desired edit and its outcome are persisted; a crash or uncertain edit
becomes `edit_state=ambiguous` without retracting or resending the
initial `sent` receipt. Feishu has no editable capability. Typed market
notifications keep their own card path.

When push is disabled, Workers still accepts and analyzes News; delivery
availability is reported separately. It cannot turn an unavailable
sender into a sent receipt or a reader-known claim.

`trading.*` is `enabled: false` by default. When enabled, a separate Analysis
process consumes the News trade-event outbox and runs a real model. `trading.analysis`
accepts `model_name` (or the configured News triage
model), `active_policy=entry_plan_v1`, `publish_signals=false`,
`root_ttl_seconds`, `model_timeout_seconds`, `max_active_cases`,
`max_model_input_bytes` (default 65,536), `max_model_output_tokens`, `max_model_concurrent_calls`,
`model_cost_budget_microusd` (default 5,000,000) with both route price ceilings
in USD per million tokens (defaults 100 input / 500 output; all three are set or
disabled together). These are conservative admission assumptions supplied by
the operator, not the provider's reported charge; a returned actual cost over
the cap is recorded as a diagnostic. Set the price ceilings for the configured route's
actual billing model; an inflated ceiling can exhaust the Case cap before a
request reaches the provider. Input-size refusal has its own
`model_input_budget_exceeded` code. The market budgets are `market_max_connections`,
`market_max_cached_rows`, `market_weight_soft_limit_1m`, excluded economic
asset IDs and reviewed native routes.
A reviewed route states source symbol, canonical `asset_id`, exact native
symbol, units per contract and an evidence reference. Unknown multipliers do
not silently become one-unit routes. Model credentials use the existing `llm`
endpoint and never enter the brief.

`trading.execution.enabled` defaults false. `execution.binance.environment`
selects `LIVE`, `DEMO`, `TESTNET`, or is omitted for the pinned SDK default.
`execution.account_slot` identifies the one Binance USD-M account
and deterministic client-order namespace. Operator-owned credential file paths
belong only to the Runtime. Trading has no Workers alert task or
`watchdog_enabled` setting; Runtime state and execution observations remain
available through the Trading status and observation read paths.

`trading` has no `control` or `notifications` block: #528 deleted the Telegram
command ingress and both never-run notification senders, and a config that still
carries either block now fails strict validation. Config diagnostics publish
only resolved paths and configured booleans.

Secret-file paths are resolved relative to the operator config directory
unless absolute. Config and status may report only the resolved path or whether
it is configured; they never expose secret contents. Disabled execution constructs
no TradingNode. Enabled execution requires secure non-empty files and selects
the configured native Binance connection through one Nautilus owner.

  The frozen `trade_brief_v4` includes `citable_evidence_ids`, the exact
  evidence keys that pass the compiler's availability rule at the Case cutoff.
  The model may cite only those IDs in its supporting/opposing arrays; missing
  frames and other brief fields can appear in the rationale as limitations.
  Catalyst source evidence carries the public `headline`/`why` text when present;
  an empty source value set is marked missing instead of presenting a false
  `ok` citation. Source text remains untrusted input to the model.
  The native DSPy 3.4 ReAct agent receives code-owned `entry_plan_v1` instances.
`immediate_entry_v1` requests an entry at the current executable quote;
`closed_bar_cross_v1` waits for one named direction and frozen level. The
`trade_assessment_v4` proposal selects one plan for TRADE/WATCH, or none for
NO_TRADE, and cites visible evidence and optional Jev judgment refs. The
compiler derives side and the ATR14 exit template from the selected plan.
The model cannot set position size, stop distance, arbitrary order type or
override the source, asset, expiry and Case fences. Market snapshots may append
new plans without replacing the frozen seed; identical inputs retain plan IDs.
The initial reader requests 241 closed one-minute bars so 15m, 60m and 240m
features represent their actual intervals. Missing coverage is missing, not
a shorter interval relabeled as 60m. Binance OI history uses separate 5m
`sumOpenInterest` quantity and `sumOpenInterestValue` fields.

A WATCH freezes one directed crossing, parent plan ID, previous close, exit
plan and root expiry. The observer scans contiguous closed bars from the
frozen point. An on-time match creates at most one conditional child Case;
the child re-analyzes the same source and can only select the parent's side.
Gaps, late matches and duplicate polls remain explicit. Neither WATCH nor
NO_TRADE grants an order or closes an existing position.
Each DSPy transport invocation and returned response is indexed by claim
attempt even when validation fails or a late attempt loses the settlement
fence. A provider error or cancellation retains its request and error type;
the provider response is marked unconfirmed, and unavailable tokens and cost
remain null.
The Case API exposes attempts, validation errors, WATCH state, tool observations,
requested/served model, the final input manifest and the root chain. Replay
reads their archived refs without a model or market call.

Historical `shadow_net_v1` research remains available through the offline
evaluation script. Its simulated results are never venue fills or realized PnL.
In the offline rule arm, a verified arrival quote that fails the frozen entry
structure, price envelope or spread bound produces an archived `refused`
receipt with zero trading cashflow and an `entry_refused` count. A missing or
unverified quote remains `unevaluable`; refusal does not count as a simulated
trade or supply a net entry sample.
Venue net PnL requires reconciled fills, commissions, funding and protection
receipts; the execution summary alone does not satisfy those inputs. Signal
publication is controlled by `publish_signals`.

`GET /api/trading/status` reports current decision and Runtime projections.
`GET /api/trading/cases/{case_id}/replay` reads frozen source, evidence and
assessment references without calling market data or the model. Its optional
`attempt=<claim_attempt>` selects one failed or late attempt's archive; omitted
means the latest attempt, while old Cases fall back to their Decision archive. A published
TRADE commits one `TradeSignalV3` with the Case decision and state in one
transaction; unpublished TRADE and NO_TRADE create no online Signal. SignalV3
contains account-slot isolation, `entry_scope_id`, native mapping digest,
versioned exit parameters and a bounded entry-price envelope with explicit
immediate or activated condition semantics. Nautilus
rechecks current Trading fact validity after persisting its scoped TradePlan
and before sending an order. The retired binding, Capital, capability, catalog, Intent, order,
replay and evidence-clock tables were dropped by `20260901_0347`; execution
writes only `trading_operator_intents`, append-only
`trading_execution_observations`, the slot-keyed current control projection, and
the generation-fenced `0343` current Runtime projection. `20260903_0359` dropped
the `0342` notification delivery ledger and the partial observation index that
fed it: the channel was never assembled in production and the ledger held zero
rows. `20260904_0360` dropped the Case's lease, attempt counter, always-empty
supplemental source keys and three restated policy identity columns, the
admission ledger's write-only `release_revision`, and the Signal ledger's
`alpha_metadata`; it narrowed the admission primary key to `(source_key)` with
`gate_version` / `gate_config_digest` moved into `evidence`, and replaced the
Runtime projection's `routes` array with `routes_count` (#537 PR-3).
`20260904_0361` dropped the Runtime projection's `runtime_release`,
`config_sha256`, `runtime_revision`, `image_digest`, `credential_fingerprint`
and `lifecycle_state`, and the observation ledger's `runtime_release` column and
stored payload key (#537 PR-4). `20260903_0356` dropped the profile
activation ledger and the Decision Plane heartbeat with it, and renamed both
execution identity columns to `account_slot`; `20260903_0357` dropped every
JSON-shape CHECK on those tables, the four `trading_*` functions behind them,
and the `payload_digest`, `alpha_contract_sha256`, `evidence_sha256` and
`confirmation_identity` columns, leaving the Pydantic contract as the only
validator of an execution fact's shape; #520 PR-B then dropped
`confirmation_identity` from that contract with the `CONFIRM` token itself.

A `position` observation's summary is `{status, quantity, avg_entry_price,
exit_price, exit_reason}`, every value the string of its Decimal and each key
present only once the fact exists. On `closed`, `quantity` is what was open
immediately before the close, `exit_price` is Nautilus
`PositionClosed.avg_px_close`, and `exit_reason` is the plan's exit reason
(`stop_filled | take_profit | time_exit | operator_flatten | external`). An
`order` or `protection` summary is `{leg, status, reason?, trigger_price?}` with
`leg` one of `entry | stop | take_profit | exit | unknown` (`protection` for the
stop and take-profit legs); a `fill` summary is `{leg, last_quantity, last_price,
commission, commission_currency}`. Realized PnL is not an observation: readers fold
it from the fills (#680). An unexpected-exposure `risk` summary is `{risk_fact:
unexpected_exposure, count, exposure}`, written whenever the set changes (an empty
set is the all-clear); `exposure` joins, with commas, `position:<id>` and
`order:<client order id>` (exposure no plan claims, or reduce-only protection kept
on an instrument the Cache holds no position on), `unconfirmed_close:<instrument>`
(a close none of the Runtime's legs sent, waiting for a flat venue read) and
`venue:<SYMBOL>:venue=<qty>:cache=<qty>` (two venue reads in a row disagreed with
the Cache, #680 PR-3). The execution Runtime's `entry_block_reason` adds
`venue_unverified`, and a `signal_disposition` can be `venue_unverified` when a
Signal's TTL ran out waiting for the venue.

All database consumers use `storage.postgres.dsn` and `password_file`. Process
identity is the connection's stable `application_name`; Serve's HTTP pool is
connection-level read-only.

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
Worker topology, News broker topology and consumer set, and all resource
capacities are code-owned. Configuration cannot add another worker or derived
product lane.

## Operator lifecycle

The fresh-clone operator contract is `make up`. It preflights `uv`, Docker,
Compose, `curl`, an authenticated GitHub CLI, a 3.13 project interpreter, and
daemon access; runs idempotent initialization; builds the frontend and backend
image; performs fresh-volume role bootstrap; runs the one-shot migration; starts
Serve and Workers; and waits for required health and console boundaries.
Execution credentials are never a deployment prerequisite: the profile-gated
Nautilus Runtime has its own image and its own `make runtime-build` /
`runtime-up` / `runtime-restart` / `runtime-down` / `runtime-logs` /
`runtime-status` lifecycle, and `make up` never names it. A repeated invocation
preserves config, passwords, and named-volume data, including across
`make down`.

`make status` is `make status-app` followed by `make runtime-status`, and fails
non-zero when PostgreSQL, RabbitMQ, migration, Serve, Workers, either required
runtime readiness endpoint, or console HTML is missing or unhealthy. Disabled
mode rejects a leftover Nautilus container. Paper/live additionally requires the
Nautilus container, its health, and its `/readyz`. `make logs` follows the
bounded startup services. `make down` stops the stack without deleting the named
PostgreSQL volume, and refuses while a Nautilus container exists. These targets do not auto-hard-cut
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
(the console routes `/`, `/news`, `/news/*`, `/trading`), and `/api/*`.
There is no WebSocket endpoint.

- `/healthz` is process liveness.
- `/readyz` combines a lightweight PostgreSQL liveness check with the cached startup schema/composition result. It does not inspect providers, queues, or business freshness.
- `/api/bootstrap` returns `{ws_token}` so the served console can authenticate every `/api/*` call (`Authorization: Bearer <ws_token>`; read routes also accept a `token` query parameter). The one command POST takes the same token but only as a bearer header: a query token, a missing token, a non-ASCII token, or a wrong one is `401`. The separate console write token went with #520 PR-B.
- `/api/status` is exactly `{measured_at_ms, runtime}`. `runtime` combines the database probe (schema revision match) with the Workers heartbeat row and fails closed on stale heartbeats; there is no provider block.
- Read endpoints do not call providers, execute models, or mutate facts. The one
  command POST only appends an authenticated `OperatorIntentV1`; it cannot call
  Nautilus or Binance and its response is not a Runtime or venue receipt.

Status contains no provider/model credentials, base URLs, request policy,
capacity counters, Program instructions/demonstrations, or raw model responses.
Code-owned Program/policy versions may accompany bounded verdict telemetry; they are
not operator configuration.

API responses use a typed envelope:

```json
{"ok": true, "data": {}}
```

Errors use `ok: false` with a stable error code. Pydantic response models generate `docs/generated/openapi.json` and `web/src/lib/types/openapi.ts`; frontend code consumes those generated types.

**What this section is, and is not.** `docs/generated/openapi.json` is the
request and response shape: every path, parameter, field, type, enum and
nullability. It is regenerated from the Pydantic models by `make regen-contract`
and it cannot be wrong. This section states only what a schema cannot: retention
windows, admission and outcome vocabularies, ordering and pagination guarantees,
ETag basis, error codes and the field each names, provenance rules, and the
arguments behind them. Where the two disagree the generated artefact wins, and
`tests/contract/test_openapi_drift.py` fails when a path named here is not live
or a live path is unmentioned.

### Endpoint families

| Family | Routes | Source of data |
|---|---|---|
| Bootstrap/status | `/api/bootstrap`, `/api/status` | Serve configuration, database probe, and the Workers runtime row |
| News | `/api/news/feed`, `/api/news/events/{event_id}`, `/api/news/market`, `/api/news/market/{item_id}`, `/api/news/status`, `/api/news/quotes`, `/api/news/symbols/{base}`, `/api/news/wallets`, `/api/news/wallets/events`, `/api/news/wallets/events/{episode_id}` | broker-driven Event feed, one Event with frozen evidence/verdict/delivery audit, market observations read straight from `news_items` and their typed facts, one observation with its group timeline, four-layer status, bounded quotes, one symbol's identity, and the chain wallet tape's own state — its roster, its ingest position, and every card it opened with the price receipt taken after it |
| Trading | `/api/trading/status`, `/api/trading/cases`, `/api/trading/cases/{case_id}/replay`, `/api/trading/executions` | Execution readiness, scoped Case and analysis decisions, read-only frozen replay, and the folded per-entry execution table. Replay reads content-addressed evidence and the recorded assessment; it makes no market or model call. These GETs carry no order authority. |

The public API is exactly the paths in `docs/generated/openapi.json` plus
`/healthz`, `/readyz`, and `/metrics`; the table above says which owner answers
each. Everything else is retired: not registered, answering the ordinary `404`,
with no alias, redirect, or feature flag.

<!-- retired-routes:begin -->
- `/api/news/wallets/cards` (#641): replaced by token episode list/detail; no redirect.
- the GMGN lane: `/ws`, `/api/recent`, `/api/events/by-ids`, `/api/search`,
  `/api/search/inspect`, `/api/token-case`, `/api/target-posts`,
  `/api/target-social-timeline`, `/api/live-market`, `/api/token-images/*`,
  `/api/token-radar`, `/api/stocks-radar`;
- the Trading reads: `/api/trading/signals`,
  `/api/trading/execution/observations` and `/api/trading/execution/state`
  (#537 PR-5), and `/api/trading/gate` and `/api/trading/gate/{event_id}`
  (#589 PR-2);
- the manual console command route: `POST /api/trading/execution/commands`
  (#624); both GET and POST return `404` with no compatibility handler;
- the console mounts `/app` and `/app/*` (#589 PR-5). They answer the same
  `404`: the SPA has no `app` route, so serving `index.html` there returned the
  console's own "404 Not Found" screen under a `200`.
<!-- retired-routes:end -->

### News

News is an operator-bound, Strategy-qualified surface with two planes: the
editorial Event plane and the market-observation plane (#553). Every News route
is a GET and there is no News write route at all; the generated schema is where
the set is counted. The four
ReviewDesk routes — two reads and the only two News HTTP writes — were removed
with the console page they served (#256); `news review
queue|evidence|submit|external-miss` is now the whole ReviewDesk surface, and
it reaches `news_reviews` through its own Serve-role connection.

`priority` is not a reader contract: feed/detail/OpenAPI expose no field,
filter, sort or badge for it. The hard-renamed `queue_priority` exists only in
broker scheduling, storage/audit/measurement and explicit operator review
projections; there is no public alias.

Every normalized OpenNews frame is classified once by
`opennews_source_classifier_v2` (`tracefold.news.source_contracts`). The closed
family set is `news_v1|listing_v1|oi_v1|liquidation_v1|smart_money_v1|unknown_market`
and it resolves into **two disjoint vocabularies** (#553):

- `EVENT_KINDS` is `news|listing`. These are the only frames that open a News
  Event, and `news_events.event_kind` holds exactly one of the two.
- `MARKET_KINDS` is `oi|liquidation|smart_money|unknown_market|wallet`. These
  are persisted as `news_items` rows carrying `market_kind`,
  `market_source_strategy_id`, `market_parse_status` (`parsed|raw`),
  `market_parse_error` and the frame's own `provider_params`, together with one
  typed fact row in `news_oi_signals`, `news_market_liquidations`,
  `news_market_smart_money` or `news_market_wallet_events`, in a single
  transaction. They open no Event, take no admission, pass no Gate, storyline,
  evidence snapshot, verdict, model call or delivery, and they are read back by
  `/api/news/market` from those facts.

  `wallet` is the one kind the classifier can never produce (#572 PR-2). It is
  not a provider frame at all: the chain tape derives it from
  `news_market_wallet_fills` and opens the Item itself, through the same
  `admit_market_item` transaction, with `provider = 'robinhood_chain'` and no
  Strategy id. Its typed row carries `kind` (`buy|exit|crowding|digest`), the wallet, the
  token, the roster version it was following, the window, the numbers the card
  shows and the fill identities in `evidence`. Everything downstream — the
  notification loop, the card, the delivery ledger, the detail route, retention
  — treats it exactly like the four the provider sends.

A frame is one or the other, never both. The market families key on the
provider's **Strategy id alone** — `1019` OI, `2000` and `2083` liquidation,
`2026` smart money — because the four-tuple binding they used to carry made a
provider display name load-bearing: when `Large-scale liquidation` was renamed,
every frame under that id fell out of its own contract. The name, source type
and engine type are recorded on the fact and gate nothing. Listing stays a
generic route: the exact tuple `1353 / Listing and Delisting Announcements /
news / listing`, or any frame whose `engine_type` is `listing`. An unbound
scoreless `market`/`wallet` frame — a market Strategy this repository has no
template for — is `unknown_market`: stored and readable, never sent to the model
wearing a news costume. Everything else is `news_v1`.

A frame whose own Strategy tuples name two different market families is stored
as `unknown_market` with `market_parse_error = market_category_conflict`; one
with no template for its family stores `unknown_market_source`, and a family
whose template did not match stores that parser's named reason
(`oi_template_unmatched`, `liquidation_template_unmatched`,
`smart_money_template_unmatched`). The frame's *primary* Strategy decides the
branch, so an Item that accumulates a market Strategy across replays does not
drag an already-classified news frame into the market plane. Recovery runs the
identical path: same classifier, same parsers, same single transaction, no Event
either way. No Strategy id or title alone selects an editorial route.

`SourceContractReason` and the public `source_contract_reason` field are gone
with the market Event kinds. `news_events.source_contract_reason` survives as a
column named by `news_event_evidence_current_contract_check`, and is `NULL` on
every Event this code can open.

- `GET /api/news/feed` returns current Events newest first: the leader title,
  the durable routing facts, `update` (the adopted EventUpdate head, #706:
  `content_revision`, `adopted_at_ms`, `claim_n`, and `headline` with
  `headline_source` -- the card headline of the latest sent update intent,
  else the first claim the head has not retired), `legacy_verdict` (the Triage
  summary of an Event judged before #706; `null` for a News Agent Event), the
  representative reader delivery (the latest sent `first`/`update` card, else
  the latest attempt) and **one `outcome`**. An Event has exactly one outcome;
  it is the feed's task-tab filter and its SQL mirrors the three outcome groups
  (`pushed|held|pending`), so a row and the tab it appears under can never
  disagree. An Event with semantic work is on the EventUpdate path and reads
  `queued_semantic`, `semantic_failed`, `no_update`, `queued_notification`,
  `notification_deferred`, `not_notified`, `pending_delivery`, `delivered`,
  `delivery_ambiguous` or `delivery_failed` from its work rows, plan and
  intents; any other Event keeps its legacy verdict outcome. `hours` bounds
  `opened_at_ms` to the last N hours (`0` or absent = no bound).

  The Event-to-Triage handoff uses a code-owned 30-minute relevance ceiling. A
  marker-null handoff is pending at exactly the boundary and expired only when
  strictly older; a non-null marker remains published regardless of age.
  `expired_triage_handoff` and `expired_delivery_handoff` are historical `held`
  outcomes, never `pending`. Page rows, outcome filtering, and first-page counts share one
  request `as_of_ms`, so a row cannot be expired in the response but pending in
  its counts. `expired_delivery_handoff` can no longer be reached: the
  historical verdict-to-card handoff was a `news_delivery_queue` row written
  with the verdict (#598 D2). Current EventUpdate notification work is read
  from its own durable marker and intents. The old branch stays for old rows.

  The feed is the editorial plane only: the query filters
  `e.event_kind IN ('news','listing')`, so a market observation is never a feed
  row under any filter, and `/api/news/market` is where it is read (#553).

  The set-valued filters accept comma-separated, duplicate-free closed sets.
  `subject_code` matches the adopted head's IPTC `topics` (else a legacy
  verdict's subject codes); `source_authority` matches the code-owned authority
  of any source the adopted head cites (else a legacy verdict's editorial
  authority); `final_decision` and `direction` exist only on legacy verdicts;
  `event_kind` filters the durable source/routing fact. The retired taxonomy
  axes `event_family`, `change_state` and `assertion_status` are not parameters
  (#706) and return 400 `unsupported_query_param`. Every filter composes with
  every existing predicate before the count aggregate and cursor pagination.

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

  `admission` is the closed set `candidate`, `listing_deterministic`,
  `suppressed_pr_template`, `suppressed_low_signal`, `recovery`. The three market
  admissions `telemetry_deterministic`, `liquidation_deterministic` and
  `unsupported_market_contract` were deleted with the Event kinds they named
  (#553); their Chinese labels remain in `tracefold.news.outcome` so historical
  rows still render.

  Supplying both `q` and `symbol` returns 400
  `news_feed_search_conflict` before repository work. Unknown query parameters, invalid `admission`,
  `final_decision` or `direction` values, malformed cursors, and the retired
  `priority`/`sort`/`oi` parameters return 400; out-of-pattern `outcome`/`hours`
  return 422. Recovery
  Events are visible with `admission=recovery`. `filters` echoes every parameter incl. `outcome`,
  `hours` (never the wall-clock bound, so unchanged pages keep their ETag) and `direction`.
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
  admitted on — grade B+/A/A+ or a literal cashtag, with crude requiring the
  registry's energy context and every other commodity requiring its own name
  in the text) and beside it `assets[]` — the durable `news_event_assets`
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
- `GET /api/news/events/{event_id}` returns one **editorial** Event in full: its
  `timeline` (ordered steps `received` → `gate`, then a legacy verdict's
  `triage` → `decide` or the EventUpdate path's `evidence` → `semantic` →
  `notify`, then `delivery`, each carrying the raw `facts` it was built from, so
  a step is auditable rather than narrated), its member Items, its deliveries
  (each with its `intent_id`), and `normalization[]`. Since #706 it carries
  `event_update` -- the adopted head's claims (mode, phase, content kind,
  quantities, time, conditions, assets, citations with their source, retired
  and disputed flags), `changes` with the previous claim's statement when it is
  found in this Event's own history or a related Event's current head (else
  `null`, never guessed), per-source `evidence_relations`
  (`supports|refutes|reports|not_addressed|unresolved`), `implications` marked
  with their origin as inference, `open_questions`, `topics`, and the content,
  input and adoption identities -- and `processing`: the semantic work state,
  its recent observations, the notification plan with one named decision per
  claim, and every update intent with its real state, receipt and exact sent
  body. A legacy Event keeps `legacy_verdict`, `verdicts`, `evidence_inputs`
  and `late_evidence` as history; its retired taxonomy axes are published only
  on a verdict row, as the stored codes with no vocabulary. Nullable
  `prompt_version` on a verdict is Prompt-era audit history only and is never
  written again.

  `normalization[]` is the alias groups this Event's assets fall into. Only
  the code-owned seed aliases count (`source = 'seed'`, reconciled from
  `ALIAS_SEEDS` on every snapshot): the venue-derived rows (`XYZ-{base}`,
  `dex:SYMBOL`) are mechanical and would fire the block on every commodity
  Event. Only groups that actually collapse more than one name are sent, so the surface
  explains a surprise (SKHY / SKHX / SKHYNIX share one storyline bucket)
  rather than restating a ticker that answers to itself. For a grounded
  restatement, the decide step includes the prior Event id, sent timestamp,
  headline, history scope, and retrieval reason (`recent`,
  `exact_fingerprint`, `canonical_asset_overlap`, or `title_similarity`).
  `tracefold news why`
  prints the same `outcome` sentence and timeline. Unknown ids return 404, and
  so does an Event of a kind this contract no longer names: the migration keeps
  every pre-cut `oi`, `liquidation` and `unsupported_market` Event as immutable
  history, the read filters `e.event_kind IN ('news','listing')` exactly as the
  feed does, and the observation such an Event was built from is served by
  `/api/news/market` (#553).
- `GET /api/news/market?kind=...&from_ms=...&to_ms=...&limit=...&cursor=...`
  returns market observations in one absolute window, newest first, with
  *consecutive* observations of the same group collapsed onto their newest
  member. It reads `news_items` left-joined to `news_oi_signals`,
  `news_market_liquidations` and `news_market_smart_money` and asks nothing of
  the editorial pipeline, Trading or a model, so it answers whenever PostgreSQL
  does.

  `kind` is a comma-separated, duplicate-free subset of the closed set
  `oi|liquidation|smart_money|unknown_market|wallet`; absent or empty means every
  kind.
  Any other value is 400 `news_market_kind_invalid` (`field: kind`). `from_ms`
  and `to_ms` are absolute epoch milliseconds (`>= 0`): an absent `to_ms` is the
  request's wall clock and an absent `from_ms` is `to_ms` minus the 72 h
  `MARKET_WINDOW_DEFAULT_MS`. The window is absolute rather than a rolling
  offset because "what arrived on Tuesday" is a question an offset cannot ask.
  `from_ms >= to_ms` is 400 `news_market_window_invalid` (`field: from_ms`); a
  span wider than the 168 h `MARKET_WINDOW_MAX_MS` is 400
  `news_market_window_too_wide` (`field: to_ms`). That span bounds what one
  request may scan, not how far back the data goes: any window inside the
  retention is readable. `limit` is 1..100 (`MARKET_PAGE_MAX`), default 50;
  outside that range FastAPI returns 422. Optional asset, provider, venue
  and measurement_definition narrow the persisted observations.
  `sort=latest|oi_change|oi_value` defaults to latest. Numeric sorting requires
  kind=oi and an explicit provider/venue/proven measurement definition;
  otherwise 400 news_market_sort_scope_required. Typed measurement_window_ms
  and measurement_contract_status disclose whether the window is proven.
  Numeric sorting filters the full bounded window before collapse and ordering,
  never a limited first page. The latest sort retains its per-scan 5,000-row cap
  and explicit scan_truncated flag.

  Cursors bind the filters, sort and fixed end time; positions include sort
  value, receive time and Item ID for stable ties. Mismatched or malformed
  cursors are 400 news_market_cursor_invalid. A latest page resumes below
  its final run's oldest observation. The per-scan cap can split a longer run,
  which is disclosed rather than treated as a complete history.
 The group key is per kind and
  is computed in SQL: OI is provider, venue, native instrument and measurement
  definition; liquidation replaces the definition with the liquidated side;
  smart money is provider, Strategy id, trader label, account address, venue,
  native instrument, action and position side; a wallet observation is its kind,
  provider, the subject wallet (buys and exits), the token and the position segment
  or crowding window the rules assigned it. An observation with no typed fact is
  its own group (`raw|<market_kind>|<item_id>`), so unknown never merges with
  unknown.

  `filters` echoes the *resolved* absolute window, so a default-window request
  carries a wall-clock `to_ms` and revalidates every call. There is no
  page-level "is push wired" flag (#553 PR-2): every group answers that for
  itself, and one banner would be a second, weaker answer to a question the rows
  already answer.

  The `wallet_snapshot` carries only the concentrated net-buy episode contract;
  provider kinds have no wallet snapshot. The exact chain/token and frozen send/initial
  snapshot identify its facts. There is no legacy wallet-kind union.
  `notify_group_key` is deliberately not the display `group_key` — a smart-money
  display run breaks when the account changes action and the notification group
  must not, because that change is exactly what earns a card. And every
  quantity and dollar figure crosses the wire as its exact stored text, not as a
  JSON number: a JSON number would round a provider notional the ledger holds
  precisely, and the console renders these rather than computing with them.

  `sources[]` is one row per market kind — all four provider kinds always
  present — with the intake half (`received`, `parsed`, `raw`, `groups`,
  `last_received_at_ms`) and the receipt half beside it, where `merged` counts
  observations a card spoke for without being the record that triggered it.
  `unknown` is never folded into `failed` — the provider may well have delivered
  those. These are fact and receipt queries over the two reads the page already
  makes; there is no gate dashboard behind them.

  "Not pushed" is not a filter and never becomes one: whether a card was sent is
  reported per group and is not a precondition for reading the observation.
  Wallet episode `notification_state` is one projection over the delivery row, the track and the
  episode's own eligibility: `awaiting_decision` before the notification loop has decided,
  `pending` only while an unattempted intent has a next due time, `unavailable`, `sending`, `sent`,
  `failed` and `unknown` as the delivery says, and `not_alerted` with the real terminal reason
  otherwise. Every terminal reason is projected — there is no whitelist and no route-side fallback,
  so a notification-stage rejection with no intent row reads as its reason rather than as pending.
  `notification_next_due_at_ms` is present exactly when the state is `pending`.
  Wallet observations processed with notifications disabled read as `not_alerted` with reason
  `wallet_notifications_disabled` while the track is muted. Previously pending cards stopped by that
  policy retain their delivery row as `failed` with that reason and their original attempt evidence.
  A later alert round may report earlier unclaimed observations as `uncovered`; it never adopts them.
- `GET /api/news/wallets` returns `roster`, `tape`, `thresholds`, `funnel`,
  `collection_lagging` and `notifications_enabled` from one read snapshot. Authentication
  `token` is its only query parameter. `roster.address_count` is every unique valid address
  in the source response for chain 4663 and `stocks=false`; no rank, PF or PnL selects members.
  The source `window` defaults to `30d` and is unrelated to the 30-minute alert window.
  Membership versions change only when the normalized address set changes. Alias-only updates
  and successful refreshes preserve the version and each continuous monitoring start.
  `supported_count` uses the detector's coverage predicate, including the global start, member
  start and any window gap. It does not certify that a token has five qualifying buyers.
  `last_attempt_at_ms`, `last_success_at_ms`, `last_error`, `next_attempt_at_ms` and
  `consecutive_failures` describe the independent roster task. Failed/invalid responses keep
  the last valid list; attempts never masquerade as successful refreshes.
  `tape.high_water_*` and `scanned_*` encode the same continuous complete receipt prefix.
  The tape also exposes its next attempt, consecutive failures, blocked transaction and optional
  `enrichment_error`. Missing display metadata is not a collection gap or a failed scan.
  `thresholds` carries `required_n`, `window_ms`, `min_net_buy_usd` and coverage sufficiency.
  `collection_lagging` is server-owned; the browser does not substitute its own clock.
  `funnel` counts episodes, intents and sends over its stated 24-hour window with the leading
  unsent reason. These distinct stages must not be summed.
- `GET /api/news/wallets/events` returns `events`, full-scope `totals`, and a keyset
  `next_cursor`. `history_range=24h|72h|7d` defaults to 24h; `limit=1..200` defaults
  to 50 (UI 25). Optional `to_ms` anchors the range. Cursors bind the range and end
  time and sort descending by trigger time and episode ID. Changed cursor scope is 400.
  Statistics and rows use a single repeatable read snapshot.
- `GET /api/news/wallets/events/{episode_id}` reads that identity directly, independently
  of list scope. It returns the event, bounded raw `fills`, `next_fills_cursor` and
  `outcomes`. `limit=1..200` defaults to 100; `fills_cursor` preserves the timeline
  time boundary and descends by block/log. An unknown episode is 404.
  A horizon outcome's `status` is `comparable`, `missing_reference` or `late`. The fourth value the
  column and the wire union used to admit, `unavailable`, had no writer and is removed (#649 §9).
  Both initial and latest snapshots carry exact decimal strings, original raw quantities, roster
  membership, exclusions and exact chain cutoff, plus the two facts the card is built with:
  `token_first_seen_at_ms` (the tape's earliest movement in the token, which dates it as a bound and
  never as the chain's first block) and each member's `recent_episodes` (the episodes that address
  qualified in over the preceding fourteen days; `null` on a snapshot written before the count
  existed). A snapshot holds one `window` — the 30-minute rule — and the 5-minute window it used to
  carry beside it is gone from the contract, the rule and the page (#649 PR-3).
  The first snapshot is immutable. Current member DTOs contain no rank/PF/performance fields.
  The storage read boundary projects only the three retired member keys from historical JSON
  before strict current-model validation. Raw initial/send snapshots and delivery payloads remain
  unchanged; unknown fields are still errors, not silently ignored.
  Timeline pages never define the totals. Notification status and episode end are separate.
  Price outcomes record target/actual/reference time, source, nullable price/reference/change and
  `comparable|missing_reference|late`; missing never becomes 0%. `reference_price` /
  `reference_at_ms` / `reference_source` are the episode's t0 baseline, written once by the price
  sampler from the first price really available within its budget and never afterwards; the delay
  from the trigger is the two stamps. An episode that aged past the budget keeps no baseline rather
  than acquiring a backfilled one, and a baseline recorded at or after a horizon's target cannot
  make that horizon `comparable`.
  Retired `/api/news/wallets/cards`, single-wallet filters, segment views and DTOs have
  no redirect, alias or fallback. Unknown query keys return 400.

- `GET /api/news/market/{item_id}` returns one observation in full: the
  observation itself, the stored `provider_params` payload, the card that spoke
  for it (`notification_delivery`, or `null`), the Items that card covered, and
  a `timeline` of every retained observation of the same group, newest first, up
  to the `MARKET_TIMELINE_MAX` of 200. The receipt itself is never published —
  only which provider answered — because a receipt carries channel identifiers
  and the console's question is whether a reader was told. It is read
  by Item identity and is therefore not bound by the list's window: a link into
  a group that last reported nine days ago still opens. `item_id` is normalized
  (trimmed and lowercased) and must match `^[0-9a-f]{64}$` — the
  `sha256(source_id, provider record id)` Item identity — before it reaches an
  indexed lookup; anything else is 400 `news_market_item_invalid`
  (`field: item_id`). `token` is the only query parameter it accepts; any other
  is 400 `unsupported_query_param`. An identity no retained market Item has is
  404 `{"ok": false, "error": "news_market_item_not_found"}`.

  Two `trigger_reason` values need their own sentence. `raw` stays in the wire
  Literal and in `news_market_deliveries_reason_check` and no writer produces
  it: the four unstructured cards production sent before #582 `3.2 are
  receipts, and a receipt is not rewritten by a rule change — the same
  treatment the retired News delivery lane's `followup` rows get below.
  `action_change` is smart money's second card of a 24 h round and means
  exactly one thing, the first `open → close` of that round, and is headed
  `平仓`. That round starts at the first observation received for an
  (account, instrument) group and runs 24 h on the host's receive clock,
  never on provider event time; it yields at most two cards, both immediate,
  and a change of position side is not a trigger.

  `parse_status`/`parse_error` and `notification_status`/`notification_reason`
  are two independent pairs on both market routes, never folded into one outcome
  field: a record the parser could not read and a parsed record no card spoke for
  are both ordinary results, and one combined column would have to misreport one
  of them.
  `parse_status` is `parsed` or `raw`, and `parse_error` is non-null exactly when
  it is `raw` — the database CHECK states that pair as one fact.

  `notification_status` is an open vocabulary the notification owner writes, and
  it has two halves. With no send attempt it names the rule currently holding the
  observation: `unprocessed` (`awaiting_market_loop`), `historical`
  (`historical_not_alerted`, which is a recovery frame or the backlog that
  existed before the loop was enabled), or `merging` with the track's own reason
  — `merging_into_prepared_card`, `oi_change_below_followup_threshold`,
  `oi_anchor_zero_and_unchanged`, `liquidation_followup_window_open`,
  `smart_money_round_open`. Two final answers exist without a send. `uncovered`
  (`alert_round_ended_before_a_card`) is one: the alert round that held this
  observation ended before any card spoke for it, and the card that opened the
  next round covers that round only. `not_alerted`
  (`unstructured_record_not_alerted`) is the other: a record whose template no
  parser could prove is stored, grouped and readable and is never a card, so it
  has no notification track at all and nothing is holding it (#582 `3.2). The two
  are distinguished by whether a track row exists, never by an empty reason
  string. With an attempt it is the card's state:
  `pending`, `sending`, `sent`, `failed`, `unknown` or `unavailable`. `unknown`
  means this process could not read the provider's answer, so the card is never
  re-sent and is never reported as delivered; `unavailable` means no sender is
  configured, which consumes no attempt. The console renders both strings
  verbatim rather than glossing them.
- `GET /api/news/status` returns `state` (`ready`, `warming`, `degraded`,
  `unavailable`), the Workers state, a four-item `health` roll-up, `funnel_24h`,
  `reasons_24h`, and four layers — `ingest`, `broker`, `pipeline` and
  `delivery` — beside the watchlist symbols and the `instruments` universe
  summary. The `health` thresholds are code-owned; `docs/OPERATIONS.md` carries
  them and their reasoning.

  The counting rules are the part a schema cannot state.

  - `funnel_24h`'s four Event-feed stages (`received`, `admitted`, `triaged`,
    `delivered`) all start from Events opened in the same rolling 24 h cohort
    and test those Events' own durable stage facts, so the stages describe one
    population rather than four windows.
  - `reasons_24h` is sorted by count and carries the raw key beside its Chinese
    label, so an unlabelled rule is visible rather than hidden.
  - `ingest` publishes no Strategy IDs or per-Strategy counts. Its `recovery`
    summary carries `reason` (`recovery_pending|recovery_transient|null`); the
    broker layer carries the latest confirmed-publish failure code and timestamp
    observed by the running Workers process.
  - `pipeline.source_contracts_24h` is keyed by the two Event families
    `news_v1` and `listing_v1` only. Each counts the same 24 h Event cohort at
    exactly three stages — `received`, `parsed`, `verdict` (any Triage verdict
    for the Event). The market families are not here at all (#553): they open no
    Event, so an Event funnel cannot count them, and `parse_failed` /
    `unsupported` went with them. Per-kind market intake is `sources[]` on
    `/api/news/market`.
  - `duplicates_withheld_24h` reports `all`, the current content-only path.
  - `delivery.availability` requires both a complete declared provider and a
    running Workers runtime; one alone is not availability.
  - `instruments.last_snapshot_ms` is the most recent moment any venue answered
    a complete catalogue, read from the per-venue snapshot state: since #570 A11
    a refresh that changes nothing writes no instrument row, so the freshness
    answer is a fact about the refresh rather than about the newest row.
  - Every `instruments` figure but `dangling_aliases` and `reference_symbols`
    counts contracts on venues we poll. `reference_symbols` is the separate US
    listed-symbol directory (#91), which tells the Gate a ticker is a stock and
    is tradeable nowhere, so it is kept out of `trading`, `by_venue`, `by_class`
    and the `符号落表` funnel.

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

There is no `oi` block on `/api/news/status` and no `pipeline.telemetry_*_24h`
counter (#553). OI facts are readable at `/api/news/market?kind=oi`; current
Trading admission is recorded in `news_trade_events`, `trading_triggers` and
`trading_cases`. `tracefold trading gate` reads only historical OI v5 answers.

The Chinese vocabulary behind `outcome`, `*_zh`, and `label_zh` lives in
`tracefold.news.outcome` for admission, processing, delivery and historical
verdict terms; claim-level notification reasons are projected from their
current plan. A new surfaced rule or error code lands with its label so the
console does not render a bare key.

`/api/news/feed`, `/api/news/events/{event_id}`, `/api/news/market`,
`/api/news/market/{item_id}`, `/api/news/wallets` and
`/api/news/wallets/events`, `/api/news/wallets/events/{episode_id}` emit strong ETags and honor `If-None-Match`;
`/api/news/status` uses a weak ETag that ignores `measured_at_ms`. All News
routes require the operator token — a bearer header or a `token` query
parameter on a read — and answer `401` without one or with a wrong one.

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

Editorial EventUpdate identity is separate from the historical verdict key.
An Item's changed body, attribution or canonical source link creates a new
evidence revision with its own source provenance; exact retransmission does not.
Only evidence not yet analyzed for the Event enters the next extraction, while
adopted claims remain comparison context. A new evidence revision causes durable semantic work. The semantic result
binds its frozen input, program identity, actual model route and judgment
answers. The adopted EventUpdate has one CAS head per Event and an insert-only
content revision. A repeated or differently worded computation with no
substantive content change does not create a fresh business update. A content
reversal can be adopted again because the revision links to its predecessor.

Each adopted update carries `topics`, claims with typed assets, mode, phase,
time and cited spans, evidence relations, changes with prior/current refs,
optional conditional implications and open questions. The stable claim refs
belong to the Event, not to a model response index or a global graph. A
source relation can support, refute, report or leave a claim unaddressed; the
cited publisher's code-owned authority does not turn its allegation into a
verified world fact. An unresolved supplied prior is `possible_new`, not a
new Trading catalyst. Current topic codes are the pinned IPTC subset in
`tracefold.news.updates.topics`; the old four-axis taxonomy and its model
Predictor are retired. [News topics](NEWS_TAXONOMY.md) owns their scope.

The optional `llm.news_judgment` System One endpoint uses native Jev
Choice/Noul batches for narrow judgments. The generated backend is complete
without Jev and provides matching task fallback when an eligible native batch
fails. Results are cached by explicit task/input/model identity; successful
native answers are not voted on again by the generator. Extraction and
judgment share the configured News Triage generative route; selected cards use
the Reader route or its explicit alias. Model and stage budgets, including
bounded fallback, are code-owned.

NotificationPlanner reads the adopted content and the reader's actual sent
bodies, not the Event's observation history. It records one named decision
per claim: `notify`, `not_notified` or `deferred`. Content rules handle
commentary, promotion, forecast, schedules, unsupported price reports,
12-hour stale sources, watchlist interest and full sent-body coverage.
An unknown mode gets one bounded re-ask and then `mode_unknown`; an
overlapping in-flight or ambiguous send defers only affected claims.
`key` (⚡) is presentation for a corroborated state change or official
measure in a key topic family, not independent source verification.

A selected claim set and update revision define a stable `intent_id`.
CardComposer sees only selected claims and makes a frozen Chinese body;
the send path rechecks the head, reader revision and overlapping sends.
The ledger stores exact body, digest, provider message ID and result. A
proved `not_sent` retries the same intent and payload; an unknown outcome
is `ambiguous` and is not blindly retried. The queue and delivery ledger
are keyed by `intent_id`, not `(event_id, kind)`. Migration 0404 assigns
historical `first`/`followup` rows deterministic legacy IDs without
resending them. Telegram's later quote/tradeability edit stays bound to the
original message.

Adoption commits notification work and a public outbox independently.
`catalyst_delta` exposes structured claims, changes, citations and
deterministic text under `news_public_update_v1`; Trading starts its
freshness at the claim's first availability, not model completion. A
`source_update` names earlier content and affected claims. App dispatches
it before target selection; Trading stores an idempotent source amendment,
does not create a Trigger/Case or extend TTL, and can reject a still
unsubmitted entry whose cited proposition was corrected. The old
headline/why-only catalyst shape is rejected on the new path. OI and other
typed market facts keep their separate contract and do not enter the
editorial semantic/notification rules.

Historical `news_verdicts`, reviews, old taxonomy and learning tables
remain audit data. Current Event detail projects them as `legacy_verdict`
rather than translating them into claims. Current EventUpdate, processing
status, claim decisions and actual deliveries are distinct read fields;
the generated OpenAPI and TypeScript declarations own exact JSON shapes.
The retained legacy verdict/review decoder accepts the fact-kind vocabulary
`state_change|new_quantity|level_crossed|period_record|quantified_flow|official_measure|statement|recap|schedule|promotion`;
these values do not classify new EventUpdate claims.

Broker contract: topic exchange `news`, dead-letter exchange `news.dlx`, two
quorum business queues — `news.raw` (`raw.#`; single-active) and `news.triage`
(`event.#`) — and `news.dead`
(delivery limit 1,000,000 so nothing can lose terminal evidence by returning it). All names take
`news.broker.name_prefix`. Declaring the topology declares exactly those names
and deletes nothing else: any other name under the prefix — the retired Analyst
queue `news.deep` (issue #57), the removed retry lane `news.retry` (issue #400),
the retired delivery queue `news.deliver` (issue #598 D2),
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
`news.raw`, 4 MiB `news.triage`, 16 MiB `news.dead`).
`tracefold news bus-policy apply|verify` is the only writer; Workers verifies
the effective policy at startup and refuses to consume on a mismatch.

Notification work is not on this broker. Adoption writes a persistent
notification marker; the Deliverer polls it and plans an intent keyed by
`intent_id`. A selected card is frozen before sending. Queue and ledger state
retain bounded attempts and an explicit dead reason. A send with uncertain
provider outcome becomes `ambiguous` and is held rather than retried under a
new identity.

Message bodies are `news_bus_v1` JSON envelopes (`schema_version`, `kind`,
`message_id`, `trace_id`, `occurred_at_ms`, `payload`) with AMQP priority 0 or 5
and an `x-news-trace` header. `BusMessage.attempt` is derived from the broker's
`x-delivery-count` (absent on a first delivery) and is never written by a
publisher. Consumer outcomes are typed and each maps to exactly one AMQP
settlement: success acks, `TransientError` is a counted `reject(requeue=true)`
that the broker delays and finally dead-letters, `DeferError` is an uncounted
`nack(requeue=true)` for when the News DB lane cannot admit the message,
handler-side `BrokerUnavailable` / `BrokerBackpressure` uses the same counted
`reject(requeue=true)` and shared delivery budget as `TransientError`, and
`PermanentError` or a decode failure is `reject(requeue=false)`; an
unclassified handler exception settles nothing and fails the consumer. There is
no operator control plane: pause and mute were removed with
`news_control_state`. Current claim-level planner decisions, actual sent-body
coverage, in-flight sends and provider availability determine notification work.

Schema history is not an HTTP contract: [`MIGRATIONS.md`](MIGRATIONS.md) owns
the baseline, the head, and each revision's evidence. Two rules about migrations
are contract, and stay here. A database on the retired pre-#449 chain must be
restored with its exact pre-cut image and source, advanced to that chain's
terminal head, and cut over before current source is used; current source has no
upgrade path from an earlier revision. And migrations perform no provider,
broker, model, or outbound call and have no compatibility reader/writer. The
exact News base-table set plus four security-barrier review views is asserted by
the schema integration test instead of a duplicated prose allowlist.

- `GET /api/news/quotes?symbols={comma-separated}` returns one result per
  requested symbol, **in request order**, for at most 100 deduplicated symbols
  (`news_quotes_symbols_too_many` / `news_quotes_symbol_invalid` otherwise).
  Each result carries the requested symbol beside the exact resolved identity,
  the price with its `price_kind`, the optional `change_pct` with the
  `change_basis` it came from, and three separate clocks: the receipt clock, the
  optional provider clock, and the independent `reference_at_ms` /
  `reference_age_ms` reference clock. A single collapsed `age_ms` does not
  exist, because three ages answer three different questions. `state` is
  `fresh` when every applicable raw clock is no more than
  5,000 ms in the future and effective age is <=45,000 ms; otherwise a result
  with a price is `stale`. `unavailable` (nothing quoted yet) and `unlisted`
  (no venue we poll lists it) carry null timestamps, ages, basis and reference.
  A price is a positive decimal
  string or `null`; it is never `0`, and a failed venue leaves the previous row
  in place rather than blanking it. `change_pct` is `null` until the venue's day
  reference is known and becomes `null` again above 600,000 ms or beyond the
  future-skew bound. Reference expiry removes only the percentage: current
  price, basis and timestamps stay. Binance refreshes the reference only after
  a successful current store and persists it on the next natural turn;
  Hyperliquid's native reference shares the current receipt time. Current
  quotes are deliberately **not** feed
  fields — a price that changed must not invalidate the Feed ETag or re-run its
  count query every three seconds.
- `GET /api/news/symbols/{base}` returns what one `base_symbol` *is* (#207
  PR-W1), including the operator-alias `normalization` group when one collapses
  more than the base itself. Identity only — the token page's Events, price and
  rank window each keep their own endpoint, so nothing here is a second answer to a question one
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
  absent — the canonical economic asset is owned by
  `tracefold.platform.market_identity`, and a News route must not assert it.
- Trading reads source-native public bars directly for Case evidence. The
  retired Trading venue-catalog and replay/evidence command surfaces have no
  current worker, CLI, or HTTP path.
**One HTTP owner per durable aggregate.** Nothing crosses: a Case carries frozen
Alpha evidence, a Signal carries the engine-neutral handoff, Observations carry
Runtime facts, and status carries readiness plus bounded totals.

- `GET /api/trading/status` publishes decision and execution readiness.
  Execution separates process heartbeat (`alive`), entry permission
  (`entries_armed`), account projection success, convergence check and venue
  read times/failures. `current_account` has its own `observed_at_ms` and
  bounded Cache and venue-only rows with source, strict Plan association,
  protection status, typed findings and totals that reveal truncation.
  A venue-only row keeps unknown entry, mark, PnL and protection. `complete`
  describes field availability, not venue agreement. `protection_status`
  is `not_applicable | protected | pending | unprotected | unknown`.
  The CLI status uses the same projection. `readyz` means process liveness
  even when entry is blocked. The status response has `Cache-Control:
  no-store`, no ETag, and a server-computed `facts_remaining_ms`. Browsers
  spend this budget on a monotonic clock and cannot renew an old response.
  Serve reads no secret file or venue client.

- `GET /api/trading/cases` defaults to summary-only 24-hour state, reason
  and admission distributions. `case_id` reads one retained frozen Case,
  with unknown identity returning an empty list. `view=list` reads
  25 rows by default (limit 1..100), optionally filtered by state, asset,
  reason and source_item_id. `total`, `next_cursor`, window_from_ms and
  window_to_ms describe the list scope; distributions remain the current
  independent 24-hour aggregates. Cursors bind filter scope and end time,
  ordering by created_at_ms and case_id. Normal browsing covers 24 hours;
  source_item_id browsing covers retention and uses the manifest's exact
  contexts.oi.source_item_id. No symbol/time inference is permitted.
  Identity lookup cannot be mixed with list filters. Invalid state is 422;
  stale/mismatched cursor is 400. The obsolete underlying filter stays
  unsupported. Frozen policy identity and per-check measurements remain
  the evidence shown in a Case, and source_item_id may be null.

- `GET /api/trading/executions` — the desk table (#528 PR-1, PR-3).
  Optional case_id selects that Case's retained entries before limiting, outside
  the ordinary 24-hour window; manual entries have no Case. Each entry identity
  appears once. The ordinary list includes recent entries/recently closed plans
  and every nonterminal plan, even older than 24 hours, bounded to 100 rows with
  `complete` and no cursor.

  TradePlan supplies lifecycle and frozen intent. Optional plan fields are
  `plan_status`, `account_slot`, `instrument_id`,
  `entry_client_order_id`, `stop_distance_bps`, `risk_budget_usd`,
  `max_leverage_at_creation`, `exit_policy_id`, `take_profit_bps` and
  `max_holding_ns`; historical entries without a plan leave them absent/null.
  Observations supply known fills, prices, the stop and take-profit triggers and
  venue refusal text.
  `stage ∈ {pending, rejected, expired, ordered, filled, protected, closed}`
  uses plan lifecycle when available; a plan that ended `not_submitted` is
  `rejected`, and a missing observation never expires an active plan.
  `entry_filled_at_ns`, `position_closed_at_ns` and `duration_ns` preserve the
  available clocks, with frozen plan clocks used when native observations are missing.

  `realized_pnl_usd` and `fees_usd` are folded from the entry's fills: exit
  notional minus entry notional, signed by direction, minus every commission. They
  and `pnl_known=true` are present only when the exit fills sum to the entry
  quantity and every commission was charged in USDT; funding is not included.
  `funding_usd` and `net_pnl_usd` add signed venue
  `FUNDING_FEE` income to that fill fold. `net_known=true` requires
  complete signed income-scan coverage from first entry fill through last exit
  fill, USDT cashflows, and no overlapping plan for the same symbol/account.
  A complete zero-cashflow interval yields `funding_usd="0"`; absent or
  ambiguous evidence yields null. The account income transaction ID is the
  deduplication identity. A successful scan is recorded as a separate durable
  `funding_coverage` observation, never inferred from a funding-rate forecast.
  Exit reasons are `stop_filled`, `take_profit`, `time_exit`, `operator_flatten`,
  `external` (a close this Runtime did not originate), `venue_unknown` or
  `not_submitted`; historical stored reason strings remain readable.

  Account totals fold the fills of every plan the slot opened and closed, manual
  entries included. `realized_known_today_usd` and `realized_known_total_usd`
  sum only known PnL values; they are null when none are known.
  `closed_today/total`, `pnl_known_today/total` and `pnl_missing_today/total`
  accompany the amounts, so a closed plan whose fills cannot yield a result is
  counted as missing rather than as zero. The UTC day is a half-open interval on
  the plan's terminal clock; totals have no 24-hour limit. Plans that were never
  opened are not counted as closed positions. `history_complete`, `gap_reason`
  and `pnl_complete_today/total` were removed in #680 without aliases.
  `net_known_today_usd/total_usd`, `net_known_today/total`, and
  `net_missing_today/total` cover all connections. These are known subsets,
  not an account-equity statement.

- The HTTP console is read-only (#624). Operator commands are available through
  the local CLI only. The former browser command route, command request/receipt
  schemas, and the orphan `commands[]` control ledger payload are removed.
- The historical OI v5 admission ledger has no HTTP route. `GET /api/trading/gate` and
  `GET /api/trading/gate/{event_id}` were deleted in #589 PR-2: `/news/oi` was
  their one browser reader and #553 PR-1 deleted that console page with the OI
  Event each row was joined to. `tracefold trading gate [--source-key KEY]
  [--since-ms N] [--limit N]` runs two read-only statements — one bounded index
  scan of `trading_candidate_gate_decisions` in frame order, and one row by
  source key — and answers per Source the status, stage, named reason,
  retryability, frozen evidence (which carries the `gate_version` and
  `gate_config_digest` that decided the row), timestamps, attempt count and the
  linked `case_id` when one exists. No current Analysis writer updates this
  ledger; it publishes no current Case state or execution state.
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
- News: `news bus-check|bus-policy|instruments|review|learning judge-calibration|replay|wallets|why|dlq`;
- Trading: `trading status|cases|signals|observations|gate|commands|issue`;
- maintenance: `ops validate-projections`.

There is no `recent` or `search` command and no market rebuild/sync/reconcile
maintenance command. Mutating maintenance commands require an explicit
execution flag where the parser offers a dry-run mode. They operate from
persisted facts and stable target keys. A rebuild does not create an alternate
generation/run identity or make a provider response the source of truth.
There is no CLI command that creates an order or approves, rejects, resolves,
submits, amends, or cancels execution. `trading issue` only appends a bounded,
authenticated `OperatorIntentV1`; its success is not Runtime or venue evidence.

`validate-projections` is a strict Serve-role read. It does not acquire the
maintenance lock, so operators can inspect the running singleton without
interrupting it.

`db audit` reports the migration revision, PostgreSQL identity/settings,
catalog row estimates for every table in the code-owned `NEWS_TABLES` contract,
and exact News/Trading table sets. Role/ACL readiness is not a business health
check under the single `tracefold` application identity. `db audit --deep`
adds exact table counts for offline migration or restore evidence. Since
#104 it also reports `trading_schema` over the code-owned `TRADING_TABLES`
contract; the two registries stay separate so "exactly these tables" remains a
per-capability claim.
`db query-audit` covers bounded reads for `/readyz`, `/api/status`, and every
News and Trading GET. Its write-route set is empty. Any HTTP write route
fails the public-surface contract.
`/healthz`, `/metrics`, and `/api/bootstrap` are declared no-SQL routes.

`news bus-check` connects, declares the topology idempotently, and prints
per-queue message/consumer counts.
`news review queue|evidence|submit|external-miss` is the whole ReviewDesk
contract since #256; submissions require the task version and an idempotency
key, and open one short transaction under the shared `tracefold`
login. Append-only triggers and business constraints reject review rewrites;
the public Serve HTTP pool remains read-only. The sole Trading Command POST
opens its own bounded short application transaction outside that pool.

The current `news learning` family has only
`judge-calibration --model MODEL [--out FILE]`. It scores the retained
card judge against its fixed perturbation corpus and can write a receipt;
it does not write the database or choose a release. The former baseline,
freeze, readiness, GEPA run, candidate, evaluate, program-artifact and
canary commands are removed. They are not aliases or dormant runtime
paths. Historical learning rows remain audit material.

`news review queue|evidence|submit|external-miss` is the ReviewDesk
surface. It presents a versioned task and evidence, and an accepted
review is append-only under an explicit reviewer and idempotency key.
The current queue reviews EventUpdate notification intent rather than
the retired taxonomy Gold, pairwise or proposal contract.

`news replay <hits.json>` runs local provider-hit admission without
model, broker or outbound notification. It does not measure the
EventUpdate model. `news why <event_id>` reads one persisted Event's
chain. `news dlq inspect|replay|purge [--limit]` handles the durable
dead-letter queue; purge removes evidence and replay first verifies
the effective topology and policy. `news bus-check` and
`news bus-policy apply|verify` inspect and manage the declared broker
policy. The exact flags and output are in
[generated CLI help](generated/cli-help.md).

The `trading` family has no direct provider-execution command. `trading status`
renders exactly the `decision` and `execution` blocks
`GET /api/trading/status` publishes, from the same
`execution_readiness_projection`: when the lane last froze a Case, and the
configured execution mode and account slot beside this Runtime's readiness,
block reason, control flags, proven flatness and current account read model. One
projection, so the CLI and the desk cannot be told two different things about
the same instant (#537 PR-4, PR-5). It never infers protection, PnL, or fees.
`trading cases [--state] [--limit]` lists the Case ledger through the same
bounded projection the HTTP route reads;
`trading signals [--limit]` lists bounded Signal ledger rows (new publications use V3); and
`trading observations [--limit]` lists append-only Runtime observations —
those two ledgers have no HTTP route since #537 PR-5, and this is where an
operator reads them;
`trading commands [--action] [--limit]` lists authenticated operator intents
and their final disposition when present. `trading issue TEXT --request-id ID
--requested-at-ns NS` is the one local OS-authenticated writer: callers preserve
both sealed fields on retries, request identity is scoped by OS UID and hostname,
it accepts only the shared closed slash grammar, and manual entry still flows
through Runtime risk/OMS without fabricating Signal/Case/Alpha facts; success says
`intent_recorded_not_order_or_fill`. There is no blacklist,
capability, replay, evidence, quantity, leverage, venue, or direct order command.

Trading consumes News public projections through App mapping. OI remains the
deterministic typed ledger joined to its source Item for `first_ingest_mode`;
the row freezes `ingest_mode`, so Item retention cannot erase live/recovery
provenance and a recovery row is not an eligible trigger. Editorial News now
also emits `news_public_update_v1` catalyst deltas and separate source
amendments. Trading never reads News private tables or a reader card, and no
News model judgment grants order authority. A source amendment updates cited
research rather than opening a new Case.

`trading_manifest_v11` freezes one `primary_trigger`, point-in-time `contexts`,
a venue-neutral `market_key`, and the policy id, version, exact typed
configuration and config digest. The serialized manifest has one market fact at
`contexts.market`; there is no alias named `market_context`. A pending Case
reconstructs its policy from that frozen snapshot, so editing thresholds affects
only later Cases. A Case frozen under an earlier manifest version is readable
but cannot advance: it is terminalized `BLOCKED / manifest_invalid` on its next
claim, and one naming a retired policy identity is `BLOCKED /
policy_identity_retired`.

The HTTP shape uses `trigger_kind`, never the retired `case_kind`. A Case
projection carries `policy_id`, `policy_version` and `policy_config_digest` read
from the frozen `manifest`, which is the copy the lane itself compares before it
decides. They are nullable for the same reason every other manifest-derived
field is: the manifest is the only writer of them, and a Case whose manifest
names no policy must render as that rather than 500 the route (#532).
All three typed market ledgers are cascade-owned by `news_items`. Migration
`20260905_0365` gave `news_market_liquidations.item_id` its first foreign key —
unique, `ON DELETE CASCADE` — and created `news_market_smart_money` with the
same one; `news_oi_signals.source_item_id` already had it, and the same revision
dropped that table's `event_id` foreign key so the OI ledger no longer depends
on an Event existing. A typed fact can no longer outlive the record it was
parsed from: the liquidation orphans that used to survive an Item purge were
unreachable evidence, and `0365` deletes them. The market Item that owns the
fact is kept for `judged_days` regardless of verdict, push or parse status.
[ADR 0002](adr/0002-trading-execution-owner-hard-cuts.md) records the retired
reader, control and replay surfaces and the older manifest generations.

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
