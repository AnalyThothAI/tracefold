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

The business capability is exported from `tracefold.news`. Code outside the
owning package imports only that root.
Keep internal modules cohesive and move behavior behind the root interface
instead of adding forwarding modules, aliases, or compatibility packages.

Private application composition and concrete provider adapters may use only
the exact package-private implementation seams named in the architecture
harness—for example, to construct an owned repository or reuse a pinned parser
behind a public protocol. This is implementation wiring, not a caller-facing
interface: public models/protocols still come from the package root, and new
private import edges fail until deliberately reviewed and enumerated.

PostgreSQL material facts and public HTTP/CLI contracts are migration
boundaries. Internal Python imports are not compatibility contracts. Hard cuts
delete the old path and update all consumers in the same change.

## Tests

| Lane | Location | Proves |
|---|---|---|
| Architecture | `tests/architecture/` | package shape, dependency direction, durable ownership |
| Contract | `tests/contract/` | public HTTP/CLI and generated schemas |
| Integration | `tests/integration/` | real PostgreSQL and composed service behavior |
| Golden | `tests/golden/` | curated fact-to-product expectations |
| E2E | `tests/e2e/` | one served process: `/readyz`, `/api/status`, `/api/news/status` shapes, retired routes `404` |
| Frontend | `web/tests/` | UI, route, model, and frontend architecture behavior |

Prefer behavior at a maintained public or persistence seam. Do not preserve
tests that assert private file layout, source text, mock call choreography, or
implementation detail. There is no coverage-percentage gate.

Select commands by risk:

- schema or repository behavior: focused real-PostgreSQL integration tests;
- HTTP/CLI behavior: contract tests plus regenerated artifacts;
- workers: claim, lease, retry/terminal, restart catch-up, idempotency,
  single-writer, and external-I/O transaction boundaries;
- UI: scoped tests, lint, typecheck, build, and a browser check when visual or
  interactive behavior changes;
- documentation: bounded surface and link checks;
- generated files: run the owning generator and verify a clean second run.

`make check` is a fast static/frontend/architecture/contract bundle, not a
universal completion mandate. Run only the additional lanes that cross the
changed seam and report omitted evidence honestly.

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
  tests/architecture/test_onboarding_surface.py \
  tests/integration/test_cli.py \
  tests/integration/test_compose_postgres.py
```

### Test lanes and speed

`make test` is the default regression (unit + architecture + contract +
integration, excluding `slow`, `e2e`, and `golden`; ~4 minutes on an idle
machine with the local test PostgreSQL). `make test-slow` runs the real-process
Workers runtime tests (`tests/integration/test_workers_runtime_v2.py`, bounded
by wall-clock deadlines, ~2 minutes), `make test-e2e`/`make test-golden` the
service and corpus lanes, and `make test-all` everything (~6.5 minutes).
Integration tests reset the schema per test through `prepare_postgres_database`
only when they seed data; validation/auth-only API tests reuse the migrated
head. Historical migration-path tests are narrow and explicit: they cover the
preservation/grant cuts that carry user evidence forward and the `0292` to
`0292` to `0293`, `0293` to `0294`, and `0294` to `0295` append-only Program
epoch transitions. The Alembic chain is the
`20260818_0275` current-schema baseline plus the linear revisions through
`20260822_0297`; schema tests also run against that migrated head. The e2e lane
(`tests/e2e/test_golden_path.py`) starts one
uvicorn Serve subprocess against a freshly migrated testcontainers PostgreSQL
and asserts `/readyz`, the `/api/status` and `/api/news/status` shapes, and
that the retired GMGN-lane and Macro routes answer `404`; it runs no Workers
subprocess. ReviewDesk and CandidateEvaluator have their own integration lanes;
production shadow/canary evidence is sealed in PostgreSQL rather than inferred
from the HTTP e2e fixture.

### News V3 evaluation seams

News V3 has three public evaluation seams: `tracefold.news.eval.replay` for
deterministic Deduper+Gate regression, pure `triage_rules.decide()` unit tests,
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

uv run tracefold news learning freeze --role development \
  --from-ms START --to-ms END --out /tmp/development.json
uv run tracefold news learning compile --development DATASET_SHA \
  --artifact-root /tmp/programs --out /tmp/program-proposal.json \
  --compiler-image sha256:FULL_LOCAL_DOCKER_IMAGE_ID \
  --max-metric-calls 100 --max-task-model-calls 150 \
  --max-cost-microusd 500000 --seed 112
uv run tracefold news learning propose --development DATASET_SHA \
  --file /tmp/program-proposal.json --out /tmp/candidate.json
uv run tracefold news learning evaluate --development DATASET_SHA \
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
appends `program_v4` for the D-generation factory/artifact and optimizer
ownership hard cut. Every earlier review, dataset, recording and release receipt
remains immutable audit history but is not compiler, DemoBank, validation,
holdout or promotion evidence. New datasets require post-epoch reviews and
acceptance receipts bound to the exact stable Program bundle, so quality
evidence begins at zero.

`learning compile` is a cold, operator-invoked DSPy GEPA workflow, not a Worker
and not a release gate. The trusted side seals the exact current development
corpus; an isolated runner sees neither DB nor holdout and can write only a
bounded `ProgramPatchV2` for LearnedStrategy and eligible Demo references.
The operator config must contain one complete, positive
`llm.news_compiler_tariff`; every invocation pins the exact local compiler
image ID and states metric/model/total-cost and resource limits plus a seed. The
trusted applier revalidates the complete receipt chain and constructs canonical
state-only JSON from the exact stable root. The runner cannot write accepted
truth, register a candidate, alter trusted Program state, accept, deploy or
promote.

Promotion requires sealed PASS artifacts in order: development, future
temporal validation, blind pairwise, 24 h shadow, deterministic 10% canary,
then stable deployment. `learning canary trip` is the fail-closed rollback
control. The migration and tests establish this mechanism; they do not prove a
candidate is better. Production proof begins only after the minimum reviewed
boundary/retention/negative clusters and future observation windows exist.
The initial hard cut therefore makes no quality-uplift claim. Its immediate
tradeoff is one normal provider call becoming two serial Predictor calls; its
future leverage is separable semantic/copy feedback, demonstrations, routing
and fine-tuning, all behind the unchanged `SemanticJudge.judge()` Interface.
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

`make check` runs only the database-free `regen_cli_help.py --check` drift
check. Generated OpenAPI and frontend types change in the same commit as their
source.

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
