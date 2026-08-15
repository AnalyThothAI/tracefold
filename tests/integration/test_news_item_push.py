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
from tracefold.news.title_presentation import (
    NewsItemTitlePresentation,
    TitleTranslationError,
)
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


def test_resolved_item_title_is_shared_with_push_and_sent_once() -> None:
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
                events=(
                    _event(
                        "translated",
                        strategy_id="1018",
                        score=91,
                        title="Iran has not decided to resume US talks",
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )

        translator = _Translator("伊朗尚未决定恢复与美国谈判")
        sender = _Sender()
        database = WorkerDatabase.create(
            Settings(storage=postgres_settings_storage()),
            telemetry=None,
        )
        finite = FiniteOperations()
        presentation = NewsItemTitlePresentation(
            db=database,
            deepl=translator,
            deepseek=None,
            clock_ms=lambda: BASE_MS + 2_000,
        )
        push = NewsItemPush(
            db=database,
            finite_operations=finite,
            sender=sender,
            delivery_available=True,
            clock_ms=lambda: BASE_MS + 2_000,
        )

        assert asyncio.run(presentation.turn()) is True
        assert asyncio.run(push.turn()) is True
        row = conn.execute(
            """
            SELECT delivery.item_id, delivery.status, delivery.source_payload,
                   delivery.legacy_presentation_snapshot,
                   delivery.attempted_at_ms, delivery.receipt,
                   delivery.sent_at_ms, presentation.display_title,
                   presentation.original_title, presentation.outcome,
                   presentation.provider, presentation.duration_ms,
                   presentation.policy_version
              FROM news_push_deliveries delivery
              JOIN news_item_title_presentations presentation
                ON presentation.item_id = delivery.item_id
               AND presentation.source_title_fingerprint =
                   delivery.source_title_fingerprint
            """
        ).fetchone()
    finally:
        if finite is not None:
            finite.close()
        if database is not None:
            database.close_executors()
            asyncio.run(database.aclose())
        conn.close()

    assert translator.titles == ["Iran has not decided to resume US talks"]
    assert len(sender.calls) == 1
    assert row is not None
    assert row["status"] == "sent"
    assert row["source_payload"]["schema_version"] == "news_item_push_v2"
    assert row["legacy_presentation_snapshot"] is None
    assert sender.calls[0]["presentation_snapshot"] == {
        "display_title": "伊朗尚未决定恢复与美国谈判",
        "original_title": "Iran has not decided to resume US talks",
        "outcome": "translated",
        "provider": "deepl",
        "policy_version": "news_title_zh_v1",
        "fallback_code": None,
        "duration_ms": 0,
    }
    assert sender.calls[0]["source_payload"] == row["source_payload"]
    assert row["display_title"] == "伊朗尚未决定恢复与美国谈判"
    assert row["outcome"] == "translated"
    assert row["provider"] == "deepl"
    assert row["policy_version"] == "news_title_zh_v1"
    assert row["attempted_at_ms"] == BASE_MS + 2_000
    assert row["receipt"] == {
        "provider": "feishu",
        "receipt_id": "receipt-1",
        "details": {"code": 0, "status_code": 200},
    }
    assert row["sent_at_ms"] == BASE_MS + 2_000


def test_provider_fallback_then_feishu_failure_is_terminal_without_retry() -> None:
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
        presentation = NewsItemTitlePresentation(
            db=database,
            deepl=_FailingTranslator(),
            deepseek=None,
            clock_ms=lambda: BASE_MS + 2_000,
        )
        push = NewsItemPush(
            db=database,
            finite_operations=finite,
            sender=sender,
            delivery_available=True,
            clock_ms=lambda: BASE_MS + 2_000,
        )

        assert asyncio.run(presentation.turn()) is True
        assert asyncio.run(push.turn()) is True
        assert asyncio.run(push.turn()) is False
        row = conn.execute(
            """
            SELECT delivery.status, delivery.legacy_presentation_snapshot,
                   delivery.last_error, presentation.display_title,
                   presentation.outcome, presentation.fallback_code
              FROM news_push_deliveries delivery
              JOIN news_item_title_presentations presentation
                ON presentation.item_id = delivery.item_id
               AND presentation.source_title_fingerprint =
                   delivery.source_title_fingerprint
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
    assert row["legacy_presentation_snapshot"] is None
    assert row["display_title"] == "Strategy report fallback-terminal"
    assert row["outcome"] == "fallback"
    assert row["fallback_code"] == "news_title_presentation_deepl_rate_limited"


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
            assert pending is None
            conn.execute(
                """
                UPDATE news_item_title_presentations
                   SET state = 'resolved', display_title = original_title,
                       outcome = 'fallback', provider = NULL,
                       policy_version = 'news_title_zh_v1',
                       fallback_code = 'news_title_presentation_provider_unavailable',
                       resolved_at_ms = %s, duration_ms = 0,
                       updated_at_ms = %s
                 WHERE state = 'pending'
                """,
                (BASE_MS + 2_000, BASE_MS + 2_000),
            )
            pending = repository.peek_item_push()
            assert pending is not None
            repository.fence_item_push(
                item_id=str(pending["item_id"]),
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

    async def translate(self, title: str) -> str:
        self.titles.append(title)
        return self.result

    async def close(self) -> None:
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
    async def translate(self, _title: str) -> str:
        raise TitleTranslationError("news_title_presentation_deepl_rate_limited")

    async def close(self) -> None:
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
                       delivery.source_title_fingerprint,
                       (SELECT count(*) FROM news_push_deliveries) AS pushes,
                       (
                         SELECT count(*)
                           FROM news_item_title_presentations
                          WHERE item_id = item.item_id
                       ) AS presentations
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
    assert row["presentations"] == 2
    assert row["source_payload"]["original_title"] == "First accepted title"
    assert row["source_title_fingerprint"] == ("027a0883971641943f78f331202dd50d9d0d0844f50d532ca7c4eea6669396fb")
    assert row["source_payload"]["score"] == 70
    assert row["source_payload"]["strategy_labels"] == ["1018 Strategy 1018"]


def test_three_exact_atoms_create_one_push_leader_and_two_suppressed_deliveries() -> None:
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
                        "exact-atom-a",
                        strategy_id="1018",
                        score=91,
                        title="Bitcoin ETF inflows accelerate after approval",
                    ),
                    _event(
                        "exact-atom-b",
                        strategy_id="1019",
                        score=None,
                        title="Bitcoin ETF inflows accelerate after approval",
                    ),
                    _event(
                        "exact-atom-c",
                        strategy_id="1018",
                        score=92,
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
                       (SELECT count(*) FROM news_push_deliveries) AS pushes,
                       count(*) FILTER (WHERE status = 'pending') AS pending,
                       count(*) FILTER (WHERE status = 'suppressed') AS suppressed,
                       min(item_id) AS deterministic_leader_item_id,
                       min(item_id) FILTER (WHERE status = 'pending') AS leader_item_id
                  FROM news_push_deliveries
                """
            ).fetchone()

        sender = _Sender()
        database = WorkerDatabase.create(
            Settings(storage=postgres_settings_storage()),
            telemetry=None,
        )
        finite = FiniteOperations()
        presentation = NewsItemTitlePresentation(
            db=database,
            deepl=None,
            deepseek=None,
            clock_ms=lambda: BASE_MS + 3_000,
        )
        push = NewsItemPush(
            db=database,
            finite_operations=finite,
            sender=sender,
            delivery_available=True,
            clock_ms=lambda: BASE_MS + 3_000,
        )
        assert asyncio.run(presentation.turn()) is True
        assert asyncio.run(presentation.turn()) is True
        assert asyncio.run(presentation.turn()) is True
        assert asyncio.run(presentation.turn()) is False
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

    assert dict(counts_before) == {
        "items": 3,
        "stories": 1,
        "pushes": 3,
        "pending": 1,
        "suppressed": 2,
        "deterministic_leader_item_id": counts_before["leader_item_id"],
        "leader_item_id": counts_before["leader_item_id"],
    }
    assert len(sender.calls) == 1
    assert sender.calls[0]["source_payload"]["item_id"] == counts_before["leader_item_id"]
    assert [call["source_payload"]["item_id"] for call in sender.calls] == sorted(
        call["source_payload"]["item_id"] for call in sender.calls
    )
    assert dict(counts_after) == {"stories": 1, "sent": 1}


def test_exact_atom_variants_are_suppressed_in_the_item_transaction() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            outcome = repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "variant-prefix-url",
                        strategy_id="1018",
                        score=91,
                        title="BREAKING: Bitcoin ETF inflows accelerate after approval https://example.com/live",
                    ),
                    _event(
                        "variant-case-space",
                        strategy_id="1019",
                        score=None,
                        title="  bitcoin   ETF inflows accelerate after approval  ",
                    ),
                    _event(
                        "variant-unicode-punctuation",
                        strategy_id="1018",
                        score=92,
                        title="“Ｂｉｔｃｏｉｎ ETF inflows—accelerate after approval”",
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            rows = conn.execute(
                """
                SELECT item_id, status, notification_fingerprint,
                       comparison_identity_version, admission_policy_version,
                       admission_reason, suppressed_by_item_id
                  FROM news_push_deliveries
                 ORDER BY item_id
                """
            ).fetchall()
            state = conn.execute(
                """
                SELECT total_count, pending_count, suppressed_count
                  FROM news_push_state
                 WHERE singleton_key = 'current'
                """
            ).fetchone()
            health = repository.push_health_snapshot(now_ms=BASE_MS + 2_000)
    finally:
        conn.close()

    leaders = [row for row in rows if row["status"] == "pending"]
    suppressed = [row for row in rows if row["status"] == "suppressed"]
    assert outcome["push_outbox_writes"] == 3
    assert len(leaders) == 1
    assert len(suppressed) == 2
    assert len({row["notification_fingerprint"] for row in rows}) == 1
    assert all(row["suppressed_by_item_id"] == leaders[0]["item_id"] for row in suppressed)
    assert all(row["admission_reason"] == "exact_atom_suppressed" for row in suppressed)
    assert dict(state) == {"total_count": 3, "pending_count": 1, "suppressed_count": 2}
    assert health["payload_schema_version"] == "news_item_push_v2"
    assert health["comparison_identity_version"] == "news_exact_atom_identity_v1"
    assert health["admission_policy_version"] == "news_push_exact_atom_admission_v1"
    assert health["suppressed_count"] == 2
    assert health["delivery_24h"] == {
        "completed": 0,
        "sent": 0,
        "terminal": 0,
        "latency_p95_ms": None,
        "slo_met": None,
        "sample_complete": True,
    }
    assert health["suppression_sample_complete"] is True
    assert len(health["recent_suppressions"]) == 2
    assert all("original_title" not in evidence for evidence in health["recent_suppressions"])


def test_numeric_changes_and_similar_nonexact_titles_remain_independent_alerts() -> None:
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
                    _event(
                        "quake-64",
                        strategy_id="1018",
                        score=91,
                        title="Magnitude 6.4 earthquake strikes northern Chile",
                    ),
                    _event(
                        "quake-68",
                        strategy_id="1018",
                        score=92,
                        title="Magnitude 6.8 earthquake strikes northern Chile",
                    ),
                    _event(
                        "similar-a",
                        strategy_id="1019",
                        score=None,
                        title="Bitcoin ETF inflows accelerate after approval",
                    ),
                    _event(
                        "similar-b",
                        strategy_id="1019",
                        score=None,
                        title="Bitcoin ETF inflows accelerate following approval",
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            rebuild_news_projection(repository, now_ms=BASE_MS + 2_000)
            rows = conn.execute(
                """
                SELECT source_payload ->> 'provider_event_id' AS provider_event_id,
                       status, notification_fingerprint
                  FROM news_push_deliveries
                 ORDER BY provider_event_id
                """
            ).fetchall()
            story_count = conn.execute("SELECT count(*) AS value FROM news_stories").fetchone()["value"]
    finally:
        conn.close()

    assert {row["status"] for row in rows} == {"pending"}
    assert len({row["notification_fingerprint"] for row in rows}) == 4
    assert story_count < len(rows)


def test_exact_atom_window_uses_first_durable_leader_and_suppressed_rows_do_not_extend_it() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        title = "Bitcoin ETF inflows accelerate after approval"
        first_published_at_ms = BASE_MS + 500
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "window-leader",
                        strategy_id="1018",
                        score=91,
                        title=title,
                        published_at_ms=first_published_at_ms,
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
            leader_id = conn.execute("SELECT item_id FROM news_push_deliveries WHERE status = 'pending'").fetchone()[
                "item_id"
            ]
            assert repository.fence_item_push(item_id=leader_id, attempted_at_ms=BASE_MS + 2_000) is not None
            assert repository.terminalize_item_push(
                item_id=leader_id,
                error_code="news_item_push_feishu_transport_failed",
                now_ms=BASE_MS + 2_001,
            )

        boundary_published_at_ms = first_published_at_ms + 12 * 60 * 60_000
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "window-boundary",
                        strategy_id="1019",
                        score=None,
                        title=title,
                        published_at_ms=boundary_published_at_ms,
                    ),
                ),
                observed_at_ms=boundary_published_at_ms + 500,
                ingest_mode="live",
            )
        with conn.transaction():
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "window-plus-one",
                        strategy_id="1018",
                        score=92,
                        title=title,
                        published_at_ms=boundary_published_at_ms + 1,
                    ),
                ),
                observed_at_ms=boundary_published_at_ms + 501,
                ingest_mode="live",
            )
            rows = conn.execute(
                """
                SELECT source_payload ->> 'provider_event_id' AS provider_event_id,
                       status, suppressed_by_item_id, item_id
                  FROM news_push_deliveries
                 ORDER BY provider_event_id
                """
            ).fetchall()
    finally:
        conn.close()

    by_provider = {str(row["provider_event_id"]): row for row in rows}
    assert by_provider["window-leader"]["status"] == "terminal"
    assert by_provider["window-boundary"]["status"] == "suppressed"
    assert by_provider["window-boundary"]["suppressed_by_item_id"] == leader_id
    assert by_provider["window-plus-one"]["status"] == "pending"
    assert by_provider["window-plus-one"]["suppressed_by_item_id"] is None


