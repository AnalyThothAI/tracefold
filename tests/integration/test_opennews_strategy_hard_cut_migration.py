from __future__ import annotations

import importlib

import pytest
from alembic import command
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb
from sqlalchemy.exc import OperationalError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import opennews_source, public_rss_sources
from tracefold.platform.postgres.postgres_audit import NEWS_TABLES
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0265_hard_cuts_legacy_opennews_state_without_erasing_push_audit() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        _reset_to_0264(conn, config)
        _seed_legacy_news_state(conn)
        immutable_delivery_hashes = _immutable_delivery_hashes(conn)
        cancelled_delivery_hashes = _cancelled_delivery_hashes(conn)

        command.upgrade(config, "20260813_0265")

        item_states = conn.execute("SELECT item_id, active FROM news_items ORDER BY item_id").fetchall()
        read_model_counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_stories) AS stories,
              (SELECT count(*) FROM news_story_members) AS members,
              (SELECT count(*) FROM news_brief_selection_current) AS selection
            """
        ).fetchone()
        brief = dict(conn.execute("SELECT * FROM news_brief_current").fetchone())
        deliveries = conn.execute(
            """
            SELECT selected_item_id, status, next_attempt_at_ms,
                   lease_owner, lease_token, lease_expires_at_ms
              FROM news_push_deliveries
             ORDER BY selected_item_id
            """
        ).fetchall()
        push_state = conn.execute(
            """
            SELECT baseline_at_ms, created_at_ms, total_count,
                   suppressed_count, pending_count, retry_count,
                   sent_count, terminal_count,
                   latest_sent_at_ms, latest_error, latest_error_at_ms,
                   reconcile_cursor_story_id, reconcile_cycle_started_at_ms
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        source_columns = {
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'news_sources'
                """
            ).fetchall()
        }
        opennews_status = conn.execute(
            """
            SELECT live_connected, last_connected_at_ms,
                   last_disconnected_at_ms, last_overflow_at_ms,
                   strategy_coverage_started_at_ms,
                   coverage_unknown_since_at_ms,
                   last_accepted_strategy_trigger_at_ms,
                   observed_strategy_provenance,
                   last_fetch_started_at_ms, last_fetch_finished_at_ms,
                   last_http_status, last_success_at_ms,
                   consecutive_failures, last_error,
                   last_rejection_counts, last_items_seen,
                   last_items_accepted
              FROM news_sources
             WHERE source_id = 'news-opennews'
            """
        ).fetchone()
        opennews_sources = conn.execute(
            """
            SELECT source_id, enabled
              FROM news_sources
             WHERE source_kind = 'opennews'
             ORDER BY source_id
            """
        ).fetchall()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()

    assert version == {"version_num": "20260813_0265"}
    assert item_states == [
        {"item_id": "legacy-opennews-item", "active": False},
        {"item_id": "legacy-secondary-opennews-item", "active": False},
        {"item_id": "retained-rss-item", "active": True},
    ]
    assert read_model_counts == {"stories": 0, "members": 0, "selection": 0}
    assert brief == {
        "singleton_key": True,
        "slot_at_ms": None,
        "slot_status": "due",
        "next_due_at_ms": 0,
        "completed_at_ms": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at_ms": None,
        "attempt_count": 0,
        "failure_count": 0,
        "model_outcome": None,
        "pointer_action": "none",
        "last_error_code": None,
        "last_attempt_at_ms": None,
        "active_selection": None,
        "served_payload": None,
        "created_at_ms": 0,
        "updated_at_ms": 0,
    }
    assert deliveries == [
        {
            "selected_item_id": "pending-delivery-item",
            "status": "suppressed",
            "next_attempt_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        },
        {
            "selected_item_id": "pending-translation-item",
            "status": "suppressed",
            "next_attempt_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        },
        {
            "selected_item_id": "preexisting-suppressed-item",
            "status": "suppressed",
            "next_attempt_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        },
        {
            "selected_item_id": "retry-item",
            "status": "suppressed",
            "next_attempt_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        },
        {
            "selected_item_id": "sent-item",
            "status": "sent",
            "next_attempt_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        },
        {
            "selected_item_id": "terminal-item",
            "status": "terminal",
            "next_attempt_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
        },
    ]
    assert _immutable_delivery_hashes_from_rows(immutable_delivery_hashes) == immutable_delivery_hashes
    assert _cancelled_delivery_hashes_from_rows(cancelled_delivery_hashes) == cancelled_delivery_hashes
    assert push_state == {
        "baseline_at_ms": 4_242,
        "created_at_ms": 100,
        "total_count": 6,
        "suppressed_count": 4,
        "pending_count": 0,
        "retry_count": 0,
        "sent_count": 1,
        "terminal_count": 1,
        "latest_sent_at_ms": 190,
        "latest_error": "legacy_terminal",
        "latest_error_at_ms": 195,
        "reconcile_cursor_story_id": None,
        "reconcile_cycle_started_at_ms": None,
    }
    assert {"last_recovery_at_ms", "last_live_at_ms"}.isdisjoint(source_columns)
    assert {
        "last_connected_at_ms",
        "last_disconnected_at_ms",
        "last_overflow_at_ms",
        "strategy_coverage_started_at_ms",
        "coverage_unknown_since_at_ms",
        "last_accepted_strategy_trigger_at_ms",
        "observed_strategy_provenance",
    } <= source_columns
    assert opennews_status == {
        "live_connected": False,
        "last_connected_at_ms": None,
        "last_disconnected_at_ms": None,
        "last_overflow_at_ms": None,
        "strategy_coverage_started_at_ms": None,
        "coverage_unknown_since_at_ms": None,
        "last_accepted_strategy_trigger_at_ms": None,
        "observed_strategy_provenance": [],
        "last_fetch_started_at_ms": None,
        "last_fetch_finished_at_ms": None,
        "last_http_status": None,
        "last_success_at_ms": None,
        "consecutive_failures": 0,
        "last_error": None,
        "last_rejection_counts": {},
        "last_items_seen": 0,
        "last_items_accepted": 0,
    }
    assert opennews_sources == [
        {"source_id": "legacy-opennews-secondary", "enabled": False},
        {"source_id": "news-opennews", "enabled": True},
    ]


def test_0265_rejects_an_active_opennews_item_without_strategy_provenance() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        _reset_to_0264(conn, config)
        command.upgrade(config, "20260813_0265")
        with conn.transaction():
            NewsRepository(conn).sync_sources((opennews_source(),), now_ms=100)

        invalid_provenance = (
            {},
            {"strategies": {}},
            {"strategies": "not-an-array"},
            {"strategies": [42]},
            {"strategies": [{}]},
            {"strategies": [{"id": "   "}]},
            {"strategies": [{"id": "x" * 129}]},
        )
        for position, metadata in enumerate(invalid_provenance):
            with pytest.raises(CheckViolation), conn.transaction():
                conn.execute(
                    """
                    INSERT INTO news_items(
                      item_id, source_id, source_item_key, provider_record_id,
                      provider_metadata, canonical_url, reporting_origin,
                      title, description, lang, published_at_ms,
                      first_observed_at_ms, last_observed_at_ms,
                      content_fingerprint, importance_factors,
                      active, created_at_ms, updated_at_ms
                    ) VALUES (
                      %(item_id)s, 'news-opennews', %(item_id)s,
                      %(item_id)s, %(provider_metadata)s, NULL, 'opennews',
                      'Active item without Strategy provenance', '', 'en',
                      100, 100, 100, 'missing-strategy-fingerprint',
                      '{}'::jsonb, true, 100, 100
                    )
                    """,
                    {
                        "item_id": f"invalid-strategy-{position}",
                        "provider_metadata": Jsonb(metadata),
                    },
                )
    finally:
        conn.close()


def test_0265_refuses_to_cut_while_the_steady_workers_gate_is_held() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    lock_conn = connect_postgres_test(read_only=False)
    try:
        _reset_to_0264(conn, config)
        _seed_legacy_news_state(conn)
        before = _news_state_snapshot(conn)
        lock_conn.execute("SELECT pg_advisory_lock_shared(%s, %s)", (0x54524644, 0))
        lock_conn.commit()

        with pytest.raises(OperationalError, match="strategy_hard_cut_workers_active"):
            command.upgrade(config, "20260813_0265")

        after = _news_state_snapshot(conn)
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        try:
            lock_conn.execute("SELECT pg_advisory_unlock_shared(%s, %s)", (0x54524644, 0))
            lock_conn.commit()
        finally:
            lock_conn.close()
            conn.close()

    assert after == before
    assert version == {"version_num": "20260813_0264"}


def _reset_to_0264(conn, config) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()
    command.upgrade(config, "20260813_0264")


def _seed_legacy_news_state(conn) -> None:
    rss_source = public_rss_sources()[0]
    with conn.transaction():
        for source in (opennews_source(), rss_source):
            is_rss = source.source_kind == "rss"
            conn.execute(
                """
                INSERT INTO news_sources (
                  source_id, name, tier, lang, enabled, source_kind,
                  feed_url, refresh_interval_seconds, next_fetch_at_ms,
                  created_at_ms, updated_at_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 100, 100)
                """,
                (
                    source.source_id,
                    source.name,
                    source.tier,
                    source.lang,
                    source.enabled,
                    source.source_kind,
                    source.feed_url if is_rss else None,
                    source.refresh_interval_seconds if is_rss else None,
                    100 if is_rss else None,
                ),
            )
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled, source_kind,
              feed_url, refresh_interval_seconds, next_fetch_at_ms,
              created_at_ms, updated_at_ms
            ) VALUES (
              'legacy-opennews-secondary', 'Legacy secondary OpenNews',
              1, 'en', true, 'opennews', NULL, NULL, NULL, 100, 100
            )
            """
        )
        conn.execute(
            """
            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, provider_score_updated_at_ms,
              push_eligibility_updated_at_ms,
              canonical_url, reporting_origin, title, description, lang,
              published_at_ms, first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, level, category, classification_source,
              classification_confidence, importance_score,
              importance_factors, active, created_at_ms, updated_at_ms
            ) VALUES (
              'legacy-opennews-item', 'news-opennews', 'legacy-wire', 'legacy-wire',
              '{"source":"Reuters","score":88,"coins":[{"symbol":"BTC","market_type":"cex"}]}'::jsonb,
              110, 110, 'https://example.test/legacy', 'reuters',
              'Legacy broad OpenNews report', 'legacy corpus', 'en',
              100, 100, 110, 'legacy-opennews-fingerprint',
              'info', 'general', 'keyword', 1, 1, '{}'::jsonb,
              true, 100, 110
            ), (
              'legacy-secondary-opennews-item', 'legacy-opennews-secondary',
              'legacy-secondary-wire', 'legacy-secondary-wire',
              '{"score":81,"source":"Legacy"}'::jsonb,
              110, 110, NULL, 'legacy-secondary',
              'Legacy secondary OpenNews report', '', 'en',
              100, 100, 110, 'legacy-secondary-fingerprint',
              'info', 'general', 'keyword', 1, 1, '{}'::jsonb,
              true, 100, 110
            ), (
              'retained-rss-item', %s, 'rss-guid', NULL, '{}'::jsonb,
              NULL, NULL, 'https://example.test/rss', 'rss outlet',
              'Retained RSS report', 'retained corroboration', 'en',
              100, 100, 100, 'retained-rss-fingerprint',
              'info', 'general', 'keyword', 1, 1, '{}'::jsonb,
              true, 100, 100
            );
            """,
            (rss_source.source_id,),
        )
        conn.execute(
            """

            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_url,
              representative_description, scoring_item_id,
              level, category, importance_score, importance_factors,
              item_count, source_count, first_published_at_ms,
              last_published_at_ms, state_fingerprint,
              created_at_ms, updated_at_ms, facet_facts
            ) VALUES (
              repeat('a', 64), 'legacy-key', 'Legacy broad OpenNews report',
              'legacy-opennews-item', 'news-opennews',
              'Legacy broad OpenNews report', 'https://example.test/legacy',
              'legacy corpus', 'legacy-opennews-item',
              'info', 'general', 1, '{}'::jsonb,
              1, 1, 100, 100, 'legacy-story-fingerprint',
              100, 100,
              '{"source_ids":["news-opennews"],"reporting_origins":["reuters"]}'::jsonb
            );

            INSERT INTO news_story_members(story_id, item_id)
            VALUES (repeat('a', 64), 'legacy-opennews-item');

            INSERT INTO news_brief_selection_current(
              singleton_key, selection_fingerprint, projection_revision,
              selector_evaluated_at_ms, top_stories, selection_stats,
              selector_version, identity_version, updated_at_ms
            ) VALUES (
              true, repeat('b', 64), 'legacy-projection', 100,
              '[{"story_id":"legacy"}]'::jsonb, '{}'::jsonb,
              'legacy-selector', 'legacy-identity', 100
            );

            UPDATE news_projection_summary
               SET active_item_count = 2,
                   active_story_count = 1,
                   newest_item_at_ms = 100,
                   newest_story_at_ms = 100,
                   last_material_change_at_ms = 100,
                   input_fingerprint = repeat('c', 64),
                   projection_version = 'legacy-projection',
                   last_attempt_at_ms = 100,
                   last_success_at_ms = 100,
                   updated_at_ms = 100
             WHERE singleton_key = 'current';

            UPDATE news_brief_current
               SET slot_at_ms = 0,
                   slot_status = 'completed',
                   next_due_at_ms = 1800000,
                   completed_at_ms = 100,
                   attempt_count = 1,
                   model_outcome = 'ok',
                   pointer_action = 'advance_ok',
                   last_attempt_at_ms = 50,
                   active_selection = '{}'::jsonb,
                   served_payload = '{"quality":"ok","slot_at_ms":0}'::jsonb,
                   updated_at_ms = 100
             WHERE singleton_key = true;

            UPDATE news_sources
               SET live_connected = true,
                   last_live_at_ms = 111,
                   last_fetch_started_at_ms = 105,
                   last_fetch_finished_at_ms = 110,
                   last_recovery_at_ms = 110,
                   last_success_at_ms = 110,
                   last_http_status = 200,
                   last_outcome = 'recovery_success',
                   last_rejection_counts = '{"duplicate":2}'::jsonb,
                   last_items_seen = 9,
                   last_items_accepted = 7,
                   updated_at_ms = 111
             WHERE source_id = 'news-opennews';
            """
        )
        conn.execute(
            """
            UPDATE news_push_state
               SET baseline_at_ms = 4242,
                   created_at_ms = 100,
                   updated_at_ms = 200,
                   total_count = 6,
                   suppressed_count = 1,
                   pending_count = 2,
                   retry_count = 1,
                   sent_count = 1,
                   terminal_count = 1,
                   latest_sent_at_ms = 190,
                   latest_error = 'legacy_terminal',
                   latest_error_at_ms = 195,
                   reconcile_cursor_story_id = repeat('f', 64),
                   reconcile_cycle_started_at_ms = 180
             WHERE singleton_key = 'current';

            INSERT INTO news_push_deliveries(
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload,
              delivery_payload, payload_fingerprint, translation_status,
              status, delivery_attempts, next_attempt_at_ms,
              lease_owner, lease_token, lease_expires_at_ms,
              receipt, last_error, sent_at_ms,
              created_at_ms, updated_at_ms
            ) VALUES
              (repeat('1', 64), 'pending-translation-item', 88, 101,
               '{"kind":"pending_translation"}'::jsonb, NULL, NULL,
               'pending', 'pending_translation', 0, 201,
               'legacy-worker', 'pending-token', 301,
               NULL, NULL, NULL, 101, 101),
              (repeat('2', 64), 'pending-delivery-item', 89, 102,
               '{"kind":"pending_delivery"}'::jsonb, '{"card":"ready"}'::jsonb,
               repeat('2', 64), 'translated', 'pending_delivery', 1, 202,
               'legacy-worker', 'delivery-token', 302,
               NULL, NULL, NULL, 102, 102),
              (repeat('3', 64), 'retry-item', 90, 103,
               '{"kind":"retry"}'::jsonb, '{"card":"retry"}'::jsonb,
               repeat('3', 64), 'translated', 'retry_wait', 2, 203,
               NULL, NULL, NULL, NULL, 'legacy_retry', NULL, 103, 103),
              (repeat('4', 64), 'sent-item', 91, 104,
               '{"kind":"sent","frozen":true}'::jsonb, '{"card":"sent"}'::jsonb,
               repeat('4', 64), 'translated', 'sent', 1, NULL,
               NULL, NULL, NULL, '{"provider":"feishu","receipt_id":"r-1"}'::jsonb,
               NULL, 190, 104, 190),
              (repeat('5', 64), 'terminal-item', 92, 105,
               '{"kind":"terminal","frozen":true}'::jsonb, '{"card":"terminal"}'::jsonb,
               repeat('5', 64), 'translated', 'terminal', 6, NULL,
               NULL, NULL, NULL, NULL, 'legacy_terminal', NULL, 105, 195),
              (repeat('6', 64), 'preexisting-suppressed-item', 93, 106,
               '{"kind":"suppressed"}'::jsonb, NULL, NULL,
               'not_requested', 'suppressed', 0, NULL,
               NULL, NULL, NULL, NULL, NULL, NULL, 106, 106);
            """
        )


