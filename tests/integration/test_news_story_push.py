from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
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
    NewsPushTranslation,
    NewsPushTranslationError,
    NewsRepository,
    NewsStoryPush,
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
                "id": record_id,
                "aiRating": {"score": score, "signal": "long", "grade": "A+"},
            },
        }
    )
    assert event is not None
    return event


def _seed_source(conn: Any) -> NewsRepository:
    repository = NewsRepository(conn)
    with conn.transaction():
        repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
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
        runtime = _runtime(conn, translator=_Translator(), delivery=_Delivery())

        result = asyncio.run(runtime.reconcile(now_ms=BASE_MS + 10))
        candidate = asyncio.run(runtime.peek(now_ms=BASE_MS + 10))

        state = conn.execute("SELECT baseline_at_ms FROM news_push_state WHERE singleton_key='current'").fetchone()
        delivery = conn.execute("SELECT status, translation_status FROM news_push_deliveries").fetchone()
        assert result == {"inserted": 1, "suppressed": 1, "terminalized": 0}
        assert state["baseline_at_ms"] == BASE_MS + 10
        assert delivery == {"status": "suppressed", "translation_status": "not_requested"}
        assert candidate is None
        health = asyncio.run(runtime.health_snapshot(now_ms=BASE_MS + 10))
        assert health["status"] == "ready"
        assert health["initialized"] is True
        assert health["suppressed_count"] == 1
    finally:
        conn.close()


