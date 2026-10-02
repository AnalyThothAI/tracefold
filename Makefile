# Public task interface. Compose owns topology; Python owns lifecycle sequencing.
.DEFAULT_GOAL := help
SHELL := /bin/sh

# No duplicated defaults: Compose resolves .env, shell and explicit Make overrides.

help: ## show deployment and development commands without contacting external tools
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "%-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## build the app image and initialize private operator files without starting services
	@python3 scripts/deploy.py init

build: ## build the application image from this checkout without starting services
	@python3 scripts/deploy.py build

up: ## build, validate, migrate and start the application and DEMO executor
	@python3 scripts/deploy.py up

config: ## show redacted application configuration using the installed container image
	@python3 scripts/deploy.py config

topology: ## show the effective project, config path, service names and published URLs
	@python3 scripts/deploy.py topology

deploy-image: ## replace the application with a local same-schema IMAGE_ID=sha256:...
	@python3 scripts/deploy.py deploy-image

status: ## report application and executor container status
	@python3 scripts/deploy.py status

status-app: ## check application health, one-shot completion, readiness and console
	@python3 scripts/deploy.py status-app

logs: ## follow all service logs, including Analysis and broker policy
	@python3 scripts/deploy.py logs

down: ## stop the stack; preserve all named volumes
	@python3 scripts/deploy.py down

db-migrate: ## build and migrate in a maintenance window; leave application roles stopped
	@python3 scripts/deploy.py db-migrate

db-health: ## check the database from the Workers container
	@python3 scripts/deploy.py db-health

serve-shell: ## open a shell in Serve
	@python3 scripts/deploy.py serve-shell

workers-shell: ## open a shell in Workers
	@python3 scripts/deploy.py workers-shell

sync: ## install locked development dependencies (uv manages Python 3.13)
	@uv sync --locked

verify-main-ci: ## optional release provenance check for the exact green primary origin/main
	@uv run --locked python scripts/require_main_ci.py

dev-serve: ## run Serve in the foreground against an explicitly isolated development config
	@uv run --locked tracefold serve

dev-workers: ## run Workers in the foreground against an explicitly isolated development config
	@uv run --locked tracefold workers

dev-analysis: ## run Analysis in the foreground against an explicitly isolated development config
	@uv run --locked tracefold analysis

dev-executor: ## run the DEMO executor in the foreground against an isolated config
	@uv run --locked tracefold executor

include make/checks.mk

.PHONY: help sync verify-main-ci dev-serve dev-workers dev-analysis dev-executor init build up config topology deploy-image status status-app logs down db-migrate db-health serve-shell workers-shell
