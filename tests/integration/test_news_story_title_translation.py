from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tracefold.news import NewsInterface, NewsRepository, opennews_source, parse_opennews_message
from tracefold.news.title_translation import (
    TITLE_TRANSLATION_LOCALE,
    TITLE_TRANSLATION_PROMPT_VERSION,
    TITLE_TRANSLATION_WORKFLOW_VERSION,
    story_title_fingerprint,
)

BASE_MS = 1_786_080_000_000
RETRY_DELAYS_MS = (30_000, 120_000)
RETENTION_MS = 48 * 60 * 60 * 1_000


def _event(record_id: str, *, title: str, score: float, published_at_ms: int):
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": record_id,
                "text": title,
                "description": f"Details for {record_id}",
                "newsType": "Reuters",
                "engineType": "news",
                "link": f"https://example.com/{record_id}",
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


def _reconcile(
    repository: NewsRepository,
    *,
    now_ms: int,
    configured: bool,
) -> dict[str, int]:
    return repository.reconcile_story_title_translation_targets(
        now_ms=now_ms,
        configured=configured,
        locale=TITLE_TRANSLATION_LOCALE,
        workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
        prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
        max_attempts=3,
        retry_delays_ms=RETRY_DELAYS_MS,
        retention_ms=RETENTION_MS,
    )


def _seed(repository: NewsRepository, conn: Any) -> None:
    with conn.transaction():
        repository.sync_source(opennews_source(), now_ms=BASE_MS)
        repository.record_opennews_events(
            source=opennews_source(),
            events=(
                _event(
                    "eligible-en-score",
                    title="Bitcoin rallies as BTC gains 10%",
                    score=95,
                    published_at_ms=BASE_MS + 1,
                ),
                _event(
                    "eligible-en",
                    title="Bitcoin rallies as BTC gains 10% after market open",
                    score=71,
                    published_at_ms=BASE_MS + 2,
                ),
                _event(
                    "threshold-en",
                    title="Ether holds exactly at the score threshold",
                    score=70,
                    published_at_ms=BASE_MS + 3,
                ),
                _event(
                    "eligible-zh",
                    title="比特币价格上涨",
                    score=80,
                    published_at_ms=BASE_MS + 4,
                ),
            ),
            observed_at_ms=BASE_MS + 10,
        )
        repository.rebuild_stories(now_ms=BASE_MS + 10)


