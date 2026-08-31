from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CURRENT_LOGIN_ROLES = {
    "tracefold_owner",
    "tracefold_serve",
    "tracefold_workers",
    "tracefold_nautilus",
}
CREATE_ROLE = re.compile(r"\bCREATE ROLE (tracefold_[a-z_]+)\b")


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_current_authorities_create_only_the_current_role_set() -> None:
    fresh_init = _read("docker/postgres-init-runtime-roles.sh")
    migration_authority = _read("tracefold/platform/postgres/alembic/runtime_roles.sql")

    assert set(CREATE_ROLE.findall(fresh_init)) == CURRENT_LOGIN_ROLES
    assert set(CREATE_ROLE.findall(migration_authority)) == {
        "tracefold_app",
        *CURRENT_LOGIN_ROLES,
    }


def test_migration_authorities_do_not_switch_or_delegate_the_owner_role() -> None:
    authorities = (
        _read("docker/postgres-init-runtime-roles.sh"),
        _read("tracefold/platform/postgres/alembic/runtime_roles.sql"),
        _read("tracefold/platform/postgres/alembic/env.py"),
    )

    assert all(re.search(r"\bSET\s+ROLE\b", source, flags=re.IGNORECASE) is None for source in authorities)
    assert all(
        re.search(r"\bGRANT\s+tracefold_owner\s+TO\b", source, flags=re.IGNORECASE) is None for source in authorities
    )


def test_owner_credential_is_migration_only_and_no_offline_role_manager_exists() -> None:
    compose = yaml.safe_load(_read("compose.yaml"))
    services = compose["services"]
    owner_credential = "postgres_migrate_password"

    assert any(owner_credential in volume for volume in services["migrate"].get("volumes", []))
    for service_name in ("serve", "workers", "nautilus"):
        assert not any(owner_credential in volume for volume in services[service_name].get("volumes", []))

    assert services["postgres"]["volumes"] == [
        "tracefold-postgres:/var/lib/postgresql",
        "./docker/postgres-init-runtime-roles.sh:/docker-entrypoint-initdb.d/10-tracefold-runtime-roles.sh:ro",
    ]
    assert "--user postgres" not in _read("Makefile")
    assert [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "docker").glob("*.sh")
        if re.search(r"\bpostgres\s+--single\b", path.read_text(encoding="utf-8"))
    ] == []
