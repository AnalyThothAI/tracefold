# Shared agent router

Canonical source for the block that `AGENTS.md` and `CLAUDE.md` both carry.
Edit it here, then run `uv run python scripts/sync_agent_router.py --write`;
`make check` fails if either router has drifted from this file.

Keep it a router. Anything that is a substantive rule belongs in `docs/`, and
anything that describes the system belongs in `docs/ARCHITECTURE.md` — a copy
here is a second source that goes stale without anyone noticing.

<!-- BEGIN SHARED AGENT ROUTER -->

## System

Tracefold is one Python service and CLI that persists audited News and Trading facts in PostgreSQL and serves them to a React operator console.

## Invariants

- PostgreSQL material facts and durable ledgers are the only business truth; frames, messages, caches, projections, model outputs, and HTTP responses are not alternate truth.
- News and Trading are sibling capabilities: neither imports the other or reads the other's tables; `tracefold.app` is their only composition seam.
- Program, envelope, policy, metric, commit, tree, lock, tool, and resource identities are release evidence; identity changes use their explicit contract pins.
- Merge evidence belongs to the exact tested HEAD. Main, manual release runs, and unknown impact always use the full plan; a PR may omit a lane only through a verified `not_required(reason)` plan entry.
- Tests cross the affected public, persistence, process, broker, browser, or order-adapter seam. A mock cannot replace the risk mechanism, and skip/xfail/rerun cannot manufacture required green.
- Use one task worktree and branch; keep the primary checkout clean. Follow `docs/agents/worktrees.md` for the single lifecycle policy.
- Live data uses the operator-owned config reported by `uv run tracefold config`. Never print or copy secrets; report only redacted state and paths.
- Internal migrations are hard cuts: update consumers and delete obsolete aliases, forwarding modules, dual reads, and compatibility paths in the same change.

## Task routing

| Task surface | Must read | Bootstrap | Development tests | Completion plan |
| --- | --- | --- | --- | --- |
| docs-only | relevant document; issue tracker for planned work | none; Python only if its checker needs it | relevant docs/router checks | quality-static |
| pure Python | relevant Architecture section; Development | `make sync` | focused pytest; `make test-fast` | quality + hermetic + owner PostgreSQL/runtime lanes |
| PostgreSQL | Architecture DB section; Operations; Development | `make sync`; isolated PostgreSQL | focused real-PostgreSQL tests | quality + hermetic + postgres/migration/runtime lanes |
| frontend | Frontend; Contracts | `npm ci` in `web/` | focused Vitest; affected lint/type/build | quality + frontend lanes |
| CI/evidence | Development Verification Evidence Contract | `make sync`; Node only for affected harness/toolchain | trust-root and focused contract tests | full |
| deploy/capital | Operations; Security; relevant Architecture section and Issue | full task bootstrap | affected production seam | full plus live receipt |

## Truth routes

- Architecture and package boundaries: relevant section of `docs/ARCHITECTURE.md`; coding and verification: `docs/DEVELOPMENT.md`.
- Frontend and public surfaces: `docs/FRONTEND.md` and `docs/CONTRACTS.md`; operations, PostgreSQL, security, and deploy: `docs/OPERATIONS.md` and `docs/SECURITY.md`.
- Install and generated artifacts: `docs/SETUP.md` and `docs/generated/`; notebooks: `notebooks/README.md`.
- GitHub Issues are the PRD and acceptance tracker. Use `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, and `docs/agents/domain.md`.
- During development run focused checks; at completion run the code-owned impact plan. `make test-evidence` remains the local full-plan entry, and the final main SHA always needs full `ci-gate` evidence.

<!-- END SHARED AGENT ROUTER -->
