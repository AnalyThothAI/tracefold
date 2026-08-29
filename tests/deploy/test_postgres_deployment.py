from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.deploy

POSTGRES_IMAGE = "postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296"


def test_compose_separates_migration_serve_and_workers() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {
        "migrate",
        "nautilus",
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
    assert (
        "./docker/postgres-provision-nautilus-role.sh:/usr/local/bin/tracefold-provision-nautilus-role:ro"
    ) in services["postgres"]["volumes"]
    assert len(services["postgres"]["volumes"]) == 3
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
        "postgres_nautilus_password",
    ]

    shared_app_image = "${TRACEFOLD_APP_IMAGE:-${COMPOSE_PROJECT_NAME:-tracefold}-app:local}"
    shared_app_build = {
        "context": ".",
        "args": {
            "TRACEFOLD_BUILD_REVISION": "${TRACEFOLD_BUILD_REVISION:-}",
        },
        "secrets": ["github_token"],
    }
    for role in ("migrate", "serve", "workers", "nautilus"):
        assert services[role]["image"] == shared_app_image
        assert services[role]["build"] == shared_app_build

    for role in ("serve", "workers", "nautilus"):
        depends = services[role]["depends_on"]
        assert depends["postgres"]["condition"] == "service_healthy"
        assert depends["migrate"]["condition"] == "service_completed_successfully"
        assert "rsshub" not in depends
    assert "rabbitmq" not in services["nautilus"]["depends_on"]
    assert services["serve"]["command"] == ["tracefold", "serve"]
    assert services["workers"]["command"] == ["tracefold", "workers"]
    assert services["nautilus"]["command"] == ["tracefold", "nautilus", "run"]
    assert services["serve"]["ports"] == ["${TRACEFOLD_API_HOST:-127.0.0.1}:${TRACEFOLD_API_PORT:-8765}:8765"]
    assert services["serve"]["healthcheck"]["test"][2] == "-c"
    assert "/healthz" in services["serve"]["healthcheck"]["test"][3]
    assert services["workers"]["ports"] == ["${TRACEFOLD_WORKERS_HOST:-127.0.0.1}:${TRACEFOLD_WORKERS_PORT:-8766}:8766"]
    assert services["nautilus"]["ports"] == [
        "${TRACEFOLD_NAUTILUS_HOST:-127.0.0.1}:${TRACEFOLD_NAUTILUS_PORT:-8767}:8767"
    ]
    assert "http://127.0.0.1:8767/readyz" in services["nautilus"]["healthcheck"]["test"][3]


def test_compose_declares_host_role_password_files_as_postgres_init_secrets() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())

    assert compose["secrets"]["postgres_serve_password"]["file"] == "${HOME}/.tracefold/postgres_serve_password"
    assert compose["secrets"]["postgres_workers_password"]["file"] == "${HOME}/.tracefold/postgres_workers_password"
    assert compose["secrets"]["postgres_migrate_password"]["file"] == "${HOME}/.tracefold/postgres_migrate_password"
    assert compose["secrets"]["postgres_nautilus_password"]["file"] == "${HOME}/.tracefold/postgres_nautilus_password"
    assert "postgres_review_password" not in compose["secrets"]


