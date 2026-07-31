from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

import pytest
from psycopg.errors import RaiseException

import tracefold.macro.thesis_service as thesis_service_module
from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tests.support.fake_macro_provider import TransientRecordingStructuredMacroModel
from tests.test_macro_thesis import (
    CUTOFF_MS,
    SESSION,
    _draft,
    _modules,
    _pack,
    _publication,
    _research_input,
)
from tracefold.integrations.deepagents.macro_thesis_deepagent import (
    MacroThesisDeepAgent,
)
from tracefold.macro import compile_research_input_v1
from tracefold.macro.thesis_repository import MacroPublicationWriteConflict
from tracefold.macro.thesis_service import MacroThesisService
from tracefold.macro.thesis_v2 import (
    evaluate_live_delta_v2,
    evaluate_outcome_replay_v2,
)


class _TestDb:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        with repository_session_for_connection(self.conn) as repos:
            yield repos


def _prepare_claimed_run(repos, *, lease_owner: str = "integration-owner"):
    pack = _pack()
    research_input = _research_input(pack=pack)
    assert repos.macro_thesis.insert_evidence_pack(pack) == 1
    assert repos.macro_thesis.insert_research_input(research_input) == 1
    assert (
        repos.macro_thesis.ensure_run(
            pack=pack,
            due_at_ms=CUTOFF_MS,
            max_attempts=2,
            now_ms=CUTOFF_MS + 100,
        )
        == 1
    )
    assert repos.macro_thesis.bind_research_input(
        session_date=SESSION,
        research_input=research_input,
        now_ms=CUTOFF_MS + 150,
    )
    claimed = repos.macro_thesis.claim_run(
        session_date=SESSION,
        lease_owner=lease_owner,
        lease_ms=60_000,
        now_ms=CUTOFF_MS + 200,
    )
    assert claimed is not None
    assert claimed["research_input_id"] == research_input.input_id
    return pack, research_input


