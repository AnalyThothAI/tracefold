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

.PHONY: help up _up-locked build-news-rollback-image deploy-image _deploy-image-locked status logs down preflight sync install uninstall tool-path test test-all test-slow lint compile check init config db-migrate db-health serve workers serve-shell workers-shell clean test-integration test-e2e test-golden test-architecture test-contract regen-contract install-hooks

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
	@uv run python -m tests.support.refactor_baseline --check
	@uv run python scripts/sync_agent_router.py --check
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

up: preflight ## build, migrate, start, and verify the complete product
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _up-locked

_up-locked:
	@test "$${TRACEFOLD_DEPLOY_LOCK_HELD:-}" = "1" || { echo "Use make up; the locked implementation target is private." >&2; exit 2; }
	@$(TRACEFOLD) init
	@set -eu; \
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
		docker compose stop -t 40 workers serve || fail; \
		docker compose up -d --no-build --force-recreate --wait \
			--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) migrate serve workers || fail; \
		make --no-print-directory status || fail; \
		echo "Tracefold ready at $(TRACEFOLD_API_URL)"

build-news-rollback-image: preflight ## build the independent Program-v5/schema-0301 rollback image
	@set -eu; \
		git_dir=$$(git rev-parse --absolute-git-dir); \
		git_common_dir=$$(git rev-parse --path-format=absolute --git-common-dir); \
		branch=$$(git branch --show-current); \
		if [ "$$git_dir" != "$$git_common_dir" ] || [ "$$branch" != "main" ]; then \
			echo "build-news-rollback-image must run from the primary checkout on main." >&2; \
			exit 2; \
		fi; \
		if ! git diff --quiet --ignore-submodules -- || \
			! git diff --cached --quiet --ignore-submodules --; then \
			echo "build-news-rollback-image refuses tracked or staged changes." >&2; \
			exit 2; \
		fi; \
		relevant_untracked=$$(git ls-files --others --exclude-standard -- ':(exclude)docs/**'); \
		ignored_deployment_inputs=$$(git ls-files --others -- \
			Dockerfile .dockerignore deploy/news-program-v5-schema0301); \
		if [ -n "$$relevant_untracked" ] || [ -n "$$ignored_deployment_inputs" ]; then \
			echo "build-news-rollback-image refuses untracked build inputs outside docs/." >&2; \
			exit 2; \
		fi; \
		if ! origin_main=$$(git rev-parse --verify refs/remotes/origin/main 2>/dev/null); then \
			echo "build-news-rollback-image requires a local origin/main ref." >&2; \
			exit 2; \
		fi; \
		head=$$(git rev-parse --verify HEAD); \
		if [ "$$head" != "$$origin_main" ]; then \
			echo "build-news-rollback-image requires primary main HEAD to equal origin/main." >&2; \
			exit 2; \
		fi; \
		bundle="$$(pwd -P)/deploy/news-program-v5-schema0301"; \
		profile="$${bundle}/profile.json"; \
		source_revision=$$(uv run python -c \
			'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["source_revision"])' "$$profile"); \
		schema_head=$$(uv run python -c \
			'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["migration_head"])' "$$profile"); \
		rollback_sha=$$(uv run python -c \
			'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["program_sha256"])' "$$profile"); \
		current_schema_head=$$(uv run python -c \
			'from tracefold.platform.postgres.migrations import latest_migration_version; print(latest_migration_version())'); \
		if [ "$$current_schema_head" != "$$schema_head" ]; then \
			echo "Rollback profile schema '$$schema_head' does not match current source head '$$current_schema_head'." >&2; \
			exit 2; \
		fi; \
		rollback_context=$$(mktemp -d); \
		if [ -z "$$rollback_context" ] || [ ! -d "$$rollback_context" ]; then \
			echo "Could not allocate the isolated rollback build context." >&2; \
			exit 2; \
		fi; \
		cleanup_rollback_context() { \
			if [ -n "$$rollback_context" ] && [ -d "$$rollback_context" ]; then \
				rm -rf -- "$$rollback_context"; \
			fi; \
		}; \
		trap cleanup_rollback_context EXIT HUP INT TERM; \
		uv run python "$$bundle/prepare_context.py" \
			--repo "$$(pwd -P)" --output "$$rollback_context" >/dev/null; \
		token="$${GITHUB_TOKEN:-}"; \
		if [ -z "$$token" ] && command -v gh >/dev/null 2>&1; then \
			token=$$(gh auth token 2>/dev/null || true); \
		fi; \
		GITHUB_TOKEN="$$token"; \
		export GITHUB_TOKEN; \
		tag="tracefold-app:program-v5-schema0301-rollback-$$(printf '%s' "$$head" | cut -c1-12)"; \
		DOCKER_BUILDKIT=1 docker build \
			--secret id=github_token,env=GITHUB_TOKEN \
			--file "$$bundle/Dockerfile" \
			--build-arg TRACEFOLD_BUILD_REVISION="$$head" \
			--build-arg TRACEFOLD_ROLLBACK_SOURCE_REVISION="$$source_revision" \
			--build-arg TRACEFOLD_ROLLBACK_SCHEMA_HEAD="$$schema_head" \
			--tag "$$tag" "$$rollback_context"; \
		image_id=$$(docker image inspect --format '{{.Id}}' "$$tag"); \
		image_profile=$$(docker image inspect --format '{{index .Config.Labels "io.tracefold.news.rollback.profile"}}' "$$image_id"); \
		revision=$$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$$image_id"); \
		image_source=$$(docker image inspect --format '{{index .Config.Labels "io.tracefold.news.rollback.source-revision"}}' "$$image_id"); \
		image_schema=$$(docker image inspect --format '{{index .Config.Labels "io.tracefold.news.rollback.schema-head"}}' "$$image_id"); \
		runtime_identity=$$(docker run --rm --entrypoint python "$$image_id" -c \
			'from tracefold.news.agents.semantic_program import PROGRAM_FACTORY_ID,load_stable_program_artifact; from tracefold.news.models import TRIAGE_POLICY_VERSION; from tracefold.platform.postgres.postgres_migrations import latest_migration_version; print("|".join((latest_migration_version(), PROGRAM_FACTORY_ID, TRIAGE_POLICY_VERSION, load_stable_program_artifact().program_sha256)))'); \
		loaded_schema=$$(printf '%s' "$$runtime_identity" | cut -d '|' -f 1); \
		loaded_factory=$$(printf '%s' "$$runtime_identity" | cut -d '|' -f 2); \
		loaded_policy=$$(printf '%s' "$$runtime_identity" | cut -d '|' -f 3); \
		loaded_sha=$$(printf '%s' "$$runtime_identity" | cut -d '|' -f 4); \
		if [ "$$image_profile" != "program_v5_schema0301_rollback" ] || [ "$$revision" != "$$head" ] || \
			[ "$$image_source" != "$$source_revision" ] || [ "$$image_schema" != "$$schema_head" ] || \
			[ "$$loaded_schema" != "$$schema_head" ] || \
			[ "$$loaded_factory" != "tracefold.news.semantic_program.factory_v3" ] || \
			[ "$$loaded_policy" != "news_triage_policy_v9" ] || [ "$$loaded_sha" != "$$rollback_sha" ]; then \
			echo "Rollback image identity verification failed." >&2; \
			exit 2; \
		fi; \
		docker run --rm --entrypoint python "$$image_id" scripts/verify_news_rollback.py >/dev/null; \
		uv run python "$$bundle/drill_image.py" --image-id "$$image_id" >/dev/null; \
		echo "Tracefold rollback image built and verified."; \
		echo "  image_id=$$image_id"; \
		echo "  source_revision=$$source_revision"; \
		echo "  schema_head=$$loaded_schema"; \
		echo "  factory_id=$$loaded_factory"; \
		echo "  policy_version=$$loaded_policy"; \
		echo "  program_sha256=$$loaded_sha"; \
		echo "  build_revision=$$revision"; \
		echo "  disposable_schema_drill=passed"; \
		echo "Deploy only with: make deploy-image IMAGE_ID=$$image_id"

