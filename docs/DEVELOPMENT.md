# Development

This document owns design, issue, test-selection, and completion rules.

## Specify the behavior first

GitHub Issues in `AnalyThothAI/tracefold` are the durable request, PRD, and
acceptance surface. A non-trivial change starts with one current issue that
states:

- the problem and observable outcome;
- invariants and public contracts that must remain stable;
- allowed hard cuts and explicitly deleted compatibility paths;
- implementation boundaries;
- verification and cutover evidence.

Update the issue when a decision changes. Do not create parallel planning
archives or historical design diaries in `docs/`.

Before adding a service, table, worker, score, or contract, trace the existing
provider input through PostgreSQL fact, durable target, current row, and
consumer. Extend the current owner unless lifecycle or responsibility is
genuinely different. New tables, workers, model-backed products, probabilistic
outputs, or evaluation control planes require an explicit current need.

## Package design

The sibling business capabilities export only stable value and port contracts
from `tracefold.news` and `tracefold.trading`; neither package imports the other
or reaches the other's tables. Ordinary feature code uses those root
interfaces. `tracefold.app` is the composition seam that wires concrete
internal implementations from both capabilities, so it uses explicit owner
imports rather than widening a package root. Only the App composition families
and provider-adapter families declared by the architecture harness may import
a private business contract, and only from the contract families named there.

Inside a business package, use relative module imports rather than importing
back through the package root. Keep modules cohesive and move behavior behind a
narrow interface instead of adding forwarding modules, aliases, compatibility
packages, or a port for an internal object with only one adapter.

Package roots are declarative: imports, constants, and a narrow `__all__`, with
no runner lifecycle, SQL, network work, model graph, provider construction, or
import-time side effects.

Module and function size are review signals, not correctness contracts. Prefer
cohesive modules and narrow orchestration phases, but do not maintain exact
line-count ledgers or rename-sensitive size exceptions in the test suite.

PostgreSQL material facts and public HTTP/CLI contracts are migration
boundaries. Internal Python imports are not compatibility contracts. Hard cuts
delete the old path and update all consumers in the same change.

## Architecture coding rules

These are normative and executed by `tests/architecture/`; the list below is
the reason each check exists. `docs/ARCHITECTURE.md` says what the system is,
this section says how code in it must be written.

**Ownership.** A table's prefix is its owner: only `tracefold.news` may read or
write `news_*`, only `tracefold.trading` may touch `trading_*`. The two never
import each other. `tracefold.app` is the single seam that knows both.

**App purity.** `tracefold.app` decides how capabilities are assembled and run,
never what a business fact means. It may read a business projection — the
News → Trading handoff is exactly that — but it may not `INSERT`, `UPDATE` or
`DELETE` a `news_*` or `trading_*` row. Business SQL lives in the owning
package's storage module, behind a named repository method.

**Ports.** A business package states what it needs from the process as a narrow
`Protocol` it owns (`NewsDatabasePort`, `QuoteDatabasePort`,
`ReactionDatabasePort`, `TradingDatabasePort`), and `tracefold.app` implements it. A business module may
not call an App-specific method — `worker_session`, `run_news`,
`heavy_business` — even through an untyped parameter: no import edge is not the
same as no dependency. Ports that look alike stay separate when their answers
differ (which lane admits the work, whether a deadline may default, whose error
vocabulary an admission timeout speaks). Do not add a `BaseDatabase`, a generic
`RepositoryService`, or a port for an internal object with a single adapter.

**Cross-context mapping.** Each side declares its own frozen, versioned row
contract, and `tracefold.app` maps between them field by field. Neither side
imports the other's contract. `dict[str, Any]` and `Mapping[str, Any]` do not
cross a context boundary: a pass-through dictionary turns one context's rename
into the other's runtime bug months later. Choose the smallest shape that fits
the existing code — a `TypedDict` where the row *is* a SELECT list and a
validating model would coerce values PostgreSQL already typed, a frozen
dataclass otherwise. Do not build a DTO framework.

**Transactions and I/O.** The caller owns the transaction; a repository never
hides a commit. Provider, model, filesystem and network I/O happen outside a
database transaction, and no connection is held across one. Database callbacks
receive the narrow News, Price, Instrument, or Trading repository capability,
not a raw connection. They contain only SQL/locks, primitive row mapping, and
immediate rowcount/`RETURNING`/CAS checks. Prepare canonical JSON, hashes and
validated payloads before the callback; run Pydantic materialization,
compression, large loops/sorts/deep comparison and retry sleep after it.

**SQL and migrations.** Production SQL belongs to the owning storage package,
the PostgreSQL platform package, Alembic, or the small reviewed App adapter
allowlist enforced by the architecture suite. Values are parameters; dynamic
identifiers use psycopg composition; public/cross-context projections list
their columns. Query-plan audit imports the production statement builder.
Follow [the migration authoring guide](MIGRATIONS.md); a new revision must
explain the database need, lock/timeout/size boundary and verified recovery
path, and must run on the exact production PostgreSQL image.

**Naming.** Modules are named for what they own, not for a layer. There are no
`services/`, `managers/`, `factories/` or `utils.py` packages, and no
compatibility alias, forwarding module or re-export left behind by a rename —
a hard cut updates every consumer in the same change.

**Complexity.** An orchestration function should own a sequence of named phases,
not the phases themselves. Use code review and static typing to improve
cohesion; do not turn exact line counts, `Any` occurrences, suppression counts,
or historical file inventories into permanent architecture contracts.

**Program identity.** Program identity has two halves and two authors, and both
are release evidence rather than implementation detail. `program_sha256` covers
the two Predictor instructions — what a human editing
`tracefold/news/program/seed.py` or GEPA proposing a replacement may write.
`envelope_sha256` (`compute_execution_identity()` in
`tracefold/news/program/identity.py`) covers what the code decides about a
model call: the golden render of each Predictor's chat request in all three
structured-output modes (`json_schema`, `json_object`, and prompt-only JSON),
the single output contract and schema, the model-visible
input shapes, the endpoint capability table, the model binding slots, the route
deadline, the token ceilings and the breaker.
`tests/contract/test_program_release_identity.py` pins both, plus the policy,
review and metric versions. `docs/ARCHITECTURE.md` describes the model.

