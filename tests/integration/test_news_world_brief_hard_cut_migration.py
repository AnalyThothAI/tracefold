from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config
from tracefold.platform.postgres.queue_terminal import terminalize_source_row

NEWS_TABLES = {
    "news_sources",
    "news_items",
    "news_stories",
    "news_story_members",
    "news_projection_summary",
    "news_story_facet_counts",
    "news_source_facet_counts",
    "news_brief_selection_current",
    "news_brief_runs",
    "news_brief_publications",
    "news_brief_current",
    "news_push_state",
    "news_push_deliveries",
}
BRIEF_TABLE_COLUMNS = {
    "news_brief_selection_current": {
        "singleton_key",
        "selection_fingerprint",
        "projection_revision",
        "selector_evaluated_at_ms",
        "top_stories",
        "selection_stats",
        "selector_version",
        "identity_version",
        "updated_at_ms",
    },
    "news_brief_runs": {
        "run_id",
        "target_fingerprint",
        "selection_fingerprint",
        "status",
        "model_outcome",
        "pointer_action",
        "failure_count",
        "next_due_at_ms",
        "lease_owner",
        "lease_token",
        "lease_expires_at_ms",
        "last_error_code",
        "last_attempt_at_ms",
        "created_at_ms",
        "updated_at_ms",
        "completed_at_ms",
    },
    "news_brief_publications": {
        "publication_id",
        "selection_fingerprint",
        "target_fingerprint",
        "quality",
        "brief_kind",
        "world_brief",
        "brief_story_lines",
        "top_stories",
        "selected_story_ids",
        "sources",
        "source_age_range",
        "provider",
        "model",
        "prompt_version",
        "workflow_version",
        "composer_version",
        "schema_version",
        "selector_version",
        "identity_version",
        "locale",
        "validation",
        "provenance",
        "published_at_ms",
        "created_at_ms",
    },
    "news_brief_current": {
        "singleton_key",
        "publication_id",
        "target_fingerprint",
        "latest_run_id",
        "pending_first_dirty_at_ms",
        "pending_due_at_ms",
        "updated_at_ms",
    },
}


def _reset_to_0245() -> tuple[Any, Any]:
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
    command.upgrade(config, "20260807_0245")
    return config, connect_postgres_test(read_only=False)


def _push_ledger_snapshot(conn: Any) -> dict[str, Any]:
    state_rows = [dict(row) for row in conn.execute("SELECT * FROM news_push_state ORDER BY singleton_key").fetchall()]
    delivery_rows = [
        dict(row) for row in conn.execute("SELECT * FROM news_push_deliveries ORDER BY story_id").fetchall()
    ]

    def snapshot(rows: list[dict[str, object]]) -> dict[str, Any]:
        encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return {
            "row_count": len(rows),
            "content_hash": hashlib.sha256(encoded.encode()).hexdigest(),
        }

    return {
        "state": snapshot(state_rows),
        "deliveries": {
            **snapshot(delivery_rows),
            "statuses": [
                {
                    "story_id": row["story_id"],
                    "status": row["status"],
                    "translation_status": row["translation_status"],
                }
                for row in delivery_rows
            ],
        },
    }


def _retained_item_snapshot(conn: Any, item_id: str) -> dict[str, Any]:
    return dict(
        conn.execute(
            """
            SELECT item_id, source_id, source_item_key, provider_record_id,
                   provider_metadata, provider_score_updated_at_ms,
                   canonical_url, reporting_origin, title, description, lang,
                   published_at_ms, first_observed_at_ms, last_observed_at_ms,
                   content_fingerprint, level, category, classification_source,
                   classification_confidence, importance_score, importance_factors,
                   active, created_at_ms, updated_at_ms
              FROM news_items
             WHERE item_id = %s
            """,
            (item_id,),
        ).fetchone()
    )


