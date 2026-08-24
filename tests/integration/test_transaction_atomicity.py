from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.app.worker_database import WorkerDatabase
from tracefold.platform.postgres import client
from tracefold.platform.postgres.client import create_pool

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]


def _worker_pool_bundle(pool: Any) -> WorkerDatabase:
    return WorkerDatabase(worker_pool=pool)


def test_worker_session_explicit_transaction_rolls_back_all_statements() -> None:
    setup_conn = connect_postgres_test(read_only=False)
    pool = create_pool(
        postgres_test_dsn(),
        min_size=0,
        max_size=1,
        connect_timeout_seconds=5,
        application_name="gmgn_test_worker",
        statement_timeout_seconds=5,
    )
    try:
        setup_conn.execute("DROP TABLE IF EXISTS transaction_atomicity_probe")
        setup_conn.execute(
            """
            CREATE TABLE transaction_atomicity_probe (
                id text PRIMARY KEY,
                label text NOT NULL
            )
            """
        )
        setup_conn.commit()
        bundle = _worker_pool_bundle(pool)

        with (
            pytest.raises(RuntimeError, match="boom"),
            bundle.worker_session("atomicity_probe") as repos,
            repos.transaction(),
        ):
            repos.conn.execute(
                "INSERT INTO transaction_atomicity_probe (id, label) VALUES (%s, %s)",
                ("first", "inside"),
            )
            repos.conn.execute(
                "INSERT INTO transaction_atomicity_probe (id, label) VALUES (%s, %s)",
                ("second", "inside"),
            )
            raise RuntimeError("boom")

        row = setup_conn.execute("SELECT count(*) AS row_count FROM transaction_atomicity_probe").fetchone()
        assert row["row_count"] == 0
    finally:
        setup_conn.execute("DROP TABLE IF EXISTS transaction_atomicity_probe")
        setup_conn.commit()
        setup_conn.close()
        pool.close()


def test_require_transaction_rejects_real_postgres_autocommit_connection() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        with pytest.raises(RuntimeError, match="projection_write_requires_explicit_transaction"):
            client.require_transaction(conn, operation="projection_write")
    finally:
        conn.close()


def test_require_transaction_accepts_real_postgres_transaction() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            client.require_transaction(conn, operation="projection_write")
    finally:
        conn.close()
