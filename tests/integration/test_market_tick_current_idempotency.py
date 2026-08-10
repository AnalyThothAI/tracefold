from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import (
    MarketTick,
    MarketTickPersistenceService,
    market_tick_id,
)

NOW_MS = 1_800_000_000_000
TARGET_ID = "eip155:1:0x1111111111111111111111111111111111111111"
SECOND_TARGET_ID = "eip155:1:0x2222222222222222222222222222222222222222"


def test_market_tick_persistence_is_idempotent_and_updates_current_in_same_transaction(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repos = repositories_for_connection(
            conn,
        )
        tick = _tick()

        with repos.transaction():
            first = MarketTickPersistenceService(repos).persist_ticks([tick, tick], now_ms=NOW_MS + 10)
        with repos.transaction():
            duplicate = MarketTickPersistenceService(repos).persist_ticks([tick], now_ms=NOW_MS + 20)

        assert first.inserted == 1
        assert first.changed_targets == [("chain_token", TARGET_ID)]
        assert duplicate.inserted == 0
        assert duplicate.changed_targets == []

        fact_count = conn.execute(
            "SELECT count(*) AS count FROM market_ticks WHERE tick_id = %s",
            (tick.tick_id,),
        ).fetchone()
        current = repos.market_tick_current.get(target_type="chain_token", target_id=TARGET_ID)
        assert fact_count["count"] == 1
        assert current is not None
        assert current["tick_id"] == tick.tick_id
        assert "raw_payload_json" not in current
        assert "payload_hash" not in current
    finally:
        conn.close()


def test_older_tick_fact_cannot_regress_market_tick_current(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repos = repositories_for_connection(
            conn,
        )
        newer = _tick()
        older_observed_at_ms = NOW_MS - 1_000
        older = replace(
            newer,
            tick_id=market_tick_id(
                target_type="chain_token",
                target_id=TARGET_ID,
                source_provider="okx_dex_rest",
                observed_at_ms=older_observed_at_ms,
            ),
            source_tier="tier2_poll",
            source_provider="okx_dex_rest",
            observed_at_ms=older_observed_at_ms,
            received_at_ms=NOW_MS + 10_000,
            price_usd=Decimal("0.50"),
        )

        with repos.transaction():
            first = MarketTickPersistenceService(repos).persist_ticks([newer], now_ms=NOW_MS + 1)
        with repos.transaction():
            stale = MarketTickPersistenceService(repos).persist_ticks([older], now_ms=NOW_MS + 10_001)

        current = repos.market_tick_current.get(target_type="chain_token", target_id=TARGET_ID)
        assert first.changed_targets == [("chain_token", TARGET_ID)]
        assert stale.inserted == 1
        assert stale.changed_targets == []
        assert current is not None
        assert current["tick_id"] == newer.tick_id
        assert current["price_usd"] == Decimal("1.23")
    finally:
        conn.close()


def test_market_tick_current_can_be_rebuilt_in_bounded_fact_batches(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repos = repositories_for_connection(
            conn,
        )
        with repos.transaction():
            MarketTickPersistenceService(repos).persist_ticks([_tick()], now_ms=NOW_MS)
        with repos.transaction():
            conn.execute("DELETE FROM market_tick_current")

        with repos.transaction():
            rebuilt = MarketTickPersistenceService(repos).rebuild_current_batch(
                after=None,
                limit=50,
                now_ms=NOW_MS + 100,
            )
        with repos.transaction():
            exhausted = MarketTickPersistenceService(repos).rebuild_current_batch(
                after=rebuilt.next_cursor,
                limit=50,
                now_ms=NOW_MS + 200,
            )

        current = repos.market_tick_current.get(target_type="chain_token", target_id=TARGET_ID)
        assert rebuilt.scanned_targets == 1
        assert rebuilt.changed_targets == (("chain_token", TARGET_ID),)
        assert rebuilt.next_cursor == ("chain_token", TARGET_ID)
        assert exhausted.scanned_targets == 0
        assert exhausted.changed_targets == ()
        assert exhausted.next_cursor is None
        assert current is not None
        assert current["tick_id"] == _tick().tick_id
    finally:
        conn.close()


def test_market_tick_current_rebuild_repairs_equal_key_payload_drift_once(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repos = repositories_for_connection(
            conn,
        )
        with repos.transaction():
            MarketTickPersistenceService(repos).persist_ticks([_tick()], now_ms=NOW_MS)
            conn.execute(
                """
                UPDATE market_tick_current
                SET price_usd = 999, liquidity_usd = NULL
                WHERE target_type = 'chain_token' AND target_id = %s
                """,
                (TARGET_ID,),
            )

        with repos.transaction():
            repaired = MarketTickPersistenceService(repos).rebuild_current_batch(
                after=None,
                limit=50,
                now_ms=NOW_MS + 100,
            )
        current = repos.market_tick_current.get(target_type="chain_token", target_id=TARGET_ID)
        assert repaired.changed_targets == (("chain_token", TARGET_ID),)
        assert current is not None
        assert current["price_usd"] == Decimal("1.23")
        assert current["liquidity_usd"] == Decimal("1000")

        with repos.transaction():
            unchanged = MarketTickPersistenceService(repos).rebuild_current_batch(
                after=None,
                limit=50,
                now_ms=NOW_MS + 200,
            )
        assert unchanged.changed_targets == ()
    finally:
        conn.close()


def test_market_tick_rebuild_cursor_seeks_one_stable_target_page_at_a_time(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repos = repositories_for_connection(
            conn,
        )
        first = _tick()
        second = replace(
            first,
            tick_id=market_tick_id(
                target_type="chain_token",
                target_id=SECOND_TARGET_ID,
                source_provider="okx_dex_rest",
                observed_at_ms=NOW_MS,
            ),
            target_id=SECOND_TARGET_ID,
            token_address="0x2222222222222222222222222222222222222222",
        )
        with repos.transaction():
            repos.market_ticks.insert_ticks_returning_rows([first, second])

        first_page = repos.market_ticks.latest_target_ticks_after(after=None, limit=1)
        second_page = repos.market_ticks.latest_target_ticks_after(
            after=(str(first_page[-1]["target_type"]), str(first_page[-1]["target_id"])),
            limit=1,
        )
        exhausted = repos.market_ticks.latest_target_ticks_after(
            after=(str(second_page[-1]["target_type"]), str(second_page[-1]["target_id"])),
            limit=1,
        )
    finally:
        conn.close()

    assert [(row["target_type"], row["target_id"]) for row in first_page] == [("chain_token", TARGET_ID)]
    assert [(row["target_type"], row["target_id"]) for row in second_page] == [("chain_token", SECOND_TARGET_ID)]
    assert exhausted == []


def _tick() -> MarketTick:
    return MarketTick(
        tick_id=market_tick_id(
            target_type="chain_token",
            target_id=TARGET_ID,
            source_provider="okx_dex_rest",
            observed_at_ms=NOW_MS,
        ),
        target_type="chain_token",
        target_id=TARGET_ID,
        chain="eip155:1",
        token_address="0x1111111111111111111111111111111111111111",
        exchange=None,
        instrument=None,
        pricefeed_id=None,
        source_tier="tier2_poll",
        source_provider="okx_dex_rest",
        observed_at_ms=NOW_MS,
        received_at_ms=NOW_MS + 1,
        price_usd=Decimal("1.23"),
        liquidity_usd=Decimal("1000"),
        volume_24h_usd=Decimal("5000"),
        market_cap_usd=Decimal("100000"),
        holders=123,
        created_at_ms=NOW_MS + 2,
        raw_payload_json={"test": True},
    )