def test_push_retries_frozen_english_fallback_outside_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    translator = _Translator(fail=True)
    delivery = _Delivery(fail_first=True)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, translator=translator, delivery=delivery)

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
        assert asyncio.run(runtime.peek(now_ms=BASE_MS + 2)) is None
        assert conn.execute("SELECT count(*) AS count FROM news_push_deliveries").fetchone()["count"] == 0

        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_annotation("after-enable", score=71),),
                observed_at_ms=BASE_MS + 3,
            )
        asyncio.run(runtime.reconcile(now_ms=clock["now_ms"]))
        candidate = asyncio.run(runtime.peek(now_ms=clock["now_ms"]))
        assert candidate is not None
        assert asyncio.run(runtime.execute(candidate))

        first = conn.execute(
            """
            SELECT story_id, status, translation_status, delivery_attempts,
                   delivery_payload, payload_fingerprint, next_attempt_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
        assert first["status"] == "retry_wait"
        assert first["translation_status"] == "unavailable"
        assert first["delivery_attempts"] == 1
        assert first["delivery_payload"]["translation"]["status"] == "unavailable"
        assert first["delivery_payload"]["card"]["title_zh"] is None
        assert first["delivery_payload"]["card"]["original_title"] == ("Issuer files for a new exchange product")
        assert first["delivery_payload"]["card"]["url"] is None
        assert first["delivery_payload"]["card"]["provider_evidence"]["score"] == 71
        retry_health = asyncio.run(runtime.health_snapshot(now_ms=clock["now_ms"]))
        assert retry_health["status"] == "degraded"
        assert retry_health["retry_count"] == 1
        frozen_payload = first["delivery_payload"]
        frozen_fingerprint = first["payload_fingerprint"]

        clock["now_ms"] = int(first["next_attempt_at_ms"]) + 1
        retry_candidate = asyncio.run(runtime.peek(now_ms=clock["now_ms"]))
        assert retry_candidate is not None
        assert retry_candidate.target_key == candidate.target_key
        assert asyncio.run(runtime.execute(retry_candidate))

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
        assert translator.calls == 1
        assert delivery.render_calls == 1
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
        assert asyncio.run(runtime.peek(now_ms=clock["now_ms"] + 1)) is None
        assert conn.execute("SELECT count(*) AS count FROM news_push_deliveries").fetchone()["count"] == 1
    finally:
        conn.close()


def test_story_identity_change_after_baseline_does_not_suppress_retained_high_score_item() -> None:
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
                        published_at_ms=BASE_MS - (95 * hour_ms),
                    ),
                    _event(
                        "retained-highest",
                        title=selected_title,
                        score=91,
                        published_at_ms=BASE_MS - (94 * hour_ms),
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

        runtime = _runtime(conn, translator=_Translator(), delivery=_Delivery())
        assert asyncio.run(runtime.reconcile(now_ms=BASE_MS)) == {
            "inserted": 1,
            "suppressed": 1,
            "terminalized": 0,
        }

        # The 96-hour window drops only the canonical earliest member. The
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
            "inserted": 1,
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
        assert {row["story_id"]: row["status"] for row in rows} == {
            original_story_id: "suppressed",
            new_story_id: "pending_translation",
        }
        assert next(row for row in rows if row["story_id"] == new_story_id)["translation_status"] == "pending"
        candidate = asyncio.run(runtime.peek(now_ms=after_expiry_ms))
        assert candidate is not None
        assert candidate.target_key == new_story_id
    finally:
        conn.close()


def test_restarts_catch_up_persisted_pending_and_frozen_retry_without_retranslation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 100}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    initial_translator = _Translator(fail=True)
    initial_delivery = _Delivery()
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        initial_runtime = _runtime(
            conn,
            translator=initial_translator,
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
        assert initial_translator.calls == 0
        assert initial_delivery.render_calls == 0

        # The runtime that established the candidate can disappear before it
        # executes. A fresh instance discovers and translates that durable row.
        pending_translator = _Translator()
        pending_delivery = _Delivery(fail_first=True)
        pending_runtime = _runtime(
            conn,
            translator=pending_translator,
            delivery=pending_delivery,
            runtime_id="pending-catch-up",
        )
        pending_candidate = asyncio.run(pending_runtime.peek(now_ms=clock["now_ms"]))
        assert pending_candidate is not None
        assert pending_candidate.target_key == persisted_pending["story_id"]
        assert asyncio.run(pending_runtime.execute(pending_candidate))

        retry = conn.execute(
            """
            SELECT story_id, status, translation_status, delivery_attempts,
                   next_attempt_at_ms, delivery_payload, payload_fingerprint
              FROM news_push_deliveries
            """
        ).fetchone()
        assert retry["status"] == "retry_wait"
        assert retry["translation_status"] == "translated"
        assert retry["delivery_payload"]["translation"]["title_zh"] == ("发行人提交新的交易所产品申请")
        frozen_payload = retry["delivery_payload"]
        frozen_fingerprint = retry["payload_fingerprint"]
        assert pending_translator.calls == 1
        assert pending_delivery.render_calls == 1

        # A fresh runtime has no in-memory state from the first attempt. It
        # discovers the durable retry and delivers the already-rendered card.
        restarted_translator = _Translator(fail=True)
        restarted_delivery = _Delivery()
        restarted_runtime = _runtime(
            conn,
            translator=restarted_translator,
            delivery=restarted_delivery,
            runtime_id="after-restart",
        )
        clock["now_ms"] = int(retry["next_attempt_at_ms"]) + 1
        retry_candidate = asyncio.run(restarted_runtime.peek(now_ms=clock["now_ms"]))
        assert retry_candidate is not None
        assert retry_candidate.target_key == retry["story_id"]
        assert asyncio.run(restarted_runtime.execute(retry_candidate))

        sent = conn.execute(
            """
            SELECT status, translation_status, delivery_attempts,
                   delivery_payload, payload_fingerprint, receipt
              FROM news_push_deliveries
            """
        ).fetchone()
        assert sent["status"] == "sent"
        assert sent["translation_status"] == "translated"
        assert sent["delivery_attempts"] == 2
        assert sent["delivery_payload"] == frozen_payload
        assert sent["payload_fingerprint"] == frozen_fingerprint
        assert sent["receipt"]["provider"] == "feishu"
        assert restarted_translator.calls == 0
        assert restarted_delivery.render_calls == 0
        assert restarted_delivery.payloads == [frozen_payload]
    finally:
        conn.close()


def test_chinese_title_bypasses_translator_and_is_delivered_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 10}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    translator = _Translator(fail=True)
    delivery = _Delivery()
    title = "央行维持利率不变"
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, translator=translator, delivery=delivery)
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
        candidate = asyncio.run(runtime.peek(now_ms=clock["now_ms"]))
        assert candidate is not None
        assert asyncio.run(runtime.execute(candidate))

        row = conn.execute(
            """
            SELECT status, translation_status, delivery_payload
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row["status"] == "sent"
        assert row["translation_status"] == "not_needed"
        assert row["delivery_payload"]["translation"] == {
            "status": "not_needed",
            "title_zh": title,
            "provider": None,
            "model": None,
            "error_code": None,
        }
        assert row["delivery_payload"]["card"]["title_zh"] == title
        assert row["delivery_payload"]["card"]["original_title"] == title
        assert translator.calls == 0
        assert delivery.render_calls == 1
    finally:
        conn.close()


def test_japanese_title_with_kana_is_translated_instead_of_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 10}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    translator = _Translator()
    delivery = _Delivery()
    title = "日本銀行が金利を引き上げる"
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, translator=translator, delivery=delivery)
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
        candidate = asyncio.run(runtime.peek(now_ms=clock["now_ms"]))
        assert candidate is not None
        assert asyncio.run(runtime.execute(candidate))

        row = conn.execute(
            """
            SELECT status, translation_status, delivery_payload
              FROM news_push_deliveries
            """
        ).fetchone()
        assert row["status"] == "sent"
        assert row["translation_status"] == "translated"
        assert row["delivery_payload"]["translation"]["title_zh"] == "发行人提交新的交易所产品申请"
        assert row["delivery_payload"]["card"]["original_title"] == title
        assert translator.calls == 1
        assert delivery.render_calls == 1
    finally:
        conn.close()