def _immutable_delivery_hashes(conn) -> dict[str, str]:
    return {
        str(row["selected_item_id"]): str(row["row_hash"])
        for row in conn.execute(
            """
            SELECT selected_item_id, md5(to_jsonb(delivery)::text) AS row_hash
              FROM news_push_deliveries delivery
             WHERE status IN ('sent', 'terminal')
             ORDER BY selected_item_id
            """
        ).fetchall()
    }


def _immutable_delivery_hashes_from_rows(expected: dict[str, str]) -> dict[str, str]:
    conn = connect_postgres_test(read_only=True)
    try:
        actual = _immutable_delivery_hashes(conn)
    finally:
        conn.close()
    assert set(actual) == set(expected)
    return actual


def _cancelled_delivery_hashes(conn) -> dict[str, str]:
    return {
        str(row["selected_item_id"]): str(row["row_hash"])
        for row in conn.execute(
            """
            SELECT selected_item_id,
                   md5((
                     to_jsonb(delivery) - ARRAY[
                       'status', 'next_attempt_at_ms', 'lease_owner',
                       'lease_token', 'lease_expires_at_ms', 'updated_at_ms'
                     ]
                   )::text) AS row_hash
              FROM news_push_deliveries delivery
             WHERE selected_item_id IN (
               'pending-translation-item', 'pending-delivery-item', 'retry-item'
             )
             ORDER BY selected_item_id
            """
        ).fetchall()
    }


def _cancelled_delivery_hashes_from_rows(expected: dict[str, str]) -> dict[str, str]:
    conn = connect_postgres_test(read_only=True)
    try:
        actual = _cancelled_delivery_hashes(conn)
    finally:
        conn.close()
    assert set(actual) == set(expected)
    return actual


def _news_state_snapshot(conn) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for table_name in NEWS_TABLES:
        row = conn.execute(
            f"""
            SELECT count(*) AS row_count,
                   md5(coalesce(string_agg(
                     to_jsonb(material_row)::text,
                     '' ORDER BY to_jsonb(material_row)::text
                   ), '')) AS content_hash
              FROM {table_name} material_row
            """
        ).fetchone()
        snapshot[table_name] = dict(row)
    return snapshot


def test_0265_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0265_opennews_strategy_hard_cut"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
