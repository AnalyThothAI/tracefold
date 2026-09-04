UV_CACHE_DIR ?= /tmp/tracefold-uv-cache
export UV_CACHE_DIR

TRACEFOLD := uv run tracefold
READ_TRADING_ENABLED := uv run python -c 'import json, sys; value = json.load(sys.stdin)["data"]["trading"]["enabled"]; print(str(value).lower()) if type(value) is bool else sys.exit("invalid trading enabled")'
READ_TRADING_EXECUTION_MODE := uv run python -c 'import json, sys; value = json.load(sys.stdin)["data"]["trading"]["execution"]["mode"]; print(value) if value in {"disabled", "paper", "live"} else sys.exit("invalid trading execution mode")'
# Every published Compose binding is declared once, here, with the same default `compose.yaml`
# renders, and exported once. Six of the twelve used to be missing: an operator who exported
# `TRACEFOLD_POSTGRES_PORT` for one command and forgot it for the next changed the rendered
# postgres port, and Compose then recreated the container holding the database (#537 D5). There is
# no `.env` support and there must not be: an untracked deployment input is exactly what the
# deployment gate refuses.
TRACEFOLD_POSTGRES_HOST ?= 127.0.0.1
TRACEFOLD_POSTGRES_PORT ?= 56532
TRACEFOLD_RABBITMQ_HOST ?= 127.0.0.1
TRACEFOLD_RABBITMQ_PORT ?= 5672
TRACEFOLD_RABBITMQ_MGMT_HOST ?= 127.0.0.1
TRACEFOLD_RABBITMQ_MGMT_PORT ?= 15672
TRACEFOLD_API_HOST ?= 127.0.0.1
TRACEFOLD_API_PORT ?= 8765
TRACEFOLD_WORKERS_HOST ?= 127.0.0.1
TRACEFOLD_WORKERS_PORT ?= 8766
TRACEFOLD_NAUTILUS_HOST ?= 127.0.0.1
TRACEFOLD_NAUTILUS_PORT ?= 8767
export TRACEFOLD_POSTGRES_HOST TRACEFOLD_POSTGRES_PORT
export TRACEFOLD_RABBITMQ_HOST TRACEFOLD_RABBITMQ_PORT
export TRACEFOLD_RABBITMQ_MGMT_HOST TRACEFOLD_RABBITMQ_MGMT_PORT
export TRACEFOLD_API_HOST TRACEFOLD_API_PORT
export TRACEFOLD_WORKERS_HOST TRACEFOLD_WORKERS_PORT
export TRACEFOLD_NAUTILUS_HOST TRACEFOLD_NAUTILUS_PORT

TRACEFOLD_API_URL ?= http://127.0.0.1:$(TRACEFOLD_API_PORT)
TRACEFOLD_WORKERS_URL ?= http://127.0.0.1:$(TRACEFOLD_WORKERS_PORT)
TRACEFOLD_NAUTILUS_URL ?= http://127.0.0.1:$(TRACEFOLD_NAUTILUS_PORT)
TRACEFOLD_COMPOSE_WAIT_SECONDS ?= 300

# The execution runtime image `make runtime-up` starts. The default is this checkout's HEAD, which
# is what `make runtime-build` tags; passing an older tag on the command line is the rollback.
RUNTIME_IMAGE ?= tracefold-runtime:$(shell git rev-parse --verify HEAD 2>/dev/null)

# One refusal for every entry that drives the production stack. Inheriting a `COMPOSE_*` variable
# would let ambient environment point a deployment at another file, project or profile set; pinning
# them here is also what lets a checkout whose directory is not named `tracefold` still deploy the
# `tracefold` project. `COMPOSE_PROFILES=execution` is now unconditional: the execution runtime is
# always part of the Compose model, and what actually starts or stops is decided only by the
# explicit service lists below (#537 D3).
define PIN_COMPOSE_STACK
if [ -n "$${COMPOSE_FILE:-}" ] || [ -n "$${COMPOSE_PROJECT_NAME:-}" ] || \
		[ -n "$${COMPOSE_ENV_FILES:-}" ] || [ -n "$${COMPOSE_PROFILES:-}" ] || \
		[ -n "$${COMPOSE_PATH_SEPARATOR:-}" ] || [ -n "$${COMPOSE_DISABLE_ENV_FILE:-}" ]; then \
		echo "$(1) refuses inherited Compose stack variables; rerun without COMPOSE_* overrides." >&2; \
		exit 2; \
	fi; \
	COMPOSE_FILE="$$(pwd -P)/compose.yaml"; \
	COMPOSE_PROJECT_NAME=tracefold; \
	COMPOSE_PROFILES=execution; \
	export COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES; \
	unset COMPOSE_ENV_FILES COMPOSE_PATH_SEPARATOR COMPOSE_DISABLE_ENV_FILE
endef

# Applying a migration under a running execution runtime replaces the schema beneath the one process
# that owns live exposure, and the runtime is not restarted by a deploy any more, so nothing would
# reload it. Refuse instead, and name the way out.
define REFUSE_MIGRATION_UNDER_RUNTIME
if [ "$${TRACEFOLD_MIGRATE_UNDER_RUNTIME:-}" != 1 ] && [ -n "$$(docker compose ps -q nautilus)" ]; then \
		migration_source_head=$$(uv run python -c 'from tracefold.platform.postgres.migrations import latest_migration_version; print(latest_migration_version())'); \
		migration_database_head=$$($(POSTGRES_READ_ONLY_PSQL) "SELECT version_num FROM alembic_version LIMIT 1" || true); \
		if [ "$$migration_database_head" != "$$migration_source_head" ]; then \
			echo "the execution runtime is running and this would migrate the database from '$$migration_database_head' to '$$migration_source_head'." >&2; \
			echo "run make runtime-down first or set TRACEFOLD_MIGRATE_UNDER_RUNTIME=1." >&2; \
			exit 2; \
		fi; \
	fi
endef

# One read-only psql invocation, reused by every head comparison. `$$1` is the statement.
POSTGRES_READ_ONLY_PSQL = docker compose exec -T postgres sh -eu -c \
	'PGPASSWORD=$$(cat /run/secrets/postgres_database_password); \
	PGOPTIONS="-c default_transaction_read_only=on"; \
	export PGPASSWORD PGOPTIONS; \
	exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold -d tracefold -c "$$1"' sh

