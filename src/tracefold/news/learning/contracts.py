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

from ..artifact_identity import canonical_json, canonical_sha, reject_nonfinite_json, reject_secret_material
from ..program.artifact import ProgramStrategyArtifactV1, ProgramStrategyPatchV1, validate_learned_instruction
from ..triage_rules import DecidePolicy

LEARNING_PROFILE_ID: Literal["news_learning_release_v1"] = "news_learning_release_v1"
LEARNING_EPOCH: Literal["program_v7"] = "program_v7"
LEARNING_PROGRAM_VERSION = "news_semantic_program_v5"
PROMPT_CANDIDATE_SCHEMA: Literal["news_prompt_candidate_v1"] = "news_prompt_candidate_v1"
OPTIMIZATION_RUN_REPORT_SCHEMA: Literal["news_optimization_run_report_v1"] = "news_optimization_run_report_v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

# The three terminal states one offline optimization can end in (#202 §5). Every one of them is a complete,
# retained artifact: `NO_OP` and `REJECTED` are answers, not failures, and an operator has to be able to read
# why a run spent a budget and shipped nothing.
OptimizationOutcome = Literal["NO_OP", "REJECTED", "ADVANCE"]


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
    # For a Program candidate this is the compile record's own identity. It used to sit beside a prompt
    # digest and a model digest that re-hashed the same compile from two other angles.
    generator_execution_sha: str | None = None
    registered_at_ms: int = Field(ge=0)
    registration_receipt_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    declared_target_dimensions: tuple[str, ...] = Field(min_length=1)
    guardrails: tuple[str, ...] = ()
    program_parent_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    program_candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # The one compile this candidate came out of. The receipt used to carry the whole provenance record
    # and a machine diff inline, so the same identities were stored twice and could disagree.
    compile_record_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

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
        if self.generator_kind == "model" and not self.generator_execution_sha:
            raise ValueError("news_learning_model_generator_receipt_incomplete")
        program_fields = (
            self.program_parent_sha256,
            self.program_candidate_sha256,
            self.compile_record_sha256,
        )
        if any(value is not None for value in program_fields) and not all(
            value is not None for value in program_fields
        ):
            raise ValueError("news_learning_program_receipt_incomplete")
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


class DevelopmentDatasetRef(BaseModel):
    """The identity of one frozen development dataset, without its episodes.

    An offline optimization is handed a corpus and must be able to prove *which* corpus, in a document a
    reader can check later. The episodes themselves are the payload; this is the binding, and
    `episode_projection_root_sha256` is what makes the two inseparable — `FrozenDevelopmentDataset` rehashes
    the episodes it was given and refuses a ref that describes a different projection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    development_dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    episode_projection_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    episode_count: int = Field(gt=0)
    learning_epoch: Literal["program_v7"] = LEARNING_EPOCH
    learning_epoch_started_at_ms: int = Field(ge=0)
    review_rubric_version: str = Field(min_length=1, max_length=64)


class OptimizationBudget(BaseModel):
    """The complete bound one offline optimization runs under.

    Typed and in-process, because #202 removes the metered proxy that used to enforce it from outside. The
    wall clock is here for the same reason the call and cost ceilings are: a run that stops answering is
    still spending, and `REJECTED` on a deadline is an auditable terminal state where a killed container was
    a missing one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_metric_calls: int = Field(gt=0)
    max_task_model_calls: int = Field(gt=0)
    max_reflection_model_calls: int = Field(gt=0)
    max_metric_judge_model_calls: int = Field(gt=0)
    max_cost_microusd: int = Field(gt=0)
    max_call_cost_microusd: int = Field(gt=0)
    max_wall_clock_seconds: float = Field(gt=0, le=86_400)
    seed: int = Field(ge=0)

    @model_validator(mode="after")
    def _reservation_can_admit_one_call(self) -> OptimizationBudget:
        if self.max_call_cost_microusd > self.max_cost_microusd:
            raise ValueError("news_learning_optimize_call_cost_reservation_invalid")
        return self


