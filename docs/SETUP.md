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
migrates to the current Alembic head; starts Serve and Workers; and waits for
PostgreSQL, migration, both runtime readiness boundaries, and an HTML console.
Any failed boundary makes the command return non-zero and directs the operator
to `make logs`.

```bash
make status            # fail closed on infrastructure/runtime readiness
make logs              # follow PostgreSQL, migration, Serve, and Workers logs
make down              # stop containers; preserve config, passwords, and database data
```

The console is available at `http://127.0.0.1:8765/`. PostgreSQL, public HTTP,
and Workers metrics/readiness are bound to loopback by default. A second
`make up` rebuilds the shared application image and deliberately recreates only
the migration, Serve, and Workers containers so edits to the bind-mounted
operator config take effect. An already running PostgreSQL container is not
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

`news.triage.deadline_seconds` is retired in #129. Remove that line from an
existing `news.triage` mapping before `make up`; keep `concurrency` and the
optional whole-chain `circuit_failures` / `circuit_open_seconds`. The Program
artifact now owns its primary and fallback route deadline, so carrying the old
key fails `extra="forbid"` rather than silently overriding the artifact.

### Initialization semantics

`make up` runs `tracefold init`. The command creates `~/.tracefold/` with mode
`0700`, `logs/` and `cache/`, one config with a locally generated API bearer
token (`ws_token`) but no external credentials, five independent PostgreSQL
password files, and an empty Telegram token placeholder:

```text
telegram_bot_token
postgres_password
postgres_serve_password
postgres_workers_password
postgres_migrate_password
postgres_nautilus_password
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
`llm.news_reader_card`, `llm.news_triage_fallback`, and
`llm.news_reader_card_fallback` triples), the RabbitMQ URL (`news.broker.url`), the
one push provider's configuration (`news.push.*`), and the PostgreSQL role
password files.

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
  Telegram bot-token file plus one private channel ID (`-100...`).

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
configured `telegram_chat_id` must be a private channel Bot API ID beginning
with `-100`. Before the first send, Workers asks Telegram for the target
metadata, verifies the exact ID is a channel without a public username, and
verifies the bot is an administrator allowed to post. Invite links, public
channels, personal chats, groups, and supergroups are rejected before the first
message. Feishu and Telegram fields may not be configured together while push
is enabled. An enabled but incomplete or insecure provider configuration makes
Workers fail startup; it is not silently treated as disabled. Serve never mounts
or reads the bot token, and reports delivery available only while Workers is
running.

The Compose deployment mounts exactly the generated
`~/.tracefold/telegram_bot_token` file into Workers, so Compose deployments must
use `telegram_bot_token_file: "telegram_bot_token"`. A directly launched local
Workers process may point at a different operator-owned secure file, but that
path is not automatically mounted by Compose.

An operator configuration for live News uses the existing generated fields and
the documented secure token file; do not add another config source or
environment variable:

```yaml
llm:
  api_key: "<operator model secret>"
  base_url: "https://api.deepseek.com/v1"
  news_triage_model: "deepseek-v4-flash"
  # Optional: omit this complete triple to run ReaderCard on the Triage endpoint.
  news_reader_card:
    api_key: "<reader model secret>"
    base_url: "https://reader.example/v1"
    model: "reader-model"
  # Optional all-or-none fallback route.
  news_triage_fallback:
    api_key: "<event fallback secret>"
    base_url: "https://event-fallback.example/v1"
    model: "event-fallback-model"
  # Optional: requires news_triage_fallback; omit to alias its endpoint explicitly.
  news_reader_card_fallback:
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
    telegram_chat_id: -1001234567890
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

When the News endpoints use exactly `https://api.kimi.com/coding/v1` (HTTPS,
standard port, no URL credentials, query or fragment), the
code-owned event profile binds `k3`/`k3-256k` to low reasoning and removes the
explicit temperature that the Coding endpoint rejects. The reader profile also
removes that unsupported temperature but does not apply a K3 effort override to
`kimi-for-coding`. These request semantics are part of
`configured_endpoint_model_v2`, so changing them changes the runtime-model
binding and cannot reuse evidence from a different effort/profile.

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
bounded DSPy GEPA with no database write, broker, delivery, canary or promotion
credential, and emits at most a typed patch carrying the two advisory
instructions. It requires explicit metric/task/reflection/metric-judge call
limits, a total and a per-call cost limit and a seed; it cannot register,
accept, deploy or promote. `tracefold news learning run` is the recommended
entry: it runs readiness, the standalone baseline and the one optimization over
that corpus and writes `run_summary.json`, which keeps the standalone, GEPA-seed
and future-test baselines apart instead of leaving three different numbers to be
quoted as one. Migration
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
The production image has one loader only: the
`news_program_strategy_artifact_v1` document, factory v7, executable v5,
epoch v7 and policy v10. Issue #175 reissues that sole stable Program and bundle
inside v7; Issue #190 reissues it again for fail-closed NaN/Infinity canonical
identity; Issue #193 reissues it a third time as the one-document artifact whose
root is the hash of the factory id plus the two instructions; Issue #288
reissues it under factory v7 for the exact source-contract route. Rollback uses
the recorded previous same-schema runtime image, never an alternate registry
entry or runtime switch.
`tracefold config` prints the effective values. Policy v10 retains policy v7's
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
an absent Reader endpoint inherits Triage. The optional fallback route has an
all-or-none `news_triage_fallback` endpoint and may add an all-or-none
`news_reader_card_fallback`; absent Reader fallback is an explicit alias of the
EventSemantics fallback endpoint. There is no environment-variable
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
uv run python -m compileall src tests
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