TRACEFOLD_TEST_RESULT_DIR ?= artifacts/test-results
QUALITY_TEST_SELECTION := tests/architecture tests/contract -m "(architecture or contract) and not external_codegen and not slow and not scheduled"
FAST_TEST_SELECTION := tests -m "not integration and not deploy and not e2e and not golden and not live and not slow and not scheduled and not external_codegen and not package"
CI_QUALITY_SELECTION := tests/architecture tests/contract -m "not live and not slow and not scheduled and not external_codegen"
CI_PYTHON_HERMETIC_SELECTION := tests -m "not architecture and not contract and not integration and not deploy and not e2e and not golden and not live and not slow and not scheduled and not external_codegen"
CI_POSTGRES_BEHAVIOR_SELECTION := tests/integration -m "integration and not migration and not slow and not scheduled"
CI_MIGRATION_SELECTION := tests/integration -m "migration and not slow and not scheduled"
CI_RUNTIME_BROKER_SELECTION := tests/golden \
	tests/integration/test_news_bus_rabbitmq.py \
	tests/integration/test_news_durable_event_plane.py \
	tests/integration/test_workers_runtime_v2.py \
	tests/test_workers_probe.py \
	-m "(golden or slow) and not live and not scheduled"
CI_DEPLOY_E2E_SELECTION := tests/deploy tests/e2e \
	tests/integration/test_news_status_scale.py \
	tests/integration/test_news_v3_price_scale.py \
	-m "(deploy or e2e or slow) and not live and not scheduled"
CI_TEST_INTEGRITY_SELECTION := tests/contract/test_hook_installer.py \
	tests/slow/test_frontend_harness_fail_closed.py \
	tests/slow/test_required_pytest_fail_closed.py \
	-m "slow and not live and not scheduled"
CI_FRONTEND_PYTHON_SELECTION := tests/contract/test_openapi_codegen.py -m external_codegen

TRACEFOLD_COVERAGE_DIR ?= artifacts/coverage
TRACEFOLD_MUTATION_DIR ?= artifacts/mutation
TRACEFOLD_MUTATION_SHARDS ?= 1
TRACEFOLD_MUTATION_SHARD ?= 0

# The required lanes run under `coverage run`, so the same execution that produces the JUnit report
# produces the coverage data — there is no second full-suite pass. `--parallel-mode` keeps one data
# file per process so the lanes, and the child processes coverage's `patch = subprocess` starts, can
# be combined afterwards. `make test-fast` deliberately does not go through here.
define RUN_REQUIRED_PYTEST
PYTEST_ADDOPTS= PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TRACEFOLD_HYPOTHESIS_PROFILE=ci \
	TRACEFOLD_TEST_RESOURCES_REQUIRED=1 uv run python -m coverage run --parallel-mode \
	-m pytest -p _hypothesis_pytestplugin \
	$(1) --maxfail=0 --override-ini=xfail_strict=true \
	--junitxml="$(TRACEFOLD_TEST_RESULT_DIR)/$(2)" --durations=50
endef

.PHONY: help up _up-locked deploy-image _deploy-image-locked verify-main-ci status status-app logs down runtime-build _runtime-build-locked runtime-up _runtime-up-locked runtime-restart runtime-down runtime-logs runtime-status preflight github-preflight sync install uninstall tool-path test test-fast test-all test-ci test-results-prepare ci-test-effectiveness mutation mutation-sentinel ci-quality-static ci-python-hermetic ci-postgres-behavior ci-migration ci-runtime-broker ci-deploy-e2e ci-test-integrity ci-frontend test-property test-slow test-scheduled postgres-restore-drill test-frontend test-browser-smoke test-visual lint compile check check-static init config db-migrate db-health serve workers serve-shell workers-shell clean test-integration test-deploy test-e2e test-golden test-architecture test-contract test-external-codegen regen-contract install-hooks

help: ## show available targets
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "%-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## install dependencies
	@uv sync

install: ## install or update the global CLI with uv tool
	@uv tool install --force --reinstall .

uninstall: ## uninstall the global CLI installed by uv tool
	@uv tool uninstall tracefold

tool-path: ## ensure uv tool executables are on PATH
	@uv tool update-shell

test: test-fast ## broad hermetic checkpoint (alias for test-fast); not a per-edit loop

test-fast: ## broad hermetic final checkpoint; no external resources; not per-edit
	@uv run python -m pytest $(FAST_TEST_SELECTION)

test-all: test-frontend ## local convenience: every Python lane plus frontend; not verification evidence
	@uv run python -m pytest

test-results-prepare:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)"/junit-*.xml \
		"$(TRACEFOLD_TEST_RESULT_DIR)"/vitest-*.json \
		"$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@rm -rf "$(TRACEFOLD_COVERAGE_DIR)"/.coverage* "$(TRACEFOLD_COVERAGE_DIR)"/html \
		"$(TRACEFOLD_COVERAGE_DIR)"/coverage.json "$(TRACEFOLD_COVERAGE_DIR)"/coverage.xml

ci-quality-static:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-quality-static.xml"
	@$(MAKE) --no-print-directory check-static
	@$(call RUN_REQUIRED_PYTEST,$(CI_QUALITY_SELECTION),junit-quality-static.xml)
	@uv run python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-quality-static.xml"

ci-python-hermetic:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-python-hermetic.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_PYTHON_HERMETIC_SELECTION),junit-python-hermetic.xml)
	@uv run python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-python-hermetic.xml"

ci-postgres-behavior:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-postgres-behavior.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_POSTGRES_BEHAVIOR_SELECTION),junit-postgres-behavior.xml)
	@uv run python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-postgres-behavior.xml"

ci-migration:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-migration.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_MIGRATION_SELECTION),junit-migration.xml)
	@uv run python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-migration.xml"

ci-runtime-broker:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-runtime-broker.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_RUNTIME_BROKER_SELECTION),junit-runtime-broker.xml)
	@uv run python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-runtime-broker.xml"

ci-deploy-e2e:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-deploy-e2e.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_DEPLOY_E2E_SELECTION),junit-deploy-e2e.xml)
	@uv run python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-deploy-e2e.xml"

ci-test-integrity:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-test-integrity.xml"
	@$(call RUN_REQUIRED_PYTEST,$(CI_TEST_INTEGRITY_SELECTION),junit-test-integrity.xml)
	@uv run python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-test-integrity.xml"

