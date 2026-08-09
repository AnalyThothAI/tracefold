from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from typing import Any

import tracefold.news.runtime as news_runtime
from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tests.support.news import rebuild_news_projection
from tracefold.app.repositories import repositories_for_connection
from tracefold.news import NewsAcquisition, NewsFeedEntry, NewsFeedFetch
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.projection import (
    NewsProjectionSnapshot,
    compute_news_story_projection,
)
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import opennews_source, public_rss_sources

NOW_MS = 1_785_560_400_000


def _event(
    *,
    title: str,
    score: int | None,
    record_id: str = "wire-1",
    published_at_ms: int = NOW_MS,
):
    ai_rating = {"score": score, "grade": "A"} if score is not None else None
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": record_id,
                "text": title,
                "description": "Policy decision and market response",
                "newsType": "Reuters",
                "engineType": "news",
                "link": "https://example.com/fed?utm_source=opennews",
                "ts": published_at_ms,
                **({"aiRating": ai_rating} if ai_rating is not None else {}),
                "coins": [
                    {
                        "symbol": "BTC",
                        "market_type": "spot",
                        "match": "Bitcoin",
                    }
                ],
            },
        }
    )
    assert event is not None
    return event


def _projection_snapshot(payload: dict[str, Any]) -> NewsProjectionSnapshot:
    return NewsProjectionSnapshot(
        input_fingerprint=str(payload["input_fingerprint"]),
        scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
        current_input_fingerprint=(
            str(payload["current_input_fingerprint"]) if payload.get("current_input_fingerprint") is not None else None
        ),
        rows=tuple(dict(row) for row in payload["rows"]),
    )


def test_opennews_primary_story_projects_before_any_rss_attempt() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        rss_sources = public_rss_sources()[:2]
        with conn.transaction():
            repository.sync_sources((*rss_sources, opennews_source()), now_ms=NOW_MS)

        empty = repository.load_story_projection(now_ms=NOW_MS)
        assert empty is not None
        assert empty["rows"] == []

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event(title="OpenNews primary report reaches Story first", score=None),),
                observed_at_ms=NOW_MS + 1,
            )

        ready = repository.load_story_projection(now_ms=NOW_MS + 1)
        assert ready is not None
        assert [row["source_kind"] for row in ready["rows"]] == ["opennews"]
        rss_attempts = conn.execute(
            """
            SELECT count(*) FILTER (WHERE last_fetch_started_at_ms IS NOT NULL) AS attempts
              FROM news_sources
             WHERE source_kind = 'rss'
            """
        ).fetchone()
        assert rss_attempts["attempts"] == 0
    finally:
        conn.close()


def test_opennews_items_expire_without_an_enabled_rss_catalog(monkeypatch) -> None:
    class _Database:
        def __init__(self, conn: Any) -> None:
            self.conn = conn

        async def run_business(self, _name, function, /, *args, **kwargs):
            kwargs.pop("operation_timeout_seconds")
            return function(*args, **kwargs)

        @contextmanager
        def worker_session(self, _name: str, _timeout: float) -> Iterator[Any]:
            yield repositories_for_connection(self.conn)

    class _Reader:
        def __init__(self) -> None:
            self.requests = 0

        def fetch_wire(self, **_kwargs):
            self.requests += 1
            raise AssertionError("disabled RSS must not issue a request")

        def close(self) -> None:
            return None

    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(_event(title="OpenNews fact expires on the acquisition clock", score=None),),
                observed_at_ms=NOW_MS,
            )

        reader = _Reader()
        expiry_at_ms = NOW_MS + 12 * 60 * 60 * 1_000 + 1
        monkeypatch.setattr(news_runtime, "_now_ms", lambda: expiry_at_ms)
        acquisition = NewsAcquisition(
            db=_Database(conn),
            finite_operations=object(),
            rss_sources=(),
            rss_feed_reader=reader,
            rss_feed_parser=lambda *_args, **_kwargs: None,
            opennews_source=source,
        )

        assert asyncio.run(acquisition.turn()) is False
        assert reader.requests == 0
        row = conn.execute(
            "SELECT active FROM news_items WHERE source_id = %s",
            (source.source_id,),
        ).fetchone()
        assert row["active"] is False
    finally:
        conn.close()


