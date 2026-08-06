# Tracefold

Tracefold is an evidence-first market research system. One Python service
ingests provider posts, news, macro, DEX/CEX, and market evidence, persists material
facts in PostgreSQL, builds deterministic read models, and serves a React
operator console plus stable HTTP, WebSocket, and CLI contracts.

Tracefold is not a trading bot or a chat product. Provider frames are inputs,
not business truth.

## Architecture

```text
providers
  -> integrations
  -> PostgreSQL material facts
  -> durable dirty targets / bounded catch-up
  -> single-writer read models or immutable publications
  -> HTTP / WebSocket / CLI / React
```

The hard invariants are:

- PostgreSQL material facts are the only business truth.
- Current rows use stable product/window/target keys, never run or attempt IDs.
- Each current read model has exactly one writer and is rebuildable from facts.
- Unchanged projections write zero serving rows.
- Workers recover by polling durable PostgreSQL state at bounded intervals.
- Read surfaces never call providers or models.
- Missing evidence is explicit, never replaced by a fabricated zero or fallback.

The Python package is deliberately shallow:

```text
src/tracefold/
  market/         capture, identity, pricing, profiles, radar, read views
  news/           Article facts, deterministic Stories, immutable analysis
  macro/          observations and completed-session research
  integrations/   provider and external-system adapters
  platform/       config, PostgreSQL, telemetry, generic worker kernel
  app/            composition plus HTTP, WebSocket, and CLI adapters
```

Other packages import business capabilities from `tracefold.market`,
`tracefold.news`, or `tracefold.macro`, not from their internal modules. See
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
and waits for both runtimes plus the HTML console. It exits non-zero and points
to `make logs` if any required boundary is not ready.

Open `http://127.0.0.1:8765/` after it succeeds. The lifecycle is deliberately
small:

```bash
make status  # fail closed unless DB, migration, Serve, Workers, and console are ready
make logs    # follow service logs; Ctrl-C leaves the services running
make down    # stop containers without deleting PostgreSQL data
```

A second `make up` rebuilds that application image and recreates only the
migration, Serve, and Workers containers so configuration changes take effect.
An already running PostgreSQL container is not recreated, and its named volume,
operator configuration, and role passwords are preserved. The generated
defaults contain no provider, model, or webhook credential, and News push is
disabled. The product still starts; credential-dependent capabilities report
an explicit degraded or unavailable state instead of fabricating data. Add
credentials only to `~/.tracefold/config.yaml`, then rerun `make up`.

The operator-owned runtime directory is:

```text
~/.tracefold/config.yaml                 # 0600
~/.tracefold/postgres_password           # fresh-volume bootstrap only; 0600
~/.tracefold/postgres_serve_password     # read-only runtime; 0600
~/.tracefold/postgres_workers_password   # writer runtime; 0600
~/.tracefold/postgres_migrate_password   # migration runtime; 0600
~/.tracefold/logs/
~/.tracefold/cache/
```

The directory itself is mode `0700`. `tracefold init` is the sole default
configuration generator; repository fixtures and `.env` files are not runtime
truth. Confirm the active path and redacted credential booleans with:

```bash
uv run tracefold config
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
make test-integration
make test-contract
cd web && npm run lint && npm run typecheck
```

The maintained documentation surface is intentionally small:

| Need | Source |
|---|---|
| Install and deployment | [docs/SETUP.md](docs/SETUP.md) |
| Data and module architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Public config/API/WS/CLI contracts | [docs/CONTRACTS.md](docs/CONTRACTS.md) |
| Operations and PostgreSQL diagnosis | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Design and testing | [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| Frontend boundaries | [docs/FRONTEND.md](docs/FRONTEND.md) |
| Secrets and authentication | [docs/SECURITY.md](docs/SECURITY.md) |

Generated artifacts live in `docs/generated/` and always have checked-in
generators. Historical design records and implementation-detail test archives
do not live in the repository.

## Non-goals

- no trade execution;
- no compatibility aliases for retired names or paths;
- no provider response, queue, process cache, or projection as alternate truth;
- no hidden provider calls or mutations in read APIs;
- no repository-local live credentials.
