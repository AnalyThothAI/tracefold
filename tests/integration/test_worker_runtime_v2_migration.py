from __future__ import annotations

import hashlib
import json

import pytest
from alembic import command
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import (
    connect_postgres_test,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as _test_postgres_dsn,
)
from tracefold.platform.postgres.postgres_migrations import alembic_config

_LEASE_OWNER = "00000000-0000-0000-0000-000000000033"
_FUTURE_MS = 9_000_000_000_000


def test_migration_preserves_every_outer_news_status_and_native_model_state() -> None:
    _prepare_0232()
    conn = connect_postgres_test(read_only=False)
    try:
        for index, status in enumerate(("clean", "dirty", "running", "retry_wait", "quarantined")):
            conn.execute(
                """
                INSERT INTO model_generation_frontiers(
                  candidate_kind, shard_key, status, first_dirty_at_ms,
                  deadline_at_ms, next_attempt_at_ms, attempt_count,
                  transient_failure_count, input_fingerprint, workflow_version,
                  claimed_by, claimed_until_ms, last_error_code, updated_at_ms
                )
                VALUES (
                  'news_brief', %s, %s, %s, %s, %s, %s,
                  0, %s, 'workflow-v1', %s, %s, %s, %s
                )
                """,
                (
                    f"news-{index}-{status}",
                    status,
                    1_000 + index,
                    2_000 + index,
                    3_000 + index if status == "retry_wait" else None,
                    index,
                    f"fingerprint-{status}",
                    "00000000-0000-0000-0000-000000000033" if status == "running" else None,
                    4_000 + index if status == "running" else None,
                    f"error-{status}" if status in {"retry_wait", "quarantined"} else None,
                    5_000 + index,
                ),
            )
        _insert_document_jobs(conn)
        conn.execute(
            """
            INSERT INTO model_generation_frontiers(
              candidate_kind, shard_key, status, first_dirty_at_ms,
              deadline_at_ms, next_attempt_at_ms, attempt_count,
              transient_failure_count, input_fingerprint, workflow_version,
              claimed_by, claimed_until_ms, last_error_code, updated_at_ms
            )
            VALUES (
              'macro_document_analysis', 'all', 'running', 1000,
              2000, NULL, 3, 0, 'document-fingerprint', 'workflow-v1',
              '00000000-0000-0000-0000-000000000033', 6000,
              'interrupted', 7000
            )
            """
        )
        _insert_thesis_run(conn)
        conn.execute(
            """
            INSERT INTO model_generation_frontiers(
              candidate_kind, shard_key, status, first_dirty_at_ms,
              deadline_at_ms, next_attempt_at_ms, attempt_count,
              transient_failure_count, input_fingerprint, workflow_version,
              claimed_by, claimed_until_ms, last_error_code, updated_at_ms
            )
            VALUES (
              'macro_thesis', '2026-07-30', 'quarantined', 1000,
              2000, NULL, 4, 0, 'thesis-fingerprint', 'workflow-v1',
              NULL, NULL, 'terminal', 8000
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    _upgrade_head()

    conn = connect_postgres_test(read_only=False)
    try:
        rows = conn.execute(
            """
            SELECT fingerprint, status, attempt_count, next_due_at_ms,
                   lease_owner, lease_expires_at_ms
              FROM news_brief_runs
             WHERE fingerprint LIKE 'fingerprint-%'
             ORDER BY fingerprint
            """
        ).fetchall()
        by_fingerprint = {row["fingerprint"]: row for row in rows}
        assert set(by_fingerprint) == {
            "fingerprint-dirty",
            "fingerprint-running",
            "fingerprint-retry_wait",
            "fingerprint-quarantined",
        }
        assert by_fingerprint["fingerprint-dirty"]["status"] == "retryable"
        assert by_fingerprint["fingerprint-running"]["status"] == "retryable"
        assert by_fingerprint["fingerprint-retry_wait"]["status"] == "retryable"
        assert by_fingerprint["fingerprint-quarantined"]["status"] == "failed"
        assert by_fingerprint["fingerprint-quarantined"]["next_due_at_ms"] is None
        assert all(row["lease_owner"] is None for row in rows)
        assert all(row["lease_expires_at_ms"] is None for row in rows)

        document_rows = conn.execute(
            """
            SELECT status, next_due_at_ms, leased_until_ms, lease_owner,
                   attempt_count, last_error_code
              FROM macro_document_analysis_jobs
             ORDER BY analysis_job_id
            """
        ).fetchall()
        assert len(document_rows) == 2
        assert all(row["status"] == "retryable" for row in document_rows)
        assert all(row["next_due_at_ms"] >= 6_000 for row in document_rows)
        assert all(row["leased_until_ms"] is None for row in document_rows)
        assert all(row["lease_owner"] is None for row in document_rows)
        assert all(row["attempt_count"] == 3 for row in document_rows)
        assert all(row["last_error_code"] == "interrupted" for row in document_rows)

        thesis = conn.execute(
            """
            SELECT status, attempt_count, leased_until_ms, lease_owner,
                   last_error_code
              FROM macro_thesis_runs
             WHERE session_date = '2026-07-30'
            """
        ).fetchone()
        assert thesis["status"] == "failed"
        assert thesis["attempt_count"] == 4
        assert thesis["leased_until_ms"] is None
        assert thesis["lease_owner"] is None
        assert thesis["last_error_code"] == "terminal"
        assert conn.execute("SELECT to_regclass('model_generation_frontiers') AS name").fetchone()["name"] is None
    finally:
        conn.close()


def test_migration_fans_out_document_terminal_targets_with_exact_hashes() -> None:
    _prepare_0232()
    conn = connect_postgres_test(read_only=False)
    try:
        _insert_document_jobs(conn)
        source = {
            "candidate_kind": "macro_document_analysis",
            "shard_key": "all",
            "input_fingerprint": "document-fingerprint",
        }
        _insert_terminal(
            conn,
            terminal_id="terminal-model",
            worker_name="model_projection",
            source_table="model_generation_frontiers",
            target_key="all",
            source=source,
        )
        _insert_terminal(
            conn,
            terminal_id="terminal-radar",
            worker_name="steady_projection_coordinator",
            source_table="radar_projection_frontiers",
            target_key="radar:all",
            source={"target_key": "radar:all"},
        )
        conn.commit()
    finally:
        conn.close()

    _upgrade_head()

    conn = connect_postgres_test(read_only=False)
    try:
        parent = conn.execute(
            """
            SELECT owner_key, operator_action, operator_reason
              FROM queue_terminal_events
             WHERE terminal_id = 'terminal-model'
            """
        ).fetchone()
        expected_target_hash = _sha256_json(["job-a", "job-b"])
        assert parent["owner_key"] == "macro_document_analysis"
        assert parent["operator_action"] == "archive"
        assert parent["operator_reason"] == f"migrated_to_native:{expected_target_hash}"

        children = conn.execute(
            """
            SELECT owner_key, source_table, target_key, source_row_json,
                   source_row_hash, operator_action
              FROM queue_terminal_events
             WHERE source_row_json->>'migrated_from_terminal_id' = 'terminal-model'
             ORDER BY target_key
            """
        ).fetchall()
        assert [row["target_key"] for row in children] == ["job-a", "job-b"]
        for row in children:
            assert row["owner_key"] == "macro_document_analysis"
            assert row["source_table"] == "macro_document_analysis_jobs"
            assert row["operator_action"] is None
            assert row["source_row_json"]["target_set_hash"] == expected_target_hash
            assert row["source_row_hash"] == _sha256_json(row["source_row_json"])

        radar = conn.execute(
            "SELECT owner_key FROM queue_terminal_events WHERE terminal_id = 'terminal-radar'"
        ).fetchone()
        assert radar["owner_key"] == "radar_projection"
    finally:
        conn.close()


def test_migration_refuses_unknown_terminal_owner_without_partial_schema_changes() -> None:
    _prepare_0232()
    conn = connect_postgres_test(read_only=False)
    try:
        _insert_terminal(
            conn,
            terminal_id="terminal-unknown",
            worker_name="unclassified_projection",
            source_table="news_projection_frontiers",
            target_key="unknown",
            source={"target_key": "unknown"},
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="worker_runtime_v2_unknown_terminal_owners:unclassified_projection"):
        _upgrade_head()

    conn = connect_postgres_test(read_only=False)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260731_0232"
        assert (
            conn.execute("SELECT to_regclass('worker_queue_terminal_events') AS name").fetchone()["name"]
            == "worker_queue_terminal_events"
        )
        assert conn.execute("SELECT to_regclass('queue_terminal_events') AS name").fetchone()["name"] is None
        assert conn.execute("SELECT to_regclass('workers_runtime') AS name").fetchone()["name"] is None
    finally:
        conn.close()


def test_migration_maps_only_authorized_legacy_terminal_owners_and_preserves_evidence() -> None:
    _prepare_0232()
    legacy_rows = (
        (
            "terminal-news-page",
            "news_page_projection",
            "news_projection_dirty_targets",
            "news:page:42",
            "news_projection",
        ),
        (
            "terminal-news-source-quality",
            "news_source_quality_projection",
            "news_projection_dirty_targets",
            "news:source:quality:7",
            "news_projection",
        ),
        (
            "terminal-radar-dirty",
            "token_radar_projection",
            "token_radar_dirty_targets",
            "radar:dirty:btc",
            "radar_projection",
        ),
        (
            "terminal-radar-source",
            "token_radar_projection",
            "token_radar_source_dirty_events",
            "radar:event:eth",
            "radar_projection",
        ),
    )
    conn = connect_postgres_test(read_only=False)
    try:
        for terminal_id, legacy_owner, source_table, target_key, _ in legacy_rows:
            _insert_terminal(
                conn,
                terminal_id=terminal_id,
                worker_name=legacy_owner,
                source_table=source_table,
                target_key=target_key,
                source={"target_key": target_key, "legacy_owner": legacy_owner},
            )
        conn.execute(
            """
            UPDATE worker_queue_terminal_events
               SET operator_action = 'archive',
                   operator_reason = 'operator-reviewed',
                   operator_action_at_ms = 350
             WHERE terminal_id = 'terminal-news-source-quality'
            """
        )
        before = {
            row["terminal_id"]: {
                key: value for key, value in dict(row).items() if key not in {"terminal_id", "worker_name"}
            }
            for row in conn.execute("SELECT * FROM worker_queue_terminal_events ORDER BY terminal_id").fetchall()
        }
        conn.commit()
    finally:
        conn.close()

    _upgrade_head()

    conn = connect_postgres_test(read_only=False)
    try:
        rows = conn.execute("SELECT * FROM queue_terminal_events ORDER BY terminal_id").fetchall()
        by_id = {row["terminal_id"]: row for row in rows}
        assert set(by_id) == {row[0] for row in legacy_rows}
        for terminal_id, _, _, _, expected_owner in legacy_rows:
            actual = dict(by_id[terminal_id])
            assert actual.pop("owner_key") == expected_owner
            actual.pop("terminal_id")
            assert actual == before[terminal_id]
    finally:
        conn.close()


def test_migration_retains_matching_valid_native_leases_and_transient_attempt_floors() -> None:
    _prepare_0232()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_brief_runs(
              run_id, fingerprint, status, attempt_count,
              candidate_story_count, candidate_source_count,
              lease_owner, lease_expires_at_ms, heartbeat_at_ms,
              created_at_ms, updated_at_ms
            )
            VALUES ('brief-valid', 'brief-valid', 'running', 1, 3, 2,
                    %s, %s, 100, 100, 100)
            """,
            (_LEASE_OWNER, _FUTURE_MS),
        )
        _insert_document_jobs(conn)
        conn.execute(
            """
            UPDATE macro_document_analysis_jobs
               SET status = 'claimed', lease_owner = %s,
                   leased_until_ms = %s, attempt_count = 1
             WHERE analysis_job_id = 'job-a'
            """,
            (_LEASE_OWNER, _FUTURE_MS),
        )
        _insert_thesis_run(conn)
        _insert_thesis_research_input(conn)
        conn.execute(
            """
            UPDATE macro_thesis_runs
               SET status = 'running', lease_owner = %s,
                   leased_until_ms = %s, attempt_count = 1,
                   research_input_id = 'research-input-1',
                   research_input_hash = 'research-input-hash'
             WHERE session_date = '2026-07-30'
            """,
            (_LEASE_OWNER, _FUTURE_MS),
        )
        for kind, shard, fingerprint in (
            ("news_brief", "brief", "brief-valid"),
            ("macro_document_analysis", "all", "document-valid"),
            ("macro_thesis", "2026-07-30", "thesis-valid"),
        ):
            conn.execute(
                """
                INSERT INTO model_generation_frontiers(
                  candidate_kind, shard_key, status, first_dirty_at_ms,
                  deadline_at_ms, attempt_count, transient_failure_count,
                  input_fingerprint, workflow_version, claimed_by,
                  claimed_until_ms, last_error_code, updated_at_ms
                )
                VALUES (%s, %s, 'running', 100, 200, 2, 4, %s,
                        'workflow-v1', %s, %s, 'outer-error', 300)
                """,
                (kind, shard, fingerprint, _LEASE_OWNER, _FUTURE_MS),
            )
        conn.commit()
    finally:
        conn.close()

    _upgrade_head()

    conn = connect_postgres_test(read_only=False)
    try:
        brief = conn.execute("SELECT * FROM news_brief_runs WHERE fingerprint = 'brief-valid'").fetchone()
        assert brief["status"] == "running"
        assert brief["lease_owner"] == _LEASE_OWNER
        assert brief["lease_expires_at_ms"] == _FUTURE_MS
        assert brief["attempt_count"] == 4
        document = conn.execute("SELECT * FROM macro_document_analysis_jobs WHERE analysis_job_id = 'job-a'").fetchone()
        assert document["status"] == "claimed"
        assert document["lease_owner"] == _LEASE_OWNER
        assert document["leased_until_ms"] == _FUTURE_MS
        assert document["attempt_count"] == 4
        thesis = conn.execute("SELECT * FROM macro_thesis_runs WHERE session_date = '2026-07-30'").fetchone()
        assert thesis["status"] == "running"
        assert thesis["lease_owner"] == _LEASE_OWNER
        assert thesis["leased_until_ms"] == _FUTURE_MS
        assert thesis["attempt_count"] == 4
    finally:
        conn.close()


