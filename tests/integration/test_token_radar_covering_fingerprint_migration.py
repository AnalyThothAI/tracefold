from __future__ import annotations

import importlib
from typing import Any

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.market.radar.reducer import token_radar_text_fingerprint
from tracefold.platform.postgres.postgres_migrations import alembic_config

_PREEXISTING_EVENT_COUNT = 10_000
_SPECIAL_TEXT = "  $RADAR\tStraße\nindependent\revidence\f1  "


def test_0261_backfills_the_generated_fingerprint_and_replaces_the_expression_index() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        _reset_schema(config, conn, revision="20260813_0259")
        _insert_preexisting_events(conn)
        conn.commit()

        command.upgrade(config, "20260813_0261")

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == {"version_num": "20260813_0261"}
        assert conn.execute("SELECT count(*) AS count FROM events").fetchone() == {"count": _PREEXISTING_EVENT_COUNT}
        generated = conn.execute(
            """
            SELECT token_radar_text_fingerprint
              FROM events
             WHERE event_id = 'radar-migration-event-00001'
            """
        ).fetchone()
        assert generated == {"token_radar_text_fingerprint": token_radar_text_fingerprint(_SPECIAL_TEXT)}

        column = conn.execute(
            """
            SELECT is_generated, generation_expression
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'events'
               AND column_name = 'token_radar_text_fingerprint'
            """
        ).fetchone()
        assert column is not None
        assert column["is_generated"] == "ALWAYS"
        expression = str(column["generation_expression"])
        assert "md5" in expression
        assert "regexp_replace" in expression
        assert "translate" in expression

        event_index = conn.execute(
            """
            SELECT pg_get_indexdef(indexrelid) AS definition
              FROM pg_index
             WHERE indexrelid = 'idx_events_token_radar_source_time'::regclass
            """
        ).fetchone()
        assert event_index is not None
        definition = str(event_index["definition"])
        assert "USING btree (timestamp_ms, event_id)" in definition
        assert (
            "INCLUDE (token_radar_text_fingerprint, received_at_ms, created_at_ms, action, author_handle)" in definition
        )
        assert "md5" not in definition
    finally:
        reset_postgres_schema(conn)
        conn.close()


def test_0261_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0261_token_radar_covering_fingerprint"
    )

    with pytest.raises(RuntimeError, match="irreversible Token Radar covering-read cut"):
        migration.downgrade()


def test_0264_keeps_the_source_time_index_and_restores_heap_visibility() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        _reset_schema(config, conn, revision="20260813_0263")
        _insert_preexisting_events(conn)
        conn.commit()

        before = conn.execute(
            """
            SELECT relpages, relallvisible
              FROM pg_class
             WHERE oid = 'events'::regclass
            """
        ).fetchone()
        assert before is not None
        assert int(before["relallvisible"]) == 0

        command.upgrade(config, "20260813_0264")

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == {"version_num": "20260813_0264"}
        assert conn.execute("SELECT count(*) AS count FROM events").fetchone() == {"count": _PREEXISTING_EVENT_COUNT}

        indexes = {
            str(row["indexname"]): str(row["indexdef"])
            for row in conn.execute(
                """
                SELECT indexname, indexdef
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND indexname LIKE 'idx_events_token_radar_%'
                """
            ).fetchall()
        }
        assert set(indexes) == {"idx_events_token_radar_source_time"}
        assert "USING btree (timestamp_ms, event_id)" in indexes["idx_events_token_radar_source_time"]
        assert (
            "INCLUDE (token_radar_text_fingerprint, received_at_ms, created_at_ms, "
            "action, author_handle)" in indexes["idx_events_token_radar_source_time"]
        )

        reloptions = {
            str(row["option"])
            for row in conn.execute(
                """
                SELECT unnest(reloptions) AS option
                  FROM pg_class
                 WHERE oid = 'events'::regclass
                """
            ).fetchall()
        }
        assert reloptions == {
            "autovacuum_analyze_scale_factor=0.01",
            "autovacuum_analyze_threshold=10000",
            "autovacuum_vacuum_insert_scale_factor=0.01",
            "autovacuum_vacuum_insert_threshold=10000",
            "autovacuum_vacuum_scale_factor=0.01",
            "autovacuum_vacuum_threshold=10000",
        }

        visibility = conn.execute(
            """
            SELECT relpages, relallvisible
              FROM pg_class
             WHERE oid = 'events'::regclass
            """
        ).fetchone()
        assert visibility is not None
        assert int(visibility["relpages"]) > 0
        assert int(visibility["relallvisible"]) == int(visibility["relpages"])
    finally:
        reset_postgres_schema(conn)
        conn.close()


