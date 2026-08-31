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
| `runtime-process` | `ci-runtime-process` | PostgreSQL, RabbitMQ, Node, and a disposable broker container identity | pytest JUnit |
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

### Audited baseline before #428 CI splitting

The last complete successful reference captured for #428 is
[`main@928cc95`, run 33329158473](https://github.com/AnalyThothAI/tracefold/actions/runs/33329158473).
The workflow took 13m56s wall-clock. Its `runtime-process` test step took
12m23s and was the critical path. A later run that was still in progress during
the audit was not used as a successful baseline.

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
