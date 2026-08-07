from __future__ import annotations

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
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


def test_world_brief_hard_cut_has_one_thirteen_table_schema() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
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
            )
            """
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
        push_before = conn.execute(
            """
            SELECT (SELECT to_jsonb(state) FROM news_push_state state) AS state,
                   (SELECT to_jsonb(delivery) FROM news_push_deliveries delivery) AS delivery
            """
        ).fetchone()
        conn.commit()
    finally:
        conn.close()

    command.upgrade(config, "head")
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
        push_after = conn.execute(
            """
            SELECT (SELECT to_jsonb(state) FROM news_push_state state) AS state,
                   (SELECT to_jsonb(delivery) FROM news_push_deliveries delivery) AS delivery
            """
        ).fetchone()
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
    assert push_after == push_before
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
        "active_item_count": 1,
        "active_story_count": 0,
        "unmaterialized_item_count": 1,
        "input_fingerprint": None,
        "projection_version": None,
        "last_attempt_at_ms": None,
        "last_success_at_ms": None,
        "last_error": None,
    }


def test_world_brief_state_accepts_only_discriminated_publication_and_run_shapes() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
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
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        RuntimeError,
        match="news_world_brief_hard_cut_unusable_retained_headline:1",
    ):
        command.upgrade(config, "head")

    conn = connect_postgres_test(read_only=False)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        retained = conn.execute("SELECT title FROM news_items WHERE item_id='unusable-item'").fetchone()
        translation_table = conn.execute("SELECT to_regclass('news_story_title_translations') AS name").fetchone()[
            "name"
        ]
    finally:
        conn.close()

    assert version == "20260807_0245"
    assert retained == {"title": "<br><b></b>"}
    assert translation_table == "news_story_title_translations"
