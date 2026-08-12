from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tests.support.news import rebuild_news_projection
from tracefold.app.repositories import repositories_for_connection
from tracefold.news import push as push_module
from tracefold.news import query_specs as query_specs_module
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.push import (
    NewsPushDeliveryError,
    NewsPushReceipt,
    NewsStoryPush,
    PreparedNewsPush,
)
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import opennews_source

BASE_MS = 1_785_560_400_000


def _event(
    record_id: str,
    *,
    title: str,
    score: float,
    published_at_ms: int,
    url: str | None = "https://example.com/news",
    asset_symbols: tuple[str, ...] = ("BTC",),
):
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": record_id,
                "text": title,
                "description": f"Details for {record_id}",
                "newsType": "Reuters",
                "engineType": "news",
                "link": url,
                "ts": published_at_ms,
                "aiRating": {
                    "score": score,
                    "signal": "long",
                    "grade": "A",
                },
                "coins": [{"symbol": symbol, "market_type": "spot"} for symbol in asset_symbols],
            },
        }
    )
    assert event is not None
    return event


def _annotation(
    record_id: str,
    *,
    score: float,
    asset_symbols: tuple[str, ...] | None = None,
):
    params: dict[str, Any] = {
        "newsId": record_id,
        "engineType": "news",
        "score": score,
        "signal": "long",
        "grade": "A+",
    }
    if asset_symbols is not None:
        params["coins"] = [{"symbol": symbol, "market_type": "spot"} for symbol in asset_symbols]
    event = parse_opennews_message(
        {
            "method": "news.ai_update",
            "params": params,
        }
    )
    assert event is not None
    return event


def _seed_source(conn: Any) -> NewsRepository:
    repository = NewsRepository(conn)
    with conn.transaction():
        repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
    return repository


def _push_health(conn: Any, *, now_ms: int) -> dict[str, Any]:
    return NewsRepository(conn).push_health_snapshot(now_ms=now_ms)


def _assert_push_summary_matches_ledger(conn: Any) -> None:
    state = conn.execute(
        """
        SELECT total_count, suppressed_count, pending_count, retry_count,
               sent_count, terminal_count, latest_sent_at_ms
          FROM news_push_state
         WHERE singleton_key = 'current'
        """
    ).fetchone()
    ledger = conn.execute(
        """
        SELECT count(*) AS total_count,
               count(*) FILTER (WHERE status = 'suppressed') AS suppressed_count,
               count(*) FILTER (
                 WHERE status IN ('pending_translation', 'pending_delivery')
               ) AS pending_count,
               count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
               count(*) FILTER (WHERE status = 'sent') AS sent_count,
               count(*) FILTER (WHERE status = 'terminal') AS terminal_count,
               max(sent_at_ms) AS latest_sent_at_ms
          FROM news_push_deliveries
        """
    ).fetchone()
    assert state == ledger


def test_push_health_sample_overflow_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        monkeypatch.setattr(query_specs_module, "SLO_SAMPLE_LIMIT", 2)
        with conn.transaction():
            repository.initialize_push_baseline(now_ms=BASE_MS - 200_000)
            for index in range(3):
                story_id = f"{index + 1:x}" * 64
                assert repository.insert_push_candidate(
                    story_id=story_id,
                    selected_item_id=f"overflow-item-{index}",
                    provider_score=88,
                    threshold_observed_at_ms=BASE_MS - 130_000,
                    source_payload={},
                    suppressed=False,
                    now_ms=BASE_MS - 130_000,
                )
            conn.execute(
                """
                UPDATE news_push_deliveries
                   SET status = 'sent',
                       translation_status = 'unavailable',
                       translation_prompt_version = 'title_zh_v2',
                       translation_attempted_at_ms = %s + ascii(left(story_id, 1)),
                       translation_duration_ms = 1_000,
                       translation_fallback_code = 'news_push_translation_timeout',
                       delivery_payload = '{}'::jsonb,
                       payload_fingerprint = repeat('a', 64),
                       next_attempt_at_ms = NULL,
                       sent_at_ms = %s + ascii(left(story_id, 1)),
                       updated_at_ms = %s + ascii(left(story_id, 1))
                """,
                (BASE_MS - 1_000, BASE_MS - 500, BASE_MS - 500),
            )
            conn.execute(
                """
                UPDATE news_push_state
                   SET total_count = 3,
                       pending_count = 0,
                       sent_count = 3,
                       latest_sent_at_ms = %s,
                       updated_at_ms = %s
                 WHERE singleton_key = 'current'
                """,
                (BASE_MS - 449, BASE_MS),
            )

        health = repository.push_health_snapshot(now_ms=BASE_MS)

        assert health["status"] == "degraded"
        assert health["translation_24h"] == {
            "attempted": 0,
            "succeeded": 0,
            "success_ratio": None,
            "latency_p95_ms": None,
            "failure_counts": {},
            "slo_met": None,
            "sample_complete": False,
        }
        assert health["delivery_24h"] == {
            "completed": 0,
            "latency_p95_ms": None,
            "over_120s": 0,
            "slo_met": None,
            "sample_complete": False,
        }
        public_health = repository.health_snapshot(
            now_ms=BASE_MS,
            rss_enabled=False,
            push_enabled=True,
            feishu_webhook_url_configured=True,
        )
        assert public_health["layers"]["push"]["status"] == "degraded"
        assert public_health["layers"]["push"]["reasons"] == [
            "push_translation_sample_overflow",
            "push_delivery_sample_overflow",
        ]
    finally:
        conn.close()


def test_push_candidate_summary_updates_only_for_committed_insertions() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)

        with conn.transaction():
            assert repository.insert_push_candidate(
                story_id="1" * 64,
                selected_item_id="summary-item-1",
                provider_score=90,
                threshold_observed_at_ms=BASE_MS,
                source_payload={},
                suppressed=False,
                now_ms=BASE_MS,
            )
        _assert_push_summary_matches_ledger(conn)
        assert _push_health(conn, now_ms=BASE_MS)["pending_count"] == 1

        with conn.transaction():
            assert not repository.insert_push_candidate(
                story_id="1" * 64,
                selected_item_id="summary-item-1",
                provider_score=90,
                threshold_observed_at_ms=BASE_MS,
                source_payload={},
                suppressed=False,
                now_ms=BASE_MS + 1,
            )
        _assert_push_summary_matches_ledger(conn)

        with pytest.raises(RuntimeError, match="rollback-summary"), conn.transaction():
            assert repository.insert_push_candidate(
                story_id="2" * 64,
                selected_item_id="summary-item-2",
                provider_score=90,
                threshold_observed_at_ms=BASE_MS + 2,
                source_payload={},
                suppressed=True,
                now_ms=BASE_MS + 2,
            )
            raise RuntimeError("rollback-summary")

        _assert_push_summary_matches_ledger(conn)
        assert _push_health(conn, now_ms=BASE_MS + 2)["total_count"] == 1
    finally:
        conn.close()


