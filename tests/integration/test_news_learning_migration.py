from __future__ import annotations

import json
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
        assert revision["version_num"] == "20260821_0289"
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
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260821_0289"
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()
