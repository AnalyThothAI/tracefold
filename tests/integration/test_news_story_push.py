from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tracefold.app.repositories import repositories_for_connection
from tracefold.news import (
    NewsInterface,
    NewsPushDeliveryError,
    NewsPushReceipt,
    NewsRepository,
    NewsStoryPush,
    PreparedNewsPush,
    opennews_source,
    parse_opennews_message,
)
from tracefold.news import push as push_module

BASE_MS = 1_785_560_400_000


def _event(
    record_id: str,
    *,
    title: str,
    score: float,
    published_at_ms: int,
    url: str | None = "https://example.com/news",
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
                "coins": [{"symbol": "BTC", "market_type": "spot"}],
            },
        }
    )
    assert event is not None
    return event


def _annotation(record_id: str, *, score: float):
    event = parse_opennews_message(
        {
            "method": "news.ai_update",
            "params": {
                "newsId": record_id,
                "engineType": "news",
                "score": score,
                "signal": "long",
                "grade": "A+",
            },
        }
    )
    assert event is not None
    return event


def _seed_source(conn: Any) -> NewsRepository:
    repository = NewsRepository(conn)
    with conn.transaction():
        repository.sync_source(opennews_source(), now_ms=BASE_MS)
    return repository


def test_story_provider_evidence_selects_numeric_max_then_newest_then_item_id() -> None:
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
            repository.rebuild_stories(now_ms=BASE_MS + 10)
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

        selected = next(iter(repository.story_provider_evidence().values()))
        evidence = selected["provider_evidence"]

        assert evidence["item_id"] == tie_ids[0]
        assert evidence["provider_score"] == 81
        assert evidence["url"] == "https://example.com/news"
        assert evidence["provider_metadata"]["signal"] == "long"
        assert set(evidence) >= {"item_id", "url", "provider_metadata"}

        feed = NewsInterface(repository).get_feed()
        scored_story = next(story for story in feed["stories"] if story["title"] == title)
        no_score_story = next(
            story for story in feed["stories"] if story["title"] == "Volcano closes an airport runway"
        )
        assert scored_story["provider_evidence"] == {
            "item_id": tie_ids[0],
            "url": "https://example.com/news",
            "provider_metadata": evidence["provider_metadata"],
        }
        assert set(scored_story["provider_evidence"]) == {
            "item_id",
            "url",
            "provider_metadata",
        }
        assert no_score_story["provider_evidence"] is None

        detail = NewsInterface(repository).get_story(story_id=scored_story["story_id"])
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
            repository.rebuild_stories(now_ms=BASE_MS + 10)

        initial = next(iter(repository.story_provider_evidence().values()))["provider_evidence"]
        assert initial["threshold_observed_at_ms"] == BASE_MS + 10

        with conn.transaction():
            repository.rebuild_stories(now_ms=BASE_MS + 7_200_000)
        after_story_write = next(iter(repository.story_provider_evidence().values()))["provider_evidence"]
        assert after_story_write["threshold_observed_at_ms"] == BASE_MS + 10

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("score-clock", score=82),),
                observed_at_ms=BASE_MS + 7_200_010,
            )
        qualified = next(iter(repository.story_provider_evidence().values()))["provider_evidence"]
        assert qualified["threshold_observed_at_ms"] == BASE_MS + 7_200_010

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("score-clock", score=82),),
                observed_at_ms=BASE_MS + 7_200_020,
            )
        duplicate = next(iter(repository.story_provider_evidence().values()))["provider_evidence"]
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
            repository.rebuild_stories(now_ms=BASE_MS + 3)

        initial = next(iter(repository.story_provider_evidence().values()))
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

        selected_later = repository.story_provider_evidence()[story_id]
        assert selected_later["provider_evidence"]["provider_score"] == 90
        assert selected_later["push_delivery_status"] == "sent"
        detail = NewsInterface(repository).get_story(story_id=story_id)
        assert detail is not None
        assert detail["push_delivery_state"] == "sent"
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
            repository.rebuild_stories(now_ms=BASE_MS + 2)
        runtime = _runtime(conn, delivery=_Delivery())

        result = asyncio.run(runtime.reconcile(now_ms=BASE_MS + 10))
        progressed = asyncio.run(runtime.turn())

        state = conn.execute("SELECT baseline_at_ms FROM news_push_state WHERE singleton_key='current'").fetchone()
        delivery = conn.execute("SELECT status, translation_status FROM news_push_deliveries").fetchone()
        assert result == {"inserted": 1, "suppressed": 1, "terminalized": 0}
        assert state["baseline_at_ms"] == BASE_MS + 10
        assert delivery == {"status": "suppressed", "translation_status": "not_requested"}
        assert progressed is False
        health = asyncio.run(runtime.health_snapshot(now_ms=BASE_MS + 10))
        assert health["status"] == "ready"
        assert health["initialized"] is True
        assert health["suppressed_count"] == 1
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)

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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
        retry_health = asyncio.run(runtime.health_snapshot(now_ms=clock["now_ms"]))
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
        completed_health = asyncio.run(runtime.health_snapshot(now_ms=clock["now_ms"]))
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
        health = asyncio.run(recovered.health_snapshot(now_ms=clock["now_ms"] + 1))
        assert health["translation_24h"] == {
            "attempted": 1,
            "succeeded": 0,
            "success_ratio": 0.0,
            "latency_p95_ms": None,
            "failure_counts": {"news_push_translation_interrupted_after_dispatch": 1},
            "slo_met": False,
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
        conn.execute(
            """
            INSERT INTO news_push_deliveries (
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload, delivery_payload,
              payload_fingerprint, translation_status, status,
              delivery_attempts, next_attempt_at_ms, lease_owner,
              lease_token, lease_expires_at_ms, receipt, last_error,
              sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
              %s, 'old-runtime-item', 88, %s, '{}'::jsonb, NULL,
              NULL, 'attempted', 'pending_translation',
              0, %s, 'news_story_push:old-runtime',
              'old-lease-token', %s, NULL, NULL,
              NULL, %s, %s
            )
            """,
            (
                "a" * 64,
                BASE_MS,
                attempted_at_ms,
                attempted_at_ms + 60_000,
                BASE_MS,
                attempted_at_ms,
            ),
        )
        conn.commit()
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
            repository.rebuild_stories(now_ms=BASE_MS)

        initial_evidence = repository.story_provider_evidence()
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
            repository.rebuild_stories(now_ms=after_expiry_ms)
        current_evidence = repository.story_provider_evidence()
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
            repository.rebuild_stories(now_ms=first_projection_ms)

        initial_evidence = repository.story_provider_evidence()
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
            repository.rebuild_stories(now_ms=clock["now_ms"])
        current_evidence = repository.story_provider_evidence()
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
        health = asyncio.run(runtime.health_snapshot(now_ms=clock["now_ms"] + 1))
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
            repository.rebuild_stories(now_ms=BASE_MS + 1)
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