def test_story_title_translation_exact_binding_threshold_search_and_retention() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        privileges = conn.execute(
            """
            SELECT has_table_privilege(
                     'tracefold_serve',
                     'news_story_title_translations',
                     'SELECT'
                   ) AS serve_select,
                   has_table_privilege(
                     'tracefold_serve',
                     'news_story_title_translations',
                     'INSERT'
                   ) AS serve_insert,
                   has_table_privilege(
                     'tracefold_workers',
                     'news_story_title_translations',
                     'SELECT,INSERT,UPDATE,DELETE'
                   ) AS workers_business
            """
        ).fetchone()
        assert privileges == {
            "serve_select": True,
            "serve_insert": False,
            "workers_business": True,
        }
        empty_health = repository.story_title_translation_health_snapshot(
            now_ms=BASE_MS,
            configured=False,
        )
        assert empty_health["status"] == "ready"
        assert empty_health["reasons"] == []
        assert empty_health["eligible_count"] == 0
        _seed(repository, conn)

        with conn.transaction():
            first = _reconcile(repository, now_ms=BASE_MS + 20, configured=True)
        assert first["eligible"] == 2

        rows = conn.execute(
            """
            SELECT translation.*, item.provider_record_id
              FROM news_story_title_translations translation
              JOIN news_stories story ON story.story_id = translation.story_id
              JOIN news_items item
                ON item.item_id = story.representative_item_id
             ORDER BY item.provider_record_id
            """
        ).fetchall()
        assert [(row["provider_record_id"], row["status"]) for row in rows] == [
            ("eligible-en", "pending"),
            ("eligible-zh", "ready"),
        ]
        chinese = next(row for row in rows if row["provider_record_id"] == "eligible-zh")
        assert chinese["result_kind"] == "source_zh"
        assert chinese["translated_title"] == "比特币价格上涨"
        assert chinese["attempt_count"] == 0

        target = repository.peek_story_title_translation_target(
            now_ms=BASE_MS + 20,
            locale=TITLE_TRANSLATION_LOCALE,
            workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
            prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
        )
        assert target is not None
        assert target["source_title"] == "Bitcoin rallies as BTC gains 10% after market open"
        selected_item_id = repository.story_provider_evidence(story_ids=(str(target["story_id"]),))[
            str(target["story_id"])
        ]["provider_evidence"]["item_id"]
        selected_record = conn.execute(
            "SELECT provider_record_id FROM news_items WHERE item_id = %s",
            (selected_item_id,),
        ).fetchone()
        assert selected_record["provider_record_id"] == "eligible-en-score"
        with conn.transaction():
            claim = repository.claim_story_title_translation(
                story_id=str(target["story_id"]),
                source_title_fingerprint=str(target["source_title_fingerprint"]),
                locale=TITLE_TRANSLATION_LOCALE,
                workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                lease_owner="test-runtime",
                lease_token="lease-1",
                lease_expires_at_ms=BASE_MS + 30_000,
                now_ms=BASE_MS + 21,
                max_attempts=3,
            )
        assert claim is not None
        with conn.transaction():
            assert repository.complete_story_title_translation(
                story_id=str(claim["story_id"]),
                source_title_fingerprint=str(claim["source_title_fingerprint"]),
                locale=TITLE_TRANSLATION_LOCALE,
                workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                lease_owner="test-runtime",
                lease_token="lease-1",
                title_zh="比特币上涨，BTC 在开盘后涨幅达 10%",
                provider="openai_compatible",
                model="model-a",
                now_ms=BASE_MS + 121,
            )

        feed = NewsInterface(repository).get_feed()
        translated = next(story for story in feed["stories"] if story["story_id"] == claim["story_id"])
        threshold = next(story for story in feed["stories"] if story["title"].startswith("Ether holds"))
        assert translated["title"] == "Bitcoin rallies as BTC gains 10% after market open"
        assert translated["description"] == "Details for eligible-en"
        assert translated["title_translation"] == {
            "state": "ready",
            "title_zh": "比特币上涨，BTC 在开盘后涨幅达 10%",
            "source_title": "Bitcoin rallies as BTC gains 10% after market open",
            "source_title_fingerprint": claim["source_title_fingerprint"],
            "locale": "zh-CN",
            "workflow_version": TITLE_TRANSLATION_WORKFLOW_VERSION,
            "prompt_version": TITLE_TRANSLATION_PROMPT_VERSION,
        }
        assert threshold["title_translation"] is None
        assert [story["story_id"] for story in NewsInterface(repository).get_feed(q="涨幅")["stories"]] == [
            claim["story_id"]
        ]

        # A ready exact-bound current target survives retention without a
        # second model attempt.
        with conn.transaction():
            retained = _reconcile(
                repository,
                now_ms=BASE_MS + RETENTION_MS + 1_000,
                configured=True,
            )
        assert retained["pruned"] == 0
        retained_row = conn.execute(
            """
            SELECT status, attempt_count, translated_title
              FROM news_story_title_translations
             WHERE story_id = %s
               AND source_title_fingerprint = %s
            """,
            (claim["story_id"], claim["source_title_fingerprint"]),
        ).fetchone()
        assert retained_row == {
            "status": "ready",
            "attempt_count": 1,
            "translated_title": "比特币上涨，BTC 在开盘后涨幅达 10%",
        }

        # If the current max score falls to the threshold, the old ready row
        # remains historical state but is neither presented nor searchable.
        with conn.transaction():
            conn.execute(
                """
                UPDATE news_items
                   SET provider_metadata = jsonb_set(
                         provider_metadata,
                         '{score}',
                         '70'::jsonb
                       )
                 WHERE provider_record_id IN ('eligible-en', 'eligible-en-score')
                """
            )
        rescored = NewsInterface(repository).get_feed()
        rescored_story = next(story for story in rescored["stories"] if story["story_id"] == claim["story_id"])
        assert rescored_story["title_translation"] is None
        assert NewsInterface(repository).get_feed(q="涨幅")["stories"] == []

    finally:
        conn.close()


