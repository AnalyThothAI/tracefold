from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tests.factories import make_event
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import EnrichedEventCapture, MarketTick, event_to_row, market_tick_id
from tracefold.market.radar.reducer import (
    RadarEvidenceRevision,
    RadarSelectionKey,
    enrich_token_radar,
    reduce_token_radar,
    token_radar_text_fingerprint,
)
from tracefold.market.radar.snapshot_repository import TokenRadarCurrentRepository

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
                replace(
                    _eligible_rows()[0],
                    target_id="weak-target",
                    event_id="weak-event",
                    received_at_ms=NOW_MS - 10 * MINUTE_MS,
                ),
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
            duplicate_failure_writes = repository.record_failure(
                error_code="token_radar_reducer_budget_exceeded",
                evaluation_at_ms=NOW_MS + 2,
            )

        with conn.transaction():
            recovered = repository.publish(irrelevant, evaluation_at_ms=NOW_MS + 3)
        recovered_row = _stored(conn)

    finally:
        conn.close()

    assert initial_failure_writes == 1
    assert initial_failure["latest_attempt_status"] == "failed"
    assert initial_failure["ruleset_version"] is None
    assert _served_payload(initial_failure) == {
        "schema_version": "token_radar_snapshot_v4",
        "social_evidence_as_of_ms": 0,
        "eligible_total": 0,
        "items": [],
    }
    assert initial_failure["state_changed_at_ms"] == 0
    assert first == {"status": "published", "rows_written": 1}
    assert published["state_changed_at_ms"] == NOW_MS
    assert unchanged == {"status": "unchanged", "rows_written": 0}
    assert after_unchanged == published
    assert failed_writes == 1
    assert duplicate_failure_writes == 0
    assert failed["latest_attempt_status"] == "failed"
    assert failed["state_changed_at_ms"] == NOW_MS + 2
    assert _served_payload(failed) == _served_payload(published)
    assert recovered == {"status": "recovered", "rows_written": 1}
    assert recovered_row["latest_attempt_status"] == "ready"
    assert recovered_row["latest_error_code"] is None
    assert recovered_row["state_changed_at_ms"] == NOW_MS + 3
    assert _served_payload(recovered_row) == _served_payload(published)
    assert recovered_row["ruleset_version"] == reduced.ruleset_version
    assert recovered_row["ruleset_fingerprint"] == reduced.ruleset_fingerprint


