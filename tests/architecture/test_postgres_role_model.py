from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _tracked(*paths: str) -> list[str]:
    """Which of these paths git still tracks. `Path.exists()` would also see build residue."""

    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", *paths],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [name for name in listed.split("\0") if name]


def test_fresh_init_creates_one_application_login_and_disables_bootstrap_login() -> None:
    source = _read("docker/postgres-init-single-login.sh")

    assert re.findall(r"\bCREATE ROLE (tracefold[a-z_]*)\b", source) == ["tracefold"]
    assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in source
    assert "ALTER SCHEMA public OWNER TO tracefold" in source
    assert "ALTER ROLE tracefold_app NOLOGIN" in source
    assert "SET default_transaction_read_only" not in source


def test_every_postgres_consumer_mounts_the_same_application_credential() -> None:
    compose = yaml.safe_load(_read("compose.yaml"))
    services = compose["services"]
    credential = "${HOME}/.tracefold/postgres_database_password:/root/.tracefold/postgres_database_password:ro"

    assert compose["secrets"]["postgres_database_password"]["file"] == ("${HOME}/.tracefold/postgres_database_password")
    assert services["postgres"]["secrets"] == ["postgres_password", "postgres_database_password"]
    for service_name in ("migrate", "serve", "workers", "nautilus"):
        assert credential in services[service_name]["volumes"]


def test_role_specific_authority_and_offline_role_manager_are_absent() -> None:
    current_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (ROOT / "tracefold", ROOT / "docker", ROOT / "scripts")
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".sql", ".sh"}
    )

    assert (
        _tracked(
            "tracefold/platform/postgres/alembic/runtime_roles.sql",
            "tracefold/platform/postgres/runtime_roles.py",
        )
        == []
    )
    assert not re.search(r"\b(?:CREATE|ALTER|SET) ROLE tracefold_(?:owner|serve|workers|nautilus)\b", current_sources)
    assert not re.search(r"postgres_(?:serve|workers|migrate|nautilus)_password", current_sources)
    assert "postgres --single" not in _read("Makefile")
