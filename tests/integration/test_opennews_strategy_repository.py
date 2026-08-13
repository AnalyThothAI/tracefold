from __future__ import annotations

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tracefold.news.models import NewsFeedEntry
from tracefold.news.opennews import OpenNewsEvent
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import opennews_source


def test_same_provider_event_unions_strategy_provenance_and_replays_without_material_writes() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=900)

            first_forward = repository.record_opennews_events(
                source=opennews_source(),
                events=(_strategy_event("wire-forward", strategy_id="1018"),),
                observed_at_ms=1_000,
            )
            second_forward = repository.record_opennews_events(
                source=opennews_source(),
                events=(_strategy_event("wire-forward", strategy_id="1019"),),
                observed_at_ms=2_000,
            )
            first_reverse = repository.record_opennews_events(
                source=opennews_source(),
                events=(_strategy_event("wire-reverse", strategy_id="1019"),),
                observed_at_ms=1_000,
            )
            second_reverse = repository.record_opennews_events(
                source=opennews_source(),
                events=(_strategy_event("wire-reverse", strategy_id="1018"),),
                observed_at_ms=2_000,
            )

            before_replay = conn.execute(
                """
                SELECT provider_record_id, provider_metadata,
                       push_eligibility_updated_at_ms, updated_at_ms
                  FROM news_items
                 ORDER BY provider_record_id
                """
            ).fetchall()
            replay = repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _strategy_event("wire-forward", strategy_id="1018"),
                    _strategy_event("wire-forward", strategy_id="1019"),
                    _strategy_event("wire-reverse", strategy_id="1019"),
                    _strategy_event("wire-reverse", strategy_id="1018"),
                ),
                observed_at_ms=3_000,
            )
            after_replay = conn.execute(
                """
                SELECT provider_record_id, provider_metadata,
                       push_eligibility_updated_at_ms, updated_at_ms
                  FROM news_items
                 ORDER BY provider_record_id
                """
            ).fetchall()
            source_status = conn.execute(
                """
                SELECT last_accepted_strategy_trigger_at_ms,
                       observed_strategy_provenance,
                       last_items_seen, last_items_accepted
                  FROM news_sources
                 WHERE source_id = 'news-opennews'
                """
            ).fetchone()
    finally:
        conn.close()

    assert first_forward == _write_outcome(inserted=1, updated=0)
    assert second_forward == _write_outcome(inserted=0, updated=1)
    assert first_reverse == _write_outcome(inserted=1, updated=0)
    assert second_reverse == _write_outcome(inserted=0, updated=1)
    assert replay == _write_outcome(inserted=0, updated=0, rejected=4)
    assert after_replay == before_replay
    expected_provenance = [
        {
            "id": "1018",
            "name": "News Score > 70",
            "source_type": "news",
            "engine_type": "news",
        },
        {
            "id": "1019",
            "name": "OI Event Monitor",
            "source_type": "market",
            "engine_type": "market",
        },
    ]
    assert before_replay == [
        {
            "provider_record_id": "wire-forward",
            "provider_metadata": {
                "coins": [{"market_type": "cex", "symbol": "BTC"}],
                "score": 85,
                "source": "binance",
                "strategies": expected_provenance,
            },
            "push_eligibility_updated_at_ms": 2_000,
            "updated_at_ms": 2_000,
        },
        {
            "provider_record_id": "wire-reverse",
            "provider_metadata": {
                "coins": [{"market_type": "cex", "symbol": "BTC"}],
                "score": 85,
                "source": "binance",
                "strategies": expected_provenance,
            },
            "push_eligibility_updated_at_ms": 2_000,
            "updated_at_ms": 2_000,
        },
    ]
    assert source_status == {
        "last_accepted_strategy_trigger_at_ms": 3_000,
        "observed_strategy_provenance": expected_provenance,
        "last_items_seen": 4,
        "last_items_accepted": 0,
    }


