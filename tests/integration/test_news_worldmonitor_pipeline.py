from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tracefold.news import NewsInterface, NewsRepository, opennews_source, parse_opennews_message

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


def test_story_projection_is_complete_for_12h_while_older_articles_remain_facts() -> None:
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
            repository.rebuild_stories(now_ms=NOW_MS)

        assert result["items_inserted"] == 3
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
            "outside-story-window": (False, False),
            "story-window-boundary": (True, True),
        }
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
            repository.rebuild_stories(now_ms=NOW_MS + 4)
        after = conn.execute("SELECT item_id FROM news_items WHERE provider_record_id='wire-1'").fetchone()
        assert result["items_updated"] == 1
        assert after["item_id"] == before["item_id"]

        story = NewsInterface(repository).get_feed()["stories"][0]
        detail = NewsInterface(repository).get_story(story_id=story["story_id"])
        assert detail is not None
        assert detail["members"][0]["provider_record_id"] == "wire-1"
        assert detail["members"][0]["provider_metadata"]["score"] == 82

        sources = repository.list_sources()["items"]
        assert len(sources) == 1
        assert sources[0]["source_kind"] == "opennews"
        assert "latest_fetch_status" not in sources[0]

        with conn.transaction():
            first_gap = repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 5,
                error_code="opennews_buffer_overflow",
                gap_unclosed=True,
                gap_boundary_provider_record_id=None,
                expected_gap_version=None,
            )
            second_gap = repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 6,
                error_code="opennews_buffer_overflow",
                gap_unclosed=True,
                gap_boundary_provider_record_id="later-dropped-record",
                expected_gap_version=None,
            )
            stale_close = repository.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 7,
                error_code=None,
                gap_unclosed=False,
                gap_boundary_provider_record_id="wire-1",
                expected_gap_version=1,
            )
            repository.mark_opennews_recovery_attempt(
                source_id=source.source_id,
                started_at_ms=NOW_MS + 8,
            )
        assert first_gap == ("wire-1", 1)
        assert second_gap == ("wire-1", 2)
        assert stale_close is None
        assert repository.opennews_recovery_state(source_id=source.source_id) == (
            NOW_MS + 8,
            "wire-1",
            2,
        )
    finally:
        conn.close()


def test_opennews_unusable_title_is_rejected_without_poisoning_batch() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        unusable = parse_opennews_message(
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
        invalid_text_events = tuple(
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
        assert unusable is not None and valid is not None
        assert all(event is not None for event in invalid_text_events)

        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            result = repository.record_opennews_events(
                source=source,
                events=(valid, unusable, *invalid_text_events),
                observed_at_ms=NOW_MS,
            )

        assert result == {
            "events_seen": 4,
            "items_inserted": 1,
            "items_updated": 0,
            "metadata_updated": 0,
            "rejected": 3,
        }
        rows = conn.execute(
            """
            SELECT provider_record_id, normalized_title, provider_metadata
              FROM news_items
             ORDER BY provider_record_id
            """
        ).fetchall()
        assert rows == [
            {
                "provider_record_id": "wire-valid",
                "normalized_title": "solana validators approve network upgrade",
                "provider_metadata": {
                    "score": 75,
                    "signal": "long",
                    "grade": "A",
                },
            }
        ]
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