def test_opennews_only_reconcile_disables_prior_rss_sources_and_releases_claims() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        rss_source = public_rss_sources()[0]
        with conn.transaction():
            repository.sync_sources((rss_source, source), now_ms=NOW_MS)
            claim = repository.claim_due_rss_source(
                now_ms=NOW_MS,
                claim_token="00000000-0000-0000-0000-000000000038",
                lease_expires_at_ms=NOW_MS + 60_000,
            )
            assert claim is not None
            repository.sync_sources((source,), now_ms=NOW_MS + 1)

        rss_row = conn.execute(
            """
            SELECT enabled, claim_token, claim_lease_expires_at_ms
              FROM news_sources
             WHERE source_id = %s
            """,
            (rss_source.source_id,),
        ).fetchone()
        assert dict(rss_row) == {
            "enabled": False,
            "claim_token": None,
            "claim_lease_expires_at_ms": None,
        }
        inventory = repository.list_sources()
        assert [item["source_id"] for item in inventory["items"]] == [source.source_id]
    finally:
        conn.close()


def test_opennews_primary_failure_allows_degraded_rss_only_projection() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        rss_sources = public_rss_sources()[:2]
        first_token = "00000000-0000-0000-0000-000000000003"
        with conn.transaction():
            repository.sync_sources(
                (*rss_sources, opennews_source()),
                now_ms=NOW_MS,
            )
            claim = repository.claim_due_rss_source(
                now_ms=NOW_MS,
                claim_token=first_token,
                lease_expires_at_ms=NOW_MS + 60_000,
            )
            assert claim is not None
            claimed_source = next(source for source in rss_sources if source.source_id == str(claim["source_id"]))
            assert repository.record_rss_fetch(
                source=claimed_source,
                claim_token=first_token,
                fetch=NewsFeedFetch(
                    status_code=200,
                    entries=(
                        NewsFeedEntry(
                            guid="rss-primary-unavailable",
                            link="https://example.com/rss-primary-unavailable",
                            title="Public RSS evidence remains available during OpenNews outage",
                            description="Independent public evidence keeps the product readable.",
                            published_at_ms=NOW_MS,
                        ),
                    ),
                    entries_seen=1,
                    etag=None,
                    last_modified=None,
                ),
                finished_at_ms=NOW_MS + 1,
            )
            assert repository.update_opennews_live_status(
                source_id=opennews_source().source_id,
                connected=False,
                now_ms=NOW_MS + 1,
                error_code="opennews_token_missing",
            )

        ready = repository.load_story_projection(now_ms=NOW_MS + 1)
        assert ready is not None
        assert len(ready["rows"]) == 1
        assert ready["rows"][0]["source_kind"] == "rss"
        unattempted_source = next(source for source in rss_sources if source != claimed_source)
        second_state = conn.execute(
            "SELECT last_fetch_finished_at_ms FROM news_sources WHERE source_id = %s",
            (unattempted_source.source_id,),
        ).fetchone()
        assert second_state["last_fetch_finished_at_ms"] is None
    finally:
        conn.close()