def test_v2_repository_binds_input_publishes_without_reviewer_and_is_idempotent(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    publication = _publication()
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            _prepare_claimed_run(repos)
            assert repos.macro_thesis.publish_v2(
                publication=publication,
                lease_owner="integration-owner",
            )
            assert (
                repos.macro_thesis.publish_v2(
                    publication=publication,
                    lease_owner="integration-owner",
                )
                is False
            )

            live = evaluate_live_delta_v2(
                publication=publication,
                modules=_modules(),
                evaluated_at_ms=CUTOFF_MS + 400,
            )
            assert repos.macro_thesis.insert_live_delta(live) == 1
            assert (
                repos.macro_thesis.insert_live_delta(
                    evaluate_live_delta_v2(
                        publication=publication,
                        modules=_modules(),
                        evaluated_at_ms=CUTOFF_MS + 500,
                    )
                )
                == 0
            )
            changed_modules = deepcopy(_modules())
            changed_modules[0]["latest_fact_at_ms"] = CUTOFF_MS + 600
            changed_live = evaluate_live_delta_v2(
                publication=publication,
                modules=changed_modules,
                evaluated_at_ms=CUTOFF_MS + 600,
            )
            assert changed_live.live_delta_id != live.live_delta_id
            assert repos.macro_thesis.insert_live_delta(changed_live) == 1

            replay = evaluate_outcome_replay_v2(
                publication=publication,
                market_rows=[],
                evaluated_at_ms=CUTOFF_MS + 400,
            )
            assert repos.macro_thesis.insert_outcome_replay(replay) == 1
            assert repos.macro_thesis.insert_outcome_replay(replay) == 0

        with repository_session_for_connection(conn) as repos:
            state = repos.macro_thesis.state(SESSION)
            current = repos.macro_thesis.current_publication_v2(SESSION)
            archive = repos.macro_thesis.archive_publication(SESSION)
            assert state is not None
            assert state["status"] == "published"
            assert state["schema_version"] == "macro_thesis_v2"
            assert state["reviewer_invocation_id"] is None
            assert state["reviewer_draft_hash"] is None
            assert state["research_input_id"] == publication.research_input_id
            assert current is not None
            assert archive is not None
            assert (
                repos.macro_thesis.latest_live_delta(publication.publication_id)["live_delta_id"]
                == changed_live.live_delta_id
            )
            assert repos.macro_thesis.latest_outcome_replay(publication.publication_id)

        with pytest.raises(RaiseException, match="macro_thesis_publications_append_only"):
            conn.execute(
                "DELETE FROM macro_thesis_publications WHERE publication_id = %s",
                (publication.publication_id,),
            )
        conn.rollback()
    finally:
        conn.close()


def test_v2_repository_rejects_different_hash_for_same_session(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    publication = _publication()
    conflict = publication.model_copy(
        update={"mainline": publication.mainline.model_copy(update={"title": "Different immutable content"})}
    )
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            _prepare_claimed_run(repos)
            assert repos.macro_thesis.publish_v2(
                publication=publication,
                lease_owner="integration-owner",
            )
        with (
            pytest.raises(
                MacroPublicationWriteConflict,
                match="macro_thesis_write_identity_conflict",
            ),
            repository_session_for_connection(conn) as repos,
            repos.transaction(),
        ):
            repos.macro_thesis.publish_v2(
                publication=conflict,
                lease_owner="integration-owner",
            )
    finally:
        conn.close()


def test_four_gate_failure_is_persisted_without_reviewer_or_extra_gate(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            _, research_input = _prepare_claimed_run(repos)
            status = repos.macro_thesis.mark_error(
                session_date=SESSION,
                lease_owner="integration-owner",
                error_code="macro_thesis_evidence_closure_invalid",
                error_message="outside-pack evidence",
                retryable=False,
                terminal_status="not_published",
                retry_ms=5_000,
                now_ms=CUTOFF_MS + 300,
                gate_category="evidence_closure",
                candidate_hash="sha256:" + "1" * 64,
            )
            assert status == "not_published"

        with repository_session_for_connection(conn) as repos:
            state = repos.macro_thesis.state(SESSION)
            assert state is not None
            assert state["attempt_count"] == 1
            assert state["status"] == "not_published"
            assert state["last_gate_category"] == "evidence_closure"
            assert state["last_candidate_hash"] == "sha256:" + "1" * 64
            assert state["research_input_id"] == research_input.input_id
            assert state["publication_id"] is None
            assert state["reviewer_invocation_id"] is None
    finally:
        conn.close()


def test_preflight_configuration_error_is_terminal_before_attempt(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            pack = _pack()
            research_input = _research_input(pack=pack)
            repos.macro_thesis.insert_evidence_pack(pack)
            repos.macro_thesis.insert_research_input(research_input)
            repos.macro_thesis.ensure_run(
                pack=pack,
                due_at_ms=CUTOFF_MS,
                max_attempts=2,
                now_ms=CUTOFF_MS + 100,
            )
            repos.macro_thesis.bind_research_input(
                session_date=SESSION,
                research_input=research_input,
                now_ms=CUTOFF_MS + 150,
            )
            assert repos.macro_thesis.mark_preflight_error(
                session_date=SESSION,
                status="config_error",
                error_code="macro_thesis_configuration_error",
                error_message="unsupported_model",
                now_ms=CUTOFF_MS + 200,
            )
        with repository_session_for_connection(conn) as repos:
            state = repos.macro_thesis.state(SESSION)
            assert state is not None
            assert state["status"] == "config_error"
            assert state["attempt_count"] == 0
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


def test_research_input_compilation_failure_is_durable_and_pre_model(
    tmp_path,
    monkeypatch,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        service = MacroThesisService(
            db=_TestDb(conn),
            agent=None,
            clock_ms=lambda: CUTOFF_MS + 100,
        )
        monkeypatch.setattr(service, "_build_pack", lambda **_kwargs: _pack())

        def fail_compilation(_pack):
            raise ValueError("research_input_budget_exceeded")

        monkeypatch.setattr(
            thesis_service_module,
            "compile_research_input_v1",
            fail_compilation,
        )

        first = asyncio.run(service.run_due(now_ms=CUTOFF_MS + 100))
        second = asyncio.run(service.run_due(now_ms=CUTOFF_MS + 200))

        assert first.status == second.status == "failed"
        assert first.model_calls == second.model_calls == 0
        assert first.research_input_id is None
        assert first.error_code == "macro_thesis_input_compilation_error"
        with repository_session_for_connection(conn) as repos:
            state = repos.macro_thesis.state(SESSION)
            assert state is not None
            assert state["attempt_count"] == 0
            assert state["status"] == "failed"
            assert state["last_error_code"] == "macro_thesis_input_compilation_error"
            assert state["research_input_id"] is None
            assert conn.execute("SELECT count(*) AS count FROM macro_research_inputs").fetchone()["count"] == 0
            assert conn.execute("SELECT count(*) AS count FROM macro_thesis_publications").fetchone()["count"] == 0
    finally:
        conn.close()


def test_transient_provider_retry_reuses_input_and_runs_one_model_call_per_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    pack = _pack()
    research_input = compile_research_input_v1(pack)
    candidate = _draft(_research_input(pack=pack)).model_copy(
        update={
            "session_date": research_input.session_date,
            "cutoff_ms": research_input.cutoff_ms,
            "evidence_pack_id": research_input.evidence_pack_id,
            "research_input_id": research_input.input_id,
            "mainline": _draft(_research_input(pack=pack)).mainline.model_copy(
                update={
                    "stance": "no_call",
                    "causal_edges": (),
                    "supporting_evidence_refs": (),
                    "no_call_reason": "The frozen input has no eligible falsifier candidate.",
                }
            ),
            "asset_outlooks": (),
            "condition_uses": (),
        }
    )
    model = TransientRecordingStructuredMacroModel.for_mapping(
        candidate.model_dump(mode="json"),
    )
    agent = MacroThesisDeepAgent(
        model=model,
        model_name=model.model_name,
        clock_ms=lambda: CUTOFF_MS + 2_000,
    )
    try:
        reset_postgres_schema(conn)
        service = MacroThesisService(
            db=_TestDb(conn),
            agent=agent,
            lease_owner="transient-provider-owner",
            clock_ms=lambda: CUTOFF_MS + 2_000,
        )
        monkeypatch.setattr(service, "_build_pack", lambda **_kwargs: pack)

        first = asyncio.run(service.run_due(now_ms=CUTOFF_MS + 100))
        second = asyncio.run(service.run_due(now_ms=CUTOFF_MS + 902_100))

        assert first.status == "retryable"
        assert first.error_code == "macro_thesis_timeouterror"
        assert second.status == "published"
        assert first.research_input_id == second.research_input_id == research_input.input_id
        assert model.invocation_count == 2
        assert model.bound_tool_names == ()
        assert [item["macro_attempt_id"] for item in model.request_metadata_history] == [
            f"macro-thesis:{SESSION.isoformat()}:attempt:1",
            f"macro-thesis:{SESSION.isoformat()}:attempt:2",
        ]
        assert {item["macro_research_input_id"] for item in model.request_metadata_history} == {research_input.input_id}

        with repository_session_for_connection(conn) as repos:
            state = repos.macro_thesis.state(SESSION)
            assert state is not None
            assert state["attempt_count"] == 2
            assert state["status"] == "published"
            assert state["research_input_id"] == research_input.input_id
            assert state["reviewer_invocation_id"] is None
            assert conn.execute("SELECT count(*) AS count FROM macro_research_inputs").fetchone()["count"] == 1
            assert conn.execute("SELECT count(*) AS count FROM macro_thesis_publications").fetchone()["count"] == 1
    finally:
        conn.close()
