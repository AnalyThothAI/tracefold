# Issues and pull requests

Use GitHub in `AnalyThothAI/tracefold` for durable requests and review. Use the
connected GitHub tools or an available `gh` CLI; neither tool is mandatory.
Resolve an ambiguous number as an Issue or PR before acting on it.

## Scope before bureaucracy

A clear user request or an existing PR discussion is sufficient to implement a
bounded change. Create or update an Issue when the work needs a durable product
specification, coordination, unresolved decisions, or tracking beyond the PR.
Do not create a duplicate Issue merely because a skill expects a ticket.

For a substantive Issue, state the problem, observable outcome, affected owners,
and acceptance evidence. Add constraints, non-goals, migration, or rollout details
only when they matter. Read the relevant discussion before changing an existing
agreement. Keep material decisions in that Issue or the implementing PR, rather
than copying the same plan into several trackers and documents.

## Default: one complete outcome, one PR

A cohesive change includes implementation, affected callers, tests, documentation,
generated outputs, and deletion of replaced internal paths. Frontend, backend,
schema, and tests are not separate PRs merely because they are separate directories.
Likewise, a checklist, TDD cycle, investigation step, or task in a plan is not a PR
boundary. Continue through the requested outcome rather than stopping after its
first small slice.

Split when parts can genuinely be reviewed, delivered, or rolled back independently,
when a migration requires staged rollout, or when size makes reliable review
impractical. Explain the reason, dependencies, and completion condition. There is
no mandatory PR count, line limit, or maximum number of files. Do not fragment a
hard cut into temporary compatibility layers solely to make smaller diffs.

Sub-issues, maps, dependencies, assignments, and labels are optional coordination
tools for genuinely independent work. Do not automatically create a `/wayfinder`
map, claim a ticket as the session's first write, or limit execution to the first
unassigned child. Existing project coordination still matters when applicable;
implementation authorization and scope come from the actual request.

## PR delivery

Use the [PR template](../../.github/pull_request_template.md) as a short review aid:
what changed, why, and how it was checked. Link a governing Issue when one exists;
otherwise put the request and acceptance summary in the PR. Omit irrelevant
fields instead of filling a checklist with invented evidence or repeated `N/A`.

Report meaningful contract changes and verification gaps. A PR can be submitted
while CI is pending; only an authorized merge depends on the required checks for
its current HEAD. PR submission, merge, deployment, and production acceptance are
separate outcomes.

Close an Issue only when its stated outcome is met, or explain another disposition.
Use `Closes #N` only for full completion; partial work should describe what remains.
[Triage labels](triage-labels.md) assist coordination and are not permission gates.