def test_initialized_push_baseline_is_a_lock_free_steady_read() -> None:
    lock_holder = connect_postgres_test(read_only=False)
    reader = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(lock_holder)
        repository = NewsRepository(lock_holder)
        with lock_holder.transaction():
            assert repository.initialize_push_baseline(now_ms=BASE_MS) == (BASE_MS, True)

        reader.execute("SET lock_timeout = '100ms'")
        reader.commit()
        with lock_holder.transaction():
            lock_holder.execute(
                """
                SELECT singleton_key
                  FROM news_push_state
                 WHERE singleton_key = 'current'
                 FOR UPDATE
                """
            ).fetchone()
            assert NewsRepository(reader).initialize_push_baseline(now_ms=BASE_MS + 1) == (BASE_MS, False)
            reader.commit()
    finally:
        reader.close()
        lock_holder.close()


def test_push_summary_tracks_suppress_retry_sent_terminal_and_bulk_transitions() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)

        def insert(story_char: str, *, now_ms: int) -> str:
            story_id = story_char * 64
            assert repository.insert_push_candidate(
                story_id=story_id,
                selected_item_id=f"summary-item-{story_char}",
                provider_score=90,
                threshold_observed_at_ms=now_ms,
                source_payload={},
                suppressed=False,
                now_ms=now_ms,
            )
            return story_id

        def claim(story_id: str, token: str, *, now_ms: int) -> None:
            assert (
                repository.claim_push_delivery(
                    story_id=story_id,
                    now_ms=now_ms,
                    max_attempts=6,
                    lease_owner="summary-test",
                    lease_token=token,
                    lease_expires_at_ms=now_ms + 10,
                )
                is not None
            )

        payload = {
            "presentation": {
                "prompt_version": "title_zh_v2",
                "fallback_code": None,
                "translation_attempted_at_ms": BASE_MS + 2,
                "translation_duration_ms": 1250,
            }
        }

        with conn.transaction():
            sent_story = insert("3", now_ms=BASE_MS)
            claim(sent_story, "sent-token", now_ms=BASE_MS + 1)
            assert (
                repository.freeze_push_delivery_payload(
                    story_id=sent_story,
                    lease_token="sent-token",
                    translation_status="translated",
                    delivery_payload=payload,
                    payload_fingerprint="3" * 64,
                    now_ms=BASE_MS + 3,
                )
                is not None
            )
            _assert_push_summary_matches_ledger(conn)
            assert (
                repository.start_push_delivery_attempt(
                    story_id=sent_story,
                    lease_token="sent-token",
                    now_ms=BASE_MS + 4,
                )
                is not None
            )
            assert not repository.complete_push_delivery(
                story_id=sent_story,
                lease_token="wrong-token",
                receipt={},
                now_ms=BASE_MS + 5,
            )
            assert repository.complete_push_delivery(
                story_id=sent_story,
                lease_token="sent-token",
                receipt={},
                now_ms=BASE_MS + 6,
            )
            _assert_push_summary_matches_ledger(conn)

            retry_story = insert("4", now_ms=BASE_MS + 10)
            claim(retry_story, "retry-token-1", now_ms=BASE_MS + 11)
            assert (
                repository.freeze_push_delivery_payload(
                    story_id=retry_story,
                    lease_token="retry-token-1",
                    translation_status="unavailable",
                    delivery_payload=payload,
                    payload_fingerprint="4" * 64,
                    now_ms=BASE_MS + 12,
                )
                is not None
            )
            assert (
                repository.start_push_delivery_attempt(
                    story_id=retry_story,
                    lease_token="retry-token-1",
                    now_ms=BASE_MS + 13,
                )
                is not None
            )
            assert (
                repository.fail_push_delivery(
                    story_id=retry_story,
                    lease_token="retry-token-1",
                    error_code="feishu_503",
                    retryable=True,
                    next_attempt_at_ms=BASE_MS + 20,
                    max_attempts=6,
                    now_ms=BASE_MS + 14,
                )
                == "retry_wait"
            )
            _assert_push_summary_matches_ledger(conn)
            claim(retry_story, "retry-token-2", now_ms=BASE_MS + 20)
            assert (
                repository.start_push_delivery_attempt(
                    story_id=retry_story,
                    lease_token="retry-token-2",
                    now_ms=BASE_MS + 21,
                )
                is not None
            )
            assert (
                repository.fail_push_delivery(
                    story_id=retry_story,
                    lease_token="retry-token-2",
                    error_code="Secret URL https://example.test",
                    retryable=False,
                    next_attempt_at_ms=BASE_MS + 30,
                    max_attempts=6,
                    now_ms=BASE_MS + 22,
                )
                == "terminal"
            )
            _assert_push_summary_matches_ledger(conn)

            claimed_story = insert("5", now_ms=BASE_MS + 30)
            claim(claimed_story, "claimed-token", now_ms=BASE_MS + 31)
            assert repository.suppress_claimed_push_delivery(
                story_id=claimed_story,
                lease_token="claimed-token",
                now_ms=BASE_MS + 32,
            )
            _assert_push_summary_matches_ledger(conn)

            prepared_story = insert("6", now_ms=BASE_MS + 40)
            claim(prepared_story, "prepared-token", now_ms=BASE_MS + 41)
            assert repository.mark_push_translation_attempted(
                story_id=prepared_story,
                lease_token="prepared-token",
                attempted_at_ms=BASE_MS + 42,
            )
            assert repository.suppress_prepared_push_delivery(
                story_id=prepared_story,
                lease_token="prepared-token",
                translation_status="translated",
                delivery_payload=payload,
                payload_fingerprint="6" * 64,
                now_ms=BASE_MS + 43,
            )
            _assert_push_summary_matches_ledger(conn)

            unsubmitted_story = insert("7", now_ms=BASE_MS + 50)
            claim(unsubmitted_story, "unsubmitted-token", now_ms=BASE_MS + 51)
            assert (
                repository.freeze_push_delivery_payload(
                    story_id=unsubmitted_story,
                    lease_token="unsubmitted-token",
                    translation_status="not_needed",
                    delivery_payload=payload,
                    payload_fingerprint="7" * 64,
                    now_ms=BASE_MS + 52,
                )
                is not None
            )
            assert (
                repository.start_push_delivery_attempt(
                    story_id=unsubmitted_story,
                    lease_token="unsubmitted-token",
                    now_ms=BASE_MS + 53,
                )
                is not None
            )
            assert repository.suppress_unsubmitted_push_delivery(
                story_id=unsubmitted_story,
                lease_token="unsubmitted-token",
                now_ms=BASE_MS + 54,
            )
            _assert_push_summary_matches_ledger(conn)

            render_story = insert("8", now_ms=BASE_MS + 60)
            claim(render_story, "render-token", now_ms=BASE_MS + 61)
            assert repository.record_push_render_failure(
                story_id=render_story,
                lease_token="render-token",
                translation_status="not_needed",
                delivery_payload={"terminal_error": "render"},
                payload_fingerprint="8" * 64,
                error_code="news_push_render_failed",
                now_ms=BASE_MS + 62,
            )
            _assert_push_summary_matches_ledger(conn)

            for offset, story_char in enumerate(("9", "a"), start=70):
                exhausted_story = insert(story_char, now_ms=BASE_MS + offset)
                token = f"exhausted-token-{story_char}"
                claim(exhausted_story, token, now_ms=BASE_MS + offset + 1)
                assert (
                    repository.freeze_push_delivery_payload(
                        story_id=exhausted_story,
                        lease_token=token,
                        translation_status="not_needed",
                        delivery_payload=payload,
                        payload_fingerprint=story_char * 64,
                        now_ms=BASE_MS + offset + 2,
                    )
                    is not None
                )
                for attempt in range(6):
                    assert (
                        repository.start_push_delivery_attempt(
                            story_id=exhausted_story,
                            lease_token=token,
                            now_ms=BASE_MS + offset + 3 + attempt,
                        )
                        is not None
                    )
            assert (
                repository.terminalize_exhausted_push_deliveries(
                    now_ms=BASE_MS + 100,
                    max_attempts=6,
                )
                == 2
            )
            _assert_push_summary_matches_ledger(conn)

        state = _push_health(conn, now_ms=BASE_MS + 100)
        assert state["total_count"] == 8
        assert state["suppressed_count"] == 3
        assert state["pending_count"] == 0
        assert state["retry_count"] == 0
        assert state["sent_count"] == 1
        assert state["terminal_count"] == 4
        assert state["latest_sent_at_ms"] == BASE_MS + 6
        assert state["latest_error"] == "delivery_attempt_limit_exhausted"
        assert state["latest_error_at_ms"] == BASE_MS + 100
        typed = conn.execute(
            """
            SELECT translation_prompt_version, translation_attempted_at_ms,
                   translation_duration_ms, translation_fallback_code
              FROM news_push_deliveries
             WHERE story_id = %s
            """,
            (prepared_story,),
        ).fetchone()
        assert typed == {
            "translation_prompt_version": "title_zh_v2",
            "translation_attempted_at_ms": BASE_MS + 2,
            "translation_duration_ms": 1250,
            "translation_fallback_code": None,
        }
    finally:
        conn.close()


