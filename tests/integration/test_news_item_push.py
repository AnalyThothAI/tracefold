from __future__ import annotations

import asyncio
from typing import Any

import pytest
from psycopg.errors import RaiseException

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    reset_postgres_schema,
)
from tests.support.news import rebuild_news_projection
from tracefold.app.database import WorkerDatabase
from tracefold.app.worker_capabilities import FiniteOperations
from tracefold.news.models import NewsFeedEntry
from tracefold.news.opennews import OpenNewsEvent
from tracefold.news.push import NewsItemPush, NewsPushExternalError, NewsPushReceipt
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import opennews_source
from tracefold.platform.config.settings import Settings

BASE_MS = 1_785_560_400_000


def test_live_scoreless_assetless_item_creates_outbox_before_story_projection() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            outcome = repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("live-1019", strategy_id="1019", score=None),),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            counts = conn.execute(
                """
                SELECT (SELECT count(*) FROM news_items) AS items,
                       (SELECT count(*) FROM news_stories) AS stories,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes
                """
            ).fetchone()
    finally:
        conn.close()

    assert outcome["push_outbox_writes"] == 1
    assert dict(counts) == {"items": 1, "stories": 0, "pushes": 1}


def test_item_and_push_outbox_roll_back_together() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
        conn.execute(
            """
            CREATE FUNCTION reject_news_item_push_test() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'reject_news_item_push_test';
            END
            $$
            """
        )
        conn.execute(
            """
            CREATE TRIGGER reject_news_item_push_test
            BEFORE INSERT ON news_push_deliveries
            FOR EACH ROW EXECUTE FUNCTION reject_news_item_push_test()
            """
        )
        conn.commit()

        with pytest.raises(RaiseException, match="reject_news_item_push_test"), conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("atomic-rollback", strategy_id="1018", score=91),),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
        failed_counts = conn.execute(
            """
            SELECT (SELECT count(*) FROM news_items) AS items,
                   (SELECT count(*) FROM news_push_deliveries) AS pushes
            """
        ).fetchone()
        conn.execute("DROP TRIGGER reject_news_item_push_test ON news_push_deliveries")
        conn.execute("DROP FUNCTION reject_news_item_push_test()")
        conn.commit()
        with conn.transaction():
            replay_outcome = repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("atomic-rollback", strategy_id="1018", score=91),),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
        replay_counts = conn.execute(
            """
            SELECT (SELECT count(*) FROM news_items) AS items,
                   (SELECT count(*) FROM news_push_deliveries) AS pushes
            """
        ).fetchone()
    finally:
        conn.close()

    assert dict(failed_counts) == {"items": 0, "pushes": 0}
    assert replay_outcome["push_outbox_writes"] == 1
    assert dict(replay_counts) == {"items": 1, "pushes": 1}


def test_story_publication_lock_does_not_block_item_and_push_commit() -> None:
    story_conn = connect_postgres_test(read_only=False)
    item_conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(story_conn)
        with story_conn.transaction():
            setup_repository = NewsRepository(story_conn)
            setup_repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            setup_repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
        story_repository = NewsRepository(story_conn)
        item_repository = NewsRepository(item_conn)
        with story_conn.transaction():
            story_repository.lock_story_inputs()
            with item_conn.transaction():
                item_conn.execute("SET LOCAL statement_timeout = '1s'")
                outcome = item_repository.record_opennews_events(
                    source=opennews_source(),
                    events=(_event("story-lock-isolated", strategy_id="1018", score=91),),
                    observed_at_ms=BASE_MS + 1_000,
                    ingest_mode="live",
                )
            counts = item_conn.execute(
                """
                SELECT (SELECT count(*) FROM news_items) AS items,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes
                """
            ).fetchone()
    finally:
        item_conn.close()
        story_conn.close()

    assert outcome["push_outbox_writes"] == 1
    assert dict(counts) == {"items": 1, "pushes": 1}


