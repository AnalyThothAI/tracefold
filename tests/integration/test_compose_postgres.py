from __future__ import annotations

from pathlib import Path

import yaml

from tests.postgres_test_utils import connect_postgres_test

POSTGRES_IMAGE = "postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296"
RSSHUB_IMAGE = (
    "diygod/rsshub:5527d18de9605e5df9112f40904596e6ae5b971e@"
    "sha256:5730db6dead9c8e6610c92fc71900fa2e7735346d80d6be79ead3f2419d88282"
)


def test_compose_runs_postgres_and_migration_before_app() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {"app", "migrate", "postgres", "rsshub"}
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

    app_depends = services["app"]["depends_on"]
    assert app_depends["postgres"]["condition"] == "service_healthy"
    assert app_depends["migrate"]["condition"] == "service_completed_successfully"
    assert "rsshub" not in app_depends
    assert services["app"]["healthcheck"]["test"][2] == "-c"
    assert "/healthz" in services["app"]["healthcheck"]["test"][3]

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


def test_compose_no_longer_mounts_sqlite_data_volume_into_app() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    app_volumes = compose["services"]["app"].get("volumes", [])

    assert all("/root/.tracefold/data" not in volume for volume in app_volumes)
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
    finally:
        conn.close()

    assert {"pg_stat_statements", "pg_trgm"} <= installed
    assert {
        "powa",
        "pg_stat_kcache",
        "pg_qualstats",
        "pg_wait_sampling",
    }.isdisjoint(installed)