def test_story_push_contexts_select_numeric_max_then_newest_then_item_id() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        title = "Central bank holds rates steady"
        events = (
            _event("lower", title=title, score=79, published_at_ms=BASE_MS + 1),
            _event("older", title=title, score=81, published_at_ms=BASE_MS + 2),
            _event("tie-a", title=title, score=81, published_at_ms=BASE_MS + 3),
            _event("tie-b", title=title, score=81, published_at_ms=BASE_MS + 3),
            _event(
                "no-score",
                title="Volcano closes an airport runway",
                score=20,
                published_at_ms=BASE_MS + 4,
            ),
        )
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=events,
                observed_at_ms=BASE_MS + 10,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 10)
        tie_ids = [
            str(row["item_id"])
            for row in conn.execute(
                """
                SELECT item_id
                  FROM news_items
                 WHERE provider_record_id IN ('tie-a', 'tie-b')
                 ORDER BY item_id
                """
            ).fetchall()
        ]
        conn.execute(
            """
            UPDATE news_items
               SET provider_metadata = provider_metadata || %s
             WHERE provider_record_id = 'lower'
            """,
            (Jsonb({"score": "999"}),),
        )
        conn.execute("UPDATE news_items SET provider_metadata = '{}'::jsonb WHERE provider_record_id = 'no-score'")
        conn.commit()

        selected = next(iter(repository.story_push_contexts().values()))
        evidence = selected["provider_evidence"]

        assert evidence["item_id"] == tie_ids[0]
        assert evidence["provider_score"] == 81
        assert evidence["url"] == "https://example.com/news"
        assert evidence["provider_metadata"]["signal"] == "long"
        assert set(evidence) >= {"item_id", "url", "provider_metadata"}

        feed = repository.list_feed(push_enabled=False, now_ms=BASE_MS + 10)
        scored_story = next(story for story in feed["stories"] if story["title"] == title)
        no_score_story = next(
            story for story in feed["stories"] if story["title"] == "Volcano closes an airport runway"
        )
        assert scored_story["provider_evidence"] == {
            "item_id": tie_ids[0],
            "url": "https://example.com/news",
            "provider_metadata": {
                "score": 81,
                "signal": "long",
                "grade": "A",
                "assets": [{"symbol": "BTC", "market_type": "spot"}],
            },
        }
        assert "coins" not in scored_story["provider_evidence"]["provider_metadata"]
        assert set(scored_story["provider_evidence"]) == {
            "item_id",
            "url",
            "provider_metadata",
        }
        assert no_score_story["provider_evidence"] is None

        detail = repository.get_story(
            story_id=scored_story["story_id"],
            push_enabled=False,
            now_ms=BASE_MS + 10,
        )
        assert detail is not None
        assert detail["provider_evidence"] == scored_story["provider_evidence"]
    finally:
        conn.close()


def test_provider_score_clock_changes_only_when_the_numeric_score_fact_changes() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "score-clock",
                        title="Bitcoin market update",
                        score=60,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 10,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 10)

        initial = next(iter(repository.story_push_contexts().values()))["provider_evidence"]
        assert initial["threshold_observed_at_ms"] == BASE_MS + 10

        with conn.transaction():
            rebuild_news_projection(repository, now_ms=BASE_MS + 7_200_000)
        after_story_write = next(iter(repository.story_push_contexts().values()))["provider_evidence"]
        assert after_story_write["threshold_observed_at_ms"] == BASE_MS + 10

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("score-clock", score=82),),
                observed_at_ms=BASE_MS + 7_200_010,
            )
        qualified = next(iter(repository.story_push_contexts().values()))["provider_evidence"]
        assert qualified["threshold_observed_at_ms"] == BASE_MS + 7_200_010

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("score-clock", score=82),),
                observed_at_ms=BASE_MS + 7_200_020,
            )
        duplicate = next(iter(repository.story_push_contexts().values()))["provider_evidence"]
        assert duplicate["threshold_observed_at_ms"] == BASE_MS + 7_200_010
    finally:
        conn.close()


def test_selected_item_score_change_keeps_the_story_ledger_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=_Delivery())
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
        shared_title = "Central bank holds rates steady after policy meeting"
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "selected-first",
                        title=shared_title,
                        score=80,
                        published_at_ms=BASE_MS + 1,
                    ),
                    _event(
                        "selected-later",
                        title=shared_title,
                        score=79,
                        published_at_ms=BASE_MS + 2,
                    ),
                ),
                observed_at_ms=BASE_MS + 3,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 3)

        initial = next(iter(repository.story_push_contexts().values()))
        story_id = initial["story_id"]
        assert initial["provider_evidence"]["provider_score"] == 80
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS + 4)) == {
            "inserted": 1,
            "suppressed": 0,
            "terminalized": 0,
        }
        clock["now_ms"] = BASE_MS + 4
        assert asyncio.run(runtime.turn()) is True

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("selected-later", score=90),),
                observed_at_ms=BASE_MS + 5,
            )

        selected_later = repository.story_push_contexts()[story_id]
        assert selected_later["provider_evidence"]["provider_score"] == 90
        assert selected_later["push_delivery_status"] == "sent"
        detail = repository.get_story(
            story_id=story_id,
            push_enabled=True,
            now_ms=BASE_MS + 5,
        )
        assert detail is not None
        assert detail["notification"]["delivery_state"] == "sent"
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS + 6)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
    finally:
        conn.close()


