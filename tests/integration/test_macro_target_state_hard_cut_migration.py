from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from alembic import command
from psycopg import pq
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test, repository_session_for_connection
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.projection import MacroProjectionService
from tracefold.platform.postgres.postgres_migrations import alembic_config


class _SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        try:
            with repository_session_for_connection(self.conn) as repos:
                yield repos
        finally:
            if self.conn.info.transaction_status != pq.TransactionStatus.IDLE:
                self.conn.rollback()


def test_0252_converts_only_legacy_target_states_and_removes_invalid() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260810_0251")
        conn.execute(
            """
            INSERT INTO macro_acquisition_targets(
              target_key, dataset_id, partition_key, clock_kind, cursor_json,
              status, next_due_at_ms, priority, attempt_count, max_attempts,
              created_at_ms, updated_at_ms
            ) VALUES
              (
                'steady-stale', 'fred.dgs10', 'latest', 'daily_settlement', '{}',
                'stale', 9000, 10, 5, 5, 100, 800
              ),
              (
                'steady-invalid', 'fred.dgs2', 'latest', 'daily_settlement', '{}',
                'invalid', 10000, 10, 3, 5, 100, 700
              ),
              (
                'backfill-invalid', 'fred.dgs10', '2020-01-01..2025-01-01', 'backfill',
                '{"start_date":"2020-01-01","end_date":"2025-01-01","history_class":"required_history"}',
                'invalid', 11000, 10, 5, 5, 100, 600
              ),
              (
                'backfill-stale', 'fred.dgs2', '2020-01-01..2025-01-01', 'backfill',
                '{"start_date":"2020-01-01","end_date":"2025-01-01","history_class":"required_history"}',
                'stale', 12000, 10, 5, 5, 100, 500
              );
            """
        )
        conn.execute(
            """
            INSERT INTO macro_module_frontiers (
              module_id, status, input_fingerprint, projection_version,
              updated_at_ms
            ) VALUES (
              'liquidity_funding', 'clean', 'sha256:legacy-target-state',
              'sha256:legacy-projection', 500
            )
            """
        )
        conn.execute(
            """
            INSERT INTO macro_dataset_projection_states (
              dataset_id, material_fingerprint, acquisition_status,
              source_frontier_ms, updated_at_ms
            ) VALUES (
              'fred.dgs10', 'sha256:persisted-state', 'current', 750, 750
            )
            """
        )
        conn.commit()

        command.upgrade(config, "20260811_0252")

        assert conn.execute(
            """
            SELECT target_key, status, attempt_count, next_due_at_ms
              FROM macro_acquisition_targets
             ORDER BY target_key
            """
        ).fetchall() == [
            {
                "target_key": "backfill-invalid",
                "status": "stale",
                "attempt_count": 5,
                "next_due_at_ms": 11000,
            },
            {
                "target_key": "backfill-stale",
                "status": "stale",
                "attempt_count": 5,
                "next_due_at_ms": 12000,
            },
            {
                "target_key": "steady-invalid",
                "status": "delayed",
                "attempt_count": 0,
                "next_due_at_ms": 700,
            },
            {
                "target_key": "steady-stale",
                "status": "delayed",
                "attempt_count": 0,
                "next_due_at_ms": 800,
            },
        ]
        index = conn.execute(
            """
            SELECT indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND tablename = 'macro_acquisition_targets'
               AND indexname = 'idx_macro_acquisition_targets_due'
            """
        ).fetchone()
        assert index is not None
        assert "'invalid'" not in index["indexdef"]
        assert "'stale'" not in index["indexdef"]
        assert conn.execute("SELECT count(*) AS count FROM macro_module_frontiers").fetchone()["count"] == 0

        service = MacroProjectionService(db=_SingleConnectionDB(conn))
        assert service.reconcile_frontiers(now_ms=13_000) == len(MACRO_MODULE_IDS)
        assert conn.execute(
            """
            SELECT module_id, status
              FROM macro_module_frontiers
             ORDER BY module_id
            """
        ).fetchall() == [{"module_id": module_id, "status": "dirty"} for module_id in sorted(MACRO_MODULE_IDS)]
        assert conn.execute("SELECT count(*) AS count FROM macro_dataset_projection_states").fetchone()["count"] == 1

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE macro_acquisition_targets
                   SET status = 'invalid'
                 WHERE target_key = 'steady-stale'
                """
            )
        conn.rollback()
    finally:
        conn.close()


def test_0252_downgrade_is_deliberately_unsupported() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260811_0252_macro_target_state_hard_cut"
    )

    with pytest.raises(RuntimeError, match="irreversible Macro target-state hard cut"):
        migration.downgrade()