ci-frontend:
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)" "$(TRACEFOLD_COVERAGE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/junit-frontend-python.xml" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/vitest-architecture.json" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/vitest-unit.json" \
		"$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@$(call RUN_REQUIRED_PYTEST,$(CI_FRONTEND_PYTHON_SELECTION),junit-frontend-python.xml)
	@uv run python scripts/require_test_reports.py --junit "$(TRACEFOLD_TEST_RESULT_DIR)/junit-frontend-python.xml"
	@npm --prefix web run typecheck
	@npm --prefix web run lint:eslint
	@npm --prefix web run test:architecture -- \
		--allowOnly=false --reporter=json \
		--outputFile="$(CURDIR)/$(TRACEFOLD_TEST_RESULT_DIR)/vitest-architecture.json"
	@uv run python scripts/require_test_reports.py \
		--vitest-json "$(TRACEFOLD_TEST_RESULT_DIR)/vitest-architecture.json"
	@npm --prefix web run test:unit -- \
		--allowOnly=false --reporter=json \
		--outputFile="$(CURDIR)/$(TRACEFOLD_TEST_RESULT_DIR)/vitest-unit.json" \
		--coverage --coverage.reportsDirectory="$(CURDIR)/$(TRACEFOLD_COVERAGE_DIR)/frontend"
	@uv run python scripts/require_test_reports.py \
		--vitest-json "$(TRACEFOLD_TEST_RESULT_DIR)/vitest-unit.json"
	@npm --prefix web run format:check
	@npm --prefix web run build
	@uv run python -m tests.browser.run_full_stack_smoke \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@uv run python scripts/require_test_reports.py \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"

test-ci: ## optional complete local preflight for declared high-risk changes; no merge authority
	@$(MAKE) --no-print-directory test-results-prepare
	@$(MAKE) --no-print-directory ci-quality-static
	@$(MAKE) --no-print-directory ci-python-hermetic
	@$(MAKE) --no-print-directory ci-postgres-behavior
	@$(MAKE) --no-print-directory ci-migration
	@$(MAKE) --no-print-directory ci-runtime-broker
	@$(MAKE) --no-print-directory ci-deploy-e2e
	@$(MAKE) --no-print-directory ci-test-integrity
	@$(MAKE) --no-print-directory ci-frontend
	@$(MAKE) --no-print-directory ci-test-effectiveness

# Report-only (#373 PR 2). Standard coverage.py combine and reports over the data the required
# lanes already produced; it re-runs nothing, reads no JUnit/Vitest/Playwright report, and
# adjudicates no pass/fail. Thresholds arrive in PR 3, from measured exact-main baselines.
#
# One shell, so the early return really returns: a lane that failed before reaching pytest
# uploaded no data, `coverage combine` exits non-zero on an empty directory, and a report that
# went red because there was nothing to report would be the one failure that is not about
# coverage at all.
ci-test-effectiveness:
	@set -eu; \
		mkdir -p "$(TRACEFOLD_COVERAGE_DIR)"; \
		if ! ls "$(TRACEFOLD_COVERAGE_DIR)"/.coverage* >/dev/null 2>&1; then \
			echo "no coverage data was produced; nothing to report"; \
			exit 0; \
		fi; \
		uv run python -m coverage combine; \
		uv run python -m coverage report; \
		echo "--- tracefold/news ---"; \
		uv run python -m coverage report --include='tracefold/news/*'; \
		echo "--- tracefold/trading ---"; \
		uv run python -m coverage report --include='tracefold/trading/*'; \
		uv run python -m coverage json -o "$(TRACEFOLD_COVERAGE_DIR)/coverage.json"; \
		uv run python -m coverage xml -o "$(TRACEFOLD_COVERAGE_DIR)/coverage.xml"; \
		uv run python -m coverage html -d "$(TRACEFOLD_COVERAGE_DIR)/html" --quiet

# The scheduled mutation batch. `mutation.toml` carries the scope and the reasoning; the sentinel
# runs first because a mutation score is only evidence once the mutants provably reach the
# interpreter, and a harness that silently tests unmutated source reports good news. Locally this is
# one sequential shard (~63 min); CI splits the same session six ways, one checkout per worker,
# because Cosmic Ray mutates in place. `uv sync --group mutation` first: the tool is in a
# non-default group so that nothing which builds or ships the service can reach it.
#
# Mutating in place means the working tree holds a mutant for the whole run, and an interrupted run
# leaves one behind — which is a live defect sitting in a tracked file, one `git add -A` away from
# being committed. So the batch refuses to start unless the files it rewrites are clean, and
# restores them on the way out however it exits. The clean check is what makes the restore safe:
# it is only ever discarding a mutant this target wrote.
#
# Two details the first version got wrong. The sentinel rewrites a *third* tracked file — the canary
# under `tests/support/` — so `$(TRACEFOLD_MUTATION_FILES)` covers it as well as `mutation.toml`'s
# two kernels; leaving it out meant a kill during the sentinel could strand a mutated canary that
# then silently defeats the next run's harness proof. And an EXIT trap alone does not fire when the
# shell is killed by an untrapped signal under dash, which is `/bin/sh` on Debian and Ubuntu — so
# Ctrl-C during the hour-long batch, by far the likeliest way this ends, would leave the mutant in
# place. INT, TERM and HUP are trapped too.
TRACEFOLD_MUTATION_FILES = $$(uv run --no-sync python -c 'import tomllib, pathlib; \
	print(" ".join([*tomllib.loads(pathlib.Path("mutation.toml").read_text())["cosmic-ray"]["module-path"], \
	"tests/support/mutation_canary.py"]))')

mutation: ## scheduled-lane mutation batch: sentinel, then the batch, then survivor triage
	@set -eu; \
		files="$(TRACEFOLD_MUTATION_FILES)"; \
		if ! git diff --quiet -- $$files; then \
			echo "mutation: these files have uncommitted changes and the batch rewrites them in place:" >&2; \
			echo "  $$files" >&2; \
			echo "commit or set the changes aside first." >&2; \
			exit 1; \
		fi; \
		trap 'git checkout -- '"$$files" EXIT INT TERM HUP; \
		mkdir -p "$(TRACEFOLD_MUTATION_DIR)"; \
		uv sync --locked --group mutation; \
		uv run --no-sync python scripts/mutation_sentinel.py; \
		session="$(TRACEFOLD_MUTATION_DIR)/shard-$(TRACEFOLD_MUTATION_SHARD).sqlite"; \
		rm -f "$$session"; \
		uv run --no-sync cosmic-ray init mutation.toml "$$session"; \
		uv run --no-sync python scripts/mutation_shard.py "$$session" \
			--shard $(TRACEFOLD_MUTATION_SHARD) --of $(TRACEFOLD_MUTATION_SHARDS); \
		uv run --no-sync cosmic-ray exec mutation.toml "$$session"; \
		uv run --no-sync python scripts/mutation_survivors.py "$$session"

# Same guard as `mutation`: the sentinel rewrites the canary in place, so an interrupted run leaves
# a mutated tracked file behind — and a stranded canary is the one file whose corruption makes the
# harness proof itself meaningless.
mutation-sentinel: ## prove the mutation harness executes mutated code, without running a batch
	@set -eu; \
		files="$(TRACEFOLD_MUTATION_FILES)"; \
		if ! git diff --quiet -- $$files; then \
			echo "mutation-sentinel: the canary or a mutated module has uncommitted changes:" >&2; \
			echo "  $$files" >&2; \
			exit 1; \
		fi; \
		trap 'git checkout -- '"$$files" EXIT INT TERM HUP; \
		uv sync --locked --group mutation; \
		uv run --no-sync python scripts/mutation_sentinel.py

test-property: ## bounded pure properties (TRACEFOLD_HYPOTHESIS_PROFILE=nightly for extended runs)
	@uv run python -m pytest -m property

test-slow: ## real-process Workers runtime tests bounded by wall-clock deadlines
	@uv run python -m pytest -m "slow and not scheduled"

test-scheduled: ## production-duration diagnostics; explicitly outside merge evidence
	@uv run python -m pytest -m scheduled --durations=20

postgres-restore-drill: ## isolated production-image dump/restore/migrate/audit/smoke evidence
	@uv run python -m tracefold.app.restore_storage

test-frontend: ## frontend type, architecture, unit/component tests, format, and production build
	@cd web && npm run typecheck && npm run lint && npm run test:unit && npm run format:check && npm run build

test-browser-smoke: ## one real FastAPI static/bootstrap/bearer/news path in Chromium
	@mkdir -p "$(TRACEFOLD_TEST_RESULT_DIR)"
	@rm -f "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@npm --prefix web run build:checked
	@uv run python -m tests.browser.run_full_stack_smoke \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"
	@uv run python scripts/require_test_reports.py \
		--playwright-json "$(TRACEFOLD_TEST_RESULT_DIR)/playwright.json"

test-visual: ## explicit four-viewport Playwright interaction/screenshot diagnostics
	@npm --prefix web run test:e2e

lint: ## run ruff
	@uv run python -m ruff check .

compile: ## compile Python files
	@uv run python -m compileall tracefold tests

check-static: ## run hermetic static and generated drift checks without pytest
	@uv run ruff check .
	@uv run ruff format --check .
	@uv run mypy tracefold
	@uv run python scripts/regen_cli_help.py --check
	@uv run python scripts/regen_rabbitmq_definitions.py --check
	@uv run python scripts/sync_agent_router.py --check
	@uv run python scripts/check_mandatory_docs_links.py
	@uv run python -m compileall tracefold tests

check: check-static ## static checks plus local architecture/contract regression
	@uv run python -m pytest $(QUALITY_TEST_SELECTION)

test-integration: ## run only tests/integration/ (real PostgreSQL boundary), excluding slow
	@uv run python -m pytest tests/integration -m "integration and not slow and not scheduled"

test-deploy: ## run deploy/operations subprocess and lifecycle tests
	@uv run python -m pytest tests/deploy -m deploy

test-e2e: ## run only tests/e2e/ (running service boundary)
	@uv run python -m pytest tests/e2e -m e2e

test-golden: ## run the real RabbitMQ -> Workers -> PostgreSQL -> HTTP golden path
	@uv run python -m pytest tests/golden -m golden

test-architecture: ## run only semantic architecture contracts
	@uv run python -m pytest tests/architecture -m architecture

test-contract: ## run only tests/contract/
	@uv run python -m pytest tests/contract -m "contract and not external_codegen"

test-external-codegen: ## run release-only Node-backed generated contract checks
	@uv run python -m pytest -m external_codegen

regen-contract: ## regenerate openapi.json + web/src/lib/types/openapi.ts
	@uv run python scripts/regen_openapi.py
	@cd web && npm run generate:types && cd ..

install-hooks: ## diagnose and install this repository's pre-commit hook
	@uv run python scripts/install_hooks.py

init: ## create ~/.tracefold/config.yaml + PostgreSQL role password files
	@$(TRACEFOLD) init

config: ## print effective runtime config
	@$(TRACEFOLD) config

db-migrate: preflight github-preflight ## apply PostgreSQL migrations
	@# The same shape as `up` and `deploy-image`, for the same reason (#373): this applies Alembic
	@# revisions to the operator's production database from whatever tree it is invoked in. Half of
	@# that protection would be worse than either half — the exact-main gate without the deployment
	@# lock would let a migration run concurrently with `up`'s own, and refusing inherited `COMPOSE_*`
	@# without pinning them would break the checkout whose directory is not named `tracefold`.
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _db-migrate-locked

_db-migrate-locked:
	@python3 scripts/with_deployment_lock.py --assert-held
	@uv run python scripts/require_main_ci.py
	@# In the migration container, not on the host. The host CLI used to reach the container
	@# database through a hard-coded `postgres -> 127.0.0.1` DSN rewrite; that rewrite is deleted
	@# (#537 D1), so every CLI entry that touches the production database runs inside a container
	@# that already has the compose network and the mounted credential.
	@set -eu; \
		$(call PIN_COMPOSE_STACK,db-migrate); \
		$(REFUSE_MIGRATION_UNDER_RUNTIME); \
		docker compose run --rm --no-deps --entrypoint tracefold migrate db migrate

db-health: ## check PostgreSQL liveness and migration version
	@set -eu; \
		COMPOSE_PROFILES=execution docker compose exec -T workers tracefold db health

serve: ## run the read-only public runtime in foreground
	@$(TRACEFOLD) serve

workers: ## run the ingestion/projection/provider/model runtime in foreground
	@$(TRACEFOLD) workers


preflight: ## verify the one-command startup prerequisites
	@command -v git >/dev/null 2>&1 || { echo "git is not installed or not on PATH" >&2; exit 127; }
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed or not on PATH" >&2; exit 127; }
	@command -v docker >/dev/null 2>&1 || { echo "docker is not installed or not on PATH" >&2; exit 127; }
	@docker compose version >/dev/null 2>&1 || { echo "docker compose plugin is unavailable" >&2; exit 127; }
	@command -v curl >/dev/null 2>&1 || { echo "curl is not installed or not on PATH" >&2; exit 127; }
	@docker info >/dev/null 2>&1 || { \
		echo "Docker daemon is not reachable from this shell." >&2; \
		echo "Start Docker Desktop or grant this terminal access to the Docker socket, then rerun make up." >&2; \
		exit 1; \
	}
	@# The image is `python:3.13-slim` and the locked Nautilus wheel is cp313. A host venv on
	@# another minor runs a different Nautilus binary than the one that will hold the account, so
	@# every local proof is about a different artefact. `.python-version` pins it; this asserts it.
	@uv run python -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else "the project interpreter must be Python 3.13 to match the deployed image; run make sync")'
	@# Binance rejects a signed request whose timestamp is outside `recvWindow`, and a suspended
	@# WSL2 host drifts silently. One read of the venue's own clock is the whole check.
	@set -eu; \
		if venue_time=$$(curl -fsS --max-time 5 https://fapi.binance.com/fapi/v1/time 2>/dev/null); then \
			venue_ms=$$(printf '%s' "$$venue_time" | tr -dc '0-9'); \
			if [ -n "$$venue_ms" ]; then \
				drift=$$(( $$(date +%s) * 1000 - venue_ms )); \
				if [ "$$drift" -lt 0 ]; then drift=$$(( 0 - drift )); fi; \
				if [ "$$drift" -gt 2000 ]; then \
					echo "host clock differs from the venue by $$drift ms (budget 2000 ms); resynchronise the clock first." >&2; \
					exit 1; \
				fi; \
			fi; \
		else \
			echo "WARNING: the venue time endpoint was unreachable; clock drift was not checked." >&2; \
		fi

github-preflight:
	@command -v gh >/dev/null 2>&1 || { echo "GitHub CLI is not installed or not on PATH" >&2; exit 127; }
	@gh auth status --active --hostname github.com >/dev/null 2>&1 || { \
		echo "GitHub CLI is not authenticated for github.com; run gh auth login --hostname github.com" >&2; \
		exit 1; \
	}

verify-main-ci: github-preflight ## require the exact origin/main SHA to have a trusted green ci-gate
	@uv run python scripts/require_main_ci.py

up: preflight github-preflight ## build, migrate, start, and verify the complete product
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _up-locked

_up-locked:
	@python3 scripts/with_deployment_lock.py --assert-held
	@uv run python scripts/require_main_ci.py
	@set -eu; \
		$(call PIN_COMPOSE_STACK,up); \
		$(TRACEFOLD) init; \
		unset TRACEFOLD_APP_IMAGE; \
		token="$${GITHUB_TOKEN:-}"; \
		if [ -z "$$token" ] && command -v gh >/dev/null 2>&1; then \
			token=$$(gh auth token 2>/dev/null || true); \
		fi; \
		GITHUB_TOKEN="$$token"; \
		TRACEFOLD_BUILD_REVISION=$$(git rev-parse --verify HEAD); \
		export GITHUB_TOKEN TRACEFOLD_BUILD_REVISION; \
		$(REFUSE_MIGRATION_UNDER_RUNTIME); \
		fail() { \
			docker compose ps --all >&2 || true; \
			echo "Startup failed. Run make logs for diagnostics." >&2; \
			exit 1; \
		}; \
		docker compose build migrate || fail; \
		image=$$(docker compose config --images migrate 2>/dev/null \
			| grep -v '@sha256:' | head -n 1); \
		TRACEFOLD_IMAGE_DIGEST=$$(docker image inspect --format '{{.Id}}' "$$image" 2>/dev/null || true); \
		export TRACEFOLD_IMAGE_DIGEST; \
		if [ -z "$$TRACEFOLD_IMAGE_DIGEST" ]; then \
			echo "Could not read the digest of $${image:-the built image}; every runtime manifest it wrote" >&2; \
			echo "  would record image_digest=unversioned and could close no learning promotion." >&2; \
			exit 1; \
		fi; \
		manifest_document=$$(docker compose run --rm --no-deps --entrypoint tracefold migrate \
			db news-genesis-manifest) || fail; \
		target_manifest=$$(printf '%s' "$$manifest_document" \
			| uv run python -c 'import json,sys; print(json.load(sys.stdin)["data"]["runtime_manifest_sha"])') || fail; \
		docker compose up -d --no-build --wait --wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) postgres || fail; \
		runtime_services="migrate rabbitmq-policy serve workers"; \
		docker compose stop -t 40 workers serve || fail; \
		docker compose up -d --no-build --force-recreate --wait \
			--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) $$runtime_services || fail; \
		make --no-print-directory status-app || fail; \
		ready_manifest=$$(curl -fsS "$(TRACEFOLD_WORKERS_URL)/readyz" \
			| uv run python -c 'import json,sys; print(str(json.load(sys.stdin).get("runtime_manifest_sha") or ""))') || fail; \
		if [ "$$ready_manifest" != "$$target_manifest" ]; then \
			echo "Workers runtime manifest does not equal the configured target." >&2; \
			fail; \
		fi; \
		echo "Tracefold ready at $(TRACEFOLD_API_URL)"