def test_ingest_health_is_driven_by_opennews_primary_with_rss_as_corroboration() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        rss_sources = public_rss_sources()[:2]
        with conn.transaction():
            repository.sync_sources(
                (*rss_sources, source),
                now_ms=NOW_MS,
            )

        warming = repository.health_snapshot(now_ms=NOW_MS, rss_enabled=True)
        assert warming["layers"]["ingest"]["status"] == "warming"
        assert warming["layers"]["ingest"]["reasons"] == [
            "opennews_primary_no_success_yet",
            "public_rss_corroboration_warming",
        ]

        with conn.transaction():
            repository.record_opennews_events(
                source=source,
                events=(_event(title="OpenNews primary live report", score=None),),
                observed_at_ms=NOW_MS + 1,
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 1,
                error_code=None,
            )

        ready = repository.health_snapshot(now_ms=NOW_MS + 1, rss_enabled=True)
        assert ready["layers"]["ingest"]["status"] == "ready"
        assert ready["layers"]["ingest"]["reasons"] == [
            "public_rss_corroboration_warming",
        ]

        with conn.transaction():
            for index in range(2):
                claim_token = f"00000000-0000-0000-0000-{index + 10:012d}"
                claim = repository.claim_due_rss_source(
                    now_ms=NOW_MS + 1,
                    claim_token=claim_token,
                    lease_expires_at_ms=NOW_MS + 60_001,
                )
                assert claim is not None
                claimed_source = next(
                    candidate for candidate in rss_sources if candidate.source_id == str(claim["source_id"])
                )
                assert repository.record_rss_fetch(
                    source=claimed_source,
                    claim_token=claim_token,
                    fetch=NewsFeedFetch(
                        status_code=200,
                        entries=(
                            NewsFeedEntry(
                                guid=f"rss-health-{index}",
                                link=f"https://example.com/rss-health-{index}",
                                title=f"Public corroboration report number {index}",
                                published_at_ms=NOW_MS,
                            ),
                        ),
                        entries_seen=1,
                    ),
                    finished_at_ms=NOW_MS + 2,
                )

        corroborated = repository.health_snapshot(now_ms=NOW_MS + 2, rss_enabled=True)
        assert corroborated["layers"]["ingest"]["status"] == "ready"
        assert corroborated["layers"]["ingest"]["reasons"] == []

        with conn.transaction():
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=False,
                now_ms=NOW_MS + 3,
                error_code=None,
            )

        disconnected = repository.health_snapshot(now_ms=NOW_MS + 3, rss_enabled=True)
        assert disconnected["layers"]["ingest"]["status"] == "degraded"
        assert disconnected["layers"]["ingest"]["reasons"] == [
            "opennews_primary_disconnected",
        ]

        with conn.transaction():
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 4,
                error_code=None,
            )
            repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=False,
                now_ms=NOW_MS + 5,
                error_code="opennews_live_disconnected",
            )

        degraded = repository.health_snapshot(now_ms=NOW_MS + 5, rss_enabled=True)
        assert degraded["layers"]["ingest"]["status"] == "degraded"
        assert degraded["layers"]["ingest"]["reasons"] == [
            "opennews_primary_error",
        ]
    finally:
        conn.close()


def test_rss_failure_preserves_the_last_successful_item_snapshot() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = public_rss_sources()[0]
        first_token = "00000000-0000-0000-0000-000000000004"
        with conn.transaction():
            repository.sync_sources((source, opennews_source()), now_ms=NOW_MS)
            claim = repository.claim_due_rss_source(
                now_ms=NOW_MS,
                claim_token=first_token,
                lease_expires_at_ms=NOW_MS + 60_000,
            )
            assert claim is not None and claim["source_id"] == source.source_id
            assert repository.record_rss_fetch(
                source=source,
                claim_token=first_token,
                fetch=NewsFeedFetch(
                    status_code=200,
                    entries=(
                        NewsFeedEntry(
                            guid="healthy-before-parse-failure",
                            link="https://example.com/healthy-before-parse-failure",
                            title="Healthy public report remains current after parse failure",
                            published_at_ms=NOW_MS,
                            language="en",
                            reporting_origin=source.name,
                        ),
                    ),
                    entries_seen=1,
                ),
                finished_at_ms=NOW_MS + 1,
            ) == {
                "items_inserted": 1,
                "items_updated": 0,
                "items_deactivated": 0,
            }

        before = conn.execute(
            """
            SELECT xmin::text AS xmin, ctid::text AS ctid,
                   content_fingerprint, active, updated_at_ms
              FROM news_items
             WHERE source_id = %s
            """,
            (source.source_id,),
        ).fetchone()
        assert before is not None and before["active"] is True

        second_now_ms = NOW_MS + source.refresh_interval_seconds * 1_000 + 1
        second_token = "00000000-0000-0000-0000-000000000005"
        with conn.transaction():
            claim = repository.claim_due_rss_source(
                now_ms=second_now_ms,
                claim_token=second_token,
                lease_expires_at_ms=second_now_ms + 60_000,
            )
            assert claim is not None and claim["source_id"] == source.source_id
            assert repository.record_rss_failure(
                source_id=source.source_id,
                claim_token=second_token,
                finished_at_ms=second_now_ms + 1,
                error_code="news_rss_parse_no_entries",
                status_code=200,
            )

        after = conn.execute(
            """
            SELECT xmin::text AS xmin, ctid::text AS ctid,
                   content_fingerprint, active, updated_at_ms
              FROM news_items
             WHERE source_id = %s
            """,
            (source.source_id,),
        ).fetchone()
        source_state = conn.execute(
            """
            SELECT last_outcome, last_error, last_http_status
              FROM news_sources
             WHERE source_id = %s
            """,
            (source.source_id,),
        ).fetchone()
        assert after == before
        assert source_state == {
            "last_outcome": "failed",
            "last_error": "news_rss_parse_no_entries",
            "last_http_status": 200,
        }
    finally:
        conn.close()


