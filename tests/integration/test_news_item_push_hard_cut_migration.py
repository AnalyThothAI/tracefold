from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0270_preserves_completed_audit_and_retires_unsent_story_work() -> None:
    config = _reset_to_0269()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_push_deliveries(
              story_id, selected_item_id, live_observed_at_ms, source_payload,
              delivery_payload, payload_fingerprint, translation_status,
              status, delivery_attempts, next_attempt_at_ms,
              lease_owner, lease_token, lease_expires_at_ms,
              receipt, last_error, sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES
              (repeat('1', 64), 'news_item_11111111111111111111111111111111', 100,
               '{"schema_version":"news_story_push_v2"}'::jsonb,
               '{"card":"sent-audit"}'::jsonb, repeat('1', 64),
               'translated', 'sent', 1, NULL, NULL, NULL, NULL,
               '{"provider":"feishu"}'::jsonb, NULL, 120, 100, 120),
              (repeat('2', 64), 'news_item_22222222222222222222222222222222', 200,
               '{"schema_version":"news_story_push_v2"}'::jsonb,
               '{"card":"retired-audit"}'::jsonb, repeat('2', 64),
               'translated', 'retry_wait', 1, 210, NULL, NULL, NULL,
               NULL, 'feishu_500', NULL, 200, 200),
              (repeat('3', 64), 'news_item_33333333333333333333333333333333', 300,
               '{"schema_version":"news_story_push_v2"}'::jsonb,
               NULL, NULL, 'not_requested', 'suppressed', 0, NULL,
               NULL, NULL, NULL, NULL, NULL, NULL, 300, 300),
              (repeat('4', 64), 'news_item_44444444444444444444444444444444', 400,
               '{"schema_version":"news_story_push_v2"}'::jsonb,
               NULL, NULL, 'pending', 'pending_translation', 0, NULL,
               NULL, NULL, NULL, NULL, NULL, NULL, 400, 400),
              (repeat('5', 64), 'news_item_55555555555555555555555555555555', 500,
               '{"schema_version":"news_story_push_v2"}'::jsonb,
               '{"card":"leased-audit"}'::jsonb, repeat('5', 64),
               'translated', 'pending_delivery', 0, NULL,
               'legacy-worker', 'legacy-token', 550,
               NULL, NULL, NULL, 500, 500),
              (repeat('6', 64), 'news_item_66666666666666666666666666666666', 600,
               '{"schema_version":"news_story_push_v2"}'::jsonb,
               NULL, NULL, 'unavailable', 'terminal', 1, NULL,
               NULL, NULL, NULL, NULL, 'legacy_terminal', NULL, 600, 600)
            """
        )
        conn.commit()

        command.upgrade(config, "20260814_0270")

        rows = conn.execute(
            """
            SELECT item_id, status, legacy_delivery_payload, last_error
              FROM news_push_deliveries
             ORDER BY item_id
            """
        ).fetchall()
        columns = {
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'news_push_deliveries'
                """
            ).fetchall()
        }
        indexes = {
            str(row["indexname"])
            for row in conn.execute(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND tablename = 'news_push_deliveries'
                """
            ).fetchall()
        }
        state = conn.execute("SELECT * FROM news_push_state WHERE singleton_key = 'current'").fetchone()
        news_table_count = conn.execute(
            """
            SELECT count(*) AS value
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name LIKE 'news_%'
            """
        ).fetchone()["value"]
    finally:
        conn.close()

    by_item_id = {str(row["item_id"]): dict(row) for row in rows}
    assert by_item_id["news_item_11111111111111111111111111111111"] == {
        "item_id": "news_item_11111111111111111111111111111111",
        "status": "sent",
        "legacy_delivery_payload": {"card": "sent-audit"},
        "last_error": None,
    }
    for suffix in ("2", "3", "4", "5"):
        row = by_item_id[f"news_item_{suffix * 32}"]
        assert row["status"] == "terminal"
        assert row["last_error"] == "news_item_push_legacy_policy_retired"
    assert by_item_id["news_item_22222222222222222222222222222222"]["legacy_delivery_payload"] == {
        "card": "retired-audit"
    }
    assert by_item_id["news_item_55555555555555555555555555555555"]["legacy_delivery_payload"] == {
        "card": "leased-audit"
    }
    assert by_item_id["news_item_66666666666666666666666666666666"] == {
        "item_id": "news_item_66666666666666666666666666666666",
        "status": "terminal",
        "legacy_delivery_payload": None,
        "last_error": "legacy_terminal",
    }
    assert columns == {
        "item_id",
        "live_observed_at_ms",
        "source_payload",
        "legacy_delivery_payload",
        "presentation_snapshot",
        "status",
        "attempted_at_ms",
        "receipt",
        "last_error",
        "sent_at_ms",
        "created_at_ms",
        "updated_at_ms",
    }
    assert indexes == {
        "news_push_deliveries_pkey",
        "ix_news_push_deliveries_pending",
        "ix_news_push_deliveries_translation_attempted",
        "ix_news_push_deliveries_completed",
    }
    assert state["delivery_available"] is False
    assert state["enablement_epoch_at_ms"] is None
    assert {
        key: int(state[key])
        for key in (
            "total_count",
            "pending_count",
            "sending_count",
            "sent_count",
            "terminal_count",
        )
    } == {
        "total_count": 0,
        "pending_count": 0,
        "sending_count": 0,
        "sent_count": 0,
        "terminal_count": 0,
    }
    assert news_table_count == 10


def test_0270_rejects_duplicate_legacy_selected_item_identity() -> None:
    config = _reset_to_0269()
    conn = connect_postgres_test(read_only=False)
    try:
        duplicate_item_id = "news_item_33333333333333333333333333333333"
        conn.execute(
            """
            INSERT INTO news_push_deliveries(
              story_id, selected_item_id, live_observed_at_ms, source_payload,
              delivery_payload, payload_fingerprint, translation_status,
              status, delivery_attempts, next_attempt_at_ms,
              lease_owner, lease_token, lease_expires_at_ms,
              receipt, last_error, sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES
              (repeat('3', 64), %s, 100, '{}'::jsonb, NULL, NULL,
               'not_requested', 'suppressed', 0, NULL, NULL, NULL, NULL,
               NULL, NULL, NULL, 100, 100),
              (repeat('4', 64), %s, 101, '{}'::jsonb, NULL, NULL,
               'not_requested', 'suppressed', 0, NULL, NULL, NULL, NULL,
               NULL, NULL, NULL, 101, 101)
            """,
            (duplicate_item_id, duplicate_item_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        IntegrityError,
        match="news_item_push_legacy_selected_item_duplicate",
    ):
        command.upgrade(config, "20260814_0270")


def _reset_to_0269():
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    command.upgrade(config, "20260814_0269")
    return config