def test_0264_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0264_token_radar_source_visibility"
    )

    with pytest.raises(RuntimeError, match="irreversible Token Radar visibility cut"):
        migration.downgrade()


def test_0264_recovers_already_committed_vacuum_policy_before_vacuum() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        _reset_schema(config, conn, revision="20260813_0263")
        _insert_preexisting_events(conn)
        conn.commit()
        _commit_0264_pre_vacuum_state(conn)

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == {"version_num": "20260813_0263"}
        assert conn.execute("SELECT to_regclass('idx_events_token_radar_source_time') AS index_name").fetchone() == {
            "index_name": "idx_events_token_radar_source_time"
        }

        command.upgrade(config, "20260813_0264")

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == {"version_num": "20260813_0264"}
        assert conn.execute("SELECT to_regclass('idx_events_token_radar_source_time') AS index_name").fetchone() == {
            "index_name": "idx_events_token_radar_source_time"
        }
        visibility = conn.execute(
            """
            SELECT relpages, relallvisible
              FROM pg_class
             WHERE oid = 'events'::regclass
            """
        ).fetchone()
        assert visibility is not None
        assert int(visibility["relpages"]) > 0
        assert int(visibility["relallvisible"]) == int(visibility["relpages"])
    finally:
        reset_postgres_schema(conn)
        conn.close()


def _reset_schema(config: Any, conn: Any, *, revision: str) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()
    command.upgrade(config, revision)


def _commit_0264_pre_vacuum_state(conn: Any) -> None:
    conn.execute(
        """
        ALTER TABLE events SET (
          autovacuum_vacuum_scale_factor = 0.01,
          autovacuum_vacuum_threshold = 10000,
          autovacuum_vacuum_insert_scale_factor = 0.01,
          autovacuum_vacuum_insert_threshold = 10000
        );
        """
    )
    conn.commit()


def _insert_preexisting_events(conn: Any) -> None:
    conn.execute(
        """
        INSERT INTO events(
          event_id, logical_dedup_key, source_provider, source_transport,
          coverage, channel, action, timestamp_ms, received_at_ms,
          author_handle, text, text_clean, search_text, raw_json, event_json,
          created_at_ms, updated_at_ms
        )
        SELECT
          'radar-migration-event-' || lpad(series_no::text, 5, '0'),
          'radar-migration-dedup-' || lpad(series_no::text, 5, '0'),
          'gmgn', 'direct_ws', 'public_stream', 'twitter_monitor_basic', 'tweet',
          1800000000000 - series_no,
          1800000000000 - series_no,
          'radar-migration-author-' || series_no,
          CASE WHEN series_no = 1
               THEN '  $RADAR' || chr(9) || 'Straße' || chr(10)
                    || 'independent' || chr(13) || 'evidence' || chr(12) || '1  '
               ELSE 'radar migration evidence ' || series_no END,
          CASE WHEN series_no = 1
               THEN '  $RADAR' || chr(9) || 'Straße' || chr(10)
                    || 'independent' || chr(13) || 'evidence' || chr(12) || '1  '
               ELSE 'radar migration evidence ' || series_no END,
          'radar migration evidence ' || series_no,
          jsonb_build_object(
            'series_no', series_no,
            'padding', repeat(md5(series_no::text), 48)
          ),
          jsonb_build_object(
            'series_no', series_no,
            'padding', repeat(md5((series_no + 1)::text), 48)
          ),
          1800000000000 - series_no,
          1800000000000 - series_no
        FROM generate_series(1, %s::integer) AS series_no
        """,
        (_PREEXISTING_EVENT_COUNT,),
    )
