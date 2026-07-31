from __future__ import annotations

from types import SimpleNamespace

from psycopg.types.json import Jsonb

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.app.cli.commands.queue_ops import handle_queue_resolve
from tracefold.platform.postgres.queue_terminal import terminalize_source_row


def test_operator_retry_requeues_all_native_model_terminal_states() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        terminal_ids = _insert_native_terminal_states(conn)
        conn.commit()

        with repository_session_for_connection(conn) as repos:
            for terminal_id in terminal_ids:
                code, payload = handle_queue_resolve(
                    SimpleNamespace(
                        terminal_id=terminal_id,
                        action="retry",
                        reason="operator verified provider recovery",
                    ),
                    repos,
                    now_ms=9_000,
                )
                assert code == 0
                assert payload["ok"] is True
                assert payload["data"]["transition"]["requeued"] == 1

        brief = conn.execute(
            "SELECT status, attempt_count, next_due_at_ms FROM news_brief_runs WHERE fingerprint = 'brief-fp'"
        ).fetchone()
        thesis = conn.execute(
            "SELECT status, attempt_count, due_at_ms FROM macro_thesis_runs WHERE session_date = '2026-07-30'"
        ).fetchone()
        document = conn.execute(
            """
            SELECT status, attempt_count, next_due_at_ms
              FROM macro_document_analysis_jobs
             WHERE analysis_job_id = 'document-job-1'
            """
        ).fetchone()
        resolved = conn.execute(
            """
            SELECT owner_key, operator_action, operator_reason
              FROM queue_terminal_events
             ORDER BY owner_key
            """
        ).fetchall()
        conn.commit()

        assert brief == {"status": "retryable", "attempt_count": 0, "next_due_at_ms": 9_000}
        assert thesis == {"status": "retryable", "attempt_count": 0, "due_at_ms": 9_000}
        assert document == {"status": "retryable", "attempt_count": 0, "next_due_at_ms": 9_000}
        assert [row["owner_key"] for row in resolved] == [
            "macro_document_analysis",
            "macro_thesis",
            "news_brief",
        ]
        assert {row["operator_action"] for row in resolved} == {"retry"}
        assert {row["operator_reason"] for row in resolved} == {"operator verified provider recovery"}
    finally:
        conn.close()


def _insert_native_terminal_states(conn) -> list[str]:
    conn.execute(
        """
        INSERT INTO news_brief_runs(
          run_id, fingerprint, status, attempt_count,
          candidate_story_count, candidate_source_count,
          last_error, completed_at_ms, created_at_ms, updated_at_ms
        )
        VALUES ('brief-run-1', 'brief-fp', 'failed', 3, 3, 2,
                'provider exhausted', 1_000, 100, 1_000)
        """
    )
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
    conn.execute(
        """
        INSERT INTO macro_thesis_runs(
          session_date, cutoff_ms, evidence_pack_id, evidence_pack_hash,
          status, attempt_count, max_attempts, due_at_ms, created_at_ms, updated_at_ms
        )
        VALUES ('2026-07-30', 100, 'pack-1', 'pack-hash',
                'pending', 0, 3, 100, 100, 100)
        """
    )
    conn.execute(
        """
        UPDATE macro_thesis_runs
           SET status = 'failed', last_error_code = 'provider_exhausted', updated_at_ms = 1_000
         WHERE session_date = '2026-07-30'
        """
    )
    conn.execute(
        """
        INSERT INTO macro_documents(
          document_id, dataset_id, document_type, title, effective_date,
          published_at_ms, received_at_ms, source_url, content_text,
          fact_hash, metadata_json
        )
        VALUES ('document-1', 'fed.documents', 'statement', 'Document 1',
                '2026-07-30', 100, 100, 'https://example.test/document-1',
                'body', 'document-fact-hash', %s)
        """,
        (Jsonb({}),),
    )
    conn.execute(
        """
        INSERT INTO macro_document_analysis_jobs(
          analysis_job_id, document_id, document_hash, model_name,
          prompt_version, status, next_due_at_ms, attempt_count,
          max_attempts, last_error_code, created_at_ms, updated_at_ms
        )
        VALUES ('document-job-1', 'document-1', 'document-hash', 'model',
                'prompt', 'failed', 1_000, 3, 3, 'provider_exhausted', 100, 1_000)
        """
    )

    rows = (
        (
            "news_brief",
            "news_brief_runs",
            "brief-fp",
        ),
        (
            "macro_thesis",
            "macro_thesis_runs",
            "2026-07-30",
        ),
        (
            "macro_document_analysis",
            "macro_document_analysis_jobs",
            "document-job-1",
        ),
    )
    terminal_ids: list[str] = []
    for owner_key, source_table, target_key in rows:
        terminal = terminalize_source_row(
            conn,
            owner_key=owner_key,
            source_table=source_table,
            target_key=target_key,
            source_row={
                "native_target_key": target_key,
                "attempt_count": 3,
                "updated_at_ms": 1_000,
            },
            final_status="failed",
            final_reason="provider_exhausted",
            now_ms=1_000,
            attempt_count=3,
        )
        terminal_ids.append(str(terminal["terminal_id"]))
    return terminal_ids
