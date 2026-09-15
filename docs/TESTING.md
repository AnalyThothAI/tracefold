# Testing and CI implementation

[Development](DEVELOPMENT.md#risk-tiered-local-verification) owns local check
selection. This document describes current commands, resources, and remote evidence;
it does not add another approval process or require every local task to run every lane.

## Fixed full CI implementation

[The workflow](../.github/workflows/ci.yml) runs the same required plan for PRs targeting
`main`, main pushes, release events, and manual dispatches. It currently has no path,
draft, or commit-message exclusion. Pull-request concurrency cancels older runs for
the same PR. A cancelled run is not successful evidence.

| Job | Make target | Resources | Native results |
| --- | --- | --- | --- |
| `quality-static` | `ci-quality-static` | Python | `junit-quality-static.xml` |
| `python-hermetic` | `ci-python-hermetic` | Python | `junit-python-hermetic.xml` |
| `postgres-behavior` | `ci-postgres-behavior` | PostgreSQL, RabbitMQ | `junit-postgres-behavior.xml`, `junit-migration.xml` |
| `runtime-broker` | `ci-runtime-broker` | PostgreSQL, RabbitMQ, disposable restartable broker | `junit-runtime-broker.xml` |
| `deploy-e2e` | `ci-deploy-e2e` | PostgreSQL, Node, Docker/Testcontainers | `junit-deploy-e2e.xml` |
| `frontend` | `ci-frontend` | PostgreSQL, RabbitMQ, Node, Chromium | `junit-frontend-python.xml`, `junit-test-integrity.xml`, `vitest-architecture.json`, `vitest-unit.json`, `playwright-golden-paths.json`, `playwright.json` |

`postgres-behavior` also walks migrations and checks the generated database schema
against a scratch database. `frontend` includes external code generation, harness
integrity checks, Vitest, the viewport interaction suite, and full-stack browser
smoke. The [Makefile](../Makefile) owns exact selections and report names; use it
rather than a historical node-count inventory when investigating coverage.

Each job checks out `TESTED_SHA` (PR HEAD for a pull request), verifies that checkout,
and installs locked dependencies as needed. Jobs have isolated resources. Required
Python, Vitest, and Playwright runs emit native reports under `artifacts/test-results/`;
`scripts/require_test_reports.py` rejects empty, missing, or non-green required results.
Do not silently update snapshots or use focus, skips, expected failures, or retries
to convert incomplete coverage into a successful required run.

The `ci-gate` job succeeds only when every required job reports `success`. Repository
rules are remote configuration, so inspect their current enforcement before an
authorized merge instead of relying on a copied ruleset name, bypass list, or merge
method in prose. `scripts/require_main_ci.py` separately checks successful main-push
workflow evidence for the exact deployment SHA. PR-head results do not attest a
later squash commit.

There is no required coverage-percentage gate. `make coverage` measures on demand;
required lanes do not pay for a coverage tracer. Historical timing measurements,
old job splits, and previous test counts remain in their Issues and workflow runs,
not as present-tense inventories in this manual.

## Local lane implementation

Use focused pytest or frontend commands during development. The common entry points
are available through `make help`:

| Command | Scope |
| --- | --- |
| `make check-static` | Static quality, pure generated/router drift, documentation file links, compilation. |
| `make check` | Static checks plus the hermetic architecture/contract selection. |
| `make test` / `make test-fast` | Broad hermetic Python regression; no real DB or broker. |
| `make test-integration` | Real dependency integration, excluding separately selected slow/scheduled tests. |
| `make test-deploy` | Deployment and operations lifecycle. |
| `make test-e2e` | Running service boundary. |
| `make test-golden` | Broker-driven Workers → PostgreSQL → HTTP path. |
| `make test-browser-smoke` | Production backend/static/bootstrap path in Chromium. |
| `make test-visual` | The viewport interaction lane also selected by CI. |
| `make test-slow` | Explicit slow process and harness diagnostics. |
| `make test-scheduled` | Production-duration diagnostics outside required merge evidence. |
| `make test-ci` | All current fixed owners, serially, with reports and required isolated resources. |
| `make coverage` | On-demand measurement of hermetic Python coverage. |

A successful local full preflight is useful evidence, not merge or deployment
authorization. Select it according to the changed risk; do not run it after every
edit or require it merely to open a PR. Do not rerun subsets of a successful superset
on unchanged relevant inputs just to populate a checklist.

### Resource isolation

PostgreSQL tests clone a migrated baseline into private test databases where
appropriate; migration-history tests use a separate empty database. Broker restart
tests require an explicitly supplied disposable `TRACEFOLD_TEST_RABBITMQ_CONTAINER`.
They must not discover and restart an operator deployment. Required CI and full
preflight treat missing required resources as failure, not a skip.

Keep the tested local tree and resource configuration stable during a run. Do not
share a destructive database or restartable broker between concurrent owners.
A local environment without a resource can still run independent pure checks and
prepare a PR, but cannot claim to have verified that resource's behavior.

### Generated artifacts and documentation

`make check-static` runs the CLI-help, RabbitMQ-definitions, and agent-router drift
checks. The generated database schema requires PostgreSQL and is checked by its
resource-owning CI job. OpenAPI and TypeScript checks also have their maintained
contract/codegen owners; inspect the actual Make selections when changing them.

For router/document edits, the pure starting point is:

```bash
python3 scripts/sync_agent_router.py --check
python3 scripts/check_mandatory_docs_links.py
```

The documentation link script checks local file existence. It does not establish
that every anchor, command, architectural claim, or backticked path is correct.
Review those against their owning headings and implementation. The existing
`tests/architecture/test_docs_surface.py` additionally checks router synchronization
and the hermetic Make surfaces; do not add tests that freeze the wording or length
of an agent instruction as a product invariant.

### Changing the test system

Improve a slow or redundant test at the risk mechanism it covers. A change to test
selection, retry behavior, or required jobs must explain retained coverage and be
validated at the affected harness boundary. The current fixed plan can be improved
in an explicitly scoped change; this document does not make it immutable.

Libraries, coverage, mutation testing, or other diagnostic tools may be evaluated
inside the current task when useful. They do not require an automatic separate
Issue per detector, and their presence alone does not prove a production seam.
Do not silently move a required risk into an optional diagnostic or weaken an
acceptance test merely to obtain green CI.
