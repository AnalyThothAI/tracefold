from __future__ import annotations

import subprocess
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from alembic import command
from psycopg import OperationalError

from scripts.require_db_role_hard_cut_preflight import record_preflight
from tests.postgres_test_utils import news_genesis_test_evidence
from tests.tracefold_postgres_container import DEFAULT_TEST_POSTGRES_IMAGE
from tracefold.platform.postgres.client import connect_postgres
from tracefold.platform.postgres.migrations import alembic_config, latest_migration_version
from tracefold.platform.postgres.runtime_roles import runtime_role_contract

pytestmark = pytest.mark.integration

_DATABASE = "tracefold"
_ADMIN_PASSWORD = "postgres-test-admin-password-0001"
_OWNER_PASSWORD = "owner-test-migration-password-0001"
_OLD_MIGRATE_PASSWORD = "retired-test-migration-password-01"
_SCRIPT = (Path(__file__).parents[2] / "docker/postgres-hard-cut-owner-role.sh").resolve()
_RUNTIME_ROLES = ("tracefold_app", "tracefold_owner", "tracefold_serve", "tracefold_workers", "tracefold_nautilus")
_DEFAULT_ACL_ENTRIES = [
    ("S", "tracefold_owner", "tracefold_workers", "SELECT", False),
    ("S", "tracefold_owner", "tracefold_workers", "USAGE", False),
    ("r", "tracefold_owner", "tracefold_serve", "SELECT", False),
    ("r", "tracefold_owner", "tracefold_workers", "DELETE", False),
    ("r", "tracefold_owner", "tracefold_workers", "INSERT", False),
    ("r", "tracefold_owner", "tracefold_workers", "SELECT", False),
    ("r", "tracefold_owner", "tracefold_workers", "UPDATE", False),
]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        check=check,
        text=True,
        timeout=240,
    )


def _wait_ready(container: str) -> None:
    for _ in range(120):
        process = _docker("exec", container, "cat", "/proc/1/comm", check=False)
        if process.stdout.strip() != "postgres":
            time.sleep(0.25)
            continue
        result = _docker("exec", container, "pg_isready", "-U", "postgres", "-d", _DATABASE, check=False)
        if result.returncode == 0:
            return
        time.sleep(0.25)
    logs = _docker("logs", container, check=False)
    pytest.fail(f"isolated PostgreSQL 18 did not become ready: {logs.stderr[-2_000:]}", pytrace=False)


def _stop(container: str) -> None:
    _docker("kill", "--signal", "SIGINT", container)
    assert _docker("wait", container).stdout.strip() == "0"


def _host_port(container: str) -> int:
    output = _docker("port", container, "5432/tcp").stdout.strip().splitlines()[0]
    return int(output.rsplit(":", 1)[1])


def _dsn(port: int, role: str, password: str, database: str = _DATABASE) -> str:
    return f"postgresql://{quote(role, safe='')}:{quote(password, safe='')}@127.0.0.1:{port}/{quote(database, safe='')}"


