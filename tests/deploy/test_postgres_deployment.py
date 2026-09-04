from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.deploy

POSTGRES_IMAGE = "postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296"
DATABASE_SECRET = "postgres_database_password"


def test_required_and_scheduled_postgres_evidence_uses_the_production_image() -> None:
    ci = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
    scheduled = yaml.safe_load(Path(".github/workflows/scheduled-diagnostics.yml").read_text())

    assert ci["jobs"]["postgres-behavior"]["services"]["postgres"]["image"] == POSTGRES_IMAGE
    assert ci["jobs"]["migration"]["services"]["postgres"]["image"] == POSTGRES_IMAGE
    assert ci["jobs"]["runtime-broker"]["services"]["postgres"]["image"] == POSTGRES_IMAGE
    assert ci["jobs"]["deploy-e2e"]["services"]["postgres"]["image"] == POSTGRES_IMAGE
    scheduled_job = scheduled["jobs"]["production-duration"]
    assert scheduled_job["services"]["postgres"]["image"] == POSTGRES_IMAGE
    run_step = next(
        step for step in scheduled_job["steps"] if step.get("name") == "Run production-duration diagnostics"
    )
    assert run_step["env"]["TRACEFOLD_POSTGRES_IMAGE"] == POSTGRES_IMAGE


def test_compose_keeps_processes_separate_but_uses_one_postgres_login() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {
        "migrate",
        "nautilus",
        "postgres",
        "rabbitmq",
        "rabbitmq-policy",
        "serve",
        "workers",
    }
    assert services["rabbitmq"]["image"].startswith("rabbitmq:4.3")
    assert "build" not in services["rabbitmq"]
    assert "tracefold-rabbitmq:/var/lib/rabbitmq" in services["rabbitmq"]["volumes"]
    assert services["rabbitmq"]["healthcheck"]["test"][0] == "CMD"
    assert "rabbitmq-diagnostics" in " ".join(services["rabbitmq"]["healthcheck"]["test"])
    assert services["rabbitmq"]["healthcheck"]["test"][1:4] == ["su", "-s", "/bin/sh"]
    assert "PATH=/opt/rabbitmq/sbin:/opt/erlang/bin" in services["rabbitmq"]["healthcheck"]["test"][-1]
    assert "rabbitmq" not in services["serve"]["depends_on"]
    assert services["workers"]["depends_on"]["rabbitmq"]["condition"] == "service_healthy"
    policy = services["rabbitmq-policy"]
    assert policy["command"] == ["tracefold", "news", "bus-policy", "apply"]
    assert policy["restart"] == "no"
    assert policy["depends_on"]["rabbitmq"]["condition"] == "service_healthy"
    assert services["migrate"]["depends_on"]["rabbitmq-policy"]["condition"] == "service_completed_successfully"
    assert services["workers"]["depends_on"]["rabbitmq-policy"]["condition"] == "service_completed_successfully"
    assert services["postgres"]["image"] == POSTGRES_IMAGE
    assert "build" not in services["postgres"]
    assert any("pg_stat_statements" in part for part in services["postgres"]["command"])
    assert all(
        retired not in str(services["postgres"]["command"])
        for retired in ("powa", "pg_stat_kcache", "pg_qualstats", "pg_wait_sampling")
    )
    assert all("logging_collector" not in str(part) for part in services["postgres"]["command"])
    assert services["postgres"]["secrets"] == ["postgres_password", DATABASE_SECRET]
    assert "tracefold-postgres:/var/lib/postgresql" in services["postgres"]["volumes"]
    assert "pg_isready -U tracefold -d tracefold" in services["postgres"]["healthcheck"]["test"][1]
    assert "tracefold_app" not in services["postgres"]["healthcheck"]["test"][1]
    assert services["postgres"]["volumes"] == [
        "tracefold-postgres:/var/lib/postgresql",
        "./docker/postgres-init-single-login.sh:/docker-entrypoint-initdb.d/10-tracefold-single-login.sh:ro",
    ]
    credential = "${HOME}/.tracefold/postgres_database_password:/root/.tracefold/postgres_database_password:ro"
    shared_app_image = "${TRACEFOLD_APP_IMAGE:-${COMPOSE_PROJECT_NAME:-tracefold}-app:local}"
    shared_app_build = {
        "context": ".",
        "target": "app",
        "args": {"TRACEFOLD_BUILD_REVISION": "${TRACEFOLD_BUILD_REVISION:-}"},
        "secrets": ["github_token"],
    }
    for service_name in ("migrate", "serve", "workers", "nautilus"):
        assert credential in services[service_name]["volumes"]
        assert services[service_name]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for service_name in ("migrate", "serve", "workers", "rabbitmq-policy"):
        assert services[service_name]["image"] == shared_app_image
        assert services[service_name]["build"] == shared_app_build
    # The execution runtime is not on the shared anchor (#537 PR-2): its own image tag and its own
    # `runtime` build target are what let `make up` rebuild and recreate the application without
    # touching the one process that owns live exposure.
    assert services["nautilus"]["image"] == "${TRACEFOLD_RUNTIME_IMAGE:-tracefold-runtime:local}"
    assert services["nautilus"]["build"] == {
        "context": ".",
        "target": "runtime",
        "args": {"TRACEFOLD_BUILD_REVISION": "${TRACEFOLD_BUILD_REVISION:-}"},
        "secrets": ["github_token"],
    }
    # PostgreSQL and nothing else. A broker outage or a migration container that has not been rerun
    # must never be what keeps the exposure owner from coming back (#537 D4); the schema head is
    # asserted inside the process instead.
    assert services["nautilus"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert services["migrate"]["environment"] == {
        "TRACEFOLD_IMAGE_DIGEST": "${TRACEFOLD_IMAGE_DIGEST:-}",
        "TRACEFOLD_NEWS_GENESIS_PREFLIGHT_JSON": "${TRACEFOLD_NEWS_GENESIS_PREFLIGHT_JSON:-}",
    }
    for service_name in ("serve", "workers", "nautilus"):
        assert "TRACEFOLD_NEWS_GENESIS_PREFLIGHT_JSON" not in services[service_name]["environment"]
        assert "rsshub" not in services[service_name]["depends_on"]
    for service_name in ("serve", "workers"):
        assert services[service_name]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["migrate"]["command"] == ["tracefold", "db", "migrate"]
    assert services["serve"]["command"] == ["tracefold", "serve"]
    assert services["workers"]["command"] == ["tracefold", "workers"]
    assert services["nautilus"]["command"] == ["tracefold", "nautilus", "run"]
    assert services["nautilus"]["profiles"] == ["execution"]
    assert services["serve"]["ports"] == ["${TRACEFOLD_API_HOST:-127.0.0.1}:${TRACEFOLD_API_PORT:-8765}:8765"]
    assert services["serve"]["healthcheck"]["test"][2] == "-c"
    assert "/healthz" in services["serve"]["healthcheck"]["test"][3]
    assert services["workers"]["ports"] == ["${TRACEFOLD_WORKERS_HOST:-127.0.0.1}:${TRACEFOLD_WORKERS_PORT:-8766}:8766"]
    assert services["nautilus"]["ports"] == [
        "${TRACEFOLD_NAUTILUS_HOST:-127.0.0.1}:${TRACEFOLD_NAUTILUS_PORT:-8767}:8767"
    ]
    assert "http://127.0.0.1:8767/readyz" in services["nautilus"]["healthcheck"]["test"][3]
    assert set(compose["secrets"]) == {"postgres_password", DATABASE_SECRET, "github_token"}
    assert compose["secrets"][DATABASE_SECRET]["file"] == "${HOME}/.tracefold/postgres_database_password"


def _run_init_script(tmp_path: Path, password: str) -> subprocess.CompletedProcess[str]:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / DATABASE_SECRET).write_text(password + "\n", encoding="utf-8")
    capture_path = tmp_path / "captured.sql"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_psql = bin_dir / "psql"
    fake_psql.write_text('#!/bin/sh\ncat > "$TRACEFOLD_CAPTURE_SQL"\n', encoding="utf-8")
    fake_psql.chmod(0o700)
    return subprocess.run(
        ["sh", "docker/postgres-init-single-login.sh", str(secrets_dir)],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "POSTGRES_DB": "tracefold",
            "POSTGRES_USER": "tracefold_app",
            "TRACEFOLD_CAPTURE_SQL": str(capture_path),
        },
        text=True,
    )


