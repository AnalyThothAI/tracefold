from __future__ import annotations

import asyncio
from typing import Any

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    reset_postgres_schema,
)
from tracefold.app.database import WorkerDatabase
from tracefold.app.worker_capabilities import FiniteOperations
from tracefold.news.models import NewsFeedEntry, NewsFeedFetch
from tracefold.news.opennews import OpenNewsEvent
from tracefold.news.push import NewsItemPush, NewsPushReceipt
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import opennews_source, public_rss_sources
from tracefold.news.title_presentation import NewsItemTitlePresentation
from tracefold.news.title_presentation_store import TitlePresentationStore
from tracefold.platform.config.settings import Settings

BASE_MS = 1_785_560_400_000


def test_live_item_atomically_creates_exact_presentation_and_push_identity() -> None:
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
                        "presentation-atomic",
                        title="Iran has not decided to resume US talks",
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )
        row = conn.execute(
            """
            SELECT item.item_id, item.title,
                   presentation.source_title_fingerprint,
                   presentation.original_title,
                   presentation.state,
                   delivery.source_title_fingerprint AS delivery_fingerprint,
                   delivery.status AS delivery_status
              FROM news_items item
              JOIN news_item_title_presentations presentation
                ON presentation.item_id = item.item_id
              JOIN news_push_deliveries delivery
                ON delivery.item_id = presentation.item_id
               AND delivery.source_title_fingerprint =
                   presentation.source_title_fingerprint
             WHERE item.provider_record_id = 'presentation-atomic'
            """
        ).fetchone()
    finally:
        conn.close()

    assert outcome["push_outbox_writes"] == 1
    assert row is not None
    assert row["title"] == "Iran has not decided to resume US talks"
    assert row["original_title"] == row["title"]
    assert row["source_title_fingerprint"] == ("201c1016b28c5c46b8ce23325ea63477dd5e442c47436f029928800fcd0f0b75")
    assert row["delivery_fingerprint"] == row["source_title_fingerprint"]
    assert row["state"] == "pending"
    assert row["delivery_status"] == "pending"


def test_deepl_success_resolves_one_immutable_shared_presentation() -> None:
    conn = connect_postgres_test(read_only=False)
    database: WorkerDatabase | None = None
    presentation: NewsItemTitlePresentation | None = None
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_sources((opennews_source(),), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=False, now_ms=BASE_MS)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "deepl-success",
                        title="Iran has not decided to resume US talks",
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )

        deepl = _Provider("伊朗尚未决定恢复与美国谈判")
        deepseek = _Provider("不应调用")
        database = WorkerDatabase.create(
            Settings(storage=postgres_settings_storage()),
            telemetry=None,
        )
        presentation = NewsItemTitlePresentation(
            db=database,
            deepl=deepl,
            deepseek=deepseek,
            clock_ms=lambda: BASE_MS + 2_000,
        )

        assert asyncio.run(presentation.turn()) is True
        assert asyncio.run(presentation.turn()) is False
        row = conn.execute(
            """
            SELECT state, original_title, display_title, outcome, provider,
                   policy_version, fallback_code, attempted_at_ms,
                   resolved_at_ms, duration_ms
              FROM news_item_title_presentations
            """
        ).fetchone()
    finally:
        if presentation is not None:
            asyncio.run(presentation.close())
        if database is not None:
            database.close_executors()
            asyncio.run(database.aclose())
        conn.close()

    assert deepl.titles == ["Iran has not decided to resume US talks"]
    assert deepseek.titles == []
    assert row == {
        "state": "resolved",
        "original_title": "Iran has not decided to resume US talks",
        "display_title": "伊朗尚未决定恢复与美国谈判",
        "outcome": "translated",
        "provider": "deepl",
        "policy_version": "news_title_zh_v1",
        "fallback_code": None,
        "attempted_at_ms": BASE_MS + 2_000,
        "resolved_at_ms": BASE_MS + 2_000,
        "duration_ms": 0,
    }


def _event(provider_record_id: str, *, title: str) -> OpenNewsEvent:
    metadata: dict[str, Any] = {
        "score": 91,
        "strategies": [{"id": "1018", "name": "News Score > 70"}],
    }
    return OpenNewsEvent(
        provider_record_id=provider_record_id,
        observation_kind="report",
        provider_metadata=metadata,
        entry=NewsFeedEntry(
            guid=provider_record_id,
            link=f"https://example.com/{provider_record_id}",
            title=title,
            description="",
            published_at_ms=BASE_MS + 500,
            reporting_origin="opennews",
        ),
    )


class _Provider:
    def __init__(self, result: str) -> None:
        self.result = result
        self.titles: list[str] = []

    async def translate(self, title: str) -> str:
        self.titles.append(title)
        return self.result

    async def close(self) -> None:
        return None