def test_changed_display_title_creates_new_work_and_never_attaches_stale_output() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        _seed(repository, conn)
        with conn.transaction():
            _reconcile(repository, now_ms=BASE_MS + 20, configured=True)
            target = repository.peek_story_title_translation_target(
                now_ms=BASE_MS + 20,
                locale=TITLE_TRANSLATION_LOCALE,
                workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
            )
            assert target is not None
            claim = repository.claim_story_title_translation(
                story_id=str(target["story_id"]),
                source_title_fingerprint=str(target["source_title_fingerprint"]),
                locale=TITLE_TRANSLATION_LOCALE,
                workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                lease_owner="test-runtime",
                lease_token="title-change-lease",
                lease_expires_at_ms=BASE_MS + 30_000,
                now_ms=BASE_MS + 21,
                max_attempts=3,
            )
        assert claim is not None
        with conn.transaction():
            assert repository.complete_story_title_translation(
                story_id=str(claim["story_id"]),
                source_title_fingerprint=str(claim["source_title_fingerprint"]),
                locale=TITLE_TRANSLATION_LOCALE,
                workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                lease_owner="test-runtime",
                lease_token="title-change-lease",
                title_zh="旧译文关键词：比特币上涨 10%",
                provider="test",
                model="test-model",
                now_ms=BASE_MS + 121,
            )
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "eligible-en",
                        title="Bitcoin extends rally as BTC gains 10% after market open",
                        score=71,
                        published_at_ms=BASE_MS + 2,
                    ),
                ),
                observed_at_ms=BASE_MS + 1_000,
            )
            repository.rebuild_stories(now_ms=BASE_MS + 1_000)

        changed = next(
            story
            for story in NewsInterface(repository).get_feed()["stories"]
            if story["title"] == "Bitcoin extends rally as BTC gains 10% after market open"
        )
        assert changed["title_translation"]["state"] == "pending"
        assert changed["title_translation"]["title_zh"] is None
        assert NewsInterface(repository).get_feed(q="旧译文关键词")["stories"] == []

        with conn.transaction():
            _reconcile(repository, now_ms=BASE_MS + 1_001, configured=True)
        current = repository.story_title_translations(story_ids=(changed["story_id"],))[changed["story_id"]]
        assert current["source_title"] == "Bitcoin extends rally as BTC gains 10% after market open"
        assert current["status"] == "pending"
    finally:
        conn.close()


def test_translation_queue_prioritizes_newest_story_over_older_due_backlog() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        _seed(repository, conn)
        with conn.transaction():
            _reconcile(repository, now_ms=BASE_MS + 20, configured=True)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "newest-eligible-en",
                        title="Solana validator client releases emergency patch",
                        score=88,
                        published_at_ms=BASE_MS + 200,
                    ),
                ),
                observed_at_ms=BASE_MS + 210,
            )
            repository.rebuild_stories(now_ms=BASE_MS + 210)
            _reconcile(repository, now_ms=BASE_MS + 220, configured=True)

        target = repository.peek_story_title_translation_target(
            now_ms=BASE_MS + 220,
            locale=TITLE_TRANSLATION_LOCALE,
            workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
            prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
        )
        assert target is not None
        assert target["source_title"] == "Solana validator client releases emergency patch"
    finally:
        conn.close()


def test_unconfigured_translation_is_durable_and_revives_when_configured() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        _seed(repository, conn)

        with conn.transaction():
            _reconcile(repository, now_ms=BASE_MS + 20, configured=False)
        unavailable = conn.execute(
            """
            SELECT status, last_error, attempt_count
              FROM news_story_title_translations translation
              JOIN news_stories story ON story.story_id = translation.story_id
              JOIN news_items item
                ON item.item_id = story.representative_item_id
             WHERE item.provider_record_id = 'eligible-en'
            """
        ).fetchone()
        assert unavailable == {
            "status": "unavailable",
            "last_error": "news_title_translation_not_configured",
            "attempt_count": 0,
        }

        health = repository.story_title_translation_health_snapshot(
            now_ms=BASE_MS + 20,
            configured=False,
        )
        assert health["status"] == "degraded"
        assert health["configured"] is False
        assert health["eligible_count"] == 2
        assert health["ready_count"] == 1
        assert health["unavailable_count"] == 1

        with conn.transaction():
            _reconcile(repository, now_ms=BASE_MS + 21, configured=True)
        revived = conn.execute(
            """
            SELECT status, last_error, next_attempt_at_ms
              FROM news_story_title_translations translation
              JOIN news_stories story ON story.story_id = translation.story_id
              JOIN news_items item
                ON item.item_id = story.representative_item_id
             WHERE item.provider_record_id = 'eligible-en'
            """
        ).fetchone()
        assert revived == {
            "status": "pending",
            "last_error": None,
            "next_attempt_at_ms": BASE_MS + 21,
        }
    finally:
        conn.close()


def test_unconfigured_health_is_ready_when_every_eligible_title_is_already_chinese() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        with conn.transaction():
            repository.sync_source(opennews_source(), now_ms=BASE_MS)
            repository.record_opennews_events(
                source=opennews_source(),
                events=(
                    _event(
                        "only-chinese",
                        title="比特币价格继续上涨",
                        score=90,
                        published_at_ms=BASE_MS + 1,
                    ),
                ),
                observed_at_ms=BASE_MS + 10,
            )
            repository.rebuild_stories(now_ms=BASE_MS + 10)
            _reconcile(repository, now_ms=BASE_MS + 20, configured=False)

        health = repository.story_title_translation_health_snapshot(
            now_ms=BASE_MS + 20,
            configured=False,
        )
        assert health["eligible_count"] == 1
        assert health["ready_count"] == 1
        assert health["status"] == "ready"
        assert health["reasons"] == []
    finally:
        conn.close()


