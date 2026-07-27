from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.app.database import DBPoolBundle
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import (
    MarketTick,
    MarketTickPersistenceService,
    RegistryRepository,
    market_tick_id,
)
from tracefold.platform.postgres import postgres_client
from tracefold.platform.postgres.postgres_client import create_pool


def _worker_pool_bundle(pool: Any) -> DBPoolBundle:
    return DBPoolBundle(
        api_pool=None,
        worker_pool=pool,
    )


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
            postgres_client.require_transaction(conn, operation="projection_write")
    finally:
        conn.close()


def test_require_transaction_accepts_real_postgres_transaction() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            postgres_client.require_transaction(conn, operation="projection_write")
    finally:
        conn.close()


def test_require_transaction_rejects_fake_connections_without_psycopg_info() -> None:
    with pytest.raises(RuntimeError, match="fake_write_requires_transaction_status_contract"):
        postgres_client.require_transaction(object(), operation="fake_write")


def test_repository_session_transaction_owns_database_transaction() -> None:
    conn = FakeTransactionConnection()
    repos = repositories_for_connection(
        conn,
    )

    with repos.transaction():
        conn.events.append("body")

    assert conn.events == ["begin", "body", "commit"]


def test_market_tick_persistence_rolls_back_fact_current_and_downstream_dirty_target() -> None:
    setup_conn = connect_postgres_test(read_only=False)
    pool = create_pool(
        postgres_test_dsn(),
        min_size=0,
        max_size=1,
        connect_timeout_seconds=5,
        application_name="gmgn_test_market_tick_atomicity",
        statement_timeout_seconds=5,
    )
    now_ms = 1_900_000_000_000
    tick = _market_tick(observed_at_ms=now_ms)
    asset_id = "asset:solana:token:atomicity"
    try:
        _delete_market_tick_target(setup_conn, tick, asset_id=asset_id)
        asset = RegistryRepository(setup_conn).upsert_chain_asset(
            chain_id="solana",
            address="atomicity",
            observed_at_ms=now_ms - 1,
        )
        assert asset["asset_id"] == asset_id
        setup_conn.commit()

        bundle = _worker_pool_bundle(pool)
        with (
            pytest.raises(RuntimeError, match="fail_after_market_projection"),
            bundle.worker_session("market_tick_atomicity") as repos,
            repos.transaction(),
        ):
            result = MarketTickPersistenceService(repos).persist_ticks([tick], now_ms=now_ms)
            assert result.changed_targets == [(tick.target_type, tick.target_id)]
            raise RuntimeError("fail_after_market_projection")

        tick_count = setup_conn.execute(
            "SELECT count(*) AS row_count FROM market_ticks WHERE observed_at_ms = %s AND tick_id = %s",
            (tick.observed_at_ms, tick.tick_id),
        ).fetchone()
        current_count = setup_conn.execute(
            """
            SELECT count(*) AS row_count
            FROM market_tick_current
            WHERE target_type = %s AND target_id = %s
            """,
            (tick.target_type, tick.target_id),
        ).fetchone()
        dirty_count = setup_conn.execute(
            """
            SELECT count(*) AS row_count
            FROM token_radar_dirty_targets
            WHERE target_type_key = 'Asset' AND identity_id = %s
            """,
            (asset_id,),
        ).fetchone()
        assert tick_count["row_count"] == 0
        assert current_count["row_count"] == 0
        assert dirty_count["row_count"] == 0
    finally:
        _delete_market_tick_target(setup_conn, tick, asset_id=asset_id)
        setup_conn.execute("DELETE FROM registry_assets WHERE asset_id = %s", (asset_id,))
        setup_conn.commit()
        setup_conn.close()
        pool.close()


class FakeTransactionConnection:
    def __init__(self) -> None:
        self.events: list[str] = []

    @contextmanager
    def transaction(self) -> Any:
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")


def _delete_market_tick_target(conn: Any, tick: MarketTick, *, asset_id: str) -> None:
    conn.execute(
        "DELETE FROM market_tick_current WHERE target_type = %s AND target_id = %s",
        (tick.target_type, tick.target_id),
    )
    conn.execute(
        "DELETE FROM token_radar_dirty_targets WHERE target_type_key = 'Asset' AND identity_id = %s",
        (asset_id,),
    )
    conn.execute(
        "DELETE FROM market_ticks WHERE target_type = %s AND target_id = %s",
        (tick.target_type, tick.target_id),
    )


def _market_tick(*, observed_at_ms: int) -> MarketTick:
    target_type = "chain_token"
    target_id = "solana:atomicity"
    source_provider = "okx_dex_ws"
    return MarketTick(
        tick_id=market_tick_id(
            target_type=target_type,
            target_id=target_id,
            source_provider=source_provider,
            observed_at_ms=observed_at_ms,
        ),
        target_type=target_type,
        target_id=target_id,
        chain="solana",
        token_address="atomicity",
        exchange=None,
        instrument=None,
        pricefeed_id=None,
        source_tier="tier1_ws",
        source_provider=source_provider,
        observed_at_ms=observed_at_ms,
        received_at_ms=observed_at_ms + 1,
        price_usd=Decimal("1.23"),
        liquidity_usd=None,
        volume_24h_usd=None,
        open_interest_usd=None,
        market_cap_usd=None,
        holders=None,
        created_at_ms=observed_at_ms + 2,
        raw_payload_json={"source": "atomicity"},
    )
