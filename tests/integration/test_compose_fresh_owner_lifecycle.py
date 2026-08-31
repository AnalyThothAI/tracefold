from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import pytest

from tracefold.platform.postgres.migrations import latest_migration_version

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_ROOT = Path(__file__).parents[2]
_SERVICES = ("postgres", "rabbitmq", "rabbitmq-policy", "migrate", "serve", "workers")


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _run(arguments: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=_ROOT,
        env=env,
        capture_output=True,
        check=check,
        text=True,
        timeout=900,
    )


def test_fresh_compose_reaches_single_login_readiness_twice(tmp_path: Path) -> None:
    """Exercise the real Compose layer twice; deploy tests separately seal the locked `make up` recipe."""

    suffix = uuid4().hex[:12]
    project = f"tracefold-login-fresh-{suffix}"
    image = f"tracefold-login-fresh-{suffix}:test"
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    env.update(
        {
            "HOME": str(home),
            "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")),
            "TRACEFOLD_APP_IMAGE": image,
            "TRACEFOLD_BUILD_REVISION": _run(
                ["git", "rev-parse", "HEAD"], env={**os.environ, "HOME": str(home)}
            ).stdout.strip(),
            "TRACEFOLD_POSTGRES_PORT": str(_free_port()),
            "TRACEFOLD_RABBITMQ_PORT": str(_free_port()),
            "TRACEFOLD_RABBITMQ_MGMT_PORT": str(_free_port()),
            "TRACEFOLD_API_PORT": str(_free_port()),
            "TRACEFOLD_WORKERS_PORT": str(_free_port()),
            "TRACEFOLD_NAUTILUS_PORT": str(_free_port()),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
        }
    )
    compose = ["docker", "compose", "--project-name", project, "--file", str(_ROOT / "compose.yaml")]

    try:
        initialized = _run(["uv", "run", "tracefold", "init"], env=env)
        assert initialized.returncode == 0, initialized.stderr
        built = _run([*compose, "build", "migrate"], env=env)
        assert built.returncode == 0, built.stderr
        env["TRACEFOLD_IMAGE_DIGEST"] = _run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image], env=env
        ).stdout.strip()

        # The fresh-volume contract is PostgreSQL-only; RabbitMQ policy behavior is an explicit
        # non-goal. Seed the isolated broker's already-supported topology, then leave PostgreSQL's
        # named volume absent for the first complete application startup below.
        broker = _run([*compose, "up", "--detach", "--wait", "--wait-timeout", "120", "rabbitmq"], env=env, check=False)
        assert broker.returncode == 0, broker.stderr
        policy = _run([*compose, "run", "--rm", "--no-deps", "rabbitmq-policy"], env=env, check=False)
        assert policy.returncode == 0, policy.stderr
        for _ in range(60):
            bus_check = _run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "tracefold",
                    "migrate",
                    "news",
                    "bus-check",
                ],
                env=env,
                check=False,
            )
            if bus_check.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail(
                f"isolated RabbitMQ policy did not settle:\n{bus_check.stdout}\n{bus_check.stderr}", pytrace=False
            )

        first_manifest: str | None = None
        for invocation in ("first", "second"):
            started = _run(
                [
                    *compose,
                    "up",
                    "--detach",
                    "--no-build",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "300",
                    *_SERVICES,
                ],
                env=env,
                check=False,
            )
            if started.returncode != 0:
                logs = _run([*compose, "logs", "--no-color"], env=env, check=False)
                pytest.fail(
                    f"{invocation} Compose up failed:\n{started.stdout}\n{started.stderr}\n"
                    f"{logs.stdout}\n{logs.stderr}",
                    pytrace=False,
                )

            with urlopen(f"http://127.0.0.1:{env['TRACEFOLD_API_PORT']}/readyz", timeout=5) as response:
                assert response.status == 200
            with urlopen(f"http://127.0.0.1:{env['TRACEFOLD_WORKERS_PORT']}/readyz", timeout=5) as response:
                readiness = json.loads(response.read())
            manifest = str(readiness["runtime_manifest_sha"])
            assert len(manifest) == 64
            if first_manifest is None:
                first_manifest = manifest
            else:
                assert manifest == first_manifest

            database_contract = _run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "postgres",
                    "sh",
                    "-eu",
                    "-c",
                    "PGPASSWORD=$(cat /run/secrets/postgres_database_password); export PGPASSWORD; "
                    "psql -X -A -t -v ON_ERROR_STOP=1 -h 127.0.0.1 -U tracefold -d tracefold "
                    " -c \"SELECT current_user || '|' || (SELECT version_num FROM alembic_version) || '|' || "
                    "(SELECT string_agg(rolname, ',' ORDER BY rolname) FROM pg_roles "
                    "WHERE rolname IN ('tracefold', 'tracefold_app'))" + '"',
                ],
                env=env,
            ).stdout.strip()
            assert database_contract == (f"tracefold|{latest_migration_version()}|tracefold,tracefold_app")
    finally:
        _run([*compose, "down", "--volumes", "--remove-orphans", "--timeout", "5"], env=env, check=False)
        _run(["docker", "image", "rm", image], env=env, check=False)