@pytest.mark.parametrize(
    (
        "status",
        "source_title",
        "result_kind",
        "translated_title",
        "attempt_count",
        "attempts",
        "next_attempt_at_ms",
        "lease_owner",
        "lease_token",
        "lease_expires_at_ms",
    ),
    (
        ("pending", "Bitcoin rises", None, None, 0, [], None, None, None, None),
        (
            "running",
            "Bitcoin rises",
            None,
            None,
            1,
            [{"attempted_at_ms": 1, "outcome": "started"}],
            None,
            "owner",
            "token",
            None,
        ),
        ("ready", "比特币上涨", "source_zh", "比特币上涨", 0, [], None, None, None, None),
    ),
)
def test_translation_state_requires_its_due_lease_or_completion_clock(
    status: str,
    source_title: str,
    result_kind: str | None,
    translated_title: str | None,
    attempt_count: int,
    attempts: list[dict[str, Any]],
    next_attempt_at_ms: int | None,
    lease_owner: str | None,
    lease_token: str | None,
    lease_expires_at_ms: int | None,
) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        with pytest.raises(CheckViolation), conn.transaction():
            conn.execute(
                """
                INSERT INTO news_story_title_translations (
                  story_id, source_title, source_title_fingerprint,
                  source_raw_title_fingerprint, locale, workflow_version,
                  prompt_version, status, result_kind, translated_title,
                  provider, model, attempt_count, attempts,
                  next_attempt_at_ms, lease_owner, lease_token,
                  lease_expires_at_ms, last_error, completed_at_ms,
                  created_at_ms, updated_at_ms
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  NULL, NULL, %s, %s, %s, %s, %s, %s, NULL, NULL, 1, 1
                )
                """,
                (
                    "f" * 64,
                    source_title,
                    story_title_fingerprint(source_title),
                    "e" * 64,
                    TITLE_TRANSLATION_LOCALE,
                    TITLE_TRANSLATION_WORKFLOW_VERSION,
                    TITLE_TRANSLATION_PROMPT_VERSION,
                    status,
                    result_kind,
                    translated_title,
                    attempt_count,
                    Jsonb(attempts),
                    next_attempt_at_ms,
                    lease_owner,
                    lease_token,
                    lease_expires_at_ms,
                ),
            )
    finally:
        conn.close()


