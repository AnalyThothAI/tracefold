# Tracefold

Tracefold is an evidence-first market research and trading system. **News** turns
provider input into durable editorial and market facts, classifications, reader
cards, and wallet net-buy episodes. **Trading** consumes a public OI projection,
freezes Cases, and produces engine-neutral Signals. The optional Nautilus Runtime
owns execution separately. A React console and HTTP/CLI surfaces expose persisted
facts, decisions, and outcomes.

News and Trading are sibling capabilities, not one another's implementation layer.
Provider frames, model predictions, submitted commands, and UI state are not substitutes
for durable evidence or actual delivery/execution outcomes.

## Architecture

```text
OpenNews -> RabbitMQ -> News admission -> PostgreSQL
                         |-- editorial Events -> Program -> decision -> delivery
                         `-- market facts -> notifications
                                          `-> App OI mapper -> Trading Case / Signal

Wallet roster + chain receipts -> fill ledger -> net-buy episodes -> notification
                                                `-> independent price samples

Signal + authenticated control -> separate Nautilus Runtime -> venue / reconciliation

PostgreSQL projections -> Serve -> HTTP / React
                      `-> read-only CLI commands
```

```text
tracefold/
  news/           editorial pipeline, market facts, wallet episodes, learning/release
  trading/        OI admission, frozen Cases, Alpha, Signals, execution transport
  integrations/   provider, broker, delivery, market, and Nautilus/Binance adapters
  platform/       configuration, PostgreSQL/Alembic, telemetry, bounded resources
  app/            Serve/Workers/Runtime composition, HTTP, CLI, cross-context mapping
```

Serve and Workers share the application image. The account-owning execution Runtime
has a separate image and lifecycle. Neither business package imports the other or
reads its tables; App performs the explicit handoff. See [Architecture](docs/ARCHITECTURE.md)
for the implemented boundaries and current data flow.

## Start the application

Prerequisites: Git, Make, [uv](https://docs.astral.sh/uv/), Docker with the Compose
plugin, and `curl`. On macOS, start Docker Desktop first. From the checkout:

```bash
make up
```

This initializes operator files without overwriting existing choices, builds the
application image containing Python and the React console, starts PostgreSQL and
RabbitMQ, applies broker policy and migrations, and starts Serve and Workers.
A failed required startup boundary returns non-zero and can be inspected with logs.
Open `http://127.0.0.1:8765/` after successful startup.

```bash
make status  # inspect required application and configured execution readiness
make logs    # follow logs; Ctrl-C does not stop the services
make down    # stop application containers without deleting PostgreSQL data
```

A subsequent `make up` rebuilds the application and recreates the application roles,
not a running PostgreSQL container. Data volumes and operator configuration persist.
Generated defaults contain no live provider/model/delivery credentials; optional
capabilities report disabled, unavailable, or degraded states rather than fake data.
Add operator settings to `~/.tracefold/config.yaml` and rerun `make up` as appropriate.
An explicitly enabled but invalid delivery configuration is not a successful setup.

### Optional execution Runtime

Execution is disabled by default. Its independent lifecycle is:

```bash
make runtime-build
make runtime-up
make runtime-status
make runtime-logs
make runtime-restart
make runtime-down
```

These are lifecycle commands, not a recommendation to enable live trading.
`make up` does not restart the execution Runtime; `make down` stops it first.
Use the [Operations](docs/OPERATIONS.md) and [Security](docs/SECURITY.md) procedures
for the configured Binance connection and any authorized activation or cutover.
The current Signal lane is OI-based; arbitrary editorial explanations do not
implicitly become implemented trading strategies.

### Operator configuration

```text
~/.tracefold/config.yaml
~/.tracefold/telegram_bot_token
~/.tracefold/postgres_password
~/.tracefold/postgres_database_password
~/.tracefold/logs/
~/.tracefold/cache/
```

The operator directory is private (`0700`) and secret/config files use `0600`.
`tracefold init` owns generated defaults. Keep live credentials out of repository
files, examples, logs, and PRs; the Telegram token belongs in its dedicated file.
Inspect redacted configuration and available commands with:

```bash
uv run tracefold config
uv run tracefold --help
```

For the standard Compose deployment, database/broker addresses in the active config
are Compose-network addresses. Run those operational commands inside Workers:

```bash
docker compose exec workers tracefold news bus-check
docker compose exec workers tracefold db audit
```

Do not assume automatic host-address rewriting. Development tests instead use their
explicit isolated resources. See [Setup](docs/SETUP.md) for detailed installation
and configuration, [CLI help](docs/generated/cli-help.md) for command grammar, and
[OpenAPI](docs/generated/openapi.json) for HTTP fields.

## Development

Start with the requested observable outcome and the affected owner. A complete change
normally belongs in one cohesive PR, including callers, tests, documentation, generated
outputs, and obsolete-path removal. Use an Issue when durable scope or coordination is
needed, not as a mandatory precondition for a bounded fix.

Run focused checks while editing and broaden according to risk. `make test-fast`
is a broad hermetic checkpoint; `make test-ci` is the complete local preflight when
that scope is useful or explicitly required, not an automatic prerequisite for PR
submission. The remote required CI plan is unchanged by local check selection.
See [Development](docs/DEVELOPMENT.md) and [Issues/PRs](docs/agents/issue-tracker.md).

| Need | Owner |
| --- | --- |
| Coding-agent entry points | [AGENTS.md](AGENTS.md), [CLAUDE.md](CLAUDE.md) |
| Architecture and flow | [Architecture](docs/ARCHITECTURE.md) |
| API, CLI, config contracts | [Contracts](docs/CONTRACTS.md) |
| Setup and operations | [Setup](docs/SETUP.md), [Operations](docs/OPERATIONS.md) |
| Local verification and CI | [Development](docs/DEVELOPMENT.md), [Testing](docs/TESTING.md) |
| UI boundaries | [Frontend](docs/FRONTEND.md) |
| Migrations and authority | [Migrations](docs/MIGRATIONS.md), [Security](docs/SECURITY.md) |
| Review language and taxonomy | [CONTEXT.md](CONTEXT.md), [News taxonomy](docs/NEWS_TAXONOMY.md) |

## Non-goals

No duplicate business truth in queues or caches, hidden provider/model calls in read
APIs, automatic execution authority from a news model's answer, repository-local live
credentials, or compatibility aliases for replaced internal implementation paths.