def test_opennews_ingestion_and_story_projection_share_the_12h_boundary() -> None:
    conn = connect_postgres_test(read_only=False)
    hour_ms = 60 * 60 * 1000
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        events = (
            _event(
                record_id="outside-story-window",
                title="Older policy background remains Article evidence",
                score=40,
                published_at_ms=NOW_MS - (12 * hour_ms) - 1,
            ),
            _event(
                record_id="story-window-boundary",
                title="Policy meeting begins at the Story window boundary",
                score=60,
                published_at_ms=NOW_MS - (12 * hour_ms),
            ),
            _event(
                record_id="current-story-item",
                title="Markets react to the current policy announcement",
                score=80,
                published_at_ms=NOW_MS - hour_ms,
            ),
        )

        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            result = repository.record_opennews_events(
                source=source,
                events=events,
                observed_at_ms=NOW_MS,
            )
            rebuild_news_projection(repository, now_ms=NOW_MS)

        assert result["items_inserted"] == 2
        assert result["rejected"] == 1
        rows = conn.execute(
            """
            SELECT item.provider_record_id, item.active,
                   member.story_id IS NOT NULL AS has_story
              FROM news_items item
              LEFT JOIN news_story_members member ON member.item_id = item.item_id
             ORDER BY item.provider_record_id
            """
        ).fetchall()
        assert {row["provider_record_id"]: (row["active"], row["has_story"]) for row in rows} == {
            "current-story-item": (True, True),
            "story-window-boundary": (True, True),
        }
    finally:
        conn.close()


def test_item_classification_has_one_owner_in_story_projection() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(
                    _event(
                        record_id="classification-owner",
                        title="Military forces launch a major missile attack",
                        score=90,
                    ),
                ),
                observed_at_ms=NOW_MS,
            )

        pending = conn.execute(
            """
            SELECT level, category, classification_source,
                   classification_confidence, importance_score,
                   importance_factors
              FROM news_items
             WHERE provider_record_id = 'classification-owner'
            """
        ).fetchone()
        assert pending == {
            "level": None,
            "category": None,
            "classification_source": None,
            "classification_confidence": None,
            "importance_score": 0,
            "importance_factors": {},
        }

        with conn.transaction():
            rebuild_news_projection(repository, now_ms=NOW_MS)

        projected = conn.execute(
            """
            SELECT level, category, classification_source,
                   classification_confidence, importance_score,
                   importance_factors
              FROM news_items
             WHERE provider_record_id = 'classification-owner'
            """
        ).fetchone()
        assert projected["level"] == "high"
        assert projected["category"] == "military"
        assert projected["classification_source"] == "keyword"
        assert projected["classification_confidence"] == 0.8
        assert projected["importance_score"] > 0
        assert projected["importance_factors"]
    finally:
        conn.close()


