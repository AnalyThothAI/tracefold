from __future__ import annotations

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.market.radar.reducer import enrich_token_radar, reduce_token_radar
from tracefold.market.radar.snapshot_repository import (
    TokenRadarCurrentRepository,
    served_token_radar_snapshot,
)

NOW_MS = 1_800_000_000_000
MINUTE_MS = 60_000


def test_singleton_publish_is_state_idempotent_and_failure_preserves_lkg(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = TokenRadarCurrentRepository(conn)
        reduced = _enriched(_eligible_rows())

        with conn.transaction():
            initial_failure_writes = repository.record_failure(
                error_code="token_radar_sample_budget_exceeded",
                evaluation_at_ms=NOW_MS - 1,
            )
        initial_failure = _stored(conn)

        with conn.transaction():
            first = repository.publish(reduced, evaluation_at_ms=NOW_MS)
        published = _stored(conn)

        irrelevant = _enriched(
            [
                *_eligible_rows(),
                {
                    **_eligible_rows()[0],
                    "target_id": "weak-target",
                    "event_id": "weak-event",
                    "received_at_ms": NOW_MS - 10 * MINUTE_MS,
                },
            ],
        )
        assert irrelevant.input_fingerprint != reduced.input_fingerprint
        assert irrelevant.state_fingerprint == reduced.state_fingerprint

        with conn.transaction():
            unchanged = repository.publish(irrelevant, evaluation_at_ms=NOW_MS + 1)
        after_unchanged = _stored(conn)

        with conn.transaction():
            failed_writes = repository.record_failure(
                error_code="token_radar_input_row_overflow",
                evaluation_at_ms=NOW_MS + 2,
            )
        failed = _stored(conn)

        with conn.transaction():
            recovered = repository.publish(irrelevant, evaluation_at_ms=NOW_MS + 3)
        recovered_row = _stored(conn)

    finally:
        conn.close()

    assert initial_failure_writes == 1
    assert initial_failure["latest_attempt_status"] == "failed"
    assert initial_failure["ruleset_version"] is None
    assert served_token_radar_snapshot(initial_failure) == {
        "schema_version": "token_radar_snapshot_v2",
        "evidence_as_of_ms": 0,
        "eligible_total": 0,
        "items": [],
    }
    assert first == {"status": "published", "rows_written": 1}
    assert unchanged == {"status": "unchanged", "rows_written": 0}
    assert after_unchanged == published
    assert failed_writes == 1
    assert failed["latest_attempt_status"] == "failed"
    assert served_token_radar_snapshot(failed) == served_token_radar_snapshot(published)
    assert recovered == {"status": "recovered", "rows_written": 1}
    assert recovered_row["latest_attempt_status"] == "ready"
    assert recovered_row["latest_error_code"] is None
    assert served_token_radar_snapshot(recovered_row) == served_token_radar_snapshot(published)
    assert recovered_row["ruleset_version"] == reduced.ruleset_version
    assert recovered_row["ruleset_fingerprint"] == reduced.ruleset_fingerprint


def test_selected_asset_presentation_facts_join_profile_and_product_market_key(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO registry_assets(
                  asset_id, chain_id, token_standard, address, status,
                  first_seen_at_ms, updated_at_ms
                )
                VALUES ('asset-1', 'solana', 'spl', 'mint-1', 'canonical', %s, %s)
                """,
                (NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )
            conn.execute(
                """
                INSERT INTO token_profile_current(
                  target_type, target_id, status, source_kind, name, logo_url,
                  quality_flags_json, source_payload_json, computed_at_ms,
                  updated_at_ms, payload_hash
                )
                VALUES (
                  'Asset', 'asset-1', 'ready', 'profile', 'Pepe', %s,
                  '[]'::jsonb, '{}'::jsonb, %s, %s, 'profile-hash'
                )
                """,
                (f"/api/token-images/{'a' * 64}", NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )
            conn.execute(
                """
                INSERT INTO market_ticks(
                  observed_at_ms, tick_id, target_type, target_id,
                  chain, token_address, source_tier, source_provider,
                  received_at_ms, price_usd, market_cap_usd,
                  raw_payload_json, payload_hash, created_at_ms
                )
                VALUES (
                  %s, 'tick-1', 'chain_token', 'solana:mint-1',
                  'solana', 'mint-1', 'tier3_inline', 'gmgn_dex_quote',
                  %s, 12, 12000000, '{}'::jsonb, 'tick-hash', %s
                )
                """,
                (NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )
            conn.execute(
                """
                INSERT INTO market_tick_current(
                  target_type, target_id, tick_observed_at_ms, tick_id,
                  source_tier, source_provider, chain, token_address,
                  price_usd, market_cap_usd, updated_at_ms, created_at_ms
                )
                VALUES (
                  'chain_token', 'solana:mint-1', %s, 'tick-1',
                  'tier3_inline', 'gmgn_dex_quote', 'solana', 'mint-1',
                  12, 12000000, %s, %s
                )
                """,
                (NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )

        repository = TokenRadarCurrentRepository(conn)
        assert repository.load_material_inputs(now_ms=NOW_MS) == []
        rows = repository.load_presentation_facts([("Asset", "asset-1"), ("Asset", "missing")])
    finally:
        conn.close()

    assert rows == [
        {
            "target_type": "Asset",
            "target_id": "asset-1",
            "name": "Pepe",
            "logo_url": f"/api/token-images/{'a' * 64}",
            "price_usd": 12,
            "market_cap_usd": 12_000_000,
            "observed_at_ms": NOW_MS - MINUTE_MS,
        },
        {
            "target_type": "Asset",
            "target_id": "missing",
            "name": None,
            "logo_url": None,
            "price_usd": None,
            "market_cap_usd": None,
            "observed_at_ms": None,
        },
    ]


def test_selected_cex_presentation_uses_the_supported_binance_usdt_swap_key(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO cex_tokens(
                  cex_token_id, base_symbol, status, evidence_level,
                  first_seen_at_ms, updated_at_ms
                ) VALUES ('cex-btc', 'BTC', 'canonical', 'direct', %s, %s)
                """,
                (NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )
            conn.execute(
                """
                INSERT INTO price_feeds(
                  pricefeed_id, feed_type, provider, subject_type, subject_id,
                  native_market_id, base_cex_token_id, base_symbol, quote_symbol,
                  status, evidence_level, first_seen_at_ms, updated_at_ms
                ) VALUES
                  ('feed-okx', 'cex_swap', 'okx', 'CexToken', 'cex-btc',
                   'BTC-USDT-SWAP', 'cex-btc', 'BTC', 'USDT',
                   'canonical', 'direct', %s, %s),
                  ('feed-binance', 'cex_swap', 'binance', 'CexToken', 'cex-btc',
                   'BTCUSDT', 'cex-btc', 'BTC', 'USDT',
                   'canonical', 'direct', %s, %s)
                """,
                (
                    NOW_MS - MINUTE_MS,
                    NOW_MS,
                    NOW_MS - 2 * MINUTE_MS,
                    NOW_MS - 2 * MINUTE_MS,
                ),
            )
            conn.execute(
                """
                INSERT INTO token_profile_current(
                  target_type, target_id, status, source_kind, name,
                  quality_flags_json, source_payload_json, computed_at_ms,
                  updated_at_ms, payload_hash
                ) VALUES (
                  'CexToken', 'cex-btc', 'ready', 'profile', 'Bitcoin',
                  '[]'::jsonb, '{}'::jsonb, %s, %s, 'profile-btc'
                )
                """,
                (NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )
            conn.execute(
                """
                INSERT INTO market_ticks(
                  observed_at_ms, tick_id, target_type, target_id,
                  exchange, instrument, pricefeed_id, source_tier, source_provider,
                  received_at_ms, price_usd, raw_payload_json, payload_hash,
                  created_at_ms
                ) VALUES (
                  %s, 'tick-btc', 'cex_symbol', 'binance:BTCUSDT',
                  'binance', 'BTCUSDT', 'feed-binance', 'tier2_poll',
                  'binance_cex_rest', %s, 70000, '{}'::jsonb, 'tick-btc-hash', %s
                )
                """,
                (NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )
            conn.execute(
                """
                INSERT INTO market_tick_current(
                  target_type, target_id, tick_observed_at_ms, tick_id,
                  source_tier, source_provider, exchange, instrument,
                  pricefeed_id, price_usd, updated_at_ms, created_at_ms
                ) VALUES (
                  'cex_symbol', 'binance:BTCUSDT', %s, 'tick-btc',
                  'tier2_poll', 'binance_cex_rest', 'binance', 'BTCUSDT',
                  'feed-binance', 70000, %s, %s
                )
                """,
                (NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )

        rows = TokenRadarCurrentRepository(conn).load_presentation_facts([("CexToken", "cex-btc")])
    finally:
        conn.close()

    assert rows == [
        {
            "target_type": "CexToken",
            "target_id": "cex-btc",
            "name": "Bitcoin",
            "logo_url": None,
            "price_usd": 70_000,
            "market_cap_usd": None,
            "observed_at_ms": NOW_MS - MINUTE_MS,
        }
    ]


def _stored(conn) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT ruleset_version, ruleset_fingerprint,
               input_fingerprint, state_fingerprint,
               evidence_as_of_ms, evaluation_at_ms,
               input_rows, input_bytes,
               latest_attempt_status, latest_error_code,
               failure_count, served_payload, updated_at_ms
          FROM token_radar_current
         WHERE singleton_key = true
        """
    ).fetchone()
    conn.commit()
    assert row is not None
    return dict(row)


def _eligible_rows() -> list[dict[str, object]]:
    return [
        {
            "target_type": "Asset",
            "target_id": "asset-1",
            "symbol": "PEPE",
            "chain": "solana",
            "exchange": None,
            "address": "mint-1",
            "resolution_status": "EXACT",
            "event_id": f"event-{index}",
            "received_at_ms": NOW_MS - minutes_ago * MINUTE_MS,
            "author_handle": f"author-{index}",
            "text": f"independent text {index}",
            "signal_price_usd": None,
        }
        for index, minutes_ago in enumerate((30, 20, 10))
    ]


def _enriched(rows: list[dict[str, object]]):
    return enrich_token_radar(
        reduce_token_radar(rows, now_ms=NOW_MS),
        [
            {
                "target_type": "Asset",
                "target_id": "asset-1",
                "name": "Pepe",
                "logo_url": f"/api/token-images/{'a' * 64}",
                "price_usd": "12",
                "market_cap_usd": "12000000",
                "observed_at_ms": NOW_MS - MINUTE_MS,
            }
        ],
        now_ms=NOW_MS,
    )
