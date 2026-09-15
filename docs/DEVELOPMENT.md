# Development

This document owns repository design guidance, local verification, and completion.
[Architecture](ARCHITECTURE.md) describes the implemented system;
[Testing](TESTING.md) describes the actual test and CI wiring. Detailed runtime
contracts belong to their implementation and the relevant operator manual, not to
a second set of global coding-agent instructions.

## Specify the behavior first

Understand the requested outcome and the affected owner before changing code.
A clear user request or PR discussion can specify a bounded change; use a GitHub
Issue for durable product scope or coordination when needed. Record material
changes to an existing agreement in that Issue or PR. Do not require a separate
planning ticket, approval cycle, or document for every non-trivial edit.

State what the user should observe and how it will be checked. Include migration,
cutover, or authority requirements when the change actually has them. Follow
[Issue and PR scope](agents/issue-tracker.md): one complete outcome is normally one
PR, not one PR per file, layer, checklist item, or red/green test cycle. Break down
the work internally without turning that breakdown into mandatory delivery slices.

Before adding another worker, table, score, or abstraction, trace the current input,
owner, persisted state, and consumer. Extend the existing owner when its lifecycle
and responsibility fit. Extract a shared component when it reduces real duplication
or isolates a meaningful boundary. There is no provider-count, file-count, or
line-count quota that decides good design.

## Package design

News and Trading are sibling capabilities. Neither imports the other or accesses
the other's tables. Their package roots expose stable value and port contracts;
`tracefold.app` composes implementations and maps the two sides explicitly.
App and integration collaborators may use the concrete internal owners permitted
by `tests/architecture/test_backend_boundaries.py`; ordinary feature callers do not
turn those collaborators into public APIs. Within a capability, use direct relative
imports rather than routing back through its public root.

Keep roots declarative and imports free of runtime work. Prefer cohesive modules
named for the responsibility they own. Follow the current architecture harness,
while evaluating design changes on behavior and dependency direction, not arbitrary
size counters. Avoid forwarding-only modules, redundant managers, one-implementation
internal interfaces, and frameworks built for hypothetical future consumers.
A meaningful abstraction or library does not need a separate detector-by-detector
Issue before it can be evaluated in the current authorized change.

## Architecture coding rules

**Ownership.** News owns `news_*`; Trading owns `trading_*`. Business mutations live
behind named repository methods in the owner. App supplies composition and process
facts rather than a competing business policy or cross-context SQL transaction.

**Ports and mappings.** A capability owns the narrow interface it needs. App adapts
it to process resources without leaking App-specific methods through untyped objects.
Choose the smallest explicit row/value shape that fits: a `TypedDict`, frozen
dataclass, or validating model as appropriate. Do not pass an unstructured dictionary
across a domain boundary to avoid naming the contract, or build a DTO framework to
wrap every local call.

