from __future__ import annotations

import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from tests.tracefold_postgres_container import DEFAULT_TEST_POSTGRES_IMAGE

pytestmark = pytest.mark.integration


def test_offline_nautilus_role_provisioning_is_real_and_idempotent(tmp_path: Path) -> None:
    suffix = uuid4().hex[:12]
    container_name = f"tracefold-nautilus-role-{suffix}"
    volume_name = f"tracefold-nautilus-role-{suffix}"
    database_name = "tracefold_test"
    password = "N" * 43
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    password_file = secrets_dir / "postgres_nautilus_password"
    password_file.write_text(password + "\n", encoding="utf-8")
    password_file.chmod(0o644)
    script_path = (Path(__file__).parents[2] / "docker/postgres-provision-nautilus-role.sh").resolve()

    def run_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            check=check,
            text=True,
            timeout=180,
        )

    def wait_until_ready() -> None:
        for _ in range(60):
            process = run_docker("exec", container_name, "cat", "/proc/1/comm", check=False)
            if process.stdout.strip() != "postgres":
                time.sleep(0.5)
                continue
            result = run_docker(
                "exec",
                container_name,
                "pg_isready",
                "-U",
                "postgres",
                "-d",
                database_name,
                check=False,
            )
            if result.returncode == 0:
                return
            time.sleep(0.5)
        pytest.fail("isolated PostgreSQL 18 container did not become ready", pytrace=False)

    def stop_postgres() -> None:
        run_docker("kill", "--signal", "SIGINT", container_name)
        assert run_docker("wait", container_name).stdout.strip() == "0"

    run_docker("volume", "create", volume_name)
    try:
        run_docker(
            "run",
            "--name",
            container_name,
            "--detach",
            "--env",
            "POSTGRES_PASSWORD=postgres",
            "--env",
            f"POSTGRES_DB={database_name}",
            "--volume",
            f"{volume_name}:/var/lib/postgresql",
            DEFAULT_TEST_POSTGRES_IMAGE,
        )
        wait_until_ready()
        stop_postgres()

        for _ in range(2):
            provision = run_docker(
                "run",
                "--rm",
                "--user",
                "postgres",
                "--env",
                f"POSTGRES_DB={database_name}",
                "--volume",
                f"{volume_name}:/var/lib/postgresql",
                "--volume",
                f"{script_path}:/usr/local/bin/tracefold-provision-nautilus-role:ro",
                "--volume",
                f"{secrets_dir.resolve()}:/run/secrets:ro",
                "--entrypoint",
                "sh",
                DEFAULT_TEST_POSTGRES_IMAGE,
                "/usr/local/bin/tracefold-provision-nautilus-role",
                "/run/secrets",
            )
            assert password not in provision.stdout
            assert password not in provision.stderr

            run_docker("start", container_name)
            wait_until_ready()
            role = run_docker(
                "exec",
                container_name,
                "psql",
                "-XAt",
                "-U",
                "postgres",
                "-d",
                database_name,
                "-c",
                """
                SELECT count(*) OVER (), rolcanlogin, rolinherit, rolsuper, rolcreatedb,
                       rolcreaterole, rolreplication, rolbypassrls
                  FROM pg_roles
                 WHERE rolname = 'tracefold_nautilus'
                """,
            )
            assert role.stdout.strip() == "1|t|t|f|f|f|f|f"
            stop_postgres()
    finally:
        run_docker("rm", "--force", container_name, check=False)
        run_docker("volume", "rm", "--force", volume_name, check=False)
