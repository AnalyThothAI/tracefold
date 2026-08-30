# Setup

> **Scope.** Owns install, dev-loop, and deployment commands for both the Python service and the `web/` frontend. Runtime invariants live in `OPERATIONS.md`.

## Complete operator startup

Install Git, Make, [uv](https://docs.astral.sh/uv/), Docker with the Compose
plugin, `curl`, and the [GitHub CLI](https://cli.github.com/); run
`gh auth login --hostname github.com` and start the Docker daemon. From a fresh
clone, run:

```bash
make up
```

This is the canonical startup path. It preflights Git, `uv`, Docker, Compose,
`curl`, an authenticated GitHub CLI, and daemon access; idempotently initializes
the operator directory; builds one application image containing the React console and Python service;
initializes PostgreSQL and its least-privilege roles on a fresh named volume;
migrates to the current Alembic head; starts Serve and Workers; starts the
manual executor when requested by validated config; and waits for PostgreSQL,
migration, required runtime boundaries, and an HTML console.
Any failed boundary makes the command return non-zero and directs the operator
to `make logs`.

```bash
make status            # fail closed on infrastructure/runtime readiness
make logs              # follow dependencies and all enabled runtime logs
make down              # stop containers; preserve config, passwords, and database data
```

The console is available at `http://127.0.0.1:8765/`. PostgreSQL, public HTTP,
and Workers metrics/readiness are bound to loopback by default. A second
`make up` rebuilds the shared application image and deliberately recreates only
the migration, Serve, Workers, and any requested manual-executor containers so
edits to the bind-mounted operator config take effect. An already running PostgreSQL container is not
recreated; the operator files and named-volume data remain in place.

### Upgrading across a removed config key

`NewsSettings` and the other config models are `extra="forbid"`, so a key that a
release deletes does not become inert — it fails startup. When release notes
retire a key, remove it from `~/.tracefold/config.yaml` **between** `git pull`
and `make up`, or Serve and Workers refuse to start with `extra_forbidden`.

`news.opennews_strategy_ids` is retired in #126. Which Strategies feed News is
now decided in the OpenNews account, so delete the key and its list:

```bash
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path.home() / ".tracefold" / "config.yaml"
s = p.read_text()
p.with_suffix(".yaml.bak").write_text(s)
print(p.write_text(re.sub(r"^  opennews_strategy_ids:\n(?:  - .*\n)*", "", s, flags=re.M)))
EOF
uv run tracefold config   # confirm it parses before make up
```

`trading.candidates.symbol_cooldown_seconds` and
`trading.candidates.max_rank_in_window` are retired in #348. A per-symbol
re-entry delay is what a lane needs when several positions can be open at once,
and this lane holds one at a time for at most three minutes; a rank ceiling is
selectivity, which the policy already owns. Delete either line if present:

```bash
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path.home() / ".tracefold" / "config.yaml"
s = p.read_text()
p.with_suffix(".yaml.bak").write_text(s)
p.write_text(re.sub(r"^ *(symbol_cooldown_seconds|max_rank_in_window):.*\n", "", s, flags=re.M))
EOF
uv run tracefold config   # confirm it parses before make up
```

Note the same key name lives under `news.oi.max_rank_in_window` and is **not**
retired — that one is the notification gate's rank and is unrelated to capital.
The regex above is indentation-blind, so check the diff before `make up` if your
file sets both.

`news.triage.deadline_seconds` is retired in #129. Remove that line from an
existing `news.triage` mapping before `make up`; keep `concurrency` and the
optional whole-chain `circuit_failures` / `circuit_open_seconds`. The Program
artifact now owns the primary and every fallback route deadline, so carrying the old
key fails `extra="forbid"` rather than silently overriding the artifact.

### Initialization semantics

`make up` runs `tracefold init`. The command creates `~/.tracefold/` with mode
`0700`, `logs/` and `cache/`, one config with a locally generated API bearer
token (`ws_token`) but no external credentials, six independent PostgreSQL
password files, and an empty Telegram token placeholder:

```text
telegram_bot_token
postgres_password
postgres_serve_password
postgres_workers_password
postgres_migrate_password
postgres_nautilus_password
postgres_onchain_password
```

The config, Telegram token placeholder, and all password files are mode `0600`.
Ordinary `tracefold init` never overwrites an existing config, never rotates an existing password, and
repairs the required permissions on every run. `tracefold init --force`
replaces only `config.yaml` with a newly generated default; it still preserves
all existing PostgreSQL passwords. Back up intentional config changes before
using `--force`.

`tracefold init` is the sole default-config authority. There is no maintained
static example or `.env` fallback. The generated default creates a local API
token plus an empty `telegram_bot_token` placeholder but contains no live
model, OpenNews, Feishu, or Telegram credential, points
`news.broker.url` at the compose RabbitMQ service, and leaves News push
disabled. Edit only the operator-owned
`~/.tracefold/config.yaml` to enable live capabilities. Keep secrets out of
terminal output, docs, tests, and commits.

The generated PostgreSQL DSNs are container-network addresses. The fresh-volume
bootstrap runs only during PostgreSQL `initdb`: it creates the non-login owner
plus Serve, Workers, and migrate roles, then revokes the temporary bootstrap
login before ordinary migration. It never attempts to reinterpret or hard-cut
an unknown non-empty volume. Existing deployments must already have the
least-privilege roles; startup fails closed when the migrate role or schema
contract is not valid.

### Credential-dependent capabilities

The credentials a live deployment can hold are exactly: the OpenNews token
(`news.opennews_token`), the direct model triple (`llm.api_key`,
`llm.base_url`, `llm.news_triage_model`, plus the optional
`llm.news_reader_card` triple and ordered `llm.news_fallbacks` route list), the RabbitMQ URL (`news.broker.url`), the
one push provider's configuration (`news.push.*`), the separate automatic and
manual Binance key/secret files, and the PostgreSQL role password files.

The product process is usable without optional live credentials, but affected
lanes report explicit degradation or unavailable evidence:

- absent `news.opennews_token` keeps the News Receiver idle (`ingest.connected`
  false, no incidents); a configured token is the whole News source setup —
  which Strategies push is decided in the OpenNews account (#126);
- absent or unreachable `news.broker.url` makes Workers fail startup while News
  is enabled (the broker is the News transport plane);
- an absent direct model triple (`llm.api_key`, `llm.base_url`,
  `llm.news_triage_model`) leaves the semantic Program unconfigured and makes
  Triage fall back to fail-closed rules
  (`triage_degraded_24h` grows); a degraded verdict carries no Chinese text, so
  the feed and card fall back to the original title;
- News push remains off until `news.push.enabled: true` and exactly one provider
  is complete: either a supported `news.push.feishu_webhook_url`, or a secure
  Telegram bot-token file plus one or more exact IDs in `telegram_chat_ids`.
  Private users are positive IDs; channels/groups/supergroups are negative.
  Only a private user with a matching `trading.telegram_profiles[]` row gets
  trading buttons.

`tracefold config` reports the effective file paths, configured booleans,
broker `url_configured`, model names, watchlist symbols, the selected push
provider, and credential/target configured booleans; it never prints Strategy
IDs/counts, provider tokens, Telegram channel IDs, the broker URL, webhook URLs,
signing secrets, or model keys.

`news.push.feishu_signing_secret` is optional. When present, the Adapter adds
the Feishu timestamp and signature. When absent, it sends the same compact
interactive card unsigned, without `timestamp` or `sign`; the operator owns
that reduced-authentication choice. Configuration diagnostics report only
configured booleans. Feishu delivery has no model-credential dependency; the
card header is the Triage verdict's `headline_zh` (the original title when
Triage is degraded) and the body is `why_zh` plus the code-owned facts line.
Telegram delivery reads `news.push.telegram_bot_token_file` under the same
regular-file, no-symlink, mode-`0600` policy as other provider files. The
configured `telegram_chat_ids` list may contain positive private-user IDs and
negative channel/group/supergroup IDs. Before the first send, Workers asks
Telegram for every target's exact metadata. A channel requires administrator
post authority; a group or supergroup requires bot membership; a positive
target must resolve as a private chat. Mismatched IDs fail before the first
message. Channels and groups never receive trading buttons. A positive target
receives them only when the same user ID owns an enabled Telegram Trading
Profile. Feishu and Telegram fields may not be configured
together while push is enabled. An enabled but incomplete or insecure provider
configuration makes Workers fail startup; it is not silently treated as
disabled. Serve never mounts or reads the bot token, and reports delivery
available only while Workers is running.

The Compose deployment mounts exactly the generated
`~/.tracefold/telegram_bot_token` file into Workers, so Compose deployments must
use `telegram_bot_token_file: "telegram_bot_token"`. A directly launched local
Workers process may point at a different operator-owned secure file, but that
path is not automatically mounted by Compose.

### Telegram manual trading (disabled by default)

Manual Trading requires the Telegram push configuration above. It does not
require `trading.enabled: true`; that flag owns the separate automatic lane.
Create a Binance USD-M production API key on a dedicated manual account, with
Read and Futures Trade only, and store the pair in regular non-symlink files:

```bash
uv run tracefold init
# For Telegram user 123456789, create these mode-0600 files without printing them:
# ~/.tracefold/trading_profiles/manual/123456789/binance_api_key
# ~/.tracefold/trading_profiles/manual/123456789/binance_api_secret
uv run tracefold config
```

Do not use the automatic account or its `binance_usdm_api_*` files. The manual
profile directory is mounted only into the manual executor. Configure shared
risk presets under `trading.manual`, then bind the private Telegram user ID to
that user's account under `trading.telegram_profiles`:

```yaml
trading:
  enabled: false                 # automatic lane remains independent
  manual:
    risk:
      notional_deviation_limit_bps: 5000
      tight_stop_deviation_limit_bps: 5000
      wide_stop_deviation_limit_bps: 10000
      max_account_risk_bps: 1000
      high_risk_loss_multiple_bps: 15000
      min_leverage: 1
      max_leverage: 20
    tight_stop:
      leverage: 10
      stop_loss_bps: 100
      take_profit_bps: 200
      account_risk_bps: 200
      min_notional_usd: 5
      max_notional_usd: 10
    wide_stop:
      leverage: 2
      stop_loss_bps: 2000
      take_profit_bps: 10000
      account_risk_bps: 100
      min_notional_usd: 5
      max_notional_usd: 10
  telegram_profiles:
    - user_id: 123456789
      manual:
        enabled: true
        live_trading_acknowledged: true # explicitly acknowledges real orders
        venue: binance_usdm_live
        account_ref: binance-manual-live-123456789
        api_key_file: trading_profiles/manual/123456789/binance_api_key
        api_secret_file: trading_profiles/manual/123456789/binance_api_secret
```

Run `uv run tracefold config` and require
the profile's `manual.interaction_available=true`; output remains redacted. Run
`make up`; it derives the `manual-trading` Compose profile from the validated
config and starts the isolated executor automatically. Do not export
`COMPOSE_PROFILES` yourself. The executor is locked to
`https://fapi.binance.com`; this configuration cannot select Binance Demo,
another CEX, or an on-chain signer.

Repeat the profile row for each additional private user, changing `user_id`,
`account_ref`, and every credential path to that user's directory. Never point
two rows at the same file. A profile user must also appear as a positive entry
in `news.push.telegram_chat_ids`; negative channel/group targets have no profile
and remain news-only.

### Telegram onchain routing and manual-wallet execution (disabled by default)

This is independent from Binance USD-M manual trading. `tracefold init` creates
empty lane directories but no user or credential. For Telegram user 123456789,
the quote files authenticate provider APIs and have no custody authority; the
wallet file is that user's one EVM signer shared by all routes in the profile:

```bash
# Populate the desired provider set without printing its contents:
# ~/.tracefold/trading_profiles/quotes/123456789/okx_api_key
# ~/.tracefold/trading_profiles/quotes/123456789/okx_api_secret
# ~/.tracefold/trading_profiles/quotes/123456789/okx_passphrase
# ~/.tracefold/trading_profiles/quotes/123456789/oneinch_api_key  # optional
# ~/.tracefold/trading_profiles/onchain/123456789/evm_private_key
```

Write one 32-byte EVM private key as 64 hexadecimal characters (an optional
`0x` prefix is accepted) to the profile's `evm_private_key`. Do not put that
key into any provider credential file. The wallet needs the configured
settlement token and the chain native token for gas. A mnemonic is deliberately
not accepted by this process; derive the dedicated manual-account private key in
the wallet and keep the seed phrase out of the application host.

Add the following under the existing `trading` block. The generated default
config already contains the complete five-chain settlement-asset list; keep
those exact chain/CA/decimal values unless you intentionally change the input
asset that every provider must quote. Live execution accepts only this
code-owned list because the database uses it to verify the quote amount and the
development-test ceiling. Supporting another settlement asset therefore
requires an explicit application release and schema migration; a local config
change alone remains analysis-only.

```yaml
trading:
  enabled: false                 # automatic lane remains independent
  onchain:
    slippage_bps: 100
    discovery_chain_ids: [1, 56, 8453, 42161, 4663]
    settlement_assets:
      - chain_id: 1
        chain_name: Ethereum
        symbol: USDC
        contract_address: "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        decimals: 6
        quote_amount: 10
        rpc_url: "https://YOUR_ETHEREUM_RPC"
      - chain_id: 56
        chain_name: BNB Chain
        symbol: USDT
        contract_address: "0x55d398326f99059ff775485246999027b3197955"
        decimals: 18
        quote_amount: 10
        rpc_url: "https://YOUR_BNB_RPC"
      - chain_id: 8453
        chain_name: Base
        symbol: USDC
        contract_address: "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        decimals: 6
        quote_amount: 10
        rpc_url: "https://YOUR_BASE_RPC"
      - chain_id: 42161
        chain_name: Arbitrum One
        symbol: USDC
        contract_address: "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
        decimals: 6
        quote_amount: 10
        rpc_url: "https://YOUR_ARBITRUM_RPC"
      - chain_id: 4663
        chain_name: Robinhood Chain
        symbol: USDG
        contract_address: "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
        decimals: 6
        quote_amount: 10
        rpc_url: "https://YOUR_ROBINHOOD_CHAIN_RPC"
  telegram_profiles:
    - user_id: 123456789
      onchain:
        enabled: true
        wallet:
          address: "0xYOUR_DEDICATED_MANUAL_EVM_WALLET"
          private_key_file: trading_profiles/onchain/123456789/evm_private_key
          live_trading_acknowledged: true
        providers:
          okx:
            enabled: true
            api_key_file: trading_profiles/quotes/123456789/okx_api_key
            api_secret_file: trading_profiles/quotes/123456789/okx_api_secret
            passphrase_file: trading_profiles/quotes/123456789/okx_passphrase
          oneinch:
            enabled: false
            api_key_file:
          binance:
            enabled: true         # public Alpha CA evidence; route quote remains unavailable
```

Run `uv run tracefold config` and require
the profile's `onchain.interaction_available=true`, then run `make up`. A news card
shows `链上路由`; only its rendered `🎯 标的` values enter resolution. Each value
may be a ticker or an exact EVM CA and need not carry a chain. Multiple targets
require a target choice, and ambiguous token names require an explicit chain/CA
choice. OKX, 1inch, Binance Alpha, and a DEX Screener discovery observation are
merged; the last source is accepted only after `symbol/name/decimals` are read
from the candidate contract on the reported chain. The result is labeled
`确定最佳` only with complete cost and safety evidence; otherwise it is visibly
`暂定最佳`.

Live signing is available for an OKX or 1inch winner when the profile wallet,
trusted execution RPC, acknowledgment, and dedicated executor heartbeat are
all current. The 1inch Router V6 verifier binds its router, source/destination
contracts, exact input, wallet recipient, minimum output, and no-partial-fill
flag. The OKX verifier independently binds the code-owned per-chain router and
approval proxy, exact approval, supported Router V1 selector, token pair,
amount, wallet recipient, minimum output, and deadline. Both routes require a
trusted-RPC simulation before local signing. Binance currently contributes public
Alpha CA evidence only because its general Web3 Swap execution contract is not
publicly documented. None of these provider states creates a second wallet.
For `/test_onchain`, `quote_amount` is rejected before confirmation and again by
the durable intent and PostgreSQL constraint when it exceeds 200
USDT-equivalent settlement units.

To send an expiring, visibly marked fixture, open a direct private conversation
with the bot from a configured profile user and send one of:

```text
/test_futures
/test_onchain
```

The defaults exercise HYPE and the multi-target BLUECHIP/COPPERINU choice.
`/test_futures` creates the same confirmation and live-execution flow as a
delivered News card, but its selected notional is hard-capped at 200 USDT in
both the domain contract and PostgreSQL. The configured presets may impose a
lower ceiling. The private bot command menu also exposes:

```text
/start       # help and command keyboard
/positions   # current manual positions
/history     # closed manual positions
/trades      # append-only interaction/order/position ledger
```

The bot accepts neither command from a group/channel nor from an unconfigured
private user. It replies only to the requesting private chat, selects only that
user's Telegram Trading Profile, and stores no News material fact. Fixture rows
expire after two hours.

An operator configuration for live News uses the existing generated fields and
the documented secure token file; do not add another config source or
environment variable:

```yaml
llm:
  api_key: "<operator model secret>"
  base_url: "https://api.deepseek.com/v1"
  news_triage_model: "deepseek-v4-flash"
  # Optional provider-neutral request controls. Omit to use known-provider defaults.
  request:
    send_temperature: true
    temperature: 0
    structured_output: "json_object"
    extra_body: {}
  # Optional: omit this complete triple to run ReaderCard on the Triage endpoint.
  news_reader_card:
    api_key: "<reader model secret>"
    base_url: "https://reader.example/v1"
    model: "reader-model"
    request:
      send_temperature: false
      structured_output: "prompt_json"
      extra_body: {}
  # Optional ordered fallback routes, tried top to bottom; at most three.
  news_fallbacks:
    - api_key: "<event fallback secret>"
      base_url: "https://event-fallback.example/v1"
      model: "event-fallback-model"
      # Optional; omit to alias ReaderCard to this route's EventSemantics endpoint.
      reader_card:
        api_key: "<reader fallback secret>"
        base_url: "https://reader-fallback.example/v1"
        model: "reader-fallback-model"
  # Required only for `news learning run` / `optimize`. Reflection uses this endpoint with
  # code-owned 32k/300s/temperature-1; metric_judge derives a distinct sealed
  # role from it with its own schema, budget, tariff and accounted calls.
  news_compiler_reflection:
    api_key: "<compiler reflection secret>"
    base_url: "https://reflection.example/v1"
    model: "reflection-model"

news:
  enabled: true
  # Which Strategies feed the pipeline is set in the OpenNews account, not here.
  opennews_token: "<operator secret>"
  broker:
    url: "amqp://tracefold:<rabbitmq password>@rabbitmq:5672/"
  push:
    enabled: true
    telegram_bot_token_file: "telegram_bot_token"
    # Private user, channel and group targets. Only a positive user with a
    # matching trading.telegram_profiles row receives trading buttons.
    telegram_chat_ids: [123456789, -1001234567890, -1009876543210]
    # Alternative provider (do not configure both):
    # feishu_webhook_url: "<Feishu v2 webhook>"
    # feishu_signing_secret:
  policy:                     # policy-v10 duplicate/safety knobs (all optional; these are the defaults)
    restatement_drop: true      # a restatement of a card the reader already received never pushes
    similarity_max: 0.25        # ordinary pushes above this sent-ledger similarity are same-fact duplicates
    listing_exempt_from_duplicate: true  # exchange listing frames are duplicates only per instrument
    stale_source_max_age_s: 43200  # an x/twitter artifact already older than 12 h on arrival is a replay
  retention:
    raw_days: 30                # an Item nobody judged is storage
    judged_days: 365            # an Item behind a verdict or accepted review is retained as learning evidence
  gate:
    suppress_low_signal: false  # true = drop ungrounded, non-macro social posts under score 70 before Program execution
  venues:                       # instrument-universe snapshot; public catalogues, no credentials
    enabled: true
    binance: true
    hyperliquid: true
    okx: true
    lighter: true                 # post-send exact market lookup and price anchors
    bitget: true                  # post-send exact market lookup and price anchors
    us_reference: true          # US listed-symbol directory (#91): tells the Gate a ticker is a stock, not tradeable here
    snapshot_period_hours: 6.0
  watchlist:
    - {symbol: BTC}
    - {symbol: ETH}
    - {symbol: SOL}
    - {symbol: NVDA}
    - {symbol: TSLA}
    - {symbol: COIN}
```

Use `prompt_json` for an OpenAI-compatible local/provider endpoint that rejects
`response_format` but can follow an in-prompt JSON Schema. Set
`send_temperature: false` when it rejects the temperature field. These controls
are available on every endpoint block; they replace model-name or URL-specific
compatibility hacks. In `auto`, MiniMax M3 uses temperature 1, `top_p: 0.95`,
thinking disabled, and prompt-only JSON; DeepSeek uses JSON-object mode. Explicit
operator values take precedence. These request semantics enter
`configured_endpoint_model_v3`, so two endpoints with different request contracts
cannot reuse the same evidence cohort.

`news.gate` controls admission and `news.policy` exposes only four duplicate/
safety knobs; trade-relevance action eligibility is code-owned. The
Gate admits nearly every Item (only recovery replays, law-firm templates,
and — behind `suppress_low_signal` — low-score ungrounded social posts skip
Program execution; exchange listing/delisting frames are admitted and judged like any
candidate), Triage is the semantic filter, and
`decide()` applies policy v10 to one `ScoredJudgment`. Semantic generation is
the code-owned `EventSemantics.v2 -> deterministic SemanticNormalizer ->
ReaderCard.v2 -> deterministic assembler` Program; `TradeRelevanceV1` is nested
inside EventSemantics.v2. It remains behind
`SemanticJudge.judge(TriageContext)`. A normal judgment makes two serial
provider calls; the Program factory owns the route deadline and retry/call
budget in code, so `deadline_seconds` is not an operator setting.
The model-visible projection excludes queue priority, provider score, Gate
macro lexicon, queue lag and watchlist; ReaderCard receives only its reduced
semantic view and never ToldContext or reader intent. Queue priority remains a
broker scheduling/audit fact and is absent from reader HTTP/OpenAPI/React.

A change is one candidate kind — a bounded two-instruction Prompt patch:
record accepted cases with `tracefold news review`, freeze development and
future validation windows with `tracefold news learning freeze`, then run the
offline, holdout, shadow and canary gates under `tracefold news learning`.
The optional GEPA workflow reads the frozen development corpus once, runs
bounded GEPA with no database write, broker, delivery, canary or promotion
credential, and emits at most a typed patch carrying the two Predictor
instructions. It requires explicit metric/task/reflection/metric-judge call
limits, a total and a per-call cost limit and a seed; it cannot register,
accept, deploy or promote. `tracefold news learning run` is the recommended
entry: it runs readiness, the standalone baseline and the one optimization over
that corpus, composed in one process over one dataset SHA and one configured
judge route, into one directory. Migration
`0292` records the initial `program_v1`
epoch; migration `0293` preserves it and starts the corrected `program_v2`
epoch; migration `0294` preserves both prior rows and starts the expert-quality
`program_v3` epoch; migration `0295` preserves v1-v3 and starts `program_v4`;
migration `0298` preserves v1-v4 and starts `program_v5`; migration `0301`
preserves history and starts `program_v6` for
factory/executable v4, policy v10, review/metric v4 and compiler protocol v3.
Migration `0303` preserves history and starts `program_v7` for
factory/executable v5 after the Program/Learning package split. Every earlier
cohort remains audit-only, and quality evidence restarts from zero at the
`program_v7` deployment. The hard cut itself does not prove a cross-generation
quality uplift; v7 evidence starts from zero
and the normal graph remains exactly two serial Predictor calls.
Migration `0304` carries the #193 strategy-artifact cut: it trips every open
canary activation and receipts itself, but does not re-open the epoch, so
accepted `news_review_v4` evidence stays eligible. Migration `0305` carries the
same issue's compile-record cut: it admits the `compile_record` learning
artifact kind, keeps `compile_receipt` readable as audit history, and trips
open activations again, because a candidate registered against the retired
receipt chain can no longer be evaluated. It does not re-open the epoch either.
Migration `0315` carries #288's exact source route and factory-v7 cut. It trips
open activations and records the cut without rewriting or appending the
`program_v7` epoch row. Accepted review labels remain immutable truth, but
prior-factory judgments are audit-only under exact current-bundle eligibility,
so the factory-v7 cohort starts at zero.
The production image has one loader only: the content-addressed
`news_program_strategy_artifact_v1` document executed as
`news_semantic_program_v8` under `news_triage_policy_v11`. Prior roots remain
immutable audit history and are not executable by the current image. Rollback
uses the recorded previous same-schema runtime image, never an alternate
registry entry or runtime switch.
`tracefold config` prints the effective values. Policy v11 retains policy v7's
removal of every 1 h/2 h/4 h reader-count veto: every distinct fact that passes the semantic contract moves
to delivery; the sent-reader ledger remains only for same-fact suppression.

Leave the signing field empty only when unsigned delivery is intentional. Do
not commit the populated operator config. With `news.push.enabled: false`,
Serve and Workers start without a provider and any delivery work settles
`terminal/delivery_unavailable`. Once push is explicitly enabled, an incomplete
or invalid provider configuration is a startup error for Workers; the requested
delivery boundary is never silently discarded.

The compose stack runs `rabbitmq:4-management` with the default user
`tracefold` and password `${TRACEFOLD_RABBITMQ_PASSWORD:-tracefold}`; ports
5672/15672 bind to `127.0.0.1`. The broker URL in `config.yaml` must match.
Setting `news.enabled: false` leaves Workers with only the probe and control
children and needs no RabbitMQ.

There is no local allowlist to keep in step (#126): enabling a Strategy in the
OpenNews dashboard starts feeding the pipeline, disabling it stops, and
`/api/news/status` reports nothing about Strategies because Tracefold neither
chooses nor filters them.

Worker topology and all safety/resource budgets are code-owned. For real data,
`config.yaml` must contain only the News credentials above; the `llm` block
owns one all-or-none direct Triage triple (`api_key`, `base_url`,
`news_triage_model`) and may own one all-or-none `news_reader_card` endpoint;
an absent Reader endpoint inherits Triage. `news_fallbacks` declares at most
three complete routes in execution order. Each item is an all-or-none EventSemantics
endpoint and may carry an all-or-none nested `reader_card`; an absent nested Reader
is an explicit alias of that same fallback endpoint. There is no environment-variable
credential path or inferred URL/model. Configs written before the GMGN lane removal must drop the
`gmgn`, `upstream`, `providers.binance`, `api.heartbeat_interval`, and
`api.replay_limit` keys, and configs written before the Analyst lane removal
(#57) must drop `news.analyst.*` and `llm.news_analyst_model`; the schema
rejects them. Before #160, also remove the retired policy-v9 action/priority
keys (`escalate_magnitude`, `min_push_magnitude`,
`min_watchlist_magnitude`, `unclear_push_min_magnitude`,
`unclear_push_event_types`, `high_priority_escalates`,
`noise_veto_max_magnitude`, `noise_veto_respects_gate_priority`, and
`contested_push_min_magnitude`) and run `uv run tracefold config`; there are no
aliases.

The OpenNews Receiver authenticates one WSS and sends zero application
subscription frames; the server pushes the account owner's `strategy.triggered`
notifications and Tracefold publishes each accepted frame to RabbitMQ. A
disconnect, broker backpressure, or process outage creates a typed incident;
reconnect restores current WSS health and the official Strategy list/hits
endpoints perform bounded idempotent recovery (recovered Items never deliver).
Deduper, Triage, and Deliverer are broker consumers; see `docs/ARCHITECTURE.md`
and `docs/OPERATIONS.md` for the pipeline and diagnosis.

Use `uv run tracefold config` to inspect the active config path and redacted
enablement. Inspect serve through authenticated `/api/status` and workers
through its internal health/readiness/metrics surface.

Useful live-data smoke checks:

```bash
uv run tracefold config
uv run tracefold news bus-check
uv run tracefold db audit
```

The first command confirms the real config paths. `news bus-check` proves the
broker URL, declares the News topology idempotently, and prints per-queue
message/consumer counts. `db audit` confirms the migration head, every current
News table count, and that the schema holds exactly the declared table set. Source
blocks, rate limits, and missing rows surface as explicit diagnostic results,
not as fake facts.

Live-data debugging starts the same way: first run `uv run tracefold config`
and confirm `config_path` points at `~/.tracefold/config.yaml`. Report only
paths, booleans, and diagnostic command status; do not paste the API token,
model keys, provider passwords, or full config payloads into docs or chat.

The Alembic chain starts at the `20260818_0275` current-schema baseline and is
linear through `20260823_0299_news_source_artifact_id`. A new empty database applies
the complete chain without replaying retired runtime tables. A database
stamped at an earlier revision migrates forward with `tracefold db migrate`;
all revisions are irreversible (see `OPERATIONS.md`). Stop Serve and Workers
before applying them and start Workers only after the migration is current.
An existing 0283 volume uses the backup/stop/migrate/redeploy sequence
documented in `OPERATIONS.md`; #112/#129 add no login role or password.

Retired routes return `404`; there is no compatibility alias.

The full CLI surface is documented by `uv run tracefold --help`.
Treat that output as the source of truth — do not enumerate commands
here. A snapshot lives at `generated/cli-help.md`.

## Container deployment

`make up`, `make status`, `make logs`, and `make down`
are the supported operator lifecycle. `make up` passes an existing
`GITHUB_TOKEN` into the image
build as a BuildKit secret; when unset, it uses `gh auth token` if available.
Public dependencies need neither. The token is not stored in an image layer or
application config.

Compose bind-mounts only role-appropriate files from `~/.tracefold/`. Serve
receives only its SELECT credential; Workers receives only its DML credential;
the migrate credential is absent from both steady containers. PostgreSQL data
is pinned to the `tracefold-postgres` named volume, and `make down` does not
delete it.

Fresh-volume bootstrap is an `initdb` hook, not a steady service or a generic
role-repair mechanism. Normal startup consists of PostgreSQL, RabbitMQ
(`rabbitmq:4-management`, data on the `tracefold-rabbitmq` volume, AMQP and
management ports bound to `127.0.0.1`), the one-shot migration service, and
separate Serve/Workers runtimes; Workers waits for the broker health check. `make status` returns
non-zero for a failed/missing migration, stopped or unhealthy required
container, failed Serve or Workers readiness endpoint, or missing HTML console.
It intentionally does not make business-data freshness part of readiness. Use
`make logs` for the bounded startup evidence named by a failure.

The preflight verifies `uv`, the Docker CLI, Compose plugin, `curl`, an
authenticated GitHub CLI, and daemon access before a build starts. GitHub is
used to bind deployment to the exact green `origin/main` commit. If the daemon
is unavailable, start Docker Desktop or grant this shell access to the Docker
socket, then rerun `make up`.

`make deploy-image IMAGE_ID=sha256:<64 lowercase hex>` is the narrow
database-compatible image rollback/redeployment path. Run it only from the
primary checkout on `main`, and pass the full ID of an image already present in
the local Docker image store; tags, short IDs, registry digest references, and
an `IMAGE_ID` inherited only from the environment are refused. The checkout
must have no tracked/staged changes, must equal local `origin/main`, and must
have no `.env`, untracked Compose override, or untracked Alembic revision; an
unrelated untracked research artifact is not a deployment input. The target
inspects the local ID and requires the image, source, and live database Alembic
heads to match (therefore rejecting every image on a different schema head
mismatch), then validates that the target can parse the active config without
printing it. It injects that exact ID as `TRACEFOLD_IMAGE_DIGEST`, stops Serve
and Workers, and recreates migration, Serve, and Workers with `--no-build`.
Before running `make status`, it verifies every recreated container image,
Workers readiness identity, and the linked active/runtime-deployment receipt.
It never downgrades PostgreSQL. See `OPERATIONS.md` for the receipt lookup and
rollback runbook. Normal `make up` still builds and deploys the current checkout
and ignores this exact-image override.

The official PostgreSQL 18 Bookworm image preloads `pg_stat_statements` with
query IDs enabled. Use `tracefold db health`, supported audit/query-audit and
status/metrics surfaces, the SQL in `OPERATIONS.md`, and `docker compose logs`
for diagnosis. Compose has no custom PostgreSQL build, auxiliary observability
services, host log mount, or HTML-report path.

## Explicit development loops

The container workflow is the fresh-clone onboarding path. It is not equivalent
to starting only `tracefold serve`: the complete product also requires a
current PostgreSQL schema, one Workers runtime, and a built or proxied console.

For frontend-only development, keep the complete stack running and start Vite
against its loopback API:

```bash
make up
cd web
npm ci
npm run dev          # Vite console with API proxy to 127.0.0.1:8765
```

For an intentional host-process backend loop, first provision PostgreSQL roles
and set the four role DSNs in `~/.tracefold/config.yaml` to a database reachable
from the host. This is for development against an already prepared database;
it does not bootstrap a blank cluster. Then use separate terminals:

```bash
# one-time dependency/schema preparation
make sync
cd web && npm ci && cd ..
uv run tracefold db migrate

# terminal 1
uv run tracefold serve

# terminal 2
uv run tracefold workers

# terminal 3
cd web && npm run dev
```

Developer checks remain separate from startup:

```bash
make install-hooks
uv run pytest
uv run ruff check .
uv run python -m compileall tracefold tests
cd web && npm run typecheck && npm run lint
```

`make install-hooks` uses Git's standard repository hook directory. If any
`core.hooksPath` override is active, it prints the single command that clears
that override instead of adding custom-path installation logic. After a
successful install it verifies that the executable hook belongs to this
repository's Git common directory. The hooks reuse the locked
Ruff toolchain and run ESLint/Prettier only on staged frontend files. They are
fast local feedback, not merge or release evidence.

Other frontend commands are:

```bash
cd web
npm run build        # production bundle
npm run preview      # serve the build locally
```

See `FRONTEND.md` for architecture and component conventions.