def test_story_projection_publishes_captured_input_and_rejects_older_publish_order() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(
                    _event(
                        record_id="projection-base",
                        title="Baseline policy report",
                        score=50,
                    ),
                ),
                observed_at_ms=NOW_MS,
            )
            rebuild_news_projection(repository, now_ms=NOW_MS)

        with conn.transaction():
            repository.record_opennews_events(
                source=source,
                events=(
                    _event(
                        record_id="projection-a",
                        title="First captured policy report",
                        score=60,
                        published_at_ms=NOW_MS + 1,
                    ),
                ),
                observed_at_ms=NOW_MS + 1,
            )
            older = _projection_snapshot(repository.load_story_projection(now_ms=NOW_MS + 2))
        older_projection = compute_news_story_projection(older)

        with conn.transaction():
            repository.record_opennews_events(
                source=source,
                events=(
                    _event(
                        record_id="projection-b",
                        title="Second captured infrastructure report",
                        score=70,
                        published_at_ms=NOW_MS + 2,
                    ),
                ),
                observed_at_ms=NOW_MS + 2,
            )
            newer = _projection_snapshot(repository.load_story_projection(now_ms=NOW_MS + 3))
        newer_projection = compute_news_story_projection(newer)

        with conn.transaction():
            repository.record_opennews_events(
                source=source,
                events=(
                    _event(
                        record_id="projection-c",
                        title="Late report arrives during Story computation",
                        score=80,
                        published_at_ms=NOW_MS + 3,
                    ),
                ),
                observed_at_ms=NOW_MS + 3,
            )
            accepted = repository.publish_story_projection(
                snapshot=newer,
                projection=newer_projection,
                now_ms=NOW_MS + 4,
            )

        with conn.transaction():
            superseded = repository.publish_story_projection(
                snapshot=older,
                projection=older_projection,
                now_ms=NOW_MS + 5,
            )

        membership = {
            str(row["provider_record_id"]): bool(row["materialized"])
            for row in conn.execute(
                """
                SELECT item.provider_record_id,
                       member.item_id IS NOT NULL AS materialized
                  FROM news_items item
                  LEFT JOIN news_story_members member ON member.item_id = item.item_id
                 WHERE item.provider_record_id LIKE 'projection-%'
                 ORDER BY item.provider_record_id
                """
            ).fetchall()
        }
        conn.commit()

        assert accepted["projection_status"] == "rebuilt"
        assert superseded["projection_status"] == "superseded_snapshot"
        assert membership == {
            "projection-a": True,
            "projection-b": True,
            "projection-base": True,
            "projection-c": False,
        }

        with conn.transaction():
            caught_up = rebuild_news_projection(repository, now_ms=NOW_MS + 6)
        assert caught_up["projection_status"] == "rebuilt"
        assert (
            conn.execute(
                """
            SELECT count(*) AS count
              FROM news_items item
              JOIN news_story_members member ON member.item_id = item.item_id
             WHERE item.provider_record_id = 'projection-c'
            """
            ).fetchone()["count"]
            == 1
        )
    finally:
        conn.close()


def test_opennews_current_fact_updates_in_place_and_serves_provider_metadata() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        report = _event(title="Fed holds rates steady", score=None)
        annotation = parse_opennews_message(
            {
                "method": "news.ai_update",
                "params": {
                    "newsId": "wire-1",
                    "engineType": "news",
                    "newsType": "Reuters",
                    "score": 80,
                    "signal": "long",
                    "grade": "A+",
                },
            }
        )
        translation = parse_opennews_message(
            {
                "method": "news.update",
                "params": {
                    "id": "wire-1-zh",
                    "text": "美联储维持利率不变",
                    "newsType": "Translation",
                    "engineType": "news",
                    "ts": NOW_MS,
                },
            }
        )
        assert annotation is not None and translation is not None

        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            first = repository.record_opennews_events(
                source=source,
                events=(report,),
                observed_at_ms=NOW_MS,
            )
            duplicate = repository.record_opennews_events(
                source=source,
                events=(report, translation),
                observed_at_ms=NOW_MS + 1,
            )
            annotated = repository.record_opennews_events(
                source=source,
                events=(annotation,),
                observed_at_ms=NOW_MS + 2,
            )

        assert first["items_inserted"] == 1
        assert duplicate == {
            "events_seen": 2,
            "items_inserted": 0,
            "items_updated": 0,
            "metadata_updated": 0,
            "rejected": 2,
        }
        assert annotated["metadata_updated"] == 1
        before = conn.execute(
            """
            SELECT item_id, provider_record_id, provider_metadata
              FROM news_items
            """
        ).fetchone()
        assert before["provider_record_id"] == "wire-1"
        assert before["provider_metadata"] == {
            "score": 80,
            "signal": "long",
            "grade": "A+",
            "coins": [{"symbol": "BTC", "market_type": "spot", "match": "Bitcoin"}],
        }

        no_score = _event(title="Fed holds rates steady", score=None)
        with conn.transaction():
            no_score_result = repository.record_opennews_events(
                source=source,
                events=(no_score,),
                observed_at_ms=NOW_MS + 3,
            )
        preserved = conn.execute(
            "SELECT provider_metadata FROM news_items WHERE provider_record_id='wire-1'"
        ).fetchone()["provider_metadata"]
        assert no_score_result["items_updated"] == 0
        assert preserved["score"] == 80
        assert preserved["signal"] == "long"

        changed = _event(title="Fed holds rates steady after policy meeting", score=82)
        with conn.transaction():
            result = repository.record_opennews_events(
                source=source,
                events=(changed,),
                observed_at_ms=NOW_MS + 4,
            )
            rebuild_news_projection(repository, now_ms=NOW_MS + 4)
        after = conn.execute("SELECT item_id FROM news_items WHERE provider_record_id='wire-1'").fetchone()
        assert result["items_updated"] == 1
        assert after["item_id"] == before["item_id"]

        story = repository.list_feed()["stories"][0]
        detail = repository.get_story(story_id=story["story_id"])
        assert detail is not None
        assert detail["members"][0]["provider_record_id"] == "wire-1"
        assert detail["members"][0]["provider_metadata"]["score"] == 82

        sources = repository.list_sources()["items"]
        assert len(sources) == 1
        assert sources[0]["source_kind"] == "opennews"
        assert "latest_fetch_status" not in sources[0]

        with conn.transaction():
            live_failed = repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 5,
                error_code="opennews_buffer_overflow",
            )
            repository.mark_opennews_recovery_attempt(
                source_id=source.source_id,
                started_at_ms=NOW_MS + 6,
            )
        assert live_failed is True
        assert repository.opennews_last_recovery_attempt(source_id=source.source_id) == NOW_MS + 6
        source_status = conn.execute(
            "SELECT last_outcome, last_error FROM news_sources WHERE source_id = %s",
            (source.source_id,),
        ).fetchone()
        assert dict(source_status) == {
            "last_outcome": "recovery_running",
            "last_error": "opennews_buffer_overflow",
        }
    finally:
        conn.close()