def test_semantic_fingerprint_change_updates_internal_identity_without_changing_public_state(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = TokenRadarCurrentRepository(conn)
        reduced = _enriched(_eligible_rows())
        with conn.transaction():
            repository.publish(reduced, evaluation_at_ms=NOW_MS)
        before = _stored(conn)

        changed_semantics = replace(
            reduced,
            ruleset_fingerprint=f"sha256:{'0' * 64}",
        )
        with conn.transaction():
            result = repository.publish(changed_semantics, evaluation_at_ms=NOW_MS + 1)
        after = _stored(conn)
    finally:
        conn.close()

    assert result == {"status": "published", "rows_written": 1}
    assert after["ruleset_fingerprint"] == changed_semantics.ruleset_fingerprint
    assert after["state_fingerprint"] == before["state_fingerprint"]
    assert after["state_changed_at_ms"] == before["state_changed_at_ms"]
    assert _served_payload(after) == _served_payload(before)


def test_persistence_seam_replays_resolution_history_and_uses_only_selected_trigger_anchor(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repos = repositories_for_connection(conn)
        source_times = (NOW_MS - 30 * MINUTE_MS, NOW_MS - 20 * MINUTE_MS, NOW_MS - 10 * MINUTE_MS)
        with repos.transaction():
            selected_asset = repos.registry.upsert_chain_asset(
                chain_id="solana",
                address="radar-selected-mint",
                observed_at_ms=source_times[0],
            )
            superseded_asset = repos.registry.upsert_chain_asset(
                chain_id="solana",
                address="radar-superseded-mint",
                observed_at_ms=source_times[0],
            )
            selected_resolution_ids: list[str] = []
            superseded_resolution = None
            for index, source_at_ms in enumerate(source_times, start=1):
                event_id = f"radar-history-event-{index}"
                intent_id = f"radar-history-intent-{index}"
                event_text = (
                    "$RADAR Straße independent evidence 1" if index == 1 else f"$RADAR independent evidence {index}"
                )
                event = make_event(
                    event_id,
                    author_handle=f"radar-author-{index}",
                    text=event_text,
                    received_at_ms=source_at_ms,
                )
                event_row = event_to_row(event, now_ms=source_at_ms + 1_000)
                if index == 1:
                    event_row["text_clean"] = "$RADAR\tStraße\nindependent\revidence\f1"
                assert repos.evidence.insert_event_row(event_row)
                repos.token_intents.insert(
                    {
                        "intent_id": intent_id,
                        "event_id": event_id,
                        "intent_key": intent_id,
                        "construction_policy": "integration_fixture",
                        "primary_evidence_id": None,
                        "display_symbol": "RADAR",
                        "display_name": "Radar",
                        "chain_hint": None,
                        "address_hint": None,
                        "intent_status": "resolved",
                        "intent_confidence": 1.0,
                        "created_at_ms": source_at_ms + 1_000,
                        "updated_at_ms": source_at_ms + 1_000,
                    }
                )
                if index == 3:
                    superseded_resolution = repos.intent_resolutions.insert_resolution(
                        _resolution_input(
                            event_id=event_id,
                            intent_id=intent_id,
                            target_id=str(superseded_asset["asset_id"]),
                            at_ms=source_at_ms + 2_000,
                        )
                    )
                selected_resolution = repos.intent_resolutions.insert_resolution(
                    _resolution_input(
                        event_id=event_id,
                        intent_id=intent_id,
                        target_id=str(selected_asset["asset_id"]),
                        at_ms=source_at_ms + (3_000 if index == 3 else 2_000),
                    )
                )
                selected_resolution_ids.append(str(selected_resolution["resolution_id"]))

            assert superseded_resolution is not None
            superseded_tick = _asset_tick(
                address="radar-superseded-mint",
                observed_at_ms=source_times[-1],
                price_usd="99",
                market_cap_usd="99000000",
            )
            repos.market_ticks.insert_ticks_returning_rows([superseded_tick])
            repos.enriched_events.insert_capture(
                EnrichedEventCapture(
                    event_id="radar-history-event-3",
                    intent_id="radar-history-intent-3",
                    resolution_id=str(superseded_resolution["resolution_id"]),
                    target_type="chain_token",
                    target_id="solana:radar-superseded-mint",
                    t_event_ms=source_times[-1],
                    tick_observed_at_ms=superseded_tick.observed_at_ms,
                    tick_id=superseded_tick.tick_id,
                    tick_lag_ms=0,
                    capture_method="tier3_inline",
                    capture_reason="integration_fixture",
                    created_at_ms=source_times[-1] + 2_500,
                )
            )
            current_tick = _asset_tick(
                address="radar-selected-mint",
                observed_at_ms=NOW_MS - MINUTE_MS,
                price_usd="12",
                market_cap_usd="12000000",
            )
            [current_tick_row] = repos.market_ticks.insert_ticks_returning_rows([current_tick])
            assert repos.market_tick_current.upsert_current_from_tick(current_tick_row)

        with repos.transaction():
            material_inputs = repos.token_radar_current.load_material_inputs(now_ms=NOW_MS)
        reduced = reduce_token_radar(material_inputs, now_ms=NOW_MS)
        presentation = repos.token_radar_current.load_presentation_facts(
            list(reduced.selected_keys),
            now_ms=NOW_MS,
        )
        enriched = enrich_token_radar(reduced, presentation, now_ms=NOW_MS)

        third_event_history = [revision for revision in material_inputs if revision.event_id == "radar-history-event-3"]
        assert material_inputs[0].text_fingerprint == token_radar_text_fingerprint(
            "$RADAR\tStraße\nindependent\revidence\f1"
        )
        assert [(revision.resolution_id, revision.target_id) for revision in third_event_history] == [
            (superseded_resolution["resolution_id"], superseded_asset["asset_id"]),
            (selected_resolution_ids[-1], selected_asset["asset_id"]),
        ]
        assert reduced.selected_keys == (
            RadarSelectionKey(
                target_type="Asset",
                target_id=str(selected_asset["asset_id"]),
                trigger_event_id="radar-history-event-3",
                trigger_intent_id="radar-history-intent-3",
                trigger_resolution_id=selected_resolution_ids[-1],
            ),
        )
        assert presentation[0]["signal_price_usd"] is None
        assert presentation[0]["price_usd"] == 12
        assert enriched.snapshot["items"][0]["market"]["price_change_since_signal"] is None
    finally:
        conn.close()


def test_selected_asset_presentation_keeps_fresh_cap_when_newer_price_tick_is_sparse(tmp_path) -> None:
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
                (NOW_MS - 2 * MINUTE_MS, NOW_MS - 2 * MINUTE_MS, NOW_MS - 2 * MINUTE_MS),
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
                  %s, 'tick-2', 'chain_token', 'solana:mint-1',
                  'solana', 'mint-1', 'tier2_poll', 'okx_dex_rest',
                  %s, 13, NULL, '{}'::jsonb, 'tick-2-hash', %s
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
                  'chain_token', 'solana:mint-1', %s, 'tick-2',
                  'tier2_poll', 'okx_dex_rest', 'solana', 'mint-1',
                  13, NULL, %s, %s
                )
                """,
                (NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS, NOW_MS - MINUTE_MS),
            )

        repository = TokenRadarCurrentRepository(conn)
        with conn.transaction():
            assert repository.load_material_inputs(now_ms=NOW_MS) == []
        rows = repository.load_presentation_facts(
            [
                RadarSelectionKey("Asset", "asset-1", "event-1", "intent-1", "resolution-1"),
                RadarSelectionKey("Asset", "missing", "event-2", "intent-2", "resolution-2"),
            ],
            now_ms=NOW_MS,
        )
        stale_rows = repository.load_presentation_facts(
            [RadarSelectionKey("Asset", "asset-1", "event-1", "intent-1", "resolution-1")],
            now_ms=NOW_MS + 4 * MINUTE_MS + 1,
        )
    finally:
        conn.close()

    assert rows == [
        {
            "target_type": "Asset",
            "target_id": "asset-1",
            "symbol": None,
            "chain": "solana",
            "exchange": None,
            "address": "mint-1",
            "name": "Pepe",
            "logo_url": f"/api/token-images/{'a' * 64}",
            "signal_price_usd": None,
            "price_usd": 13,
            "price_observed_at_ms": NOW_MS - MINUTE_MS,
            "market_cap_usd": 12_000_000,
            "market_cap_observed_at_ms": NOW_MS - 2 * MINUTE_MS,
        },
        {
            "target_type": "Asset",
            "target_id": "missing",
            "symbol": None,
            "chain": None,
            "exchange": None,
            "address": None,
            "name": None,
            "logo_url": None,
            "signal_price_usd": None,
            "price_usd": None,
            "price_observed_at_ms": None,
            "market_cap_usd": None,
            "market_cap_observed_at_ms": None,
        },
    ]
    assert stale_rows[0]["market_cap_usd"] is None
    assert stale_rows[0]["market_cap_observed_at_ms"] is None


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

        rows = TokenRadarCurrentRepository(conn).load_presentation_facts(
            [RadarSelectionKey("CexToken", "cex-btc", "event-1", "intent-1", "resolution-1")],
            now_ms=NOW_MS,
        )
    finally:
        conn.close()

    assert rows == [
        {
            "target_type": "CexToken",
            "target_id": "cex-btc",
            "symbol": "BTC",
            "chain": None,
            "exchange": "binance",
            "address": None,
            "name": "Bitcoin",
            "logo_url": None,
            "signal_price_usd": None,
            "price_usd": 70_000,
            "price_observed_at_ms": NOW_MS - MINUTE_MS,
            "market_cap_usd": None,
            "market_cap_observed_at_ms": None,
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
               failure_count, state_changed_at_ms, served_payload, updated_at_ms
          FROM token_radar_current
         WHERE singleton_key = true
        """
    ).fetchone()
    conn.commit()
    assert row is not None
    return dict(row)