def test_postgres_init_script_provisions_distinct_runtime_roles_without_outputting_passwords(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    passwords = {
        "postgres_serve_password": "A" * 43,
        "postgres_workers_password": "B" * 43,
        "postgres_migrate_password": "C" * 43,
        "postgres_nautilus_password": "D" * 43,
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
    assert "CREATE ROLE tracefold_nautilus" in sql
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
        "postgres_nautilus_password": "D" * 43,
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
        "tracefold_nautilus",
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


def test_offline_nautilus_role_provisioning_is_narrow_and_keeps_the_password_out_of_output(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    password = "N" * 43
    password_file = secrets_dir / "postgres_nautilus_password"
    password_file.write_text(password + "\n", encoding="utf-8")
    password_file.chmod(0o600)
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    capture_args_path = tmp_path / "captured.args"
    capture_path = tmp_path / "captured.sql"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_postgres = bin_dir / "postgres"
    fake_postgres.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$TRACEFOLD_CAPTURE_ARGS"\ncat > "$TRACEFOLD_CAPTURE_SQL"\n',
        encoding="utf-8",
    )
    fake_postgres.chmod(0o700)

    result = subprocess.run(
        ["sh", "docker/postgres-provision-nautilus-role.sh", str(secrets_dir)],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PGDATA": str(pgdata),
            "POSTGRES_DB": "tracefold",
            "TRACEFOLD_CAPTURE_ARGS": str(capture_args_path),
            "TRACEFOLD_CAPTURE_SQL": str(capture_path),
        },
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert password not in result.stdout
    assert password not in result.stderr
    assert capture_args_path.read_text(encoding="utf-8").splitlines() == [
        "--single",
        "-j",
        "-D",
        str(pgdata),
        "tracefold",
    ]
    sql = capture_path.read_text(encoding="utf-8")
    assert "CREATE ROLE tracefold_nautilus LOGIN" in sql
    assert "ALTER ROLE tracefold_nautilus" in sql
    assert "NOCREATEROLE" in sql
    assert "GRANT tracefold_owner" not in sql


def test_offline_nautilus_role_provisioning_refuses_a_running_cluster(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "postgres_nautilus_password").write_text("N" * 43 + "\n", encoding="utf-8")
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text("123\n", encoding="utf-8")

    result = subprocess.run(
        ["sh", "docker/postgres-provision-nautilus-role.sh", str(secrets_dir)],
        capture_output=True,
        check=False,
        env={**os.environ, "PGDATA": str(pgdata), "POSTGRES_DB": "tracefold"},
        text=True,
    )

    assert result.returncode != 0
    assert "offline" in result.stderr.lower()


def test_compose_mounts_only_role_credentials_into_steady_runtimes() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    serve_volumes = compose["services"]["serve"].get("volumes", [])
    worker_volumes = compose["services"]["workers"].get("volumes", [])
    nautilus_volumes = compose["services"]["nautilus"].get("volumes", [])

    assert any("postgres_serve_password" in volume for volume in serve_volumes)
    assert not any(
        "postgres_workers_password" in volume or "postgres_migrate_password" in volume for volume in serve_volumes
    )
    assert any("postgres_workers_password" in volume for volume in worker_volumes)
    assert "${HOME}/.tracefold/telegram_bot_token:/root/.tracefold/telegram_bot_token:ro" in worker_volumes
    assert "${HOME}/.tracefold/binance_usdm_api_key:/root/.tracefold/binance_usdm_api_key:ro" in worker_volumes
    assert "${HOME}/.tracefold/binance_usdm_api_secret:/root/.tracefold/binance_usdm_api_secret:ro" in worker_volumes
    assert "${HOME}/.tracefold/hyperliquid_private_key:/root/.tracefold/hyperliquid_private_key:ro" in worker_volumes
    assert all("telegram_bot_token" not in volume for volume in serve_volumes)
    assert not any(
        "postgres_serve_password" in volume or "postgres_migrate_password" in volume for volume in worker_volumes
    )
    assert any("postgres_nautilus_password" in volume for volume in nautilus_volumes)
    assert any("binance_usdm_api_key" in volume for volume in nautilus_volumes)
    assert any("binance_usdm_api_secret" in volume for volume in nautilus_volumes)
    assert not any(
        role_password in volume
        for volume in nautilus_volumes
        for role_password in ("postgres_serve_password", "postgres_workers_password", "postgres_migrate_password")
    )
    for service_name, service in compose["services"].items():
        if service_name not in {"workers", "nautilus"}:
            assert not any(
                "binance_usdm_api_key" in volume
                or "binance_usdm_api_secret" in volume
                or "hyperliquid_private_key" in volume
                for volume in service.get("volumes", [])
            )
    assert all("/root/.tracefold/data" not in volume for volume in [*serve_volumes, *worker_volumes, *nautilus_volumes])
    assert "tracefold-postgres" in compose["volumes"]
