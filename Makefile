UV_CACHE_DIR ?= /tmp/tracefold-uv-cache
export UV_CACHE_DIR

TRACEFOLD := uv run tracefold

.PHONY: help sync install uninstall tool-path test lint compile check init config db-migrate db-health serve status recent asset-flow docker-check docker-up docker-status docker-logs docker-down docker-shell clean test-integration test-e2e test-golden test-architecture test-contract regen-contract install-hooks

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

test: ## run tests
	@uv run python -m pytest

lint: ## run ruff
	@uv run python -m ruff check .

compile: ## compile Python files
	@uv run python -m compileall src tests

check: ## run static, frontend, architecture, and public-contract checks
	@uv run ruff check .
	@uv run ruff format --check .
	@uv run mypy src
	@cd web && npm run typecheck && npm run lint && npm run format:check
	@uv run python -m pytest tests/architecture tests/contract -m "architecture or contract"
	@uv run python -m compileall src tests

test-integration: ## run only tests/integration/ (real PostgreSQL boundary)
	@uv run python -m pytest tests/integration -m integration

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

init: ## create ~/.tracefold/config.yaml + workers.yaml
	@$(TRACEFOLD) init

config: ## print effective runtime config
	@$(TRACEFOLD) config

db-migrate: ## apply PostgreSQL migrations
	@$(TRACEFOLD) db migrate

db-health: ## check PostgreSQL liveness and migration version
	@$(TRACEFOLD) db health

serve: ## run collector and API in foreground
	@$(TRACEFOLD) serve

status: ## print health and readiness for the running API
	@curl -fsS http://127.0.0.1:8765/healthz
	@curl -fsS http://127.0.0.1:8765/readyz

recent: ## print recent stored events
	@$(TRACEFOLD) recent --limit 20

asset-flow: ## print 5m token activity
	@$(TRACEFOLD) asset-flow --window 5m --limit 20

docker-check: ## verify Docker CLI, Compose plugin, and daemon access
	@command -v docker >/dev/null 2>&1 || { echo "docker is not installed or not on PATH" >&2; exit 127; }
	@docker compose version >/dev/null 2>&1 || { echo "docker compose plugin is unavailable" >&2; exit 127; }
	@docker info >/dev/null 2>&1 || { \
		echo "Docker daemon is not reachable from this shell." >&2; \
		echo "Start Docker Desktop or grant this terminal access to the Docker socket, then rerun make docker-up." >&2; \
		exit 1; \
	}

docker-up: docker-check init ## build and start container service
	@if [ -n "$${GITHUB_TOKEN:-}" ]; then \
		docker compose up -d --build app; \
	elif command -v gh >/dev/null 2>&1 && GITHUB_TOKEN=$$(gh auth token 2>/dev/null); then \
		GITHUB_TOKEN="$$GITHUB_TOKEN" docker compose up -d --build app; \
	else \
		docker compose up -d --build app; \
	fi

docker-status: ## show container and readiness
	@docker compose ps
	@curl -fsS http://127.0.0.1:8765/readyz || true

docker-logs: ## tail container logs
	@docker compose logs -f --tail=100 app

docker-down: ## stop container service
	@docker compose down

docker-shell: ## open shell in container
	@docker compose exec app /bin/sh

clean: ## remove local test/cache artifacts
	@rm -rf .pytest_cache .ruff_cache __pycache__
	@find src tests -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: docs-generated docs-db-schema docs-cli-help docs-score-versions docs-ws-protocol

docs-generated: docs-db-schema docs-cli-help docs-score-versions docs-ws-protocol ## regenerate docs/generated/*

docs-db-schema: ## regenerate docs/generated/db-schema.md (requires Postgres)
	@uv run python scripts/regen_db_schema.py

docs-cli-help: ## regenerate docs/generated/cli-help.md
	@uv run python scripts/regen_cli_help.py

docs-score-versions: ## regenerate docs/generated/score-versions.md
	@uv run python scripts/regen_score_versions.py

docs-ws-protocol: ## regenerate docs/generated/ws-protocol.md
	@uv run python scripts/regen_ws_protocol.py
