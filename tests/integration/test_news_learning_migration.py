from __future__ import annotations

import json
import time
from typing import Any

from alembic import command

from tests.postgres_test_utils import (
    connect_postgres_test,
    reset_postgres_schema,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as postgres_test_dsn,
)
from tracefold.platform.postgres.postgres_migrations import alembic_config


def _upgrade(revision: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    command.upgrade(config, revision)


def _fresh_schema_at(revision: str) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    _upgrade(revision)


def test_0283_to_head_preserves_eventless_legacy_label_byte_for_byte() -> None:
    """The production upgrade hard-cuts Label v1 only after a lossless copy.

    Event-less misses are the most important legacy shape because they are the
    only evidence that the old pipeline failed before creating an Event.
    """

    label_id = "a" * 64
    label = {"label": "missed", "note": "operator observed an upstream miss"}
    subject = "DRAM export unit price continued to rise"
    conn: Any | None = None
    try:
        _fresh_schema_at("20260820_0283")
        conn = connect_postgres_test(read_only=False)
        conn.execute(
            """
            INSERT INTO news_event_labels (
              event_id, label_version, source, label, created_at_ms,
              labeled_by, subject, label_id
            ) VALUES (NULL, %s, 'human', %s::jsonb, %s, %s, %s, %s)
            """,
            ("news_label_v1", json.dumps(label), 1_787_279_400_000, "massis", subject, label_id),
        )
        conn.commit()
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision["version_num"] == "20260822_0292"
        assert conn.execute("SELECT to_regclass('public.news_event_labels') AS name").fetchone()["name"] is None

        migrated = conn.execute(
            """
            SELECT review_kind, subject_kind, task_id, task_version, event_id,
                   rubric_version, reader_contract_version, reviewer,
                   release_eligible, created_at_ms, payload
              FROM news_reviews
             WHERE review_kind = 'legacy'
            """
        ).fetchall()
        assert len(migrated) == 1
        row = migrated[0]
        assert row["review_kind"] == "legacy"
        assert row["subject_kind"] == "legacy_label"
        assert row["task_id"] == f"legacy:{label_id}"
        assert row["task_version"] == f"legacy:{label_id}"
        assert row["event_id"] is None
        assert row["rubric_version"] == "news_label_v1_legacy"
        assert row["reader_contract_version"] == "unknown"
        assert row["reviewer"] == "massis"
        assert row["release_eligible"] is False
        assert row["created_at_ms"] == 1_787_279_400_000
        assert row["payload"] == {
            "event_id": None,
            "label_version": "news_label_v1",
            "source": "human",
            "label": label,
            "created_at_ms": 1_787_279_400_000,
            "labeled_by": "massis",
            "subject": subject,
            "label_id": label_id,
        }

        privileges = conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_serve', 'news_reviews', 'SELECT') AS review_select,
              has_table_privilege('tracefold_serve', 'news_reviews', 'INSERT') AS review_insert,
              has_table_privilege('tracefold_serve', 'news_reviews', 'UPDATE,DELETE') AS review_rewrite,
              has_table_privilege('tracefold_serve', 'news_events', 'INSERT') AS news_insert,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'SELECT')
                AS workers_evidence_select,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'INSERT')
                AS workers_evidence_insert,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'UPDATE')
                AS workers_evidence_update,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'DELETE')
                AS workers_evidence_delete
            """
        ).fetchone()
        assert privileges == {
            "review_select": True,
            "review_insert": True,
            "review_rewrite": False,
            "news_insert": False,
            "workers_evidence_select": True,
            "workers_evidence_insert": True,
            "workers_evidence_update": False,
            "workers_evidence_delete": False,
        }
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0288_to_head_repairs_the_worker_evidence_grant() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260821_0288")
        conn = connect_postgres_test(read_only=False)
        conn.execute("REVOKE ALL ON news_event_evidence_snapshots FROM tracefold_workers")
        conn.commit()
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        privileges = conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'SELECT')
                AS select_allowed,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'INSERT')
                AS insert_allowed,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'UPDATE')
                AS update_allowed,
              has_table_privilege('tracefold_workers', 'news_event_evidence_snapshots', 'DELETE')
                AS delete_allowed
            """
        ).fetchone()
        assert privileges == {
            "select_allowed": True,
            "insert_allowed": True,
            "update_allowed": False,
            "delete_allowed": False,
        }
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260822_0292"
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0291_to_head_preserves_prompt_recordings_as_audit_and_starts_program_epoch() -> None:
    """The hard cut adds call-path identity without rewriting append-only history."""

    conn: Any | None = None
    try:
        _fresh_schema_at("20260821_0291")
        conn = connect_postgres_test(read_only=False)
        conn.execute(
            """
            INSERT INTO news_model_recordings (
              recording_sha, run_sha, case_id, arm, trial, request_sha256,
              response_sha256, request, response, provider, model, model_sha,
              execution_contract_sha, latency_ms, input_tokens, output_tokens,
              finish_reason, error_code, created_at_ms
            ) VALUES (
              %s, %s, 'legacy-case', 'stable', 1, %s,
              %s, '{}'::jsonb, '{}'::jsonb, 'litellm', 'legacy-model', %s,
              %s, 10, 11, 7, 'stop', NULL, 1787329286999
            )
            """,
            ("1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, "6" * 64),
        )
        conn.commit()
        conn.close()
        conn = None

        deployed_after_ms = int(time.time() * 1000) - 5_000
        _upgrade("head")
        deployed_before_ms = int(time.time() * 1000) + 5_000

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260822_0292"
        epoch = conn.execute("SELECT * FROM news_learning_epochs WHERE epoch_id = 'program_v1'").fetchone()
        assert epoch is not None
        assert deployed_after_ms <= epoch["starts_at_ms"] <= deployed_before_ms
        assert epoch["created_at_ms"] == epoch["starts_at_ms"]
        assert epoch["prior_evidence_disposition"] == "audit_only"
        assert epoch["reset_reason"] == "zero_compatibility_reset_reaccrue_evidence"
        assert epoch["program_factory_id"] == "tracefold.news.semantic_program.factory_v1"
        assert epoch["artifact_schema_version"] == "news_semantic_program_artifact_v1"
        assert epoch["baseline_program_version"] == "news_semantic_program_v1"
        assert epoch["baseline_program_sha256"] == ("04b10a7cc83cb876b79d89f2727caab747e503d326cfdaa5636f76c2648b10c8")
        legacy = conn.execute(
            "SELECT predictor_name, call_index, attempt, route, cached_tokens, total_tokens, "
            "provider_cost_microusd FROM news_model_recordings WHERE recording_sha = %s",
            ("1" * 64,),
        ).fetchone()
        assert legacy == {
            "predictor_name": "legacy_prompt",
            "call_index": 0,
            "attempt": 1,
            "route": "legacy",
            "cached_tokens": None,
            "total_tokens": None,
            "provider_cost_microusd": None,
        }
        verdict_columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'news_verdicts'"
            ).fetchall()
        }
        assert {"program_version", "program_sha256"} <= verdict_columns
        privileges = conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_serve', 'news_learning_epochs', 'SELECT') AS serve_select,
              has_table_privilege('tracefold_serve', 'news_learning_epochs', 'INSERT') AS serve_insert,
              has_table_privilege('tracefold_workers', 'news_learning_epochs', 'SELECT') AS workers_select,
              has_table_privilege('tracefold_workers', 'news_learning_epochs', 'INSERT') AS workers_insert
            """
        ).fetchone()
        assert privileges == {
            "serve_select": True,
            "serve_insert": False,
            "workers_select": True,
            "workers_insert": False,
        }
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()
