"""The learning plane's view of its own ledger: what a row means, not how it is stored.

Extracted from `CandidateEvaluator` (#202 §8) because three lifecycles were reaching the same handful of
reads and writes through one 3,000-line class: freezing a dataset, evaluating a candidate, and moving a
release stage. None of them needs the other two, and while they shared a class they shared everything —
which is why a change to any objective, dataset, metric or release boundary edited one file.

It holds no SQL. `docs/DEVELOPMENT.md` puts business SQL in the owning package's storage module behind a
named repository method, and `NewsRepository` is that module; what lives here is the part storage should
not know — that an epoch row has to match what the epoch was *opened* with, that an evaluation may only
proceed against the stable arm the last deployment appointed, and what identity a cohort is described by.

Notably it holds no judge, no Program and no DSPy: an artifact write is not a model call, and a caller
that only needs to read the epoch should not pay four seconds of import to do it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..review.desk import READER_CONTRACT_SHA256, READER_CONTRACT_VERSION
from ..storage.root import NewsRepository
from .contracts import LEARNING_EPOCH, LEARNING_PROGRAM_VERSION, ArmManifest
from .epoch import (
    LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION,
    LEARNING_EPOCH_OPENED_FACTORY_ID,
    LEARNING_EPOCH_RESET_REASON,
)


class LearningLedger:
    """The learning plane's own rows, and the active stable arm every one of them is written against."""

    def __init__(self, conn: Any, *, stable: ArmManifest, principal: str) -> None:
        self._repository = NewsRepository(conn)
        self._stable = stable
        self._principal = principal

    def now_ms(self) -> int:
        return self._repository.db_now_ms()

    def persist_artifact(self, kind: str, payload: Mapping[str, Any], *, parent_sha: str | None = None) -> str:
        return self._repository.learning_artifact_read_back(
            kind,
            payload,
            parent_sha=parent_sha,
            created_by=self._principal,
            now_ms=self.now_ms(),
        )

    def active_stable_sha(self) -> str:
        # Only worker startup/deployment may appoint the active Agent. The evaluator receives a candidate
        # comparator, not authority to create a production root when the runtime receipt is absent.
        stable_sha = self._repository.active_stable_agent_sha()
        if stable_sha is None:
            raise ValueError("news_learning_active_stable_receipt_missing")
        return stable_sha

    def assert_active_stable(self) -> str:
        if self._stable.program_version != LEARNING_PROGRAM_VERSION:
            raise ValueError("news_learning_program_v1_unsupported")
        active_sha = self.active_stable_sha()
        if active_sha != self._stable.bundle_sha:
            raise ValueError("news_learning_active_stable_mismatch")
        return active_sha

    def reviews_by_id(self, review_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        return self._repository.reviews_by_id(review_ids)

    def epoch_started_at_ms(self) -> int:
        row = self._repository.learning_epoch_row(LEARNING_EPOCH)
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