def _insert_opennews_migration_rows(conn: Any, *, count: int, title_prefix: str) -> None:
    conn.execute(
        """
        INSERT INTO news_sources(
          source_id, name, tier, lang, enabled, consecutive_failures,
          created_at_ms, updated_at_ms, source_kind, live_connected,
          gap_unclosed, gap_version
        ) VALUES (
          'news-opennews', 'OpenNews', 2, 'en', true, 0,
          0, 0, 'opennews', false, false, 0
        )
        """
    )
    for index in range(1, count + 1):
        conn.execute(
            """
            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, canonical_url, reporting_origin,
              title, normalized_title, description, lang, published_at_ms,
              first_observed_at_ms, last_observed_at_ms, content_fingerprint,
              level, category, classification_source, classification_confidence,
              importance_score, importance_factors, brief_excluded,
              active, created_at_ms, updated_at_ms
            ) VALUES (
              %(item_id)s, 'news-opennews', %(record_id)s, %(record_id)s,
              %(metadata)s, NULL, 'twitter', %(title)s, 'legacy', '', 'en',
              %(clock)s, %(clock)s, %(clock)s, 'legacy-fingerprint',
              'info', 'general', 'keyword', 1, 0, '{}'::jsonb, false,
              true, %(clock)s, %(clock)s
            )
            """,
            {
                "item_id": f"migration-item-{index}",
                "record_id": f"migration-record-{index}",
                "metadata": Jsonb({"source": f"Author{index}"}),
                "title": (
                    f"<b>{title_prefix} {index}</b><br>"
                    "Detailed retained context is longer than forty UTF-16 code units."
                ),
                "clock": 100 + index,
            },
        )


def _migration_module():
    return importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260807_0246_news_world_brief_hard_cut"
    )


def _sqlalchemy_test_url() -> str:
    return _test_postgres_dsn().replace("postgresql://", "postgresql+psycopg://", 1)


def test_world_brief_hard_cut_has_one_thirteen_table_schema() -> None:
    config, conn = _reset_to_0245()
    try:
        conn.close()
        command.upgrade(config, "20260807_0246")
        conn = connect_postgres_test(read_only=False)
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        tables = {
            row["tablename"]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'news_%'"
            ).fetchall()
        }
        item_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='news_items'
                """
            ).fetchall()
        }
        brief_columns: dict[str, set[str]] = {}
        for table_name in BRIEF_TABLE_COLUMNS:
            brief_columns[table_name] = {
                row["column_name"]
                for row in conn.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema='public' AND table_name=%s
                    """,
                    (table_name,),
                ).fetchall()
            }
    finally:
        conn.close()

    assert version == "20260807_0246"
    assert tables == NEWS_TABLES
    assert {"normalized_title", "brief_excluded"}.isdisjoint(item_columns)
    assert brief_columns == BRIEF_TABLE_COLUMNS


