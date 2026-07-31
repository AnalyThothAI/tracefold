from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.macro.thesis import MacroEvidencePackV3
from tracefold.macro.thesis_v2 import (
    MacroLiveDeltaV2,
    MacroOutcomeReplayV2,
    MacroResearchInputV1,
    MacroThesisV2,
)
from tracefold.platform.postgres.queue_terminal import terminalize_source_row


class MacroPublicationWriteConflict(RuntimeError):
    pass


class MacroThesisRepository:
    """One durable run lifecycle, v2 current publications, and immutable v1/v2 archive."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def insert_evidence_pack(self, pack: MacroEvidencePackV3) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_evidence_packs(
              evidence_pack_id, session_date, cutoff_ms, sealed_at_ms,
              source_max_received_at_ms, schema_version, payload_json, payload_hash
            )
            VALUES (%s, %s, %s, %s, %s, 'macro_evidence_pack_v3', %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                pack.evidence_pack_id,
                pack.session_date,
                int(pack.cutoff_ms),
                int(pack.sealed_at_ms),
                int(pack.source_max_received_at_ms),
                Jsonb(pack.model_dump(mode="json")),
                pack.payload_hash,
            ),
        )
        return int(cursor.rowcount)

    def evidence_pack(self, evidence_pack_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_evidence_packs
            WHERE evidence_pack_id = %s
              AND schema_version = 'macro_evidence_pack_v3'
            """,
            (evidence_pack_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def evaluation_evidence_packs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT packs.*
            FROM macro_thesis_runs AS runs
            JOIN macro_evidence_packs AS packs
              ON packs.evidence_pack_id = runs.evidence_pack_id
            WHERE packs.schema_version = 'macro_evidence_pack_v3'
            ORDER BY runs.session_date, packs.evidence_pack_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_research_input(self, research_input: MacroResearchInputV1) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_research_inputs(
              research_input_id, evidence_pack_id, session_date, cutoff_ms,
              schema_version, profile_version, prompt_version, payload_json, input_hash
            )
            VALUES (
              %s, %s, %s, %s, 'macro_research_input_v1', %s, %s, %s, %s
            )
            ON CONFLICT DO NOTHING
            """,
            (
                research_input.input_id,
                research_input.evidence_pack_id,
                research_input.session_date,
                int(research_input.cutoff_ms),
                research_input.profile_version,
                research_input.prompt_version,
                Jsonb(research_input.model_dump(mode="json")),
                research_input.input_hash,
            ),
        )
        return int(cursor.rowcount)

    def research_input(self, research_input_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_research_inputs
            WHERE research_input_id = %s
              AND schema_version = 'macro_research_input_v1'
            """,
            (research_input_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def ensure_run(
        self,
        *,
        pack: MacroEvidencePackV3,
        due_at_ms: int,
        max_attempts: int,
        now_ms: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_thesis_runs(
              session_date, cutoff_ms, evidence_pack_id, evidence_pack_hash,
              status, attempt_count, max_attempts, due_at_ms, created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, %s, 'pending', 0, %s, %s, %s, %s)
            ON CONFLICT(session_date) DO NOTHING
            """,
            (
                pack.session_date,
                int(pack.cutoff_ms),
                pack.evidence_pack_id,
                pack.payload_hash,
                int(max_attempts),
                int(due_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        return int(cursor.rowcount)

    def bind_research_input(
        self,
        *,
        session_date: date,
        research_input: MacroResearchInputV1,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE macro_thesis_runs
            SET research_input_id = %s,
                research_input_hash = %s,
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'pending'
              AND attempt_count = 0
              AND evidence_pack_id = %s
              AND research_input_id IS NULL
            RETURNING session_date
            """,
            (
                research_input.input_id,
                research_input.input_hash,
                int(now_ms),
                session_date,
                research_input.evidence_pack_id,
            ),
        ).fetchone()
        if row is not None:
            return True
        existing = self.conn.execute(
            """
            SELECT 1
            FROM macro_thesis_runs
            WHERE session_date = %s
              AND evidence_pack_id = %s
              AND research_input_id = %s
              AND research_input_hash = %s
            """,
            (
                session_date,
                research_input.evidence_pack_id,
                research_input.input_id,
                research_input.input_hash,
            ),
        ).fetchone()
        return existing is not None

    def claim_run(
        self,
        *,
        session_date: date,
        lease_owner: str,
        lease_ms: int,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH candidate AS (
              SELECT session_date
              FROM macro_thesis_runs
              WHERE session_date = %s
                AND research_input_id IS NOT NULL
                AND research_input_hash IS NOT NULL
                AND (
                  (status IN ('pending', 'retryable') AND due_at_ms <= %s)
                  OR (status = 'running' AND leased_until_ms <= %s)
                )
                AND attempt_count < max_attempts
              FOR UPDATE SKIP LOCKED
            )
            UPDATE macro_thesis_runs AS runs
            SET status = 'running',
                attempt_count = runs.attempt_count + 1,
                leased_until_ms = %s,
                lease_owner = %s,
                updated_at_ms = %s
            FROM candidate
            WHERE runs.session_date = candidate.session_date
            RETURNING runs.*
            """,
            (
                session_date,
                int(now_ms),
                int(now_ms),
                int(now_ms) + int(lease_ms),
                _required_text(lease_owner, "lease_owner"),
                int(now_ms),
            ),
        ).fetchone()
        return dict(row) if row is not None else None

    def release_run_claim(
        self,
        *,
        session_date: date,
        lease_owner: str,
        claimed_attempt_count: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE macro_thesis_runs
               SET status = CASE
                     WHEN attempt_count = 1 THEN 'pending'
                     ELSE 'retryable'
                   END,
                   attempt_count = attempt_count - 1,
                   leased_until_ms = NULL,
                   lease_owner = NULL
             WHERE session_date = %s
               AND status = 'running'
               AND lease_owner = %s
               AND attempt_count = %s
               AND attempt_count > 0
            RETURNING session_date
            """,
            (
                session_date,
                _required_text(lease_owner, "lease_owner"),
                int(claimed_attempt_count),
            ),
        ).fetchone()
        return row is not None

    def renew_lease(
        self,
        *,
        session_date: date,
        lease_owner: str,
        lease_ms: int,
        now_ms: int,
    ) -> bool:
        return (
            self.conn.execute(
                """
                UPDATE macro_thesis_runs
                SET leased_until_ms = %s,
                    updated_at_ms = %s
                WHERE session_date = %s
                  AND status = 'running'
                  AND lease_owner = %s
                RETURNING session_date
                """,
                (
                    int(now_ms) + int(lease_ms),
                    int(now_ms),
                    session_date,
                    lease_owner,
                ),
            ).fetchone()
            is not None
        )

    def publish_v2(
        self,
        *,
        publication: MacroThesisV2,
        lease_owner: str,
    ) -> bool:
        inserted = self.conn.execute(
            """
            INSERT INTO macro_thesis_publications(
              publication_id, session_date, cutoff_ms, evidence_pack_id,
              schema_version, thesis_json, thesis_hash, reviewer_invocation_id,
              reviewer_draft_hash, published_at_ms
            )
            SELECT
              %s, runs.session_date, runs.cutoff_ms, runs.evidence_pack_id,
              'macro_thesis_v2', %s, %s, NULL, NULL, %s
            FROM macro_thesis_runs AS runs
            WHERE runs.session_date = %s
              AND runs.status = 'running'
              AND runs.lease_owner = %s
              AND runs.evidence_pack_hash = %s
              AND runs.research_input_id = %s
              AND runs.research_input_hash = %s
            ON CONFLICT DO NOTHING
            RETURNING publication_id
            """,
            (
                publication.publication_id,
                Jsonb(publication.model_dump(mode="json")),
                publication.content_hash,
                int(publication.published_at_ms),
                publication.session_date,
                _required_text(lease_owner, "lease_owner"),
                publication.evidence_pack_hash,
                publication.research_input_id,
                publication.research_input_hash,
            ),
        ).fetchone()
        if inserted is None:
            existing = self.conn.execute(
                """
                SELECT publication_id, thesis_hash
                FROM macro_thesis_publications
                WHERE session_date = %s
                """,
                (publication.session_date,),
            ).fetchone()
            if (
                existing is not None
                and existing["publication_id"] == publication.publication_id
                and existing["thesis_hash"] == publication.content_hash
            ):
                return False
            raise MacroPublicationWriteConflict("macro_thesis_write_identity_conflict")
        completed = self.conn.execute(
            """
            UPDATE macro_thesis_runs
            SET status = 'published',
                publication_id = %s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'running'
              AND lease_owner = %s
            RETURNING session_date
            """,
            (
                publication.publication_id,
                int(publication.published_at_ms),
                publication.session_date,
                lease_owner,
            ),
        ).fetchone()
        if completed is None:
            raise MacroPublicationWriteConflict("macro_thesis_write_run_completion_failed")
        return True

    def mark_error(
        self,
        *,
        session_date: date,
        lease_owner: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        terminal_status: str,
        retry_ms: int,
        now_ms: int,
        gate_category: str | None = None,
        candidate_hash: str | None = None,
    ) -> str:
        if terminal_status not in {"config_error", "not_published", "failed"}:
            raise ValueError("macro_thesis_terminal_status_invalid")
        row = self.conn.execute(
            """
            UPDATE macro_thesis_runs
            SET status = CASE
                  WHEN %s = FALSE THEN %s
                  WHEN attempt_count >= max_attempts THEN 'failed'
                  ELSE 'retryable'
                END,
                due_at_ms = CASE WHEN %s THEN %s ELSE due_at_ms END,
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error_code = %s,
                last_error_message = %s,
                last_gate_category = %s,
                last_candidate_hash = %s,
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'running'
              AND lease_owner = %s
            RETURNING *
            """,
            (
                bool(retryable),
                terminal_status,
                bool(retryable),
                int(now_ms) + int(retry_ms),
                _required_text(error_code, "error_code")[:120],
                _required_text(error_message, "error_message").replace("\n", " ")[:2_000],
                gate_category,
                candidate_hash,
                int(now_ms),
                session_date,
                lease_owner,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("macro_thesis_run_error_owner_mismatch")
        status = str(row["status"])
        if status != "retryable":
            self._terminalize_run(row, now_ms=now_ms)
        return status

    def mark_preflight_error(
        self,
        *,
        session_date: date,
        status: str,
        error_code: str,
        error_message: str,
        now_ms: int,
    ) -> bool:
        if status not in {"failed", "config_error"}:
            raise ValueError("macro_thesis_preflight_status_invalid")
        row = self.conn.execute(
            """
            UPDATE macro_thesis_runs
            SET status = %s,
                last_error_code = %s,
                last_error_message = %s,
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'pending'
              AND attempt_count = 0
            RETURNING *
            """,
            (
                status,
                _required_text(error_code, "error_code")[:120],
                _required_text(error_message, "error_message").replace("\n", " ")[:2_000],
                int(now_ms),
                session_date,
            ),
        ).fetchone()
        if row is None:
            return False
        self._terminalize_run(row, now_ms=now_ms)
        return True

    def _terminalize_run(self, row: Mapping[str, Any], *, now_ms: int) -> None:
        target_key = str(row["session_date"])
        source_row = {**dict(row), "native_target_key": target_key}
        terminalize_source_row(
            self.conn,
            owner_key="macro_thesis",
            source_table="macro_thesis_runs",
            target_key=target_key,
            source_row=source_row,
            final_status=str(row["status"]),
            final_reason=str(row.get("last_error_code") or row["status"]),
            final_reason_bucket="model_terminal",
            now_ms=int(now_ms),
            attempt_count=int(row.get("attempt_count") or 0),
        )

    def archive_publication(self, session_date: date) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM macro_thesis_publications WHERE session_date = %s",
            (session_date,),
        ).fetchone()
        return dict(row) if row is not None else None

    def current_publication_v2(self, session_date: date) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_thesis_publications
            WHERE session_date = %s
              AND schema_version = 'macro_thesis_v2'
            """,
            (session_date,),
        ).fetchone()
        return dict(row) if row is not None else None

    def publication(self, session_date: date | None = None) -> dict[str, Any] | None:
        if session_date is not None:
            return self.archive_publication(session_date)
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_thesis_publications
            ORDER BY session_date DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def prior_publication(self, session_date: date) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_thesis_publications
            WHERE session_date < %s
            ORDER BY session_date DESC
            LIMIT 1
            """,
            (session_date,),
        ).fetchone()
        return dict(row) if row is not None else None

    def state(self, session_date: date | None = None) -> dict[str, Any] | None:
        if session_date is None:
            row = self.conn.execute(
                """
                SELECT
                  runs.*,
                  publications.schema_version,
                  publications.thesis_json,
                  publications.thesis_hash,
                  publications.reviewer_invocation_id,
                  publications.reviewer_draft_hash,
                  publications.published_at_ms
                FROM macro_thesis_runs AS runs
                LEFT JOIN macro_thesis_publications AS publications USING (session_date)
                ORDER BY runs.session_date DESC
                LIMIT 1
                """
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT
                  runs.*,
                  publications.schema_version,
                  publications.thesis_json,
                  publications.thesis_hash,
                  publications.reviewer_invocation_id,
                  publications.reviewer_draft_hash,
                  publications.published_at_ms
                FROM macro_thesis_runs AS runs
                LEFT JOIN macro_thesis_publications AS publications USING (session_date)
                WHERE runs.session_date = %s
                """,
                (session_date,),
            ).fetchone()
        return dict(row) if row is not None else None

    def insert_live_delta(self, delta: MacroLiveDeltaV2) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_live_deltas(
              live_delta_id, publication_id, evaluated_at_ms, module_fact_cutoff_ms,
              schema_version, status, payload_json, input_hash
            )
            VALUES (%s, %s, %s, %s, 'macro_live_delta_v2', %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                delta.live_delta_id,
                delta.publication_id,
                int(delta.evaluated_at_ms),
                int(delta.module_fact_cutoff_ms),
                delta.mainline_validity,
                Jsonb(delta.model_dump(mode="json")),
                delta.input_hash,
            ),
        )
        return int(cursor.rowcount)

    def latest_live_delta(self, publication_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_live_deltas
            WHERE publication_id = %s
              AND schema_version = 'macro_live_delta_v2'
            ORDER BY evaluated_at_ms DESC, live_delta_id DESC
            LIMIT 1
            """,
            (publication_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def insert_outcome_replay(self, replay: MacroOutcomeReplayV2) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_outcome_replays(
              replay_id, publication_id, evaluated_at_ms, schema_version,
              payload_json, input_hash
            )
            VALUES (%s, %s, %s, 'macro_outcome_replay_v2', %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                replay.replay_id,
                replay.publication_id,
                int(replay.evaluated_at_ms),
                Jsonb(replay.model_dump(mode="json")),
                replay.input_hash,
            ),
        )
        return int(cursor.rowcount)

    def latest_outcome_replay(self, publication_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_outcome_replays
            WHERE publication_id = %s
              AND schema_version = 'macro_outcome_replay_v2'
            ORDER BY evaluated_at_ms DESC, replay_id DESC
            LIMIT 1
            """,
            (publication_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def publications(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM macro_thesis_publications
            ORDER BY session_date DESC
            LIMIT %s
            """,
            (max(1, min(int(limit), 250)),),
        ).fetchall()
        return [dict(row) for row in rows]


def publication_payload(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = row.get("thesis_json")
    if not isinstance(value, Mapping):
        raise ValueError("macro_thesis_publication_payload_invalid")
    payload = dict(value)
    payload["payload_hash"] = row.get("thesis_hash")
    return payload


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"macro_thesis_{field_name}_required")
    return normalized


__all__ = [
    "MacroPublicationWriteConflict",
    "MacroThesisRepository",
    "publication_payload",
]