def test_first_reconcile_suppresses_existing_eligible_story() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "before-enable",
                        title="Exchange launches a spot market",
                        score=90,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 2,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2)
        runtime = _runtime(conn, delivery=_Delivery())

        result = asyncio.run(runtime.reconcile(now_ms=BASE_MS + 10))
        progressed = asyncio.run(runtime.turn())

        state = conn.execute("SELECT baseline_at_ms FROM news_push_state WHERE singleton_key='current'").fetchone()
        delivery = conn.execute("SELECT status, translation_status FROM news_push_deliveries").fetchone()
        assert result == {"inserted": 1, "suppressed": 1, "terminalized": 0}
        assert state["baseline_at_ms"] == BASE_MS + 10
        assert delivery == {"status": "suppressed", "translation_status": "not_requested"}
        assert progressed is False
        health = _push_health(conn, now_ms=BASE_MS + 10)
        assert health["status"] == "ready"
        assert health["initialized"] is True
        assert health["suppressed_count"] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("asset_symbols", "should_suppress"),
    (
        ((), True),
        (("CL",), True),
        (("XYZ-CL",), True),
        (("CL", "XYZ-CL"), True),
        (("BTC",), False),
        (("GOOGL",), False),
        (("NATGAS",), False),
        (("CL", "BTC"), False),
    ),
)
def test_reconcile_admits_only_candidates_with_non_cl_family_assets(
    monkeypatch: pytest.MonkeyPatch,
    asset_symbols: tuple[str, ...],
    should_suppress: bool,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 3}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery()
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "asset-admission",
                        title="Provider reports a market-moving development",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                        asset_symbols=asset_symbols,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)

        expected_inserted = int(not should_suppress)
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS + 2)) == {
            "inserted": expected_inserted,
            "suppressed": 0,
            "terminalized": 0,
        }
        assert asyncio.run(runtime.turn()) is (not should_suppress)

        row = conn.execute("SELECT status, translation_status FROM news_push_deliveries").fetchone()
        expected_row = None if should_suppress else {"status": "sent", "translation_status": "not_needed"}
        assert row == expected_row
        assert delivery.prepare_calls == int(not should_suppress)
        assert len(delivery.payloads) == int(not should_suppress)
    finally:
        conn.close()


def test_skipped_candidate_can_qualify_after_provider_assets_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 4}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery()
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "later-assets",
                        title="Issuer updates its market outlook",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                        asset_symbols=(),
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)

        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS + 2)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
        assert conn.execute("SELECT count(*) AS count FROM news_push_deliveries").fetchone()["count"] == 0

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _annotation(
                        "later-assets",
                        score=88,
                        asset_symbols=("BTC",),
                    ),
                ),
                observed_at_ms=BASE_MS + 3,
            )

        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS + 4)) == {
            "inserted": 1,
            "suppressed": 0,
            "terminalized": 0,
        }
        assert asyncio.run(runtime.turn()) is True
        assert conn.execute("SELECT status FROM news_push_deliveries").fetchone()["status"] == "sent"
        assert delivery.prepare_calls == 1
        assert len(delivery.payloads) == 1
    finally:
        conn.close()


@pytest.mark.parametrize("asset_symbols", ((), ("CL", "XYZ-CL")))
def test_existing_unfrozen_delivery_with_filtered_assets_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    asset_symbols: tuple[str, ...],
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 3}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery()
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "existing-unfrozen",
                        title="Provider publishes a market alert",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS + 2))["inserted"] == 1
        with conn.transaction():
            if asset_symbols:
                repository.record_opennews_events(
                    source=opennews_source(),
                    events=(
                        _annotation(
                            "existing-unfrozen",
                            score=88,
                            asset_symbols=asset_symbols,
                        ),
                    ),
                    observed_at_ms=BASE_MS + 3,
                )
            else:
                conn.execute(
                    """
                    UPDATE news_items
                       SET provider_metadata = provider_metadata - 'coins'
                     WHERE provider_record_id = 'existing-unfrozen'
                    """
                )

        assert asyncio.run(runtime.turn()) is True
        assert conn.execute("SELECT status, translation_status FROM news_push_deliveries").fetchone() == {
            "status": "suppressed",
            "translation_status": "not_requested",
        }
        assert delivery.prepare_calls == 0
        assert delivery.payloads == []
        assert asyncio.run(runtime.turn()) is False
    finally:
        conn.close()


def test_existing_frozen_retry_with_cl_family_only_assets_is_suppressed_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 3}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery(fail_first=True)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "existing-frozen-retry",
                        title="Provider publishes a second market alert",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS + 2))
        assert asyncio.run(runtime.turn()) is True
        assert conn.execute("SELECT status, delivery_attempts FROM news_push_deliveries").fetchone() == {
            "status": "retry_wait",
            "delivery_attempts": 1,
        }
        assert delivery.prepare_calls == 1
        assert len(delivery.payloads) == 1

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _annotation(
                        "existing-frozen-retry",
                        score=88,
                        asset_symbols=("CL", "XYZ-CL"),
                    ),
                ),
                observed_at_ms=BASE_MS + 3,
            )
        clock["now_ms"] += 5_000

        assert asyncio.run(runtime.turn()) is True
        assert conn.execute(
            """
            SELECT status, translation_status, delivery_attempts,
                   delivery_payload IS NOT NULL AS has_delivery_payload
              FROM news_push_deliveries
            """
        ).fetchone() == {
            "status": "suppressed",
            "translation_status": "not_needed",
            "delivery_attempts": 1,
            "has_delivery_payload": True,
        }
        assert delivery.prepare_calls == 1
        assert len(delivery.payloads) == 1
        assert asyncio.run(runtime.turn()) is False
    finally:
        conn.close()


def test_late_recovery_score_does_not_push_a_stale_article() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=_Delivery())
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "late-recovery-score",
                        title="Issuer files for a spot exchange product",
                        score=70,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)

        recovered_at_ms = BASE_MS + push_module.PUSH_SOURCE_FRESHNESS_MS + 2
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("late-recovery-score", score=88),),
                observed_at_ms=recovered_at_ms,
            )

        assert asyncio.run(runtime.reconcile(now_ms=recovered_at_ms)) == {
            "inserted": 1,
            "suppressed": 1,
            "terminalized": 0,
        }
        row = conn.execute("SELECT status, translation_status FROM news_push_deliveries").fetchone()
        assert row == {"status": "suppressed", "translation_status": "not_requested"}
        assert asyncio.run(runtime.turn()) is False
    finally:
        conn.close()


