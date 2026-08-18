from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from psycopg.errors import RaiseException

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.macro import (
    DocumentFact,
    FedDocumentAnalysisAgent,
    MacroProjectionCandidate,
    rebuild_all_macro_modules_for_maintenance,
)
from tracefold.macro.fed_analysis import (
    FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
    FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
    FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
    FedAnalysisEvidence,
    FedDocumentAnalysisDraft,
    MacroDocumentAnalysisService,
)
from tracefold.macro.projection import MacroProjectionService
from tracefold.platform.resource import ResourceAdmissionTimeout


class _TestDb:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        with repository_session_for_connection(self.conn) as repos:
            yield repos

    async def run_business(self, _name: str, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("operation_timeout_seconds", None)
        return function(*args, **kwargs)


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


class _InvalidEvidenceAgent(_Agent):
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
            rationale="This intentionally invalid excerpt must be rejected.",
            evidence=[
                FedAnalysisEvidence(
                    excerpt="This excerpt is absent from the official body",
                    claim="invalid test evidence",
                )
            ],
        )


class _HangingModel:
    def with_structured_output(self, _schema: object, **_kwargs: object) -> _HangingModel:
        return self

    async def ainvoke(self, _messages: list[object]) -> None:
        await asyncio.Event().wait()


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        self.value += 1_000
        return self.value


class _SequenceClock:
    def __init__(self, *values: int) -> None:
        self.values = iter(values)

    def __call__(self) -> int:
        return next(self.values)


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