def test_postgres_init_script_provisions_one_application_login(tmp_path: Path) -> None:
    password = "A" * 43
    result = _run_init_script(tmp_path, password)

    assert result.returncode == 0, result.stderr
    assert password not in result.stdout and password not in result.stderr
    sql = (tmp_path / "captured.sql").read_text(encoding="utf-8")
    assert sql.lstrip().startswith("BEGIN;")
    assert sql.count("CREATE ROLE") == 1
    assert "CREATE ROLE tracefold" in sql
    assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in sql
    assert "ALTER SCHEMA public OWNER TO tracefold" in sql
    assert "ALTER ROLE tracefold_app NOLOGIN" in sql
    assert "tracefold_owner" not in sql
    assert "default_transaction_read_only" not in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_postgres_init_script_rejects_invalid_password_without_echoing_it(tmp_path: Path) -> None:
    password = f"{'A' * 42}+"
    result = _run_init_script(tmp_path, password)

    assert result.returncode != 0
    assert f"charset is invalid: {DATABASE_SECRET}" in result.stderr
    assert password not in result.stdout and password not in result.stderr


def test_role_capability_matrix_resources_are_deleted() -> None:
    assert not Path("tracefold/platform/postgres/alembic/runtime_roles.sql").exists()
    assert not Path("tracefold/platform/postgres/runtime_roles.py").exists()


def test_compose_preserves_non_postgres_secret_isolation() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]
    serve_volumes = services["serve"].get("volumes", [])
    worker_volumes = services["workers"].get("volumes", [])
    nautilus_volumes = services["nautilus"].get("volumes", [])

    assert "${HOME}/.tracefold/telegram_bot_token:/root/.tracefold/telegram_bot_token:ro" in worker_volumes
    # #528 deleted the Telegram control ingress, so nothing reads a webhook secret any more.
    assert all("telegram_webhook_secret" not in volume for volume in worker_volumes)
    assert all("binance_usdm_api_" not in volume for volume in worker_volumes)
    assert all("hyperliquid_private_key" not in volume for volume in worker_volumes)
    assert all("telegram_bot_token" not in volume for volume in serve_volumes)
    assert all("telegram_webhook_secret" not in volume for volume in serve_volumes)
    # #520 PR-B: Serve authenticates the one command write with the bootstrap token it already
    # holds, so it mounts no secret file at all.
    assert all("trading_console_write_token" not in volume for volume in serve_volumes)
    assert any("binance_usdm_api_key" in volume for volume in nautilus_volumes)
    assert any("binance_usdm_api_secret" in volume for volume in nautilus_volumes)
    for service_name, service in services.items():
        if service_name != "nautilus":
            assert not any(
                "binance_usdm_api_key" in volume
                or "binance_usdm_api_secret" in volume
                or "hyperliquid_private_key" in volume
                for volume in service.get("volumes", [])
            )
    assert all("/root/.tracefold/data" not in volume for volume in [*serve_volumes, *worker_volumes, *nautilus_volumes])
    assert "tracefold-postgres" in compose["volumes"]
