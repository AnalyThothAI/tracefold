# Shared agent router

Canonical source for the compact invariant summary and task routes that
`AGENTS.md` and `CLAUDE.md` both carry. Detailed normative rules live in the
linked documents; this generated block intentionally keeps only the few
cross-task invariants needed before an agent can choose those documents.

Edit it here, then run `uv run python scripts/sync_agent_router.py --write`;
`make check` fails if either router has drifted from this file.

<!-- BEGIN SHARED AGENT ROUTER -->

## System

Tracefold is one Python service and CLI that persists audited News and Trading facts in PostgreSQL and serves them to a React operator console.

## Invariants

- PostgreSQL material facts and durable ledgers are the only business truth; frames, messages, caches, projections, model outputs, and HTTP responses are not alternate truth.
- News and Trading are sibling capabilities: neither imports the other or reads the other's tables; `tracefold.app` is their only composition seam.
- Program, envelope, policy, metric, commit, tree, lock, tool, and resource identities are release evidence; identity changes use their explicit contract pins.
- Every PR, main push, release, and manual run executes the fixed complete CI job set. Merge requires successful `ci-gate` evidence for the exact PR HEAD; release/deploy requires it for the exact final main SHA. No path plan or omitted required job can manufacture green.
- Tests cross the affected public, persistence, process, broker, browser, or order-adapter seam. A mock cannot replace the risk mechanism, and skip/xfail/rerun cannot manufacture required green.
- Live data uses the operator-owned config reported by `uv run tracefold config`. Never print or copy secrets; report only redacted state and paths.
- Internal migrations are hard cuts: update consumers and delete obsolete aliases, forwarding modules, dual reads, and compatibility paths in the same change.

## Task routing

Read the sections needed for the affected concern, not every document in a row.
Reuse context already read unless the task or source changes. Paths below are
repository-relative; this is a lookup table, not a pre-edit checklist.

| Task surface | Read for the affected concern |
| --- | --- |
| inspection/docs-only | The target document; `docs/DEVELOPMENT.md#generated-contracts` when changing generated artifacts. |
| Python/business behavior | The affected owner and tests; `docs/agents/domain.md` for business behavior, `docs/ARCHITECTURE.md#package-map` for package boundaries. |
| PostgreSQL/RabbitMQ | `docs/ARCHITECTURE.md#transaction-ownership` for transactions; the corresponding database or broker section of `docs/OPERATIONS.md`; `docs/DEVELOPMENT.md#database-development` for DB test setup. |
| frontend | The affected conventions in `docs/FRONTEND.md`; the relevant `docs/CONTRACTS.md` section when changing a public API or shared schema. |
| test module | The production seam under test; local verification below. |
| package/build | `docs/ARCHITECTURE.md#package-map`; the affected installation/build section of `docs/SETUP.md`. |
| CI/test infrastructure | `docs/TESTING.md#fixed-full-ci-implementation` for jobs, resources and native reports. |
| deploy/release | The governing Issue, the affected lifecycle in `docs/OPERATIONS.md`, and relevant `docs/SECURITY.md` rules; fixed CI implementation for release evidence. |
| capital/order authority | The governing Issue, the affected trading owner in `docs/ARCHITECTURE.md`, and relevant Operations/Security authority rules. |
| notebooks | `notebooks/README.md`. |

## Execution

- Before editing, follow `docs/agents/worktrees.md`: one task worktree and branch; keep the primary checkout clean. Read-only inspection needs no new worktree.
- `docs/DEVELOPMENT.md#risk-tiered-local-verification` owns local check selection, bug/refactor evidence, and necessary revalidation. Start with the smallest affected check; use its risk table for the final checkpoint.
- Bootstrap only dependencies needed by the selected commands. Run `make sync` when Python dependencies are unavailable or their lock/dependency inputs change; run `npm ci` in `web/` under the equivalent Node conditions. A complete `make test-ci` needs every fixed owner's isolated resources; a generated DB document needs its generator's PostgreSQL resource.
- GitHub Issues are the PRD and acceptance tracker. Record non-trivial scope and changed decisions in the governing Issue using `docs/agents/issue-tracker.md`; consult `docs/agents/triage-labels.md` when applying triage labels.
- Complete the requested outcome using `docs/DEVELOPMENT.md#completion`. Continue already-authorized work through verification and repairs; report blockers against the specific step they prevent.

<!-- END SHARED AGENT ROUTER -->