def test_migration_creates_missing_dirty_thesis_native_intent() -> None:
    _prepare_0232()
    conn = connect_postgres_test(read_only=False)
    try:
        _insert_thesis_pack(conn)
        conn.execute(
            """
            INSERT INTO model_generation_frontiers(
              candidate_kind, shard_key, status, first_dirty_at_ms,
              deadline_at_ms, attempt_count, transient_failure_count,
              input_fingerprint, workflow_version, updated_at_ms
            )
            VALUES ('macro_thesis', '2026-07-30', 'dirty', 100, 200,
                    0, 0, 'thesis-dirty', 'workflow-v1', 300)
            """
        )
        conn.commit()
    finally:
        conn.close()

    _upgrade_head()

    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute(
            "SELECT status, due_at_ms FROM macro_thesis_runs WHERE session_date = '2026-07-30'"
        ).fetchone()
        assert row == {"status": "pending", "due_at_ms": 200}
    finally:
        conn.close()


def test_migration_revives_existing_failed_native_state_for_dirty_outer_intent() -> None:
    _prepare_0232()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_brief_runs(
              run_id, fingerprint, status, attempt_count,
              candidate_story_count, candidate_source_count,
              last_error, created_at_ms, updated_at_ms, completed_at_ms
            )
            VALUES ('brief-revive', 'brief-revive', 'failed', 1, 3, 2,
                    'old-native-failure', 100, 200, 200)
            """
        )
        _insert_document_jobs(conn)
        conn.execute(
            """
            UPDATE macro_document_analysis_jobs
               SET status = 'failed', attempt_count = 1, max_attempts = 3,
                   last_error_code = 'old-native-failure'
             WHERE analysis_job_id = 'job-a'
            """
        )
        _insert_thesis_run(conn)
        conn.execute("ALTER TABLE macro_thesis_runs DISABLE TRIGGER macro_thesis_runs_lifecycle")
        conn.execute(
            """
            UPDATE macro_thesis_runs
               SET status = 'failed', attempt_count = 1,
                   last_error_code = 'old-native-failure'
             WHERE session_date = '2026-07-30'
            """
        )
        conn.execute("ALTER TABLE macro_thesis_runs ENABLE TRIGGER macro_thesis_runs_lifecycle")
        for kind, shard, fingerprint in (
            ("news_brief", "brief", "brief-revive"),
            ("macro_document_analysis", "all", "document-revive"),
            ("macro_thesis", "2026-07-30", "thesis-revive"),
        ):
            conn.execute(
                """
                INSERT INTO model_generation_frontiers(
                  candidate_kind, shard_key, status, first_dirty_at_ms,
                  deadline_at_ms, attempt_count, transient_failure_count,
                  input_fingerprint, workflow_version, last_error_code,
                  updated_at_ms
                )
                VALUES (%s, %s, 'dirty', 100, 150, 1, 0, %s,
                        'workflow-v1', 'outer-dirty', 300)
                """,
                (kind, shard, fingerprint),
            )
        conn.commit()
    finally:
        conn.close()

    _upgrade_head()

    conn = connect_postgres_test(read_only=False)
    try:
        brief = conn.execute(
            """
            SELECT status, attempt_count, completed_at_ms
              FROM news_brief_runs
             WHERE fingerprint = 'brief-revive'
            """
        ).fetchone()
        assert brief == {
            "status": "retryable",
            "attempt_count": 1,
            "completed_at_ms": None,
        }
        document = conn.execute(
            """
            SELECT status, attempt_count, max_attempts
              FROM macro_document_analysis_jobs
             WHERE analysis_job_id = 'job-a'
            """
        ).fetchone()
        assert document == {
            "status": "retryable",
            "attempt_count": 1,
            "max_attempts": 4,
        }
        thesis = conn.execute(
            """
            SELECT status, attempt_count, max_attempts
              FROM macro_thesis_runs
             WHERE session_date = '2026-07-30'
            """
        ).fetchone()
        assert thesis == {
            "status": "pending",
            "attempt_count": 1,
            "max_attempts": 6,
        }
    finally:
        conn.close()


def test_migration_recompute_terminal_brief_owner_clears_pending_clocks() -> None:
    _prepare_0232()
    story_id = "story-terminal-owner"
    state_fingerprint = "state-terminal-owner"
    fingerprint = _sha256_json(
        {
            "contract": {
                "prompt": "worldmonitor_top8_zh_v1",
                "workflow": "worldmonitor_world_brief_v1",
                "schema": "worldmonitor_world_brief_schema_v1",
                "locale": "zh-CN",
            },
            "stories": [
                {
                    "story_id": story_id,
                    "state_fingerprint": state_fingerprint,
                    "rank": 1,
                }
            ],
        }
    )
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, feed_url, tier, lang, enabled,
              refresh_interval_seconds, next_fetch_at_ms, created_at_ms, updated_at_ms
            )
            VALUES ('source-terminal-owner', 'Terminal Owner',
                    'https://example.com/terminal-owner.xml', 1, 'en', true,
                    60, 0, 0, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO news_items(
              item_id, source_id, source_item_key, canonical_url,
              reporting_origin, title, normalized_title, description, lang,
              published_at_ms, first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, level, category, classification_source,
              classification_confidence, importance_score, importance_factors,
              brief_excluded, active, created_at_ms, updated_at_ms
            )
            VALUES (
              'item-terminal-owner', 'source-terminal-owner', 'item-key',
              'https://example.com/terminal-owner', 'example',
              'Terminal owner story', 'terminal owner story', '', 'en',
              100, 100, 100, 'content-terminal-owner', 'high', 'economic',
              'keyword', 1.0, 90, %s, false, true, 100, 100
            )
            """,
            (Jsonb({}),),
        )
        conn.execute(
            """
            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_url,
              representative_description, scoring_item_id, level, category,
              importance_score, importance_factors, item_count, source_count,
              first_published_at_ms, last_published_at_ms, active,
              state_fingerprint, created_at_ms, updated_at_ms
            )
            VALUES (
              %s, 'canonical-terminal-owner', 'Terminal owner story',
              'item-terminal-owner', 'source-terminal-owner',
              'Terminal owner story', 'https://example.com/terminal-owner',
              '', 'item-terminal-owner', 'high', 'economic', 90, %s, 1, 1,
              100, 100, true, %s, 100, 100
            )
            """,
            (story_id, Jsonb({}), state_fingerprint),
        )
        conn.execute(
            "INSERT INTO news_brief_selection_current(rank, story_id, updated_at_ms) VALUES (1, %s, 100)",
            (story_id,),
        )
        conn.execute(
            """
            INSERT INTO news_brief_runs(
              run_id, fingerprint, status, attempt_count,
              candidate_story_count, candidate_source_count,
              created_at_ms, updated_at_ms, completed_at_ms
            )
            VALUES ('run-terminal-owner', %s, 'insufficient_material', 1,
                    1, 1, 100, 100, 100)
            """,
            (fingerprint,),
        )
        conn.execute(
            """
            UPDATE news_brief_current
               SET target_fingerprint = 'old-target', latest_run_id = NULL,
                   publication_id = NULL, updated_at_ms = 100
             WHERE singleton_key
            """
        )
        conn.commit()
    finally:
        conn.close()

    _upgrade_head()

    conn = connect_postgres_test(read_only=False)
    try:
        current = conn.execute(
            """
            SELECT target_fingerprint, latest_run_id,
                   pending_first_dirty_at_ms, pending_due_at_ms
              FROM news_brief_current
             WHERE singleton_key
            """
        ).fetchone()
        assert current == {
            "target_fingerprint": fingerprint,
            "latest_run_id": "run-terminal-owner",
            "pending_first_dirty_at_ms": None,
            "pending_due_at_ms": None,
        }
    finally:
        conn.close()