def test_same_provider_event_uses_an_order_independent_payload_winner() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=900)
            repository.record_opennews_events(
                source=source,
                events=(
                    _strategy_event(
                        "payload-forward",
                        strategy_id="1018",
                        score=71,
                        provider_source="alpha",
                        title="Alpha wrapper payload",
                    ),
                ),
                observed_at_ms=1_000,
            )
            repository.record_opennews_events(
                source=source,
                events=(
                    _strategy_event(
                        "payload-forward",
                        strategy_id="1019",
                        score=89,
                        provider_source="omega",
                        title="Omega wrapper payload",
                    ),
                ),
                observed_at_ms=2_000,
            )
            repository.record_opennews_events(
                source=source,
                events=(
                    _strategy_event(
                        "payload-reverse",
                        strategy_id="1019",
                        score=89,
                        provider_source="omega",
                        title="Omega wrapper payload",
                    ),
                ),
                observed_at_ms=1_000,
            )
            repository.record_opennews_events(
                source=source,
                events=(
                    _strategy_event(
                        "payload-reverse",
                        strategy_id="1018",
                        score=71,
                        provider_source="alpha",
                        title="Alpha wrapper payload",
                    ),
                ),
                observed_at_ms=2_000,
            )

        rows = conn.execute(
            """
            SELECT provider_metadata, canonical_url, reporting_origin,
                   title, description, lang, published_at_ms,
                   first_observed_at_ms, last_observed_at_ms,
                   provider_score_updated_at_ms,
                   push_eligibility_updated_at_ms,
                   content_fingerprint, active, updated_at_ms
              FROM news_items
             WHERE provider_record_id IN ('payload-forward', 'payload-reverse')
             ORDER BY provider_record_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    assert rows[0] == rows[1]
    assert rows[0]["provider_metadata"]["strategies"] == [
        {
            "id": "1018",
            "name": "News Score > 70",
            "source_type": "news",
            "engine_type": "news",
        },
        {
            "id": "1019",
            "name": "OI Event Monitor",
            "source_type": "market",
            "engine_type": "market",
        },
    ]


def test_first_failed_connection_does_not_invent_strategy_coverage_or_a_gap() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=900)
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=False,
                now_ms=1_000,
                error_code="opennews_connect_failed",
            )
        row = conn.execute(
            """
            SELECT strategy_coverage_started_at_ms,
                   coverage_unknown_since_at_ms,
                   last_disconnected_at_ms, last_error
              FROM news_sources
             WHERE source_id = %s
            """,
            (source.source_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row == {
        "strategy_coverage_started_at_ms": None,
        "coverage_unknown_since_at_ms": None,
        "last_disconnected_at_ms": 1_000,
        "last_error": "opennews_connect_failed",
    }


def test_first_strategy_observation_replaces_an_inactive_legacy_payload() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=900)
            conn.execute(
                """
                INSERT INTO news_items(
                  item_id, source_id, source_item_key, provider_record_id,
                  provider_metadata, provider_score_updated_at_ms,
                  push_eligibility_updated_at_ms,
                  canonical_url, reporting_origin,
                  title, description, lang, published_at_ms,
                  first_observed_at_ms, last_observed_at_ms,
                  content_fingerprint, importance_factors,
                  active, created_at_ms, updated_at_ms
                ) VALUES (
                  'inactive-legacy', 'news-opennews', 'legacy-reused-id',
                  'legacy-reused-id',
                  '{"score":99,"source":"legacy-full-corpus"}'::jsonb,
                  100, 100,
                  'https://legacy.invalid/item', 'legacy-full-corpus',
                  'Legacy broad-corpus payload', '', 'en',
                  100, 100, 100, 'legacy-inactive-fingerprint',
                  '{}'::jsonb, false, 100, 100
                )
                """
            )
            outcome = repository.record_opennews_events(
                source=source,
                events=(
                    _strategy_event(
                        "legacy-reused-id",
                        strategy_id="1018",
                        score=75,
                        provider_source="strategy-source",
                        title="Strategy-qualified replacement",
                    ),
                ),
                observed_at_ms=1_000,
            )
        row = conn.execute(
            """
            SELECT active, provider_metadata, title, reporting_origin,
                   canonical_url, provider_score_updated_at_ms,
                   push_eligibility_updated_at_ms
              FROM news_items
             WHERE provider_record_id = 'legacy-reused-id'
            """
        ).fetchone()
    finally:
        conn.close()

    assert outcome == _write_outcome(inserted=0, updated=1)
    assert row["active"] is True
    assert row["title"] == "Strategy-qualified replacement"
    assert row["reporting_origin"] == "strategy-source"
    assert row["canonical_url"] is None
    assert row["provider_score_updated_at_ms"] == 1_000
    assert row["push_eligibility_updated_at_ms"] == 1_000
    assert row["provider_metadata"] == {
        "coins": [{"market_type": "cex", "symbol": "BTC"}],
        "score": 75,
        "source": "strategy-source",
        "strategies": [
            {
                "id": "1018",
                "name": "News Score > 70",
                "source_type": "news",
                "engine_type": "news",
            }
        ],
    }


def test_opennews_transport_gap_is_durable_across_reconnect_and_overflow() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=900)
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=1_000,
                error_code=None,
            )
            repository.record_opennews_events(
                source=source,
                events=(_strategy_event("wire-gap", strategy_id="1018"),),
                observed_at_ms=1_050,
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=False,
                now_ms=1_100,
                error_code="opennews_receive_failed",
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=1_200,
                error_code=None,
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=False,
                now_ms=1_300,
                error_code="opennews_queue_overflow",
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=1_400,
                error_code=None,
            )

        row = conn.execute(
            """
            SELECT live_connected, last_connected_at_ms,
                   last_disconnected_at_ms, last_overflow_at_ms,
                   strategy_coverage_started_at_ms,
                   coverage_unknown_since_at_ms,
                   last_accepted_strategy_trigger_at_ms,
                   last_error, last_outcome
              FROM news_sources
             WHERE source_id = %s
            """,
            (source.source_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row == {
        "live_connected": True,
        "last_connected_at_ms": 1_400,
        "last_disconnected_at_ms": 1_300,
        "last_overflow_at_ms": 1_300,
        "strategy_coverage_started_at_ms": 1_000,
        "coverage_unknown_since_at_ms": 1_100,
        "last_accepted_strategy_trigger_at_ms": 1_050,
        "last_error": None,
        "last_outcome": "strategy_connected",
    }


def test_source_reconcile_marks_a_stale_live_connection_as_an_unclean_gap() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=900)
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=1_000,
                error_code=None,
            )
            repository.record_opennews_events(
                source=source,
                events=(_strategy_event("wire-before-outage", strategy_id="1018"),),
                observed_at_ms=1_100,
            )

        with conn.transaction():
            writes = NewsRepository(conn).sync_sources((source,), now_ms=1_500)

        row = conn.execute(
            """
            SELECT live_connected, last_disconnected_at_ms,
                   coverage_unknown_since_at_ms, last_error, last_outcome
              FROM news_sources
             WHERE source_id = %s
            """,
            (source.source_id,),
        ).fetchone()
    finally:
        conn.close()

    assert writes == 1
    assert row == {
        "live_connected": False,
        "last_disconnected_at_ms": 1_500,
        "coverage_unknown_since_at_ms": 1_000,
        "last_error": "opennews_process_outage",
        "last_outcome": "strategy_process_outage",
    }


def test_strategy_health_exposes_counts_and_no_replay_without_provenance_values() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=900)
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=1_000,
                error_code=None,
            )

        warming = repository.health_snapshot(
            now_ms=1_001,
            rss_enabled=False,
            configured_strategy_count=2,
        )

        with conn.transaction():
            repository.record_opennews_events(
                source=source,
                events=(_strategy_event("wire-health", strategy_id="1018"),),
                observed_at_ms=1_100,
            )
        ready_ingest = repository.health_snapshot(
            now_ms=1_101,
            rss_enabled=False,
            configured_strategy_count=2,
        )

        with conn.transaction():
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=False,
                now_ms=1_200,
                error_code="opennews_receive_failed",
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=1_300,
                error_code=None,
            )
        unknown = repository.health_snapshot(
            now_ms=1_301,
            rss_enabled=False,
            configured_strategy_count=2,
        )
        source_payload = repository.list_sources()["items"][0]
    finally:
        conn.close()

    warming_opennews = warming["layers"]["ingest"]["opennews"]
    assert warming["layers"]["ingest"]["status"] == "warming"
    assert "opennews_strategy_no_trigger_yet" in warming["layers"]["ingest"]["reasons"]
    assert warming["operating_state"] == "warming"
    assert warming_opennews["configured_strategy_count"] == 2
    assert warming_opennews["observed_strategy_count"] == 0
    assert warming_opennews["replay_supported"] is False

    assert ready_ingest["layers"]["ingest"]["status"] == "ready"
    assert ready_ingest["layers"]["ingest"]["opennews"]["observed_strategy_count"] == 1
    assert unknown["layers"]["ingest"]["status"] == "degraded"
    assert "opennews_strategy_coverage_unknown" in unknown["layers"]["ingest"]["reasons"]
    assert unknown["layers"]["ingest"]["opennews"]["live_connected"] is True
    assert unknown["layers"]["ingest"]["opennews"]["last_error"] is None
    assert unknown["layers"]["ingest"]["opennews"]["coverage_unknown_since_at_ms"] == 1_200

    assert source_payload["replay_supported"] is False
    assert source_payload["observed_strategy_count"] == 1
    assert "observed_strategy_provenance" not in source_payload


def _strategy_event(
    provider_record_id: str,
    *,
    strategy_id: str,
    score: int = 85,
    provider_source: str = "binance",
    title: str = "BTC market event",
) -> OpenNewsEvent:
    strategy = (
        {
            "id": "1018",
            "name": "News Score > 70",
            "source_type": "news",
            "engine_type": "news",
        }
        if strategy_id == "1018"
        else {
            "id": "1019",
            "name": "OI Event Monitor",
            "source_type": "market",
            "engine_type": "market",
        }
    )
    return OpenNewsEvent(
        provider_record_id=provider_record_id,
        observation_kind="report",
        provider_metadata={
            "score": score,
            "source": provider_source,
            "coins": [{"symbol": "BTC", "market_type": "cex"}],
            "strategies": [strategy],
        },
        entry=NewsFeedEntry(
            guid=provider_record_id,
            link=None,
            title=title,
            description="",
            published_at_ms=900,
            reporting_origin=provider_source,
        ),
    )


def _write_outcome(*, inserted: int, updated: int, rejected: int = 0) -> dict[str, int]:
    return {
        "events_seen": inserted + updated + rejected,
        "items_inserted": inserted,
        "items_updated": updated,
        "metadata_updated": 0,
        "rejected": rejected,
    }