class PromptPatchV1(BaseModel):
    """The entire legal write-set of News learning: two advisory instructions.

    `ProgramStrategyPatchV1` says the same two things bound to a parent, because applying a patch to a
    Program is the Program package's business and needs the parent to refuse a mismatch. This one is the
    *candidate's* write-set, which is why it carries nothing else — a field here is a field an optimizer
    could learn to write. The safety bounds are not restated: `validate_learned_instruction` is the one
    implementation, so a candidate cannot be admitted under looser rules than the artifact it becomes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_semantics_instruction: str
    reader_card_instruction: str

    @model_validator(mode="after")
    def _write_set_is_safe(self) -> PromptPatchV1:
        validate_learned_instruction(self.event_semantics_instruction)
        validate_learned_instruction(self.reader_card_instruction)
        return self

    @classmethod
    def of(cls, patch: ProgramStrategyPatchV1) -> PromptPatchV1:
        return cls(
            event_semantics_instruction=patch.event_semantics_instruction,
            reader_card_instruction=patch.reader_card_instruction,
        )

    def applied_to(self, parent: ProgramStrategyArtifactV1) -> ProgramStrategyPatchV1:
        """Bind this write-set to the Program it was optimized against."""

        return ProgramStrategyPatchV1.issue(
            parent=parent,
            event_semantics_instruction=self.event_semantics_instruction,
            reader_card_instruction=self.reader_card_instruction,
        )

    def changes(self, parent: ProgramStrategyArtifactV1) -> bool:
        return (
            self.event_semantics_instruction != parent.event_semantics_instruction
            or self.reader_card_instruction != parent.reader_card_instruction
        )


class PromptCandidateV1(BaseModel):
    """One prompt candidate, whatever produced it.

    Provenance is recorded and audited; it grants nothing. Until #202 a candidate's release eligibility came
    from *where it was generated* — inside a sealed compiler image, against a metered proxy — so an
    experiment that found a better instruction had to be reproduced by a container before any gate would
    look at it. The write-set is two strings; the generator cannot be the authority for them. Registration,
    independent evaluation, future holdout, shadow, canary and a human promotion are.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["news_prompt_candidate_v1"] = PROMPT_CANDIDATE_SCHEMA
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    development_dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_runtime_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    patch: PromptPatchV1
    objective_summary: dict[str, Any]
    optimizer: dict[str, Any]
    model_identities: dict[str, Any]
    budget: dict[str, Any]
    usage: dict[str, Any]
    created_at_ms: int = Field(ge=0)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> PromptCandidateV1:
        draft = cls.model_construct(candidate_sha256="0" * 64, **values)
        payload = draft.model_dump(mode="json", exclude={"candidate_sha256"})
        return cls(**values, candidate_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _identity_is_exact_and_carries_no_credential(self) -> PromptCandidateV1:
        payload = self.model_dump(mode="json", exclude={"candidate_sha256"})
        reject_nonfinite_json(payload, path="prompt_candidate")
        reject_secret_material(payload, path="prompt_candidate")
        if self.candidate_sha256 != canonical_sha(payload):
            raise ValueError("news_learning_prompt_candidate_hash_mismatch")
        return self


class OptimizationRunReport(BaseModel):
    """The retained record of one optimization, in every terminal state.

    A `NO_OP` and a `REJECTED` produce this and nothing else; an `ADVANCE` produces this *and* a candidate,
    and the report names the candidate's hash so the two are readable as one run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["news_optimization_run_report_v1"] = OPTIMIZATION_RUN_REPORT_SCHEMA
    outcome: OptimizationOutcome
    dataset: DevelopmentDatasetRef
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_runtime_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    objective: dict[str, Any]
    # Absent on a run the Objective Plan refused before any model call: there was no split, no metric and no
    # trajectory, and writing an empty object for each would make an unspent refusal look like a spent run.
    split: dict[str, Any] | None = None
    retrieval: dict[str, Any] | None = None
    metric: dict[str, Any] | None = None
    optimizer: dict[str, Any] | None = None
    trajectory: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    model_identities: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any]
    usage: dict[str, Any]
    reasons: tuple[str, ...] = ()
    started_at_ms: int = Field(ge=0)
    completed_at_ms: int = Field(ge=0)
    candidate_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    report_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> OptimizationRunReport:
        draft = cls.model_construct(report_sha256="0" * 64, **values)
        payload = draft.model_dump(mode="json", exclude={"report_sha256"})
        return cls(**values, report_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _terminal_state_is_coherent(self) -> OptimizationRunReport:
        payload = self.model_dump(mode="json", exclude={"report_sha256"})
        reject_nonfinite_json(payload, path="optimization_run_report")
        reject_secret_material(payload, path="optimization_run_report")
        if self.completed_at_ms < self.started_at_ms:
            raise ValueError("news_learning_optimize_report_window_invalid")
        if (self.outcome == "ADVANCE") != (self.candidate_sha256 is not None):
            raise ValueError("news_learning_optimize_report_outcome_mismatch")
        if self.outcome != "ADVANCE" and not self.reasons:
            raise ValueError("news_learning_optimize_report_reason_required")
        if self.report_sha256 != canonical_sha(payload):
            raise ValueError("news_learning_optimize_report_hash_mismatch")
        return self


class OptimizationResult(BaseModel):
    """What the one offline entry point returns. Never a promotion, in any branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: OptimizationOutcome
    report: OptimizationRunReport
    candidate: PromptCandidateV1 | None = None

    @model_validator(mode="after")
    def _candidate_belongs_to_this_run(self) -> OptimizationResult:
        if self.outcome != self.report.outcome:
            raise ValueError("news_learning_optimize_result_outcome_mismatch")
        if (self.outcome == "ADVANCE") != (self.candidate is not None):
            raise ValueError("news_learning_optimize_result_outcome_mismatch")
        if self.candidate is not None and self.candidate.candidate_sha256 != self.report.candidate_sha256:
            raise ValueError("news_learning_optimize_result_candidate_mismatch")
        return self


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
    "OPTIMIZATION_RUN_REPORT_SCHEMA",
    "PROMPT_CANDIDATE_SCHEMA",
    "ArmManifest",
    "CandidateManifest",
    "ClosedWindow",
    "DatasetCaseRef",
    "DevelopmentDatasetRef",
    "OptimizationBudget",
    "OptimizationOutcome",
    "OptimizationResult",
    "OptimizationRunReport",
    "PromptCandidateV1",
    "PromptPatchV1",
    "ProposalReceipt",
]
