# CLAUDE.md

Claude-specific router. Detailed rules live under `docs/`; the generated block
keeps only a compact invariant summary and task routes, followed by the
Claude-only Skills / Plan-mode / Worktree protocol.

The block between the two markers below is generated from
`docs/agents/shared-router.md`, which `AGENTS.md` also carries. Edit that file
and run `uv run python scripts/sync_agent_router.py --write`; `make check`
fails on drift. Everything after the closing marker is Claude-specific and is
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
- Each edit runs only the smallest check that can disprove it. `make test-fast` is a broad hermetic final checkpoint, not a save/commit loop, and an ordinary behavior change runs it at most once. `make test-ci` is an optional complete local preflight, run at most once only for declared high-risk changes; `NOT RUN` is honest local evidence. Every PR and final main SHA still runs the fixed complete remote CI plan.
- A bug or refactor records the smallest failing-to-passing reproducer, the real seam it crosses, and adjacent passing-to-passing regressions. A successful broader check subsumes narrower checks on the same unchanged tree.
- Use one task worktree and branch; keep the primary checkout clean. Follow `docs/agents/worktrees.md` for the single lifecycle policy.
- Live data uses the operator-owned config reported by `uv run tracefold config`. Never print or copy secrets; report only redacted state and paths.
- Internal migrations are hard cuts: update consumers and delete obsolete aliases, forwarding modules, dual reads, and compatibility paths in the same change.

## Task routing

The fixed complete remote CI plan never varies by surface. The table chooses local
reading, bootstrap, edit feedback, and the final checkpoint. `make sync` is initial
worktree bootstrap when dependencies are unavailable, or required after lock/dependency
changes; it is not a per-edit step.

| Task surface | Must read | Bootstrap | Edit loop | Final local checkpoint |
| --- | --- | --- | --- | --- |
| docs-only | relevant document; issue tracker for planned work | none; Python only if the owning checker needs it | owning docs/router checker | owning checker; no `test-fast` or `test-ci` |
| localized Python | relevant Architecture section; `docs/DEVELOPMENT.md#risk-tiered-local-verification` | `make sync` only when needed | exact pytest/nodeid or touched static check | F2P/P2P plus affected seam; `test-fast` optional, at most once |
| PostgreSQL/RabbitMQ | Architecture DB/broker section; Operations; local verification | Python plus only the affected isolated resource | exact real-resource test | affected real dependency seam; full preflight only when cross-owner |
| frontend | Frontend; Contracts; local verification | `npm ci` in `web/` | exact Vitest or touched lint/type check | affected lint/type/build and browser seam; no unrelated backend lane |
| test module | relevant production seam; local verification | only resources declared by that module's seam | exact module/nodeid | affected owner checkpoint; broaden only for shared fixtures or selection |
| package/build | Architecture package boundaries; Setup; local verification | Python; Node only for affected toolchain | focused package or distribution check | affected build/distribution seam plus one complete `make test-ci` |
| CI/test infrastructure | `docs/TESTING.md#fixed-full-ci-implementation` | Python; Node and resources for affected owners | focused contract/report check | one complete `make test-ci` |
| deploy/release verification | Operations; Security; fixed CI implementation; Issue | full task bootstrap | smallest affected production-seam check | affected production seam, one complete `make test-ci`, and Issue-declared live receipt |
| capital/order authority | Operations; Security; relevant Architecture section and Issue | affected real-resource bootstrap | smallest affected production-seam check | Issue-declared real seams and live receipt; full preflight when multiple owners change |

## Truth routes

- Architecture and package boundaries: relevant section of `docs/ARCHITECTURE.md`; coding: `docs/DEVELOPMENT.md`; local verification policy: `docs/DEVELOPMENT.md#risk-tiered-local-verification`; fixed CI wiring: `docs/TESTING.md#fixed-full-ci-implementation`.
- Frontend and public surfaces: `docs/FRONTEND.md` and `docs/CONTRACTS.md`; operations, PostgreSQL, security, and deploy: `docs/OPERATIONS.md` and `docs/SECURITY.md`.
- Install and generated artifacts: `docs/SETUP.md` and `docs/generated/`; notebooks: `notebooks/README.md`.
- GitHub Issues are the PRD and acceptance tracker. Use `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, and `docs/agents/domain.md`.
- Merge authority is the strict required `ci-gate` for the exact PR HEAD; release/deploy evidence is the fixed workflow and successful `ci-gate` for the exact final main SHA.

<!-- END SHARED AGENT ROUTER -->

## Claude-specific invocation

- Follow the canonical lifecycle in `docs/agents/worktrees.md`.
- Use `EnterWorktree` for a new Claude task worktree and keep an already assigned task worktree. Use the `claude/<issue>-<slug>` branch prefix unless the environment assigned another task prefix.
- Use Plan mode for non-trivial hard cuts and keep durable scope or decision changes in the GitHub Issue.
- Do not require local `/code-review` as a final checkpoint; code review belongs to the PR. Run `/security-review` when the changed surface requires it.