deploy-image: preflight ## deploy an explicit local DB-compatible sha256 image from the primary checkout
	@uv run python scripts/with_deployment_lock.py make --no-print-directory _deploy-image-locked

_deploy-image-locked:
	@test "$${TRACEFOLD_DEPLOY_LOCK_HELD:-}" = "1" || { echo "Use make deploy-image; the locked implementation target is private." >&2; exit 2; }
	@set -eu; \
		if [ -n "$${COMPOSE_FILE:-}" ] || [ -n "$${COMPOSE_PROJECT_NAME:-}" ] || \
			[ -n "$${COMPOSE_ENV_FILES:-}" ] || [ -n "$${COMPOSE_PROFILES:-}" ]; then \
			echo "deploy-image refuses inherited Compose stack variables; unset COMPOSE_FILE, COMPOSE_PROJECT_NAME, COMPOSE_ENV_FILES, and COMPOSE_PROFILES." >&2; \
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
		if ! docker compose run --rm --no-deps --entrypoint tracefold migrate config >/dev/null; then \
			echo "Target image could not parse the active operator config; no services were stopped." >&2; \
			exit 2; \
		fi; \
		fail() { \
			docker compose ps --all >&2 || true; \
			echo "Exact-image deployment failed. Run make logs for diagnostics." >&2; \
			exit 1; \
		}; \
		docker compose stop -t 40 workers serve || fail; \
		docker compose up -d --no-build --force-recreate --wait \
			--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) migrate serve workers || fail; \
		for service in migrate serve workers; do \
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
