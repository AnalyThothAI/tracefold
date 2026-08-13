from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news.push import NewsStoryPush

_BASE_MS = 1_785_560_400_000
_SMALL_STORY_COUNT = 2_252
_CAPPED_STORY_COUNT = 10_000


def test_small_story_ring_waits_until_25_seconds_before_revisiting_after_restart(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _seed_story_range(conn, start=1, stop=_SMALL_STORY_COUNT)
        runtime = _runtime(conn, runtime_id="ring-initial")

        assert asyncio.run(runtime.reconcile(now_ms=_BASE_MS))["inserted"] == 0
        with conn.transaction():
            conn.execute(
                """
                UPDATE news_items
                   SET provider_metadata = jsonb_set(
                         provider_metadata,
                         '{score}',
                         '90'::jsonb
                       ),
                       provider_score_updated_at_ms = %s,
                       push_eligibility_updated_at_ms = %s,
                       updated_at_ms = %s
                 WHERE item_id = 'ring-item-00001'
                """,
                (_BASE_MS + 1, _BASE_MS + 1, _BASE_MS + 1),
            )

        assert asyncio.run(runtime.reconcile(now_ms=_BASE_MS + 2_500))["inserted"] == 0
        assert asyncio.run(runtime.reconcile(now_ms=_BASE_MS + 5_000))["inserted"] == 0
        assert _reconcile_cursor(conn) is None

        restarted = _runtime(conn, runtime_id="ring-restarted")
        assert asyncio.run(restarted.reconcile(now_ms=_BASE_MS + 7_500))["inserted"] == 0
        assert asyncio.run(restarted.reconcile(now_ms=_BASE_MS + 24_999))["inserted"] == 0
        assert conn.execute("SELECT count(*) AS count FROM news_push_deliveries").fetchone() == {"count": 0}

        assert asyncio.run(restarted.reconcile(now_ms=_BASE_MS + 25_000))["inserted"] == 1
        assert conn.execute("SELECT selected_item_id FROM news_push_deliveries").fetchone() == {
            "selected_item_id": "ring-item-00001"
        }
    finally:
        conn.close()


def test_capped_story_ring_starts_its_next_ten_page_cycle_at_25_seconds(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _seed_story_range(conn, start=1, stop=_CAPPED_STORY_COUNT)
        runtime = _runtime(conn, runtime_id="capped-ring-initial")

        for page_index in range(10):
            result = asyncio.run(runtime.reconcile(now_ms=_BASE_MS + (page_index * 2_500)))
            assert result["inserted"] == 0
            expected_cursor = None if page_index == 9 else f"{(page_index + 1) * 1_000:064x}"
            assert _reconcile_cursor(conn) == expected_cursor

        restarted = _runtime(conn, runtime_id="capped-ring-restarted")
        assert asyncio.run(restarted.reconcile(now_ms=_BASE_MS + 24_999))["inserted"] == 0
        assert _reconcile_cursor(conn) is None

        assert asyncio.run(restarted.reconcile(now_ms=_BASE_MS + 25_000))["inserted"] == 0
        assert _reconcile_cursor(conn) == f"{1_000:064x}"
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


class _NoopDelivery:
    def close(self) -> None:
        return None


def _runtime(conn: Any, *, runtime_id: str) -> NewsStoryPush:
    return NewsStoryPush(
        db=_DirectDatabase(conn),
        finite_operations=object(),
        delivery=_NoopDelivery(),  # type: ignore[arg-type]
        runtime_id=runtime_id,
    )


def _reconcile_cursor(conn: Any) -> str | None:
    row = conn.execute(
        """
        SELECT reconcile_cursor_story_id
          FROM news_push_state
         WHERE singleton_key = 'current'
        """
    ).fetchone()
    return None if row["reconcile_cursor_story_id"] is None else str(row["reconcile_cursor_story_id"])


def _seed_story_range(conn: Any, *, start: int, stop: int) -> None:
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled, consecutive_failures,
              created_at_ms, updated_at_ms, source_kind, live_connected
            ) VALUES (
              'ring-source', 'Ring Source', 1, 'en', true, 0,
              0, 0, 'opennews', false
            )
            ON CONFLICT (source_id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, reporting_origin, title, description, lang,
              published_at_ms, first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, level, category, classification_source,
              classification_confidence, importance_score, importance_factors,
              active, created_at_ms, updated_at_ms,
              provider_score_updated_at_ms, push_eligibility_updated_at_ms
            )
            SELECT 'ring-item-' || lpad(series_no::text, 5, '0'),
                   'ring-source',
                   'ring-item-' || lpad(series_no::text, 5, '0'),
                   'ring-record-' || lpad(series_no::text, 5, '0'),
                   jsonb_build_object(
                     'score', 60,
                     'coins', jsonb_build_array(
                       jsonb_build_object('symbol', 'BTC', 'market_type', 'spot')
                     )
                   ),
                   'Ring Wire',
                   'Push reconcile ring story ' || series_no,
                   '',
                   'en',
                   %s,
                   %s,
                   %s,
                   'ring-item-fingerprint-' || lpad(series_no::text, 5, '0'),
                   'info',
                   'general',
                   'keyword',
                   1,
                   60,
                   '{}'::jsonb,
                   true,
                   %s,
                   %s,
                   %s,
                   %s
              FROM generate_series(%s::integer, %s::integer) series_no
            """,
            (
                _BASE_MS + 1,
                _BASE_MS - 1,
                _BASE_MS - 1,
                _BASE_MS - 1,
                _BASE_MS - 1,
                _BASE_MS - 1,
                _BASE_MS - 1,
                start,
                stop,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_description,
              scoring_item_id, level, category, importance_score,
              importance_factors, item_count, source_count,
              first_published_at_ms, last_published_at_ms,
              state_fingerprint, created_at_ms, updated_at_ms, facet_facts
            )
            SELECT lpad(to_hex(series_no), 64, '0'),
                   'ring-key-' || lpad(series_no::text, 5, '0'),
                   'Push reconcile ring story ' || series_no,
                   'ring-item-' || lpad(series_no::text, 5, '0'),
                   'ring-source',
                   'Push reconcile ring story ' || series_no,
                   '',
                   'ring-item-' || lpad(series_no::text, 5, '0'),
                   'info',
                   'general',
                   60,
                   '{}'::jsonb,
                   1,
                   1,
                   %s,
                   %s,
                   'ring-story-fingerprint-' || lpad(series_no::text, 5, '0'),
                   %s,
                   %s,
                   '{"source_ids":["ring-source"],"reporting_origins":["Ring Wire"]}'::jsonb
              FROM generate_series(%s::integer, %s::integer) series_no
            """,
            (
                _BASE_MS + 1,
                _BASE_MS + 1,
                _BASE_MS - 1,
                _BASE_MS - 1,
                start,
                stop,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_story_members(story_id, item_id)
            SELECT lpad(to_hex(series_no), 64, '0'),
                   'ring-item-' || lpad(series_no::text, 5, '0')
              FROM generate_series(%s::integer, %s::integer) series_no
            """,
            (start, stop),
        )