# There is no revision-equality check here. The three Alembic heads are the compatibility rule;
# additionally requiring the image to carry current main's SHA meant the one image an operator
# actually needs during an incident — the previous one — was the one image this target refused
# (#537 D12). It never touches the execution runtime: that is `make runtime-up`'s job.
deploy-image: preflight github-preflight ## deploy an explicit local DB-compatible sha256 image from the primary checkout
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _deploy-image-locked

_deploy-image-locked:
	@python3 scripts/with_deployment_lock.py --assert-held
	@uv run python scripts/require_main_ci.py
	@set -eu; \
		$(call PIN_COMPOSE_STACK,deploy-image); \
		git_dir=$$(git rev-parse --absolute-git-dir); \
		git_common_dir=$$(git rev-parse --path-format=absolute --git-common-dir); \
		branch=$$(git branch --show-current); \
		if [ "$$git_dir" != "$$git_common_dir" ] || [ "$$branch" != "main" ]; then \
			echo "deploy-image must run from the primary checkout on main." >&2; \
			exit 2; \
		fi; \
		if ! git diff --quiet --ignore-submodules -- || \
			! git diff --cached --quiet --ignore-submodules --; then \
			echo "deploy-image refuses tracked or staged changes in the primary checkout." >&2; \
			exit 2; \
		fi; \
		relevant_untracked=$$(git ls-files --others -- \
			':(glob)compose*.yaml' ':(glob)compose*.yml' \
			':(glob)docker-compose*.yaml' ':(glob)docker-compose*.yml' \
			':(glob)tracefold/platform/postgres/alembic/versions/*.py'); \
		if [ -e .env ] || [ -n "$$relevant_untracked" ]; then \
			echo "deploy-image refuses an untracked deployment input (.env, Compose override, or migration source)." >&2; \
			exit 2; \
		fi; \
		if ! origin_main=$$(git rev-parse --verify refs/remotes/origin/main 2>/dev/null); then \
			echo "deploy-image requires a local origin/main ref; fetch it before rollback." >&2; \
			exit 2; \
		fi; \
		head=$$(git rev-parse --verify HEAD); \
		if [ "$$head" != "$$origin_main" ]; then \
			echo "deploy-image requires primary main HEAD to equal origin/main; fetch and pull --ff-only first." >&2; \
			exit 2; \
		fi; \
		$(TRACEFOLD) init >/dev/null; \
		if [ "$(origin IMAGE_ID)" != "command line" ]; then \
			echo "Pass an explicit local image ID: make deploy-image IMAGE_ID=sha256:<64 lowercase hex>." >&2; \
			exit 2; \
		fi; \
		image_id="$${IMAGE_ID:-}"; \
		if ! printf '%s\n' "$$image_id" | grep -Eq '^sha256:[0-9a-f]{64}$$'; then \
			echo "IMAGE_ID must be a full immutable sha256 image ID; tags and short IDs are refused." >&2; \
			exit 2; \
		fi; \
		if ! inspected_image_id=$$(docker image inspect --format '{{.Id}}' "$$image_id" 2>/dev/null); then \
			echo "IMAGE_ID is not present in the local Docker image store: $$image_id" >&2; \
			exit 2; \
		fi; \
		if [ "$$inspected_image_id" != "$$image_id" ]; then \
			echo "Docker resolved IMAGE_ID to $$inspected_image_id instead of the exact requested ID." >&2; \
			exit 2; \
		fi; \
		source_head=$$(uv run python -c 'from tracefold.platform.postgres.migrations import latest_migration_version; print(latest_migration_version())'); \
		if ! image_head=$$(docker run --rm --entrypoint python "$$image_id" -c 'import importlib.util; from importlib import import_module; module_name="tracefold.platform.postgres.migrations" if importlib.util.find_spec("tracefold.platform.postgres.migrations") is not None else "tracefold.platform.postgres.postgres_migrations"; print(import_module(module_name).latest_migration_version())'); then \
			echo "Could not inspect the target image Alembic head: $$image_id" >&2; \
			exit 2; \
		fi; \
		if [ -z "$$source_head" ] || [ "$$image_head" != "$$source_head" ]; then \
			echo "Target image Alembic head '$$image_head' does not match current source head '$$source_head'; schema-incompatible images are refused." >&2; \
			exit 2; \
		fi; \
		if ! database_head=$$($(POSTGRES_READ_ONLY_PSQL) "SELECT version_num FROM alembic_version LIMIT 1"); then \
			echo "Could not inspect the live database Alembic head; no services were stopped." >&2; \
			exit 2; \
		fi; \
		if [ -z "$$database_head" ] || [ "$$database_head" != "$$source_head" ]; then \
			echo "Live database Alembic head '$$database_head' does not match source/image head '$$source_head'; no services were stopped." >&2; \
			exit 2; \
		fi; \
		TRACEFOLD_APP_IMAGE="$$image_id"; \
		TRACEFOLD_IMAGE_DIGEST="$$inspected_image_id"; \
		export TRACEFOLD_APP_IMAGE TRACEFOLD_IMAGE_DIGEST; \
		configured_image=$$(docker compose config --images migrate 2>/dev/null | grep -v '@sha256:' | head -n 1); \
		if [ "$$configured_image" != "$$image_id" ]; then \
			echo "Compose did not resolve migrate to the exact requested image ID." >&2; \
			exit 2; \
		fi; \
		if ! runtime_config=$$(docker compose run --rm --no-deps --entrypoint tracefold migrate config); then \
			echo "Target image could not parse the active operator config; no services were stopped." >&2; \
			exit 2; \
		fi; \
		printf '%s\n' "$$runtime_config" | $(READ_TRADING_EXECUTION_MODE) >/dev/null; \
		fail() { \
			docker compose ps --all >&2 || true; \
			echo "Exact-image deployment failed. Run make logs for diagnostics." >&2; \
			exit 1; \
		}; \
		runtime_services="migrate rabbitmq-policy serve workers"; \
		docker compose stop -t 40 workers serve || fail; \
		docker compose up -d --no-build --force-recreate --wait \
			--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) $$runtime_services || fail; \
		for service in $$runtime_services; do \
			container_id=$$(docker compose ps --all -q "$$service"); \
			if [ -z "$$container_id" ]; then \
				echo "$$service container is missing after exact-image deployment." >&2; \
				fail; \
			fi; \
			if ! actual_image=$$(docker inspect --format '{{.Image}}' "$$container_id"); then \
				echo "Could not inspect the $$service container image." >&2; \
				fail; \
			fi; \
			if [ "$$actual_image" != "$$image_id" ]; then \
				echo "$$service container image '$$actual_image' does not equal requested '$$image_id'." >&2; \
				fail; \
			fi; \
		done; \
		if ! ready_image=$$(curl -fsS "$(TRACEFOLD_WORKERS_URL)/readyz" \
			| uv run python -c 'import json,sys; print(str(json.load(sys.stdin).get("image_digest") or ""))'); then \
			echo "Could not read Workers readiness image_digest after exact-image deployment." >&2; \
			fail; \
		fi; \
		if [ "$$ready_image" != "$$image_id" ]; then \
			echo "Workers readiness image_digest '$$ready_image' does not equal requested '$$image_id'." >&2; \
			fail; \
		fi; \
		if ! receipt_identity=$$(docker compose exec -T postgres sh -eu -c \
			'PGPASSWORD=$$(cat /run/secrets/postgres_database_password); \
			PGOPTIONS="-c default_transaction_read_only=on"; \
			export PGPASSWORD PGOPTIONS; \
			exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold -d tracefold \
			-c "$$1"' sh \
			"WITH active AS ( \
			   SELECT artifact_sha, payload \
			     FROM news_learning_artifacts \
			    WHERE kind = 'active_agent' \
			    ORDER BY created_at_ms DESC, artifact_sha DESC \
			    LIMIT 1 \
			 ), deployment AS ( \
			   SELECT parent_sha, payload \
			     FROM news_learning_artifacts \
			    WHERE kind = 'deployment_receipt' \
			      AND payload->>'action' = 'runtime_deploy' \
			    ORDER BY created_at_ms DESC, artifact_sha DESC \
			    LIMIT 1 \
			 ) \
			 SELECT CASE WHEN \
			   active.payload->>'image_digest' = '$$image_id' \
			   AND deployment.payload->>'image_digest' = '$$image_id' \
			   AND deployment.payload->>'active_agent_sha' = active.artifact_sha \
			   AND deployment.parent_sha = active.artifact_sha \
			   AND EXISTS ( \
			     SELECT 1 FROM news_agent_runtime_manifests AS manifest \
			      WHERE manifest.manifest_sha = active.payload->>'runtime_manifest_sha' \
			        AND manifest.image_digest = '$$image_id' \
			   ) \
			 THEN 'ok' ELSE 'mismatch' END \
			 FROM active CROSS JOIN deployment"); then \
			echo "Could not inspect the latest active/deployment receipt identity." >&2; \
			fail; \
		fi; \
		if [ "$$receipt_identity" != "ok" ]; then \
			echo "Latest active/deployment receipt does not prove requested image '$$image_id'." >&2; \
			fail; \
		fi; \
		make --no-print-directory status-app || fail; \
		echo "Tracefold deployed exact local image $$image_id."

