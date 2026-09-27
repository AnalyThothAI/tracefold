# Verification and generated artifacts; deployment does not import this workflow.
TRACEFOLD_TEST_RESULT_DIR ?= artifacts/test-results
QUALITY_TEST_SELECTION := tests/architecture tests/contract -m "(architecture or contract) and not external_codegen and not slow and not scheduled"
FAST_TEST_SELECTION := tests -m "not integration and not deploy and not e2e and not golden and not slow and not scheduled and not external_codegen and not package"
CI_QUALITY_SELECTION := tests/architecture tests/contract -m "not slow and not scheduled and not external_codegen"
CI_PYTHON_HERMETIC_SELECTION := tests -m "not architecture and not contract and not integration and not deploy and not e2e and not golden and not slow and not scheduled and not external_codegen"
CI_POSTGRES_BEHAVIOR_SELECTION := tests/integration -m "integration and not migration and not slow and not scheduled"
CI_MIGRATION_SELECTION := tests/integration -m "migration and not slow and not scheduled"
CI_RUNTIME_BROKER_SELECTION := tests/golden \
	tests/integration/test_news_bus_rabbitmq.py \
	tests/integration/test_news_durable_event_plane.py \
	tests/integration/test_workers_runtime_v2.py \
	tests/test_workers_probe.py \
	-m "(golden or slow) and not scheduled"
CI_DEPLOY_E2E_SELECTION := tests/deploy tests/e2e \
	tests/integration/test_news_status_scale.py \
	tests/integration/test_news_v3_price_scale.py \
	-m "(deploy or e2e or slow) and not scheduled"
CI_TEST_INTEGRITY_SELECTION := tests/contract/test_hook_installer.py \
	tests/slow/test_frontend_harness_fail_closed.py \
	tests/slow/test_required_pytest_fail_closed.py \
	-m "slow and not scheduled"
CI_FRONTEND_PYTHON_SELECTION := tests/contract/test_openapi_codegen.py -m external_codegen

TRACEFOLD_COVERAGE_DIR ?= artifacts/coverage

# The required lanes run pytest directly. They used to run under `coverage run --parallel-mode` so a
# ninth report-only job could combine the data, and that job is gone (#598 D8): every required lane
# paid the tracer, the artifact upload and the download for a number nothing read and nothing gated.
# `make coverage` still measures on demand; nothing in the fixed plan does.
define RUN_REQUIRED_PYTEST
PYTEST_ADDOPTS= PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TRACEFOLD_HYPOTHESIS_PROFILE=ci \
	TRACEFOLD_TEST_RESOURCES_REQUIRED=1 uv run --locked python \
	-m pytest -p _hypothesis_pytestplugin \
	$(1) --maxfail=0 --override-ini=xfail_strict=true \
	--junitxml="$(TRACEFOLD_TEST_RESULT_DIR)/$(2)" --durations=50
endef

test: test-fast ## broad hermetic checkpoint (alias for test-fast); not a per-edit loop

test-fast: ## broad hermetic final checkpoint; no external resources; not per-edit
	@uv run --locked python -m pytest $(FAST_TEST_SELECTION)

test-all: ## local convenience: every Python lane plus frontend; not verification evidence
	@cd web && npm run typecheck && npm run lint && npm run test:unit && npm run format:check && npm run build
	@uv run --locked python -m pytest

test-results-prepare:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)"/junit-*.xml \
		"$(TRACEFOLD_TEST_RESULT_DIR)"/vitest-*.json \
		"$(TRACEFOLD_TEST_RESULT_DIR)"/playwright*.json

ci-quality-static:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-quality-static.xml"
	@$(MAKE) --no-print-directory check-static
	@$(call RUN_REQUIRED_PYTEST,$(CI_QUALITY_SELECTION),junit-quality-static.xml)
	@uv run --locked python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-quality-static.xml"

ci-python-hermetic:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-python-hermetic.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_PYTHON_HERMETIC_SELECTION),junit-python-hermetic.xml)
	@uv run --locked python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-python-hermetic.xml"

# `docs/generated/db-schema.md` is introspected from a real database, so `make check-static` — which
# owns every other generated-artifact drift check — cannot run it. `ci-postgres-behavior` is the
# lane that already has a PostgreSQL. The check builds its own scratch database and drops it again,
# so it never reads whatever state a test left in the shared one.
define CHECK_GENERATED_DB_SCHEMA_PY
import os
import subprocess
import sys

from tests.postgres_test_utils import (
    temporary_unmigrated_postgres_database,
    test_postgres_dsn,
    upgrade_test_head,
)

