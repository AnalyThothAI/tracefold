"""Drill an immutable rollback image against an isolated disposable PostgreSQL."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
import uuid

POSTGRES_IMAGE = "postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296"
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _run(
    args: list[str],
    *,
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - callers use only the code-owned docker argv below
        args,
        check=check,
        capture_output=True,
        text=True,
        env=None if environment is None else {**os.environ, **environment},
    )


def drill(image_id: str) -> None:
    if IMAGE_ID.fullmatch(image_id) is None:
        raise ValueError("news_rollback_drill_image_id_invalid")
    inspected = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image_id]).stdout.strip()
    if inspected != image_id:
        raise ValueError("news_rollback_drill_image_id_resolution_mismatch")

    suffix = uuid.uuid4().hex[:16]
    network = f"tracefold-news-rollback-drill-{suffix}"
    postgres = f"tracefold-news-rollback-postgres-{suffix}"
    password = uuid.uuid4().hex
    database = "tracefold_rollback_drill"
    network_created = False
    postgres_created = False
    try:
        _run(["docker", "network", "create", network])
        network_created = True
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                postgres,
                "--network",
                network,
                "--label",
                "io.tracefold.news.rollback.drill=true",
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                "POSTGRES_DB",
                POSTGRES_IMAGE,
                "postgres",
                "-c",
                "shared_preload_libraries=pg_stat_statements",
                "-c",
                "compute_query_id=on",
            ],
            environment={"POSTGRES_PASSWORD": password, "POSTGRES_DB": database},
        )
        postgres_created = True
        for _ in range(45):
            ready = _run(
                ["docker", "exec", postgres, "pg_isready", "-U", "postgres", "-d", database],
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("news_rollback_drill_postgres_not_ready")

        dsn = f"postgresql://postgres:{password}@{postgres}:5432/{database}"
        result = _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--env",
                "TRACEFOLD_ROLLBACK_DRILL_DSN",
                "--entrypoint",
                "python",
                image_id,
                "scripts/drill_news_rollback.py",
            ],
            check=False,
            environment={"TRACEFOLD_ROLLBACK_DRILL_DSN": dsn},
        )
        if result.returncode != 0:
            diagnostic = (result.stderr or result.stdout).replace(password, "<redacted>").replace(dsn, "<redacted-dsn>")
            raise RuntimeError(f"news_rollback_drill_container_failed:\n{diagnostic[-4000:]}")
        if result.stdout.strip() != "news_program_v5_schema0300_rollback_drill_ok":
            raise RuntimeError("news_rollback_drill_receipt_invalid")
    finally:
        if postgres_created:
            _run(["docker", "container", "rm", "--force", postgres], check=False)
        if network_created:
            _run(["docker", "network", "rm", network], check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-id", required=True)
    args = parser.parse_args()
    drill(args.image_id)
    print("news_program_v5_schema0300_rollback_image_drilled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
