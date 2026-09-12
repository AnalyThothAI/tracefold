# Task worktrees

This is the one lifecycle policy shared by coding agents. Tool-specific root files may explain how their tool invokes a worktree, but must not copy or alter these rules.

## Lifecycle

Read-only inspection may use the current checkout without creating a branch or
worktree. A request to implement authorizes routine isolated setup, edits,
checks, and repairs within that scope; do not ask again for those steps.
The requested outcome and existing authorization determine whether the task also
includes a PR, merge, deployment, or cleanup. See [Completion](../DEVELOPMENT.md#completion).

1. Before editing, inspect the repository root, current branch and status, and the registered worktrees. Stay in an existing worktree only when it is dedicated to the current task.
2. The primary checkout stays on `main`, clean, and reserved for deployment lifecycle commands. Create a separate task worktree from the current `origin/main`; never switch or edit the primary checkout for a task.
3. Use one task, one worktree, one branch, and one PR. Do not reuse, reset, clean, prune, or remove another task's worktree or changes.
4. Bootstrap dependencies only when missing or their lock/dependency inputs changed, for the commands selected by [local verification](../DEVELOPMENT.md#risk-tiered-local-verification). A complete local preflight needs all fixed owners' resources, including Node and browsers; an affected-only check needs only its own resources. Use isolated test resources, never the primary checkout's shared runtime stack.
5. Develop with focused checks and complete the risk-selected final checkpoint. Follow the same verification policy for failure recovery and revalidation; do not treat an earlier failed attempt as successful evidence.
6. When delivery includes a PR, keep the branch local through the final checkpoint by default, then commit, push, and create a ready-for-review PR so incomplete synchronize pushes do not repeatedly consume the fixed CI plan. An early Draft PR is allowed for collaboration, with the explicit cost that every push triggers CI; `cancel-in-progress` cancels only the older run for that same PR.
7. Use the repository PR template, report `NOT RUN` when focused local evidence is sufficient, review the final diff, and merge only after `ci-gate` is green for that exact HEAD. The active strict `main-production-verification` Ruleset requires `ci-gate`, has no bypass actor, and permits squash merges only. Do not add draft/path/message skips or another gate name.
8. After merge, update the primary checkout by fast-forwarding `main`. Deploy only when deployment is part of the task and the exact final main SHA has fixed-CI `ci-gate` evidence. Remove the task worktree and branch only after merge is confirmed and cleanup is authorized.

## Failure boundaries

- A dirty primary checkout blocks updating or deploying from it; preserve its changes and continue independent work in the task worktree. Never reset another task's changes to make the primary clean.
- Fetch the target branch before creating a new task worktree or preparing a merge. If its current state cannot be established, report the uncertainty and do not claim the branch is current or merge-ready; independent inspection and local work can continue.
- A missing required resource blocks that check and any acceptance depending on it. Repair task-owned isolated resources where possible; otherwise report the failed, partial, or unrun check and continue independent work. Do not use production resources or skips to replace missing evidence.
- Failed, cancelled, skipped, missing, or unknown required PR CI blocks merge. Missing successful fixed CI for the exact final main SHA blocks release/deployment of that change. Neither blocks independent local inspection, fixes, or preparation.
- Earlier-commit, local-only, skipped-resource, or PR-head evidence does not attest a later merge SHA. Missing, cancelled, skipped, or unknown required CI is not green.
- When a step needs new authorization, identify the exact action and governing instruction or unresolved decision; first finish independent work already authorized. Existing authorization remains valid within its stated scope.
- Machine-local paths, ports, credentials, and runtime topology belong in operator onboarding or local configuration, not in this portable policy.