def test_non_retryable_delivery_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect_postgres_test(read_only=False)
    clock = {"now_ms": BASE_MS + 10}
    monkeypatch.setattr(push_module, "_now_ms", lambda: clock["now_ms"])
    translator = _Translator()
    delivery = _Delivery(error=NewsPushDeliveryError("feishu_signature_rejected", retryable=False))
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(conn, translator=translator, delivery=delivery)
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
        candidate = asyncio.run(runtime.peek(now_ms=clock["now_ms"]))
        assert candidate is not None
        assert asyncio.run(runtime.execute(candidate))

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
        assert asyncio.run(runtime.peek(now_ms=clock["now_ms"] + 1)) is None
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

    translator = _Translator()
    delivery = _Delivery(on_deliver=replace_lease_before_stale_completion)
    try:
        reset_postgres_schema(conn)
        repository = _seed_source(conn)
        runtime = _runtime(
            conn,
            translator=translator,
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
        candidate = asyncio.run(runtime.peek(now_ms=clock["now_ms"]))
        assert candidate is not None

        # The external provider returned success, but another owner replaced
        # the lease before completion. The stale completion must be fenced.
        assert asyncio.run(runtime.execute(candidate)) is False
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
        assert translator.calls == 1
        assert delivery.render_calls == 1
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
        on_submitted = kwargs.pop("on_submitted", None)
        assert not self.conn.in_transaction
        if on_submitted is not None:
            on_submitted()
        return function(*args, **kwargs)


class _Translator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def translate_title(self, _title: str) -> NewsPushTranslation:
        self.calls += 1
        if self.fail:
            raise NewsPushTranslationError("translator_unavailable")
        return NewsPushTranslation(
            title_zh="发行人提交新的交易所产品申请",
            provider="deepseek",
            model="deepseek-v4-flash",
        )


class _Delivery:
    def __init__(
        self,
        *,
        fail_first: bool = False,
        error: NewsPushDeliveryError | None = None,
        on_deliver: Callable[[], None] | None = None,
    ) -> None:
        self.fail_first = fail_first
        self.error = error
        self.on_deliver = on_deliver
        self.render_calls = 0
        self.payloads: list[dict[str, Any]] = []
        self.idempotency_keys: list[str] = []

    def render(
        self,
        source_payload: Mapping[str, Any],
        translation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.render_calls += 1
        evidence = dict(source_payload["provider_evidence"])
        metadata = dict(evidence["provider_metadata"])
        return {
            "channel": "feishu",
            "translation": dict(translation),
            "card": {
                "title_zh": translation["title_zh"],
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
    translator: _Translator,
    delivery: _Delivery,
    runtime_id: str = "test-runtime",
) -> NewsStoryPush:
    capability = _InlineCapability(conn)
    return NewsStoryPush(
        db=_DirectDatabase(conn),
        model_adapter=capability,
        finite_operations=capability,
        translator=translator,
        delivery=delivery,
        runtime_id=runtime_id,
    )
