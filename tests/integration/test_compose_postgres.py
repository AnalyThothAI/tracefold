from __future__ import annotations

from pathlib import Path

import yaml

from tests.postgres_test_utils import connect_postgres_test

POSTGRES_IMAGE = "postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296"
RSSHUB_IMAGE = (
    "diygod/rsshub:5527d18de9605e5df9112f40904596e6ae5b971e@"
    "sha256:5730db6dead9c8e6610c92fc71900fa2e7735346d80d6be79ead3f2419d88282"
)


def test_compose_separates_serve_workers_and_explicit_cutover() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {
        "cutover",
        "migrate",
        "postgres",
        "rsshub",
        "serve",
        "workers",
    }
    assert services["postgres"]["image"] == POSTGRES_IMAGE
    assert "build" not in services["postgres"]
    assert any("pg_stat_statements" in part for part in services["postgres"]["command"])
    assert all(
        retired not in str(services["postgres"]["command"])
        for retired in ("powa", "pg_stat_kcache", "pg_qualstats", "pg_wait_sampling")
    )
    assert all("logging_collector" not in str(part) for part in services["postgres"]["command"])
    assert "tracefold-postgres:/var/lib/postgresql" in services["postgres"]["volumes"]
    assert len(services["postgres"]["volumes"]) == 1
    assert services["postgres"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert "pg_isready" in services["postgres"]["healthcheck"]["test"][1]

    for role in ("serve", "workers"):
        depends = services[role]["depends_on"]
        assert depends["postgres"]["condition"] == "service_healthy"
        assert depends["migrate"]["condition"] == "service_completed_successfully"
        assert "rsshub" not in depends
    assert services["serve"]["command"] == ["tracefold", "serve"]
    assert services["workers"]["command"] == ["tracefold", "workers"]
    assert services["serve"]["healthcheck"]["test"][2] == "-c"
    assert "/healthz" in services["serve"]["healthcheck"]["test"][3]
    assert services["workers"]["ports"] == ["127.0.0.1:8766:8766"]
    assert services["workers"]["build"]["args"] == {
        "TRACEFOLD_BUILD_REVISION": "${TRACEFOLD_BUILD_REVISION:-}",
    }
    assert services["cutover"]["profiles"] == ["maintenance"]
    assert services["cutover"]["command"] == [
        "tracefold",
        "db",
        "hard-cut",
    ]

    rsshub = services["rsshub"]
    assert rsshub["image"] == RSSHUB_IMAGE
    assert rsshub["environment"] == {
        "NODE_ENV": "production",
        "CACHE_TYPE": "memory",
    }
    assert rsshub["env_file"] == [
        {
            "path": "${HOME}/.tracefold/rsshub.env",
            "required": False,
        }
    ]
    assert "ports" not in rsshub


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
        "regen_score_versions.py",
        "regen_ws_protocol.py",
    }
    assert not any(path.name == "__pycache__" for path in Path("scripts").iterdir())


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
