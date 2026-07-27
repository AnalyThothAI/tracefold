from __future__ import annotations

import asyncio
from datetime import date

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.errors import RaiseException

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
)
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn


def test_one_session_has_one_immutable_publication_and_replay_writes_zero(
    tmp_path,
) -> None:
    conn = connect_postgres_test(
        tmp_path / "postgres_test_db",
        read_only=False,
    )
    try:
        migrate(conn)
        session_date = date(2026, 7, 23)
        with repository_session_for_connection(conn) as repos:
            with repos.transaction():
                repos.macro.insert_evidence_pack(
                    evidence_pack_id="mep_integration",
                    session_date=session_date,
                    judgment_cutoff_ms=100,
                    latest_fact_at_ms=100,
                    schema_version="macro_evidence_pack_v1",
                    compiler_version="integration-test",
                    payload={"schema_version": "macro_evidence_pack_v1", "modules": {}},
                    payload_hash="sha256:evidence-pack",
                    created_at_ms=100,
                )
                inserted = repos.macro_research.ensure_run(
                    session_date=session_date,
                    market_cutoff_ms=100,
                    evidence_pack_id="mep_integration",
                    sealed_at_ms=110,
                    max_attempts=3,
                    due_at_ms=110,
                    now_ms=110,
                )
                claimed = repos.macro_research.claim_run(
                    session_date=session_date,
                    lease_owner="integration-test",
                    lease_ms=1_000,
                    now_ms=110,
                )
            with repos.transaction():
                stale_owner_renewed = repos.macro_research.renew_run_lease(
                    session_date=session_date,
                    lease_owner="stale-owner",
                    lease_ms=1_000,
                    now_ms=115,
                )
                owner_renewed = repos.macro_research.renew_run_lease(
                    session_date=session_date,
                    lease_owner="integration-test",
                    lease_ms=1_000,
                    now_ms=115,
                )
            with repos.transaction():
                published = repos.macro_research.publish(
                    session_date=session_date,
                    lease_owner="integration-test",
                    artifact={
                        "schema_version": "macro_research_artifact_v3",
                        "session_date": "2026-07-23",
                        "market_cutoff_ms": 100,
                        "evidence_pack_id": "mep_integration",
                        "title": "宏观研究",
                        "executive_summary": "证据仍有分歧。",
                        "sections": [
                            {
                                "section_id": "overview",
                                "title": "核心观察",
                                "body_markdown": "证据仍有分歧。",
                                "citation_ids": [],
                            }
                        ],
                        "evidence_gaps": [],
                        "citations": [],
                        "reviewer_disposition": "pass",
                        "reviewer_notes": [],
                    },
                    report_markdown="# 宏观研究",
                    audit={"scope_id": "scope-1", "model_calls": 3},
                    model_name="fake-model",
                    prompt_version="prompt-v1",
                    workflow_version="workflow-v1",
                    artifact_hash="sha256:artifact",
                    now_ms=120,
                )
            with repos.transaction():
                replay_published = repos.macro_research.publish(
                    session_date=session_date,
                    lease_owner="integration-test",
                    artifact={"title": "must-not-write", "reviewer_disposition": "pass"},
                    report_markdown="# must not write",
                    audit={},
                    model_name="fake-model",
                    prompt_version="prompt-v1",
                    workflow_version="workflow-v1",
                    artifact_hash="sha256:other",
                    now_ms=130,
                )
                state = repos.macro_research.research_state(session_date)

        publication_count = conn.execute(
            """
            SELECT COUNT(*)::int AS count
            FROM macro_research_publications
            WHERE session_date = %s
            """,
            (session_date,),
        ).fetchone()["count"]
        try:
            conn.execute(
                """
                DELETE FROM macro_research_publications
                WHERE session_date = %s
                """,
                (session_date,),
            )
        except RaiseException:
            immutable = True
            conn.rollback()
        else:
            immutable = False
        checkpoint_roundtrip = asyncio.run(_async_checkpoint_roundtrip())
    finally:
        conn.close()

    assert inserted == 1
    assert claimed is not None
    assert stale_owner_renewed is False
    assert owner_renewed is True
    assert published is True
    assert replay_published is False
    assert publication_count == 1
    assert state is not None
    assert state["run_status"] == "published"
    assert state["evidence_pack_id"] == "mep_integration"
    assert state["reviewer_disposition"] == "pass"
    assert state["artifact_hash"] == "sha256:artifact"
    assert immutable is True
    assert checkpoint_roundtrip is True


async def _async_checkpoint_roundtrip() -> bool:
    checkpoint = empty_checkpoint()
    config = {
        "configurable": {
            "thread_id": "macro-research:integration",
            "checkpoint_ns": "",
        }
    }
    async with AsyncPostgresSaver.from_conn_string(_test_postgres_dsn()) as saver:
        saved_config = await saver.aput(
            config,
            checkpoint,
            {"source": "input", "step": 0, "parents": {}},
            {},
        )
        restored = await saver.aget_tuple(saved_config)
    return restored is not None and restored.checkpoint["id"] == checkpoint["id"]
