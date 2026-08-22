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

# Measure before you change anything. This is the only command in the learning
# plane that needs no dataset, sandbox, tariff or container, and it writes
# nothing. `--mode recorded` scores the verdict production actually persisted
# against the action it actually shipped, so it costs no provider call and stays
# reproducible across policy revisions; `--mode live` re-runs the Program and is
# the only mode that can measure a Prompt or RulePack change.
uv run tracefold news learning baseline --from-ms START --to-ms END \
  --mode recorded --out /tmp/baseline.json

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
appends `program_v5` for the candidate-conditioned ToldContext factory and its
ownership hard cut. Every earlier review, dataset, recording and release receipt
remains immutable audit history but is not compiler, DemoBank, validation,
holdout or promotion evidence. New datasets require post-epoch reviews and
acceptance receipts bound to the exact stable Program bundle, so quality
evidence begins at zero.

`learning compile` is a cold, operator-invoked DSPy GEPA workflow, not a Worker
and not a release gate. The trusted side seals the exact current development
corpus; an isolated runner sees neither DB nor holdout and can write only a
bounded `ProgramPatchV2` for LearnedStrategy and eligible Demo references.

`learning baseline` (#143) is the step that has to come first and did not exist
until then: a cold, read-only `dspy.Evaluate` over the same graph, the same
`decide()` and — literally the same function object — the same
`accepted_review_metric`, so the number an operator reads before a RulePack edit
is the number GEPA will later try to maximize. It needs no dataset, sandbox,
tariff or container and writes nothing. Two source facts about the optimizer are
worth stating plainly, because both were invisible while the compiler's only
tests drove a fake GEPA:

- **`dspy.GEPA` never writes demos.** Its `build_program` only assigns
  `pred.signature = pred.signature.with_instructions(...)`. `DemoBank`,
  `EligibleDemoBank` and `demo_refs` are therefore always empty under this
  optimizer — a recorded property, not a defect to chase. Demos would need a
  `BootstrapFewShot` pass after GEPA, and only then would the metric need the
  tutorial's `if trace is not None: return score >= 1.0` branch.
- **GEPA matches traces to components by signature equality**
  (`t[0].signature.equals(module.signature)`). `_OptimizerOwnedPredictor` renders
  RulePacks plus the advisory into a fresh inner `dspy.Predict` and delegates, so
  the trace records a signature the outer one never equals; the compiler re-keys
  those two entries positionally. Without that, `make_reflective_dataset` raises
  "No valid predictions found for any module" and the reflective loop cannot
  propose anything at all.

The reflection endpoint is configured separately from the task endpoint
(`llm.news_compiler_reflection`) with its own 32k-token, 300 s, temperature-1.0
budget. Passing one endpoint for both made the local student its own teacher,
capped a proposed instruction at the task route's 1,200 tokens — below what
`LearnedStrategy` itself accepts — and pointed a multi-hour run at the same
single-slot GPU that serves production Triage. A code-owned
`RulePackAwareProposer` puts the full rendered instruction in front of the
reflection model as read-only context; before it, `<curr_param>` was one space
and the model was rewriting 8.5 KB of rules it could not see.

The tariff is not optional bookkeeping. Neither the local llama.cpp endpoint nor
DeepSeek returns a price litellm can resolve, so `provider_cost_microusd` is
`None` for every endpoint this project uses and the budget meter fails closed
without a trusted worst-case rate. Note also that `_BudgetMeter` reserves
`max_call_cost_microusd` for every call, so the reachable call count is
`max_cost / max_call_cost`: the two limits look independent and are not.

The metric scores the **reader-facing action**, not the model's intermediate
`decision` field. Each sealed episode carries a frozen policy projection — Gate
facts plus the ordered sent ledger — so the metric assembles the predicted
verdict, computes the final storyline key, and runs the exact production
`decide()`, which since #137 has no operational input at all — every path it
takes is editorial, which is exactly the property the metric needs, because a
card silenced by an operator would not be evidence that its editorial judgment
was wrong. The sealed projection carries no control state either. `decision` is
only an intent: a grounded restatement drop, a similarity throttle, a contested
high-priority rescue or a watchlist rescue all override it, so an offline gain
measured on `decision` could not predict what the reader would see.

Hard gates come first and are not averaged with anything: a `must_push` miss, a
`must_hold` send, a schema failure, an unchanged card the reviewer called
factually wrong, or an ungrounded primary asset each score the example zero. The
predecessor averaged every check flat, so four retention anchors agreeing could
outweigh a dangerous miss. What survives the gates is weighted 50% final action,
35% accepted `EventSemantics` dimensions, 15% accepted `ReaderCard` fidelity;
a missing or `uncertain` label leaves that component's denominator rather than
counting as a pass. Feedback is routed per Predictor, so neither is asked to
repair a failure it cannot cause. The metric receipt binds the weights, the
policy identity and values, and the review rubric version.

Accepted development is split into disjoint halves by connected fact cluster:
clusters ordered by their own latest Event time then by stable cluster id, the
earlier 70% to GEPA's `trainset` and the later 30% to its `valset` — no shuffle,
no seed, and a cluster is never divided. The predecessor passed the same list
object to both and said so in its own receipt (`same_object_as_trainset`), which
proves nothing about generalization. Both halves must independently carry safety
cases, both action labels and novelty cases; a half that cannot detect the
regressions it exists for fails the compile closed. The receipt records counts,
cluster roots, coverage and an explicit disjointness proof.

Retrieval is scored on its own and cannot be hidden by the scalar: for every
accepted `restatement` whose `duplicate_of` was inside the bounded window, the
receipt reports target recall and the selected rank. "The model called it new"
and "the model was never shown the card" are different defects with different
fixes.
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
