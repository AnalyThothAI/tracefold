from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any

import pytest
from psycopg.errors import RaiseException

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.macro import DocumentFact
from tracefold.macro.fed_analysis import (
    FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
    FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
    FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
    FedAnalysisEvidence,
    FedDocumentAnalysisDraft,
    MacroDocumentAnalysisService,
)
from tracefold.platform.resource import ResourceAdmissionTimeout


class _TestDb:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        with repository_session_for_connection(self.conn) as repos:
            yield repos


class _Agent:
    model_name = "test-fed-analysis-model"

    async def analyze(
        self,
        *,
        document: Any,
        roster_context: Any,
        prior_analysis: Any,
        on_model_submitted: Any,
    ) -> FedDocumentAnalysisDraft:
        assert roster_context is None
        assert prior_analysis is None
        on_model_submitted()
        return FedDocumentAnalysisDraft(
            policy_relevance="policy_signal",
            stance="hawkish",
            confidence=0.8,
            change_from_prior="no_prior",
            rationale="通胀仍高，政策保持限制性。",
            evidence=[
                FedAnalysisEvidence(
                    excerpt="Inflation remains too high",
                    claim="通胀判断偏鹰",
                )
            ],
        )


class _ReleaseClaimAgent(_Agent):
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def analyze(
        self,
        *,
        document: Any,
        roster_context: Any,
        prior_analysis: Any,
        on_model_submitted: Any,
    ) -> FedDocumentAnalysisDraft:
        row = self.conn.execute(
            """
            SELECT analysis_job_id, lease_owner, attempt_count
              FROM macro_document_analysis_jobs
             WHERE status = 'claimed'
            """
        ).fetchone()
        assert row is not None
        with repository_session_for_connection(self.conn) as repos, repos.transaction():
            assert repos.macro.release_document_analysis_claim(
                analysis_job_id=str(row["analysis_job_id"]),
                lease_owner=str(row["lease_owner"]),
                claimed_attempt_count=int(row["attempt_count"]),
            )
        return await super().analyze(
            document=document,
            roster_context=roster_context,
            prior_analysis=prior_analysis,
            on_model_submitted=on_model_submitted,
        )


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        self.value += 1_000
        return self.value