def test_opennews_accepts_nonempty_canonical_plaintext_and_rejects_empty_title() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        emoji_only = parse_opennews_message(
            {
                "method": "news.update",
                "params": {
                    "id": "3442019",
                    "text": "👑👑👑",
                    "newsType": "Twitter",
                    "engineType": "news",
                    "link": "https://example.com/emoji-only",
                    "ts": NOW_MS,
                    "source": "aeyakovenko",
                    "score": 5,
                    "aiRating": {"score": 5, "signal": "neutral", "grade": "C"},
                    "coins": [{"symbol": "SOL", "market_type": "cex"}],
                },
            }
        )
        canonicalized_text_events = tuple(
            parse_opennews_message(
                {
                    "method": "news.update",
                    "params": {
                        "id": record_id,
                        "text": invalid_text,
                        "description": invalid_text,
                        "newsType": "Reuters",
                        "engineType": "news",
                        "link": f"https://example.com/{invalid_text}",
                        "ts": NOW_MS,
                    },
                }
            )
            for record_id, invalid_text in (
                ("wire-nul-title", "bad\x00title"),
                ("wire-surrogate-title", "bad\ud800title"),
            )
        )
        valid = parse_opennews_message(
            {
                "method": "news.update",
                "params": {
                    "id": "wire-valid",
                    "text": "Solana validators approve network upgrade",
                    "newsType": "Reuters",
                    "engineType": "news",
                    "ts": NOW_MS,
                    "aiRating": {"score": 75, "signal": "long", "grade": "A"},
                },
            }
        )
        assert emoji_only is not None and valid is not None
        assert all(event is not None for event in canonicalized_text_events)

        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            result = repository.record_opennews_events(
                source=source,
                events=(valid, emoji_only, *canonicalized_text_events),
                observed_at_ms=NOW_MS,
            )

        assert result == {
            "events_seen": 4,
            "items_inserted": 3,
            "items_updated": 0,
            "metadata_updated": 0,
            "rejected": 1,
        }
        rows = conn.execute(
            """
            SELECT provider_record_id, title, reporting_origin, provider_metadata
              FROM news_items
             ORDER BY provider_record_id
            """
        ).fetchall()
        assert rows == [
            {
                "provider_record_id": "3442019",
                "title": "👑👑👑",
                "reporting_origin": "aeyakovenko",
                "provider_metadata": {
                    "score": 5,
                    "source": "aeyakovenko",
                    "signal": "neutral",
                    "grade": "C",
                    "coins": [{"symbol": "SOL", "market_type": "cex"}],
                },
            },
            {
                "provider_record_id": "wire-nul-title",
                "title": "bad title",
                "reporting_origin": "reuters",
                "provider_metadata": {},
            },
            {
                "provider_record_id": "wire-valid",
                "title": "Solana validators approve network upgrade",
                "reporting_origin": "reuters",
                "provider_metadata": {
                    "score": 75,
                    "signal": "long",
                    "grade": "A",
                },
            },
        ]
    finally:
        conn.close()


