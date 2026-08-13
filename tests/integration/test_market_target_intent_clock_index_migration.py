from __future__ import annotations

import importlib

import pytest
from alembic import command
from sqlalchemy.exc import ProgrammingError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0263_replaces_the_redundant_event_index_with_the_intent_clock_cover() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260813_0262")
        conn.execute(
            """
            INSERT INTO events(
              event_id, logical_dedup_key, source_provider, source_transport,
              coverage, channel, action, timestamp_ms, received_at_ms,
              raw_json, event_json, created_at_ms, updated_at_ms
            ) VALUES (
              'clock-event', 'clock-event', 'fixture', 'fixture',
              'public_stream', 'fixture', 'post', 7, 7,
              '{}'::jsonb, '{}'::jsonb, 7, 7
            );
            INSERT INTO token_intents(
              intent_id, event_id, intent_key, construction_policy,
              display_symbol, intent_status, intent_confidence,
              created_at_ms, updated_at_ms
            ) VALUES (
              'clock-intent', 'clock-event', 'clock-intent', 'fixture',
              'BTC', 'resolved', 1.0, 7, 7
            );
            """
        )
        conn.commit()
        before = conn.execute(
            """
            SELECT count(*) AS count,
                   min(event_id) AS event_id,
                   min(created_at_ms) AS created_at_ms
              FROM token_intents
            """
        ).fetchone()

        command.upgrade(config, "20260813_0263")

        after = conn.execute(
            """
            SELECT count(*) AS count,
                   min(event_id) AS event_id,
                   min(created_at_ms) AS created_at_ms
              FROM token_intents
            """
        ).fetchone()
        rows = conn.execute(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = ANY(%s)
             ORDER BY indexname
            """,
            (
                [
                    "idx_token_intents_event",
                    "idx_token_intents_event_intent",
                    "idx_token_intents_market_targets_created",
                ],
            ),
        ).fetchall()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()

    assert after == before == {"count": 1, "event_id": "clock-event", "created_at_ms": 7}
    indexes = {str(row["indexname"]): " ".join(str(row["indexdef"]).split()) for row in rows}
    assert "idx_token_intents_event" not in indexes
    assert indexes["idx_token_intents_event_intent"] == (
        "CREATE INDEX idx_token_intents_event_intent ON public.token_intents USING btree (event_id, intent_id)"
    )
    assert indexes["idx_token_intents_market_targets_created"] == (
        "CREATE INDEX idx_token_intents_market_targets_created "
        "ON public.token_intents USING btree (created_at_ms, intent_id) INCLUDE (event_id)"
    )
    assert version == {"version_num": "20260813_0263"}


def test_0263_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0263_market_target_intent_clock_index"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()


def test_0263_refuses_to_change_market_target_clock_semantics() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260813_0262")
        conn.execute(
            """
            INSERT INTO events(
              event_id, logical_dedup_key, source_provider, source_transport,
              coverage, channel, action, timestamp_ms, received_at_ms,
              raw_json, event_json, created_at_ms, updated_at_ms
            ) VALUES (
              'mismatch-event', 'mismatch-event', 'fixture', 'fixture',
              'public_stream', 'fixture', 'post', 7, 7,
              '{}'::jsonb, '{}'::jsonb, 7, 7
            );
            INSERT INTO token_intents(
              intent_id, event_id, intent_key, construction_policy,
              display_symbol, intent_status, intent_confidence,
              created_at_ms, updated_at_ms
            ) VALUES (
              'mismatch-intent', 'mismatch-event', 'mismatch-intent', 'fixture',
              'BTC', 'resolved', 1.0, 8, 8
            );
            """
        )
        conn.commit()

        with pytest.raises(ProgrammingError, match="token_intent_acquisition_clock_mismatch"):
            command.upgrade(config, "20260813_0263")

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        old_index = conn.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = 'idx_token_intents_event'
            """
        ).fetchone()
        new_index = conn.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = 'idx_token_intents_market_targets_created'
            """
        ).fetchone()
    finally:
        conn.close()

    assert version == {"version_num": "20260813_0262"}
    assert old_index == {"indexname": "idx_token_intents_event"}
    assert new_index is None