class _FailOnBusinessCall:
    def __init__(self, call_number: int) -> None:
        self.call_number = call_number
        self.calls = 0

    async def run_business(self, _name: str, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("operation_timeout_seconds", None)
        self.calls += 1
        if self.calls == self.call_number:
            raise ResourceAdmissionTimeout("test_business_admission_timeout")
        return function(*args, **kwargs)


def _insert_analysis_document(conn: Any) -> None:
    content = "Inflation remains too high and policy must stay restrictive. " * 12
    content_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    with repository_session_for_connection(conn) as repos, repos.transaction():
        repos.macro.insert_document(
            DocumentFact(
                document_id="macrodoc_admission_boundary",
                dataset_id="federal_reserve.fomc.documents",
                document_type="statement",
                title="Federal Reserve issues FOMC statement",
                effective_date=date(2026, 7, 29),
                published_at_ms=2_000,
                received_at_ms=2_500,
                source_url="https://www.federalreserve.gov/admission-boundary.htm",
                content_text=content,
                metadata={"content_hash": content_hash},
            )
        )


def test_document_analysis_pre_model_admission_releases_exact_claim(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        service = MacroDocumentAnalysisService(
            db=_TestDb(conn),
            agent=_Agent(),
            clock_ms=lambda: 3_603_000,
            database=_FailOnBusinessCall(2),
        )

        assert asyncio.run(service.run_once(now_ms=3_603_000)) == {
            "status": "idle",
            "jobs_written": 1,
        }
        row = conn.execute(
            """
            SELECT status, attempt_count, lease_owner, leased_until_ms
              FROM macro_document_analysis_jobs
             WHERE document_id = 'macrodoc_admission_boundary'
            """
        ).fetchone()
        assert row == {
            "status": "pending",
            "attempt_count": 0,
            "lease_owner": None,
            "leased_until_ms": None,
        }
    finally:
        conn.close()


def test_document_analysis_post_model_publication_admission_is_fatal(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        service = MacroDocumentAnalysisService(
            db=_TestDb(conn),
            agent=_Agent(),
            clock_ms=lambda: 3_603_000,
            database=_FailOnBusinessCall(3),
        )

        with pytest.raises(ResourceAdmissionTimeout, match="test_business_admission_timeout"):
            asyncio.run(service.run_once(now_ms=3_603_000))
        row = conn.execute(
            """
            SELECT status, attempt_count, lease_owner
              FROM macro_document_analysis_jobs
             WHERE document_id = 'macrodoc_admission_boundary'
            """
        ).fetchone()
        assert row["status"] == "claimed"
        assert row["attempt_count"] == 1
        assert row["lease_owner"] == "macro_document_analysis"
    finally:
        conn.close()


def test_document_analysis_lost_claim_does_not_publish(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        service = MacroDocumentAnalysisService(
            db=_TestDb(conn),
            agent=_ReleaseClaimAgent(conn),
            clock_ms=lambda: 3_603_000,
        )

        result = asyncio.run(service.run_once(now_ms=3_603_000))
        analysis_count = conn.execute(
            "SELECT COUNT(*)::int AS count FROM macro_document_analyses"
        ).fetchone()["count"]
        job = conn.execute(
            """
            SELECT status, attempt_count, lease_owner
              FROM macro_document_analysis_jobs
             WHERE document_id = 'macrodoc_admission_boundary'
            """
        ).fetchone()
    finally:
        conn.close()

    assert result == {
        "status": "idle",
        "document_id": "macrodoc_admission_boundary",
        "rows_written": 0,
        "jobs_written": 1,
    }
    assert analysis_count == 0
    assert job == {"status": "pending", "attempt_count": 0, "lease_owner": None}


def test_document_analysis_is_immutable_idempotent_and_source_cutoff_bound(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        content = (
            "Inflation remains too high and policy must stay restrictive. "
            "The Committee will assess incoming labor-market evidence."
        )
        content_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        document = DocumentFact(
            document_id="macrodoc_statement_20260729",
            dataset_id="federal_reserve.fomc.documents",
            document_type="statement",
            title="Federal Reserve issues FOMC statement",
            effective_date=date(2026, 7, 29),
            published_at_ms=2_000,
            received_at_ms=2_500,
            source_url=("https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm"),
            content_text=content,
            metadata={
                "content_hash": content_hash,
                "body_source": "official_page",
                "fomc_role_records": [],
            },
        )
        with repository_session_for_connection(conn) as repos, repos.transaction():
            assert repos.macro.insert_document(document) == 1
        conn.execute(
            """
            INSERT INTO macro_document_analysis_jobs(
              analysis_job_id, document_id, document_hash, model_name,
              prompt_version, status, next_due_at_ms, attempt_count,
              max_attempts, created_at_ms, updated_at_ms
            )
            VALUES (
              'macroda_000_retired', %s, %s, 'retired-pending-model',
              %s, 'pending', 0, 0, 1, 0, 0
            )
            """,
            (
                document.document_id,
                content_hash,
                FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
            ),
        )

        service = MacroDocumentAnalysisService(
            db=_TestDb(conn),
            agent=_Agent(),
            clock_ms=_Clock(3_603_000),
        )
        first = asyncio.run(service.run_once(now_ms=3_603_000))
        second = asyncio.run(service.run_once(now_ms=3_605_000))

        with repository_session_for_connection(conn) as repos:
            analyses_before_creation = repos.macro.document_analysis_history(received_before_ms=3_603_500)
            analyses = repos.macro.document_analysis_history(received_before_ms=3_605_000)
            projection_documents = repos.macro.document_projection_history(
                dataset_ids=("federal_reserve.fomc.documents",),
            )
            projection_analyses = repos.macro.document_analysis_projection_history(
                document_ids=(document.document_id,),
            )
            jobs = repos.macro.document_analysis_job_state(received_before_ms=2_500)
        assert analyses_before_creation == []
        stored = analyses[0]
        assert first["status"] == "published"
        assert first["rows_written"] == 1
        assert second == {"status": "idle", "jobs_written": 0}
        assert jobs == {"total": 1, "open": 0, "failed": 0, "completed": 1}
        assert stored["document_hash"] == content_hash
        assert stored["prompt_version"] == FED_DOCUMENT_ANALYSIS_PROMPT_VERSION
        assert stored["analysis_json"]["evidence"][0]["excerpt"] == ("Inflation remains too high")
        assert len(projection_documents) == 1
        assert "content_text" not in projection_documents[0]
        assert projection_documents[0]["semantic_sample_count"] == 1
        assert [row["analysis_id"] for row in projection_analyses] == [stored["analysis_id"]]
        retired_job = conn.execute(
            """
            SELECT status, attempt_count
            FROM macro_document_analysis_jobs
            WHERE analysis_job_id = 'macroda_000_retired'
            """
        ).fetchone()
        assert retired_job == {"status": "pending", "attempt_count": 0}

        conn.execute(
            """
            INSERT INTO macro_document_analysis_jobs(
              analysis_job_id, document_id, document_hash, model_name,
              prompt_version, status, next_due_at_ms, attempt_count,
              max_attempts, created_at_ms, updated_at_ms, last_error_code
            )
            VALUES (
              'macroda_retired_model', %s, %s, 'retired-model',
              %s, 'failed', 4_000, 1, 1, 3_000, 4_000,
              'unsupported_model'
            )
            """,
            (
                document.document_id,
                content_hash,
                FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
            ),
        )
        with repository_session_for_connection(conn) as repos:
            resolved_state = repos.macro.document_analysis_job_state(received_before_ms=2_500)
        assert resolved_state == {
            "total": 1,
            "open": 0,
            "failed": 0,
            "completed": 1,
        }

        with pytest.raises(RaiseException, match="macro_document_analyses_append_only"):
            conn.execute(
                """
                UPDATE macro_document_analyses
                SET stance = 'dovish'
                WHERE analysis_id = %s
                """,
                (stored["analysis_id"],),
            )
        conn.rollback()
    finally:
        conn.close()


def test_document_analysis_admission_does_not_queue_old_raw_history(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    now_ms = int(datetime(2026, 7, 27, tzinfo=UTC).timestamp() * 1_000)
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            for document_id, effective_date in (
                ("macrodoc_old_minutes", date(2024, 1, 31)),
                ("macrodoc_current_minutes", date(2026, 6, 17)),
            ):
                content = f"Official minutes body for {effective_date}."
                content_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
                repos.macro.insert_document(
                    DocumentFact(
                        document_id=document_id,
                        dataset_id="federal_reserve.fomc.documents",
                        document_type="minutes",
                        title="Minutes",
                        effective_date=effective_date,
                        published_at_ms=now_ms,
                        received_at_ms=now_ms,
                        source_url=f"https://www.federalreserve.gov/{document_id}.htm",
                        content_text=content,
                        metadata={"content_hash": content_hash},
                    )
                )
            written = repos.macro.ensure_document_analysis_jobs(
                model_name="test-fed-analysis-model",
                prompt_version=FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
                max_attempts=3,
                now_ms=now_ms,
                fomc_lookback_days=FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
                speech_lookback_days=FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
            )
        queued = conn.execute("SELECT document_id FROM macro_document_analysis_jobs ORDER BY document_id").fetchall()
    finally:
        conn.close()

    assert written == 1
    assert [row["document_id"] for row in queued] == ["macrodoc_current_minutes"]


def test_identical_source_bodies_produce_distinct_document_bound_analyses(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        content = "Inflation remains too high and policy must stay restrictive."
        content_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        with repository_session_for_connection(conn) as repos, repos.transaction():
            for document_id in ("macrodoc_statement_a", "macrodoc_statement_b"):
                repos.macro.insert_document(
                    DocumentFact(
                        document_id=document_id,
                        dataset_id="federal_reserve.fomc.documents",
                        document_type="statement",
                        title="Federal Reserve issues FOMC statement",
                        effective_date=date(2026, 7, 29),
                        published_at_ms=2_000,
                        received_at_ms=2_500,
                        source_url=f"https://www.federalreserve.gov/{document_id}.htm",
                        content_text=content,
                        metadata={"content_hash": content_hash},
                    )
                )
        service = MacroDocumentAnalysisService(
            db=_TestDb(conn),
            agent=_Agent(),
            clock_ms=_Clock(3_603_000),
        )

        first = asyncio.run(service.run_once(now_ms=3_603_000))
        second = asyncio.run(service.run_once(now_ms=3_604_000))

        rows = conn.execute(
            """
            SELECT document_id, payload_hash
            FROM macro_document_analyses
            ORDER BY document_id
            """
        ).fetchall()
        assert first["rows_written"] == 1
        assert second["rows_written"] == 1
        assert [row["document_id"] for row in rows] == [
            "macrodoc_statement_a",
            "macrodoc_statement_b",
        ]
        assert len({row["payload_hash"] for row in rows}) == 2
    finally:
        conn.close()
