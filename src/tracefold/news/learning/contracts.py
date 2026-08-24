"""Frozen manifest contracts for the learning plane.

Separated from `evaluator.py` so a caller that only needs to *read* a candidate manifest — the Workers
composition root, which validates image-carried candidates at startup — does not import the evaluator,
and through it the compiler and DSPy. The online process paid ~4 s of import for four Pydantic models.

Nothing here evaluates, scores, compiles or persists; it is the shape a candidate has, plus the hashes
that make that shape content-addressed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_json, canonical_sha
from ..triage_rules import DecidePolicy

LEARNING_PROFILE_ID: Literal["news_learning_release_v1"] = "news_learning_release_v1"
LEARNING_EPOCH: Literal["program_v7"] = "program_v7"
LEARNING_PROGRAM_VERSION = "news_semantic_program_v5"


def _sha(value: Any) -> str:
    return canonical_sha(value)


def _proposal_json(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize tuple/list representation before hashing a registration receipt."""

    normalized = json.loads(canonical_json(dict(value)))
    if not isinstance(normalized, dict):  # pragma: no cover - Mapping input guarantees this
        raise TypeError("news_learning_proposal_payload_invalid")
    return normalized


class ClosedWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_ms: int = Field(ge=0)
    to_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> ClosedWindow:
        if self.to_ms <= self.from_ms:
            raise ValueError("news_learning_window_invalid")
        return self


class ArmManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    program_version: str = Field(min_length=1, max_length=128)
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_model_bindings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: dict[str, Any]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hashes_match(self) -> ArmManifest:
        if _sha(self.policy) != self.policy_sha256:
            raise ValueError("news_learning_policy_sha_mismatch")
        # Parse now, before a model call, so a malformed candidate policy can
        # never consume provider budget.
        DecidePolicy(**self.policy)
        return self

    @property
    def bundle_sha(self) -> str:
        return _sha(self.model_dump(mode="json"))


class ProposalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    development_dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    generator_kind: Literal["human", "model"]
    generator_prompt_sha: str | None = None
    generator_model_sha: str | None = None
    generator_execution_sha: str | None = None
    registered_at_ms: int = Field(ge=0)
    registration_receipt_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_patch_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    declared_target_dimensions: tuple[str, ...] = Field(min_length=1)
    guardrails: tuple[str, ...] = ()
    program_parent_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    program_candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    program_state_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    program_machine_diff: dict[str, Any] | None = None
    compile_provenance: dict[str, Any] | None = None

    @classmethod
    def issue(cls, **values: Any) -> ProposalReceipt:
        """Issue a content-addressed DB/CI registration receipt.

        The caller still has to persist ``registration_payload`` under the
        returned SHA before CandidateEvaluator may use the candidate.  Keeping
        construction and verification on the value type prevents the CLI and
        tests from inventing subtly different receipt hashes.
        """

        draft = cls.model_construct(registration_receipt_sha="0" * 64, **values)
        registration_sha = _sha({"kind": "candidate_registration", "payload": draft.registration_payload})
        return cls(registration_receipt_sha=registration_sha, **values)

    @model_validator(mode="after")
    def registration_is_exact(self) -> ProposalReceipt:
        if self.generator_kind == "model" and not all(
            (self.generator_prompt_sha, self.generator_model_sha, self.generator_execution_sha)
        ):
            raise ValueError("news_learning_model_generator_receipt_incomplete")
        program_fields = (
            self.program_parent_sha256,
            self.program_candidate_sha256,
            self.program_state_sha256,
            self.program_machine_diff,
            self.compile_provenance,
        )
        if any(value is not None for value in program_fields) and not all(
            value is not None for value in program_fields
        ):
            raise ValueError("news_learning_program_receipt_incomplete")
        if self.program_machine_diff is not None and not self.program_machine_diff:
            raise ValueError("news_learning_program_machine_diff_empty")
        if self.compile_provenance is not None and not self.compile_provenance:
            raise ValueError("news_learning_program_compile_provenance_empty")
        expected = _sha({"kind": "candidate_registration", "payload": self.registration_payload})
        if self.registration_receipt_sha != expected:
            raise ValueError("news_learning_registration_receipt_sha_mismatch")
        return self

    @property
    def registration_payload(self) -> dict[str, Any]:
        return _proposal_json(self.model_dump(mode="json", exclude={"registration_receipt_sha"}))


class CandidateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Literal["program", "policy"]
    parent_stable_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_arm: ArmManifest
    hypothesis: str = Field(min_length=1, max_length=2_000)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    development_dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_receipt: ProposalReceipt

    @property
    def candidate_sha(self) -> str:
        return _sha(self.model_dump(mode="json"))


class DatasetCaseRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    subject_kind: Literal["event", "external_miss"]
    event_id: str | None = None
    evidence_version: int | None = None
    external_snapshot_id: str | None = None
    evidence_sha256: str
    review_id: str
    cluster_id: str
    stratum: str
    should_push: str
    opened_at_ms: int
    delivery_truth: Literal["observed_sent", "observed_not_sent", "unknown"] = "unknown"


__all__ = [
    "LEARNING_EPOCH",
    "LEARNING_PROFILE_ID",
    "LEARNING_PROGRAM_VERSION",
    "ArmManifest",
    "CandidateManifest",
    "ClosedWindow",
    "DatasetCaseRef",
    "ProposalReceipt",
]
