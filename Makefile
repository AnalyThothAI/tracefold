UV_CACHE_DIR ?= /tmp/tracefold-uv-cache
export UV_CACHE_DIR

TRACEFOLD := uv run tracefold
READ_NAUTILUS_CREDENTIALS_CONFIGURED := uv run python -c 'import json, sys; value = json.load(sys.stdin)["data"]["trading"]["nautilus"]["credentials_configured"]; print(str(value).lower()) if type(value) is bool else sys.exit("invalid credentials_configured")'
READ_TRADING_ENABLED := uv run python -c 'import json, sys; value = json.load(sys.stdin)["data"]["trading"]["enabled"]; print(str(value).lower()) if type(value) is bool else sys.exit("invalid trading enabled")'
TRACEFOLD_API_HOST ?= 127.0.0.1
TRACEFOLD_API_PORT ?= 8765
TRACEFOLD_WORKERS_HOST ?= 127.0.0.1
TRACEFOLD_WORKERS_PORT ?= 8766
TRACEFOLD_NAUTILUS_HOST ?= 127.0.0.1
TRACEFOLD_NAUTILUS_PORT ?= 8767
TRACEFOLD_API_URL ?= http://127.0.0.1:$(TRACEFOLD_API_PORT)
TRACEFOLD_WORKERS_URL ?= http://127.0.0.1:$(TRACEFOLD_WORKERS_PORT)
TRACEFOLD_NAUTILUS_URL ?= http://127.0.0.1:$(TRACEFOLD_NAUTILUS_PORT)
TRACEFOLD_COMPOSE_WAIT_SECONDS ?= 300
export TRACEFOLD_API_HOST TRACEFOLD_API_PORT TRACEFOLD_WORKERS_HOST TRACEFOLD_WORKERS_PORT TRACEFOLD_NAUTILUS_HOST TRACEFOLD_NAUTILUS_PORT

TRACEFOLD_TEST_ARTIFACT_DIR ?= artifacts/test-evidence
TRACEFOLD_TEST_LANE_DIR := $(TRACEFOLD_TEST_ARTIFACT_DIR)/lanes

.PHONY: help up _up-locked deploy-image _deploy-image-locked _trading-capability-bootstrap-if-needed verify-main-ci status logs down preflight github-preflight sync install uninstall tool-path test test-fast test-all test-evidence test-property test-slow test-scheduled test-frontend test-browser-smoke test-visual lint compile check init config db-migrate db-health db-provision-nautilus-role _db-provision-nautilus-role-locked serve workers serve-shell workers-shell clean trading-smoke trading-hard-cut-preflight _trading-hard-cut-preflight-if-needed test-integration test-deploy test-e2e test-golden test-architecture test-contract test-external-codegen regen-contract install-hooks

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

test: test-fast ## hermetic default regression (alias for test-fast)

test-fast: ## unit + hermetic contract + semantic architecture; no external resources
	@uv run python -m pytest -m "not integration and not deploy and not e2e and not golden and not live and not slow and not scheduled and not external_codegen"

test-all: test-frontend ## local convenience: every Python lane plus frontend; not verification evidence
	@uv run python -m pytest