with temporary_unmigrated_postgres_database(test_postgres_dsn()) as dsn:
    upgrade_test_head(dsn)
    completed = subprocess.run(
        [sys.executable, "scripts/regen_db_schema.py", "--check"],
        env={**os.environ, "TRACEFOLD_TEST_POSTGRES_DSN": dsn},
        check=False,
    )
sys.exit(completed.returncode)
endef
export CHECK_GENERATED_DB_SCHEMA_PY

# Two selections, two reports, one PostgreSQL. The historical migration walk used to be a job of
# its own (#598 D8): it needs exactly the pinned image this lane already starts, and a second
# runner bought a second bootstrap for 58 nodeids. The selections stay separate — the behavior
# tests clone a run-scoped baseline at head, the migration tests own an empty database and
# traverse revisions — and each writes the JUnit report `require_test_reports.py` reads by name.
ci-postgres-behavior:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-postgres-behavior.xml" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/junit-migration.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_POSTGRES_BEHAVIOR_SELECTION),junit-postgres-behavior.xml)
	@uv run --locked python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-postgres-behavior.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_MIGRATION_SELECTION),junit-migration.xml)
	@uv run --locked python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-migration.xml"
	@uv run --locked python -c "$$CHECK_GENERATED_DB_SCHEMA_PY"

ci-runtime-broker:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-runtime-broker.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_RUNTIME_BROKER_SELECTION),junit-runtime-broker.xml)
	@uv run --locked python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-runtime-broker.xml"

ci-deploy-e2e:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-deploy-e2e.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_DEPLOY_E2E_SELECTION),junit-deploy-e2e.xml)
	@uv run --locked python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-deploy-e2e.xml"

# The Node-dependent lanes all live here now (#598 D8). `test-integrity` was a job whose whole
# resource list was "Node", which this job already installs, and the golden-path interaction specs
# need the Chromium this job already downloads. Every selection keeps its own native report, and
# the report names may not collide: `junit-frontend-python.xml`, `junit-test-integrity.xml`,
# `vitest-architecture.json`, `vitest-unit.json`, `playwright-golden-paths.json` and
# `playwright.json` are six independent fail-closed checks.
ci-frontend:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-frontend-python.xml" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/junit-test-integrity.xml" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/vitest-architecture.json" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/vitest-unit.json" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/playwright-golden-paths.json" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@$(call RUN_REQUIRED_PYTEST,$(CI_FRONTEND_PYTHON_SELECTION),junit-frontend-python.xml)
	@uv run --locked python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-frontend-python.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_TEST_INTEGRITY_SELECTION),junit-test-integrity.xml)
	@uv run --locked python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-test-integrity.xml"
	@npm --prefix web run typecheck
	@npm --prefix web run lint:eslint
	@npm --prefix web run test:architecture -- \
		--allowOnly=false --reporter=json \
		--outputFile="$(CURDIR)/$(TRACEFOLD_TEST_RESULT_DIR)/vitest-architecture.json"
	@uv run --locked python scripts/require_test_reports.py \
		--vitest-json "$(TRACEFOLD_TEST_RESULT_DIR)/vitest-architecture.json"
	@npm --prefix web run test:unit -- \
		--allowOnly=false --reporter=json \
		--outputFile="$(CURDIR)/$(TRACEFOLD_TEST_RESULT_DIR)/vitest-unit.json"
	@uv run --locked python scripts/require_test_reports.py \
		--vitest-json "$(TRACEFOLD_TEST_RESULT_DIR)/vitest-unit.json"
	@npm --prefix web run format:check
	@npm --prefix web run build
	@PLAYWRIGHT_JSON_OUTPUT_NAME="$(CURDIR)/$(TRACEFOLD_TEST_RESULT_DIR)/playwright-golden-paths.json" \
		npm --prefix web run test:e2e
	@uv run --locked python scripts/require_test_reports.py \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright-golden-paths.json"
	@uv run --locked python -m tests.browser.run_full_stack_smoke \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@uv run --locked python scripts/require_test_reports.py \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"

test-ci: ## optional complete local preflight for declared high-risk changes; no merge authority
	@$(MAKE) --no-print-directory test-results-prepare
	@$(MAKE) --no-print-directory ci-quality-static
	@$(MAKE) --no-print-directory ci-python-hermetic
	@$(MAKE) --no-print-directory ci-postgres-behavior
	@$(MAKE) --no-print-directory ci-runtime-broker
	@$(MAKE) --no-print-directory ci-deploy-e2e
	@$(MAKE) --no-print-directory ci-frontend