def test_push_consumes_the_exact_resolved_presentation_without_private_snapshot() -> None:
    conn = connect_postgres_test(read_only=False)
    database: WorkerDatabase | None = None
    finite: FiniteOperations | None = None
    presentation: NewsItemTitlePresentation | None = None
    push: NewsItemPush | None = None
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
                        "push-shared-presentation",
                        title="Iran has not decided to resume US talks",
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
                ingest_mode="live",
            )

        database = WorkerDatabase.create(
            Settings(storage=postgres_settings_storage()),
            telemetry=None,
        )
        presentation = NewsItemTitlePresentation(
            db=database,
            deepl=_Provider("伊朗尚未决定恢复与美国谈判"),
            deepseek=None,
            clock_ms=lambda: BASE_MS + 2_000,
        )
        assert asyncio.run(presentation.turn()) is True

        finite = FiniteOperations()
        sender = _Sender()
        push = NewsItemPush(
            db=database,
            finite_operations=finite,
            sender=sender,
            delivery_available=True,
            clock_ms=lambda: BASE_MS + 3_000,
        )
        assert asyncio.run(push.turn()) is True
        row = conn.execute(
            """
            SELECT delivery.status, delivery.source_title_fingerprint,
                   delivery.legacy_presentation_snapshot,
                   presentation.display_title, presentation.original_title
              FROM news_push_deliveries delivery
              JOIN news_item_title_presentations presentation
                ON presentation.item_id = delivery.item_id
               AND presentation.source_title_fingerprint =
                   delivery.source_title_fingerprint
            """
        ).fetchone()
    finally:
        if push is not None:
            asyncio.run(push.close())
        if presentation is not None:
            asyncio.run(presentation.close())
        if finite is not None:
            finite.close()
        if database is not None:
            database.close_executors()
            asyncio.run(database.aclose())
        conn.close()

    assert len(sender.calls) == 1
    assert sender.calls[0]["presentation"]["display_title"] == "伊朗尚未决定恢复与美国谈判"
    assert sender.calls[0]["presentation"]["original_title"] == ("Iran has not decided to resume US talks")
    assert row["status"] == "sent"
    assert row["source_title_fingerprint"] is not None
    assert row["legacy_presentation_snapshot"] is None
    assert row["display_title"] == "伊朗尚未决定恢复与美国谈判"


def test_live_push_blocking_title_is_prioritized_over_large_rss_backlog() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        rss_source = public_rss_sources()[0]
        claim_token = "00000000-0000-0000-0000-000000000043"
        with conn.transaction():
            repository.sync_sources((rss_source, opennews_source()), now_ms=BASE_MS)
            repository.reconcile_item_push(delivery_available=True, now_ms=BASE_MS)
            claim = repository.claim_due_rss_source(
                now_ms=BASE_MS,
                claim_token=claim_token,
                lease_expires_at_ms=BASE_MS + 60_000,
            )
            assert claim is not None
            repository.record_rss_fetch(
                source=rss_source,
                claim_token=claim_token,
                fetch=NewsFeedFetch(
                    status_code=200,
                    entries=tuple(
                        NewsFeedEntry(
                            guid=f"rss-backlog-{index}",
                            link=f"https://example.com/rss-backlog-{index}",
                            title=f"RSS backlog title {index}",
                            published_at_ms=BASE_MS,
                        )
                        for index in range(5)
                    ),
                    entries_seen=5,
                ),
                finished_at_ms=BASE_MS + 1_000,
            )
            conn.execute(
                """
                INSERT INTO news_item_title_presentations (
                  item_id, source_title_fingerprint, original_title, state,
                  created_at_ms, updated_at_ms
                )
                SELECT item.item_id,
                       encode(
                         sha256(convert_to('RSS historical title ' || n, 'UTF8')),
                         'hex'
                       ),
                       'RSS historical title ' || n,
                       'pending', %s + n, %s + n
                  FROM (
                    SELECT item_id
                      FROM news_items
                     WHERE source_id = %s
                     ORDER BY item_id
                     LIMIT 1
                  ) item
                  CROSS JOIN generate_series(1, 95) n
                """,
                (BASE_MS + 1_000, BASE_MS + 1_000, rss_source.source_id),
            )
            repository.record_opennews_events(
                source=opennews_source(),
                events=(_event("live-priority", title="Live push title"),),
                observed_at_ms=BASE_MS + 2_000,
                ingest_mode="live",
            )

        selected = TitlePresentationStore(conn).peek_pending()
        counts = conn.execute(
            """
            SELECT (SELECT count(*) FROM news_item_title_presentations)
                     AS presentations,
                   (SELECT count(*) FROM news_push_deliveries) AS pushes
            """
        ).fetchone()
    finally:
        conn.close()

    assert dict(counts) == {"presentations": 101, "pushes": 1}
    assert selected is not None
    assert selected["original_title"] == "Live push title"


class _Sender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send(
        self,
        source_payload: dict[str, Any],
        presentation: dict[str, Any],
    ) -> NewsPushReceipt:
        self.calls.append({"source_payload": source_payload, "presentation": presentation})
        return NewsPushReceipt(
            provider="feishu",
            receipt_id="receipt-1",
            details={"code": 0, "status_code": 200},
        )

    def close(self) -> None:
        return None
