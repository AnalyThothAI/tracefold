from __future__ import annotations

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tracefold.news.models import NewsFeedEntry
from tracefold.news.opennews import OpenNewsEvent
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import opennews_source

BASE_MS = 1_785_560_400_000


class _CountingConnection:
    def __init__(self, conn) -> None:
        self._conn = conn
        self.execute_count = 0

    def execute(self, sql: str, params=None):
        self.execute_count += 1
        return self._conn.execute(sql, params)


def test_strategy_batch_publication_uses_constant_database_statements() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        source = opennews_source()
        with conn.transaction():
            NewsRepository(conn).sync_sources((source,), now_ms=BASE_MS)
        counting = _CountingConnection(conn)
        with conn.transaction():
            outcome = NewsRepository(counting).record_opennews_events(
                source=source,
                events=tuple(_event(f"batch-{index:03d}", "1018") for index in range(100)),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
    finally:
        conn.close()

    assert outcome["items_inserted"] == 100
    assert counting.execute_count <= 5


def test_live_history_overlap_unions_strategies_and_preserves_first_ingest_mode() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=BASE_MS)
            first = repository.record_opennews_events(
                source=source,
                events=(_event("overlap", "1019"),),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="recovery",
            )
            second = repository.record_opennews_events(
                source=source,
                events=(_event("overlap", "1018"),),
                observed_at_ms=BASE_MS + 2_000,
                ingest_mode="live",
            )
            replay = repository.record_opennews_events(
                source=source,
                events=(_event("overlap", "1019"), _event("overlap", "1018")),
                observed_at_ms=BASE_MS + 3_000,
                ingest_mode="recovery",
            )
            row = conn.execute(
                """
                SELECT first_ingest_mode, provider_metadata, updated_at_ms
                  FROM news_items WHERE provider_record_id = 'overlap'
                """
            ).fetchone()
    finally:
        conn.close()

    assert first["items_inserted"] == 1
    assert second["items_updated"] == 1
    assert replay["items_inserted"] == replay["items_updated"] == 0
    assert row["first_ingest_mode"] == "recovery"
    assert row["provider_metadata"]["strategies"] == [
        {"id": "1018", "name": "Strategy 1018"},
        {"id": "1019", "name": "Strategy 1019"},
    ]
    assert row["updated_at_ms"] == BASE_MS + 2_000


def test_reconnect_is_currently_green_even_when_an_incident_is_unresolved() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=BASE_MS)
            repository.record_opennews_events(
                source=source,
                events=(_event("health", "1018"),),
                observed_at_ms=BASE_MS + 100,
                ingest_mode="live",
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=False,
                now_ms=BASE_MS + 200,
                error_code="opennews_receive_failed",
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=BASE_MS + 300,
                error_code=None,
            )
            health = repository.health_snapshot(
                now_ms=BASE_MS + 400,
                rss_enabled=False,
                configured_strategy_count=2,
            )
    finally:
        conn.close()

    assert health["realtime"]["wss_state"] == "connected"
    assert health["layers"]["ingest"]["status"] == "ready"
    opennews = health["layers"]["ingest"]["opennews"]
    assert opennews["unresolved_incident_count"] == 1
    assert opennews["incidents"][0]["cause_class"] == "provider_close"
    assert "opennews_strategy_coverage_unknown" not in health["reasons"]


def test_overflow_incident_does_not_change_connected_wss_state() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.update_opennews_live_status(
                source_id="news-opennews",
                connected=True,
                now_ms=BASE_MS + 100,
                error_code="opennews_buffer_overflow",
                coverage_gap=True,
            )
            source = conn.execute(
                "SELECT live_connected, last_error FROM news_sources WHERE source_id = 'news-opennews'"
            ).fetchone()
            incident = conn.execute("SELECT cause_class, recovery_status FROM news_opennews_incidents").fetchone()
    finally:
        conn.close()

    assert dict(source) == {"live_connected": True, "last_error": None}
    assert dict(incident) == {"cause_class": "buffer_overflow", "recovery_status": "pending"}


def _event(provider_record_id: str, strategy_id: str) -> OpenNewsEvent:
    return OpenNewsEvent(
        provider_record_id=provider_record_id,
        observation_kind="report",
        provider_metadata={
            "strategies": [{"id": strategy_id, "name": f"Strategy {strategy_id}"}],
        },
        entry=NewsFeedEntry(
            guid=provider_record_id,
            link=f"https://example.com/{provider_record_id}",
            title=f"OpenNews strategy event {provider_record_id}",
            description="",
            published_at_ms=BASE_MS,
            reporting_origin="opennews",
        ),
    )
