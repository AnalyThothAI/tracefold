"""Outstanding and recent failure reads stay bounded after jobs consolidation."""

from contextlib import closing

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_pg import EVENT, STAMP, seed_event
from tracefold.news.storage.query_specs import news_query_specs
from tracefold.news.storage.semantic_work import SemanticWorkStorage
from tracefold.platform.postgres.audit import PostgresQueryAudit, QueryAuditCatalog

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_semantic_reads_skip_completed_history_and_count_completed_failures():
    seed_event()
    now_ms = STAMP + 60_000
    with closing(connect_postgres_test()) as conn, conn.transaction():
        conn.execute(
            """INSERT INTO news_jobs(job_kind,subject_id,state,detail,next_attempt_at_ms,created_at_ms,updated_at_ms)
               SELECT 'semantic','completed:'||g,'done',
                      detail||jsonb_build_object('done_revision',1,'last_outcome','failed'),%s,%s,%s
                 FROM news_jobs CROSS JOIN generate_series(1,20000) g
                WHERE job_kind='semantic' AND subject_id=%s""",
            (STAMP, STAMP, STAMP - 86_400_000, EVENT),
        )
        for subject, state, done, outcome, due, error in (
            ("deferred", "pending", None, "deferred", now_ms + 60_000, None),
            ("failed", "failed", None, "failed", STAMP, "provider_failed"),
            ("completed-failed", "done", 1, "failed", STAMP, "completed_error"),
        ):
            conn.execute(
                """INSERT INTO news_jobs(job_kind,subject_id,state,detail,next_attempt_at_ms,
                                         last_error_code,created_at_ms,updated_at_ms)
                   SELECT 'semantic',%s,%s,detail||jsonb_build_object('done_revision',%s::integer,
                         'last_outcome',%s::text),%s,%s,%s,%s
                     FROM news_jobs WHERE job_kind='semantic' AND subject_id=%s""",
                (subject, state, done, outcome, due, error, STAMP, STAMP, EVENT),
            )
        conn.execute("ANALYZE news_jobs")
        storage = SemanticWorkStorage(conn)
        status = storage.semantic_status(now_ms=now_ms)
        assert status["semantic_pending"] == 1
        assert status["semantic_deferred"] == 1
        assert status["semantic_failed_exhausted"] == 1
        assert status["semantic_failed_24h"] == 2
        assert status["semantic_failed_by_code_24h"] == {"completed_error": 1, "provider_failed": 1}
        assert storage.semantic_wake_state() == {"pending": 2, "expired": 1, "oldest_pending_at_ms": STAMP}
        catalog = QueryAuditCatalog(
            queries=tuple(q for q in news_query_specs(now_ms=now_ms) if q.name.startswith("news_semantic_")),
            query_routes={},
            no_sql_routes=frozenset(),
        )
        result = PostgresQueryAudit(conn, catalog=catalog).run(analyze=True)
        assert result["ok"], result
        for query in result["queries"]:
            assert query["metrics"]["scanned_rows"] < 50, query


def test_failed_backlog_does_not_hide_pending_jobs_or_truncate_counts():
    seed_event()
    with closing(connect_postgres_test()) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO news_jobs(job_kind,subject_id,state,detail,next_attempt_at_ms,created_at_ms,updated_at_ms) "
            "SELECT 'semantic','failed:'||g,'failed',detail||jsonb_build_object('last_outcome','failed'),0,0,0 "
            "FROM news_jobs CROSS JOIN generate_series(1,1500) g WHERE job_kind='semantic' AND subject_id=%s",
            (EVENT,),
        )
        storage = SemanticWorkStorage(conn)
        assert storage.semantic_wake_state() == {"pending": 1, "expired": 1500, "oldest_pending_at_ms": STAMP}
        status = storage.semantic_status(now_ms=STAMP + 1)
        assert status["semantic_pending"] == 1 and status["semantic_failed_exhausted"] == 1500
