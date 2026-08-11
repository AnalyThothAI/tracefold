"""Run one real Token Radar worker sample against the isolated E2E database."""

from __future__ import annotations

import asyncio
import os
import sys


async def _sample(postgres_dsn: str) -> None:
    from tracefold.app.database import WorkerDatabase
    from tracefold.app.worker_capabilities import CpuProcess
    from tracefold.market import TokenRadarCurrentProjection
    from tracefold.platform.config.settings import Settings

    settings = Settings(
        storage={
            "postgres": {
                "serve_dsn": postgres_dsn,
                "workers_dsn": postgres_dsn,
                "migrate_dsn": postgres_dsn,
                "serve_password_file": None,
                "workers_password_file": None,
                "migrate_password_file": None,
            }
        }
    )
    database = WorkerDatabase.create(settings)
    cpu = CpuProcess()
    try:
        await cpu.prewarm()
        await TokenRadarCurrentProjection(
            db=database,
            cpu=cpu,
            source_is_streaming=lambda: True,
        ).sample()
    finally:
        cpu.close_admission()
        database.close_business_admission()
        try:
            if not await cpu.drain(timeout_seconds=5.0):
                raise RuntimeError("token_radar_e2e_cpu_drain_timeout")
            if not await database.drain_business(timeout_seconds=5.0):
                raise RuntimeError("token_radar_e2e_database_drain_timeout")
        finally:
            cpu.close()
            try:
                await database.aclose()
            finally:
                database.close_executors()


def main() -> int:
    postgres_dsn = os.environ.get("TRACEFOLD_POSTGRES_DSN")
    if not postgres_dsn:
        print("FATAL: TRACEFOLD_POSTGRES_DSN not set", file=sys.stderr)
        return 1
    asyncio.run(_sample(postgres_dsn))
    print("TOKEN_RADAR_SAMPLE_COMPLETED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