status: ## fail closed unless the product and the execution runtime are both ready
	@$(MAKE) --no-print-directory status-app
	@$(MAKE) --no-print-directory runtime-status

status-app: preflight ## fail closed unless PostgreSQL, migration, Serve and Workers are ready
	@set -eu; \
		COMPOSE_PROFILES=execution; export COMPOSE_PROFILES; \
		docker compose ps --all; \
		failed=0; \
		for service in postgres rabbitmq serve workers; do \
			container_id=$$(docker compose ps -q "$$service"); \
			if [ -z "$$container_id" ]; then \
				echo "$$service: missing or stopped" >&2; \
				failed=1; \
				continue; \
			fi; \
			state=$$(docker inspect --format '{{.State.Status}}' "$$container_id"); \
			health=$$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$$container_id"); \
			if [ "$$state" != "running" ] || [ "$$health" != "healthy" ]; then \
				echo "$$service: state=$$state health=$$health" >&2; \
				failed=1; \
			fi; \
		done; \
		migrate_id=$$(docker compose ps --all -q migrate); \
		if [ -z "$$migrate_id" ]; then \
			echo "migrate: missing" >&2; \
			failed=1; \
		else \
			migrate_state=$$(docker inspect --format '{{.State.Status}}' "$$migrate_id"); \
			migrate_exit_code=$$(docker inspect --format '{{.State.ExitCode}}' "$$migrate_id"); \
			if [ "$$migrate_state" != "exited" ] || [ "$$migrate_exit_code" != "0" ]; then \
				echo "migrate: state=$$migrate_state exit_code=$$migrate_exit_code" >&2; \
				failed=1; \
			fi; \
		fi; \
		curl -fsS "$(TRACEFOLD_API_URL)/readyz" >/dev/null || { echo "serve readiness failed" >&2; failed=1; }; \
		curl -fsS "$(TRACEFOLD_WORKERS_URL)/readyz" >/dev/null || { echo "workers readiness failed" >&2; failed=1; }; \
		if ! console_html=$$(curl -fsS "$(TRACEFOLD_API_URL)/"); then \
			echo "console request failed" >&2; \
			failed=1; \
		elif ! printf '%s' "$$console_html" | grep -Eiq '<(!doctype html|html)'; then \
			echo "console HTML missing" >&2; \
			failed=1; \
		fi; \
		if [ "$$failed" -ne 0 ]; then \
			echo "Run make logs for diagnostics." >&2; \
			exit 1; \
		fi

