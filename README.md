# Tracefold

Tracefold is an evidence-first market research and bounded trading system with
two sibling business capabilities. News V3 turns the operator's OpenNews Strategy pushes
into deduplicated Events, triages them with bounded structured model calls
under deterministic rules, and delivers reader cards through Feishu or configured
Telegram private, group, and channel destinations. One Python service
persists material facts in PostgreSQL, builds a deterministic read model, and
serves a React operator console plus stable HTTP and CLI contracts.

Trading consumes only persisted public News projections or an explicit
Telegram operator confirmation and is disabled by default. Provider frames,
Telegram callbacks and venue responses are inputs, not alternate business
truth.

## Architecture

```text
OpenNews WSS
  -> integrations (RabbitMQ is the News transport plane)
  -> PostgreSQL material facts
  -> single-writer read models
  -> HTTP / CLI / React
```

The hard invariants are:

- PostgreSQL material facts are the only business truth.
- Current rows use stable product/window/target keys, never run or attempt IDs.
- Each current read model has exactly one writer and is rebuildable from facts.
- Unchanged projections write zero serving rows.
- News consumers recover by re-consuming durable broker queues plus database
  idempotency keys.
- Read surfaces never call providers or models.
- Missing evidence is explicit, never replaced by a fabricated zero or fallback.

The Python package is deliberately shallow:

```text
tracefold/
  news/           broker-driven Event pipeline: Deduper, Gate, Triage, delivery, labels
  trading/        automatic and Telegram-manual capital contracts and ledgers
  integrations/   provider and external-system adapters (OpenNews, RabbitMQ, Feishu, Telegram)
  platform/       config, PostgreSQL/Alembic, telemetry, bounded resource primitives
  app/            composition (`tracefold.app.workers` root), HTTP, and CLI adapters
```

Other packages import business contracts from `tracefold.news` or
`tracefold.trading`, not from their internal modules. See
[Architecture](docs/ARCHITECTURE.md).

## Start the complete product

Prerequisites are Git, Make, [uv](https://docs.astral.sh/uv/), a running
Docker daemon, the Docker Compose plugin, and `curl`. On macOS, start Docker
Desktop before continuing. From a fresh clone, the complete operator path is
one command:

```bash
make up
```

`make up` preflights the prerequisites, initializes the operator files without
overwriting existing choices, builds one application image containing the
React console and Python service, bootstraps least-privilege PostgreSQL roles
on a fresh volume, migrates to the current schema, starts Serve and Workers,
starts each trading executor requested by validated config, and waits for
all required runtimes plus the HTML console. It exits non-zero and points
to `make logs` if any required boundary is not ready.

Open `http://127.0.0.1:8765/` after it succeeds. The lifecycle is deliberately
small:

```bash
make status  # fail closed unless DB, migration, enabled runtimes, and console are ready
make logs    # follow service logs; Ctrl-C leaves the services running
make down    # stop containers without deleting PostgreSQL data
```

A second `make up` rebuilds that application image and recreates migration,
Serve, Workers, and each explicitly enabled execution process so configuration changes take effect.
An already running PostgreSQL container is not recreated, and its named volume,
operator configuration, and role passwords are preserved. The generated
defaults contain no live OpenNews, model, webhook, or bot credential (the
Telegram token file is an empty placeholder), and News push is disabled. The
product still starts; credential-dependent capabilities report
an explicit degraded or unavailable state instead of fabricating data. Add
structured settings to `~/.tracefold/config.yaml`; place the Telegram bot token
only in `~/.tracefold/telegram_bot_token`, then rerun `make up`.
If push is explicitly enabled with an incomplete or insecure provider
configuration, Workers fails startup instead of silently discarding requested
deliveries.

The operator-owned runtime directory is:

```text
~/.tracefold/config.yaml                 # 0600
~/.tracefold/telegram_bot_token          # optional Telegram secret; 0600
~/.tracefold/trading_profiles/manual/<telegram-user-id>/binance_api_key       # per-user key; 0600
~/.tracefold/trading_profiles/manual/<telegram-user-id>/binance_api_secret    # per-user secret; 0600
~/.tracefold/trading_profiles/onchain/<telegram-user-id>/evm_private_key      # per-user signer; 0600
~/.tracefold/trading_profiles/quotes/<telegram-user-id>/okx_api_key           # per-user route key; 0600
~/.tracefold/trading_profiles/quotes/<telegram-user-id>/okx_api_secret        # per-user route secret; 0600
~/.tracefold/trading_profiles/quotes/<telegram-user-id>/okx_passphrase        # per-user passphrase; 0600
~/.tracefold/trading_profiles/quotes/<telegram-user-id>/oneinch_api_key       # optional per-user route key; 0600
~/.tracefold/binance_usdm_api_key           # optional automatic USD-M key; 0600
~/.tracefold/binance_usdm_api_secret        # optional automatic USD-M secret; 0600
~/.tracefold/postgres_password           # fresh-volume bootstrap only; 0600
~/.tracefold/postgres_serve_password     # read-only runtime; 0600
~/.tracefold/postgres_workers_password   # writer runtime; 0600
~/.tracefold/postgres_migrate_password   # migration runtime; 0600
~/.tracefold/postgres_nautilus_password  # execution runtime; 0600
~/.tracefold/postgres_onchain_password   # isolated onchain execution runtime; 0600
~/.tracefold/logs/
~/.tracefold/cache/
```

Each private Telegram user must point its `trading.telegram_profiles[]` row at
its own secret files; account references, wallet addresses, and secret paths
cannot be shared across profiles. The directory itself is mode `0700`. `tracefold init` is the sole default
configuration generator; repository fixtures and `.env` files are not runtime
truth. Confirm the active path and redacted credential booleans with:

```bash
uv run tracefold config
uv run tracefold news bus-check   # broker reachable, topology declared, queue depths
uv run tracefold db audit         # migration head, news_* row counts, exact news_* tables
uv run tracefold --help
```

Exact HTTP fields come from [OpenAPI](docs/generated/openapi.json). The
complete CLI snapshot is [cli-help.md](docs/generated/cli-help.md). Detailed
installation, credential, initialization, and development-loop instructions
are in [Setup](docs/SETUP.md).

## Development

GitHub Issues are the durable specification and acceptance surface. Tests are
selected by the changed seam; `make check` is a useful fast bundle, not a
universal completion gate.

```bash
make check
make test            # default regression (~4 min): unit + architecture + contract + integration, no slow/e2e/golden
make test-slow       # real-process Workers runtime tests
make test-all        # everything (~6.5 min)
cd web && npm run lint && npm run typecheck
```

The maintained documentation surface is intentionally small:

| Need | Source |
|---|---|
| Install and deployment | [docs/SETUP.md](docs/SETUP.md) |
| Data and module architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Public config/API/CLI contracts | [docs/CONTRACTS.md](docs/CONTRACTS.md) |
| Operations and PostgreSQL diagnosis | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Design and testing | [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| Frontend boundaries | [docs/FRONTEND.md](docs/FRONTEND.md) |
| Secrets and authentication | [docs/SECURITY.md](docs/SECURITY.md) |

Generated artifacts live in `docs/generated/` and always have checked-in
generators. Historical design records and implementation-detail test archives
do not live in the repository.

## Non-goals

- no unconfirmed, unbounded, or cross-user trade execution;
- no compatibility aliases for retired names or paths;
- no provider response, queue, process cache, or projection as alternate truth;
- no hidden provider calls or mutations in read APIs;
- no repository-local live credentials.