def test_translation_attempt_ledger_recovers_lease_and_reports_terminal_failures() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        _seed(repository, conn)
        with conn.transaction():
            _reconcile(repository, now_ms=BASE_MS + 20, configured=True)
        target = repository.peek_story_title_translation_target(
            now_ms=BASE_MS + 20,
            locale=TITLE_TRANSLATION_LOCALE,
            workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
            prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
        )
        assert target is not None

        old_attempted_at_ms = BASE_MS - 25 * 60 * 60 * 1_000
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO news_story_title_translations (
                  story_id, source_title, source_title_fingerprint,
                  source_raw_title_fingerprint, locale, workflow_version,
                  prompt_version, status, result_kind, translated_title,
                  provider, model, attempt_count, attempts,
                  next_attempt_at_ms, lease_owner, lease_token,
                  lease_expires_at_ms, last_error, completed_at_ms,
                  created_at_ms, updated_at_ms
                )
                SELECT story_id, source_title, source_title_fingerprint,
                       source_raw_title_fingerprint, locale,
                       'obsolete-workflow', prompt_version,
                       'running', NULL, NULL, NULL, NULL, 1,
                       jsonb_build_array(
                         jsonb_build_object(
                           'attempted_at_ms', cast(%s AS bigint),
                           'outcome', 'started'
                         )
                       ),
                       NULL, 'old-runtime', 'old-lease', %s,
                       NULL, NULL, %s, %s
                  FROM news_story_title_translations
                 WHERE story_id = %s
                   AND source_title_fingerprint = %s
                   AND workflow_version = %s
                """,
                (
                    old_attempted_at_ms,
                    BASE_MS + 19,
                    old_attempted_at_ms,
                    old_attempted_at_ms,
                    target["story_id"],
                    target["source_title_fingerprint"],
                    TITLE_TRANSLATION_WORKFLOW_VERSION,
                ),
            )
            obsolete_recovery = _reconcile(repository, now_ms=BASE_MS + 20, configured=True)
        assert obsolete_recovery["recovered"] == 1
        obsolete = conn.execute(
            """
            SELECT status, last_error, completed_at_ms
              FROM news_story_title_translations
             WHERE story_id = %s
               AND source_title_fingerprint = %s
               AND workflow_version = 'obsolete-workflow'
            """,
            (target["story_id"], target["source_title_fingerprint"]),
        ).fetchone()
        assert obsolete == {
            "status": "unavailable",
            "last_error": "news_title_translation_workflow_obsolete",
            "completed_at_ms": BASE_MS + 20,
        }

        with conn.transaction():
            first_claim = repository.claim_story_title_translation(
                story_id=str(target["story_id"]),
                source_title_fingerprint=str(target["source_title_fingerprint"]),
                locale=TITLE_TRANSLATION_LOCALE,
                workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                lease_owner="test-runtime",
                lease_token="lease-1",
                lease_expires_at_ms=BASE_MS + 30,
                now_ms=BASE_MS + 21,
                max_attempts=3,
            )
        assert first_claim is not None
        with conn.transaction():
            recovered = _reconcile(repository, now_ms=BASE_MS + 31, configured=True)
        assert recovered["recovered"] == 1

        row = conn.execute(
            """
            SELECT status, attempt_count, attempts, next_attempt_at_ms
              FROM news_story_title_translations
             WHERE story_id = %s
               AND source_title_fingerprint = %s
            """,
            (target["story_id"], target["source_title_fingerprint"]),
        ).fetchone()
        assert row["status"] == "retry_wait"
        assert row["attempt_count"] == 1
        assert row["attempts"] == [
            {
                "attempted_at_ms": BASE_MS + 21,
                "outcome": "failed",
                "duration_ms": 10,
                "error_code": "news_title_translation_interrupted",
            }
        ]
        due_at_ms = int(row["next_attempt_at_ms"])

        for attempt_number in (2, 3):
            with conn.transaction():
                claim = repository.claim_story_title_translation(
                    story_id=str(target["story_id"]),
                    source_title_fingerprint=str(target["source_title_fingerprint"]),
                    locale=TITLE_TRANSLATION_LOCALE,
                    workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                    prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                    lease_owner="test-runtime",
                    lease_token=f"lease-{attempt_number}",
                    lease_expires_at_ms=due_at_ms + 30_000,
                    now_ms=due_at_ms,
                    max_attempts=3,
                )
            assert claim is not None
            failed_at_ms = due_at_ms + 100
            with conn.transaction():
                assert repository.fail_story_title_translation(
                    story_id=str(target["story_id"]),
                    source_title_fingerprint=str(target["source_title_fingerprint"]),
                    locale=TITLE_TRANSLATION_LOCALE,
                    workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                    prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                    lease_owner="test-runtime",
                    lease_token=f"lease-{attempt_number}",
                    error_code="news_title_translation_provider_unavailable",
                    retryable=True,
                    retry_delays_ms=RETRY_DELAYS_MS,
                    max_attempts=3,
                    now_ms=failed_at_ms,
                )
            terminal = conn.execute(
                """
                SELECT status, attempt_count, next_attempt_at_ms
                  FROM news_story_title_translations
                 WHERE story_id = %s
                   AND source_title_fingerprint = %s
                """,
                (target["story_id"], target["source_title_fingerprint"]),
            ).fetchone()
            if attempt_number == 2:
                assert terminal["status"] == "retry_wait"
                due_at_ms = int(terminal["next_attempt_at_ms"])
            else:
                assert terminal == {
                    "status": "failed",
                    "attempt_count": 3,
                    "next_attempt_at_ms": None,
                }

        health = repository.story_title_translation_health_snapshot(
            now_ms=due_at_ms + 100,
            configured=True,
        )
        assert health["status"] == "degraded"
        assert health["failed_count"] == 1
        assert health["rolling_24h"] == {
            "attempted": 3,
            "succeeded": 0,
            "success_ratio": 0.0,
            "latency_p95_ms": 100,
            "failure_counts": {
                "news_title_translation_interrupted": 1,
                "news_title_translation_provider_unavailable": 2,
            },
        }
        feed = NewsInterface(repository).get_feed()
        failed_story = next(story for story in feed["stories"] if story["story_id"] == target["story_id"])
        assert failed_story["title_translation"]["state"] == "failed"
        assert failed_story["title_translation"]["title_zh"] is None
    finally:
        conn.close()