logs: preflight ## tail all product runtime and dependency logs
	@COMPOSE_PROFILES=execution docker compose logs -f --tail=100 serve workers nautilus migrate postgres rabbitmq

down: preflight ## stop the container stack without deleting PostgreSQL data
	@set -eu; \
		if [ -n "$$(COMPOSE_PROFILES=execution docker compose ps --all -q nautilus)" ]; then \
			echo "the execution runtime is running and owns live exposure; run make runtime-down first." >&2; \
			exit 2; \
		fi; \
		COMPOSE_PROFILES=execution docker compose down

# The execution runtime lifecycle (#537 PR-2). It is separate from `up`/`deploy-image` on purpose:
# a News, Serve or Workers deploy must change nothing about the one process that owns live Binance
# exposure. `runtime-build` is the only entry here that builds, reaches GitHub, or takes the
# exact-main gate; `runtime-up`, `runtime-restart` and `runtime-down` move an already-proven image
# and never migrate. Cutover order for a release that also changes the runtime:
#   make runtime-build   (gated build of tracefold-runtime:<sha>)
#   make up              (News/Serve/Workers, including any migration)
#   make runtime-up      (stop the old runtime, start the new one)
runtime-build: preflight github-preflight ## build and tag the execution runtime image for this HEAD
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _runtime-build-locked