def _served_payload(row: dict[str, object]) -> dict[str, object]:
    payload = row.get("served_payload")
    assert isinstance(payload, dict)
    return payload


def _eligible_rows() -> list[RadarEvidenceRevision]:
    rows = []
    for index, minutes_ago in enumerate((30, 20, 10)):
        source_event_at_ms = NOW_MS - minutes_ago * MINUTE_MS
        rows.append(
            RadarEvidenceRevision(
                intent_id=f"intent-{index}",
                resolution_id=f"resolution-{index}",
                target_type="Asset",
                target_id="asset-1",
                resolution_status="EXACT",
                event_id=f"event-{index}",
                source_event_at_ms=source_event_at_ms,
                received_at_ms=source_event_at_ms + 1_000,
                event_created_at_ms=source_event_at_ms + 2_000,
                action="tweet",
                author_key=f"author-{index}",
                text_fingerprint=token_radar_text_fingerprint(f"independent text {index}"),
                resolution_decision_at_ms=source_event_at_ms + 2_000,
                resolution_created_at_ms=source_event_at_ms + 3_000,
            )
        )
    return rows


def _enriched(rows: list[RadarEvidenceRevision]):
    return enrich_token_radar(
        reduce_token_radar(rows, now_ms=NOW_MS),
        [
            {
                "target_type": "Asset",
                "target_id": "asset-1",
                "symbol": "PEPE",
                "chain": "solana",
                "exchange": None,
                "address": "mint-1",
                "name": "Pepe",
                "logo_url": f"/api/token-images/{'a' * 64}",
                "signal_price_usd": "10",
                "price_usd": "12",
                "price_observed_at_ms": NOW_MS - MINUTE_MS,
                "market_cap_usd": "12000000",
                "market_cap_observed_at_ms": NOW_MS - MINUTE_MS,
            }
        ],
        now_ms=NOW_MS,
    )


