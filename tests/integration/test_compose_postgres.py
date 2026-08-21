from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from tests.postgres_test_utils import connect_postgres_test

POSTGRES_IMAGE = "postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296"


def test_compose_separates_migration_serve_and_workers() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {
        "migrate",
        "postgres",
        "rabbitmq",
        "serve",
        "workers",
    }
    assert services["rabbitmq"]["image"].startswith("rabbitmq:")
    assert "build" not in services["rabbitmq"]
    assert "tracefold-rabbitmq:/var/lib/rabbitmq" in services["rabbitmq"]["volumes"]
    assert services["rabbitmq"]["healthcheck"]["test"][0] == "CMD"
    assert "rabbitmq-diagnostics" in services["rabbitmq"]["healthcheck"]["test"]
    assert "rabbitmq" not in services["serve"]["depends_on"]
    assert services["workers"]["depends_on"]["rabbitmq"]["condition"] == "service_healthy"
    assert services["postgres"]["image"] == POSTGRES_IMAGE
    assert "build" not in services["postgres"]
    assert any("pg_stat_statements" in part for part in services["postgres"]["command"])
    assert all(
        retired not in str(services["postgres"]["command"])
        for retired in ("powa", "pg_stat_kcache", "pg_qualstats", "pg_wait_sampling")
    )
    assert all("logging_collector" not in str(part) for part in services["postgres"]["command"])
    assert "tracefold-postgres:/var/lib/postgresql" in services["postgres"]["volumes"]
    assert (
        "./docker/postgres-init-runtime-roles.sh:/docker-entrypoint-initdb.d/10-tracefold-runtime-roles.sh:ro"
    ) in services["postgres"]["volumes"]
    assert len(services["postgres"]["volumes"]) == 2
    assert services["postgres"]["healthcheck"]["test"][0] == "CMD-SHELL"
    postgres_healthcheck = services["postgres"]["healthcheck"]["test"][1]
    assert "pg_isready" in postgres_healthcheck
    assert "tracefold_workers" in postgres_healthcheck
    assert "tracefold_migrate" not in postgres_healthcheck
    assert "tracefold_app" not in postgres_healthcheck
    assert services["postgres"]["secrets"] == [
        "postgres_password",
        "postgres_serve_password",
        "postgres_workers_password",
        "postgres_migrate_password",
    ]

    shared_app_image = "${TRACEFOLD_APP_IMAGE:-${COMPOSE_PROJECT_NAME:-tracefold}-app:local}"
    shared_app_build = {
        "context": ".",
        "args": {
            "TRACEFOLD_BUILD_REVISION": "${TRACEFOLD_BUILD_REVISION:-}",
        },
        "secrets": ["github_token"],
    }
    for role in ("migrate", "serve", "workers"):
        assert services[role]["image"] == shared_app_image
        assert services[role]["build"] == shared_app_build

    for role in ("serve", "workers"):
        depends = services[role]["depends_on"]
        assert depends["postgres"]["condition"] == "service_healthy"
        assert depends["migrate"]["condition"] == "service_completed_successfully"
        assert "rsshub" not in depends
    assert services["serve"]["command"] == ["tracefold", "serve"]
    assert services["workers"]["command"] == ["tracefold", "workers"]
    assert services["serve"]["ports"] == ["${TRACEFOLD_API_HOST:-127.0.0.1}:${TRACEFOLD_API_PORT:-8765}:8765"]
    assert services["serve"]["healthcheck"]["test"][2] == "-c"
    assert "/healthz" in services["serve"]["healthcheck"]["test"][3]
    assert services["workers"]["ports"] == ["${TRACEFOLD_WORKERS_HOST:-127.0.0.1}:${TRACEFOLD_WORKERS_PORT:-8766}:8766"]


def test_compose_declares_host_role_password_files_as_postgres_init_secrets() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())

    assert compose["secrets"]["postgres_serve_password"]["file"] == "${HOME}/.tracefold/postgres_serve_password"
    assert compose["secrets"]["postgres_workers_password"]["file"] == "${HOME}/.tracefold/postgres_workers_password"
    assert compose["secrets"]["postgres_migrate_password"]["file"] == "${HOME}/.tracefold/postgres_migrate_password"
    assert "postgres_review_password" not in compose["secrets"]