_runtime-build-locked:
	@python3 scripts/with_deployment_lock.py --assert-held
	@uv run python scripts/require_main_ci.py
	@set -eu; \
		$(call PIN_COMPOSE_STACK,runtime-build); \
		revision=$$(git rev-parse --verify HEAD); \
		token="$${GITHUB_TOKEN:-}"; \
		if [ -z "$$token" ] && command -v gh >/dev/null 2>&1; then \
			token=$$(gh auth token 2>/dev/null || true); \
		fi; \
		GITHUB_TOKEN="$$token"; \
		TRACEFOLD_BUILD_REVISION="$$revision"; \
		TRACEFOLD_RUNTIME_IMAGE="tracefold-runtime:$$revision"; \
		export GITHUB_TOKEN TRACEFOLD_BUILD_REVISION TRACEFOLD_RUNTIME_IMAGE; \
		docker compose build nautilus; \
		echo "Execution runtime image built: $$TRACEFOLD_RUNTIME_IMAGE"

runtime-up: preflight ## start or replace the execution runtime from an already built local image
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _runtime-up-locked RUNTIME_IMAGE="$(RUNTIME_IMAGE)"

_runtime-up-locked:
	@python3 scripts/with_deployment_lock.py --assert-held
	@set -eu; \
		$(call PIN_COMPOSE_STACK,runtime-up); \
		image="$(RUNTIME_IMAGE)"; \
		if [ -z "$$image" ]; then \
			echo "RUNTIME_IMAGE is empty; pass make runtime-up RUNTIME_IMAGE=tracefold-runtime:<sha>." >&2; \
			exit 2; \
		fi; \
		execution_mode=$$($(TRACEFOLD) config | $(READ_TRADING_EXECUTION_MODE)); \
		if [ "$$execution_mode" = disabled ]; then \
			echo "trading.execution.mode is disabled; there is no execution runtime to start." >&2; \
			exit 2; \
		fi; \
		if ! image_id=$$(docker image inspect --format '{{.Id}}' "$$image" 2>/dev/null); then \
			echo "Execution runtime image '$$image' is not in the local image store; run make runtime-build." >&2; \
			exit 2; \
		fi; \
		if ! image_head=$$(docker run --rm --entrypoint python "$$image" -c 'from tracefold.platform.postgres.migrations import latest_migration_version; print(latest_migration_version())'); then \
			echo "Could not inspect the Alembic head of '$$image'; the running runtime was not stopped." >&2; \
			exit 2; \
		fi; \
		if ! database_head=$$($(POSTGRES_READ_ONLY_PSQL) "SELECT version_num FROM alembic_version LIMIT 1"); then \
			echo "Could not inspect the live database Alembic head; the running runtime was not stopped." >&2; \
			exit 2; \
		fi; \
		if [ -z "$$database_head" ] || [ "$$image_head" != "$$database_head" ]; then \
			echo "Runtime image Alembic head '$$image_head' does not match the live database head '$$database_head'; the running runtime was not stopped." >&2; \
			exit 2; \
		fi; \
		previous_container=$$(docker compose ps --all -q nautilus); \
		if [ -n "$$previous_container" ]; then \
			previous_image=$$(docker inspect --format '{{.Config.Image}}' "$$previous_container"); \
			echo "Replacing execution runtime image $$previous_image; roll back with make runtime-up RUNTIME_IMAGE=$$previous_image"; \
		fi; \
		TRACEFOLD_RUNTIME_IMAGE="$$image"; \
		TRACEFOLD_IMAGE_DIGEST="$$image_id"; \
		export TRACEFOLD_RUNTIME_IMAGE TRACEFOLD_IMAGE_DIGEST; \
		docker compose stop -t 90 nautilus; \
		docker compose up -d --no-build --force-recreate --wait \
			--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) nautilus; \
		make --no-print-directory runtime-status