def _resolution_input(*, event_id: str, intent_id: str, target_id: str, at_ms: int) -> dict[str, object]:
    return {
        "intent_id": intent_id,
        "event_id": event_id,
        "resolution_status": "EXACT",
        "resolver_policy_version": "integration_fixture",
        "target_type": "Asset",
        "target_id": target_id,
        "pricefeed_id": None,
        "reason_codes": [],
        "candidate_ids": [],
        "lookup_keys": [],
        "decision_time_ms": at_ms,
        "created_at_ms": at_ms,
    }


def _asset_tick(
    *,
    address: str,
    observed_at_ms: int,
    price_usd: str,
    market_cap_usd: str,
) -> MarketTick:
    target_id = f"solana:{address}"
    return MarketTick(
        tick_id=market_tick_id(
            target_type="chain_token",
            target_id=target_id,
            source_provider="gmgn_dex_quote",
            observed_at_ms=observed_at_ms,
        ),
        target_type="chain_token",
        target_id=target_id,
        chain="solana",
        token_address=address,
        exchange=None,
        instrument=None,
        pricefeed_id=None,
        source_tier="tier3_inline",
        source_provider="gmgn_dex_quote",
        observed_at_ms=observed_at_ms,
        received_at_ms=observed_at_ms,
        price_usd=Decimal(price_usd),
        liquidity_usd=None,
        volume_24h_usd=None,
        market_cap_usd=Decimal(market_cap_usd),
        holders=None,
        created_at_ms=observed_at_ms,
    )