def test_postgres_init_script_provisions_distinct_runtime_roles_without_outputting_passwords(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    passwords = {
        "postgres_serve_password": "A" * 43,
        "postgres_workers_password": "B" * 43,
        "postgres_migrate_password": "C" * 43,
    }
    for name, password in passwords.items():
        path = secrets_dir / name
        path.write_text(password + "\n", encoding="utf-8")
        path.chmod(0o600)

    capture_path = tmp_path / "captured.sql"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_psql = bin_dir / "psql"
    fake_psql.write_text('#!/bin/sh\ncat > "$TRACEFOLD_CAPTURE_SQL"\n', encoding="utf-8")
    fake_psql.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_DB": "tracefold",
        "POSTGRES_USER": "tracefold_app",
        "TRACEFOLD_CAPTURE_SQL": str(capture_path),
    }

    result = subprocess.run(
        ["sh", "docker/postgres-init-runtime-roles.sh", str(secrets_dir)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert all(password not in result.stdout and password not in result.stderr for password in passwords.values())
    sql = capture_path.read_text(encoding="utf-8")
    assert sql.lstrip().startswith("BEGIN;")
    assert sql.index("CREATE EXTENSION IF NOT EXISTS pg_stat_statements") < sql.index("CREATE ROLE tracefold_owner")
    assert "CREATE ROLE tracefold_owner" in sql
    assert "CREATE ROLE tracefold_serve" in sql
    assert "CREATE ROLE tracefold_workers" in sql
    assert "tracefold_review" not in sql
    assert "CREATE ROLE tracefold_migrate" in sql
    assert "GRANT tracefold_owner TO tracefold_migrate WITH ADMIN FALSE" in sql
    assert "GRANT tracefold_owner TO tracefold_migrate WITH INHERIT FALSE" in sql
    assert "GRANT tracefold_owner TO tracefold_migrate WITH SET TRUE" in sql
    assert "ALTER SCHEMA public OWNER TO tracefold_owner" in sql
    assert "ALTER VIEW public.pg_stat_statements OWNER TO tracefold_owner" in sql
    assert "ALTER VIEW public.pg_stat_statements_info OWNER TO tracefold_owner" in sql
    assert sql.index("ALTER ROLE tracefold_app NOLOGIN;") < sql.index("COMMIT;")
    assert sql.rstrip().endswith("COMMIT;")
    assert "DROP DATABASE" not in sql
    assert "DROP SCHEMA" not in sql


def test_postgres_init_script_rejects_invalid_password_charset_without_echoing_value(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    invalid_password = f"{'A' * 42}+"
    passwords = {
        "postgres_serve_password": invalid_password,
        "postgres_workers_password": "B" * 43,
        "postgres_migrate_password": "C" * 43,
    }
    for name, password in passwords.items():
        path = secrets_dir / name
        path.write_text(password + "\n", encoding="utf-8")
        path.chmod(0o600)

    result = subprocess.run(
        ["sh", "docker/postgres-init-runtime-roles.sh", str(secrets_dir)],
        capture_output=True,
        check=False,
        env={**os.environ, "POSTGRES_DB": "tracefold", "POSTGRES_USER": "tracefold_app"},
        text=True,
    )

    assert result.returncode != 0
    assert "charset is invalid: postgres_serve_password" in result.stderr
    assert invalid_password not in result.stdout
    assert invalid_password not in result.stderr


def test_runtime_role_migration_validates_owner_bootstrap_and_normalizes_legacy_membership() -> None:
    migration = Path("src/tracefold/platform/postgres/alembic/runtime_roles.sql").read_text(encoding="utf-8")

    assert "IF current_user <> 'tracefold_owner' THEN" in migration
    assert "tracefold_runtime_role_bootstrap_superuser_required" in migration
    for contract_part in (
        "tracefold_owner",
        "tracefold_serve",
        "tracefold_workers",
        "tracefold_migrate",
        "tracefold_migrate_owner_membership",
        "public_schema_owner",
        "bootstrap_login_disabled",
    ):
        assert f"tracefold_runtime_role_contract_invalid:{contract_part}" in migration
    assert "GRANT tracefold_owner TO tracefold_migrate WITH ADMIN FALSE" in migration
    assert "GRANT tracefold_owner TO tracefold_migrate WITH INHERIT FALSE" in migration
    assert "GRANT tracefold_owner TO tracefold_migrate WITH SET TRUE" in migration
    assert "AND NOT rolcreaterole" in migration
    assert "AND NOT rolsuper" in migration


def test_compose_mounts_only_role_credentials_into_steady_runtimes() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    serve_volumes = compose["services"]["serve"].get("volumes", [])
    worker_volumes = compose["services"]["workers"].get("volumes", [])

    assert any("postgres_serve_password" in volume for volume in serve_volumes)
    assert not any(
        "postgres_workers_password" in volume or "postgres_migrate_password" in volume for volume in serve_volumes
    )
    assert any("postgres_workers_password" in volume for volume in worker_volumes)
    assert not any(
        "postgres_serve_password" in volume or "postgres_migrate_password" in volume for volume in worker_volumes
    )
    assert all("/root/.tracefold/data" not in volume for volume in [*serve_volumes, *worker_volumes])
    assert "tracefold-postgres" in compose["volumes"]


def test_retired_ops_tree_and_orphan_scripts_are_absent() -> None:
    assert not Path("ops").exists()
    scripts = {path.name for path in Path("scripts").iterdir() if path.is_file()}
    assert scripts == {
        "regen_cli_help.py",
        "regen_db_schema.py",
        "regen_openapi.py",
        "with_deployment_lock.py",
    }
    assert not Path("docker/postgres-provision-review-role.sh").exists()


def test_postgres_keeps_supported_extensions_and_removes_retired_ones() -> None:
    conn = connect_postgres_test(read_only=True)
    try:
        installed = {row["extname"] for row in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        retired_setting_files = conn.execute(
            """
            SELECT name
              FROM pg_file_settings
             WHERE name IN ('powa.coalesce', 'powa.frequency')
            """
        ).fetchall()
    finally:
        conn.close()

    assert {"pg_stat_statements", "pg_trgm"} <= installed
    assert {
        "powa",
        "pg_stat_kcache",
        "pg_qualstats",
        "pg_wait_sampling",
    }.isdisjoint(installed)
    assert retired_setting_files == []
