from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.macro.thesis import (
    MacroEvidencePackV3,
    MacroLiveDeltaV1,
    MacroOutcomeReplayV1,
    MacroThesisReviewV1,
    MacroThesisV1,
)


class MacroThesisRepository:
    """PostgreSQL lifecycle for one immutable daily Thesis and deterministic follow-ups."""

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
                last_error_code = NULL,
                last_error_message = NULL,
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

    def record_review(
        self,
        *,
        session_date: date,
        review: MacroThesisReviewV1,
        review_sequence: int,
        created_at_ms: int,
    ) -> int:
        review_id = f"{session_date.isoformat()}:{review.invocation_id}"
        cursor = self.conn.execute(
            """
            INSERT INTO macro_thesis_reviews(
              review_id, session_date, review_sequence, draft_hash, disposition,
              review_json, invocation_id, model_name, prompt_version, created_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                review_id,
                session_date,
                int(review_sequence),
                review.draft_hash,
                review.disposition,
                Jsonb(review.model_dump(mode="json")),
                review.invocation_id,
                review.model_name,
                review.prompt_version,
                int(created_at_ms),
            ),
        )
        return int(cursor.rowcount)

    def publish(
        self,
        *,
        publication: MacroThesisV1,
        lease_owner: str,
    ) -> bool:
        publication_payload = publication.model_dump(mode="json")
        publication_hash = publication.content_hash
        inserted = self.conn.execute(
            """
            INSERT INTO macro_thesis_publications(
              publication_id, session_date, cutoff_ms, evidence_pack_id,
              schema_version, thesis_json, thesis_hash, reviewer_invocation_id,
              reviewer_draft_hash, published_at_ms
            )
            SELECT
              %s, runs.session_date, runs.cutoff_ms, runs.evidence_pack_id,
              'macro_thesis_v1', %s, %s, %s, %s, %s
            FROM macro_thesis_runs AS runs
            WHERE runs.session_date = %s
              AND runs.status = 'running'
              AND runs.lease_owner = %s
              AND runs.evidence_pack_hash = %s
            ON CONFLICT DO NOTHING
            RETURNING publication_id
            """,
            (
                publication.publication_id,
                Jsonb(publication_payload),
                publication_hash,
                publication.review.invocation_id,
                publication.review.draft_hash,
                int(publication.published_at_ms),
                publication.session_date,
                _required_text(lease_owner, "lease_owner"),
                publication.evidence_pack_hash,
            ),
        ).fetchone()
        if inserted is None:
            return False
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
            raise RuntimeError("macro_thesis_run_completion_failed")
        return True

    def mark_error(
        self,
        *,
        session_date: date,
        lease_owner: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        terminal_status: str = "config_error",
        retry_ms: int,
        now_ms: int,
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
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'running'
              AND lease_owner = %s
            RETURNING status
            """,
            (
                bool(retryable),
                terminal_status,
                bool(retryable),
                int(now_ms) + int(retry_ms),
                _required_text(error_code, "error_code")[:120],
                _required_text(error_message, "error_message").replace("\n", " ")[:2_000],
                int(now_ms),
                session_date,
                lease_owner,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("macro_thesis_run_error_owner_mismatch")
        return str(row["status"])

    def mark_configuration_error_before_attempt(
        self,
        *,
        session_date: date,
        error_code: str,
        error_message: str,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE macro_thesis_runs
            SET status = 'config_error',
                last_error_code = %s,
                last_error_message = %s,
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'pending'
              AND attempt_count = 0
            RETURNING session_date
            """,
            (
                _required_text(error_code, "error_code")[:120],
                _required_text(error_message, "error_message").replace("\n", " ")[:2_000],
                int(now_ms),
                session_date,
            ),
        ).fetchone()
        return row is not None

    def publication(self, session_date: date | None = None) -> dict[str, Any] | None:
        if session_date is None:
            row = self.conn.execute(
                """
                SELECT *
                FROM macro_thesis_publications
                ORDER BY session_date DESC
                LIMIT 1
                """
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM macro_thesis_publications WHERE session_date = %s",
                (session_date,),
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
            session_row = self.conn.execute(
                "SELECT MAX(session_date) AS session_date FROM macro_thesis_runs"
            ).fetchone()
            session_date = session_row["session_date"] if session_row is not None else None
        if session_date is None:
            return None
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

    def insert_live_delta(self, delta: MacroLiveDeltaV1) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_live_deltas(
              live_delta_id, publication_id, evaluated_at_ms, module_fact_cutoff_ms,
              schema_version, status, payload_json, input_hash
            )
            VALUES (%s, %s, %s, %s, 'macro_live_delta_v1', %s, %s, %s)
            ON CONFLICT (live_delta_id) DO UPDATE
            SET evaluated_at_ms = EXCLUDED.evaluated_at_ms,
                module_fact_cutoff_ms = EXCLUDED.module_fact_cutoff_ms,
                status = EXCLUDED.status,
                payload_json = EXCLUDED.payload_json,
                input_hash = EXCLUDED.input_hash
            WHERE macro_live_deltas.input_hash IS DISTINCT FROM EXCLUDED.input_hash
            """,
            (
                delta.live_delta_id,
                delta.publication_id,
                int(delta.evaluated_at_ms),
                int(delta.module_fact_cutoff_ms),
                delta.status,
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
            ORDER BY evaluated_at_ms DESC, live_delta_id DESC
            LIMIT 1
            """,
            (publication_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def insert_outcome_replay(self, replay: MacroOutcomeReplayV1) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_outcome_replays(
              replay_id, publication_id, evaluated_at_ms, schema_version,
              payload_json, input_hash
            )
            VALUES (%s, %s, %s, 'macro_outcome_replay_v1', %s, %s)
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


__all__ = ["MacroThesisRepository", "publication_payload"]
