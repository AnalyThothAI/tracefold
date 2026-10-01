"""Insert-only semantic observations, checkpoints and atomic adoption.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..updates.contracts import EventUpdate, SemanticLease
from .errors import EventUpdateConflict
from .semantic_rows import ANALYSES_SQL, ANALYSIS_HEADS_SQL, SEMANTIC_RESULTS_SQL
from .semantic_work import SemanticWorkStorage
from .trade_projection import TradeProjectionStorage
from .update_commit import SemanticSource, commit_update, lock_event


class SemanticUpdateStorage:
    def __init__(self, conn: Any, *, work: SemanticWorkStorage, outbox: TradeProjectionStorage) -> None:
        self.conn = conn
        self.work = work
        self.outbox = outbox

    def event_update_head_document(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"""
            SELECT u.document
              FROM ({ANALYSIS_HEADS_SQL}) h
              JOIN ({ANALYSES_SQL}) u ON u.event_id = h.event_id AND u.content_revision = h.content_revision
             WHERE h.event_id = %s
            """,  # noqa: S608 -- fixed SQL; bound values.
            (event_id,),
        ).fetchone()
        return None if row is None else dict(row["document"])

    def semantic_checkpoint_documents(self, work_id: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT split_part(substr(cache_key,21),':',1) AS stage,answer AS document FROM"
            " news_judgment_cache WHERE cache_key IN (%s,%s)",
            (f"semantic_checkpoint:extraction:{work_id}", f"semantic_checkpoint:understanding:{work_id}"),
        ).fetchall()
        return {str(row["stage"]): dict(row["document"]) for row in rows}

    def insert_semantic_checkpoint(
        self, *, work_id: str, stage: str, document_json: str, now_ms: int
    ) -> dict[str, Any]:
        """Insert-only; the first stored stage document wins a race and is returned."""

        self.conn.execute(
            """
            INSERT INTO news_judgment_cache (cache_key, answer, created_at_ms)
            VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (cache_key) DO NOTHING
            """,
            (f"semantic_checkpoint:{stage}:{work_id}", document_json, int(now_ms)),
        )
        row = self.conn.execute(
            "SELECT answer AS document FROM news_judgment_cache WHERE cache_key = %s",
            (f"semantic_checkpoint:{stage}:{work_id}",),
        ).fetchone()
        return dict(row["document"])

    def insert_semantic_observation(
        self,
        *,
        result_id: str,
        work_id: str,
        event_id: str,
        input_revision: int,
        input_sha256: str,
        program_identity: str,
        completed_at_ms: int,
        understanding_json: str,
        input_manifest_json: str,
        read_refs: Sequence[str],
        reanalysis_reason: str | None,
        reanalysis_head_ref: str | None,
    ) -> dict[str, Any]:
        """Insert-only by result id; the stored row, with its original completion clock, is returned."""

        self.conn.execute(
            """
            INSERT INTO news_analyses (
              analysis_id, origin,work_id,event_id,input_revision,input_sha256,program_identity,
              completed_at_ms,understanding,read_refs,reanalysis_reason,reanalysis_head_ref,input_manifest
            ) VALUES (%s,'semantic',%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb)
            ON CONFLICT (analysis_id) DO NOTHING
            """,
            (
                result_id,
                work_id,
                event_id,
                int(input_revision),
                input_sha256,
                program_identity,
                int(completed_at_ms),
                understanding_json,
                list(read_refs),
                reanalysis_reason,
                reanalysis_head_ref,
                input_manifest_json,
            ),
        )
        row = self.conn.execute(f"SELECT * FROM ({SEMANTIC_RESULTS_SQL}) WHERE result_id = %s", (result_id,)).fetchone()  # noqa: S608 -- fixed SQL; bound values.
        return dict(row)

    def adopt_event_update(
        self,
        *,
        expected_head_ref: str | None,
        lease: SemanticLease,
        update: EventUpdate,
        document_json: str,
        observation_result_id: str,
        public_rows: Sequence[tuple[str, Mapping[str, Any]]],
        now_ms: int,
    ) -> bool:
        """CAS the head and write the update, its public outbox rows and the notification marker.

        Serialized per Event by a Event row lock, so two adopters of one expected head can
        never both write: the second sees the first's head and returns False with nothing written.
        """

        event_id = update.event_id
        if event_id != lease.event_id or update.input_revision != lease.wanted_revision:
            raise EventUpdateConflict("news_semantic_update_lease_mismatch")
        lock_event(self.conn, event_id)
        self.work.require_semantic_owner(lease, now_ms=now_ms)
        try:
            return commit_update(
                self.conn,
                outbox=self.outbox,
                expected_head_ref=expected_head_ref,
                update=update,
                document_json=document_json,
                source=SemanticSource(observation_result_id),
                public_rows=public_rows,
                now_ms=now_ms,
            )
        except ValueError as exc:
            raise EventUpdateConflict(str(exc)) from exc