def _upgrade(dsn: str, revision: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = dsn
    with news_genesis_test_evidence():
        command.upgrade(config, revision)


def _hard_cut(volume: str, secrets: Path) -> subprocess.CompletedProcess[str]:
    return _docker(
        "run",
        "--rm",
        "--user",
        "postgres",
        "--env",
        f"POSTGRES_DB={_DATABASE}",
        "--volume",
        f"{volume}:/var/lib/postgresql",
        "--volume",
        f"{_SCRIPT}:/usr/local/bin/tracefold-hard-cut-owner-role:ro",
        "--volume",
        f"{secrets.resolve()}:/run/secrets:ro",
        "--entrypoint",
        "sh",
        DEFAULT_TEST_POSTGRES_IMAGE,
        "/usr/local/bin/tracefold-hard-cut-owner-role",
        "/run/secrets",
        check=False,
    )


def _acl_fingerprint(dsn: str) -> str:
    with connect_postgres(dsn) as conn:
        row = conn.execute(
            """
            SELECT md5(string_agg(entry, E'\n' ORDER BY entry)) AS fingerprint
            FROM (
              SELECT 'schema:' || namespace.nspname || ':' || COALESCE(namespace.nspacl::text, '') AS entry
                FROM pg_namespace namespace WHERE namespace.nspname = 'public'
              UNION ALL
              SELECT 'relation:' || object.relname || ':' || COALESCE(object.relacl::text, '')
                FROM pg_class object
                JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
               WHERE namespace.nspname = 'public'
              UNION ALL
              SELECT 'column:' || object.relname || '.' || attribute.attname || ':'
                     || COALESCE(attribute.attacl::text, '')
                FROM pg_attribute attribute
                JOIN pg_class object ON object.oid = attribute.attrelid
                JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
               WHERE namespace.nspname = 'public' AND attribute.attnum > 0 AND NOT attribute.attisdropped
              UNION ALL
              SELECT 'function:' || object.oid::regprocedure::text || ':' || COALESCE(object.proacl::text, '')
                FROM pg_proc object
                JOIN pg_namespace namespace ON namespace.oid = object.pronamespace
               WHERE namespace.nspname = 'public'
              UNION ALL
              SELECT 'default:' || defaults.defaclobjtype::text || ':' || COALESCE(defaults.defaclacl::text, '')
                FROM pg_default_acl defaults
                JOIN pg_roles owner ON owner.oid = defaults.defaclrole
               WHERE owner.rolname = 'tracefold_owner'
            ) contract
            """
        ).fetchone()
    return str(row["fingerprint"])


def _default_acl_entries(dsn: str) -> list[tuple[str, str, str, str, bool]]:
    with connect_postgres(dsn) as conn:
        rows = conn.execute(
            """
            SELECT defaults.defaclobjtype::text AS object_type,
                   grantor.rolname AS grantor_name,
                   COALESCE(grantee.rolname, 'PUBLIC') AS grantee_name,
                   privilege.privilege_type,
                   privilege.is_grantable
            FROM pg_default_acl defaults
            CROSS JOIN LATERAL aclexplode(defaults.defaclacl) privilege
            LEFT JOIN pg_roles grantor ON grantor.oid = privilege.grantor
            LEFT JOIN pg_roles grantee ON grantee.oid = privilege.grantee
            ORDER BY defaults.defaclobjtype::text COLLATE "C", grantor.rolname,
                     COALESCE(grantee.rolname, 'PUBLIC'), privilege.privilege_type,
                     privilege.is_grantable
            """
        ).fetchall()
    return [tuple(row.values()) for row in rows]  # type: ignore[misc]


def test_fresh_volume_uses_direct_owner_and_restarts_without_role_repair(tmp_path: Path) -> None:
    suffix = uuid4().hex[:12]
    container = f"tracefold-owner-fresh-{suffix}"
    volume = f"tracefold-owner-fresh-{suffix}"
    secrets = tmp_path / "fresh-secrets"
    secrets.mkdir()
    passwords = {
        "postgres_password": _ADMIN_PASSWORD,
        "postgres_serve_password": "serve-test-runtime-password-00001",
        "postgres_workers_password": "workers-test-runtime-password-001",
        "postgres_migrate_password": _OWNER_PASSWORD,
        "postgres_nautilus_password": "nautilus-test-runtime-password-01",
    }
    for name, password in passwords.items():
        path = secrets / name
        path.write_text(password + "\n", encoding="utf-8")
        path.chmod(0o644)
    init_script = (Path(__file__).parents[2] / "docker/postgres-init-runtime-roles.sh").resolve()

    _docker("volume", "create", volume)
    try:
        _docker(
            "run",
            "--name",
            container,
            "--detach",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_USER=tracefold_app",
            "--env",
            f"POSTGRES_DB={_DATABASE}",
            "--env",
            "POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password",
            "--volume",
            f"{volume}:/var/lib/postgresql",
            "--volume",
            f"{secrets.resolve()}:/run/secrets:ro",
            "--volume",
            f"{init_script}:/docker-entrypoint-initdb.d/10-tracefold-runtime-roles.sh:ro",
            DEFAULT_TEST_POSTGRES_IMAGE,
        )
        _wait_ready(container)
        port = _host_port(container)
        owner_dsn = _dsn(port, "tracefold_owner", _OWNER_PASSWORD)
        try:
            conn = connect_postgres(owner_dsn)
        except OperationalError as exc:
            logs = _docker("logs", container, check=False)
            pytest.fail(
                f"fresh owner login failed: {exc}; logs={(logs.stdout + logs.stderr)[-4_000:]}",
                pytrace=False,
            )
        with conn:
            roles = conn.execute(
                """
                SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                       rolreplication, rolbypassrls
                  FROM pg_roles WHERE rolname LIKE 'tracefold_%' ORDER BY rolname
                """
            ).fetchall()
        assert [row["rolname"] for row in roles] == sorted(_RUNTIME_ROLES)
        owner = next(row for row in roles if row["rolname"] == "tracefold_owner")
        bootstrap = next(row for row in roles if row["rolname"] == "tracefold_app")
        assert owner == {
            "rolname": "tracefold_owner",
            "rolcanlogin": True,
            "rolsuper": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
            "rolreplication": False,
            "rolbypassrls": False,
        }
        assert bootstrap["rolcanlogin"] is False
        assert bootstrap["rolsuper"] is True
        assert bootstrap["rolcreatedb"] is True
        assert bootstrap["rolcreaterole"] is True
        assert bootstrap["rolreplication"] is True
        assert bootstrap["rolbypassrls"] is True
        with connect_postgres(owner_dsn) as conn:
            set_capabilities = conn.execute(
                """
                SELECT role_name, pg_has_role(role_name, 'tracefold_owner', 'SET') AS can_set_owner
                  FROM unnest(ARRAY['tracefold_serve', 'tracefold_workers', 'tracefold_nautilus']) role_name
                 ORDER BY role_name
                """
            ).fetchall()
        assert set_capabilities == [
            {"role_name": "tracefold_nautilus", "can_set_owner": False},
            {"role_name": "tracefold_serve", "can_set_owner": False},
            {"role_name": "tracefold_workers", "can_set_owner": False},
        ]

        _upgrade(owner_dsn, "head")
        with connect_postgres(owner_dsn) as conn:
            assert runtime_role_contract(conn)["ok"] is True
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == (
                latest_migration_version()
            )

        _stop(container)
        _docker("start", container)
        _wait_ready(container)
        owner_dsn = _dsn(_host_port(container), "tracefold_owner", _OWNER_PASSWORD)
        with connect_postgres(owner_dsn) as conn:
            assert runtime_role_contract(conn)["ok"] is True
        _stop(container)
        final = _hard_cut(volume, secrets)
        assert final.returncode == 0, final.stderr
        assert "already complete" in final.stdout
    finally:
        _docker("rm", "--force", container, check=False)
        _docker("volume", "rm", "--force", volume, check=False)


def test_exact_old_volume_hard_cut_is_atomic_idempotent_and_preserves_acl_and_data(tmp_path: Path) -> None:
    suffix = uuid4().hex[:12]
    container = f"tracefold-owner-cut-{suffix}"
    volume = f"tracefold-owner-cut-{suffix}"
    dependency_database = f"tracefold_dependency_{suffix}"
    secrets = tmp_path / "cut-secrets"
    secrets.mkdir()
    password_file = secrets / "postgres_migrate_password"
    password_file.write_text(_OWNER_PASSWORD + "\n", encoding="utf-8")
    password_file.chmod(0o644)

    _docker("volume", "create", volume)
    try:
        _docker(
            "run",
            "--name",
            container,
            "--detach",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_PASSWORD={_ADMIN_PASSWORD}",
            "--env",
            f"POSTGRES_DB={_DATABASE}",
            "--volume",
            f"{volume}:/var/lib/postgresql",
            DEFAULT_TEST_POSTGRES_IMAGE,
        )
        _wait_ready(container)
        port = _host_port(container)
        admin_dsn = _dsn(port, "postgres", _ADMIN_PASSWORD)
        owner_dsn = _dsn(port, "tracefold_owner", _OWNER_PASSWORD)
        runtime_roles = (Path(__file__).parents[2] / "tracefold/platform/postgres/alembic/runtime_roles.sql").read_text(
            encoding="utf-8"
        )
        with connect_postgres(admin_dsn) as conn:
            conn.execute("CREATE EXTENSION pg_stat_statements WITH SCHEMA public")
            conn.execute("CREATE EXTENSION pg_trgm WITH SCHEMA public")
            conn.execute(runtime_roles)
            # The supported volume keeps the official image's bootstrap-superuser
            # catalog flags; owner-run migrations never rewrite that identity.
            conn.execute("ALTER ROLE tracefold_app REPLICATION BYPASSRLS")
            conn.execute(f"ALTER ROLE tracefold_owner PASSWORD '{_OWNER_PASSWORD}'")
        _upgrade(owner_dsn, "20260831_0338")
        with connect_postgres(owner_dsn) as conn:
            conn.execute("CREATE TABLE hard_cut_marker (id integer PRIMARY KEY, value text NOT NULL)")
            conn.execute("INSERT INTO hard_cut_marker VALUES (1, 'preserved')")
        with connect_postgres(admin_dsn) as conn:
            conn.execute(
                f"CREATE ROLE tracefold_migrate LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"NOREPLICATION NOBYPASSRLS PASSWORD '{_OLD_MIGRATE_PASSWORD}'"
            )
            conn.execute("GRANT tracefold_owner TO tracefold_migrate WITH ADMIN FALSE")
            conn.execute("GRANT tracefold_owner TO tracefold_migrate WITH INHERIT FALSE")
            conn.execute("GRANT tracefold_owner TO tracefold_migrate WITH SET TRUE")
            conn.execute("ALTER ROLE tracefold_owner NOLOGIN")
        assert _default_acl_entries(admin_dsn) == _DEFAULT_ACL_ENTRIES
        acl_before = _acl_fingerprint(admin_dsn)

        backup = tmp_path / "tracefold-0338.dump"
        receipt = tmp_path / "tracefold-0338-preflight.json"
        _docker(
            "exec",
            container,
            "pg_dump",
            "--username=postgres",
            f"--dbname={_DATABASE}",
            "--format=custom",
            "--file=/tmp/tracefold-0338.dump",
        )
        _docker("cp", f"{container}:/tmp/tracefold-0338.dump", str(backup))
        record_preflight(backup, receipt)
        receipt_payload = receipt.read_text(encoding="utf-8")
        assert '"migration_head": "20260831_0338"' in receipt_payload
        assert '"public_schema_owner": "tracefold_owner"' in receipt_payload
        assert '"owner_default_acl_count": 2' in receipt_payload
        assert '"default_acl_privilege_mismatches": 0' in receipt_payload

        with connect_postgres(admin_dsn) as conn:
            conn.execute("ALTER ROLE tracefold_owner SUPERUSER")
        _stop(container)
        malformed = _hard_cut(volume, secrets)
        assert malformed.returncode != 0
        assert _OWNER_PASSWORD not in malformed.stdout + malformed.stderr
        _docker("start", container)
        _wait_ready(container)
        port = _host_port(container)
        admin_dsn = _dsn(port, "postgres", _ADMIN_PASSWORD)
        owner_dsn = _dsn(port, "tracefold_owner", _OWNER_PASSWORD)
        with connect_postgres(admin_dsn) as conn:
            state = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'tracefold_owner'").fetchone()
            assert state["rolcanlogin"] is False
            assert conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_migrate'").fetchone() is not None
            conn.execute("ALTER ROLE tracefold_owner NOSUPERUSER")

            conn.execute("ALTER TABLE hard_cut_marker OWNER TO tracefold_migrate")

        _stop(container)
        malformed_object = _hard_cut(volume, secrets)
        assert malformed_object.returncode != 0
        assert _OWNER_PASSWORD not in malformed_object.stdout + malformed_object.stderr
        _docker("start", container)
        _wait_ready(container)
        port = _host_port(container)
        admin_dsn = _dsn(port, "postgres", _ADMIN_PASSWORD)
        owner_dsn = _dsn(port, "tracefold_owner", _OWNER_PASSWORD)
        with connect_postgres(admin_dsn) as conn:
            state = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'tracefold_owner'").fetchone()
            assert state["rolcanlogin"] is False
            assert conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_migrate'").fetchone() is not None
            conn.execute("ALTER TABLE hard_cut_marker OWNER TO tracefold_owner")

            conn.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public "
                "GRANT DELETE ON TABLES TO tracefold_nautilus"
            )
            assert conn.execute("SELECT count(*) AS count FROM pg_default_acl").fetchone()["count"] == 2

        _stop(container)
        malformed_default_acl = _hard_cut(volume, secrets)
        assert malformed_default_acl.returncode != 0
        assert _OWNER_PASSWORD not in malformed_default_acl.stdout + malformed_default_acl.stderr
        _docker("start", container)
        _wait_ready(container)
        port = _host_port(container)
        admin_dsn = _dsn(port, "postgres", _ADMIN_PASSWORD)
        owner_dsn = _dsn(port, "tracefold_owner", _OWNER_PASSWORD)
        with connect_postgres(admin_dsn) as conn:
            state = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'tracefold_owner'").fetchone()
            assert state["rolcanlogin"] is False
            assert conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_migrate'").fetchone() is not None
            conn.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public "
                "REVOKE DELETE ON TABLES FROM tracefold_nautilus"
            )

        assert _default_acl_entries(admin_dsn) == _DEFAULT_ACL_ENTRIES

        with connect_postgres(admin_dsn) as conn:
            conn.execute(f"CREATE DATABASE {dependency_database} OWNER tracefold_migrate")

        _stop(container)
        interrupted = _hard_cut(volume, secrets)
        assert interrupted.returncode != 0
        assert "rolled back" in interrupted.stderr
        assert _OWNER_PASSWORD not in interrupted.stdout + interrupted.stderr
        _docker("start", container)
        _wait_ready(container)
        port = _host_port(container)
        admin_dsn = _dsn(port, "postgres", _ADMIN_PASSWORD)
        owner_dsn = _dsn(port, "tracefold_owner", _OWNER_PASSWORD)
        with connect_postgres(admin_dsn) as conn:
            state = conn.execute(
                """
                SELECT owner.rolcanlogin,
                       pg_has_role('tracefold_migrate', 'tracefold_owner', 'MEMBER') AS member
                  FROM pg_roles owner WHERE owner.rolname = 'tracefold_owner'
                """
            ).fetchone()
            assert state == {"rolcanlogin": False, "member": True}
            conn.execute(f"DROP DATABASE {dependency_database}")

        _stop(container)
        first = _hard_cut(volume, secrets)
        assert first.returncode == 0, first.stderr
        assert "hard cut complete" in first.stdout
        assert _OWNER_PASSWORD not in first.stdout + first.stderr
        second = _hard_cut(volume, secrets)
        assert second.returncode == 0, second.stderr
        assert "already complete" in second.stdout
        assert _OWNER_PASSWORD not in second.stdout + second.stderr

        _docker("start", container)
        _wait_ready(container)
        port = _host_port(container)
        admin_dsn = _dsn(port, "postgres", _ADMIN_PASSWORD)
        owner_dsn = _dsn(port, "tracefold_owner", _OWNER_PASSWORD)
        assert _acl_fingerprint(admin_dsn) == acl_before
        with connect_postgres(owner_dsn) as conn:
            assert conn.execute("SELECT value FROM hard_cut_marker WHERE id = 1").fetchone()["value"] == "preserved"
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == (
                "20260831_0338"
            )
        with pytest.raises(OperationalError):
            connect_postgres(_dsn(port, "tracefold_migrate", _OLD_MIGRATE_PASSWORD))

        _upgrade(owner_dsn, "head")
        with connect_postgres(owner_dsn) as conn:
            assert conn.execute("SELECT value FROM hard_cut_marker WHERE id = 1").fetchone()["value"] == "preserved"
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == (
                latest_migration_version()
            )
            contract = runtime_role_contract(conn)
            assert contract["ok"] is True, contract
    finally:
        _docker("rm", "--force", container, check=False)
        _docker("volume", "rm", "--force", volume, check=False)