def test_world_brief_hard_cut_normalizes_facts_and_preserves_push_ledger() -> None:
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
    command.upgrade(config, "20260807_0245")

    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled, consecutive_failures,
              created_at_ms, updated_at_ms, source_kind, live_connected,
              gap_unclosed, gap_version
            ) VALUES (
              'news-opennews', 'OpenNews', 2, 'en', true, 0,
              0, 0, 'opennews', false, false, 0
            ), (
              'legacy-rss', 'Legacy RSS', 3, 'en', false, 0,
              0, 0, 'rss', false, false, 0
            );

            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, provider_score_updated_at_ms,
              canonical_url, reporting_origin, title, normalized_title,
              description, lang, published_at_ms, first_observed_at_ms,
              last_observed_at_ms, content_fingerprint, level, category,
              classification_source, classification_confidence,
              importance_score, importance_factors, brief_excluded,
              active, created_at_ms, updated_at_ms
            ) VALUES (
              'item-retained', 'news-opennews', 'wire-1', 'wire-1',
              '{"source":"@WireAuthor","score":88}'::jsonb, 124,
              'https://example.com/story', 'twitter',
              '<b>Bitcoin &amp; markets rally</b><BR/>Detailed market context spans more than forty characters for readers.<br/>https://example.com/body',
              'polluted old normalized title', '', 'en', 123, 124, 125,
              'old-content-fingerprint', 'info', 'general', 'keyword', 1,
              0, '{}'::jsonb, true, true, 124, 125
            ), (
              'item-legacy-rss', 'legacy-rss', 'rss-1', NULL,
              '{"legacy":"sealed"}'::jsonb, NULL,
              'https://legacy.example/story', 'Legacy RSS',
              '<b>Legacy RSS headline</b><br>Irreplaceable body',
              'legacy-rss-normalized', 'Irreplaceable historical RSS description',
              'en', 120, 121, 122, 'sealed-rss-content-fingerprint',
              'medium', 'general', 'keyword', 0.75,
              17, '{"sealed":true}'::jsonb, false, false, 121, 122
            );

            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_url,
              representative_description, scoring_item_id, level, category,
              importance_score, importance_factors, item_count, source_count,
              first_published_at_ms, last_published_at_ms,
              state_fingerprint, created_at_ms, updated_at_ms
            ) VALUES (
              repeat('a', 64), 'old-key', 'old title', 'item-retained', 'news-opennews',
              'old title', 'https://example.com/story', '', 'item-retained',
              'info', 'general', 0, '{}'::jsonb, 1, 1, 123, 123,
              'old-story-fingerprint', 124, 125
            );
            INSERT INTO news_story_members(story_id, item_id)
            VALUES (repeat('a', 64), 'item-retained');
            INSERT INTO news_brief_selection_current(rank, story_id, updated_at_ms)
            VALUES (1, repeat('a', 64), 125);

            INSERT INTO news_brief_runs(
              run_id, fingerprint, status, attempt_count,
              candidate_story_count, candidate_source_count,
              lease_owner, lease_expires_at_ms, heartbeat_at_ms, last_error,
              created_at_ms, updated_at_ms, completed_at_ms, next_due_at_ms
            ) VALUES (
              'old-run', 'old-target', 'failed', 3, 1, 1,
              NULL, NULL, NULL, 'old_failure', 124, 126, 126, NULL
            );
            INSERT INTO news_brief_publications(
              publication_id, fingerprint, evidence_cutoff_at_ms,
              published_at_ms, provider, model, prompt_version,
              workflow_version, schema_version, locale, selected_story_ids,
              lead, lines, sources, validation, raw_response, created_at_ms
            ) VALUES (
              'old-publication', 'old-target', 123, 126,
              'old-provider', 'old-model', 'old-prompt', 'old-workflow',
              'old-schema', 'zh-CN', jsonb_build_array(repeat('a', 64)),
              'old lead', '["old line"]'::jsonb,
              jsonb_build_array(jsonb_build_object('story_id', repeat('a', 64), 'url', NULL)),
              '{}'::jsonb, 'old raw provider output', 126
            );
            UPDATE news_brief_current
               SET publication_id='old-publication',
                   target_fingerprint='old-target',
                   latest_run_id='old-run',
                   updated_at_ms=126
             WHERE singleton_key;

            INSERT INTO news_story_title_translations(
              story_id, source_title, source_title_fingerprint,
              source_raw_title_fingerprint, locale, workflow_version,
              prompt_version, status, attempt_count, attempts,
              next_attempt_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
              repeat('a', 64), 'Old display title',
              encode(sha256(convert_to('Old display title', 'UTF8')), 'hex'),
              repeat('c', 64), 'zh-CN', 'old-workflow', 'old-prompt',
              'pending', 0, '[]'::jsonb, 127, 127, 127
            );

            UPDATE news_push_state
               SET baseline_at_ms=42, updated_at_ms=42
             WHERE singleton_key='current';
            INSERT INTO news_push_deliveries(
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload,
              delivery_payload, payload_fingerprint, translation_status,
              status, delivery_attempts, next_attempt_at_ms,
              lease_owner, lease_token, lease_expires_at_ms,
              receipt, last_error, sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
              repeat('b', 64), 'item-retained', 88, 124,
              '{"title":"original push title"}'::jsonb,
              NULL, NULL, 'not_requested', 'retry_wait', 1, 200,
              NULL, NULL, NULL, NULL, 'provider_unavailable', NULL, 124, 125
            );
            INSERT INTO news_push_deliveries(
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload,
              delivery_payload, payload_fingerprint, translation_status,
              status, delivery_attempts, next_attempt_at_ms,
              lease_owner, lease_token, lease_expires_at_ms,
              receipt, last_error, sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
              repeat('c', 64), 'item-retained', 91, 125,
              '{"title":"sealed historical source"}'::jsonb,
              '{"text":"sealed push"}'::jsonb, repeat('d', 64),
              'translated', 'sent', 1, NULL,
              NULL, NULL, NULL, '{"message_id":"abc"}'::jsonb,
              NULL, 130, 125, 130
            )
            """
        )
        conn.execute(
            """
            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, provider_score_updated_at_ms,
              canonical_url, reporting_origin, title, normalized_title,
              description, lang, published_at_ms, first_observed_at_ms,
              last_observed_at_ms, content_fingerprint, level, category,
              classification_source, classification_confidence,
              importance_score, importance_factors, brief_excluded,
              active, created_at_ms, updated_at_ms
            ) VALUES (
              'item-astral-clamp', 'news-opennews', 'wire-astral', 'wire-astral',
              '{}'::jsonb, NULL, NULL, 'Reuters', %(title)s, 'legacy',
              '', 'en', 123, 124, 125, 'old-astral-fingerprint',
              'info', 'general', 'keyword', 1, 0, '{}'::jsonb, true,
              true, 124, 125
            )
            """,
            {"title": "a" * 499 + "𝔸" + "z"},
        )
        terminalize_source_row(
            conn,
            owner_key="news_brief",
            source_table="news_brief_runs",
            target_key="old-target",
            source_row={"run_id": "old-run", "attempt_count": 3, "created_at_ms": 124},
            final_status="failed",
            final_reason="old_failure",
            now_ms=126,
        )
        push_before = _push_ledger_snapshot(conn)
        rss_before = _retained_item_snapshot(conn, "item-legacy-rss")
        conn.commit()
    finally:
        conn.close()

    command.upgrade(config, "20260807_0246")
    conn = connect_postgres_test(read_only=False)
    try:
        item = conn.execute(
            """
            SELECT title, description, reporting_origin, content_fingerprint,
                   first_observed_at_ms, last_observed_at_ms, created_at_ms, updated_at_ms
              FROM news_items
             WHERE item_id='item-retained'
            """
        ).fetchone()
        astral_item = conn.execute("SELECT title FROM news_items WHERE item_id='item-astral-clamp'").fetchone()
        push_after = _push_ledger_snapshot(conn)
        rss_after = _retained_item_snapshot(conn, "item-legacy-rss")
        old_state_counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_stories) AS stories,
              (SELECT count(*) FROM news_story_members) AS members,
              (SELECT count(*) FROM news_brief_selection_current) AS selections,
              (SELECT count(*) FROM news_brief_runs) AS runs,
              (SELECT count(*) FROM news_brief_publications) AS publications,
              (SELECT count(*) FROM queue_terminal_events
                WHERE owner_key='news_brief' OR source_table='news_brief_runs') AS terminals
            """
        ).fetchone()
        current = conn.execute("SELECT * FROM news_brief_current").fetchone()
        summary = conn.execute(
            """
            SELECT active_item_count, active_story_count,
                   unmaterialized_item_count, input_fingerprint,
                   projection_version, last_attempt_at_ms,
                   last_success_at_ms, last_error
              FROM news_projection_summary
             WHERE singleton_key='current'
            """
        ).fetchone()
    finally:
        conn.close()

    assert item == {
        "title": "Bitcoin & markets rally",
        "description": (
            "Detailed market context spans more than forty characters for readers. https://example.com/body"
        ),
        "reporting_origin": "@wireauthor",
        "content_fingerprint": "d1ea9c2c103a82d39e989c146d2e759607f08929deb7ec786c7cdf0162050754",
        "first_observed_at_ms": 124,
        "last_observed_at_ms": 125,
        "created_at_ms": 124,
        "updated_at_ms": 125,
    }
    assert astral_item == {"title": "a" * 499 + "\ufffd"}
    assert rss_after == rss_before
    assert push_after == push_before
    assert push_before["state"]["row_count"] == 1
    assert push_before["deliveries"]["row_count"] == 2
    assert push_before["deliveries"]["statuses"] == [
        {
            "story_id": "b" * 64,
            "status": "retry_wait",
            "translation_status": "not_requested",
        },
        {
            "story_id": "c" * 64,
            "status": "sent",
            "translation_status": "translated",
        },
    ]
    assert old_state_counts == {
        "stories": 0,
        "members": 0,
        "selections": 0,
        "runs": 0,
        "publications": 0,
        "terminals": 0,
    }
    assert current == {
        "singleton_key": True,
        "publication_id": None,
        "target_fingerprint": None,
        "latest_run_id": None,
        "pending_first_dirty_at_ms": None,
        "pending_due_at_ms": None,
        "updated_at_ms": 0,
    }
    assert summary == {
        "active_item_count": 2,
        "active_story_count": 0,
        "unmaterialized_item_count": 2,
        "input_fingerprint": None,
        "projection_version": None,
        "last_attempt_at_ms": None,
        "last_success_at_ms": None,
        "last_error": None,
    }