def test_item_push_translates_before_fence_and_sends_once() -> None:
    conn = connect_postgres_test(read_only=False)
    database: WorkerDatabase | None = None
    finite: FiniteOperations | None = None
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(
                delivery_available=True,
                now_ms=BASE_MS,
            )
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("translated", strategy_id="1018", score=91),),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )

        translator = _Translator("比特币 ETF 资金流入加速")
        sender = _Sender()
        database = WorkerDatabase.create(
            Settings(storage=postgres_settings_storage()),
            telemetry=None,
        )
        finite = FiniteOperations()
        push = NewsItemPush(
            db=database,
            finite_operations=finite,
            translator=translator,
            sender=sender,
            delivery_available=True,
            clock_ms=lambda: BASE_MS + 2_000,
        )

        assert asyncio.run(push.turn()) is True
        row = conn.execute(
            """
            SELECT item_id, status, source_payload, presentation_snapshot,
                   attempted_at_ms, receipt, sent_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
    finally:
        if finite is not None:
            finite.close()
        if database is not None:
            database.close_executors()
            asyncio.run(database.aclose())
        conn.close()

    assert translator.titles == ["Strategy report translated"]
    assert len(sender.calls) == 1
    assert row is not None
    assert row["status"] == "sent"
    assert row["source_payload"]["schema_version"] == "news_item_push_v1"
    assert row["presentation_snapshot"] == {
        "display_title": "比特币 ETF 资金流入加速",
        "outcome": "translated",
        "translation_duration_ms": 0,
        "translation_policy_version": "title_zh_v3",
    }
    assert sender.calls[0]["source_payload"] == row["source_payload"]
    assert sender.calls[0]["presentation_snapshot"] == row["presentation_snapshot"]
    assert row["attempted_at_ms"] == BASE_MS + 2_000
    assert row["receipt"] == {
        "provider": "feishu",
        "receipt_id": "receipt-1",
        "details": {"code": 0, "status_code": 200},
    }
    assert row["sent_at_ms"] == BASE_MS + 2_000


def test_translation_failure_falls_back_and_feishu_failure_is_terminal_without_retry() -> None:
    conn = connect_postgres_test(read_only=False)
    database: WorkerDatabase | None = None
    finite: FiniteOperations | None = None
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("fallback-terminal", strategy_id="1018", score=90),),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )

        sender = _FailingSender()
        database = WorkerDatabase.create(
            Settings(storage=postgres_settings_storage()),
            telemetry=None,
        )
        finite = FiniteOperations()
        push = NewsItemPush(
            db=database,
            finite_operations=finite,
            translator=_FailingTranslator(),
            sender=sender,
            delivery_available=True,
            clock_ms=lambda: BASE_MS + 2_000,
        )

        assert asyncio.run(push.turn()) is True
        assert asyncio.run(push.turn()) is False
        row = conn.execute(
            """
            SELECT status, presentation_snapshot, last_error
              FROM news_push_deliveries
            """
        ).fetchone()
    finally:
        if finite is not None:
            finite.close()
        if database is not None:
            database.close_executors()
            asyncio.run(database.aclose())
        conn.close()

    assert sender.calls == 1
    assert row is not None
    assert row["status"] == "terminal"
    assert row["last_error"] == "news_item_push_feishu_transport_failed"
    assert row["presentation_snapshot"] == {
        "display_title": "Strategy report fallback-terminal",
        "fallback_code": "news_item_push_translation_rate_limited",
        "outcome": "fallback",
        "translation_duration_ms": 0,
        "translation_policy_version": "title_zh_v3",
    }


def test_startup_terminalizes_preexisting_sending_even_when_delivery_is_unavailable() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event("interrupted", strategy_id="1018", score=90),
                    _event("never-fenced", strategy_id="1019", score=None),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            pending = repository.peek_item_push()
            assert pending is not None
            repository.fence_item_push(
                item_id=str(pending["item_id"]),
                presentation_snapshot={
                    "display_title": "Strategy report interrupted",
                    "outcome": "fallback",
                    "fallback_code": "news_item_push_translation_unavailable",
                    "translation_policy_version": "title_zh_v3",
                },
                attempted_at_ms=BASE_MS + 2_000,
            )
            outcome = repository.reconcile_item_push(
                delivery_available=False,
                now_ms=BASE_MS + 3_000,
            )
            row = conn.execute(
                """
                SELECT count(*) FILTER (
                         WHERE delivery.status = 'pending'
                       ) AS durable_pending,
                       count(*) FILTER (
                         WHERE delivery.status = 'terminal'
                           AND delivery.last_error =
                               'news_item_push_interrupted_unknown'
                       ) AS interrupted_terminal,
                       state.delivery_available, state.pending_count,
                       state.sending_count, state.terminal_count
                  FROM news_push_deliveries AS delivery
                  CROSS JOIN news_push_state AS state
                 WHERE state.singleton_key = 'current'
                 GROUP BY state.delivery_available, state.pending_count,
                          state.sending_count, state.terminal_count
                """
            ).fetchone()
    finally:
        conn.close()

    assert outcome["terminalized"] == 1
    assert dict(row) == {
        "durable_pending": 1,
        "interrupted_terminal": 1,
        "delivery_available": False,
        "pending_count": 1,
        "sending_count": 0,
        "terminal_count": 1,
    }


class _Translator:
    def __init__(self, result: str) -> None:
        self.result = result
        self.titles: list[str] = []

    def translate(self, title: str) -> str:
        self.titles.append(title)
        return self.result

    def close(self) -> None:
        return None


class _Sender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send(
        self,
        source_payload: dict[str, Any],
        presentation_snapshot: dict[str, Any],
    ) -> NewsPushReceipt:
        self.calls.append(
            {
                "source_payload": source_payload,
                "presentation_snapshot": presentation_snapshot,
            }
        )
        return NewsPushReceipt(
            provider="feishu",
            receipt_id="receipt-1",
            details={"code": 0, "status_code": 200, "secret": "must-not-persist"},
        )

    def close(self) -> None:
        return None


class _FailingTranslator:
    def translate(self, _title: str) -> str:
        raise NewsPushExternalError("news_item_push_translation_rate_limited")

    def close(self) -> None:
        return None


class _FailingSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(
        self,
        _source_payload: dict[str, Any],
        _presentation_snapshot: dict[str, Any],
    ) -> NewsPushReceipt:
        self.calls += 1
        raise NewsPushExternalError("news_item_push_feishu_transport_failed")

    def close(self) -> None:
        return None


def test_same_event_under_both_strategies_creates_one_story_and_one_push() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            rebuild_news_projection(repository, now_ms=BASE_MS)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event("both-strategies", strategy_id="1018", score=70),
                    _event("both-strategies", strategy_id="1019", score=None),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2_000)
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
    assert payload["strategy_labels"] == [
        "1018 Strategy 1018",
        "1019 Strategy 1019",
    ]


def test_later_item_changes_and_live_replay_leave_first_push_snapshot_frozen() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        first = _event(
            "immutable-item",
            strategy_id="1018",
            score=70,
            title="First accepted title",
        )
        changed = _event(
            "immutable-item",
            strategy_id="1019",
            score=99,
            title="Later material title",
        )
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            first_outcome = repository.record_opennews_events(
                source=opennews_source(),
                events=(first,),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            replay_outcome = repository.record_opennews_events(
                source=opennews_source(),
                events=(first,),
                observed_at_ms=BASE_MS + 2_000,
                ingest_mode="live",
            )
            changed_outcome = repository.record_opennews_events(
                source=opennews_source(),
                events=(changed,),
                observed_at_ms=BASE_MS + 3_000,
                ingest_mode="live",
            )
            row = conn.execute(
                """
                SELECT item.title, delivery.source_payload,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes
                  FROM news_items AS item
                  JOIN news_push_deliveries AS delivery USING (item_id)
                 WHERE item.provider_record_id = 'immutable-item'
                """
            ).fetchone()
    finally:
        conn.close()

    assert first_outcome["push_outbox_writes"] == 1
    assert replay_outcome["push_outbox_writes"] == 0
    assert changed_outcome["push_outbox_writes"] == 0
    assert row["title"] == "Later material title"
    assert row["pushes"] == 1
    assert row["source_payload"]["original_title"] == "First accepted title"
    assert row["source_payload"]["score"] == 70
    assert row["source_payload"]["strategy_labels"] == ["1018 Strategy 1018"]


def test_two_items_that_merge_into_one_story_are_delivered_independently() -> None:
    conn = connect_postgres_test(read_only=False)
    database: WorkerDatabase | None = None
    finite: FiniteOperations | None = None
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "cluster-item-a",
                        strategy_id="1018",
                        score=91,
                        title="Bitcoin ETF inflows accelerate after approval",
                    ),
                    _event(
                        "cluster-item-b",
                        strategy_id="1019",
                        score=None,
                        title="Bitcoin ETF inflows accelerate after approval",
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2_000)
            counts_before = conn.execute(
                """
                SELECT (SELECT count(*) FROM news_items) AS items,
                       (SELECT count(*) FROM news_stories) AS stories,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes
                """
            ).fetchone()

        sender = _Sender()
        database = WorkerDatabase.create(
            Settings(storage=postgres_settings_storage()),
            telemetry=None,
        )
        finite = FiniteOperations()
        push = NewsItemPush(
            db=database,
            finite_operations=finite,
            translator=None,
            sender=sender,
            delivery_available=True,
            clock_ms=lambda: BASE_MS + 3_000,
        )
        assert asyncio.run(push.turn()) is True
        assert asyncio.run(push.turn()) is True
        assert asyncio.run(push.turn()) is False
        counts_after = conn.execute(
            """
            SELECT (SELECT count(*) FROM news_stories) AS stories,
                   count(*) FILTER (WHERE status = 'sent') AS sent
              FROM news_push_deliveries
            """
        ).fetchone()
    finally:
        if finite is not None:
            finite.close()
        if database is not None:
            database.close_executors()
            asyncio.run(database.aclose())
        conn.close()

    assert dict(counts_before) == {"items": 2, "stories": 1, "pushes": 2}
    assert len(sender.calls) == 2
    assert {call["source_payload"]["provider_event_id"] for call in sender.calls} == {
        "cluster-item-a",
        "cluster-item-b",
    }
    assert [call["source_payload"]["item_id"] for call in sender.calls] == sorted(
        call["source_payload"]["item_id"] for call in sender.calls
    )
    assert dict(counts_after) == {"stories": 1, "sent": 2}


def test_recovery_only_story_and_later_live_replay_never_create_push() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            rebuild_news_projection(repository, now_ms=BASE_MS)
            event = _event("recovery-only", strategy_id="1018", score=95)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(event,),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="recovery",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2_000)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(event,),
                observed_at_ms=BASE_MS + 3_000,
                ingest_mode="live",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 4_000)
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
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            rebuild_news_projection(repository, now_ms=BASE_MS)
            repository.reconcile_item_push(
                delivery_available=False,
                now_ms=BASE_MS + 1_000,
            )
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("disabled-window", strategy_id="1018", score=99),),
                observed_at_ms=BASE_MS + 2_000,
                ingest_mode="live",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2_500)
            repository.reconcile_item_push(
                delivery_available=True,
                now_ms=BASE_MS + 3_000,
            )
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("stale-pre-epoch-batch", strategy_id="1018", score=88),),
                observed_at_ms=BASE_MS + 2_500,
                ingest_mode="live",
            )
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("post-repair-live", strategy_id="1018", score=89),),
                observed_at_ms=BASE_MS + 4_000,
                ingest_mode="live",
            )
            state = conn.execute(
                """
                SELECT delivery_available, enablement_epoch_at_ms,
                       (SELECT count(*) FROM news_items) AS items,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes,
                       (SELECT source_payload ->> 'provider_event_id'
                          FROM news_push_deliveries) AS pushed_provider_id
                  FROM news_push_state WHERE singleton_key = 'current'
                """
            ).fetchone()
    finally:
        conn.close()

    assert dict(state) == {
        "delivery_available": True,
        "enablement_epoch_at_ms": BASE_MS + 3_000,
        "items": 3,
        "pushes": 1,
        "pushed_provider_id": "post-repair-live",
    }


def _event(
    provider_record_id: str,
    *,
    strategy_id: str,
    score: int | None,
    title: str | None = None,
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
            title=title or f"Strategy report {provider_record_id}",
            description="",
            published_at_ms=BASE_MS + 500,
            reporting_origin="opennews",
        ),
    )
