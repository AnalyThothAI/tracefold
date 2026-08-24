# Shared agent router

Canonical source for the block that `AGENTS.md` and `CLAUDE.md` both carry.
Edit it here, then run `uv run python scripts/sync_agent_router.py --write`;
`make check` fails if either router has drifted from this file.

Keep it a router. Anything that is a substantive rule belongs in `docs/`, and
anything that describes the system belongs in `docs/ARCHITECTURE.md` — a copy
here is a second source that goes stale without anyone noticing.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: one Python service and CLI named
`tracefold` that turns provider news pushes into audited research signals and
serves them over HTTP / CLI to a React operator console.

Two business capabilities sit over one PostgreSQL store, as siblings rather
than layers: **News V3** (`tracefold.news`) turns OpenNews frames into Events,
judges each one, and delivers at most one reader card; **Trading**
(`tracefold.trading`, #104, disabled by default) is the capital lane that
consumes two public News projections. They never import each other and never
read each other's tables — `tracefold.app` is the only seam that knows both.

`docs/ARCHITECTURE.md` is the single description of how any of that works: the
Kappa/CQRS truth model, the broker-driven Event pipeline, the Gate, the
Program-native Triage and its policy, delivery, the instrument universe, the
Price Review plane, the learning/canary plane, and the Trading state machine.
Read it before changing behavior; do not reconstruct it from this file.

Two facts worth carrying into every task because they are easy to get wrong:

- PostgreSQL material facts and durable ledgers are the only business truth.
  Provider frames, broker messages, process caches, projections, model outputs
  and HTTP responses are not an alternate truth.
- Program identity — prompt, RulePack, DemoBank, model routing, call budgets,
  `program_sha256`, policy and metric versions — is release evidence. Changing
  it is an explicit, evidence-gated migration, never a side effect.

## Agent skills

### Issue tracker

GitHub Issues in `AnalyThothAI/tracefold` are the project request and PRD
tracker. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical label mapping in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Follow `docs/agents/domain.md` before
domain exploration; absent optional context or ADR files are not errors.

## Runtime config for real data

Live-data runs use the operator-owned `~/.tracefold/config.yaml` for
application/provider/credential/storage settings. Worker topology and
safety/resource budgets are code-owned. Do not assume repository fixtures,
example YAML, or `.env` files are the active runtime config. Before debugging
provider data or News events against real data, run `uv run tracefold config`
and confirm the reported `config_path` points at `~/.tracefold/config.yaml`.
Never print or copy secret values; report only redacted booleans, paths, and
diagnostic command results.

## Frontend guardrails

Frontend CSS is harness-constrained, not convention-only. Before changing
`web/src` UI code, read `docs/FRONTEND.md`. Do not recreate retired CSS buckets
such as `cockpit.css`, `macro.css`, `macroResponsive.css`, `shared.css`, or
`signalLab.css`; owner CSS must live beside the component or route that imports
it. Feature CSS must use the owning feature namespace and must not restyle
shared UI internals or Obsidian `.ods-*` selectors. `npm run lint` runs ESLint
plus the frontend architecture harness; do not bypass it after CSS, responsive,
route shell, or shared UI changes.

## Where to read what

| Need                                      | File                                                                                     |
| ----------------------------------------- | ---------------------------------------------------------------------------------------- |
| Install, run, docker                      | `docs/SETUP.md`                                                                          |
| What the system is, and why               | `docs/ARCHITECTURE.md`                                                                   |
| How code here must be written             | `docs/DEVELOPMENT.md` ("Architecture coding rules")                                      |
| Frontend architecture                     | `docs/FRONTEND.md`                                                                       |
| Public surfaces (config, HTTP, CLI)       | `docs/CONTRACTS.md`                                                                      |
| Development, issue specs, design, testing | `docs/DEVELOPMENT.md`                                                                    |
| Secrets, config, authn changes            | `docs/SECURITY.md`                                                                       |
| Operations, workers, PostgreSQL diagnosis | `docs/OPERATIONS.md`                                                                     |
| Business package boundaries               | `docs/ARCHITECTURE.md`; the public Python interfaces are the `tracefold.news` and `tracefold.trading` package roots |
| Durable specs and acceptance              | GitHub Issues; repository conventions are in `docs/agents/issue-tracker.md`              |
| Auto-generated artefacts                  | `docs/generated/`                                                                        |

CLI surface: `uv run tracefold --help` is the source of truth (snapshot at
`docs/generated/cli-help.md`).

<!-- END SHARED AGENT ROUTER -->
