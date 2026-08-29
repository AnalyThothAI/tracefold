# Task worktrees

This is the one lifecycle policy shared by coding agents. Tool-specific root files may explain how their tool invokes a worktree, but must not copy or alter these rules.

## Lifecycle

1. Before editing, inspect the repository root, current branch and status, and the registered worktrees. Stay in an existing worktree only when it is dedicated to the current task.
2. The primary checkout stays on `main`, clean, and reserved for deployment lifecycle commands. Create a separate task worktree from the current `origin/main`; never switch or edit the primary checkout for a task.
3. Use one task, one worktree, one branch, and one PR. Do not reuse, reset, clean, prune, or remove another task's worktree or changes.
4. Bootstrap only what the task matrix in the root router requires. In particular, docs-only and Python-only work do not install Node dependencies; frontend, frontend-harness, package, and Node-toolchain work does.
5. Run focused development checks and the completion lanes selected by the code-owned CI impact plan inside the task worktree. Do not use the primary checkout's shared runtime stack as task-test evidence.
6. Commit and push only from the task branch. Use the repository PR template, complete the exact-HEAD verification fields, review the final diff, and merge only after the required `ci-gate` is green for that HEAD.
7. After merge, update the primary checkout by fast-forwarding `main`. Deploy only when deployment is part of the task and the final main SHA has full-plan `ci-gate` evidence. Remove the task worktree and branch only after merge is confirmed and cleanup is authorized.

## Failure boundaries

- A dirty primary checkout, stale target branch, missing resource, missing plan manifest, or unclassified path fails closed.
- Earlier-commit, local-only, skipped-resource, or PR-head evidence does not attest a later merge SHA.
- Machine-local paths, ports, credentials, and runtime topology belong in operator onboarding or local configuration, not in this portable policy.