Envelope identity is *computed*, not declared (#314). Editing any material above
turns the pin red, and re-pinning `NEWS_EXECUTION_ENVELOPE_SHA256` in that test
is the signature on the identity migration — there is no `factory_id` literal to
bump, no epoch migration to write and no counter to keep in step. The learning
epoch follows automatically: the Workers startup barrier opens
`bundle_<sha8>` for any bundle it has not seen and trips every armed or active
canary. When the pin fails, diff `execution_envelope()` before re-pinning: the
hash says something moved, and the document says what.

**Pins carry greppable names.** A pinned value must be a named constant
(`NEWS_EXECUTION_ENVELOPE_SHA256`, `_EXPECTED_REPORT_SHA256`), never a bare
literal inside an assertion. Every consumer must reference that name so an
identity migration has a complete, searchable update set. Historical rationale
and incidents remain in #313.

**Deleting a guard.** Verify the mechanism in current code, identify secondary
error or concurrency contracts, update every durable rationale, and write the
acceptance search before deleting a check. Judge each guard separately; a prose
delete list is not evidence that the old path is gone. Historical examples and
their corrections remain in #319.

## Tests

| Entry | Purpose | Allowed | Excluded |
|---|---|---|---|
| `make check-static` | CI static quality owner | Ruff, format, mypy, generated/router drift, mandatory agent/operator documentation links, compileall | Pytest, Docker, DB, RabbitMQ, Node |
| `make check` | static and pure drift checks | Ruff, format, mypy, compileall, pure architecture/contract | Docker, DB, RabbitMQ, network, Node, sleeps/process orchestration, duplicate checkers |
| `make test` / `make test-fast` | default AI/developer loop | unit, hermetic contract, semantic architecture, temporary files, controlled local CLI subprocesses | Testcontainers, real PG/RabbitMQ, uvicorn, multiprocess orchestration, external codegen, load/p95 benchmarks |
| `make test-integration` | targeted real-dependency evidence | PostgreSQL, RabbitMQ, HTTP app/worker integration | unrelated deploy/e2e behavior |
| `make trading-smoke` | the Trading PostgreSQL acceptance contract | #350 Decision/Capital attribution and zero-Intent no-key blocks plus retained immutable Intent/recovery evidence on real PostgreSQL | live provider truth and everything outside the focused Trading integration modules; it is a subset of `make test-integration`, never merge evidence on its own |
| `make test-deploy` | deployment and operations behavior | Compose, locks, rollback, receipts, signals, fake executable simulation | default loop |
| `make test-e2e` | Serve-process evidence | real PostgreSQL, uvicorn, readiness and HTTP read surfaces | Workers or broker behavior |
| `make test-golden` | broker-driven production path | real RabbitMQ, production Workers wiring, PostgreSQL facts and HTTP read projection | provider/paid model truth |
| `make test-browser-smoke` | required browser/backend seam | production FastAPI static mount, bootstrap bearer, real API envelope and one Chromium `/news` fact | visual matrix and screenshot baselines |
| `make test-slow` | explicit process/meta-test diagnostics | shortened injected deadlines and nested fail-closed harness F2P | `make check`, `make test-fast`, live/provider truth |
| `make test-scheduled` | non-gating production-duration diagnostics | real code-owned timeout envelopes on a fixed runner | merge evidence and the default developer loop |
| `make test-visual` | explicit visual diagnostics | four viewport projects and screenshot baselines | required per-PR evidence |
| `make test-all` | local complete-suite convenience | all Python lanes and frontend | exact-HEAD CI or fail-closed evidence claims |
| `make test-ci` | complete local preflight | the same six owner surfaces as fixed CI, run serially with native reports and fail-closed resources/outcomes | merge/release authorization, `live`, visual/live/scheduled diagnostics, missing declared resources, skip/xfail/xpass/rerun/maxfail |

Prefer behavior at a maintained public or persistence seam. Do not preserve
tests that assert private file layout, source text, mock call choreography, or
implementation detail. Coverage is measured and reported; no percentage gates a
merge today.

Select commands by risk:

- schema or repository behavior: focused real-PostgreSQL integration tests;
- HTTP/CLI behavior: contract tests plus regenerated artifacts;
- workers: claim, lease, retry/terminal, restart catch-up, idempotency,
  single-writer, and external-I/O transaction boundaries;
- UI: scoped tests, lint, typecheck, build, and a browser check when visual or
  interactive behavior changes;
- documentation: bounded surface and link checks;
- generated files: run the owning generator and verify a clean second run.

Every bug or refactor PR records four pieces of evidence:

1. the smallest F2P reproducer that failed before and passes after;
2. the production seam it crosses;
3. the targeted P2P regressions for affected public or persisted boundaries;
4. the integration, deploy, e2e, or release lane still required.

### Verification Contract v4

GitHub Actions runs one fixed, complete verification set for every pull request
to `main`, every `main` push, release event, and manual dispatch. There is no
path planner, conditional lane, test inventory database, profile ratchet, lane
manifest, or evidence aggregate. Test frameworks own execution truth; GitHub
Actions owns job truth. Native reports are diagnostic artifacts for the exact
run, not a second release database.

`make test-ci` runs the same owner surfaces serially as a complete local
preflight. It is useful before pushing, but a local result cannot authorize a
merge, release, or deployment. The merge/release record is the successful fixed
CI workflow associated with the exact commit SHA being evaluated; deployment
requires the successful `main` push workflow for that same SHA.

1. **Exact HEAD.** A result belongs only to the full commit SHA it actually
   tested. Any later commit invalidates it and must run the required gate
   again. Pull-request HEAD and the eventual `main` merge SHA each need their
   own automated status; prose claiming an earlier green run is not evidence.
2. **Real F2P.** A bug regression must fail against the pre-fix behavior and
   pass after the fix. A new test observed only on the new implementation does
   not establish failure-to-pass evidence.
3. **Production seam.** The PR states which real boundary the reproducer
   crosses: CLI, HTTP, PostgreSQL, RabbitMQ, serializer, package identity,
   container, or order adapter. A fake may support the test but may not replace
   the risk mechanism being proved.
4. **Targeted P2P.** The F2P does not replace adjacent public or persisted
   regression coverage, and the whole-repository suite does not replace the
   issue-specific reproducer. Record both.
5. **Resources fail closed.** In fixed CI and `make test-ci`, an
   unavailable required resource—PostgreSQL, RabbitMQ, Docker/Testcontainers,
   Chromium, or Node codegen dependencies—is a failure, never a skip. Local
   fast mode remains hermetic and starts none of them.
6. **No pseudo-green.** Required runs reject unexpected skip, xfail, xpass,
   rerun, `--maxfail`, rerun plugins, and catch-and-continue behavior. Golden
   or snapshot outputs are checked for drift; a required run may not silently
   update them and continue green. Provider/live diagnostics are deselected by
   the explicit `not live and not scheduled` expression; this exclusion is
   visible in the canonical commands and never represents required green.
7. **Acceptance-test changes.** When the same PR changes an existing
   acceptance test, its verification section classifies the change as a
   product-contract change, a test defect, or a fixture repair and links the
   governing Issue decision.
8. **Temporary guard removal.** A migration or compatibility guard records an
   owner, Issue, and objective removal condition. “Later” is not a condition.
9. **Model quality stays separate.** Pytest protects call budgets, identity,
   serialization, policy, replay, and state semantics. Model quality remains
   the responsibility of frozen corpora, golden evidence,
   `CandidateEvaluator`, shadow/canary runs, and durable production evidence.

The six fixed jobs are `quality-static`, `python-hermetic`,
`postgres-behavior`, `migration`, `runtime-process`, and `frontend`. They run
without path conditions. PostgreSQL, RabbitMQ, process, and browser owners use
isolated job-local services rather than a shared schema or runtime. Each job
checks out `TESTED_SHA` (`pull_request.head.sha` for a pull request,
`github.sha` otherwise), installs with `uv sync --locked` and, where needed,
`npm ci`, and keeps Actions SHA-pinned and service images digest-pinned.

Two of those owners carry the evidence the flat package layout needs (#373).
`python-hermetic` runs the installed-distribution smoke: it builds the wheel and
sdist, installs the wheel into a throwaway environment pinned to the locked
dependency versions outside the repository, and imports the product and its
packaged resources there. `migration` walks every historical revision one at a
time from an empty database and compares the reachable revision graph against
the revision files on disk, so a migration tree that stops resolving from the
package is a failure rather than a shorter chain. Neither adds a package runner,
a manifest, or a second release record.

`postgres-behavior` carries the business failure windows (#373).
`test_news_crash_replay.py` runs the production consumers over real PostgreSQL
with the broker and the push sender as fault injectors, covering the Receiver
outage that becomes a durable incident and is recovered officially, the publish
that succeeds while its mark fails, the evidence that changes while the model is
thinking, and the begin-send-settle windows where a known-unsent card may retry
and an unknown remote outcome may not. `test_news_to_trading_seam.py` runs one OI
frame through News, the App mapper and the capital lane.
`test_trading_capital_lane.py` drives the Case lifecycle as a Hypothesis
`RuleBasedStateMachine` against real SQL and walks the whole authority product.
Assertions there are durable rows, queue outcomes and HTTP projections; a private
call count is not a contract.

The same run also produces coverage. `test-effectiveness` is report-only: it
combines what the fixed jobs measured and prints standard coverage.py reports,
depends on no job result, and `ci-gate` does not depend on it. Percentages are a
gap signal; the acceptance for a critical state or exception path remains its F2P
and its state model.

Required pytest runs disable plugin autoload, load only the named plugins they
need, use the deterministic Hypothesis `ci` profile, and emit JUnit XML under
`artifacts/test-results/`. Required Vitest runs set `allowOnly=false` and emit
native JSON; Playwright emits native JSON. Runner configuration and required-
test lint prohibit expected-failure, focus, retry, repeat, and snapshot-update
escapes. Required `test`/`it` declarations use unaliased named imports directly
and have exactly two arguments (case name and callback); namespace/dynamic
imports, copied/extended bindings, computed modifiers, and literal, computed,
or runtime-loaded options are inadmissible. This syntax boundary covers every
executable JavaScript and TypeScript module extension under `web/tests`,
including helpers/support modules; the one Playwright fixture factory may only
export `test = base.extend(...)` and may not register a case. A small
report guard checks only that each native report exists,
executed at least one test, and contains no failure, error, skipped/pending,
flaky, retried, snapshot-mutating, or unhandled outcome. For pytest, strict
xfail plus JUnit's non-green skipped/failure outcomes enforce the same rule. The
guard may not select tests, maintain an inventory, reinterpret a pass, or write
plan, profile, provenance, or aggregate manifests. CI uploads these native
files as `test-results-<job>-<TESTED_SHA>` with missing files treated as an
error.
Hypothesis remains a test-only dependency in the uv `dev` group and is resolved
by `uv.lock`; production images use `uv sync --locked --no-dev`, so test tools
do not enter the runtime environment.

The stable `ci-gate` is deliberately thin: it reads only the six
`needs.<job>.result` values and succeeds only when all six equal `success`.
Failure, cancellation, skip, or an unknown result fails closed. It does not
checkout code, download artifacts, or invoke a project-owned planner. The
deployment verifier remains a separate real seam and requires the trusted
GitHub Actions `ci-gate` from a successful `main` push for the exact SHA.

The repository is private and the organization is currently on GitHub Free;
as verified for #353 on 2026-08-30, the branch-protection and Rulesets APIs are
unavailable. The repository therefore does not claim that GitHub currently
enforces `ci-gate` before merge. `ci-gate` remains the observable full-result
interface and deployment authorization boundary, and can become the one
required check if the platform constraint changes. Private artifact
attestation, branch protection, and Rulesets are separate platform work, not
substitutes for this test contract.

Adopting `pytest-postgresql`, `pytest-alembic`, import-linter, Schemathesis,
mutation testing, or other generic tooling requires a separate detector-by-
detector Issue. A package's green result may not replace the real PostgreSQL,
migration, capital, browser, or provider/order seam it is meant to protect.

A pull-request run proves its exact PR-head commit. A squash merge creates a
different `main` commit, so the post-merge push run is the first merge/release
evidence for that SHA. Deployment, cutover, and release work must wait for that
exact main SHA's `ci-gate`. A green PR-head run is not pre-merge proof of a
future squash SHA. This repository does not claim merge-queue evidence.

Every PR uses the repository template and completes these fields:

```md
## Verification

- Tested HEAD:
- Risk being closed:
- F2P reproducer:
- Production seam:
- Targeted P2P:
- Focused / local commands:
- Local full preflight:
- Exact-head fixed CI run:
- Skipped / xfail / rerun:
- Acceptance-test contract changes:
- Native report artifacts:
```

Mocking is not itself a problem. Mocking the risk mechanism under test is: a
source-identity, import-path, wiring, serialization, migration, or transaction
regression must traverse that real production seam. The optimizer boundary test
is the positive example: it parses the real import graph of
`tracefold.news.learning.optimizer` rather than asserting its docstring.

`make check` is a hermetic static/architecture/contract bundle, not a universal
completion mandate. Run the additional lanes that cross the changed seam and
report omitted evidence honestly. It is also the gate: `ruff check` alone is a
narrower check that passes while `make check` fails, because the bundle runs the
formatter and the architecture harness too. Green from a command you chose is not
green from the command the project defines.

**A result binds to the exact commit and its isolated runner.** Editing the tree
during a local run, sharing one destructive database across CI jobs, or
borrowing the deployment checkout's Compose stack breaks that binding. Local
`make test-ci` therefore remains serial on a frozen tree. CI jobs may run
concurrently because each resource owner receives a separate job-local
PostgreSQL/RabbitMQ service and checks out the same `TESTED_SHA`.

**On a pull request, "no checks reported" is not "checks passed".** CI here
triggers on `pull_request` for PRs targeting `main` with the default activity
types, so a PR based on a feature branch runs nothing, and re-targeting its base
does not trigger a run — reopening it does. Read the check list before calling a
branch ready.

### Operator onboarding acceptance

Startup changes treat `make up`, `make status`, `make logs`, and `make down` as
one public lifecycle seam. Tests must keep `tracefold init` as the only default
config authority, reject a returned static example, verify `0700`/`0600`
permissions and non-rotation, inspect the Compose role/bootstrap boundary, and
prove that status cannot pass with a failed migration, stopped Worker, failed
readiness endpoint, or missing console.

The release acceptance uses an isolated empty home and a distinct empty Compose
volume. The first `make up` must reach Alembic head with healthy Serve/Workers
and real console HTML; the second must preserve config/password hashes and a
database sentinel, leave the PostgreSQL container identity and start time
unchanged, and run migration, Serve, and Workers from one image whose runtime
and OCI revisions match the checked-out commit. Missing optional live
credentials must appear as capability degradation rather than startup failure
or fake data. A failed startup keeps its containers/logs available for
`make logs` and returns non-zero.

The focused maintained checks include:

```bash
uv run pytest -q \
  tests/deploy/test_onboarding_surface.py \
  tests/deploy/test_postgres_deployment.py \
  tests/integration/test_cli_resources.py
```

### Test lanes and speed

`make test` aliases `make test-fast` and never starts an external resource.
Tests that need PostgreSQL declare an explicit PostgreSQL resource fixture;
their directory is not a resource trigger. `make test-integration`, `make
test-deploy`, `make test-e2e`, `make test-golden`, `make test-slow`, `make
test-scheduled`, and `make test-external-codegen` expose the larger lanes; scheduled
diagnostics are reported separately and never feed the merge gate. `make trading-smoke` is a named
subset of the integration lane and never evidence on its own. `make test-all` remains a local
complete-suite convenience. `make test-ci` is the canonical local
full-set entry, runs every deterministic owner with fail-closed resource and
outcome checks, and includes frontend validation. It emits native reports but
does not mint merge evidence. Pull requests, main, release, and manual runs all
execute the fixed six-job set; the exact main SHA's successful workflow is the
merge/release record.

PostgreSQL behavior tests use a run-scoped baseline database migrated once to
head, then receive a private function- or module-scoped clone. A test that
drops a schema, commits across connections, exercises roles, or takes locks
therefore keeps the real PostgreSQL seam without rerunning the Alembic chain or
sharing `public` with another behavior test. Destructive schema reset remains
only in the explicit migration-owner modules, which receive an empty private
database; the retired behavior reset helper has no caller. Historical
migration-path tests do not use the head clone shortcut: they are narrow and
explicit, and cover the
preservation/grant cuts that carry user evidence forward and the `0292` to
`0293`, `0293` to `0294`, `0294` to `0295`, and `0300` to `0301` append-only Program
epoch transitions. The Alembic chain is the
`20260818_0275` current-schema baseline plus the linear revisions through the
current `20260830_0335` head; schema tests also run against that migrated head.
The e2e lane
(`tests/e2e/test_serve_process_smoke.py`) starts one
uvicorn Serve subprocess against a freshly migrated testcontainers PostgreSQL
and asserts `/readyz`, `/api/status` and `/api/news/status`; it runs no Workers
subprocess and makes no broker claim. Retired route absence belongs to the
OpenAPI/public-route contract. The golden lane separately starts the public
Workers root, publishes through RabbitMQ, persists the deterministic OI path in
PostgreSQL and reads it through HTTP. ReviewDesk and CandidateEvaluator have their own integration lanes;
production shadow/canary evidence is sealed in PostgreSQL rather than inferred
from the HTTP e2e fixture.

The #377 Trading evidence clock is verified at three distinct seams. Pure tests
prove canonical capture/drain/corpus/candidate/future artifacts, exact funding
scope/sign, actual database-stamped receipt clocks, finite candidate selection,
append-only blind-batch continuity/health, deterministic block bootstrap and declared
power. PostgreSQL tests prove the irreversible `0335` cutover, the append-only
corpus -> candidate -> future capture -> future drain -> result parent chain,
one candidate/capture/drain/result constraints, grant-to-PROMOTE hard
link, database-recomputed complete future chains/health incidents, gap-free
bar/funding boundaries, News-source-to-Gate conservation, Case/Intent conservation, exact-release
fixed-window accounting, pre-window release registration against exact
Workers/Serve generations, append-only Nautilus process generations,
signed-tag/restart/canary release binding, and release-bound rollback snapshots
that require new Workers and Serve generations plus a binding reconciliation
heartbeat written by Nautilus query-first reconciliation after the new Workers
generation starts. The canonical fixed-window/release report also binds the
complete per-binding accounting digest.
Pure verifier tests own interpretation of receipt artifacts and the bounded
DB/runtime/Git facts; App integration tests use real PostgreSQL, raw artifact bytes,
and real Git identities to prove the public handler supplies those facts and cannot
replace a failed check. CLI contract tests keep one `release-register` transition and
one `trading evidence verify` entry with
receipt, lifecycle, seven-day window, release, and rollback subjects. No pure
test, fixture, local artifact, mock, or green CI job may stand in for future
calendar data, a human grant/arm, a venue-native write/flat receipt, or the
final fixed-window/rollback terminal.

### News V3 evaluation seams

News V3 has three public evaluation seams: `tracefold.news.eval.replay` for
deterministic source-classifier/Deduper/Gate regression (including same-kind
dedupe), pure `triage_rules.decide()` unit tests,
and the #112/#129 `CandidateEvaluator` for a whole semantic Program candidate.
The first two are code correctness tests; only the third can compare Program or
policy behavior against accepted production evidence. Runtime model identity
is part of the cohort and recording contract, not a supported candidate target.

Work from evidence, never directly from a complaint:

```bash
uv run tracefold news review queue --view coverage --hours 168
uv run tracefold news review queue --stratum model_drop --hours 168
uv run tracefold news review evidence TASK --version TASK_VERSION
uv run tracefold news review submit TASK --version TASK_VERSION \
  --file /tmp/rubric.json --idempotency-key UUID

# Measure before you change anything. This is the only command in the learning
# plane that needs no dataset, sandbox, tariff or container, and it writes
# nothing. Three modes, three different questions — pick the one you actually
# mean (#150 removed the ambiguous `live`):
#   recorded     — the persisted verdict against the action that shipped. No
#                  provider call, reproducible across policy revisions.
#   compile_live — the native Program GEPA optimizes on one task endpoint. No
#                  route fallback/deadline/breaker; per-call timeout and JSON
#                  format fallback remain.
#   runtime_live — the configured four-slot production Program route, run
#                  sequentially so circuit state means something.
uv run tracefold news learning baseline --from-ms START --to-ms END \
  --mode recorded --out /tmp/baseline.json
uv run tracefold news learning baseline --from-ms START --to-ms END \
  --mode runtime_live --max-model-cases 30 --out /tmp/baseline-runtime.json

# Draft the cases nobody has judged, over the ReviewDesk look-back window; a
# human accepts or rewrites every rubric before it becomes truth.
uv run tracefold news learning draft-reviews --model deepseek-v4-pro \
  --hours 24 --out /tmp/drafts.json

# The golden path (#253). Freeze once, then `run` — readiness, the standalone
# `compile_live` baseline over that exact corpus, and the one optimization,
# composed in one process over one dataset SHA and one configured judge route.
# It composes the three commands below and owns no second Objective Plan,
# Metric, split, budget or optimizer. Exit 0 means ADVANCE; 1 means NO_OP or
# REJECTED, both complete answers.
uv run tracefold news learning freeze --role development \
  --from-ms START --to-ms END --out artifacts/run-1/development.json
uv run tracefold news learning run --development DATASET_SHA \
  --out artifacts/run-1 --max-baseline-model-cases 80 \
  --max-metric-calls 100 --max-task-model-calls 150 \
  --max-reflection-model-calls 40 --max-metric-judge-model-calls 100 \
  --max-cost-microusd 500000 --max-call-cost-microusd 5000 --seed 112

# The same three legs one at a time, for a partial re-run or a cheap probe.
# `readiness` costs nothing and answers a blocked corpus for free.
uv run tracefold news learning readiness --development DATASET_SHA \
  --out /tmp/readiness.json   # 0 model calls, 0 writes
uv run tracefold news learning baseline --dataset DATASET_SHA \
  --mode compile_live --max-model-cases 80 \
  --semantic-judge deepseek-v4-pro --out /tmp/baseline.json
uv run tracefold news learning optimize --development DATASET_SHA \
  --out artifacts/optimize-1 \
  --max-metric-calls 100 --max-task-model-calls 150 \
  --max-reflection-model-calls 40 --max-metric-judge-model-calls 100 \
  --max-cost-microusd 500000 --max-call-cost-microusd 5000 --seed 112

# Only after `optimization_report.json` says outcome=ADVANCE, and only because a human
# decided to test the candidate on examples it was never optimized against.
uv run tracefold news release register --development DATASET_SHA \
  --candidate artifacts/run-1/optimization/prompt_candidate.json \
  --artifact-root /tmp/programs --out /tmp/candidate.json
uv run tracefold news release evaluate --development DATASET_SHA \
  --candidate /tmp/candidate.json --stage offline --live-program \
  --out /tmp/offline-report.json
```

The trusted root is the reader contract, rubric, accepted reviews, temporal
holdout membership, release thresholds, stable bundle/image and production
receipts. No optimizer or candidate path may write any of them. Validation and
holdout run stable and candidate sequentially because a different earlier
decision changes each arm's later would-reach-reader ledger. A candidate changes
exactly one target: the content-addressed Program or `decide()` policy. Program
candidates keep policy fixed; policy candidates reuse recorded semantic
judgments for the cheap development screen but still pass the later semantic
and production stages. Record/replay is exact at each Predictor request and
fails on an unrecorded request or runtime-model identity mismatch.

Issue #129 deliberately resets learning eligibility. Migration `0292` records
the initial `program_v1` epoch start from the database deployment clock;
`0293` preserves it and appends the corrected `program_v2` epoch after the
semantic fast-retry state bug was found in production proof. `0294` preserves
both prior rows and appends `program_v3` after the expert quality baseline and
semantic normalization change Program identity. `0295` preserves v1-v3 and
appends `program_v4` for the D-generation ownership hard cut; `0298` preserves
v1-v4 and appends `program_v5` for candidate-conditioned ToldContext. `0301`
preserves history and starts `program_v6` for
factory/executable v4, policy v10, `news_review_v4`, metric v4 and compiler
protocol/receipt v3. `0303` preserves history and starts the current
`program_v7` for factory/executable v5. Every earlier review, dataset, recording
and release receipt remains immutable audit history but is not optimizer,
validation, holdout or promotion evidence. New datasets require post-epoch
reviews and acceptance receipts bound to the exact stable Program bundle, so
quality evidence begins at zero. Issue #190 later reissues the sole bundle
inside v7 for canonical non-finite-number rejection, and Issue #193 reissues it
again as the single-document strategy artifact under factory v6; `0304` trips
open canaries and receipts that cut. `0305` admits the `compile_record`
artifact kind, keeps `compile_receipt` readable as history, and trips open
canaries a second time, because a candidate registered against the retired
receipt chain can no longer be evaluated. Those reissues do not change the
epoch or accepted `news_review_v4` truth. `0315` then records #288's exact
source route and factory-v7 hard cut without rewriting or appending the
`program_v7` epoch row. The append-only rows and prior bundles remain audit
history, but exact current-bundle acceptance makes factory-v6 evidence
audit-only and starts the factory-v7 eligible cohort at zero.

Current Review v6 retains exact gold for `trade_impact_breadth`, `trade_tradability`,
`trade_surprise`, `trade_development_delta`, `trade_channels`,
`trade_affected_markets` and `reader_value`, and adds exact Gold for all five
`news_taxonomy_v1` axes. Critical taxonomy cases require an independent
adjudication receipt before they enter release denominators. Work the fixed targeted strata
`local_macro_false_interrupt`, `systemic_macro_must_interrupt`,
`regional_direct_exception`, `scheduled_or_in_line_macro`,
`color_only_progression` and `macro_random_control`. Model drafts remain files;
only an explicit append-only review submission and acceptance receipt becomes
truth.

`learning run` (#253) is the one recommended GEPA path in this repository, and
the four questions it answers are the whole plane: are there accepted examples,
what did Stable score on them, did GEPA find an instruction worth testing, and
does that instruction still win on examples it never saw. It is a composition —
`readiness`, `baseline --dataset --mode compile_live`, `optimize` — into one
directory. Everything it adds is a check the three
commands could not make about each other: the equivalence judge is
`llm.news_compiler_reflection` rather than a model name retyped per command, the
corpus bound is checked against readiness before a provider call is spent, and a
corpus readiness already refused skips the baseline instead of paying for the
refusal twice.

Three baselines, three jobs, and #253 §3.2 exists because #225 published two of
them as if they were one number:

- **standalone baseline** — an independent physical `compile_live` run over the
  frozen corpus (`subsets.development_selection.case_macro_failure_as_zero`).
  It is for diagnosis, cost and repeatability.
- **GEPA seed baseline** — the seed Program's score inside the same GEPA run
  (`trajectory.val_aggregate_scores[0]`). This, not the standalone number, is
  the *before* the optimizer improved on.
- **future test baseline** — Stable on accepted examples that did not exist when
  the candidate was made (`release evaluate --stage holdout` against a
  ValidationDataset frozen strictly after registration). Only this one answers
  generalization.

The separate `run_summary.json` comparability projection is gone (#343): with
the three legs composed in one process over one dataset SHA and one configured
judge route, same-population holds by construction rather than by a fourth
document re-checking the other three. The standalone number lives in
`baseline-compile-live.json`, the GEPA seed number in
`optimization/optimization_report.json`, and the frozen dataset's own sealed
`coverage` counts in `readiness.json`. Two physical runs of one graph can still
differ in the last digits; that is a fact about two runs that happened, and it
is not on its own evidence that a dataset identity is wrong.

**When a corpus is big enough (#259).** Coverage decides it: independent
connected fact clusters by role, at least one safety case, the required strata
on both sides of the split, verified Prompt targets and Stable-correct controls.
A day count never did. `natural_days_min` is gone from the release profile; it
counted how many distinct UTC dates the accepted cases opened on, so two cases
two minutes apart across midnight were two and a hundred cases spread over 23 h
inside one date were one, and combined with the active-bundle filter it made a
Stable deployed this morning unusable until the calendar caught up. Phase A runs
as soon as `news learning readiness` says `ready`, whatever the age of the
bundle. `natural_day_n` and `window_duration_hours` remain in the dataset counts and
the readiness `coverage` block as diagnostics of case
concentration — read together, since a 72 h freeze whose reviews all landed in
one afternoon reads `1` and `72.0`; a corpus that concentrated is worth
*looking* at, and observing several settled days before trusting a result is
reasonable operator advice — but neither is a code admission contract.
Out-of-time generalization stays entirely with the Future Holdout:
a ValidationDataset frozen strictly after candidate registration, ≥ 24 h, with
its own eligible-Event and reviewed-cluster floors. The profile change is a hard
cut — it moves `TRUSTED_ROOT_SHA`, and the profile is named
`news_learning_release_v2` — so datasets and candidates frozen under v1 stay as
audit history and a new experiment re-freezes.

`learning optimize` (#202) is a cold, operator-invoked GEPA workflow, not a
Worker and not a release gate. It reads the frozen development corpus once as
`serve`, then holds nothing but three model endpoints and a typed budget: no
database write credential, no broker, no delivery, no canary, no promotion. It
ends in `NO_OP`, `REJECTED` or `ADVANCE`; all three write a complete
`OptimizationRunReport`, and only `ADVANCE` also writes a
`news_prompt_candidate_v1` — two Predictor instructions, and nothing that could
ask to ship.

Until #202 there were two generation paths and two candidate types, because
release eligibility came from *where* a candidate was produced: a sealed
sealed image against a metered proxy, or the fast loop behind
`promotable=false`. The generator was never the authority for two strings.
`release register` now binds any Prompt patch — GEPA's or a person's — to the
active stable Program and a frozen dataset, re-derives the #199 Objective Plan
rather than trusting the candidate's own summary, and refuses anything that
disagrees. An `ADVANCE` is still not a release: future holdout, blind pairwise,
shadow, canary and a human promotion are unchanged.

`learning snapshot | compare` — the #193 research fast loop of frozen run
directories and per-arm comparison — was deleted in #343, along with the
`tracefold.news.learning.experiment` package that carried it; `optimize` over a
frozen corpus is the one research entry left.
`tests/architecture/test_news_optimizer_boundary.py` names the one CLI module
allowed to load the optimizer in process, and asserts what the optimizer itself
can reach: no database session, no review plane, no canary, no promotion.

`learning baseline` (#143) is the step that has to come first and did not exist
until then: a cold, read-only run of the native Program over the same `decide()`
and the same `accepted_review_metric`, so the number an operator reads before a
prompt edit is the number GEPA will later try to maximize. It needs no dataset,
sandbox, tariff or container and writes nothing. `compile_live` and GEPA execute
the same `NativeNewsProgram`, two Predictor bindings, stock JSONAdapter and task
endpoint. The SemanticJudge adapter needed by the report disables the
whole-route deadline and cross-case breaker that GEPA does not run; the endpoint
retains its per-call timeout. There is no fallback slot.

Two facts about the optimizer are worth stating plainly, because both were
invisible while its only tests drove a fake GEPA:

- **The write-set is two strings, and there is nowhere else to write.** Public
  `dspy.GEPA` returns an optimized native Module; `run_gepa` reads only the
  `event_semantics` and `reader_card` named Predictor instructions and refuses
  any demos or extra Predictor. There is no demo bank.
- **A rejected instruction spends no task-model call.** `NativeNewsProgram`
  validates the candidate artifact and shared growth budget at the start of
  both sync and async entry points. That guard therefore covers proposal,
  mutation and merge before either Predictor; the bounded refusal becomes
  metric feedback rather than a candidate or an `ADVANCE` terminal.

The reflection endpoint is configured separately from the task endpoint
(`llm.news_compiler_reflection`) with its own 32k-token, 300 s, temperature-1.0
budget. Passing one endpoint for both made the local student its own teacher,
capped a proposed instruction at the task route's 1,200 tokens — far below what
the instruction bound accepts (8,192 estimated tokens since #306; 2,048 in the
advisory era this incident dates from) — and pointed a multi-hour run at the same
single-slot GPU that serves production Triage. The code-owned
`InstructionProposer` (named `RulePackAwareProposer` until #306 retired the
RulePack layering) puts the candidate's complete current instruction in front
of the reflection model; before it, `<curr_param>` was one space and the model
was rewriting 8.5 KB of rules it could not see.

The optimization has three typed roles, not copied adjacent scalars: task,
reflection and `metric_judge`, each one `ModelExecutionIdentity`. Secret-free
identity, grant, bundle and proxy enforcement derive from that single object,
whose only digest is `endpoint_fingerprint` — the endpoint URL travels beside a
credential and so is fingerprinted rather than stored. The three-level
`endpoint_sha256 -> model_sha256 -> binding_sha256` chain it replaced hashed
values printed immediately below it: a verifier holding the object never needed
them, and one without the object could not use them. The judge is
explicitly constructed for headline/why/factual semantic equivalence and binds
model/endpoint, instruction/schema, JSONAdapter, max tokens, timeout,
temperature, LM kwargs, cache and retry. Its calls, cost and unavailable
failures are separate facts inside the compile record; unavailable enters the
affected free-text dimension as failure-as-zero, never byte equality, hidden
retry or cache.
Use #148's measured same-output ruler delta `+0.060662`; the earlier roughly
`+0.13` simulation is not release evidence.

`learning baseline` answers one question per mode, and #150 exists because the
first version answered two under one name. `compile_live` is the optimizer's
object; `runtime_live` is the reader's. The same broken ReaderCard answer is
fatal to the first and survivable on the second, so their failure rates are not
comparable and the report says which contract it executed in `execution_scope`.

The report publishes no single ambiguous number. `case_macro_answered` is
quality given an answer; `case_macro_failure_as_zero` is the end-to-end lower
bound; they differ by exactly the unanswered cases. The v1 report computed only
the first, so 29 provider failures turned a 0.482 lower bound into a published
0.587 by disappearing from the mean. `review_label_distribution` is what
reviewers labelled and is byte-identical however predictions change;
`prediction_dimensions` is what the candidate did. Read the second when
comparing two runs. The label distribution is grouped by which Predictor's
score each label feeds, so `timeliness` is visible under `not_scored` —
operators keep labelling it, and it is no longer scored against EventSemantics,
which has no field that could repair it. A hard-gated case keeps its action and
its per-dimension outcomes, so its zero enters every denominator: leaving them
out let a candidate with more hard failures publish a higher hit rate.

Policy travels with the example. `policy_metric.policy_values` plus
`policy_sha256` is the exact arm policy, verified before `decide()` replays it,
and a missing or tampered policy raises rather than scoring — a corpus that
cannot verify its own policy is a construction bug, and scoring it 0 would blame
the Program for it.

`tests/fixtures/news_audit_replay_corpus_v2.json` is immutable pre-hard-cut
audit evidence. It is archive-only: no ordinary test, current metric, dataset,
Program, or release gate imports it, and there is no current generator or
recorded-mode reader for its legacy judgment shape. The current-contract
architecture guard names the file and its historical keys explicitly; any new
consumer or additional legacy reference fails CI. Current metric-v6 evidence is
built only from exact `news_judgment_v2` rows in the active epoch.

The tariff is not optional bookkeeping. Neither the local llama.cpp endpoint nor
DeepSeek returns a price litellm can resolve, so `provider_cost_microusd` is
`None` for every endpoint this project uses and the budget meter fails closed
without a trusted worst-case rate. Note also that `_BudgetMeter` reserves
`max_call_cost_microusd` for every call, so the reachable call count is
`max_cost / max_call_cost`: the two limits look independent and are not.

The metric scores the **reader-facing final action**, not a model-owned action
proxy. Each sealed episode carries one `ScoredJudgment` and a frozen
policy projection — objective Gate facts plus the ordered sent ledger — so the
shared pure/version-bound `production_decision()` returns the complete
`DecisionResult` used by failure-cluster selection, baseline, the optimizer and
CandidateEvaluator. The projection contains no queue priority, provider score,
macro lexicon or control state. Grounded restatement, stale-source, similarity,
listing/telemetry and watchlist guards can all differ from model intent, so an
offline gain measured on editorial relevance alone could not predict what the
reader would see.

Hard gates come first and are not averaged with anything: `must_push` miss,
`must_hold` send, background sent realtime (objective guards separated), schema
invalidity, ungrounded primary, factual contradiction, relevance inconsistency,
a card carrying a URL or describing its writer as a model, or known duplicate
leak scores the example zero. What survives is metric v6: 45% final production
action, 35% exact TradeRelevance dimensions, 10% semantics/novelty, 10%
ReaderCard reviewer anchors and 10% the deterministic ReaderCard copy lint.
Every failed scored dimension needs exact expected gold; without it the field is
not scored — the lint is the one component that needs no reviewer label at all,
which is why it is the only card evidence an unlabelled case carries. Reports publish per-component
denominators, effective weight mass, gold coverage and field n. `pred_name`
never changes the score; it only routes owned feedback. Listing/telemetry are
excluded from relevance scoring, and watchlist-guard action feedback cannot ask
a Predictor to repair code-owned policy. The receipt binds weights, complete
helper source root, policy, schema, corpus and full judge execution identity.

Before the honest split, each connected fact cluster elects exactly one optimizer
representative. A target beats a control; otherwise the representative prefers
more target dimensions, safety status, the newer Event and the
stable case id. Non-elected Events remain in the frozen corpus and readiness
audit as `cluster_representative_shadowed:*`, but contribute no second metric
weight. Representatives are split into disjoint halves by connected fact
cluster: ordered by Event time then stable cluster id, the earlier 70% to GEPA's
`trainset` and the later 30% to its `valset` — no shuffle, no seed. The
predecessor passed the same list object to both and said so in its own receipt
(`same_object_as_trainset`), which proves nothing about generalization. Both
halves must independently carry a target, a control, safety cases, both action labels and novelty
cases; representative election never relaxes those gates, so lost coverage
fails the compile closed. Objective Plan v2 and split receipt v2 record counts,
representative policy, cluster roots, coverage and an explicit disjointness
proof.

`optimization_objective_summary.v2` also carries the Objective Plan schema and
the representative case ids/count/root. Registration re-derives and compares
that population and rejects a candidate that
declares split roots under an older or missing plan identity; historical
artifacts remain readable evidence but cannot be re-armed under the new metric
population.

Retrieval is scored on its own and cannot be hidden by the scalar: for every
accepted `restatement` whose `duplicate_of` was inside the bounded window, the
receipt reports target recall and the selected rank. "The model called it new"
and "the model was never shown the card" are different defects with different
fixes.
Every invocation states metric/model/total-cost limits, a per-call cost ceiling
and a seed. The job cannot write accepted truth, register a candidate, alter
trusted Program state, accept, deploy or promote — `release register` is a
separate command with a separate credential, and it re-derives the Objective
Plan rather than trusting what the candidate declares.

The compile record was the second half of #193. Seven content-addressed receipts, a
chain root, a runner receipt, an optimizer provenance record and a machine diff
became one document, embedded whole and addressed by `compile_record_sha256`,
which was also its `news_learning_artifacts` key. #202 removed the compile
itself, and with it that record, the sealed input bundle, the sidecar ledger,
the `CompilerBuildAttestation` and the tariff — documents that proved *where*
two advisory instructions were produced, which is not what makes them safe to
ship. The rule those cuts were decided by still holds and is worth keeping: a
digest survives only if it addresses independently stored bytes, crosses a real
trust boundary, is a durable key, fingerprints an external mutable identity that
cannot be stored whole, or serves a consumer that cannot read the parent
payload. Everything else is computed and verified by the same code in the same
process over a payload sitting next to it — a self-proof, not an attestation.

What replaced provenance is binding, checked by a party that did not produce the
candidate. `ProposalReceipt` names the registered `news_prompt_candidate_v1` by
`prompt_candidate_sha256` and carries the registrar's *own*
`development_episode_projection_root_sha256`; `release register` re-applies the
patch to derive the arm's Program identity and re-derives the #199 Objective
Plan rather than trusting the candidate's summary. Migration `20260825_0307`
admits the new kind, keeps `compile_receipt` and `compile_record` readable as
audit history, and trips open canary activations whose candidate was registered
against the old contract.

Promotion requires sealed PASS artifacts in order: development, future
temporal validation, blind pairwise, 24 h shadow, deterministic 10% canary,
then stable deployment. `release canary trip` is the fail-closed rollback
control. Canary selector `news_canary_selector_v2` includes queue-high Events, excludes recovery,
listing and telemetry lanes, and validates selector/profile/runtime-manifest
identity at startup, resume and assignment. The migration and tests establish this mechanism; they do not prove a
candidate is better. Production proof begins only after the minimum reviewed
boundary/retention/negative clusters and future observation windows exist.
The v6/v10 hard cut therefore makes no cross-generation quality-uplift claim;
its evidence starts from zero. It retains two normal serial Predictor calls and
creates separable semantic/copy feedback behind the unchanged
`SemanticJudge.judge()` Interface.
Broker behavior is covered by `tests/integration/test_news_bus_rabbitmq.py`
against the compose RabbitMQ (`TRACEFOLD_TEST_AMQP_URL`, default
`amqp://tracefold:tracefold@127.0.0.1:5672/`; skipped when unreachable); every
test declares its own `tf_test_<id>`-prefixed topology and deletes it on
teardown, so the operator queues are never touched. Run the focused lane with:

```bash
uv run pytest -q tests/news tests/integration/test_news_v3_pipeline.py \
  tests/integration/test_news_v3_consumers.py tests/integration/test_news_bus_rabbitmq.py
```

Transport/status acceptance records disconnect, overflow, process outage, and
planned shutdown with distinct causes. Database backpressure must retain WSS;
overflow records an incident without falsely disconnecting it. Reconnect
restores current state independently of bounded official Strategy list/hits
recovery. Tests prove overlap idempotency, complete/partial/unavailable status,
and that the provider news-search endpoint never appears in the production
recovery seam.

## Generated contracts

`docs/generated/` contains only reproducible outputs: `README.md`,
`cli-help.md` (`scripts/regen_cli_help.py`), `db-schema.md`
(`scripts/regen_db_schema.py`, needs PostgreSQL), and `openapi.json`
(`scripts/regen_openapi.py`, paired with `web/src/lib/types/openapi.ts`).

```bash
make docs-generated   # db-schema.md + cli-help.md
make regen-contract   # openapi.json + web/src/lib/types/openapi.ts
```

`AGENTS.md` and `CLAUDE.md` share one generated block, produced from
`docs/agents/shared-router.md` by `scripts/sync_agent_router.py --write`.
The portable task-worktree lifecycle lives only in
`docs/agents/worktrees.md`; tool-specific router appendices link to it rather
than copying it.

Each generated artifact has one source of truth and one update command:

- CLI help: the production parser rendered by `scripts/regen_cli_help.py`;
- agent routers: `docs/agents/shared-router.md`, updated with
  `scripts/sync_agent_router.py --write`;
- OpenAPI and TypeScript: the Python app schema, updated with
  `make regen-contract`.

`make check` executes the database-free CLI-help, agent-router canonical, and
mandatory agent/operator documentation-link checkers exactly once each. Its
Python OpenAPI check is hermetic; Node-backed TypeScript codegen runs in the
external-codegen/full lane. Generated outputs change in the same commit as
their source. Regenerate the owning output only through its generator and
inspect its diff before committing.

## Completion

A change is complete only when:

- observable behavior and durable invariants have direct successful evidence;
- generated outputs are current;
- public contracts and PostgreSQL fact semantics remain intact or change
  through an explicitly approved migration;
- old names, files, imports, and compatibility paths are gone;
- deployment/cutover evidence is recorded for runtime changes;
- omitted lanes and remaining risks are named without manufacturing green
  results through skips or compatibility mocks.
