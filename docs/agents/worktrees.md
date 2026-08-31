# Task worktrees

This is the one lifecycle policy shared by coding agents. Tool-specific root files may explain how their tool invokes a worktree, but must not copy or alter these rules.

## Lifecycle

1. Before editing, inspect the repository root, current branch and status, and the registered worktrees. Stay in an existing worktree only when it is dedicated to the current task.
2. The primary checkout stays on `main`, clean, and reserved for deployment lifecycle commands. Create a separate task worktree from the current `origin/main`; never switch or edit the primary checkout for a task.
3. Use one task, one worktree, one branch, and one PR. Do not reuse, reset, clean, prune, or remove another task's worktree or changes.
4. Bootstrap only what the task matrix in the root router requires. In particular, docs-only and Python-only work do not install Node dependencies; frontend, frontend-harness, package, and Node-toolchain work does.
5. Develop locally on the task branch with the smallest focused checks, then run the final local checkpoint selected by the root task matrix. `make test-ci` is run at most once only when that matrix or the governing Issue declares the change high risk. Do not use the primary checkout's shared runtime stack as task-test evidence.
6. Keep the branch local through the final checkpoint by default, then commit, push, and create a ready-for-review PR so incomplete synchronize pushes do not repeatedly consume the fixed CI plan. An early Draft PR is allowed for collaboration, with the explicit cost that every push triggers CI; `cancel-in-progress` cancels only the older run for that same PR.
7. Use the repository PR template, report `NOT RUN` when focused local evidence is sufficient, review the final diff, and merge only after `ci-gate` is green for that exact HEAD. The active strict `main-production-verification` Ruleset requires `ci-gate`, has no bypass actor, and permits squash merges only. Do not add draft/path/message skips or another gate name.
8. After merge, update the primary checkout by fast-forwarding `main`. Deploy only when deployment is part of the task and the exact final main SHA has fixed-CI `ci-gate` evidence. Remove the task worktree and branch only after merge is confirmed and cleanup is authorized.

## Failure boundaries

- A dirty primary checkout, stale target branch, missing required resource, skipped required job, or unknown CI result fails closed.
- Earlier-commit, local-only, skipped-resource, or PR-head evidence does not attest a later merge SHA. Missing, cancelled, skipped, or unknown required CI is not green.
- Machine-local paths, ports, credentials, and runtime topology belong in operator onboarding or local configuration, not in this portable policy.