def test_exact_atom_batch_admission_is_invariant_to_frame_permutation() -> None:
    conn = connect_postgres_test(read_only=False)
    try:

        def run(order: tuple[str, ...]) -> dict[str, str]:
            reset_postgres_schema(conn)
            repository = NewsRepository(conn)
            with conn.transaction():
                repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
                repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
                repository.record_opennews_events(
                    source=opennews_source(),
                    events=tuple(
                        _event(
                            provider_id,
                            strategy_id="1018",
                            score=91,
                            title="Bitcoin ETF inflows accelerate after approval",
                        )
                        for provider_id in order
                    ),
                    observed_at_ms=BASE_MS + 1_000,
                    ingest_mode="live",
                )
                return {
                    str(row["provider_event_id"]): str(row["status"])
                    for row in conn.execute(
                        """
                        SELECT source_payload ->> 'provider_event_id' AS provider_event_id,
                               status
                          FROM news_push_deliveries
                         ORDER BY provider_event_id
                        """
                    ).fetchall()
                }

        forward = run(("permutation-a", "permutation-b", "permutation-c"))
        reverse = run(("permutation-c", "permutation-b", "permutation-a"))
    finally:
        conn.close()

    assert reverse == forward
    assert list(forward.values()).count("pending") == 1
    assert list(forward.values()).count("suppressed") == 2


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
    published_at_ms: int | None = None,
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
            published_at_ms=BASE_MS + 500 if published_at_ms is None else published_at_ms,
            reporting_origin="opennews",
        ),
    )