def test_document_analysis_model_timeout_uses_durable_retry(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        service = MacroDocumentAnalysisService(
            db=_TestDb(conn),
            agent=FedDocumentAnalysisAgent(
                model=_HangingModel(),
                model_name="test-fed-analysis-model",
                completion_timeout_seconds=0.01,
            ),
            clock_ms=lambda: 3_603_000,
        )

        result = asyncio.run(service.run_once(now_ms=3_603_000))
        row = conn.execute(
            """
            SELECT status, attempt_count, next_due_at_ms, lease_owner,
                   leased_until_ms, last_error_code
              FROM macro_document_analysis_jobs
             WHERE document_id = 'macrodoc_admission_boundary'
            """
        ).fetchone()
    finally:
        conn.close()

    assert result == {
        "status": "failed",
        "document_id": "macrodoc_admission_boundary",
        "error_code": "macro_document_model_expected_timeouterror",
        "jobs_written": 1,
    }
    assert row == {
        "status": "retryable",
        "attempt_count": 1,
        "next_due_at_ms": 3_903_000,
        "lease_owner": None,
        "leased_until_ms": None,
        "last_error_code": "macro_document_model_expected_timeouterror",
    }


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
        analysis_count = conn.execute("SELECT COUNT(*)::int AS count FROM macro_document_analyses").fetchone()["count"]
        job = conn.execute(
            """
            SELECT status, attempt_count, lease_owner
              FROM macro_document_analysis_jobs
             WHERE document_id = 'macrodoc_admission_boundary'
            """
        ).fetchone()
        with repository_session_for_connection(conn) as repos:
            [dataset_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
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
    assert dataset_state["acquisition_status"] == "pending"
    assert dataset_state["source_frontier_ms"] == 0


def test_published_document_analysis_makes_rates_projection_immediately_claimable(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        db = _TestDb(conn)
        service = MacroDocumentAnalysisService(
            db=db,
            agent=_Agent(),
            clock_ms=lambda: 3_604_000,
        )

        result = asyncio.run(service.run_once(now_ms=3_603_000))
        with repository_session_for_connection(conn) as repos:
            [dataset_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
        runtime_id = str(uuid4())
        candidate = MacroProjectionCandidate(
            db=db,
            cpu=object(),
            runtime_id=runtime_id,
        )
        shard = asyncio.run(candidate.peek(now_ms=3_604_000))
        claim = MacroProjectionService(db=db).claim_module(
            module_id="rates_fed",
            runtime_id=runtime_id,
            now_ms=3_604_000,
        )
    finally:
        conn.close()

    assert result["status"] == "published"
    assert dataset_state["acquisition_status"] == "current"
    assert dataset_state["material_fingerprint"].startswith("sha256:")
    assert dataset_state["material_fingerprint"] != "missing"
    assert dataset_state["source_frontier_ms"] == 3_604_000
    assert shard is not None
    assert shard.domain == "macro"
    assert shard.shard_key == "rates_fed"
    assert shard.deadline_at_ms == 3_663_000
    assert claim is not None


def test_document_analysis_publication_rolls_back_if_frontier_update_fails(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        service = MacroDocumentAnalysisService(
            db=_TestDb(conn),
            agent=_Agent(),
            clock_ms=lambda: 3_604_000,
        )
        assert asyncio.run(service.reconcile(now_ms=3_603_000)) == 1
        with repository_session_for_connection(conn) as repos:
            [state_before] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
        frontier_before = conn.execute(
            """
            SELECT status, input_fingerprint, updated_at_ms
              FROM macro_module_frontiers
             WHERE module_id = 'rates_fed'
            """
        ).fetchone()
        conn.execute(
            """
            CREATE FUNCTION reject_analysis_frontier_update()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
              RAISE EXCEPTION 'test_reject_analysis_frontier_update';
            END;
            $$
            """
        )
        conn.execute(
            """
            CREATE TRIGGER reject_analysis_frontier_update
            BEFORE UPDATE ON macro_module_frontiers
            FOR EACH ROW
            EXECUTE FUNCTION reject_analysis_frontier_update()
            """
        )
        conn.commit()

        with pytest.raises(RaiseException, match="test_reject_analysis_frontier_update"):
            asyncio.run(service.run_once(now_ms=3_603_000))

        with repository_session_for_connection(conn) as repos:
            [state_after] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
        frontier_after = conn.execute(
            """
            SELECT status, input_fingerprint, updated_at_ms
              FROM macro_module_frontiers
             WHERE module_id = 'rates_fed'
            """
        ).fetchone()
        analysis_count = conn.execute("SELECT count(*)::int AS count FROM macro_document_analyses").fetchone()["count"]
        job = conn.execute(
            """
            SELECT status, lease_owner
              FROM macro_document_analysis_jobs
             WHERE document_id = 'macrodoc_admission_boundary'
            """
        ).fetchone()
    finally:
        conn.close()

    assert analysis_count == 0
    assert job == {"status": "claimed", "lease_owner": "macro_document_analysis"}
    assert state_after == state_before
    assert frontier_after == frontier_before


def test_document_analysis_projection_input_is_stable_across_maintenance_rebuild(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        db = _TestDb(conn)
        service = MacroDocumentAnalysisService(
            db=db,
            agent=_Agent(),
            clock_ms=lambda: 3_604_000,
        )

        assert asyncio.run(service.reconcile(now_ms=3_603_000)) == 1
        with repository_session_for_connection(conn) as repos:
            [live_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )

        rebuild = rebuild_all_macro_modules_for_maintenance(
            db=db,
            now_ms=3_603_001,
        )
        with repository_session_for_connection(conn) as repos:
            [rebuilt_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
    finally:
        conn.close()

    assert rebuild["modules_computed"] == 6
    assert live_state["acquisition_status"] == "pending"
    assert rebuilt_state == live_state


def test_completed_document_analysis_input_is_stable_across_maintenance_rebuild(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        db = _TestDb(conn)
        service = MacroDocumentAnalysisService(
            db=db,
            agent=_Agent(),
            clock_ms=lambda: 3_604_000,
        )

        result = asyncio.run(service.run_once(now_ms=3_603_000))
        with repository_session_for_connection(conn) as repos:
            [live_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )

        rebuild_all_macro_modules_for_maintenance(
            db=db,
            now_ms=3_604_001,
        )
        with repository_session_for_connection(conn) as repos:
            [rebuilt_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
    finally:
        conn.close()

    assert result["status"] == "published"
    assert live_state["acquisition_status"] == "current"
    assert rebuilt_state == live_state


def test_analysis_change_token_advances_for_same_clock_and_smaller_identity(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        common = {
            "document_id": "macrodoc_admission_boundary",
            "document_hash": "sha256:document",
            "official_id": None,
            "policy_relevance": "policy_signal",
            "stance": "neutral",
            "confidence": 0.5,
            "analysis": {"fixture": "append-only-change-token"},
            "prompt_version": "projection-token-test-v1",
            "reviewer_disposition": "pass",
            "created_at_ms": 5_000,
        }
        with repository_session_for_connection(conn) as repos, repos.transaction():
            assert (
                repos.macro.insert_document_analysis(
                    analysis_id="zzzz-analysis",
                    model_name="projection-token-model-a",
                    payload_hash="sha256:projection-token-a",
                    **common,
                )
                == 1
            )
            assert repos.macro.refresh_document_analysis_projection_state(updated_at_ms=5_000)
        with repository_session_for_connection(conn) as repos:
            [first_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )

        with repository_session_for_connection(conn) as repos, repos.transaction():
            assert (
                repos.macro.insert_document_analysis(
                    analysis_id="aaaa-analysis",
                    model_name="projection-token-model-b",
                    payload_hash="sha256:projection-token-b",
                    **common,
                )
                == 1
            )
            assert repos.macro.refresh_document_analysis_projection_state(updated_at_ms=6_000)
        with repository_session_for_connection(conn) as repos:
            [second_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
    finally:
        conn.close()

    assert first_state["source_frontier_ms"] == second_state["source_frontier_ms"] == 5_000
    assert first_state["material_fingerprint"] != second_state["material_fingerprint"]
    assert second_state["updated_at_ms"] == 6_000


def test_terminal_document_analysis_failure_rebuilds_the_same_rates_input(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        _insert_analysis_document(conn)
        db = _TestDb(conn)
        service = MacroDocumentAnalysisService(
            db=db,
            agent=_InvalidEvidenceAgent(),
            clock_ms=_SequenceClock(3_604_000, 3_905_000, 4_206_000),
        )

        first = asyncio.run(service.run_once(now_ms=3_603_000))
        with repository_session_for_connection(conn) as repos:
            [first_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
        second = asyncio.run(service.run_once(now_ms=3_904_000))
        with repository_session_for_connection(conn) as repos:
            [second_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
        third = asyncio.run(service.run_once(now_ms=4_205_000))
        with repository_session_for_connection(conn) as repos:
            [failed_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
        candidate = MacroProjectionCandidate(
            db=db,
            cpu=object(),
            runtime_id=str(uuid4()),
        )
        shard = asyncio.run(candidate.peek(now_ms=4_206_000))

        rebuild_all_macro_modules_for_maintenance(
            db=db,
            now_ms=4_206_001,
        )
        with repository_session_for_connection(conn) as repos:
            [rebuilt_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
            persisted = repos.macro.module_current("rates_fed")
    finally:
        conn.close()

    assert first["status"] == second["status"] == third["status"] == "failed"
    assert first_state == second_state
    assert first_state["acquisition_status"] == "pending"
    assert failed_state["acquisition_status"] == "failed"
    assert failed_state["material_fingerprint"] != first_state["material_fingerprint"]
    assert shard is not None
    assert shard.shard_key == "rates_fed"
    assert rebuilt_state == failed_state
    assert persisted is not None
    analysis_state = next(
        state
        for state in persisted["payload_json"]["evidence"]["dataset_states"]
        if state["dataset_id"] == "federal_reserve.document.analysis"
    )
    assert analysis_state["current_health"] == "unavailable"
    assert analysis_state["current_reason"]["code"] == "document_analysis_jobs_failed"


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
            [analysis_dataset_state] = repos.macro.dataset_projection_states(
                dataset_ids=("federal_reserve.document.analysis",),
            )
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
        assert analysis_dataset_state["acquisition_status"] == "current"
        assert analysis_dataset_state["updated_at_ms"] == 3_604_000
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