def test_public_tracking_hash_does_not_fold_distinct_story_components() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        frames = (
            ("hyphenated", "Alpha-Beta!!!", "Reuters", None),
            ("joined", "AlphaBeta???", "AP", None),
            ("emoji-1", "👑👑👑", "Twitter", "aeyakovenko"),
            ("emoji-2", "👑👑👑", "Twitter", "aeyakovenko"),
        )
        parsed_events = []
        for index, (record_id, title, news_type, author) in enumerate(frames):
            event = parse_opennews_message(
                {
                    "method": "news.update",
                    "params": {
                        "id": record_id,
                        "text": title,
                        "newsType": news_type,
                        "engineType": "news",
                        "ts": NOW_MS + index,
                        **({"source": author} if author else {}),
                    },
                }
            )
            assert event is not None
            parsed_events.append(event)
        events = tuple(parsed_events)

        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS + len(events))
            repository.record_opennews_events(
                source=source,
                events=events,
                observed_at_ms=NOW_MS + len(events),
            )
        with conn.transaction():
            rebuilt = rebuild_news_projection(repository, now_ms=NOW_MS + len(events))

        ownership = conn.execute(
            """
            SELECT count(*) AS membership_count,
                   count(DISTINCT member.story_id) AS story_count,
                   count(DISTINCT story.canonical_key) AS canonical_key_count
              FROM news_story_members member
              JOIN news_stories story ON story.story_id = member.story_id
            """
        ).fetchone()
        assert ownership == {
            "membership_count": 4,
            "story_count": 4,
            "canonical_key_count": 2,
        }
        assert rebuilt["projection_status"] == "rebuilt"
        assert conn.execute("SELECT count(*) AS count FROM news_items").fetchone()["count"] == 4
    finally:
        conn.close()


def test_live_success_preserves_exhausted_overlap_diagnostic_until_recovery_succeeds() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        source = opennews_source()
        with conn.transaction():
            repository = NewsRepository(conn)
            repository.sync_sources((source,), now_ms=NOW_MS)
            repository.mark_opennews_recovery_attempt(
                source_id=source.source_id,
                started_at_ms=NOW_MS + 1,
            )
            exhausted = repository.complete_opennews_recovery(
                source_id=source.source_id,
                started_at_ms=NOW_MS + 1,
                finished_at_ms=NOW_MS + 2,
                window_exhausted=True,
                items_seen=220,
                items_accepted=200,
                rejection_counts={"future_date": 20},
            )

        # A fresh Repository models a restarted Workers process reconnecting live.
        with conn.transaction():
            live = NewsRepository(conn).update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 3,
                error_code=None,
            )
        still_exhausted = conn.execute(
            "SELECT last_outcome, last_error FROM news_sources WHERE source_id = %s",
            (source.source_id,),
        ).fetchone()

        with conn.transaction():
            recovered_repository = NewsRepository(conn)
            recovered_repository.mark_opennews_recovery_attempt(
                source_id=source.source_id,
                started_at_ms=NOW_MS + 4,
            )
            recovered = recovered_repository.complete_opennews_recovery(
                source_id=source.source_id,
                started_at_ms=NOW_MS + 4,
                finished_at_ms=NOW_MS + 5,
                window_exhausted=False,
                items_seen=20,
                items_accepted=10,
                rejection_counts={},
            )
        after_recovery = conn.execute(
            "SELECT last_outcome, last_error FROM news_sources WHERE source_id = %s",
            (source.source_id,),
        ).fetchone()

        assert exhausted is True
        assert live is True
        assert dict(still_exhausted) == {
            "last_outcome": "recovery_window_exhausted",
            "last_error": "opennews_recovery_window_exhausted",
        }
        assert recovered is True
        assert dict(after_recovery) == {
            "last_outcome": "recovery_success",
            "last_error": None,
        }
    finally:
        conn.close()