runtime-restart: preflight ## restart the execution runtime on the exact image it is already running
	@set -eu; \
		container_id=$$(COMPOSE_PROFILES=execution docker compose ps --all -q nautilus); \
		if [ -z "$$container_id" ]; then \
			echo "there is no execution runtime container to restart; run make runtime-up." >&2; \
			exit 2; \
		fi; \
		image=$$(docker inspect --format '{{.Config.Image}}' "$$container_id"); \
		make --no-print-directory runtime-up RUNTIME_IMAGE="$$image"

runtime-down: preflight ## stop and remove the execution runtime container
	@set -eu; \
		COMPOSE_PROFILES=execution; export COMPOSE_PROFILES; \
		docker compose stop -t 90 nautilus; \
		docker compose rm -f nautilus

runtime-logs: preflight ## tail the execution runtime log
	@COMPOSE_PROFILES=execution docker compose logs -f --tail=100 nautilus

runtime-status: preflight ## report the execution runtime container, health, and operator readiness
	@set -eu; \
		runtime_config=$$($(TRACEFOLD) config); \
		trading_enabled=$$(printf '%s\n' "$$runtime_config" | $(READ_TRADING_ENABLED)); \
		execution_mode=$$(printf '%s\n' "$$runtime_config" | $(READ_TRADING_EXECUTION_MODE)); \
		COMPOSE_PROFILES=execution; export COMPOSE_PROFILES; \
		failed=0; \
		container_id=$$(docker compose ps -q nautilus); \
		if [ "$$execution_mode" = disabled ]; then \
			if [ -n "$$container_id" ]; then \
				echo "execution runtime: disabled but nautilus is still running" >&2; \
				failed=1; \
			elif [ "$$trading_enabled" = true ]; then \
				echo "execution runtime: disabled (operator selected)"; \
			else \
				echo "execution runtime: disabled (Trading disabled)"; \
			fi; \
		elif [ -z "$$container_id" ]; then \
			echo "execution runtime: mode=$$execution_mode but no container is running; run make runtime-up" >&2; \
			failed=1; \
		else \
			state=$$(docker inspect --format '{{.State.Status}}' "$$container_id"); \
			health=$$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$$container_id"); \
			if [ "$$state" != "running" ] || [ "$$health" != "healthy" ]; then \
				echo "nautilus: state=$$state health=$$health" >&2; \
				failed=1; \
			fi; \
			echo "execution runtime image: $$(docker inspect --format '{{.Config.Image}}' "$$container_id")"; \
			if readiness=$$(curl -fsS "$(TRACEFOLD_NAUTILUS_URL)/readyz"); then \
				printf 'execution runtime readyz: %s\n' "$$readiness"; \
			else \
				echo "nautilus readiness failed" >&2; \
				failed=1; \
			fi; \
			if [ "$$failed" -eq 0 ]; then \
				echo "execution runtime: mode=$$execution_mode (Binance Runtime ready)"; \
			fi; \
		fi; \
		if [ "$$failed" -ne 0 ]; then \
			echo "Run make runtime-logs for diagnostics." >&2; \
			exit 1; \
		fi

serve-shell: preflight ## open a shell in the Serve container
	@docker compose exec serve /bin/sh

workers-shell: preflight ## open a shell in the Workers container
	@docker compose exec workers /bin/sh

clean: ## remove local test/cache artifacts
	@rm -rf .pytest_cache .ruff_cache __pycache__
	@find tracefold tests -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: docs-generated docs-db-schema docs-cli-help docs-rabbitmq-definitions

docs-generated: docs-db-schema docs-cli-help docs-rabbitmq-definitions ## regenerate docs/generated/* and the broker policy document

docs-db-schema: ## regenerate docs/generated/db-schema.md (requires Postgres)
	@uv run python scripts/regen_db_schema.py

docs-cli-help: ## regenerate docs/generated/cli-help.md
	@uv run python scripts/regen_cli_help.py

docs-rabbitmq-definitions:
	@uv run python scripts/regen_rabbitmq_definitions.py
