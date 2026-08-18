from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    prepare_postgres_database,
)
from tracefold.app.cli.commands import ops
from tracefold.app.database import WorkerDatabase
from tracefold.app.queue_health import fetch_queue_table_health, queue_tables_for_owner
from tracefold.app.repositories import repositories_for_connection
from tracefold.platform.config.settings import Settings
from tracefold.platform.postgres.projection_frontier import MACRO_FRONTIER

NOW_MS = 10_000
ALL_QUEUE_TABLES = (
    "event_anchor_backfill_jobs",
    "macro_document_analysis_jobs",
    "macro_module_frontiers",
)


def test_active_queue_inspection_covers_every_declared_queue_while_workers_are_running(
    tmp_path,
    monkeypatch,
) -> None:
    prepare_postgres_database()
    settings = Settings(storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    monkeypatch.setattr(ops, "load_settings", lambda **_kwargs: settings)
    worker_db = WorkerDatabase.create(settings)
    steady_lock = worker_db.acquire_steady_runtime_lock()
    try:
        queue_code, queue_payload = ops.handle_ops(
            SimpleNamespace(
                ops_command="queue-inspect",
                status="active",
                owner="",
                source_table="",
                reason_bucket="",
                limit=50,
            ),
            object(),
        )
    finally:
        worker_db.release_steady_runtime_lock(steady_lock)
        asyncio.run(worker_db.aclose())

    assert queue_code == 0
    assert queue_payload["ok"] is True
    assert tuple(item["source_table"] for item in queue_payload["data"]["items"]) == ALL_QUEUE_TABLES
    assert all(item["queue_health"]["available"] for item in queue_payload["data"]["items"])


def test_queue_registry_has_one_truthful_owner_for_all_three_tables() -> None:
    assert queue_tables_for_owner(None) == ALL_QUEUE_TABLES
    assert queue_tables_for_owner("macro_projection") == ("macro_module_frontiers",)
    assert queue_tables_for_owner("unknown") == ()


def test_frontier_and_native_job_health_use_eligibility_and_expired_lease_clocks() -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    repos = repositories_for_connection(conn)
    try:
        with repos.transaction():
            repos.projection_frontiers.mark_dirty(
                MACRO_FRONTIER,
                key={"module_id": "queue-health-module"},
                dirty_at_ms=NOW_MS - 2_000,
                deadline_at_ms=NOW_MS + 5_000,
                eligible_at_ms=NOW_MS - 1_000,
                input_fingerprint="sha256:macro-input",
                version="macro-test-v1",
            )
            _insert_macro_document_jobs(conn)

        frontier = fetch_queue_table_health(conn, "macro_module_frontiers", now_ms=NOW_MS)
        document = fetch_queue_table_health(conn, "macro_document_analysis_jobs", now_ms=NOW_MS)

        assert frontier["kind"] == "projection_frontier"
        assert frontier["queue_depth"] == 1
        assert frontier["due_count"] == 1
        assert frontier["oldest_due_age_ms"] == 1_000

        assert document["counts_by_status"] == {"claimed": 1, "retryable": 1}
        assert document["queue_depth"] == 2
        assert document["due_count"] == 1
        assert document["running_count"] == 0
        assert document["failed_count"] == 1
        assert document["status"] == "degraded"
    finally:
        conn.close()


def _insert_macro_document_jobs(conn) -> None:
    conn.execute(
        """
        INSERT INTO macro_documents(
          document_id, dataset_id, document_type, title, effective_date,
          published_at_ms, received_at_ms, source_url, content_text,
          fact_hash, metadata_json
        )
        VALUES (
          'document-health-1', 'federal_reserve.fomc.documents', 'statement',
          'Document 1', '2026-08-01', 1, 1, 'https://example.test/1',
          'body', 'hash-1', '{}'::jsonb
        ), (
          'document-health-2', 'federal_reserve.fomc.documents', 'statement',
          'Document 2', '2026-08-02', 1, 1, 'https://example.test/2',
          'body', 'hash-2', '{}'::jsonb
        )
        """
    )
    conn.execute(
        """
        INSERT INTO macro_document_analysis_jobs(
          analysis_job_id, document_id, document_hash, model_name,
          prompt_version, status, next_due_at_ms, leased_until_ms,
          lease_owner, attempt_count, max_attempts, last_error_code,
          created_at_ms, updated_at_ms
        )
        VALUES (
          'document-job-running', 'document-health-1', 'hash-1', 'model',
          'prompt', 'claimed', 1, %(leased_until_ms)s, 'test-owner',
          1, 3, NULL, 1, %(updated_at_ms)s
        ), (
          'document-job-retry', 'document-health-2', 'hash-2', 'model',
          'prompt', 'retryable', %(next_due_at_ms)s, NULL, NULL,
          1, 3, 'provider_timeout', 1, %(updated_at_ms)s
        )
        """,
        {
            "leased_until_ms": NOW_MS - 1_000,
            "next_due_at_ms": NOW_MS + 2_000,
            "updated_at_ms": NOW_MS - 500,
        },
    )