def test_world_brief_state_accepts_only_discriminated_publication_and_run_shapes() -> None:
    config, conn = _reset_to_0245()
    try:
        conn.close()
        command.upgrade(config, "20260807_0246")
        conn = connect_postgres_test(read_only=False)
        conn.execute(
            """
            INSERT INTO news_brief_publications(
              publication_id, selection_fingerprint, target_fingerprint,
              quality, brief_kind, world_brief, brief_story_lines,
              top_stories, selected_story_ids, sources, source_age_range,
              provider, model, prompt_version, workflow_version,
              composer_version, schema_version, selector_version,
              identity_version, locale, validation, provenance,
              published_at_ms, created_at_ms
            ) VALUES
              (
                repeat('a', 64), repeat('d', 64), repeat('e', 64),
                'ok', 'l1', 'Accepted cited world brief.', '["line [1]"]'::jsonb,
                '[{"story_id":"story-1"}]'::jsonb, '["story-1"]'::jsonb,
                '[{"url":""}]'::jsonb, '{"oldest_at_ms":1,"newest_at_ms":1}'::jsonb,
                'ollama', 'model', 'prompt', 'workflow', 'composer', 'schema',
                'selector', 'identity', 'en', '{}'::jsonb, '{}'::jsonb, 2, 1
              ),
              (
                repeat('b', 64), repeat('d', 64), repeat('e', 64),
                'degraded', 'l2', 'Single-headline fallback.', '[]'::jsonb,
                '[{"story_id":"story-1"}]'::jsonb, '["story-1"]'::jsonb,
                '[]'::jsonb, '{"oldest_at_ms":1,"newest_at_ms":1}'::jsonb,
                'groq', 'model', 'prompt', 'workflow', 'composer', 'schema',
                'selector', 'identity', 'en', '{}'::jsonb, '{}'::jsonb, 3, 1
              ),
              (
                repeat('c', 64), repeat('d', 64), repeat('f', 64),
                'degraded', 'none', '', '[]'::jsonb,
                '[{"story_id":"story-1"}]'::jsonb, '["story-1"]'::jsonb,
                '[{"url":"https://example.com/story-1"}]'::jsonb,
                '{"oldest_at_ms":1,"newest_at_ms":1}'::jsonb,
                '', '', 'prompt', 'workflow', 'composer', 'schema',
                'selector', 'identity', 'en', '{}'::jsonb, '{}'::jsonb, 4, 1
              );

            INSERT INTO news_brief_runs(
              run_id, target_fingerprint, selection_fingerprint, status,
              model_outcome, pointer_action, failure_count, next_due_at_ms,
              lease_owner, lease_token, lease_expires_at_ms,
              last_error_code, last_attempt_at_ms,
              created_at_ms, updated_at_ms, completed_at_ms
            ) VALUES
              (
                'waiting-run', repeat('1', 64), repeat('d', 64),
                'waiting_input', 'none', 'advance_degraded', 0, NULL,
                NULL, NULL, NULL, NULL, NULL, 1, 2, 2
              ),
              (
                'running-run', repeat('2', 64), repeat('d', 64),
                'running', NULL, 'none', 0, NULL,
                'worker', 'lease', 100, NULL, NULL, 1, 2, NULL
              ),
              (
                'retry-run', repeat('3', 64), repeat('d', 64),
                'retry_wait', 'l2', 'preserve_lkg', 1, 100,
                NULL, NULL, NULL, 'provider_exhausted', 2, 1, 2, NULL
              ),
              (
                'published-run', repeat('4', 64), repeat('d', 64),
                'published', 'ok', 'advance_ok', 0, NULL,
                NULL, NULL, NULL, NULL, 2, 1, 2, 2
              )
            """
        )
        conn.commit()

        publications = conn.execute(
            """
            SELECT brief_kind, quality, jsonb_array_length(brief_story_lines) AS line_count,
                   jsonb_array_length(sources) AS source_count
              FROM news_brief_publications
             ORDER BY brief_kind
            """
        ).fetchall()
        shared_target_count = conn.execute(
            "SELECT count(*) AS count FROM news_brief_publications WHERE target_fingerprint=repeat('e', 64)"
        ).fetchone()["count"]
        runs = conn.execute(
            "SELECT status, model_outcome, pointer_action FROM news_brief_runs ORDER BY status"
        ).fetchall()

        invalid_updates = (
            "UPDATE news_brief_publications SET sources='[]'::jsonb WHERE brief_kind='l1'",
            "UPDATE news_brief_publications SET brief_story_lines='[\"bad\"]'::jsonb WHERE brief_kind='l2'",
            "UPDATE news_brief_publications SET provider='not-empty' WHERE brief_kind='none'",
            "UPDATE news_brief_runs SET model_outcome='none' WHERE status='running'",
            "UPDATE news_brief_runs SET next_due_at_ms=10 WHERE status='waiting_input'",
        )
        for statement in invalid_updates:
            with pytest.raises(CheckViolation):
                conn.execute(statement)
            conn.rollback()
    finally:
        conn.close()

    assert publications == [
        {"brief_kind": "l1", "quality": "ok", "line_count": 1, "source_count": 1},
        {"brief_kind": "l2", "quality": "degraded", "line_count": 0, "source_count": 0},
        {"brief_kind": "none", "quality": "degraded", "line_count": 0, "source_count": 1},
    ]
    assert shared_target_count == 2
    assert runs == [
        {"status": "published", "model_outcome": "ok", "pointer_action": "advance_ok"},
        {"status": "retry_wait", "model_outcome": "l2", "pointer_action": "preserve_lkg"},
        {"status": "running", "model_outcome": None, "pointer_action": "none"},
        {"status": "waiting_input", "model_outcome": "none", "pointer_action": "advance_degraded"},
    ]


