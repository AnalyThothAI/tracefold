# AGENTS.md

Router for coding agents (Codex, Cursor, generic LLM tooling). Detailed rules
live under `docs/`; the generated block keeps only a compact invariant summary
and task routes, followed by Codex-specific invocation rules.

The block between the two markers below is generated from
`docs/agents/shared-router.md`, which `CLAUDE.md` also carries. Edit that file
and run `uv run python scripts/sync_agent_router.py --write`; `make check`
fails on drift. Everything after the closing marker is Codex-specific and is
edited here.

<!-- BEGIN SHARED AGENT ROUTER -->

## System

Tracefold is one Python service and CLI that persists audited News and Trading facts in PostgreSQL and serves them to a React operator console.

## Invariants

- PostgreSQL material facts and durable ledgers are the only business truth; frames, messages, caches, projections, model outputs, and HTTP responses are not alternate truth.
- News and Trading are sibling capabilities: neither imports the other or reads the other's tables; `tracefold.app` is their only composition seam.
- Program, envelope, policy, metric, commit, tree, lock, tool, and resource identities are release evidence; identity changes use their explicit contract pins.
- Merge evidence belongs to the exact tested HEAD. Every PR, main push, release, and manual run executes the fixed complete CI job set; no path plan or omitted required job can manufacture green.
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
| test module | relevant production seam; Development | dependencies for its stable owner lane | focused module | quality + stable owner lane |
| CI/evidence | Development Verification Contract | `make sync`; Node for affected frontend harness/toolchain | native-report guard and focused contract tests | fixed full CI |
| deploy/capital | Operations; Security; relevant Architecture section and Issue | full task bootstrap | affected production seam | full plus live receipt |

## Truth routes

- Architecture and package boundaries: relevant section of `docs/ARCHITECTURE.md`; coding and verification: `docs/DEVELOPMENT.md`.
- Frontend and public surfaces: `docs/FRONTEND.md` and `docs/CONTRACTS.md`; operations, PostgreSQL, security, and deploy: `docs/OPERATIONS.md` and `docs/SECURITY.md`.
- Install and generated artifacts: `docs/SETUP.md` and `docs/generated/`; notebooks: `notebooks/README.md`.
- GitHub Issues are the PRD and acceptance tracker. Use `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, and `docs/agents/domain.md`.
- During development run focused checks. `make test-evidence` is the complete local preflight; merge/release evidence is the fixed CI workflow and successful `ci-gate` for the exact final main SHA.

<!-- END SHARED AGENT ROUTER -->

## Codex-specific invocation

- Follow the canonical lifecycle in `docs/agents/worktrees.md`.
- At task start, use Codex's repository/status tools to identify the current checkout and registered worktrees. Keep an already assigned task worktree; otherwise prefer the Codex app's **Worktree** mode.
- A detached Codex worktree needs a task branch before edits. Use the `codex/<issue>-<slug>` prefix or the branch already assigned by the app.
- Record non-trivial scope and changed decisions in the GitHub Issue. Run the `code-review` skill on the final task branch; add the available security review when the changed surface requires it.
