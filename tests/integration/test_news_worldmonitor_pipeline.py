from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tracefold.news import (
    NewsInterface,
    NewsProjectionSnapshot,
    NewsRepository,
    compute_news_story_projection,
    opennews_source,
    parse_opennews_message,
)

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
        cutoff_ms=int(payload["cutoff_ms"]),
        scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
        current_input_fingerprint=(
            str(payload["current_input_fingerprint"]) if payload.get("current_input_fingerprint") is not None else None
        ),
        rows=tuple(dict(row) for row in payload["rows"]),
    )


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
            repository.sync_source(source, now_ms=NOW_MS)
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


def test_story_projection_publishes_captured_input_and_rejects_older_publish_order() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_source(source, now_ms=NOW_MS)
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
            repository.rebuild_stories(now_ms=NOW_MS)

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
            caught_up = repository.rebuild_stories(now_ms=NOW_MS + 6)
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
            repository.sync_source(source, now_ms=NOW_MS)
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
            repository.sync_source(source, now_ms=NOW_MS)
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
            repository.sync_source(source, now_ms=NOW_MS + len(events))
            repository.record_opennews_events(
                source=source,
                events=events,
                observed_at_ms=NOW_MS + len(events),
            )
        with conn.transaction():
            rebuilt = repository.rebuild_stories(now_ms=NOW_MS + len(events))

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


def test_live_success_preserves_incomplete_recovery_diagnostic_until_gap_closes() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        source = opennews_source()
        with conn.transaction():
            NewsRepository(conn).sync_source(source, now_ms=NOW_MS)
            opened = NewsRepository(conn).update_opennews_live_status(
                source_id=source.source_id,
                connected=False,
                now_ms=NOW_MS + 1,
                error_code="opennews_recovery_window_incomplete",
                gap_unclosed=True,
                gap_boundary_provider_record_id="missing-boundary",
                expected_gap_version=None,
            )

        # A fresh Repository models a restarted Workers process reconnecting live.
        with conn.transaction():
            live = NewsRepository(conn).update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 2,
                error_code=None,
                gap_unclosed=True,
                gap_boundary_provider_record_id="missing-boundary",
                expected_gap_version=None,
            )
        still_open = conn.execute(
            "SELECT gap_unclosed, last_error FROM news_sources WHERE source_id = %s",
            (source.source_id,),
        ).fetchone()

        with conn.transaction():
            closed = NewsRepository(conn).update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=NOW_MS + 3,
                error_code=None,
                gap_unclosed=False,
                gap_boundary_provider_record_id=None,
                expected_gap_version=2,
            )
        after_close = conn.execute(
            "SELECT gap_unclosed, last_error FROM news_sources WHERE source_id = %s",
            (source.source_id,),
        ).fetchone()

        assert opened == ("missing-boundary", 1)
        assert live == ("missing-boundary", 2)
        assert dict(still_open) == {
            "gap_unclosed": True,
            "last_error": "opennews_recovery_window_incomplete",
        }
        assert closed == (None, 2)
        assert dict(after_close) == {"gap_unclosed": False, "last_error": None}
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
            NewsRepository(setup).sync_source(source, now_ms=NOW_MS)
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