# Coverage on demand, and nowhere else. It was a ninth CI job that combined data every required
# lane produced under a tracer; it re-ran nothing, gated nothing and nobody read it, so #598 D8
# deleted the job and the wrapper. The configuration in `pyproject.toml` stays, and this target is
# the way in: one hermetic run, measured, reported, thresholdless.
coverage: ## measure and print coverage of the hermetic selection; reports only, gates nothing
	@rm -rf "$(TRACEFOLD_COVERAGE_DIR)"
	@mkdir -p "$(TRACEFOLD_COVERAGE_DIR)"
	@uv run --locked python -m coverage run --parallel-mode -m pytest $(FAST_TEST_SELECTION)
	@uv run --locked python -m coverage combine
	@uv run --locked python -m coverage report
	@echo "--- tracefold/news ---"
	@uv run --locked python -m coverage report --include='tracefold/news/*'
	@echo "--- tracefold/trading ---"
	@uv run --locked python -m coverage report --include='tracefold/trading/*'

test-slow: ## real-process Workers runtime tests bounded by wall-clock deadlines
	@uv run --locked python -m pytest -m "slow and not scheduled"

test-scheduled: ## production-duration diagnostics; explicitly outside merge evidence
	@uv run --locked python -m pytest -m scheduled --durations=20

postgres-restore-drill: ## isolated production-image dump/restore/migrate/audit/smoke evidence
	@uv run --locked python -m tracefold.app.restore_storage

test-browser-smoke: ## one real FastAPI static/bootstrap/bearer/news path in Chromium
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@npm --prefix web run build:checked
	@uv run --locked python -m tests.browser.run_full_stack_smoke \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@uv run --locked python scripts/require_test_reports.py \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"

# The same config `ci-frontend` runs. The lane stopped being a local diagnostic when its two
# screenshot specs and their 40 committed `-darwin` baselines were deleted (#598 D8): what is left
# is eight interaction specs over four viewports, and they are required per PR now. This target is
# the local way to run exactly what CI runs.
test-visual: ## the four-viewport Playwright interaction lane `ci-frontend` requires
	@npm --prefix web run test:e2e

check-static: ## run hermetic static and generated drift checks without pytest
	@uv run --locked ruff check .
	@uv run --locked ruff format --check .
	@uv run --locked mypy tracefold
	@uv run --locked python scripts/regen_cli_help.py --check
	@uv run --locked python scripts/regen_rabbitmq_definitions.py --check
	@uv run --locked python scripts/sync_agent_router.py --check
	@uv run --locked python scripts/check_mandatory_docs_links.py
	@uv run --locked python -m compileall tracefold tests

check: check-static ## static checks plus local architecture/contract regression
	@uv run --locked python -m pytest $(QUALITY_TEST_SELECTION)

test-integration: ## run only tests/integration/ (real PostgreSQL boundary), excluding slow
	@uv run --locked python -m pytest tests/integration -m "integration and not slow and not scheduled"

test-deploy: ## run deploy/operations subprocess and lifecycle tests
	@uv run --locked python -m pytest tests/deploy -m deploy

test-e2e: ## run only tests/e2e/ (running service boundary)
	@uv run --locked python -m pytest tests/e2e -m e2e

test-golden: ## run the real RabbitMQ -> Workers -> PostgreSQL -> HTTP golden path
	@uv run --locked python -m pytest tests/golden -m golden

regen-contract: ## regenerate openapi.json + web/src/lib/types/openapi.ts
	@uv run --locked python scripts/regen_openapi.py
	@cd web && npm run generate:types && cd ..

install-hooks: ## diagnose and install this repository's pre-commit hook
	@uv run --locked python scripts/install_hooks.py

.PHONY: docs-generated docs-db-schema docs-cli-help docs-rabbitmq-definitions

docs-generated: docs-db-schema docs-cli-help docs-rabbitmq-definitions ## regenerate docs/generated/* and the broker policy document

docs-db-schema: ## regenerate docs/generated/db-schema.md (requires Postgres)
	@uv run --locked python scripts/regen_db_schema.py

docs-cli-help: ## regenerate docs/generated/cli-help.md
	@uv run --locked python scripts/regen_cli_help.py

docs-rabbitmq-definitions:
	@uv run --locked python scripts/regen_rabbitmq_definitions.py

.PHONY: test test-fast test-all test-results-prepare ci-quality-static ci-python-hermetic ci-postgres-behavior ci-runtime-broker ci-deploy-e2e ci-frontend test-ci coverage test-slow test-scheduled postgres-restore-drill test-browser-smoke test-visual check-static check test-integration test-deploy test-e2e test-golden regen-contract install-hooks docs-generated docs-db-schema docs-cli-help docs-rabbitmq-definitions
