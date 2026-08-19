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
head. There are no historical migration-path tests: the Alembic chain is the
`20260818_0275` current-schema baseline plus `20260818_0276`,
`20260818_0277`, and `20260819_0278`, and schema tests run against that
migrated head. The e2e lane (`tests/e2e/test_golden_path.py`) starts one
uvicorn Serve subprocess against a freshly migrated testcontainers PostgreSQL
and asserts `/readyz`, the `/api/status` and `/api/news/status` shapes, and
that the retired GMGN-lane and Macro routes answer `404`; it runs no Workers
subprocess. There is no
acceptance-bundle, collector, or sealing workflow; runtime evidence comes from
the maintained lanes above.

### News V3 evaluation seams

News V3 is evaluated through public seams: `tracefold.news.eval.replay`
(`tracefold news replay <hits.json>` runs Deduper+Gate in memory over provider
hits; `tests/fixtures/news_v3_hits_sample.json` is the real, redacted golden
corpus), `tracefold.news.triage_rules.decide()` (pure post-rules with an
optional `DecidePolicy`, golden-tested in `tests/news/test_news_v3_pure.py`),
and `tracefold.news.eval.offline` (`tracefold news eval` scores stored verdicts
against `news_event_labels` only; `tracefold news replay-decisions` re-runs
`decide()` with a candidate policy and no model).
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
