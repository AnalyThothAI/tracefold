"""Every read and write the learning plane makes against its own ledger tables.

Extracted from `CandidateEvaluator` (#202 §8) because three different lifecycles were reaching the same
seven SQL statements through one 3,000-line class: freezing a dataset, evaluating a candidate, and moving
a release stage. None of them needs the other two, and while they shared a class they shared everything —
which is the reason a change to any objective, dataset, metric or release boundary edited one file.

It holds a connection and the active stable arm, and nothing else. Notably it holds no judge, no Program
and no DSPy: an artifact write is not a model call, and a caller that only needs to read the epoch should
not pay four seconds of import to do it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..review.desk import READER_CONTRACT_SHA256, READER_CONTRACT_VERSION
from .contracts import LEARNING_EPOCH, LEARNING_PROGRAM_VERSION, ArmManifest
from .epoch import (
    LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION,
    LEARNING_EPOCH_OPENED_FACTORY_ID,
    LEARNING_EPOCH_RESET_REASON,
)
from .projection import _json, _sha


class LearningLedger:
    """The learning plane's own tables, and the active stable arm every row is written against."""

    def __init__(self, conn: Any, *, stable: ArmManifest, principal: str) -> None:
        self._conn = conn
        self._stable = stable
        self._principal = principal

    def persist_artifact(self, kind: str, payload: Mapping[str, Any], *, parent_sha: str | None = None) -> str:
        artifact_sha = _sha({"kind": kind, "payload": payload})
        self._conn.execute(
            """
            INSERT INTO news_learning_artifacts (artifact_sha, kind, parent_sha, payload, created_by, created_at_ms)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s) ON CONFLICT (artifact_sha) DO NOTHING
            """,
            (artifact_sha, kind, parent_sha, _json(payload), self._principal, self.now_ms()),
        )
        row = self._conn.execute(
            "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s", (artifact_sha,)
        ).fetchone()
        if row is None or row["kind"] != kind or _sha({"kind": kind, "payload": row["payload"]}) != artifact_sha:
            raise ValueError("news_learning_artifact_collision")
        return artifact_sha

    def active_stable_sha(self) -> str:
        # Only worker startup/deployment may appoint the active Agent. The
        # evaluator receives a candidate comparator, not authority to create a
        # production root when the runtime receipt is absent.
        row = self._conn.execute(
            "SELECT payload ->> 'stable_sha' AS stable_sha FROM news_learning_artifacts "
            "WHERE kind = 'active_agent' ORDER BY created_at_ms DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("news_learning_active_stable_receipt_missing")
        return str(row["stable_sha"])

    def assert_active_stable(self) -> str:
        if self._stable.program_version != LEARNING_PROGRAM_VERSION:
            raise ValueError("news_learning_program_v1_unsupported")
        active_sha = self.active_stable_sha()
        if active_sha != self._stable.bundle_sha:
            raise ValueError("news_learning_active_stable_mismatch")
        return active_sha

    def reviews_by_id(self, review_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not review_ids:
            return {}
        rows = self._conn.execute(
            "SELECT * FROM news_reviews WHERE review_id = ANY(%s)", (list(review_ids),)
        ).fetchall()
        return {str(row["review_id"]): dict(row) for row in rows}

    def epoch_started_at_ms(self) -> int:
        row = self._conn.execute(
            "SELECT starts_at_ms, program_factory_id, artifact_schema_version, "
            "baseline_program_version, prior_evidence_disposition, reset_reason "
            "FROM news_learning_epochs WHERE epoch_id = %s",
            (LEARNING_EPOCH,),
        ).fetchone()
        if row is None:
            raise ValueError("news_learning_epoch_not_deployed")
        # Compared against what the epoch was opened with, not against today's runtime constants. Both
        # still prove the persisted epoch identity before its evidence is treated as eligible, which is
        # what catches migration drift or a corrupted ledger row.
        if (
            str(row["program_factory_id"]) != LEARNING_EPOCH_OPENED_FACTORY_ID
            or str(row["artifact_schema_version"]) != LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION
            or str(row["baseline_program_version"]) != LEARNING_PROGRAM_VERSION
            or str(row["prior_evidence_disposition"]) != "audit_only"
            or str(row["reset_reason"]) != LEARNING_EPOCH_RESET_REASON
        ):
            raise ValueError("news_learning_epoch_contract_mismatch")
        return int(row["starts_at_ms"])

    def now_ms(self) -> int:
        row = self._conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
        ).fetchone()
        return int(row["now_ms"])

    def agent_cohort(self) -> dict[str, str]:
        return {
            "bundle_sha": self._stable.bundle_sha,
            "learning_epoch": LEARNING_EPOCH,
            "program_version": self._stable.program_version,
            "program_sha256": self._stable.program_sha256,
            "runtime_model_bindings_sha256": self._stable.runtime_model_bindings_sha256,
            "retrieval_sha256": self._stable.retrieval_sha256,
            "policy_sha256": self._stable.policy_sha256,
            "reader_contract_version": READER_CONTRACT_VERSION,
            "reader_contract_sha256": READER_CONTRACT_SHA256,
        }


__all__ = ["LearningLedger"]
