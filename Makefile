UV_CACHE_DIR ?= /tmp/tracefold-uv-cache
export UV_CACHE_DIR

TRACEFOLD := uv run tracefold
TRACEFOLD_API_HOST ?= 127.0.0.1
TRACEFOLD_API_PORT ?= 8765
TRACEFOLD_WORKERS_HOST ?= 127.0.0.1
TRACEFOLD_WORKERS_PORT ?= 8766
TRACEFOLD_API_URL ?= http://127.0.0.1:$(TRACEFOLD_API_PORT)
TRACEFOLD_WORKERS_URL ?= http://127.0.0.1:$(TRACEFOLD_WORKERS_PORT)
TRACEFOLD_COMPOSE_WAIT_SECONDS ?= 300
export TRACEFOLD_API_HOST TRACEFOLD_API_PORT TRACEFOLD_WORKERS_HOST TRACEFOLD_WORKERS_PORT

.PHONY: help up status logs down preflight sync install uninstall tool-path test test-all test-slow lint compile check init config db-migrate db-health serve workers serve-shell workers-shell clean test-integration test-e2e test-golden test-architecture test-contract regen-contract install-hooks

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

test: ## fast regression: unit + architecture + contract + integration, excluding slow/e2e/golden
	@uv run python -m pytest -m "not slow" --ignore=tests/e2e --ignore=tests/golden

test-all: ## every lane including slow runtime, e2e, and golden
	@uv run python -m pytest

test-slow: ## real-process Workers runtime tests bounded by wall-clock deadlines
	@uv run python -m pytest tests/integration -m slow

lint: ## run ruff
	@uv run python -m ruff check .

compile: ## compile Python files
	@uv run python -m compileall src tests

check: ## run static, frontend, architecture, and public-contract checks
	@uv run ruff check .
	@uv run ruff format --check .
	@uv run mypy src
	@cd web && npm run typecheck && npm run lint && npm run format:check
	@uv run python scripts/regen_cli_help.py --check
	@uv run python -m pytest tests/architecture tests/contract -m "architecture or contract"
	@uv run python -m compileall src tests

test-integration: ## run only tests/integration/ (real PostgreSQL boundary), excluding slow
	@uv run python -m pytest tests/integration -m "integration and not slow"

test-e2e: ## run only tests/e2e/ (running service boundary)
	@uv run python -m pytest tests/e2e -m e2e

test-golden: ## run only tests/golden/ (real Postgres golden corpus)
	@uv run python -m pytest tests/golden -m golden

test-architecture: ## run only tests/architecture/ (AST/grep checks)
	@uv run python -m pytest tests/architecture -m architecture

test-contract: ## run only tests/contract/
	@uv run python -m pytest tests/contract -m contract

regen-contract: ## regenerate openapi.json + web/src/lib/types/openapi.ts
	@uv run python scripts/regen_openapi.py
	@cd web && npm run generate:types && cd ..

install-hooks: ## install pre-commit hooks
	@uv run pre-commit install

init: ## create ~/.tracefold/config.yaml + PostgreSQL role password files
	@$(TRACEFOLD) init

config: ## print effective runtime config
	@$(TRACEFOLD) config

db-migrate: ## apply PostgreSQL migrations
	@$(TRACEFOLD) db migrate

db-health: ## check PostgreSQL liveness and migration version
	@$(TRACEFOLD) db health

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

up: preflight init ## build, migrate, start, and verify the complete product
	@set -eu; \
		token="$${GITHUB_TOKEN:-}"; \
		if [ -z "$$token" ] && command -v gh >/dev/null 2>&1; then \
			token=$$(gh auth token 2>/dev/null || true); \
		fi; \
		GITHUB_TOKEN="$$token"; \
		TRACEFOLD_BUILD_REVISION=$$(git rev-parse --verify HEAD); \
		export GITHUB_TOKEN TRACEFOLD_BUILD_REVISION; \
		if ! docker compose build migrate || \
			! docker compose stop -t 40 workers serve || \
			! docker compose up -d --no-build --force-recreate --wait \
				--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) migrate serve workers; then \
			docker compose ps --all >&2 || true; \
			echo "Startup failed. Run make logs for diagnostics." >&2; \
			exit 1; \
		fi
	@$(MAKE) --no-print-directory status
	@echo "Tracefold ready at $(TRACEFOLD_API_URL)"

status: preflight ## fail closed unless database, API, Workers, and console are ready
	@docker compose ps --all
	@set -eu; \
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

logs: preflight ## tail Serve, Workers, migration, PostgreSQL, and RabbitMQ logs
	@docker compose logs -f --tail=100 serve workers migrate postgres rabbitmq

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
