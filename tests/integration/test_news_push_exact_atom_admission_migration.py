from __future__ import annotations

import importlib

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0273_preserves_settled_audit_retires_unsettled_v1_and_resets_current_epoch() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260815_0272")
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, source_kind, enabled,
              created_at_ms, updated_at_ms
            ) VALUES ('news-opennews', 'OpenNews', 4, 'en', 'opennews', true, 1, 1);

            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, first_ingest_mode, reporting_origin,
              title, description, lang, published_at_ms,
              first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, importance_score, importance_factors,
              active, created_at_ms, updated_at_ms
            ) VALUES
              (
                'news_item_11111111111111111111111111111111',
                'news-opennews', 'sent-v1', 'sent-v1',
                '{"strategies":[{"id":"1018"}]}'::jsonb,
                'live', 'OpenNews', 'Same exact alert', '', 'en', 1, 2, 2,
                repeat('a', 64), 0, '{}'::jsonb, true, 2, 2
              ),
              (
                'news_item_22222222222222222222222222222222',
                'news-opennews', 'pending-v1', 'pending-v1',
                '{"strategies":[{"id":"1018"}]}'::jsonb,
                'live', 'OpenNews', 'Same exact alert', '', 'en', 2, 3, 3,
                repeat('b', 64), 0, '{}'::jsonb, true, 3, 3
              );

            INSERT INTO news_item_title_presentations(
              item_id, source_title_fingerprint, original_title, state,
              created_at_ms, updated_at_ms
            )
            SELECT item_id,
                   encode(sha256(convert_to(title, 'UTF8')), 'hex'),
                   title, 'pending', created_at_ms, updated_at_ms
              FROM news_items;

            INSERT INTO news_push_deliveries(
              item_id, source_title_fingerprint, live_observed_at_ms,
              source_payload, status, attempted_at_ms, receipt,
              last_error, sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES
              (
                'news_item_11111111111111111111111111111111',
                encode(sha256(convert_to('Same exact alert', 'UTF8')), 'hex'),
                2,
                jsonb_build_object(
                  'schema_version', 'news_item_push_v1',
                  'item_id', 'news_item_11111111111111111111111111111111',
                  'provider_event_id', 'sent-v1',
                  'live_observed_at_ms', 2,
                  'original_title', 'Same exact alert',
                  'reporting_origin', 'OpenNews',
                  'provider_published_at_ms', 1,
                  'strategy_labels', jsonb_build_array('1018'),
                  'assets', '[]'::jsonb
                ),
                'sent', 3, '{"provider":"feishu"}'::jsonb,
                NULL, 4, 2, 4
              ),
              (
                'news_item_22222222222222222222222222222222',
                encode(sha256(convert_to('Same exact alert', 'UTF8')), 'hex'),
                3,
                jsonb_build_object(
                  'schema_version', 'news_item_push_v1',
                  'item_id', 'news_item_22222222222222222222222222222222',
                  'provider_event_id', 'pending-v1',
                  'live_observed_at_ms', 3,
                  'original_title', 'Same exact alert',
                  'reporting_origin', 'OpenNews',
                  'provider_published_at_ms', 2,
                  'strategy_labels', jsonb_build_array('1018'),
                  'assets', '[]'::jsonb
                ),
                'pending', NULL, NULL, NULL, NULL, 3, 3
              );

            UPDATE news_push_state
               SET delivery_available = true,
                   enablement_epoch_at_ms = 1,
                   total_count = 2,
                   pending_count = 1,
                   sending_count = 0,
                   sent_count = 1,
                   terminal_count = 0
             WHERE singleton_key = 'current';
            """
        )
        table_count_before = conn.execute(
            """
            SELECT count(*) AS value
              FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name LIKE 'news_%'
            """
        ).fetchone()["value"]
        conn.commit()

        command.upgrade(config, "20260815_0273")

        deliveries = conn.execute(
            """
            SELECT source_payload ->> 'provider_event_id' AS provider_event_id,
                   status, attempted_at_ms, receipt, last_error,
                   notification_fingerprint, admission_policy_version
              FROM news_push_deliveries
             ORDER BY provider_event_id
            """
        ).fetchall()
        state = conn.execute(
            """
            SELECT delivery_available, enablement_epoch_at_ms,
                   total_count, suppressed_count, pending_count,
                   sending_count, sent_count, terminal_count
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        table_count_after = conn.execute(
            """
            SELECT count(*) AS value
              FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name LIKE 'news_%'
            """
        ).fetchone()["value"]
        indexes = {
            row["indexname"]
            for row in conn.execute(
                """
                SELECT indexname FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND tablename = 'news_push_deliveries'
                """
            ).fetchall()
        }
    finally:
        conn.close()

    by_provider = {str(row["provider_event_id"]): row for row in deliveries}
    assert by_provider["sent-v1"]["status"] == "sent"
    assert by_provider["sent-v1"]["receipt"] == {"provider": "feishu"}
    assert by_provider["pending-v1"]["status"] == "terminal"
    assert by_provider["pending-v1"]["attempted_at_ms"] is not None
    assert by_provider["pending-v1"]["last_error"] == "news_push_exact_atom_policy_retired"
    assert all(row["notification_fingerprint"] is None for row in deliveries)
    assert all(row["admission_policy_version"] is None for row in deliveries)
    assert dict(state) == {
        "delivery_available": False,
        "enablement_epoch_at_ms": None,
        "total_count": 0,
        "suppressed_count": 0,
        "pending_count": 0,
        "sending_count": 0,
        "sent_count": 0,
        "terminal_count": 0,
    }
    assert table_count_after == table_count_before
    assert {
        "ix_news_push_deliveries_exact_atom_leader",
        "ix_news_push_deliveries_suppressed_recent",
    } <= indexes


def test_0273_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260815_0273_news_push_exact_atom_admission"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
