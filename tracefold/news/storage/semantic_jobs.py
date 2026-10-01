"""Typed semantic job detail; the Event row is locked before any job mutation."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .sql_values import _dumps
from .update_commit import lock_event


class SemanticJobDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    wanted_revision: int = Field(ge=1)
    done_revision: int | None = None
    lineage_id: str
    published_at_ms: int | None = None
    last_outcome: str | None = None
    extra_read_state: str | None = None
    extra_read_target_ref: str | None = None
    attached_evidence: list[dict[str, Any]] | None = None
    focus_claim_refs: list[str] | None = None
    processed_read_refs: list[str] = Field(default_factory=list)
    failed_read_refs: list[str] = Field(default_factory=list)
    attempt_read_refs: list[str] = Field(default_factory=list)
    reanalysis_read_ref: str | None = None
    reanalysis_reason: str | None = None
    reanalysis_head_ref: str | None = None


def semantic_job(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        **dict(row),
        **SemanticJobDetail.model_validate(row["detail"]).model_dump(),
        "event_id": row["subject_id"],
        "leased_until_ms": row["lease_until_ms"],
    }


class SemanticJobs:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def lock(self, event_id: str) -> dict[str, Any] | None:
        lock_event(self.conn, event_id)
        return semantic_job(
            self.conn.execute(
                "SELECT * FROM news_jobs WHERE job_kind='semantic' AND subject_id=%s FOR UPDATE",
                (event_id,),
            ).fetchone()
        )

    def save(self, row: dict[str, Any]) -> None:
        detail = SemanticJobDetail.model_validate({key: row[key] for key in SemanticJobDetail.model_fields})
        state = (
            "done"
            if (detail.done_revision or 0) >= detail.wanted_revision
            else ("failed" if detail.last_outcome == "failed" else "pending")
        )
        self.conn.execute(
            """UPDATE news_jobs SET state=%s,attempts=%s,next_attempt_at_ms=%s,lease_token=%s,
                 lease_until_ms=%s,last_error_code=%s,detail=%s::jsonb,updated_at_ms=%s
               WHERE job_kind='semantic' AND subject_id=%s""",
            (
                state,
                row["attempts"],
                row["next_attempt_at_ms"],
                row["lease_token"],
                row["leased_until_ms"],
                row["last_error_code"],
                _dumps(detail.model_dump()),
                row["updated_at_ms"],
                row["event_id"],
            ),
        )
