from __future__ import annotations

import asyncio
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage
from tests.postgres_test_utils import test_postgres_dsn as postgres_dsn
from tracefold.app.http.app import create_app
from tracefold.app.serve_database import ServeDatabase
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_http_monitoring_cannot_append_manual_intents(tmp_path) -> None:
    """Cross the real HTTP / Serve / PostgreSQL boundary with a formerly valid command."""
    settings = Settings(ws_token="read-only-test", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path)
    with psycopg.connect(postgres_dsn()) as conn:
        before = conn.execute("SELECT count(*) FROM trading_operator_intents").fetchone()
    with TestClient(create_app(settings=settings)) as client:
        headers = {"Authorization": "Bearer read-only-test"}
        for text in ("/pause investigation", "/resume investigation complete", "/flatten account 30"):
            response = client.post(
                "/api/trading/execution/commands",
                headers=headers,
                json={
                    "request_id": "11111111-1111-4111-8111-111111111111",
                    "requested_at_ms": int(time.time() * 1000),
                    "text": text,
                },
            )
            assert response.status_code == 404
        for path in ("status", "cases", "executions"):
            response = client.get(f"/api/trading/{path}", headers=headers)
            assert response.status_code == 200
            assert response.json()["ok"] is True
    with psycopg.connect(postgres_dsn()) as conn:
        assert conn.execute("SELECT count(*) FROM trading_operator_intents").fetchone() == before


def test_serve_session_policy_is_applied_at_connect_time() -> None:
    database = ServeDatabase.create(
        Settings(storage=postgres_settings_storage()),
        telemetry=TelemetryRegistry(),
    )
    try:
        with database.api_session() as repos:
            row = repos.session_policy()
        assert row == {
            "jit": "off",
            "max_parallel_workers_per_gather": "0",
            "work_mem": "8MB",
        }
    finally:
        asyncio.run(database.aclose())


def test_serve_pool_uses_the_shared_login_with_stable_read_only_attribution() -> None:
    database = ServeDatabase.create(
        Settings(storage=postgres_settings_storage()),
        telemetry=TelemetryRegistry(),
    )
    try:
        with database.api_pool.connection() as conn:
            identity = conn.execute(
                "SELECT current_user AS role_name, "
                "current_setting('application_name') AS application_name, "
                "current_setting('default_transaction_read_only') AS read_only"
            ).fetchone()
            assert identity == {
                "role_name": "tracefold",
                "application_name": "tracefold_serve",
                "read_only": "on",
            }
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute(
                    "UPDATE news_ingest_state SET updated_at_ms = updated_at_ms WHERE singleton_key = 'opennews'"
                )
    finally:
        asyncio.run(database.aclose())


def test_serve_pool_is_fully_warm_when_startup_returns() -> None:
    database = ServeDatabase.create(
        Settings(storage=postgres_settings_storage()),
        telemetry=TelemetryRegistry(),
    )
    try:
        stats = database.api_pool.get_stats()
        assert stats["pool_min"] == 7
        assert stats["pool_max"] == 7
        assert stats["pool_size"] == 7
        assert stats["pool_available"] == 7
    finally:
        asyncio.run(database.aclose())
