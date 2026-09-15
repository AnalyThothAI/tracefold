# AGENTS.md

Start here for repository work. The shared block is generated from
`docs/agents/shared-router.md`; edit that source and run
`python3 scripts/sync_agent_router.py --write`.

<!-- BEGIN SHARED AGENT ROUTER -->

## System

Tracefold has sibling News and Trading capabilities, a React console, and a
PostgreSQL ledger. Serve and Workers share the application image; the optional
Nautilus execution process has a separate image and lifecycle.

## Work on the requested outcome

- Read the affected implementation and tests, then the relevant document section.
  Do not read every linked manual or invoke every available skill before editing.
- Prefer one cohesive PR that completes the requested outcome, including its tests,
  documentation, generated outputs, and removal of obsolete internal paths. Split
  only for independently useful changes or a concrete review, rollout, or rollback
  reason; implementation steps and checkpoints are not automatically separate PRs.
- Use the existing task checkout/branch or an isolated worktree as appropriate.
  Preserve unrelated changes. A local worktree is not required for connector-only
  edits. See `docs/agents/worktrees.md`.
- A clear user request can authorize implementation. Use an Issue for durable scope
  or coordination when needed, not as a prerequisite for every fix. Record material
  decisions in the existing Issue or PR; do not create a ticket hierarchy by default.
- Run checks that exercise the changed risk; broaden for shared or uncertain impact.
  Report what actually ran and what remains unverified. Missing resources block the
  affected check, not independent editing, inspection, or PR preparation.
- Continue authorized work through verification and repairs. Opening a requested PR
  does not authorize merging, deployment, live trading, or accepting model reviews.

## Boundaries to preserve

- News and Trading own their respective facts and tables. Neither imports or reads
  through the other; `tracefold.app` maps their public contracts and composes them.
- PostgreSQL facts and durable decisions are not interchangeable with provider
  frames, model predictions, queues, caches, or UI projections. External execution
  results must be reconciled with the venue, not inferred from a local request.
- Keep transactions short and external I/O outside them. Internal renames are hard
  cuts: update consumers and remove obsolete aliases and duplicate paths together.
- Keep secrets out of source, logs, examples, and PRs. Do not replace required tests
  with skips or report pending CI as passing. Preserve actual data, permission,
  concurrency, and order-authority controls while simplifying unnecessary process.

## Where to look

| Concern | Owner |
| --- | --- |
| Current architecture and data flow | `docs/ARCHITECTURE.md`; `docs/agents/domain.md` |
| Design, local verification, generated outputs, completion | `docs/DEVELOPMENT.md` |
| Issue scope and PR boundaries | `docs/agents/issue-tracker.md` |
| CI jobs, resources, and reports | `docs/TESTING.md` and `.github/workflows/ci.yml` |
| API, CLI, configuration, and schemas | `docs/CONTRACTS.md`; `docs/generated/` |
| Frontend | `docs/FRONTEND.md` |
| Installation, operations, migrations, or authority | The affected section of `docs/SETUP.md`, `docs/OPERATIONS.md`, `docs/MIGRATIONS.md`, or `docs/SECURITY.md` |
| Research notebooks | `notebooks/README.md` |

These are task routes, not a mandatory reading sequence. Verify suspected drift
against the current implementation and the requested behavior; fix the owning
document instead of adding a competing rule. Historical Issue plans and optional
tool skills do not override the current task's scope or imply extra approval gates.

<!-- END SHARED AGENT ROUTER -->

## Tool use

Use the tools available in the current environment. A plan, review skill, or
worktree helper is useful when it reduces risk, not a required ceremony.
Checkout handling is defined in `docs/agents/worktrees.md`.