def _prepare_0232() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    command.upgrade(config, "20260731_0232")


def _upgrade_head() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    command.upgrade(config, "head")


def _insert_document_jobs(conn) -> None:
    for suffix in ("a", "b"):
        conn.execute(
            """
            INSERT INTO macro_documents(
              document_id, dataset_id, document_type, title, effective_date,
              published_at_ms, received_at_ms, source_url, content_text,
              fact_hash, metadata_json
            )
            VALUES (%s, 'fed.documents', 'statement', %s, '2026-07-30',
                    100, 100, %s, 'body', %s, %s)
            """,
            (
                f"document-{suffix}",
                f"Document {suffix}",
                f"https://example.test/{suffix}",
                f"fact-{suffix}",
                Jsonb({}),
            ),
        )
        conn.execute(
            """
            INSERT INTO macro_document_analysis_jobs(
              analysis_job_id, document_id, document_hash, model_name,
              prompt_version, status, next_due_at_ms, leased_until_ms,
              lease_owner, attempt_count, max_attempts, last_error_code,
              created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, 'model', 'prompt', 'pending', 100,
                    NULL, NULL, 0, 5, NULL, 100, 100)
            """,
            (f"job-{suffix}", f"document-{suffix}", f"hash-{suffix}"),
        )


def _insert_thesis_run(conn) -> None:
    _insert_thesis_pack(conn)
    conn.execute(
        """
        INSERT INTO macro_thesis_runs(
          session_date, cutoff_ms, evidence_pack_id, evidence_pack_hash,
          status, attempt_count, max_attempts, due_at_ms, leased_until_ms,
          lease_owner, publication_id, last_error_code, last_error_message,
          created_at_ms, updated_at_ms
        )
        VALUES ('2026-07-30', 100, 'pack-1', 'pack-hash', 'pending',
                0, 5, 100, NULL, NULL, NULL, NULL, NULL, 100, 100)
        """
    )


