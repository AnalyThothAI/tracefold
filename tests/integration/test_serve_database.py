from __future__ import annotations

import asyncio

import pytest

from tests.postgres_test_utils import postgres_settings_storage
from tracefold.app.serve_database import ServeDatabase
from tracefold.platform.config.models import Settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_serve_session_policy_is_applied_at_connect_time() -> None:
    database = ServeDatabase.create(
        Settings(storage=postgres_settings_storage()),
        telemetry=None,
    )
    try:
        with database.api_session() as repos:
            row = repos.conn.execute(
                "SELECT current_setting('jit') AS jit, "
                "current_setting('max_parallel_workers_per_gather') AS max_parallel_workers_per_gather, "
                "current_setting('work_mem') AS work_mem"
            ).fetchone()
        assert row == {
            "jit": "off",
            "max_parallel_workers_per_gather": "0",
            "work_mem": "8MB",
        }
    finally:
        asyncio.run(database.aclose())