**Transactions.** The caller owns transaction scope; a repository does not hide a
commit. Keep database callbacks bounded and SQL-focused. Provider, model, broker,
filesystem, and other network I/O happen outside transactions, without holding a
connection. Prepare expensive validation, canonical serialization, and hashes before
the callback; materialize richer objects after it. See
[transaction ownership](ARCHITECTURE.md#transaction-ownership).

**SQL and migrations.** Parameterize values and compose dynamic identifiers with
psycopg's SQL facilities. Reuse the actual production statement in query audits.
List public projection columns explicitly. Follow [Migrations](MIGRATIONS.md) for
schema changes; preserve genuine cross-process, economic, and append-only invariants.

**Hard cuts.** Internal Python paths are not external compatibility contracts.
Update callers, tests, documentation, and generated artifacts together, and remove
the replaced internal path. Public APIs and persisted data require an explicit
migration decision; do not destroy real data or silently change stored meaning in
the name of deleting compatibility code.

**Identity and guards.** Program, execution-envelope, schema, and policy changes
may invalidate stored evidence. Inspect the owning identity calculation and tests
before changing a pin; do not blindly re-pin a failing digest. When simplifying a
guard, identify the risk it actually protects and retain or replace that mechanism's
evidence. Do not retain a redundant gate merely because an old Issue once requested
it, or add a separate approval gate just to remove one. Secrets, permission checks,
transaction/concurrency guarantees, and uncertain external order outcomes are not
optional process ceremony.

## Tests

Test observable behavior at the boundary whose failure matters. Reuse existing
checks when they exercise that risk. A mock can isolate unrelated dependencies;
it cannot prove the behavior of the database, broker, process, browser, or order
adapter it replaces. Avoid tests that merely mirror private call choreography,
source wording, arbitrary file sizes, or a historical inventory.

### Risk-tiered local verification

This policy selects local checks; it is not a mandatory staircase of test suites.
Read-only analysis can finish with sourced findings without running unrelated tests.

During editing, use the smallest command that can disprove the current change.
At the final checkpoint, cover the affected seams and broaden when impact is shared
or uncertain. A checkpoint is not a reason to stop implementing the rest of the
already-authorized outcome.

| Changed risk | Useful local evidence |
| --- | --- |
| Documentation, comments, or spelling | Affected link/surface checks; the owning generator and drift check for generated text. |
| Mechanical rename or formatting | Touched static checks and behavior checks where imports or behavior can change. |
| Local Python behavior | Focused regression and neighboring behavior; broader hermetic checks when shared impact warrants them. |
| Shared contracts, serialization, or domain logic | Affected contract tests and broad hermetic regression, normally `make test-fast`. |
| Database, broker, or process semantics | Tests crossing that real boundary with isolated resources. |
| Frontend behavior | Affected tests, lint/type checks, build as relevant, and the browser seam for interaction changes. |
| Shared fixtures, test selection, CI, packaging, or deployment code | Validate the changed harness/build/lifecycle and affected consumers; use complete `make test-ci` when full-plan interaction is the risk. |
| Order authority, schema, or security semantics | Direct evidence for the changed authority or invariant; production exercise only when explicitly required and authorized. |

`make test-fast` is a broad hermetic checkpoint, not an edit loop.
`make test-ci` runs the whole fixed plan locally and needs all its isolated resources.
Use it for appropriate cross-system confidence or explicit task acceptance, not as
an automatic prerequisite for every PR, shared-fixture edit, or documentation change.
Remote required CI remains required regardless of the local selection. When a needed
check cannot run locally, state what remains unverified and use the actual remote
result rather than blocking independent work or fabricating a local pass.

For a **bug fix**, capture the smallest reproducer and observe failure before the
fix and success after it when practical. When the pre-fix run is unavailable, say
so; an after-only test is regression coverage, not observed failure-to-pass evidence.
For a **refactor**, demonstrate preserved observable behavior; do not invent a bug
just to fill an F2P field. For **new behavior**, check the intended result and adjacent
regressions. Documentation changes do not need a synthetic behavior failure.

Record commands and actual outcomes: `PASS`, `FAIL`, `PARTIAL`, or `NOT RUN`.
Explain significant gaps and changed acceptance semantics, without turning routine
fixture corrections into separate approval tickets. A failed attempt is not a pass;
a diagnostic rerun is not forbidden after investigation or repair. Do not use
retries, skips, or modified expectations to hide an unresolved defect.

Reuse successful local evidence for an unchanged tested tree and relevant inputs.
Rerun when later edits, dependencies, generated artifacts, environment, or unresolved
risk affect it. A broader successful command covers the subsets it actually ran;
do not repeat them merely to fill a template. Remote status, however, belongs to
its exact tested commit: an earlier PR green does not attest a new HEAD or squash SHA.

### CI and release evidence

The current workflow runs a fixed full plan and exposes `ci-gate`; its implementation
is documented in [Testing](TESTING.md#fixed-full-ci-implementation). This is a current
implementation, not a permanent prohibition on improving CI in an explicitly scoped
change. Changing prose does not change workflow behavior or repository enforcement.

Inspect required checks and repository rules when an authorized merge is requested.
Pending, missing, cancelled, skipped, or failed required checks are not green.
The deployment verifier requires successful main-push evidence for the exact final
main SHA; a local run or PR-head result does not substitute for it.

## News V3 evaluation seams

Keep code correctness and model quality distinct. Pytest can verify identity,
serialization, policy, state, replay, budgets, and wiring; it does not establish
that a candidate classifies or explains real news better.

The native News Program has EventSemantics, Taxonomy, and ReaderCard predictors.
`news learning run --target classification|understanding|explanation` optimizes one
predictor per run with that target's metric, on that predictor's production primary
endpoint (no fallback route, route deadline or breaker offline; production adds them).
The candidate is a `news_program_state_v1` document with only the target predictor's
native state moved. Reviews may label one task at a time; a dataset case is eligible
for the targets its accepted labels cover. Diagnose whether a defect belongs to source
evidence, entity identity, classification, reader explanation, novelty, deterministic
policy, delivery, or evaluation before changing a prompt.

Use [News taxonomy](NEWS_TAXONOMY.md), [review terminology](../CONTEXT.md), the
owning `tracefold/news/program/` and `tracefold/news/learning/` code, and the relevant
[operational commands](OPERATIONS.md). Read current CLI help for exact flags.
Do not copy machine-specific models, historical experiment results, retired epoch
numbers, or obsolete calibration gates into general development policy.
Accepted reviews, held-out evaluation, and release decisions retain their explicit
authority. A draft is not Gold; an optimizer improvement is not release approval;
a code PR is not authorization to accept reviews, spend an unspecified model budget,
promote a candidate, or perform live trades.

## Database development

PostgreSQL and Alembic's single head own the schema. The deployment's application
login is `tracefold`; process attribution uses `application_name`. Runtime code does
not execute DDL. Use an isolated test database, not the operator's deployment.

Keep transactions short, queries bounded, ordering deterministic, and idempotency
based on the appropriate natural key, unique constraint, or conditional write.
Indexes should serve an identified query and be justified by its plan and workload,
not by a copied historical row-count target. Refer to [Migrations](MIGRATIONS.md) and
[Testing](TESTING.md#local-lane-implementation) for migration and resource setup.

## Generated contracts

Edit the owner and regenerate its output in the same change. Inspect the diff;
do not hand-edit a generated file to make a drift check green.

| Output | Source / update command |
| --- | --- |
| Shared blocks in `AGENTS.md` and `CLAUDE.md` | `docs/agents/shared-router.md`; `python3 scripts/sync_agent_router.py --write` |
| CLI help | Production parser; `uv run python scripts/regen_cli_help.py` |
| OpenAPI and frontend API types | Python HTTP schema; `make regen-contract` |
| Database schema document | A disposable PostgreSQL database at Alembic head; `uv run python scripts/regen_db_schema.py` |
| RabbitMQ definitions | News broker policy; `uv run python scripts/regen_rabbitmq_definitions.py` |

Set `TRACEFOLD_TEST_POSTGRES_DSN` to the isolated database for DB generation; without
it the generator reads the operator config. The DB must already be at Alembic head.
Pure router and link checks need only Python; do not bootstrap the whole application
to run them:

```bash
python3 scripts/sync_agent_router.py --check
python3 scripts/check_mandatory_docs_links.py
```

`make check-static` and the relevant CI jobs own the other drift checks. The link
checker verifies local file targets, not every Markdown anchor or inline code path;
review changed routes and section links as well. See [Testing](TESTING.md).

## Completion

Complete the requested outcome, not merely the first implementation slice.
Implementation includes affected callers, tests, documentation, generated outputs,
and removal of obsolete internal paths. Report verification limits honestly.
A missing permission or resource blocks its dependent step, not independent work.

A requested **PR** is delivered when the reviewed change and its evidence are in an
open PR; report pending or failed CI and do not call it merge-ready without checking.
A requested **merge** additionally requires authorization, the actual required checks
on current HEAD, and confirmation that the merge happened. A requested **deployment**
requires its separate authorization, exact-main CI evidence, the operational procedure,
and any explicitly required live acceptance. Implementation alone does not require
an unrelated rollout or grant permission to operate a live account.

Use [task checkouts](agents/worktrees.md) for isolation and resource boundaries.
Do not repeatedly ask permission for routine edits, checks, and repairs already
within scope. Ask or stop only at the genuinely unresolved decision or unauthorized
action, after completing independent authorized work.