def test_unfrozen_candidate_that_ages_out_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery()
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "candidate-ages-out",
                        title="Regulator approves a spot exchange product",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        assert asyncio.run(runtime.reconcile(now_ms=clock["now_ms"])) == {
            "inserted": 1,
            "suppressed": 0,
            "terminalized": 0,
        }

        clock["now_ms"] = BASE_MS + push_module.PUSH_SOURCE_FRESHNESS_MS + 2
        assert asyncio.run(runtime.turn())

        row = conn.execute(
            """
            SELECT status, translation_status, delivery_attempts,
                   delivery_payload, payload_fingerprint
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row == {
            "status": "suppressed",
            "translation_status": "not_requested",
            "delivery_attempts": 0,
            "delivery_payload": None,
            "payload_fingerprint": None,
        }
        assert delivery.prepare_calls == 0
        assert delivery.payloads == []
    finally:
        conn.close()


def test_push_retries_frozen_prepared_payload_outside_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery(fail_first=True, translation_status="translated")
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)

        # A zero-candidate baseline is still durable. The exact boundary score
        # remains ineligible until a later provider update crosses it.
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "after-enable",
                        title="Issuer files for a new exchange product",
                        score=70,
                        published_at_ms=BASE_MS + 1,
                        url=None,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS + 2))
        assert asyncio.run(runtime.turn()) is False
        assert conn.execute("SELECT count(*) AS count FROM news_push_deliveries").fetchone()["count"] == 0

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("after-enable", score=71),),
                observed_at_ms=BASE_MS + 3,
            )
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))
        assert asyncio.run(runtime.turn())

        first = conn.execute(
            """
            SELECT story_id, status, translation_status, delivery_attempts,
                   delivery_payload, payload_fingerprint, next_attempt_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
        assert first["status"] == "retry_wait"
        assert first["translation_status"] == "translated"
        assert first["delivery_attempts"] == 1
        assert first["delivery_payload"]["card"]["original_title"] == ("Issuer files for a new exchange product")
        assert first["delivery_payload"]["card"]["url"] is None
        assert first["delivery_payload"]["card"]["provider_evidence"]["score"] == 71
        retry_health = _push_health(conn, now_ms=clock["now_ms"])
        assert retry_health["status"] == "degraded"
        assert retry_health["retry_count"] == 1
        frozen_payload = first["delivery_payload"]
        frozen_fingerprint = first["payload_fingerprint"]

        clock["now_ms"] = int(first["next_attempt_at_ms"]) + 1
        assert asyncio.run(runtime.turn())

        completed = conn.execute(
            """
            SELECT status, delivery_attempts, delivery_payload,
                   payload_fingerprint, receipt, sent_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
        assert completed["status"] == "sent"
        assert completed["delivery_attempts"] == 2
        assert completed["delivery_payload"] == frozen_payload
        assert completed["payload_fingerprint"] == frozen_fingerprint
        assert completed["receipt"]["provider"] == "feishu"
        assert completed["sent_at_ms"] == clock["now_ms"]
        assert delivery.prepare_calls == 1
        assert delivery.payloads == [frozen_payload, frozen_payload]
        assert delivery.idempotency_keys == [first["story_id"], first["story_id"]]
        completed_health = _push_health(conn, now_ms=clock["now_ms"])
        assert completed_health["status"] == "ready"
        assert completed_health["sent_count"] == 1

        # A later score update cannot create or resend the same current Story.
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("after-enable", score=99),),
                observed_at_ms=clock["now_ms"] + 1,
            )
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"] + 1))
        assert asyncio.run(runtime.turn()) is False
        assert conn.execute("SELECT count(*) AS count FROM news_push_deliveries").fetchone()["count"] == 1
    finally:
        conn.close()


def test_frozen_retry_that_ages_out_is_suppressed_before_network_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery(fail_first=True, translation_status="translated")
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "retry-ages-out",
                        title="Issuer files for a new exchange product",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))

        assert asyncio.run(runtime.turn()) is True
        retry = conn.execute(
            """
            SELECT status, delivery_attempts, delivery_payload,
                   payload_fingerprint, last_error
              FROM news_push_deliveries
            """
        ).fetchone()
        assert retry["status"] == "retry_wait"
        assert retry["delivery_attempts"] == 1
        assert retry["last_error"] == "feishu_503"

        clock["now_ms"] = BASE_MS + push_module.PUSH_SOURCE_FRESHNESS_MS + 2
        assert asyncio.run(runtime.turn()) is True

        suppressed = conn.execute(
            """
            SELECT status, translation_status, delivery_attempts,
                   delivery_payload, payload_fingerprint, next_attempt_at_ms,
                   lease_owner, lease_token, lease_expires_at_ms, last_error
              FROM news_push_deliveries
            """
        ).fetchone()
        assert suppressed == {
            "status": "suppressed",
            "translation_status": "translated",
            "delivery_attempts": 1,
            "delivery_payload": retry["delivery_payload"],
            "payload_fingerprint": retry["payload_fingerprint"],
            "next_attempt_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
            "last_error": None,
        }
        assert delivery.prepare_calls == 1
        assert delivery.payloads == [retry["delivery_payload"]]
    finally:
        conn.close()


def test_candidate_that_ages_out_during_prepare_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])

    def cross_freshness_boundary() -> None:
        clock["now_ms"] = BASE_MS + push_module.PUSH_SOURCE_FRESHNESS_MS + 2

    delivery = _Delivery(
        on_prepare=cross_freshness_boundary,
        translation_status="translated",
    )
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "prepare-ages-out",
                        title="Issuer files for a spot exchange product",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS + 100))

        assert asyncio.run(runtime.turn())

        row = conn.execute(
            """
            SELECT status, translation_status, delivery_payload,
                   payload_fingerprint, delivery_attempts
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row == {
            "status": "suppressed",
            "translation_status": "not_requested",
            "delivery_payload": None,
            "payload_fingerprint": None,
            "delivery_attempts": 0,
        }
        assert delivery.prepare_calls == 1
        assert delivery.payloads == []
    finally:
        conn.close()


def test_cancelled_prepare_releases_unfrozen_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery(prepare_error=asyncio.CancelledError())
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "cancelled-prepare",
                        title="Exchange lists a new spot asset",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runtime.turn())

        row = conn.execute(
            """
            SELECT status, translation_status, delivery_payload,
                   lease_owner, lease_token, lease_expires_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row == {
            "status": "pending_translation",
            "translation_status": "pending",
            "delivery_payload": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        }
    finally:
        conn.close()


def test_cancelled_translation_dispatch_is_fenced_and_restart_uses_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])

    def cancel_after_fence() -> None:
        clock["now_ms"] += 50

    interrupted = _Delivery(
        simulate_translation_dispatch=True,
        cancel_after_translation_dispatch=True,
        on_translation_dispatch=cancel_after_fence,
    )
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=interrupted, runtime_id="before-restart")
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "translation-fence-restart",
                        title="Bitcoin ETF sees fresh inflows",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runtime.turn())

        fenced = conn.execute(
            """
            SELECT status, translation_status, delivery_payload, updated_at_ms,
                   lease_owner, lease_token, lease_expires_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
        attempted_at_ms = BASE_MS + 100
        assert fenced == {
            "status": "pending_translation",
            "translation_status": "attempted",
            "delivery_payload": None,
            "updated_at_ms": attempted_at_ms,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        }
        assert interrupted.translation_dispatches == 1

        restarted = _Delivery()
        recovered = _runtime(conn, delivery=restarted, runtime_id="after-restart")
        assert asyncio.run(recovered.turn()) is True

        sent = conn.execute(
            """
            SELECT status, translation_status, delivery_attempts,
                   delivery_payload, receipt
              FROM news_push_deliveries
            """
        ).fetchone()
        assert sent["status"] == "sent"
        assert sent["translation_status"] == "unavailable"
        assert sent["delivery_attempts"] == 1
        assert sent["receipt"]["provider"] == "feishu"
        assert sent["delivery_payload"]["presentation"] == {
            "prompt_version": "title_zh_v2",
            "fallback_code": "news_push_translation_interrupted_after_dispatch",
            "translation_attempted_at_ms": attempted_at_ms,
            "translation_duration_ms": None,
        }
        assert restarted.interrupted_translation_attempts == [attempted_at_ms]
        assert restarted.translation_dispatches == 0
        health = _push_health(conn, now_ms=clock["now_ms"] + 1)
        assert health["translation_24h"] == {
            "attempted": 1,
            "succeeded": 0,
            "success_ratio": 0.0,
            "latency_p95_ms": None,
            "failure_counts": {"news_push_translation_interrupted_after_dispatch": 1},
            "slo_met": False,
            "sample_complete": True,
        }
    finally:
        conn.close()


def test_completed_translation_that_ages_out_keeps_frozen_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])

    def cross_freshness_boundary() -> None:
        clock["now_ms"] = BASE_MS + push_module.PUSH_SOURCE_FRESHNESS_MS + 2

    delivery = _Delivery(
        translation_status="translated",
        simulate_translation_dispatch=True,
        on_prepare=cross_freshness_boundary,
    )
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "translated-then-stale",
                        title="Bitcoin ETF sees fresh inflows",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS + 100))

        assert asyncio.run(runtime.turn()) is True

        row = conn.execute(
            """
            SELECT status, translation_status, delivery_attempts,
                   delivery_payload, payload_fingerprint
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row["status"] == "suppressed"
        assert row["translation_status"] == "translated"
        assert row["delivery_attempts"] == 0
        assert row["delivery_payload"]["presentation"] == {
            "prompt_version": "title_zh_v2",
            "fallback_code": None,
            "translation_attempted_at_ms": BASE_MS + 100,
            "translation_duration_ms": 1,
        }
        assert row["payload_fingerprint"] is not None
        assert delivery.translation_dispatches == 1
        assert delivery.payloads == []
    finally:
        conn.close()


def test_restart_reconcile_releases_an_old_fenced_translation_lease() -> None:
    conn = connect_postgres_test(read_only=False)
    attempted_at_ms = BASE_MS + 100
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            assert repository.insert_push_candidate(
                story_id="a" * 64,
                selected_item_id="old-runtime-item",
                provider_score=88,
                threshold_observed_at_ms=BASE_MS,
                source_payload={},
                suppressed=False,
                now_ms=BASE_MS,
            )
            conn.execute(
                """
                UPDATE news_push_deliveries
                   SET translation_status = 'attempted',
                       next_attempt_at_ms = %s,
                       lease_owner = 'news_story_push:old-runtime',
                       lease_token = 'old-lease-token',
                       lease_expires_at_ms = %s,
                       updated_at_ms = %s
                 WHERE story_id = %s
                """,
                (attempted_at_ms, attempted_at_ms + 60_000, attempted_at_ms, "a" * 64),
            )
        runtime = _runtime(conn, delivery=_Delivery(), runtime_id="new-runtime")

        assert asyncio.run(runtime.reconcile(now_ms=attempted_at_ms + 1)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }

        row = conn.execute(
            """
            SELECT translation_status, next_attempt_at_ms, updated_at_ms,
                   lease_owner, lease_token, lease_expires_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row == {
            "translation_status": "attempted",
            "next_attempt_at_ms": attempted_at_ms + 1,
            "updated_at_ms": attempted_at_ms,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        }
    finally:
        conn.close()


def test_story_identity_change_does_not_requalify_the_same_selected_item() -> None:
    conn = connect_postgres_test(read_only=False)
    hour_ms = 60 * 60 * 1000
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        earliest_title = "Fed holds interest rates steady amid inflation concerns"
        selected_title = "Fed holds rates steady as inflation concerns persist"
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "expiring-earliest",
                        title=earliest_title,
                        score=60,
                        published_at_ms=BASE_MS - (11 * hour_ms),
                    ),
                    _event(
                        "retained-highest",
                        title=selected_title,
                        score=91,
                        published_at_ms=BASE_MS - (10 * hour_ms),
                    ),
                ),
                observed_at_ms=BASE_MS,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS)

        initial_evidence = repository.story_push_contexts()
        assert len(initial_evidence) == 1
        original_story_id, original = next(iter(initial_evidence.items()))
        selected_item_id = original["provider_evidence"]["item_id"]
        assert original["provider_evidence"]["title"] == selected_title

        runtime = _runtime(conn, delivery=_Delivery())
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS)) == {
            "inserted": 1,
            "suppressed": 1,
            "terminalized": 0,
        }

        # The 12-hour window drops only the canonical earliest member. The
        # already-selected high-score Item stays current under a new Story ID.
        after_expiry_ms = BASE_MS + (2 * hour_ms)
        with conn.transaction():
            rebuild_news_projection(repository, now_ms=after_expiry_ms)
        current_evidence = repository.story_push_contexts()
        assert len(current_evidence) == 1
        new_story_id, current = next(iter(current_evidence.items()))
        assert new_story_id != original_story_id
        assert current["provider_evidence"]["item_id"] == selected_item_id
        assert current["provider_evidence"]["title"] == selected_title

        assert asyncio.run(runtime.reconcile(now_ms=after_expiry_ms)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
        rows = conn.execute(
            """
            SELECT story_id, status, translation_status
              FROM news_push_deliveries
             ORDER BY story_id
            """
        ).fetchall()
        assert [dict(row) for row in rows] == [
            {
                "story_id": original_story_id,
                "status": "suppressed",
                "translation_status": "not_requested",
            }
        ]
        assert asyncio.run(runtime.turn()) is False
    finally:
        conn.close()


def test_sent_item_is_not_resent_when_its_story_identity_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    hour_ms = 60 * 60 * 1000
    clock = {"now_ms": BASE_MS}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery()
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }

        first_projection_ms = BASE_MS + 60_000
        earliest_published_ms = BASE_MS - (12 * hour_ms) + 90_000
        selected_title = "Fed holds rates steady as inflation concerns persist"
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "soon-expiring-earliest",
                        title="Fed holds interest rates steady amid inflation concerns",
                        score=60,
                        published_at_ms=earliest_published_ms,
                    ),
                    _event(
                        "fresh-selected",
                        title=selected_title,
                        score=91,
                        published_at_ms=first_projection_ms,
                    ),
                ),
                observed_at_ms=first_projection_ms,
            )
            rebuild_news_projection(repository, now_ms=first_projection_ms)

        initial_evidence = repository.story_push_contexts()
        assert len(initial_evidence) == 1
        original_story_id, original = next(iter(initial_evidence.items()))
        selected_item_id = original["provider_evidence"]["item_id"]

        clock["now_ms"] = first_projection_ms
        assert asyncio.run(runtime.reconcile(now_ms=clock["now_ms"])) == {
            "inserted": 1,
            "suppressed": 0,
            "terminalized": 0,
        }
        assert asyncio.run(runtime.turn()) is True
        assert len(delivery.payloads) == 1

        # Sixty-one seconds later only the canonical member crosses the exact
        # 12-hour boundary. The fresh selected Article acquires a new Story ID.
        clock["now_ms"] = BASE_MS + 121_000
        with conn.transaction():
            rebuild_news_projection(repository, now_ms=clock["now_ms"])
        current_evidence = repository.story_push_contexts()
        assert len(current_evidence) == 1
        new_story_id, current = next(iter(current_evidence.items()))
        assert new_story_id != original_story_id
        assert current["provider_evidence"]["item_id"] == selected_item_id

        assert asyncio.run(runtime.reconcile(now_ms=clock["now_ms"])) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
        assert asyncio.run(runtime.turn()) is False
        assert len(delivery.payloads) == 1
        row = conn.execute(
            """
            SELECT story_id, selected_item_id, status, delivery_attempts
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row == {
            "story_id": original_story_id,
            "selected_item_id": selected_item_id,
            "status": "sent",
            "delivery_attempts": 1,
        }
    finally:
        conn.close()


def test_restarts_catch_up_persisted_render_and_frozen_retry_without_rerendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    initial_delivery = _Delivery()
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        initial_runtime = _runtime(
            conn,
            delivery=initial_delivery,
            runtime_id="before-restart",
        )
        assert asyncio.run(initial_runtime.reconcile(now_ms=BASE_MS)) == {
            "inserted": 0,
            "suppressed": 0,
            "terminalized": 0,
        }
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "restart-catch-up",
                        title="Regulator approves a new spot exchange product",
                        score=88,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(initial_runtime.reconcile(now_ms=clock["now_ms"]))
        persisted_pending = conn.execute(
            """
            SELECT story_id, status, translation_status
              FROM news_push_deliveries
            """
        ).fetchone()
        assert persisted_pending["status"] == "pending_translation"
        assert persisted_pending["translation_status"] == "pending"
        assert initial_delivery.prepare_calls == 0

        # The runtime that established the candidate can disappear before it
        # executes. A fresh instance discovers and renders that durable row.
        pending_delivery = _Delivery(fail_first=True)
        pending_runtime = _runtime(
            conn,
            delivery=pending_delivery,
            runtime_id="pending-catch-up",
        )
        assert asyncio.run(pending_runtime.turn())

        retry = conn.execute(
            """
            SELECT story_id, status, translation_status, delivery_attempts,
                   next_attempt_at_ms, delivery_payload, payload_fingerprint
              FROM news_push_deliveries
            """
        ).fetchone()
        assert retry["status"] == "retry_wait"
        assert retry["translation_status"] == "not_needed"
        assert retry["delivery_payload"]["card"]["original_title"] == ("Regulator approves a new spot exchange product")
        frozen_payload = retry["delivery_payload"]
        frozen_fingerprint = retry["payload_fingerprint"]
        assert pending_delivery.prepare_calls == 1

        # A fresh runtime has no in-memory state from the first attempt. It
        # discovers the durable retry and delivers the already-rendered card.
        restarted_delivery = _Delivery()
        restarted_runtime = _runtime(
            conn,
            delivery=restarted_delivery,
            runtime_id="after-restart",
        )
        clock["now_ms"] = int(retry["next_attempt_at_ms"]) + 1
        assert asyncio.run(restarted_runtime.turn())

        sent = conn.execute(
            """
            SELECT status, translation_status, delivery_attempts,
                   delivery_payload, payload_fingerprint, receipt
              FROM news_push_deliveries
            """
        ).fetchone()
        assert sent["status"] == "sent"
        assert sent["translation_status"] == "not_needed"
        assert sent["delivery_attempts"] == 2
        assert sent["delivery_payload"] == frozen_payload
        assert sent["payload_fingerprint"] == frozen_fingerprint
        assert sent["receipt"]["provider"] == "feishu"
        assert restarted_delivery.prepare_calls == 0
        assert restarted_delivery.payloads == [frozen_payload]
    finally:
        conn.close()


def test_chinese_title_is_delivered_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 10}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery()
    title = "央行维持利率不变"
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "chinese-title",
                        title=title,
                        score=86,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))
        assert asyncio.run(runtime.turn())

        row = conn.execute(
            """
            SELECT status, translation_status, delivery_payload
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row["status"] == "sent"
        assert row["translation_status"] == "not_needed"
        assert row["delivery_payload"]["card"]["original_title"] == title
        assert delivery.prepare_calls == 1
    finally:
        conn.close()


def test_japanese_title_is_delivered_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 10}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery()
    title = "日本銀行が金利を引き上げる"
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "japanese-title",
                        title=title,
                        score=86,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))
        assert asyncio.run(runtime.turn())

        row = conn.execute(
            """
            SELECT status, translation_status, delivery_payload
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row["status"] == "sent"
        assert row["translation_status"] == "not_needed"
        assert row["delivery_payload"]["card"]["original_title"] == title
        assert delivery.prepare_calls == 1
    finally:
        conn.close()


def test_non_retryable_delivery_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 10}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    delivery = _Delivery(error=NewsPushDeliveryError("feishu_signature_rejected", retryable=False))
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, delivery=delivery)
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "terminal-delivery",
                        title="Exchange rejects an invalid signed request",
                        score=82,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))
        assert asyncio.run(runtime.turn())

        row = conn.execute(
            """
            SELECT status, delivery_attempts, next_attempt_at_ms, receipt,
                   sent_at_ms, lease_owner, lease_token,
                   lease_expires_at_ms, last_error
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row == {
            "status": "terminal",
            "delivery_attempts": 1,
            "next_attempt_at_ms": None,
            "receipt": None,
            "sent_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
            "last_error": "feishu_signature_rejected",
        }
        assert asyncio.run(runtime.turn()) is False
        health = _push_health(conn, now_ms=clock["now_ms"] + 1)
        assert health["status"] == "degraded"
        assert health["terminal_count"] == 1
    finally:
        conn.close()


def test_stale_claim_cannot_mark_delivery_sent_after_lease_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 10}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    stolen: dict[str, str] = {}

    def replace_lease_before_stale_completion() -> None:
        row = conn.execute(
            """
            SELECT story_id, lease_token
              FROM news_push_deliveries
             WHERE status = 'pending_delivery'
            """
        ).fetchone()
        assert row is not None
        stolen["original_lease_token"] = str(row["lease_token"])
        conn.execute(
            """
            UPDATE news_push_deliveries
               SET lease_owner = 'replacement-runtime',
                   lease_token = 'replacement-lease-token',
                   lease_expires_at_ms = %s
             WHERE story_id = %s AND lease_token = %s
            """,
            (
                clock["now_ms"] + 60_000,
                row["story_id"],
                row["lease_token"],
            ),
        )
        conn.commit()

    delivery = _Delivery(on_deliver=replace_lease_before_stale_completion)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(
            conn,
            delivery=delivery,
            runtime_id="stale-runtime",
        )
        asyncio.run(runtime.reconcile(now_ms=BASE_MS))
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "fenced-completion",
                        title="Regulator clears a market structure proposal",
                        score=84,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 1,
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 1)
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))
        # The external provider returned success, but another owner replaced
        # the lease before completion. The stale completion must be fenced.
        assert asyncio.run(runtime.turn()) is False
        row = conn.execute(
            """
            SELECT status, delivery_attempts, receipt, sent_at_ms,
                   lease_owner, lease_token, lease_expires_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row == {
            "status": "pending_delivery",
            "delivery_attempts": 1,
            "receipt": None,
            "sent_at_ms": None,
            "lease_owner": "replacement-runtime",
            "lease_token": "replacement-lease-token",
            "lease_expires_at_ms": clock["now_ms"] + 60_000,
        }
        assert stolen["original_lease_token"] != row["lease_token"]
        assert delivery.prepare_calls == 1
        assert len(delivery.payloads) == 1
    finally:
        conn.close()


class _DirectDatabase:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def run_business(
        self,
        _operation_name: str,
        function: Any,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        kwargs.pop("operation_timeout_seconds")
        return function(*args, **kwargs)

    @contextmanager
    def worker_session(self, _operation_name: str, _timeout_seconds: float) -> Iterator[Any]:
        try:
            yield repositories_for_connection(self.conn)
        finally:
            if self.conn.in_transaction:
                self.conn.commit()


class _InlineCapability:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def run(
        self,
        _operation_name: str,
        function: Any,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        kwargs.pop("timeout_seconds")
        before_submit = kwargs.pop("before_submit", None)
        on_submitted = kwargs.pop("on_submitted", None)
        assert not self.conn.in_transaction
        if before_submit is not None:
            await before_submit()
        if on_submitted is not None:
            on_submitted()
        return function(*args, **kwargs)


class _Delivery:
    def __init__(
        self,
        *,
        fail_first: bool = False,
        error: NewsPushDeliveryError | None = None,
        on_deliver: Callable[[], None] | None = None,
        on_prepare: Callable[[], None] | None = None,
        translation_status: str = "not_needed",
        prepare_error: BaseException | None = None,
        simulate_translation_dispatch: bool = False,
        cancel_after_translation_dispatch: bool = False,
        on_translation_dispatch: Callable[[], None] | None = None,
    ) -> None:
        self.fail_first = fail_first
        self.error = error
        self.on_deliver = on_deliver
        self.on_prepare = on_prepare
        self.translation_status = translation_status
        self.prepare_error = prepare_error
        self.simulate_translation_dispatch = simulate_translation_dispatch
        self.cancel_after_translation_dispatch = cancel_after_translation_dispatch
        self.on_translation_dispatch = on_translation_dispatch
        self.prepare_calls = 0
        self.translation_dispatches = 0
        self.interrupted_translation_attempts: list[int] = []
        self.payloads: list[dict[str, Any]] = []
        self.idempotency_keys: list[str] = []

    async def prepare(
        self,
        source_payload: Mapping[str, Any],
        *,
        deadline_ms: int,
        before_translation_submit: Callable[[], Awaitable[None]] | None = None,
        interrupted_translation_attempted_at_ms: int | None = None,
    ) -> PreparedNewsPush:
        assert deadline_ms > int(source_payload["provider_evidence"]["published_at_ms"])
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        attempted_at_ms: int | None = None
        fallback_code: str | None = None
        translation_status = self.translation_status
        if interrupted_translation_attempted_at_ms is not None:
            self.interrupted_translation_attempts.append(interrupted_translation_attempted_at_ms)
            attempted_at_ms = interrupted_translation_attempted_at_ms
            fallback_code = "news_push_translation_interrupted_after_dispatch"
            translation_status = "unavailable"
        elif self.simulate_translation_dispatch:
            assert before_translation_submit is not None
            attempted_at_ms = push_module._now_ms()
            await before_translation_submit()
            self.translation_dispatches += 1
            if self.on_translation_dispatch is not None:
                self.on_translation_dispatch()
            if self.cancel_after_translation_dispatch:
                raise asyncio.CancelledError
        if self.on_prepare is not None:
            self.on_prepare()
        evidence = dict(source_payload["provider_evidence"])
        metadata = dict(evidence["provider_metadata"])
        payload: dict[str, Any] = {
            "channel": "feishu",
            "card": {
                "original_title": evidence["title"],
                "url": evidence["url"],
                "provider_evidence": {
                    "item_id": evidence["item_id"],
                    "score": evidence["provider_score"],
                    "source": metadata.get("source"),
                    "signal": metadata.get("signal"),
                    "grade": metadata.get("grade"),
                    "coins": list(metadata.get("coins") or []),
                },
                "tracefold_story": dict(source_payload["tracefold_story"]),
            },
        }
        if attempted_at_ms is not None:
            payload["presentation"] = {
                "prompt_version": "title_zh_v2",
                "fallback_code": fallback_code,
                "translation_attempted_at_ms": attempted_at_ms,
                "translation_duration_ms": (None if fallback_code is not None else 1),
            }
        return PreparedNewsPush(
            payload=payload,
            translation_status=translation_status,
        )

    def deliver(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> NewsPushReceipt:
        self.payloads.append(dict(payload))
        self.idempotency_keys.append(idempotency_key)
        if self.fail_first and len(self.payloads) == 1:
            raise NewsPushDeliveryError("feishu_503", retryable=True)
        if self.error is not None:
            raise self.error
        if self.on_deliver is not None:
            self.on_deliver()
        return NewsPushReceipt(
            provider="feishu",
            receipt_id=f"receipt-{len(self.payloads)}",
        )


def _runtime(
    conn: Any,
    *,
    delivery: _Delivery,
    runtime_id: str = "test-runtime",
) -> NewsStoryPush:
    capability = _InlineCapability(conn)
    return NewsStoryPush(
        db=_DirectDatabase(conn),
        finite_operations=capability,
        delivery=delivery,
        runtime_id=runtime_id,
    )
