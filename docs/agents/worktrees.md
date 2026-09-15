# Task checkouts and worktrees

The goal is to isolate changes, not to require a particular local topology.
This policy also applies when a coding agent edits through a GitHub connector.

## Lifecycle

1. For local edits, inspect the current branch and status. Reuse an assigned
   task checkout or branch when it is suitable. Create a separate worktree when
   concurrent work, unrelated changes, or an operator deployment checkout needs
   isolation. Read-only inspection needs neither a new branch nor a worktree.
2. Keep PR changes on a task branch, not directly on `main`. Use an appropriate
   descriptive branch name; no tool-specific prefix or Issue number is required.
   For connector-only edits, use an isolated remote branch and a known base SHA;
   do not claim to have created or tested a local checkout.
3. Preserve unrelated changes and other tasks' worktrees. Never reset, clean,
   overwrite, or remove them to obtain a clean status. Do not use production
   databases, broker instances, accounts, or credentials as test fixtures.
4. Implement the complete agreed outcome and select checks using
   [local verification](../DEVELOPMENT.md#risk-tiered-local-verification).
   Install only dependencies needed by those checks. Several implementation
   steps or local checkpoints can belong to the same branch and PR.
5. Push and open a PR when requested and useful for review. Drafts and subsequent
   correction pushes are legitimate; a local full-suite pass is not a universal
   prerequisite for creating a PR. Report pending, failed, or unrun checks.
6. Merge, deploy, and remove task resources only when those actions are authorized.
   For an authorized merge or deployment, inspect the actual required checks and
   follow [completion](../DEVELOPMENT.md#completion) and the operational runbook.

## Failure boundaries

A missing dependency or permission blocks only the action that needs it. Repair
an isolated task resource where practical, continue independent authorized work,
and name any remaining blocker precisely. Do not fabricate evidence or use an
unrelated live service to make a test pass.

Record the base ref actually inspected. Refresh or compare with the target branch
before delivery when possible; if freshness cannot be established, state that
limit. A dirty deployment checkout blocks changes to that checkout, not work on
an independent branch. Do not repeatedly request permission for already-authorized
edits, checks, or repairs, and do not infer permission for a live rollout from a
request to submit a PR.
