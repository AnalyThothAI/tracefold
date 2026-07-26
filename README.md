# Tracefold

Tracefold is an evidence-first market research system. One Python service
ingests social, news, macro, DEX/CEX, and provider evidence, persists material
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
  notifications/  durable notification and delivery behavior
  integrations/   provider and external-system adapters
  platform/       config, PostgreSQL, telemetry, generic worker kernel
  app/            composition plus HTTP, WebSocket, and CLI adapters
```

Other packages import business capabilities from `tracefold.market`,
`tracefold.news`, `tracefold.macro`, or `tracefold.notifications`, not from
their internal modules. See [Architecture](docs/ARCHITECTURE.md).

## Runtime

Live configuration is operator-owned:

```text
~/.tracefold/config.yaml
~/.tracefold/workers.yaml
~/.tracefold/postgres_password
~/.tracefold/logs/
~/.tracefold/cache/
```

Repository fixtures and `.env` files are not runtime truth. Confirm effective
paths without printing secrets:

```bash
uv run tracefold config
```

Quick start:

```bash
make sync
make init
make db-migrate
make serve
```

The console is served at `http://127.0.0.1:8765/`. Docker users can run
`make docker-up`, inspect with `make docker-status`, and stop with
`make docker-down`.

Useful read-only checks:

```bash
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:8765/readyz
uv run tracefold db health
uv run tracefold ops queue-inspect --status active
uv run tracefold macro status
```

Exact HTTP fields come from
[OpenAPI](docs/generated/openapi.json). The complete CLI snapshot is
[cli-help.md](docs/generated/cli-help.md).

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
