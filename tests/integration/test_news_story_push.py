from __future__ import annotations

from typing import Any

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tests.support.news import rebuild_news_projection
from tracefold.news.models import NewsFeedEntry
from tracefold.news.opennews import OpenNewsEvent
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import opennews_source

BASE_MS = 1_785_560_400_000


def test_live_scoreless_assetless_strategy_story_creates_one_same_commit_outbox() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            rebuild_news_projection(repository, now_ms=BASE_MS, push_enabled=True)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("live-1019", strategy_id="1019", score=None),),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            outcome = rebuild_news_projection(
                repository,
                now_ms=BASE_MS + 2_000,
                push_enabled=True,
            )
            delivery = conn.execute(
                """
                SELECT status, source_payload, live_observed_at_ms,
                       selected_item_id,
                       (SELECT representative_item_id FROM news_stories LIMIT 1)
                         AS representative_item_id
                  FROM news_push_deliveries
                """
            ).fetchone()
    finally:
        conn.close()

    assert outcome["push_outbox_writes"] == 1
    assert delivery is not None
    assert delivery["status"] == "pending_translation"
    assert delivery["live_observed_at_ms"] == BASE_MS + 1_000
    assert delivery["selected_item_id"] == delivery["representative_item_id"]
    assert delivery["source_payload"]["provider_evidence"]["item_id"] == delivery["selected_item_id"]
    metadata = delivery["source_payload"]["provider_evidence"]["provider_metadata"]
    assert metadata["strategies"] == [{"id": "1019", "name": "Strategy 1019"}]
    assert "score" not in metadata
    assert "coins" not in metadata


def test_same_event_under_both_strategies_creates_one_story_and_one_push() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            rebuild_news_projection(repository, now_ms=BASE_MS, push_enabled=True)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event("both-strategies", strategy_id="1018", score=70),
                    _event("both-strategies", strategy_id="1019", score=None),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2_000, push_enabled=True)
            counts = conn.execute(
                """
                SELECT (SELECT count(*) FROM news_items) AS items,
                       (SELECT count(*) FROM news_stories) AS stories,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes
                """
            ).fetchone()
            payload = conn.execute("SELECT source_payload FROM news_push_deliveries").fetchone()["source_payload"]
    finally:
        conn.close()

    assert dict(counts) == {"items": 1, "stories": 1, "pushes": 1}
    assert payload["provider_evidence"]["provider_metadata"]["strategies"] == [
        {"id": "1018", "name": "Strategy 1018"},
        {"id": "1019", "name": "Strategy 1019"},
    ]


def test_recovery_only_story_and_later_live_replay_never_create_push() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            rebuild_news_projection(repository, now_ms=BASE_MS, push_enabled=True)
            event = _event("recovery-only", strategy_id="1018", score=95)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(event,),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="recovery",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2_000, push_enabled=True)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(event,),
                observed_at_ms=BASE_MS + 3_000,
                ingest_mode="live",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 4_000, push_enabled=True)
            row = conn.execute(
                """
                SELECT first_ingest_mode,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes
                  FROM news_items
                 WHERE provider_record_id = 'recovery-only'
                """
            ).fetchone()
    finally:
        conn.close()

    assert dict(row) == {"first_ingest_mode": "recovery", "pushes": 0}


def test_disable_reenable_epoch_does_not_backfill_disabled_interval() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            rebuild_news_projection(repository, now_ms=BASE_MS, push_enabled=True)
            rebuild_news_projection(repository, now_ms=BASE_MS + 1_000, push_enabled=False)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("disabled-window", strategy_id="1018", score=99),),
                observed_at_ms=BASE_MS + 2_000,
                ingest_mode="live",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2_500, push_enabled=False)
            rebuild_news_projection(repository, now_ms=BASE_MS + 3_000, push_enabled=True)
            state = conn.execute(
                """
                SELECT enabled, enablement_epoch_at_ms,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes
                  FROM news_push_state WHERE singleton_key = 'current'
                """
            ).fetchone()
    finally:
        conn.close()

    assert dict(state) == {
        "enabled": True,
        "enablement_epoch_at_ms": BASE_MS + 3_000,
        "pushes": 0,
    }


def _event(
    provider_record_id: str,
    *,
    strategy_id: str,
    score: int | None,
) -> OpenNewsEvent:
    metadata: dict[str, Any] = {
        "strategies": [{"id": strategy_id, "name": f"Strategy {strategy_id}"}],
    }
    if score is not None:
        metadata["score"] = score
    return OpenNewsEvent(
        provider_record_id=provider_record_id,
        observation_kind="report",
        provider_metadata=metadata,
        entry=NewsFeedEntry(
            guid=provider_record_id,
            link=f"https://example.com/{provider_record_id}",
            title=f"Strategy report {provider_record_id}",
            description="",
            published_at_ms=BASE_MS + 500,
            reporting_origin="opennews",
        ),
    )