def _insert_thesis_pack(conn) -> None:
    conn.execute(
        """
        INSERT INTO macro_evidence_packs(
          evidence_pack_id, session_date, cutoff_ms, sealed_at_ms,
          source_max_received_at_ms, schema_version, payload_json, payload_hash
        )
        VALUES ('pack-1', '2026-07-30', 100, 100, 100,
                'macro_evidence_pack_v3', %s, 'pack-hash')
        """,
        (Jsonb({}),),
    )


def _insert_thesis_research_input(conn) -> None:
    conn.execute(
        """
        INSERT INTO macro_research_inputs(
          research_input_id, evidence_pack_id, session_date, cutoff_ms,
          schema_version, profile_version, prompt_version, payload_json, input_hash
        )
        VALUES ('research-input-1', 'pack-1', '2026-07-30', 100,
                'macro_research_input_v1', 'profile-v1', 'prompt-v1', %s,
                'research-input-hash')
        """,
        (Jsonb({}),),
    )


def _insert_terminal(
    conn,
    *,
    terminal_id: str,
    worker_name: str,
    source_table: str,
    target_key: str,
    source: dict[str, str],
) -> None:
    conn.execute(
        """
        INSERT INTO worker_queue_terminal_events(
          terminal_id, worker_name, source_table, target_key,
          source_row_json, source_row_hash, final_status, final_reason,
          attempt_count, payload_hash, first_seen_at_ms, last_attempted_at_ms,
          terminalized_at_ms, terminal_generation, final_reason_bucket
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'quarantined', 'test',
                3, 'payload-hash', 100, 200, 300, 1, 'other')
        """,
        (
            terminal_id,
            worker_name,
            source_table,
            target_key,
            Jsonb(source),
            _sha256_json(source),
        ),
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
