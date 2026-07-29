from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from psycopg.errors import RaiseException

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tests.test_macro_thesis import (
    CUTOFF_MS,
    SESSION,
    _Agent,
    _draft,
    _modules,
    _pack,
    _Reviewer,
)
from tracefold.macro.thesis import (
    MacroThesisReviewV1,
    build_publication,
    evaluate_live_delta,
    payload_hash,
    pending_outcome_replay,
)
from tracefold.macro.thesis_service import MacroThesisService
from tracefold.platform.config.settings import MacroThesisWorkerSettings


class _SingleConnectionDB:
    def __init__(self, conn) -> None:
        self._conn = conn

    def worker_session(self, *_args, **_kwargs):
        return repository_session_for_connection(self._conn)


def test_macro_thesis_repository_enforces_bound_review_and_immutable_followups(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    pack = _pack()
    draft = _draft()
    draft_hash = payload_hash(draft.model_dump(mode="json"))
    review = MacroThesisReviewV1(
        draft_hash=draft_hash,
        disposition="pass",
        findings=("证据、反证与资产条件已核对",),
        invocation_id="review-integration-1",
        model_name="openai/gpt-5.4-mini",
        prompt_version="macro-thesis-review-v1",
    )
    publication = build_publication(
        evidence_pack=pack,
        draft=draft,
        review=review,
        research_provenance={
            "invocation_id": "research-integration-1",
            "model_name": "openai/gpt-5.4-mini",
            "prompt_version": "macro-thesis-research-v1",
        },
        published_at_ms=CUTOFF_MS + 300,
    )
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            assert repos.macro_thesis.insert_evidence_pack(pack) == 1
            assert (
                repos.macro_thesis.ensure_run(
                    pack=pack,
                    due_at_ms=CUTOFF_MS,
                    max_attempts=2,
                    now_ms=CUTOFF_MS + 100,
                )
                == 1
            )
            claimed = repos.macro_thesis.claim_run(
                session_date=SESSION,
                lease_owner="integration-owner",
                lease_ms=60_000,
                now_ms=CUTOFF_MS + 200,
            )
            assert claimed is not None
            assert (
                repos.macro_thesis.record_review(
                    session_date=SESSION,
                    review=review,
                    review_sequence=1,
                    created_at_ms=CUTOFF_MS + 250,
                )
                == 1
            )
            assert repos.macro_thesis.publish(
                publication=publication,
                lease_owner="integration-owner",
            )
            assert (
                repos.macro_thesis.insert_live_delta(
                    live_delta := evaluate_live_delta(
                        publication=publication,
                        modules=_modules(),
                        evaluated_at_ms=CUTOFF_MS + 400,
                    )
                )
                == 1
            )
            unchanged_live_delta = evaluate_live_delta(
                publication=publication,
                modules=_modules(),
                evaluated_at_ms=CUTOFF_MS + 500,
            )
            assert unchanged_live_delta.live_delta_id == live_delta.live_delta_id
            assert unchanged_live_delta.input_hash == live_delta.input_hash
            assert repos.macro_thesis.insert_live_delta(unchanged_live_delta) == 0
            post_cutoff_modules = deepcopy(_modules())
            post_cutoff_modules[0]["latest_fact_at_ms"] = CUTOFF_MS + 550
            post_cutoff_live_delta = evaluate_live_delta(
                publication=publication,
                modules=post_cutoff_modules,
                evaluated_at_ms=CUTOFF_MS + 600,
            )
            assert post_cutoff_live_delta.live_delta_id == live_delta.live_delta_id
            assert post_cutoff_live_delta.input_hash != live_delta.input_hash
            assert repos.macro_thesis.insert_live_delta(post_cutoff_live_delta) == 1
            assert (
                repos.macro_thesis.insert_outcome_replay(
                    pending_outcome_replay(
                        publication=publication,
                        evaluated_at_ms=CUTOFF_MS + 400,
                    )
                )
                == 1
            )

        with repository_session_for_connection(conn) as repos:
            state = repos.macro_thesis.state(SESSION)
            assert state is not None
            assert state["status"] == "published"
            assert state["reviewer_invocation_id"] == review.invocation_id
            assert state["reviewer_draft_hash"] == draft_hash
            assert repos.macro_thesis.latest_live_delta(publication.publication_id)
            assert repos.macro_thesis.latest_outcome_replay(publication.publication_id)

        with pytest.raises(RaiseException, match="macro_thesis_publications_append_only"):
            conn.execute(
                "DELETE FROM macro_thesis_publications WHERE publication_id = %s",
                (publication.publication_id,),
            )
        conn.rollback()
    finally:
        conn.close()


def test_reviewer_second_rejection_is_persisted_before_not_published(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    pack = _pack()
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.macro_thesis.insert_evidence_pack(pack)
            repos.macro_thesis.ensure_run(
                pack=pack,
                due_at_ms=CUTOFF_MS,
                max_attempts=3,
                now_ms=CUTOFF_MS + 100,
            )

        service = MacroThesisService(
            db=_SingleConnectionDB(conn),
            settings=MacroThesisWorkerSettings(enabled=True, lease_ms=60_000),
            agent=_Agent(),
            reviewer=_Reviewer(["revise", "revise"]),
            lease_owner="review-rejection-integration",
            clock_ms=lambda: CUTOFF_MS + 2_000,
        )
        view = asyncio.run(service.run_due(now_ms=CUTOFF_MS + 2_000))

        reviews = conn.execute(
            """
            SELECT review_sequence, disposition, draft_hash
            FROM macro_thesis_reviews
            WHERE session_date = %s
            ORDER BY review_sequence
            """,
            (SESSION,),
        ).fetchall()
        state = conn.execute(
            """
            SELECT status, attempt_count, publication_id, last_error_code
            FROM macro_thesis_runs
            WHERE session_date = %s
            """,
            (SESSION,),
        ).fetchone()

        assert view.status == "not_published"
        assert state == {
            "status": "not_published",
            "attempt_count": 1,
            "publication_id": None,
            "last_error_code": "macro_thesis_reviewer_block",
        }
        assert [(row["review_sequence"], row["disposition"]) for row in reviews] == [
            (1, "revise"),
            (2, "revise"),
        ]
        assert all(str(row["draft_hash"]).startswith("sha256:") for row in reviews)
    finally:
        conn.close()


def test_macro_thesis_configuration_error_is_terminal_before_retry(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    pack = _pack()
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.macro_thesis.insert_evidence_pack(pack)
            repos.macro_thesis.ensure_run(
                pack=pack,
                due_at_ms=CUTOFF_MS,
                max_attempts=3,
                now_ms=CUTOFF_MS + 100,
            )
            assert repos.macro_thesis.mark_configuration_error_before_attempt(
                session_date=SESSION,
                error_code="macro_thesis_configuration_error",
                error_message="unsupported_model",
                now_ms=CUTOFF_MS + 200,
            )
        with repository_session_for_connection(conn) as repos:
            state = repos.macro_thesis.state(SESSION)
            assert state is not None
            assert state["status"] == "config_error"
            assert state["attempt_count"] == 0
        with repository_session_for_connection(conn) as repos, repos.transaction():
            assert (
                repos.macro_thesis.claim_run(
                    session_date=SESSION,
                    lease_owner="other-owner",
                    lease_ms=60_000,
                    now_ms=CUTOFF_MS + 10_000,
                )
                is None
            )
    finally:
        conn.close()
