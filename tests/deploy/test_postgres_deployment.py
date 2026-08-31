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
    assert services["postgres"]["image"] == POSTGRES_IMAGE
    assert services["postgres"]["secrets"] == ["postgres_password", DATABASE_SECRET]
    assert "pg_isready -U tracefold -d tracefold" in services["postgres"]["healthcheck"]["test"][1]
    assert services["postgres"]["volumes"] == [
        "tracefold-postgres:/var/lib/postgresql",
        "./docker/postgres-init-runtime-roles.sh:/docker-entrypoint-initdb.d/10-tracefold-runtime-roles.sh:ro",
    ]
    credential = "${HOME}/.tracefold/postgres_database_password:/root/.tracefold/postgres_database_password:ro"
    for service_name in ("migrate", "serve", "workers", "nautilus"):
        assert credential in services[service_name]["volumes"]
        assert services[service_name]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["migrate"]["command"] == ["tracefold", "db", "migrate"]
    assert services["serve"]["command"] == ["tracefold", "serve"]
    assert services["workers"]["command"] == ["tracefold", "workers"]
    assert services["nautilus"]["command"] == ["tracefold", "nautilus", "run"]
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
        ["sh", "docker/postgres-init-runtime-roles.sh", str(secrets_dir)],
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