def test_opennews_recovery_stops_at_the_first_existing_provider_record() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        existing = _event(
            record_id="existing-overlap",
            title="Existing overlap report",
            score=60,
            published_at_ms=NOW_MS - 60_000,
        )
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            repository.record_opennews_events(
                source=source,
                events=(existing,),
                observed_at_ms=NOW_MS,
            )
            repository.mark_opennews_recovery_attempt(
                source_id=source.source_id,
                started_at_ms=NOW_MS + 1,
            )
            outcome = repository.record_opennews_recovery_page(
                source=source,
                events=(
                    _event(
                        record_id="new-before-overlap",
                        title="New report before overlap",
                        score=70,
                        published_at_ms=NOW_MS,
                    ),
                    existing,
                    _event(
                        record_id="must-not-cross-overlap",
                        title="Older report beyond overlap",
                        score=50,
                        published_at_ms=NOW_MS - 120_000,
                    ),
                ),
                observed_at_ms=NOW_MS + 1,
                recovery_started_at_ms=NOW_MS + 1,
            )

        assert outcome["events_seen"] == 3
        assert outcome["items_inserted"] == 1
        assert outcome["overlap_complete"] is True
        assert outcome["stop_reason"] == "existing_provider_record"
        provider_ids = {
            str(row["provider_record_id"])
            for row in conn.execute("SELECT provider_record_id FROM news_items ORDER BY provider_record_id").fetchall()
        }
        assert provider_ids == {"existing-overlap", "new-before-overlap"}
    finally:
        conn.close()


def test_opennews_rest_and_websocket_reports_atomically_merge_during_overlap() -> None:
    setup = connect_postgres_test(read_only=False)
    writer_a = connect_postgres_test(read_only=False)
    writer_b = connect_postgres_test(read_only=False)
    observer = connect_postgres_test(read_only=False)
    release_first = Event()
    first_holding = Event()
    second_started = Event()
    source = opennews_source()
    first = _event(title="Fed holds rates steady", score=70)
    second = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-1",
                "text": "Fed holds rates steady",
                "description": "Policy decision and market response",
                "newsType": "Reuters",
                "engineType": "news",
                "link": "https://example.com/fed?utm_source=opennews",
                "ts": NOW_MS,
                "aiRating": {"signal": "long", "grade": "A+"},
                "coins": [],
            },
        }
    )
    assert second is not None

    try:
        reset_postgres_schema(setup)
        with setup.transaction():
            NewsRepository(setup).sync_sources((source,), now_ms=NOW_MS)
        writer_b_pid = int(writer_b.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
        writer_b.commit()

        def write_first() -> dict[str, int]:
            with writer_a.transaction():
                result = NewsRepository(writer_a).record_opennews_events(
                    source=source,
                    events=(first,),
                    observed_at_ms=NOW_MS,
                )
                first_holding.set()
                assert release_first.wait(timeout=5.0)
                return result

        def write_second() -> dict[str, int]:
            assert first_holding.wait(timeout=5.0)
            with writer_b.transaction():
                second_started.set()
                return NewsRepository(writer_b).record_opennews_events(
                    source=source,
                    events=(second,),
                    observed_at_ms=NOW_MS + 1,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(write_first)
            assert first_holding.wait(timeout=5.0)
            second_future = executor.submit(write_second)
            assert second_started.wait(timeout=5.0)
            deadline = time.monotonic() + 5.0
            blocked = False
            try:
                while time.monotonic() < deadline:
                    blocked = bool(
                        observer.execute(
                            "SELECT cardinality(pg_blocking_pids(%s)) > 0 AS blocked",
                            (writer_b_pid,),
                        ).fetchone()["blocked"]
                    )
                    observer.commit()
                    if blocked:
                        break
                    time.sleep(0.01)
                assert blocked, "second OpenNews writer never overlapped the uncommitted first writer"
            finally:
                release_first.set()
            first_result = first_future.result(timeout=5.0)
            second_result = second_future.result(timeout=5.0)

        assert first_result["items_inserted"] == 1
        assert second_result["items_updated"] == 1
        row = setup.execute(
            "SELECT count(*) AS count, provider_metadata FROM news_items GROUP BY provider_metadata"
        ).fetchone()
        assert row["count"] == 1
        assert row["provider_metadata"] == {
            "score": 70,
            "signal": "long",
            "grade": "A+",
            "coins": [{"symbol": "BTC", "market_type": "spot", "match": "Bitcoin"}],
        }
    finally:
        release_first.set()
        observer.close()
        writer_b.close()
        writer_a.close()
        setup.close()