def test_opennews_fact_normalization_crosses_keyset_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    _, conn = _reset_to_0245()
    try:
        _insert_opennews_migration_rows(conn, count=3, title_prefix="Batch headline")
        conn.commit()
    finally:
        conn.close()

    migration = _migration_module()
    engine = sa.create_engine(_sqlalchemy_test_url())
    try:
        with engine.begin() as bind:
            monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
            monkeypatch.setattr(migration, "_NORMALIZATION_BATCH_SIZE", 1)
            monkeypatch.setattr(migration, "_NORMALIZATION_ROW_CAP", 10)
            migration._normalize_retained_opennews_facts()
    finally:
        engine.dispose()

    conn = connect_postgres_test(read_only=False)
    try:
        facts = conn.execute(
            """
            SELECT item_id, title, description, reporting_origin
              FROM news_items
             ORDER BY item_id
            """
        ).fetchall()
    finally:
        conn.close()
    assert facts == [
        {
            "item_id": f"migration-item-{index}",
            "title": f"Batch headline {index}",
            "description": "Detailed retained context is longer than forty UTF-16 code units.",
            "reporting_origin": f"author{index}",
        }
        for index in range(1, 4)
    ]


def test_opennews_fact_normalization_row_cap_fails_before_rewriting(monkeypatch: pytest.MonkeyPatch) -> None:
    _, conn = _reset_to_0245()
    try:
        _insert_opennews_migration_rows(conn, count=2, title_prefix="Cap headline")
        before = [
            dict(row)
            for row in conn.execute(
                "SELECT item_id, title, reporting_origin, content_fingerprint FROM news_items ORDER BY item_id"
            ).fetchall()
        ]
        conn.commit()
    finally:
        conn.close()

    migration = _migration_module()
    engine = sa.create_engine(_sqlalchemy_test_url())
    try:
        with (
            pytest.raises(RuntimeError, match="news_world_brief_hard_cut_opennews_row_cap:2"),
            engine.begin() as bind,
        ):
            monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
            monkeypatch.setattr(migration, "_NORMALIZATION_BATCH_SIZE", 1)
            monkeypatch.setattr(migration, "_NORMALIZATION_ROW_CAP", 1)
            migration._normalize_retained_opennews_facts()
    finally:
        engine.dispose()

    conn = connect_postgres_test(read_only=False)
    try:
        after = [
            dict(row)
            for row in conn.execute(
                "SELECT item_id, title, reporting_origin, content_fingerprint FROM news_items ORDER BY item_id"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert after == before


def test_world_brief_hard_cut_fails_atomically_instead_of_deleting_an_unusable_fact() -> None:
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
    command.upgrade(config, "20260807_0245")
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled, consecutive_failures,
              created_at_ms, updated_at_ms, source_kind, live_connected,
              gap_unclosed, gap_version
            ) VALUES (
              'news-opennews', 'OpenNews', 2, 'en', true, 0,
              0, 0, 'opennews', false, false, 0
            );
            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, canonical_url, reporting_origin,
              title, normalized_title, description, lang, published_at_ms,
              first_observed_at_ms, last_observed_at_ms, content_fingerprint,
              level, category, classification_source, classification_confidence,
              importance_score, importance_factors, brief_excluded,
              active, created_at_ms, updated_at_ms
            ) VALUES (
              'unusable-item', 'news-opennews', 'unusable', 'unusable',
              '{}'::jsonb, NULL, 'opennews', '<br><b></b>', 'old-admitted-token',
              '', 'en', 1, 1, 1, 'old-fingerprint', 'info', 'general',
              'keyword', 1, 0, '{}'::jsonb, false, true, 1, 1
            );
            UPDATE news_push_state
               SET baseline_at_ms=1, updated_at_ms=1
             WHERE singleton_key='current';
            INSERT INTO news_push_deliveries(
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload,
              delivery_payload, payload_fingerprint, translation_status,
              status, delivery_attempts, next_attempt_at_ms,
              lease_owner, lease_token, lease_expires_at_ms,
              receipt, last_error, sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
              repeat('f', 64), 'unusable-item', 88, 1,
              '{"title":"sealed unusable source"}'::jsonb,
              NULL, NULL, 'not_requested', 'retry_wait', 1, 2,
              NULL, NULL, NULL, NULL, 'provider_unavailable', NULL, 1, 1
            )
            """
        )
        push_before = _push_ledger_snapshot(conn)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        RuntimeError,
        match="news_world_brief_hard_cut_unusable_retained_headline:1",
    ):
        command.upgrade(config, "20260807_0246")

    conn = connect_postgres_test(read_only=False)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        retained = conn.execute("SELECT title FROM news_items WHERE item_id='unusable-item'").fetchone()
        push_after = _push_ledger_snapshot(conn)
        translation_table = conn.execute("SELECT to_regclass('news_story_title_translations') AS name").fetchone()[
            "name"
        ]
    finally:
        conn.close()

    assert version == "20260807_0245"
    assert retained == {"title": "<br><b></b>"}
    assert push_after == push_before
    assert push_after["state"]["row_count"] == 1
    assert push_after["deliveries"]["row_count"] == 1
    assert translation_table == "news_story_title_translations"
