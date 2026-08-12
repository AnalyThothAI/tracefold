from __future__ import annotations

import importlib

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0257_installs_news_bounded_read_state_and_indexes() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260813_0256")

        conn.execute(
            """
            UPDATE news_push_state
               SET baseline_at_ms = 100, updated_at_ms = 100
             WHERE singleton_key = 'current'
            """
        )
        conn.execute(
            """
            INSERT INTO news_push_deliveries (
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload, delivery_payload,
              payload_fingerprint, translation_status, status,
              delivery_attempts, next_attempt_at_ms, lease_owner,
              lease_token, lease_expires_at_ms, receipt, last_error,
              sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES
              (
                repeat('a', 64), 'item-a', 90, 100, '{}'::jsonb, NULL,
                NULL, 'not_requested', 'suppressed', 0, NULL,
                NULL, NULL, NULL, NULL, NULL, NULL, 100, 100
              ),
              (
                repeat('b', 64), 'item-b', 90, 200, '{}'::jsonb, NULL,
                NULL, 'pending', 'pending_translation', 0, 210,
                NULL, NULL, NULL, NULL, NULL, NULL, 200, 200
              ),
              (
                repeat('c', 64), 'item-c', 90, 300, '{}'::jsonb,
                jsonb_build_object('presentation', jsonb_build_object(
                  'translation_attempted_at_ms', 280,
                  'translation_duration_ms', 7,
                  'fallback_code', 'legacy_without_prompt'
                )), repeat('c', 64), 'not_needed', 'pending_delivery',
                0, 310, NULL, NULL, NULL, NULL, NULL, NULL, 300, 300
              ),
              (
                repeat('d', 64), 'item-d', 90, 400, '{}'::jsonb,
                jsonb_build_object('presentation', jsonb_build_object(
                  'prompt_version', 'title_zh_v2'
                )), repeat('d', 64), 'unavailable', 'retry_wait',
                1, 410, NULL, NULL, NULL, NULL, 'feishu_503', NULL, 400, 400
              ),
              (
                repeat('e', 64), 'item-e', 90, 500, '{}'::jsonb,
                jsonb_build_object('presentation', jsonb_build_object(
                  'prompt_version', 'title_zh_v2',
                  'translation_attempted_at_ms', 480,
                  'translation_duration_ms', 1234
                )), repeat('e', 64), 'translated', 'sent',
                1, NULL, NULL, NULL, NULL, '{}'::jsonb, NULL, 550, 500, 550
              ),
              (
                repeat('f', 64), 'item-f', 90, 600, '{}'::jsonb,
                jsonb_build_object('presentation', jsonb_build_object(
                  'prompt_version', 'title_zh_v2',
                  'fallback_code', 'news_push_translation_rate_limited',
                  'translation_attempted_at_ms', 580
                )), repeat('f', 64), 'unavailable', 'terminal',
                1, NULL, NULL, NULL, NULL, NULL,
                'Secret URL https://example.test', NULL, 600, 600
              )
            """
        )
        conn.commit()

        command.upgrade(config, "20260813_0257")

        state = conn.execute(
            """
            SELECT total_count, suppressed_count, pending_count, retry_count,
                   sent_count, terminal_count, latest_sent_at_ms,
                   latest_error, latest_error_at_ms
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        telemetry = conn.execute(
            """
            SELECT story_id, translation_prompt_version,
                   translation_attempted_at_ms, translation_duration_ms,
                   translation_fallback_code
              FROM news_push_deliveries
             WHERE story_id IN (
               repeat('c', 64), repeat('e', 64), repeat('f', 64)
             )
             ORDER BY story_id
            """
        ).fetchall()

        rows = conn.execute(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = ANY(%s)
            """,
            (
                [
                    "ix_news_items_member_provider_score",
                    "ix_news_push_deliveries_oldest_waiting",
                    "ix_news_push_deliveries_translation_attempted",
                    "ix_news_push_deliveries_completed_at",
                ],
            ),
        ).fetchall()
    finally:
        conn.close()

    assert state == {
        "total_count": 6,
        "suppressed_count": 1,
        "pending_count": 2,
        "retry_count": 1,
        "sent_count": 1,
        "terminal_count": 1,
        "latest_sent_at_ms": 550,
        "latest_error": "news_story_push_delivery_error",
        "latest_error_at_ms": 600,
    }
    assert telemetry == [
        {
            "story_id": "c" * 64,
            "translation_prompt_version": None,
            "translation_attempted_at_ms": None,
            "translation_duration_ms": None,
            "translation_fallback_code": None,
        },
        {
            "story_id": "e" * 64,
            "translation_prompt_version": "title_zh_v2",
            "translation_attempted_at_ms": 480,
            "translation_duration_ms": 1234,
            "translation_fallback_code": None,
        },
        {
            "story_id": "f" * 64,
            "translation_prompt_version": "title_zh_v2",
            "translation_attempted_at_ms": 580,
            "translation_duration_ms": None,
            "translation_fallback_code": "news_push_translation_rate_limited",
        },
    ]

    indexes = {str(row["indexname"]): " ".join(str(row["indexdef"]).split()) for row in rows}
    member_score = indexes["ix_news_items_member_provider_score"]
    assert "ON public.news_items USING btree (item_id," in member_score
    assert "((provider_metadata ->> 'score'::text))::numeric" in member_score
    assert "INCLUDE (provider_metadata)" in member_score
    assert "WHERE (jsonb_typeof((provider_metadata -> 'score'::text)) = 'number'::text)" in member_score
    assert "active" not in member_score

    assert "ix_news_push_deliveries_health" not in indexes

    oldest_waiting = indexes["ix_news_push_deliveries_oldest_waiting"]
    assert "threshold_observed_at_ms" in oldest_waiting
    assert "pending_translation" in oldest_waiting
    assert "retry_wait" in oldest_waiting

    translation = indexes["ix_news_push_deliveries_translation_attempted"]
    assert "translation_attempted_at_ms" in translation
    assert "translation_prompt_version" in translation
    assert "title_zh_v2" in translation
    assert "INCLUDE (translation_status, translation_duration_ms, translation_fallback_code)" in translation

    completed = indexes["ix_news_push_deliveries_completed_at"]
    assert "CASE WHEN (status = 'sent'::text) THEN sent_at_ms ELSE updated_at_ms END" in completed
    assert "INCLUDE (status, sent_at_ms, updated_at_ms, threshold_observed_at_ms)" in completed
    assert "translation_prompt_version" in completed
    assert "title_zh_v2" in completed
    assert "status = ANY (ARRAY['sent'::text, 'terminal'::text])" in completed


def test_0257_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module("tracefold.platform.postgres.alembic.versions.20260813_0257_news_bounded_reads")

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
