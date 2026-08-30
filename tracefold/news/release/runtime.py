"""Image-carried candidate executables and startup release reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ..learning.contracts import ArmManifest, CandidateManifest
from ..program.artifact import (
    ProgramStrategyArtifactV1,
    load_program_artifact,
    load_stable_program_artifact,
)
from ..program.runtime import PROGRAM_VERSION
from .canary import canary_identity_mismatch_reason

CandidateRuntimeFailureKind = Literal[
    "parent_stale",
    "artifact_invalid",
    "runtime_invalid",
    "runtime_unavailable",
]


@dataclass(frozen=True, slots=True)
class CandidateRuntimeFact:
    """What this image compiled and whether this process can execute it."""

    candidate_manifest_sha: str
    compiled_bundle_sha: str
    runnable_bundle_sha: str | None
    failure_kind: CandidateRuntimeFailureKind | None

    def __post_init__(self) -> None:
        runnable = self.runnable_bundle_sha
        if runnable is None and self.failure_kind is None:
            raise ValueError("news_candidate_runtime_fact_failure_required")
        if runnable is not None and (runnable != self.compiled_bundle_sha or self.failure_kind is not None):
            raise ValueError("news_candidate_runtime_fact_runnable_invalid")


class CandidateArtifactUnavailable(ValueError):
    """A News-owned candidate lineage or image-artifact rejection."""

    def __init__(self, failure_kind: Literal["parent_stale", "artifact_invalid"], message: str) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


def candidate_program_artifact(
    candidate: CandidateManifest,
    stable: ArmManifest,
    *,
    stable_artifact: ProgramStrategyArtifactV1 | None = None,
) -> ProgramStrategyArtifactV1:
    """Resolve one candidate only when its bundle and Program lineage match Stable."""

    artifact = stable_artifact or load_stable_program_artifact()
    if stable.program_version != PROGRAM_VERSION or artifact.program_sha256 != stable.program_sha256:
        raise ValueError("news_stable_program_manifest_mismatch")
    if candidate.parent_stable_sha != stable.bundle_sha:
        raise CandidateArtifactUnavailable("parent_stale", "news_candidate_parent_stable_mismatch")
    arm = candidate.candidate_arm
    if (
        arm.program_version != PROGRAM_VERSION
        or candidate.proposal_receipt.program_parent_sha256 != artifact.program_sha256
        or candidate.proposal_receipt.program_candidate_sha256 != arm.program_sha256
    ):
        raise CandidateArtifactUnavailable("artifact_invalid", "news_candidate_program_parent_mismatch")
    try:
        return load_program_artifact(arm.program_sha256)
    except (OSError, ValueError) as exc:
        raise CandidateArtifactUnavailable("artifact_invalid", str(exc)) from exc


def artifact_valid_candidate_bundles(
    stable: ArmManifest,
    candidates: Mapping[str, CandidateManifest],
) -> dict[str, str]:
    """Return only candidates whose complete image-carried lineage validates."""

    stable_artifact = load_stable_program_artifact()
    if stable.program_version != PROGRAM_VERSION or stable_artifact.program_sha256 != stable.program_sha256:
        raise ValueError("news_stable_program_manifest_mismatch")
    shipped: dict[str, str] = {}
    for candidate_sha, candidate in candidates.items():
        try:
            candidate_program_artifact(candidate, stable, stable_artifact=stable_artifact)
        except CandidateArtifactUnavailable:
            continue
        shipped[candidate_sha] = candidate.candidate_arm.bundle_sha
    return shipped


_FAILURE_REASONS: dict[CandidateRuntimeFailureKind, str] = {
    "parent_stale": "candidate_parent_stale",
    "artifact_invalid": "candidate_artifact_invalid",
    "runtime_invalid": "candidate_runtime_invalid",
    "runtime_unavailable": "candidate_runtime_unavailable",
}


def reconcile_canary_startup(
    repository: Any,
    *,
    candidate_facts: Mapping[str, CandidateRuntimeFact],
    now_ms: int,
) -> bool:
    """Fail closed a nonterminal activation that this process cannot execute."""

    status = repository.canary_status()
    activation = status.get("activation")
    if activation is None or str(activation["state"]) not in {"armed", "active"}:
        return False
    reason = canary_identity_mismatch_reason(activation)
    candidate_manifest_sha = str(activation["candidate_manifest_sha"])
    candidate_bundle_sha = str(activation["candidate_bundle_sha"])
    fact = candidate_facts.get(candidate_manifest_sha)
    if reason is None and (fact is None or fact.candidate_manifest_sha != candidate_manifest_sha):
        reason = "candidate_manifest_missing_or_invalid"
    if reason is None:
        if fact is None:
            raise AssertionError("news_candidate_runtime_fact_missing")
        if candidate_bundle_sha != fact.compiled_bundle_sha:
            reason = "candidate_bundle_mismatch"
        elif fact.runnable_bundle_sha == candidate_bundle_sha:
            return False
    if reason is None:
        if fact is None:
            raise AssertionError("news_candidate_runtime_fact_missing")
        if fact.failure_kind is None:
            raise ValueError("news_candidate_runtime_fact_failure_missing")
        reason = _FAILURE_REASONS[fact.failure_kind]
    return bool(
        repository.transition_canary(
            activation_id=str(activation["activation_id"]),
            target_state="tripped",
            reason=reason,
            now_ms=now_ms,
        )
    )


__all__ = [
    "CandidateArtifactUnavailable",
    "CandidateRuntimeFact",
    "CandidateRuntimeFailureKind",
    "artifact_valid_candidate_bundles",
    "candidate_program_artifact",
    "reconcile_canary_startup",
]