test-evidence: ## exact-HEAD fail-closed deterministic verification evidence (explicitly excludes live)
	@uv run python -m tests.support.evidence --assert-clean
	@mkdir -p "$(TRACEFOLD_TEST_LANE_DIR)"
	@rm -f "$(TRACEFOLD_TEST_ARTIFACT_DIR)/manifest.json" "$(TRACEFOLD_TEST_LANE_DIR)"/*.json \
		"$(TRACEFOLD_TEST_ARTIFACT_DIR)"/vitest-*.json "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright.json" \
		"$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright-selection.json"
	@PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TRACEFOLD_TEST_EVIDENCE=1 uv run python -m pytest \
		-p tests.support.evidence -p _hypothesis_pytestplugin tests -m "not live and not scheduled" \
		--junitxml="$(TRACEFOLD_TEST_ARTIFACT_DIR)/junit.xml" \
		--durations=50 \
		--evidence-manifest="$(TRACEFOLD_TEST_LANE_DIR)/python.json" \
		--resource-evidence-manifest="$(TRACEFOLD_TEST_LANE_DIR)/resource.json"
	@uv run python -m tests.support.evidence record-command \
		--lane frontend-typecheck --output "$(TRACEFOLD_TEST_LANE_DIR)/frontend-typecheck.json" \
		--tool typescript=$$(node -p "require('./web/node_modules/typescript/package.json').version") \
		-- npm --prefix web run typecheck
	@uv run python -m tests.support.evidence record-command \
		--lane frontend-lint --output "$(TRACEFOLD_TEST_LANE_DIR)/frontend-lint.json" \
		--tool eslint=$$(node -p "require('./web/node_modules/eslint/package.json').version") \
		-- npm --prefix web run lint:eslint
	@cd web && TRACEFOLD_VITEST_SEMANTICS_REPORT="$(CURDIR)/$(TRACEFOLD_TEST_ARTIFACT_DIR)/vitest-architecture.json" \
		npm run test:architecture -- --allowOnly=false --reporter=./tests/support/evidenceReporter.ts
	@uv run python -m tests.support.evidence record-vitest \
		--lane frontend-architecture \
		--input "$(TRACEFOLD_TEST_ARTIFACT_DIR)/vitest-architecture.json" \
		--output "$(TRACEFOLD_TEST_LANE_DIR)/frontend-architecture.json"
	@cd web && TRACEFOLD_VITEST_SEMANTICS_REPORT="$(CURDIR)/$(TRACEFOLD_TEST_ARTIFACT_DIR)/vitest-unit.json" \
		npm run test:unit -- --allowOnly=false --reporter=./tests/support/evidenceReporter.ts
	@uv run python -m tests.support.evidence record-vitest \
		--lane frontend-unit \
		--input "$(TRACEFOLD_TEST_ARTIFACT_DIR)/vitest-unit.json" \
		--output "$(TRACEFOLD_TEST_LANE_DIR)/frontend-unit.json"
	@uv run python -m tests.support.evidence record-command \
		--lane frontend-format --output "$(TRACEFOLD_TEST_LANE_DIR)/frontend-format.json" \
		--tool prettier=$$(node -p "require('./web/node_modules/prettier/package.json').version") \
		-- npm --prefix web run format:check
	@uv run python -m tests.support.evidence record-command \
		--lane frontend-build --output "$(TRACEFOLD_TEST_LANE_DIR)/frontend-build.json" \
		--tool vite=$$(node -p "require('./web/node_modules/vite/package.json').version") \
		-- npm --prefix web run build
	@uv run python -m tests.browser.run_full_stack_smoke \
		--playwright-json "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright.json" \
		--playwright-selection "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright-selection.json"
	@uv run python -m tests.support.evidence record-playwright \
		--lane browser --input "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright.json" \
		--selection "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright-selection.json" \
		--output "$(TRACEFOLD_TEST_LANE_DIR)/browser.json"
	@uv run python -m tests.support.evidence --assert-clean
	@uv run python -m tests.support.evidence aggregate \
		--lane-dir "$(TRACEFOLD_TEST_LANE_DIR)" \
		--output "$(TRACEFOLD_TEST_ARTIFACT_DIR)/manifest.json" \
		--required-lane python \
		--required-lane resource \
		--required-lane frontend-typecheck \
		--required-lane frontend-lint \
		--required-lane frontend-architecture \
		--required-lane frontend-unit \
		--required-lane frontend-format \
		--required-lane frontend-build \
		--required-lane browser

test-property: ## bounded pure properties (TRACEFOLD_HYPOTHESIS_PROFILE=nightly for extended runs)
	@uv run python -m pytest -m property

test-slow: ## real-process Workers runtime tests bounded by wall-clock deadlines
	@uv run python -m pytest -m "slow and not scheduled"

test-scheduled: ## production-duration diagnostics; explicitly outside merge evidence
	@uv run python -m pytest -m scheduled --durations=20

test-frontend: ## frontend type, architecture, unit/component tests, format, and production build
	@cd web && npm run typecheck && npm run lint && npm run test:unit && npm run format:check && npm run build

test-browser-smoke: ## one real FastAPI static/bootstrap/bearer/news path in Chromium
	@rm -f "$(TRACEFOLD_TEST_LANE_DIR)/browser.json" \
		"$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright.json" \
		"$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright-selection.json"
	@npm --prefix web run build:checked
	@mkdir -p "$(TRACEFOLD_TEST_ARTIFACT_DIR)" "$(TRACEFOLD_TEST_LANE_DIR)"
	@uv run python -m tests.browser.run_full_stack_smoke \
		--playwright-json "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright.json" \
		--playwright-selection "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright-selection.json"
	@uv run python -m tests.support.evidence record-playwright \
		--lane browser --input "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright.json" \
		--selection "$(TRACEFOLD_TEST_ARTIFACT_DIR)/playwright-selection.json" \
		--output "$(TRACEFOLD_TEST_LANE_DIR)/browser.json"

test-visual: ## explicit four-viewport Playwright interaction/screenshot diagnostics
	@npm --prefix web run test:e2e

lint: ## run ruff
	@uv run python -m ruff check .

compile: ## compile Python files
	@uv run python -m compileall src tests

check: ## run hermetic static, architecture, contract, and generated drift checks
	@uv run ruff check .
	@uv run ruff format --check .
	@uv run mypy src
	@uv run python scripts/regen_cli_help.py --check
	@uv run python scripts/sync_agent_router.py --check
	@uv run python -m pytest tests/architecture tests/contract -m "(architecture or contract) and not generated and not external_codegen and not slow and not scheduled"
	@uv run python -m compileall src tests

test-integration: ## run only tests/integration/ (real PostgreSQL boundary), excluding slow
	@uv run python -m pytest tests/integration -m "integration and not slow and not scheduled"

trading-smoke: ## Case -> Intent atomicity and Nautilus outcome acceptance on real PostgreSQL (#283)
	@echo "This focused lane proves the native Intent ledger and atomic Case handoff."
	@echo "It does not contact Binance Demo and is not a substitute for test-evidence."
	@uv run python -m pytest tests/integration/test_trading_intents.py -m integration

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

db-migrate: preflight ## apply PostgreSQL migrations
	@make --no-print-directory _trading-hard-cut-preflight-if-needed
	@$(TRACEFOLD) db migrate

db-health: ## check PostgreSQL liveness and migration version
	@$(TRACEFOLD) db health

db-provision-nautilus-role: preflight ## offline one-shot provisioning for an existing PostgreSQL volume
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _db-provision-nautilus-role-locked

_db-provision-nautilus-role-locked:
	@python3 scripts/with_deployment_lock.py --assert-held
	@set -eu; \
		running=$$(docker compose ps -q); \
		if [ -n "$$running" ]; then \
			echo "Nautilus role provisioning is offline-only; stop the entire Tracefold stack first." >&2; \
			exit 1; \
		fi; \
		docker compose run --rm --no-deps --user postgres \
			--entrypoint /usr/local/bin/tracefold-provision-nautilus-role postgres

trading-hard-cut-preflight: preflight ## prove one-time Trading execution cutover prerequisites
	@set -eu; \
		nautilus_ids=$$(docker compose ps --all -q nautilus); \
		nautilus_count=$$(printf '%s\n' "$$nautilus_ids" | awk 'NF { count += 1 } END { print count + 0 }'); \
		if [ "$$nautilus_count" -ne 1 ]; then \
			echo "Hard cut requires exactly one Nautilus replica; found $$nautilus_count." >&2; \
			exit 1; \
		fi; \
		curl -fsS "$(TRACEFOLD_NAUTILUS_URL)/readyz" >/dev/null || { \
			echo "Hard cut requires Nautilus readiness to prove the Demo venue is authoritative flat." >&2; \
			exit 1; \
		}; \
		cut_state=$$(docker compose exec -T postgres sh -eu -c \
			'PGPASSWORD=$$(cat /run/secrets/postgres_serve_password); \
			PGOPTIONS="-c default_transaction_read_only=on"; \
			export PGPASSWORD PGOPTIONS; \
			exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold_serve -d tracefold -c "$$1"' sh \
			"SELECT concat_ws('|', \
			  COALESCE((SELECT control FROM trading_runtime_state WHERE id = 1), 'MISSING'), \
			  (SELECT count(*) FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')), \
			  (SELECT count(*) FROM trading_intents WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')), \
			  (SELECT count(*) FROM trading_orders WHERE state IN ('PREPARED', 'AWAITING_APPROVAL', 'APPROVED', 'SUBMITTING', 'AMBIGUOUS', 'RECONCILING', 'MANUAL_REVIEW_REQUIRED', 'ACKNOWLEDGED', 'PARTIAL', 'OPEN', 'UNPROTECTED', 'SAFETY_CLOSING')))"); \
		if [ "$$cut_state" != "PAUSED|0|0|0" ]; then \
			echo "Hard cut requires PAUSED|pending_cases=0|nonterminal_intents=0|active_legacy_orders=0; observed $$cut_state." >&2; \
			exit 1; \
		fi; \
		echo "Trading hard-cut preflight passed: venue flat, PAUSED, ledgers drained, one Nautilus replica."

_trading-hard-cut-preflight-if-needed:
	@set -eu; \
		postgres_id=$$(docker compose ps -q postgres); \
		if [ -z "$$postgres_id" ]; then \
			echo "Cannot inspect the database before migration; start the PostgreSQL service first." >&2; \
			exit 2; \
		fi; \
		schema_state=$$(docker compose exec -T postgres sh -eu -c \
			'PGPASSWORD=$$(cat /run/secrets/postgres_serve_password); \
			PGOPTIONS="-c default_transaction_read_only=on"; \
			export PGPASSWORD PGOPTIONS; \
			exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold_serve -d tracefold \
			-c "SELECT CASE WHEN to_regclass('"'"'public.alembic_version'"'"') IS NULL THEN '"'"'fresh'"'"' ELSE '"'"'existing'"'"' END"'); \
		if [ "$$schema_state" = fresh ]; then \
			echo "Fresh database: no PR 1 execution authority exists to cut over."; \
			exit 0; \
		fi; \
		migration_state=$$(docker compose exec -T postgres sh -eu -c \
			'PGPASSWORD=$$(cat /run/secrets/postgres_serve_password); \
			PGOPTIONS="-c default_transaction_read_only=on"; \
			export PGPASSWORD PGOPTIONS; \
			exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold_serve -d tracefold -c "$$1"' sh \
			"SELECT concat_ws('|', \
			  (SELECT version_num FROM alembic_version LIMIT 1), \
			  EXISTS ( \
			    SELECT 1 FROM pg_constraint \
			     WHERE conname = 'trading_cases_state_check' \
			       AND pg_get_constraintdef(oid) LIKE '%INTENT_EMITTED%' \
			  ), \
			  EXISTS ( \
			    SELECT 1 FROM pg_class \
			     WHERE relnamespace = 'public'::regnamespace \
			       AND relname = 'trading_execution_capability_snapshots' \
			       AND relkind = 'r' \
			  ))"); \
		case "$$migration_state" in \
			20260828_0316\|f\|f|20260828_0317\|t\|f|20260828_0318\|t\|f) \
				make --no-print-directory trading-hard-cut-preflight ;; \
			*\|t\|t) echo "Trading hard cut is already present at database head $${migration_state%%|*}." ;; \
			*) echo "Database state '$$migration_state' cannot safely enter the Trading hard cut." >&2; exit 2 ;; \
		esac

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
		if [ -n "$${COMPOSE_FILE:-}" ] || [ -n "$${COMPOSE_PROJECT_NAME:-}" ] || \
			[ -n "$${COMPOSE_ENV_FILES:-}" ] || [ -n "$${COMPOSE_PROFILES:-}" ] || \
			[ -n "$${COMPOSE_PATH_SEPARATOR:-}" ] || [ -n "$${COMPOSE_DISABLE_ENV_FILE:-}" ]; then \
			echo "up refuses inherited Compose stack variables; rerun without COMPOSE_* overrides." >&2; \
			exit 2; \
		fi; \
		COMPOSE_FILE="$$(pwd -P)/compose.yaml"; \
		COMPOSE_PROJECT_NAME=tracefold; \
		export COMPOSE_FILE COMPOSE_PROJECT_NAME; \
		$(TRACEFOLD) init; \
		unset TRACEFOLD_APP_IMAGE; \
		token="$${GITHUB_TOKEN:-}"; \
		if [ -z "$$token" ] && command -v gh >/dev/null 2>&1; then \
			token=$$(gh auth token 2>/dev/null || true); \
		fi; \
		GITHUB_TOKEN="$$token"; \
		TRACEFOLD_BUILD_REVISION=$$(git rev-parse --verify HEAD); \
		export GITHUB_TOKEN TRACEFOLD_BUILD_REVISION; \
		fail() { \
			docker compose ps --all >&2 || true; \
			echo "Startup failed. Run make logs for diagnostics." >&2; \
			exit 1; \
		}; \
		runtime_config=$$($(TRACEFOLD) config); \
		trading_enabled=$$(printf '%s\n' "$$runtime_config" | $(READ_TRADING_ENABLED)); \
		nautilus_configured=$$(printf '%s\n' "$$runtime_config" | $(READ_NAUTILUS_CREDENTIALS_CONFIGURED)); \
		if [ "$$trading_enabled" = true ] && [ "$$nautilus_configured" != true ]; then \
			echo "Trading is enabled but Binance Demo Nautilus credentials are not configured." >&2; \
			exit 1; \
		fi; \
		docker compose build migrate || fail; \
		image=$$(docker compose config --images migrate 2>/dev/null \
			| grep -v '@sha256:' | head -n 1); \
		TRACEFOLD_IMAGE_DIGEST=$$(docker image inspect --format '{{.Id}}' "$$image" 2>/dev/null || true); \
		export TRACEFOLD_IMAGE_DIGEST; \
		if [ -z "$$TRACEFOLD_IMAGE_DIGEST" ]; then \
			echo "WARNING: could not read the digest of $${image:-the built image}." >&2; \
			echo "  Deployment continues, but every runtime manifest it writes records" >&2; \
			echo "  image_digest=unversioned and cannot close a learning promotion." >&2; \
		fi; \
		docker compose up -d --no-build --wait --wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) postgres || fail; \
		make --no-print-directory _trading-hard-cut-preflight-if-needed || fail; \
		runtime_services="migrate serve workers"; \
		docker compose stop -t 40 workers serve nautilus || fail; \
		docker compose up -d --no-build --force-recreate --wait \
			--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) $$runtime_services || fail; \
		if [ "$$trading_enabled" = true ]; then \
			make --no-print-directory _trading-capability-bootstrap-if-needed || fail; \
		fi; \
		make --no-print-directory status || fail; \
		echo "Tracefold ready at $(TRACEFOLD_API_URL)"

_trading-capability-bootstrap-if-needed:
	@set -eu; \
		active_capability=$$(docker compose exec -T postgres sh -eu -c \
			'PGPASSWORD=$$(cat /run/secrets/postgres_serve_password); \
			PGOPTIONS="-c default_transaction_read_only=on"; \
			export PGPASSWORD PGOPTIONS; \
			exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold_serve -d tracefold \
			-c "SELECT active_capability_snapshot_sha256 FROM trading_runtime_state WHERE id = 1"'); \
		if [ -z "$$active_capability" ]; then \
			echo "Bootstrapping the first Trading execution capability snapshot."; \
			docker compose up -d --no-build --force-recreate nautilus; \
			bootstrap_ready=; attempt=0; \
			while [ "$$attempt" -lt 60 ]; do \
				bootstrap_ready=$$(docker compose exec -T postgres sh -eu -c \
					'PGPASSWORD=$$(cat /run/secrets/postgres_serve_password); \
					PGOPTIONS="-c default_transaction_read_only=on"; \
					export PGPASSWORD PGOPTIONS; \
					exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold_serve -d tracefold \
					-c "SELECT CASE WHEN nautilus_bootstrap_account_zero_at_ms IS NOT NULL \
					AND nautilus_bootstrap_account_zero_at_ms >= \
					floor(extract(epoch from clock_timestamp()) * 1000)::bigint - 15000 \
					AND NOT nautilus_unexpected_exposure THEN '\''ready'\'' ELSE '\'''\'' END \
					FROM trading_runtime_state WHERE id = 1"'); \
				[ "$$bootstrap_ready" = ready ] && break; \
				attempt=$$((attempt + 1)); sleep 1; \
			done; \
			if [ "$$bootstrap_ready" != ready ]; then \
				echo "Nautilus did not establish a fresh bootstrap account-zero proof." >&2; \
				exit 1; \
			fi; \
			docker compose run --rm --no-deps --entrypoint tracefold workers trading refresh-capabilities; \
		fi; \
		docker compose up -d --no-build --force-recreate --wait \
			--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) nautilus

deploy-image: preflight github-preflight ## deploy an explicit local DB-compatible sha256 image from the primary checkout
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _deploy-image-locked

_deploy-image-locked:
	@python3 scripts/with_deployment_lock.py --assert-held
	@uv run python scripts/require_main_ci.py
	@set -eu; \
		if [ -n "$${COMPOSE_FILE:-}" ] || [ -n "$${COMPOSE_PROJECT_NAME:-}" ] || \
			[ -n "$${COMPOSE_ENV_FILES:-}" ] || [ -n "$${COMPOSE_PROFILES:-}" ] || \
			[ -n "$${COMPOSE_PATH_SEPARATOR:-}" ] || [ -n "$${COMPOSE_DISABLE_ENV_FILE:-}" ]; then \
			echo "deploy-image refuses inherited Compose stack variables; rerun without COMPOSE_* overrides." >&2; \
			exit 2; \
		fi; \
		COMPOSE_FILE="$$(pwd -P)/compose.yaml"; \
		COMPOSE_PROJECT_NAME=tracefold; \
		unset COMPOSE_ENV_FILES COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR COMPOSE_DISABLE_ENV_FILE; \
		export COMPOSE_FILE COMPOSE_PROJECT_NAME; \
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
			':(glob)src/tracefold/platform/postgres/alembic/versions/*.py'); \
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
		if ! database_head=$$(docker compose exec -T postgres sh -eu -c \
			'PGPASSWORD=$$(cat /run/secrets/postgres_serve_password); \
			PGOPTIONS="-c default_transaction_read_only=on"; \
			export PGPASSWORD PGOPTIONS; \
			exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold_serve -d tracefold \
			-c "SELECT version_num FROM alembic_version LIMIT 1"'); then \
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
		if ! nautilus_configured=$$(printf '%s\n' "$$runtime_config" | $(READ_NAUTILUS_CREDENTIALS_CONFIGURED)); then \
			echo "Target image did not report Nautilus credential availability; no services were stopped." >&2; \
			exit 2; \
		fi; \
		if ! trading_enabled=$$(printf '%s\n' "$$runtime_config" | $(READ_TRADING_ENABLED)); then \
			echo "Target image did not report whether Trading is enabled; no services were stopped." >&2; \
			exit 2; \
		fi; \
		if [ "$$trading_enabled" = true ] && [ "$$nautilus_configured" != true ]; then \
			echo "Trading is enabled but Binance Demo Nautilus credentials are not configured; no services were stopped." >&2; \
			exit 2; \
		fi; \
		fail() { \
			docker compose ps --all >&2 || true; \
			echo "Exact-image deployment failed. Run make logs for diagnostics." >&2; \
			exit 1; \
		}; \
		runtime_services="migrate serve workers"; \
		base_services="$$runtime_services"; \
		if [ "$$trading_enabled" = true ]; then runtime_services="$$runtime_services nautilus"; fi; \
		docker compose stop -t 40 workers serve nautilus || fail; \
		docker compose up -d --no-build --force-recreate --wait \
			--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) $$base_services || fail; \
		if [ "$$trading_enabled" = true ]; then \
			make --no-print-directory _trading-capability-bootstrap-if-needed || fail; \
		fi; \
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
			'PGPASSWORD=$$(cat /run/secrets/postgres_serve_password); \
			PGOPTIONS="-c default_transaction_read_only=on"; \
			export PGPASSWORD PGOPTIONS; \
			exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold_serve -d tracefold \
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
		make --no-print-directory status || fail; \
		echo "Tracefold deployed exact local image $$image_id."

status: preflight ## fail closed unless every enabled runtime is ready
	@docker compose ps --all
	@set -eu; \
		runtime_config=$$($(TRACEFOLD) config); \
		trading_enabled=$$(printf '%s\n' "$$runtime_config" | $(READ_TRADING_ENABLED)); \
		nautilus_configured=$$(printf '%s\n' "$$runtime_config" | $(READ_NAUTILUS_CREDENTIALS_CONFIGURED)); \
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
		if [ "$$trading_enabled" = true ]; then \
			if [ "$$nautilus_configured" != true ]; then \
				echo "nautilus: Trading enabled but Demo credentials are not configured" >&2; \
				failed=1; \
			else \
				nautilus_ids=$$(docker compose ps --all -q nautilus); \
				nautilus_count=$$(printf '%s\n' "$$nautilus_ids" | awk 'NF { count += 1 } END { print count + 0 }'); \
				if [ "$$nautilus_count" -ne 1 ]; then \
					echo "nautilus: expected exactly one replica, found $$nautilus_count" >&2; \
					failed=1; \
				else \
					nautilus_state=$$(docker inspect --format '{{.State.Status}}' "$$nautilus_ids"); \
					nautilus_health=$$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$$nautilus_ids"); \
					if [ "$$nautilus_state" != "running" ] || [ "$$nautilus_health" != "healthy" ]; then \
						echo "nautilus: state=$$nautilus_state health=$$nautilus_health" >&2; \
						failed=1; \
					else \
						curl -fsS "$(TRACEFOLD_NAUTILUS_URL)/readyz" >/dev/null || { echo "nautilus readiness failed" >&2; failed=1; }; \
					fi; \
				fi; \
			fi; \
		else \
			echo "nautilus: not required (Trading disabled)"; \
		fi; \
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
	@docker compose logs -f --tail=100 serve workers nautilus migrate postgres rabbitmq

down: preflight ## stop the container stack without deleting PostgreSQL data
	@docker compose down

serve-shell: preflight ## open a shell in the Serve container
	@docker compose exec serve /bin/sh

workers-shell: preflight ## open a shell in the Workers container
	@docker compose exec workers /bin/sh

clean: ## remove local test/cache artifacts
	@rm -rf .pytest_cache .ruff_cache __pycache__
	@find src tests -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: docs-generated docs-db-schema docs-cli-help

docs-generated: docs-db-schema docs-cli-help ## regenerate docs/generated/*

docs-db-schema: ## regenerate docs/generated/db-schema.md (requires Postgres)
	@uv run python scripts/regen_db_schema.py

docs-cli-help: ## regenerate docs/generated/cli-help.md
	@uv run python scripts/regen_cli_help.py
