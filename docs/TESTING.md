# Testing and CI implementation

The stable risk policy is owned only by
[Risk-tiered local verification](DEVELOPMENT.md#risk-tiered-local-verification).
This document records the current lane wiring, resource topology, native report
contract, platform enforcement, and measured implementation facts. Changing a
job here does not change when developers should run broad local checks.

## Fixed full CI implementation

`.github/workflows/ci.yml` runs the same fixed required plan for pull requests
targeting `main`, `main` pushes, releases, and manual dispatches. It has no path,
draft, commit-message, or impact-selection bypass. Pull-request concurrency
cancels only an older run for the same PR; a cancelled run is not green.

The current owners are:

| Job | Make target | Isolated resources | Native results |
|---|---|---|---|
| `quality-static` | `ci-quality-static` | none | pytest JUnit |
| `python-hermetic` | `ci-python-hermetic` | none | pytest JUnit |
| `postgres-behavior` | `ci-postgres-behavior` | PostgreSQL and RabbitMQ | pytest JUnit |
| `migration` | `ci-migration` | PostgreSQL | pytest JUnit |
| `runtime-broker` | `ci-runtime-broker` | PostgreSQL, RabbitMQ, and a disposable broker container identity | pytest JUnit |
| `deploy-e2e` | `ci-deploy-e2e` | PostgreSQL, Node, and a job-local testcontainer | pytest JUnit |
| `test-integrity` | `ci-test-integrity` | Node | pytest JUnit |
| `frontend` | `ci-frontend` | PostgreSQL, RabbitMQ, Node, and Chromium | pytest JUnit, Vitest JSON, and Playwright JSON |

Each job checks out `TESTED_SHA` (`pull_request.head.sha` for a PR,
`github.sha` otherwise), asserts the checkout SHA, installs from locked Python
and Node dependency state where needed, and uses SHA-pinned Actions plus
digest-pinned service images. PostgreSQL, RabbitMQ, processes, ports, and
browser state are job-local; fixed jobs do not share a destructive database or
broker.

Required pytest runs disable plugin autoload, load only named plugins, select
the deterministic Hypothesis `ci` profile, enforce strict xfail and no early
`maxfail`, record the slowest 50 cases, and emit JUnit under
`artifacts/test-results/`. Required Vitest and Playwright runs emit their native
JSON reports with focus, expected failure, retry, repeat, pending, snapshot
update, and empty-run escapes rejected. `scripts/require_test_reports.py`
checks only that the native report exists, executed tests, and contains no
non-green outcome; it does not select, inventory, or re-adjudicate tests.

`test-effectiveness` is report-only. It combines standard coverage.py data
already produced by the fixed jobs, reruns no test, is absent from `ci-gate`
dependencies, and uses `continue-on-error` so coverage cannot decide whether a
workflow or deployment is green.

`ci-gate` is the single merge interface. It has no checkout, services,
artifacts, or project script: it reads each required `needs.<job>.result` and
succeeds only when all are exactly `success`. The active strict
`main-production-verification` repository Ruleset requires this check for the
latest `main`-targeting PR HEAD, permits squash merges only, and has no bypass
actor. `scripts/require_main_ci.py` separately requires a successful run of
this workflow and check from a `main` push for the exact deployment SHA.

The fixed-plan and native-report hard cut originated in #353. #373 added the
installed-distribution smoke, historical migration walk, business failure
windows, report-only coverage, and scheduled mutation separation. These Issue
references explain the current wiring; they do not define a second policy.

### Audited #428 baseline and fixed split

The successful pre-split baseline is
[`main@3a9a84f`, run 33346454722](https://github.com/AnalyThothAI/tracefold/actions/runs/33346454722).
The workflow took 13m53s wall-clock. Its `runtime-process` job took 13m02s,
and its test step took 12m15s. The native JUnit report recorded 204 passing
nodeids in 731.349 seconds, with no failure, error, or skip.

The exact required-job timings were:

| Job | Job wall | Pre-test bootstrap | Required step | Native runner outcomes |
|---|---:|---:|---:|---|
| `quality-static` | 2m16s | 19s | 1m52s | 311 pytest testcase rows in 77.703s |
| `python-hermetic` | 2m14s | 14s | 1m56s | 1,622 pytest testcase rows in 110.972s |
| `postgres-behavior` | 8m13s | 35s | 7m32s | 363 pytest testcase rows in 447.493s |
| `migration` | 2m12s | 29s | 1m37s | 58 pytest testcase rows in 93.201s |
| `runtime-process` | 13m02s | 42s | 12m15s | 204 pytest testcase rows in 731.349s |
| `frontend` | 2m36s | 1m20s | 1m09s | 1 pytest, 234 Vitest, and 1 Playwright outcome |

Bootstrap is the job start through the required-step start, so it includes
job-local services, checkout, tool setup, locked `uv sync`, and `npm ci` where
applicable. The frontend native reports further record: pytest 1/1 in 1.005s;
architecture Vitest 26/26 in 3.522s wall-clock; unit Vitest 208/208 in 23.654s
wall-clock; and Playwright 1 expected result in 1.965s. Across all native
reports there were 2,794 testcase/result rows, zero failure, error, skip,
pending, todo, unexpected, flaky, retry, xfail, or xpass outcomes.

The runtime report preserves the complete per-test timing data and the job log
prints the slowest 50. Its ten longest cases were 196.497s dead-letter
recovery, 104.461s price-corpus sizing, 65.664s transient-failure budgeting,
29.903s broker restart in a retry window, 13.009s never-returning worker
control, 12.729s golden production pipeline, 7.886s transient heartbeat
recovery, 7.694s broker restart mid-flight, 7.578s publication-preserving
SIGTERM, and 7.538s Serve readiness. The linked run and its native artifacts
are the full top-50 and outcome evidence; no profile database was created.

The run used Python 3.13, Node 22, and uv 0.11.7. PostgreSQL was
`postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296`;
RabbitMQ was
`rabbitmq:4.3.5-management-alpine@sha256:e2f08f846de10bb09649a8b020f286ed362a8f72ee45e5a8d043851f1533fda8`.
Every service, port, checkout, browser, and restartable broker identity was
job-local.

The hard cut preserves that exact required nodeid union and every marker.
`runtime-broker` owns 27 broker, worker-process, and golden nodeids;
`deploy-e2e` owns 115 deployment, end-to-end, capacity, and Nautilus nodeids;
`test-integrity` owns 62 pytest, frontend-runner, and hook fail-closed nodeids.
Their path selections are fixed in the Makefile, their reports are independent,
and all three results are required directly by `ci-gate`. No case was moved to
a scheduled or optional workflow. The only acceptance-test source changes
repair assertions for the renamed CI owners; they change no test nodeid,
marker, production-risk mechanism, or outcome.

The changed acceptance tests are contract repairs, not detector changes:

| Source | Classification |
|---|---|
| `tests/architecture/test_docs_surface.py` | replaces the retired Make owner with all three fixed owner targets |
| `tests/contract/test_verification_gate_contract.py` | hard-cuts required jobs, resource topology, native targets, and fail-closed gate results to the new owners |
| `tests/deploy/test_postgres_deployment.py` | transfers the pinned PostgreSQL image assertion from `runtime-process` to `runtime-broker` and `deploy-e2e` |

The `slow` audit is exhaustive at the source-owner boundary:

| Source | Slow nodeids | Disposition and retained seam |
|---|---:|---|
| `tests/integration/test_news_bus_rabbitmq.py` | 3 | required: real delayed retry, broker restart, and dead-letter recovery |
| `tests/integration/test_news_durable_event_plane.py` | 1 | required: real RabbitMQ restart with durable PostgreSQL convergence |
| `tests/integration/test_workers_runtime_v2.py` | 21 | required: real process, lock, timeout, readiness, failure, and shutdown windows |
| `tests/test_workers_probe.py` | 1 | required: readiness remains responsive during blocked metrics rendering |
| `tests/integration/test_news_status_scale.py` | 1 | required: production-sized status corpus against real PostgreSQL |
| `tests/integration/test_news_v3_price_scale.py` | 6 | required: Serve statement-timeout capacity gates against real PostgreSQL |
| `tests/integration/test_nautilus_config.py` | 6 | required: pinned public Nautilus construction and lifecycle contract |
| `tests/contract/test_hook_installer.py` | 3 | required harness seam: the installed frontend hooks execute from the locked project |
| `tests/slow/test_frontend_harness_fail_closed.py` | 52 | required harness seam: Vitest and Playwright cannot turn non-green outcomes into green |
| `tests/slow/test_required_pytest_fail_closed.py` | 7 | required harness seam: pytest reports reject skips, xfails, xpasses, failures, and collection faults |

All 101 current `slow` nodeids therefore remain required. The 103 additional
deployment, E2E, and golden nodeids also remain in the fixed plan. There is no
deletion, scheduled reclassification, mock substitution, or duplicate owner.

## Local lane implementation

`make test` aliases `make test-fast`. Both run the broad hermetic Python
selection and start no external resource. They are final-checkpoint tools, not
the edit loop. Focused execution uses pytest and Vitest directly; the repository
does not provide a changed-file planner, nodeid inventory, wrapper DSL, or test
impact database.

`make test-ci` serially runs every fixed owner target and then combines
coverage. Serial execution binds one frozen local tree to its isolated
resources and avoids concurrent broker restarts or destructive database use.
Its native reports are diagnostic local evidence and never merge, release, or
deployment authority.

PostgreSQL behavior tests migrate one run-scoped baseline database to head and
clone it into private function- or module-scoped databases. Tests that reset a
schema or traverse historical revisions use a separate empty migration-owned
database. Broker restart tests require an explicitly supplied disposable
`TRACEFOLD_TEST_RABBITMQ_CONTAINER`; they never discover and restart an
operator deployment on their own. CI supplies that identity and fails if it
cannot find the job-local service container.

Scheduled, live/provider, visual, performance, and mutation diagnostics remain
outside the fixed merge plan unless a dedicated Issue moves a detector after
showing which retained required seam owns the original risk.
