"""Production CandidateEvaluator for the News learning loop (#112).

The module owns dataset freezing, exact-one-variable validation, true semantic
arm execution, arm-local sequential reader ledgers, strict model recordings,
and sealed release evidence.  It never changes the active agent, delivery,
broker, or canary controls.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agents.semantic_program import SemanticJudge, SemanticProgramError, TriageContext
from .artifact_identity import canonical_json, canonical_sha
from .models import TriageVerdict
from .review import READER_CONTRACT_SHA256, READER_CONTRACT_VERSION, REVIEW_RUBRIC_VERSION
from .storyline import final_storyline_key
from .triage_rules import DecidePolicy, GateFacts, StorylineStatus, decide

LEARNING_PROFILE_ID: Literal["news_learning_release_v1"] = "news_learning_release_v1"
DATASET_VERSION: Literal["news_learning_dataset_v1"] = "news_learning_dataset_v1"
EVALUATOR_VERSION = "news_candidate_evaluator_v1"
LEARNING_EPOCH: Literal["program_v1"] = "program_v1"
# Issue #129 creation time. Prompt-era evidence remains immutable audit data but
# cannot enter the Program-native release denominator.
LEARNING_EPOCH_STARTED_AT_MS = 1_787_329_287_000
SETTLEMENT_GRACE_MS = 10 * 60_000
MODEL_RECORDING_BYTES_MAX = 64 * 1024

_PROFILE: dict[str, Any] = {
    "profile_id": LEARNING_PROFILE_ID,
    "learning_epoch": LEARNING_EPOCH,
    "learning_epoch_started_at_ms": LEARNING_EPOCH_STARTED_AT_MS,
    "development": {
        "boundary_clusters_min": 30,
        "retention_clusters_min": 100,
        "negative_clusters_min": 50,
        "natural_days_min": 3,
        "strata_min": 3,
        "safety_required": True,
    },
    "validation": {
        "duration_hours_min": 24,
        "eligible_events_min": 200,
        "planned_primary_clusters": 50,
        "primary_clusters_min": 30,
        "max_review_budget": 100,
    },
    "guardrails": {
        "mean_total_tokens_growth_pct": 0.10,
        "mean_call_growth_pct": 0.10,
        "mean_provider_cost_growth_pct": 0.10,
        "candidate_latency_p95_ms_max": 30_000,
        "candidate_degraded_or_error_rate_max": 0.05,
        "canary_candidate_min_n": 8,
        "critical_regressions": 0,
    },
    "bootstrap": {"seed": 112, "replicates": 2_000, "confidence": 0.95},
    "supported_candidates": ["program", "policy"],
}
TRUSTED_ROOT_SHA = hashlib.sha256(
    json.dumps(
        {
            "profile": _PROFILE,
            "rubric": REVIEW_RUBRIC_VERSION,
            "reader_contract_version": READER_CONTRACT_VERSION,
            "reader_contract_sha256": READER_CONTRACT_SHA256,
            "evaluator": EVALUATOR_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


class ClosedWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_ms: int = Field(ge=0)
    to_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> ClosedWindow:
        if self.to_ms <= self.from_ms:
            raise ValueError("news_learning_window_invalid")
        return self


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window: ClosedWindow
    role: Literal["development", "validation"]
    profile_id: Literal["news_learning_release_v1"] = LEARNING_PROFILE_ID
    learning_epoch: Literal["program_v1"] = LEARNING_EPOCH
    observation_ref: str | None = None


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


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_sha: str
    dataset_version: Literal["news_learning_dataset_v1"] = DATASET_VERSION
    role: Literal["development", "validation"]
    profile_id: str
    learning_epoch: Literal["program_v1"]
    window: ClosedWindow
    freeze_as_of_ms: int
    settlement_grace_ms: int
    reader_contract_version: str
    agent_cohort: dict[str, str]
    observation_ref: str | None = None
    cases: tuple[DatasetCaseRef, ...]
    seed_receipts: tuple[dict[str, Any], ...] = ()
    counts: dict[str, Any]
    hashes: dict[str, str]


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
    program_machine_diff: dict[str, Any] | None = None
    compile_provenance: dict[str, Any] | None = None
    holdout_access_attestation: Literal[False] = False

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


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    development_dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_dataset_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: Literal["offline", "holdout", "shadow", "canary"]
    observation_manifest_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validation_required_after_offline(self) -> EvaluationRequest:
        if self.stage != "offline" and self.validation_dataset_sha is None:
            raise ValueError("news_learning_validation_dataset_required")
        if self.stage in {"offline", "holdout"} and self.observation_manifest_sha is not None:
            raise ValueError("news_learning_production_observation_not_allowed")
        return self


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_sha: str
    run_sha: str
    run_state: Literal["running", "complete", "incomplete"]
    gate_outcome: Literal["pass", "fail", "unknown"]
    eligibility: Literal["current", "stale"]
    next_stage: Literal["holdout", "shadow", "canary", "promotion", "none"]
    recommended_action: Literal["advance", "hold", "reject", "rollback"]
    evidence: dict[str, Any]


class RecordReplayMiss(RuntimeError):
    pass


@dataclass(slots=True)
class _Receipt:
    event_id: str
    at_ms: int
    storyline_key: str
    magnitude: int
    direction: str
    headline_zh: str


@dataclass(slots=True)
class _ArmState:
    receipts: deque[_Receipt] = field(default_factory=deque)
    observations: list[dict[str, Any]] = field(default_factory=list)

    def expire(self, at_ms: int) -> None:
        cutoff = at_ms - 4 * 3_600_000
        while self.receipts and self.receipts[0].at_ms < cutoff:
            self.receipts.popleft()


class CandidateEvaluator:
    """Freeze reviewed evidence and compare stable/candidate; never publish."""

    def __init__(
        self,
        conn: Any,
        *,
        stable: ArmManifest,
        judges: Mapping[str, SemanticJudge],
        candidate_catalog: Sequence[CandidateManifest] = (),
        principal: str = "operator",
        trusted_root_sha: str = TRUSTED_ROOT_SHA,
    ) -> None:
        if not trusted_root_sha or trusted_root_sha != TRUSTED_ROOT_SHA:
            raise ValueError("news_learning_trusted_root_invalid")
        self._conn = conn
        self._stable = stable
        self._judges = dict(judges)
        self._candidates = {candidate.candidate_sha: candidate for candidate in candidate_catalog}
        self._principal = principal
        self._trusted_root_sha = trusted_root_sha

    async def freeze_dataset(self, spec: DatasetSpec) -> DatasetManifest:
        self._assert_active_stable()
        if spec.learning_epoch != LEARNING_EPOCH:
            raise ValueError("news_learning_epoch_mismatch")
        if spec.window.from_ms < LEARNING_EPOCH_STARTED_AT_MS:
            raise ValueError("news_learning_window_precedes_program_epoch")
        freeze_as_of_ms = self._db_now_ms()
        if spec.window.to_ms > freeze_as_of_ms - SETTLEMENT_GRACE_MS:
            raise ValueError("news_learning_window_not_settled")
        if spec.role == "validation":
            if not spec.observation_ref:
                raise ValueError("news_learning_validation_candidate_required")
            candidate = self._candidate(spec.observation_ref)
            self._validate_candidate_static(candidate)
            self._persist_candidate(candidate)
            registered_at_ms = max(
                candidate.proposal_receipt.registered_at_ms,
                self._candidate_registered_at(candidate.candidate_sha),
            )
            if spec.window.from_ms <= registered_at_ms:
                raise ValueError("news_learning_holdout_precedes_candidate_registration")
        elif spec.observation_ref is not None:
            raise ValueError("news_learning_development_observation_ref_not_allowed")

        cases = self._accepted_cases(spec.window, freeze_as_of_ms=freeze_as_of_ms)
        seed = self._seed_receipts(spec.window.from_ms)
        counts = self._dataset_counts(spec, cases)
        payload = {
            "dataset_version": DATASET_VERSION,
            "role": spec.role,
            "profile_id": spec.profile_id,
            "learning_epoch": spec.learning_epoch,
            "window": spec.window.model_dump(mode="json"),
            "freeze_as_of_ms": freeze_as_of_ms,
            "settlement_grace_ms": SETTLEMENT_GRACE_MS,
            "reader_contract_version": READER_CONTRACT_VERSION,
            "agent_cohort": self._agent_cohort(),
            "observation_ref": spec.observation_ref,
            "cases": [case.model_dump(mode="json") for case in cases],
            "seed_receipts": seed,
            "counts": counts,
            "hashes": {
                "trusted_root_sha": self._trusted_root_sha,
                "learning_epoch_sha": _sha({"epoch": LEARNING_EPOCH, "started_at_ms": LEARNING_EPOCH_STARTED_AT_MS}),
                "rubric_sha": _text_sha(REVIEW_RUBRIC_VERSION),
                "reader_contract_sha": READER_CONTRACT_SHA256,
                "agent_bundle_sha": self._stable.bundle_sha,
                "extraction_sha": _text_sha("news_learning_freeze_query_v1"),
            },
        }
        artifact_sha = self._persist_artifact("dataset", payload)
        return DatasetManifest(artifact_sha=artifact_sha, **payload)

    def development_compile_episodes(self, dataset_sha: str) -> tuple[dict[str, Any], ...]:
        """Expose accepted development truth to a cold Program compiler.

        This is the learning plane's only compiler-facing Interface. It keeps
        dataset/epoch validation and told-ledger construction inside the
        evaluator instead of making a CLI depend on private case loaders.
        """

        self._assert_active_stable()
        dataset = self._load_dataset(dataset_sha)
        if dataset.role != "development":
            raise ValueError("news_learning_compile_requires_development_dataset")
        if dataset.agent_cohort != self._agent_cohort():
            raise ValueError("news_learning_dataset_agent_cohort_mismatch")

        state = _ArmState(deque(_Receipt(**receipt) for receipt in dataset.seed_receipts))
        pending: list[_Receipt] = []
        episodes: list[dict[str, Any]] = []
        for case_ref in sorted(dataset.cases, key=lambda item: (item.opened_at_ms, item.case_id)):
            ready = [receipt for receipt in pending if receipt.at_ms <= case_ref.opened_at_ms]
            pending = [receipt for receipt in pending if receipt.at_ms > case_ref.opened_at_ms]
            state.receipts.extend(sorted(ready, key=lambda item: (item.at_ms, item.event_id)))
            state.expire(case_ref.opened_at_ms)
            case = self._load_case(case_ref)
            context = self._build_context(case, state)
            review = dict(case["review"])
            episodes.append(
                {
                    "case_id": case_ref.case_id,
                    "cluster_id": case_ref.cluster_id,
                    "stratum": case_ref.stratum,
                    "context": context.model_dump(mode="json"),
                    "accepted_review": {
                        "review_id": case_ref.review_id,
                        "should_push": review.get("should_push"),
                        "dimensions": dict(review.get("dimensions") or {}),
                        "novelty": dict(review.get("novelty") or {}),
                        "first_bad_owner": review.get("first_bad_owner"),
                        "evidence_refs": list(review.get("evidence_refs") or []),
                        "expected_correction": str(review.get("expected_correction") or ""),
                        "note": str(review.get("note") or ""),
                    },
                    "production_verdict": case.get("production_verdict"),
                }
            )
            verdict_payload = case.get("production_verdict")
            receipt_at_ms = case.get("receipt_at_ms")
            if case_ref.delivery_truth == "observed_sent" and verdict_payload and receipt_at_ms is not None:
                verdict = TriageVerdict.model_validate(verdict_payload)
                card = dict((case.get("snapshot") or {}).get("card") or {})
                pending.append(
                    _Receipt(
                        event_id=str(case_ref.event_id or case_ref.case_id),
                        at_ms=int(receipt_at_ms),
                        storyline_key=str(card.get("storyline_key") or "macro:general"),
                        magnitude=verdict.magnitude,
                        direction=verdict.direction,
                        headline_zh=verdict.headline_zh,
                    )
                )
        return tuple(episodes)

    async def evaluate(self, request: EvaluationRequest) -> EvaluationReport:
        # Reject a stale constructor arm before loading data or spending one
        # model call. Re-read after execution as well, because a deployment can
        # legitimately change the active root while a long evaluation runs.
        self._assert_active_stable()
        development = self._load_dataset(request.development_dataset_sha)
        validation = (
            development if request.stage == "offline" else self._load_dataset(str(request.validation_dataset_sha))
        )
        if development.role != "development" or (request.stage != "offline" and validation.role != "validation"):
            raise ValueError("news_learning_dataset_role_invalid")
        if development.agent_cohort != self._agent_cohort() or (
            request.stage != "offline" and validation.agent_cohort != self._agent_cohort()
        ):
            raise ValueError("news_learning_dataset_agent_cohort_mismatch")
        if development.reader_contract_version != READER_CONTRACT_VERSION or (
            request.stage != "offline" and validation.reader_contract_version != READER_CONTRACT_VERSION
        ):
            raise ValueError("news_learning_dataset_reader_contract_mismatch")
        candidate = self._candidate(request.candidate_sha)
        self._validate_candidate_static(candidate)
        self._persist_candidate(candidate)
        prior_stage = {"holdout": "offline", "shadow": "holdout", "canary": "shadow"}.get(request.stage)
        if prior_stage and not self._has_passed_stage(candidate.candidate_sha, prior_stage):
            raise ValueError(f"news_learning_prior_{prior_stage}_evidence_not_passed")
        if candidate.development_dataset_sha != development.artifact_sha:
            raise ValueError("news_learning_candidate_development_dataset_mismatch")
        if request.stage != "offline" and validation.observation_ref != candidate.candidate_sha:
            raise ValueError("news_learning_validation_candidate_mismatch")

        run_sha = _sha(
            {
                "request": request.model_dump(mode="json"),
                "stable": self._stable.bundle_sha,
                "candidate": candidate.candidate_sha,
                "trusted_root": self._trusted_root_sha,
                "evaluator": EVALUATOR_VERSION,
            }
        )
        dataset = development if request.stage == "offline" else validation
        existing = self._load_run_cases(run_sha)
        execution_errors: list[str] = []
        observation_dimensions: dict[str, Any] | None = None
        observation_manifest_sha = request.observation_manifest_sha
        if not existing:
            if request.stage in {"shadow", "canary"}:
                if request.observation_manifest_sha:
                    observations, observation_dimensions = self._load_production_observations(
                        artifact_sha=request.observation_manifest_sha,
                        stage=request.stage,
                        dataset=dataset,
                        candidate=candidate,
                    )
                else:
                    try:
                        if request.stage == "shadow":
                            observations, observation_dimensions = await self._run_shadow(
                                run_sha=run_sha,
                                dataset=dataset,
                                candidate=candidate,
                            )
                        else:
                            observations, observation_dimensions = self._collect_canary_observations(
                                dataset=dataset,
                                candidate=candidate,
                            )
                    except RecordReplayMiss as exc:
                        observations = []
                        execution_errors.append(str(exc))
            else:
                try:
                    observations = await self._run_sequential(
                        run_sha=run_sha,
                        dataset=dataset,
                        candidate=candidate,
                    )
                except RecordReplayMiss as exc:
                    observations = []
                    execution_errors.append(str(exc))
            if observations:
                self._persist_run_cases(run_sha, dataset, observations, stage=request.stage)
                existing = observations
            if request.stage in {"shadow", "canary"} and request.observation_manifest_sha is None:
                observation_manifest_sha = self._persist_observation_manifest(
                    run_sha=run_sha,
                    stage=request.stage,
                    dataset=dataset,
                    candidate=candidate,
                    observations=observations,
                    dimensions=observation_dimensions or {},
                )
        elif request.stage in {"shadow", "canary"}:
            if request.observation_manifest_sha:
                loaded, observation_dimensions = self._load_production_observations(
                    artifact_sha=request.observation_manifest_sha,
                    stage=request.stage,
                    dataset=dataset,
                    candidate=candidate,
                )
                if _observation_root(loaded) != _observation_root(existing):
                    raise ValueError("news_learning_production_observation_run_mismatch")
            else:
                observation_manifest_sha, observation_dimensions = self._generated_observation_manifest(
                    run_sha=run_sha,
                    stage=request.stage,
                    dataset=dataset,
                    candidate=candidate,
                    observations=existing,
                )

        evidence = self._evaluate_evidence(
            request=request,
            development=development,
            validation=validation,
            candidate=candidate,
            run_sha=run_sha,
            observations=existing,
            execution_errors=execution_errors,
            observation_dimensions=observation_dimensions,
        )
        if observation_manifest_sha:
            evidence["observation_manifest_sha"] = observation_manifest_sha
        outcome = str(evidence["gate_outcome"])
        active_sha = self._active_stable_sha()
        eligibility = "current" if active_sha == candidate.parent_stable_sha else "stale"
        if eligibility == "stale":
            outcome = "unknown"
            evidence["blockers"].append("active_stable_changed")
        run_state = (
            "incomplete"
            if execution_errors or not existing or bool(evidence.get("execution_incomplete"))
            else "complete"
        )
        if outcome == "pass":
            if request.stage == "offline":
                next_stage, action = "holdout", "advance"
            elif request.stage == "holdout":
                next_stage, action = "shadow", "advance"
            elif request.stage == "shadow":
                next_stage, action = "canary", "advance"
            else:
                next_stage, action = "promotion", "advance"
        elif outcome == "fail":
            next_stage, action = "none", "reject" if request.stage != "canary" else "rollback"
        else:
            next_stage, action = "none", "hold"
        report_payload = {
            "run_sha": run_sha,
            "run_state": run_state,
            "gate_outcome": outcome,
            "eligibility": eligibility,
            "next_stage": next_stage,
            "recommended_action": action,
            "evidence": evidence,
        }
        report_sha = self._persist_artifact("evaluation_report", report_payload, parent_sha=candidate.candidate_sha)
        self._persist_artifact(
            "release_evidence",
            {
                "report_sha": report_sha,
                "run_sha": run_sha,
                "candidate_sha": candidate.candidate_sha,
                "gate_outcome": outcome,
                "stage": request.stage,
                "trusted_root_sha": self._trusted_root_sha,
            },
            parent_sha=report_sha,
        )
        return EvaluationReport(report_sha=report_sha, **report_payload)

    def _validate_candidate_static(self, candidate: CandidateManifest) -> None:
        if candidate.parent_stable_sha != self._stable.bundle_sha:
            raise ValueError("news_learning_candidate_parent_stable_mismatch")
        stable = self._stable.model_dump(mode="json")
        proposed = candidate.candidate_arm.model_dump(mode="json")
        changed = {key for key in stable if stable[key] != proposed[key]}
        allowed = (
            {"program_version", "program_sha256"} if candidate.target == "program" else {"policy", "policy_sha256"}
        )
        if not changed or not changed <= allowed:
            raise ValueError(f"news_learning_exact_one_variable_violation:{','.join(sorted(changed))}")
        receipt = candidate.proposal_receipt
        if candidate.target == "program":
            if candidate.candidate_arm.program_sha256 == self._stable.program_sha256:
                raise ValueError("news_learning_program_sha_unchanged")
            if receipt.program_parent_sha256 != self._stable.program_sha256:
                raise ValueError("news_learning_program_parent_mismatch")
            if receipt.program_candidate_sha256 != candidate.candidate_arm.program_sha256:
                raise ValueError("news_learning_program_candidate_mismatch")
            if not receipt.program_machine_diff:
                raise ValueError("news_learning_program_machine_diff_required")
            provenance = dict(receipt.compile_provenance or {})
            if provenance.get("development_dataset_sha") != candidate.development_dataset_sha:
                raise ValueError("news_learning_program_compile_dataset_mismatch")
            expected_patch_sha = _sha(
                {
                    "parent_program_sha256": receipt.program_parent_sha256,
                    "candidate_program_sha256": receipt.program_candidate_sha256,
                    "machine_diff": receipt.program_machine_diff,
                    "compile_provenance": provenance,
                }
            )
            if receipt.candidate_patch_sha != expected_patch_sha:
                raise ValueError("news_learning_program_patch_sha_mismatch")
        elif any(
            value is not None
            for value in (
                receipt.program_parent_sha256,
                receipt.program_candidate_sha256,
                receipt.program_machine_diff,
                receipt.compile_provenance,
            )
        ):
            raise ValueError("news_learning_policy_receipt_contains_program_change")
        if candidate.development_dataset_sha != candidate.proposal_receipt.development_dataset_sha:
            raise ValueError("news_learning_proposal_dataset_mismatch")
        if tuple(candidate.target_dimensions) != tuple(candidate.proposal_receipt.declared_target_dimensions):
            raise ValueError("news_learning_target_dimensions_mismatch")
        development = self._load_dataset(candidate.development_dataset_sha)
        if development.role != "development":
            raise ValueError("news_learning_proposal_requires_development_dataset")
        reviews = self._reviews_by_id([case.review_id for case in development.cases])
        failure_clusters = {
            case.cluster_id
            for case in development.cases
            if (
                _case_delivery_failed(case)
                or "fail" in dict(reviews.get(case.review_id, {}).get("dimensions") or {}).values()
                or bool(reviews.get(case.review_id, {}).get("expected_correction"))
            )
        }
        declared_clusters = set(candidate.proposal_receipt.failure_cluster_ids)
        if not declared_clusters <= failure_clusters:
            unknown = ",".join(sorted(declared_clusters - failure_clusters))
            raise ValueError(f"news_learning_proposal_failure_cluster_unverified:{unknown}")
        self._verify_registration_receipt(candidate.proposal_receipt)

    def _accepted_cases(self, window: ClosedWindow, *, freeze_as_of_ms: int) -> tuple[DatasetCaseRef, ...]:
        rows = self._conn.execute(
            """
            WITH accepted AS (
              SELECT DISTINCT ON (j.event_id) j.*, a.created_at_ms AS accepted_at_ms
                FROM news_reviews a
                JOIN news_reviews j ON j.review_id = a.accepts_review_id
               WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'event'
                 AND a.release_eligible AND j.release_eligible
                 AND a.created_at_ms >= %s AND j.created_at_ms >= %s
                 AND a.created_at_ms <= %s AND j.rubric_version = %s
                 AND j.reader_contract_version = %s
               ORDER BY j.event_id, a.created_at_ms DESC, a.review_id DESC
            )
            SELECT accepted.*, source.evidence_sha256, source.opened_at_ms,
                   source.final_decision, source.delivery_state, source.evidence_release_eligible,
                   source.evidence_snapshot
              FROM accepted
              JOIN news_review_task_source_v1 source
                ON source.event_id = accepted.event_id
               AND source.evidence_version = accepted.evidence_version
             WHERE source.opened_at_ms >= %s AND source.opened_at_ms < %s
               AND source.ingest_mode = 'live' AND source.evidence_release_eligible
               AND source.trace #>> '{agent_assignment,bundle_sha}' = %s
               AND NOT (
                 source.final_decision IN ('push', 'escalate')
                 AND COALESCE(source.delivery_state, '') NOT IN ('sent', 'terminal')
               )
               AND NOT (
                 source.delivery_state = 'terminal'
                 AND source.delivery_error_code = 'ambiguous_after_crash'
               )
            """,
            (
                LEARNING_EPOCH_STARTED_AT_MS,
                LEARNING_EPOCH_STARTED_AT_MS,
                freeze_as_of_ms,
                REVIEW_RUBRIC_VERSION,
                READER_CONTRACT_VERSION,
                window.from_ms,
                window.to_ms,
                self._stable.bundle_sha,
            ),
        ).fetchall()
        external = self._conn.execute(
            """
            SELECT DISTINCT ON (j.external_snapshot_id) j.*, a.created_at_ms AS accepted_at_ms,
                   x.evidence_sha256, x.occurred_at_ms AS opened_at_ms, x.snapshot AS evidence_snapshot
              FROM news_reviews a
              JOIN news_reviews j ON j.review_id = a.accepts_review_id
              JOIN news_external_miss_snapshots x ON x.snapshot_id = j.external_snapshot_id
             WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'external_miss'
               AND a.created_at_ms >= %s AND j.created_at_ms >= %s
               AND a.created_at_ms <= %s AND j.rubric_version = %s
               AND j.reader_contract_version = %s
               AND x.occurred_at_ms >= %s AND x.occurred_at_ms < %s
             ORDER BY j.external_snapshot_id, a.created_at_ms DESC, a.review_id DESC
            """,
            (
                LEARNING_EPOCH_STARTED_AT_MS,
                LEARNING_EPOCH_STARTED_AT_MS,
                freeze_as_of_ms,
                REVIEW_RUBRIC_VERSION,
                READER_CONTRACT_VERSION,
                window.from_ms,
                window.to_ms,
            ),
        ).fetchall()
        drafts: list[tuple[DatasetCaseRef, str, str]] = []
        for row in [*rows, *external]:
            subject_kind = str(row["subject_kind"])
            snapshot = dict(row["evidence_snapshot"] or {})
            text = (
                str((snapshot.get("focus_fact") or {}).get("text") or "")
                if subject_kind == "event"
                else str(snapshot.get("title") or "")
            )
            cluster_id = _fact_cluster(text)
            selection = dict(row.get("selection") or {})
            case_id = _sha(
                {
                    "subject_kind": subject_kind,
                    "event_id": row.get("event_id"),
                    "external_snapshot_id": row.get("external_snapshot_id"),
                    "evidence_sha256": row["evidence_sha256"],
                    "review_id": row["review_id"],
                }
            )
            case = DatasetCaseRef(
                case_id=case_id,
                subject_kind=subject_kind,
                event_id=row.get("event_id"),
                evidence_version=row.get("evidence_version"),
                external_snapshot_id=row.get("external_snapshot_id"),
                evidence_sha256=row["evidence_sha256"],
                review_id=row["review_id"],
                cluster_id=cluster_id,
                stratum=str(selection.get("stratum") or "eventless_miss"),
                should_push=str(row.get("should_push") or "uncertain"),
                opened_at_ms=int(row["opened_at_ms"]),
                delivery_truth=(
                    "observed_not_sent"
                    if subject_kind == "external_miss"
                    else "observed_sent"
                    if str(row.get("delivery_state") or "") == "sent"
                    else "observed_not_sent"
                ),
            )
            novelty = dict(row.get("novelty") or {})
            duplicate_of = (
                str(novelty.get("duplicate_of") or "") if str(novelty.get("judgment") or "") == "restatement" else ""
            )
            if subject_kind == "event":
                source_identity = _sha(
                    {
                        "url": (snapshot.get("card") or {}).get("leader_url"),
                        "focus_fact_id": (snapshot.get("focus_fact") or {}).get("fact_id"),
                    }
                )
            else:
                source_identity = _sha({"url": snapshot.get("source_url"), "title": snapshot.get("title")})
            drafts.append((case, duplicate_of, source_identity))
        cases = _connected_fact_clusters(drafts)
        cases.sort(key=lambda case: (case.opened_at_ms, case.case_id))
        return tuple(cases)

    def _dataset_counts(self, spec: DatasetSpec, cases: Sequence[DatasetCaseRef]) -> dict[str, Any]:
        reviews = self._reviews_by_id([case.review_id for case in cases])
        boundary: set[str] = set()
        retention: set[str] = set()
        negative: set[str] = set()
        safety: set[str] = set()
        strata: set[str] = set()
        days: set[int] = set()
        for case in cases:
            review = reviews.get(case.review_id, {})
            dimensions = dict(review.get("dimensions") or {})
            is_boundary = (
                case.should_push in {"must_push", "must_hold"}
                or "fail" in dimensions.values()
                or bool(review.get("expected_correction"))
            )
            (boundary if is_boundary else retention).add(case.cluster_id)
            if (
                case.should_push in {"should_hold", "must_hold"}
                or (review.get("novelty") or {}).get("judgment") == "restatement"
            ):
                negative.add(case.cluster_id)
            if case.should_push in {"must_push", "must_hold"} or dimensions.get("factual_fidelity") == "fail":
                safety.add(case.cluster_id)
            strata.add(case.stratum)
            days.add(case.opened_at_ms // 86_400_000)
        eligible = self._conn.execute(
            "SELECT count(*) AS n FROM news_review_task_source_v1 "
            "WHERE opened_at_ms >= %s AND opened_at_ms < %s AND ingest_mode = 'live' "
            "AND trace #>> '{agent_assignment,bundle_sha}' = %s",
            (spec.window.from_ms, spec.window.to_ms, self._stable.bundle_sha),
        ).fetchone()
        return {
            "case_n": len(cases),
            "independent_cluster_n": len({case.cluster_id for case in cases}),
            "boundary_cluster_n": len(boundary),
            "retention_cluster_n": len(retention),
            "negative_cluster_n": len(negative),
            "safety_cluster_n": len(safety),
            "natural_day_n": len(days),
            "stratum_n": len(strata),
            "strata": sorted(strata),
            "eligible_event_n": int(eligible["n"] or 0),
            "window_duration_hours": round((spec.window.to_ms - spec.window.from_ms) / 3_600_000, 3),
        }

    def _seed_receipts(self, from_ms: int) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            """
            SELECT v.event_id, d.settled_at_ms AS at_ms, e.storyline_key,
                   COALESCE((v.verdict ->> 'magnitude')::int, 0) AS magnitude,
                   COALESCE(v.verdict ->> 'direction', 'unclear') AS direction,
                   COALESCE(NULLIF(d.card #>> '{header,title,content}', ''), v.verdict ->> 'headline_zh', '')
                     AS headline_zh
              FROM news_deliveries d
              JOIN news_verdicts v ON v.event_id = d.event_id AND v.stage = 'triage'
              JOIN news_events e ON e.event_id = d.event_id
             WHERE d.kind = 'first' AND d.state = 'sent'
               AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
             ORDER BY d.settled_at_ms, v.event_id
            """,
            (from_ms - 4 * 3_600_000, from_ms),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    async def _run_sequential(
        self,
        *,
        run_sha: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
    ) -> list[dict[str, Any]]:
        states = {
            "stable": _ArmState(deque(_Receipt(**receipt) for receipt in dataset.seed_receipts)),
            "candidate": _ArmState(deque(_Receipt(**receipt) for receipt in dataset.seed_receipts)),
        }
        arms = {"stable": self._stable, "candidate": candidate.candidate_arm}
        review_case_ids = self._review_case_ids(dataset, candidate=candidate)
        observations: list[dict[str, Any]] = []
        for case_ref in dataset.cases:
            case = self._load_case(case_ref)
            case_outputs: dict[str, dict[str, Any]] = {}
            order = ["stable", "candidate"]
            if int(case_ref.case_id[:2], 16) % 2:
                order.reverse()
            for arm_name in order:
                state = states[arm_name]
                state.expire(case_ref.opened_at_ms)
                arm = arms[arm_name]
                # The development pass is the cheap, zero-model policy screen.
                # A hidden holdout must call the same SemanticJudge separately
                # for each arm because their simulated reader ledgers can
                # diverge and therefore change the next model input.
                frozen_policy_screen = candidate.target == "policy" and dataset.role == "development"
                if frozen_policy_screen:
                    verdict = case.get("production_verdict")
                    program_observations: list[dict[str, Any]] = []
                    if verdict is None:
                        case_outputs[arm_name] = {"error_code": "frozen_verdict_missing", "delivered": False}
                        continue
                else:
                    context = self._build_context(case, state)
                    first = await self._invoke_and_record(
                        run_sha=run_sha,
                        case_id=case_ref.case_id,
                        arm_name=arm_name,
                        arm=arm,
                        context=context,
                        trial=1,
                    )
                    program_observations = [first]
                    verdict = first.get("verdict")
                    if verdict is None:
                        case_outputs[arm_name] = {
                            "error_code": first.get("error_code") or "program_output_missing",
                            "delivered": False,
                            "program": [first],
                        }
                        continue
                result = self._apply_policy(case, verdict, state, arm)
                result["program"] = list(program_observations)
                case_outputs[arm_name] = result
            # A pre-registered stability subset plus every first-trial disagreement gets k=3.
            if candidate.target != "policy" and self._needs_stability_trials(case_ref.case_id, case_outputs):
                for arm_name in order:
                    if not case_outputs.get(arm_name, {}).get("verdict"):
                        continue
                    state = states[arm_name]
                    context = self._build_context(case, state)
                    trials = [
                        await self._invoke_and_record(
                            run_sha=run_sha,
                            case_id=case_ref.case_id,
                            arm_name=arm_name,
                            arm=arms[arm_name],
                            context=context,
                            trial=trial,
                        )
                        for trial in (2, 3)
                    ]
                    first_verdict = case_outputs[arm_name]["verdict"]
                    serialized_observations: list[dict[str, Any]] = list(case_outputs[arm_name].get("program") or [])
                    serialized_observations.extend(trials)
                    case_outputs[arm_name]["program"] = serialized_observations
                    trial_results: list[dict[str, Any]] = [
                        {
                            "trial": 1,
                            "error_code": case_outputs[arm_name].get("error_code"),
                            "delivered": bool(case_outputs[arm_name].get("delivered")),
                            "verdict_sha256": _sha(first_verdict),
                        }
                    ]
                    for trial_number, trial_observation in zip((2, 3), trials, strict=True):
                        trial_result: dict[str, Any] = {
                            "trial": trial_number,
                            "error_code": trial_observation.get("error_code"),
                            "delivered": False,
                            "verdict_sha256": None,
                        }
                        if trial_observation.get("verdict") is not None:
                            replayed = self._apply_policy(
                                case,
                                trial_observation["verdict"],
                                state,
                                arms[arm_name],
                            )
                            trial_result.update(
                                delivered=bool(replayed.get("delivered")),
                                verdict_sha256=_sha(trial_observation["verdict"]),
                            )
                        trial_results.append(trial_result)
                    expected_delivery = _expected_delivery(case_ref.should_push)
                    pass_n = (
                        None
                        if expected_delivery is None
                        else sum(bool(result["delivered"]) == expected_delivery for result in trial_results)
                    )
                    case_outputs[arm_name]["stability"] = {
                        "trials": 3,
                        "agreement_n": 1
                        + sum(
                            item.get("verdict") == first_verdict for item in trials if item.get("verdict") is not None
                        ),
                        "pass_n": pass_n,
                        "pass_k": None if pass_n is None else pass_n == 3,
                        "trial_results": trial_results,
                    }
            for arm_name, state in states.items():
                output = case_outputs.get(arm_name) or {"error_code": "arm_missing", "delivered": False}
                if output.get("delivered"):
                    verdict = output["verdict"]
                    state.receipts.append(
                        _Receipt(
                            event_id=case_ref.case_id,
                            at_ms=case_ref.opened_at_ms,
                            storyline_key=str(output["storyline_key"]),
                            magnitude=int(verdict.get("magnitude") or 0),
                            direction=str(verdict.get("direction") or "unclear"),
                            headline_zh=str(verdict.get("headline_zh") or ""),
                        )
                    )
                state.observations.append(output)
            pair_order = "candidate_A" if int(case_ref.case_id[-2:], 16) % 2 else "stable_A"
            observations.append(
                {
                    "case_ref": case_ref.model_dump(mode="json"),
                    "stable": case_outputs.get("stable") or {},
                    "candidate": case_outputs.get("candidate") or {},
                    "comparison": {
                        "pair_order": pair_order,
                        "blind_task_version": "news_blind_pairwise_v1",
                        "review_eligible": case_ref.case_id in review_case_ids,
                        "review_plan_sha": _sha(
                            {
                                "profile_id": LEARNING_PROFILE_ID,
                                "dataset_sha": dataset.artifact_sha,
                                "case_ids": sorted(review_case_ids),
                            }
                        ),
                        "outcome_revealed": False,
                    },
                }
            )
        return observations

    @staticmethod
    def _review_case_ids(dataset: DatasetManifest, *, candidate: CandidateManifest) -> frozenset[str]:
        """Freeze the human review batch without looking at either arm output.

        Development Program replay remains a diagnostic screen, so every
        independent reviewed case is exposed.  Hidden validation pre-registers
        one deterministic representative for at most the profile's planned
        number of fact clusters.  Policy candidates use accepted should-push
        truth directly and do not create copy-preference work.
        """

        if candidate.target != "program":
            return frozenset()
        by_cluster: dict[str, DatasetCaseRef] = {}
        for case in dataset.cases:
            current = by_cluster.get(case.cluster_id)
            if current is None or (case.opened_at_ms, case.case_id) < (current.opened_at_ms, current.case_id):
                by_cluster[case.cluster_id] = case
        if dataset.role == "development":
            return frozenset(case.case_id for case in by_cluster.values())
        planned = int(_PROFILE["validation"]["planned_primary_clusters"])
        ranked = sorted(
            by_cluster.values(),
            key=lambda case: (
                _sha(
                    {
                        "seed": int(_PROFILE["bootstrap"]["seed"]),
                        "dataset_sha": dataset.artifact_sha,
                        "cluster_id": case.cluster_id,
                    }
                ),
                case.case_id,
            ),
        )
        return frozenset(case.case_id for case in ranked[:planned])

    async def _run_shadow(
        self,
        *,
        run_sha: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Cold-run the candidate over the whole closed production distribution.

        Stable output and delivery are observed production facts. Candidate
        output uses a private counterfactual reader ledger and can only write
        learning artifacts/model recordings.
        """

        rows = self._conn.execute(
            """
            SELECT *
              FROM news_review_task_source_v1
             WHERE opened_at_ms >= %s AND opened_at_ms < %s
               AND ingest_mode = 'live'
               AND admission IN ('candidate', 'listing_deterministic')
               AND evidence_release_eligible
               AND verdict IS NOT NULL
             ORDER BY opened_at_ms, event_id, evidence_version
            """,
            (dataset.window.from_ms, dataset.window.to_ms),
        ).fetchall()
        state = _ArmState(deque(_Receipt(**receipt) for receipt in dataset.seed_receipts))
        observations: list[dict[str, Any]] = []
        for row in rows:
            opened_at_ms = int(row["opened_at_ms"])
            state.expire(opened_at_ms)
            snapshot = dict(row["evidence_snapshot"] or {})
            focus = dict(snapshot.get("focus_fact") or {})
            case_id = _sha(
                {
                    "shadow": EVALUATOR_VERSION,
                    "event_id": row["event_id"],
                    "evidence_version": row["evidence_version"],
                    "evidence_sha256": row["evidence_sha256"],
                }
            )
            case_ref = {
                "case_id": case_id,
                "subject_kind": "event",
                "event_id": row["event_id"],
                "evidence_version": row["evidence_version"],
                "external_snapshot_id": None,
                "evidence_sha256": row["evidence_sha256"],
                "review_id": None,
                "cluster_id": _fact_cluster(str(focus.get("text") or case_id)),
                "stratum": "shadow_distribution",
                "opened_at_ms": opened_at_ms,
            }
            case = {"snapshot": snapshot, "opened_at_ms": opened_at_ms}
            context = self._build_context(case, state)
            program_observation = await self._invoke_and_record(
                run_sha=run_sha,
                case_id=case_id,
                arm_name="candidate",
                arm=candidate.candidate_arm,
                context=context,
                trial=1,
            )
            if program_observation.get("verdict") is None:
                candidate_output: dict[str, Any] = {
                    "error_code": program_observation.get("error_code") or "program_output_missing",
                    "delivered": False,
                    "execution": "live",
                    "delivery": "simulated",
                    "program": [program_observation],
                }
            else:
                candidate_output = self._apply_policy(
                    case,
                    program_observation["verdict"],
                    state,
                    candidate.candidate_arm,
                )
                candidate_output["execution"] = "live"
                candidate_output["program"] = [program_observation]
            if candidate_output.get("delivered"):
                verdict = dict(candidate_output.get("verdict") or {})
                state.receipts.append(
                    _Receipt(
                        event_id=case_id,
                        at_ms=opened_at_ms,
                        storyline_key=str(candidate_output.get("storyline_key") or "macro:general"),
                        magnitude=int(verdict.get("magnitude") or 0),
                        direction=str(verdict.get("direction") or "unclear"),
                        headline_zh=str(verdict.get("headline_zh") or ""),
                    )
                )
            observations.append(
                {
                    "case_ref": case_ref,
                    "stable": _observed_production_output(row),
                    "candidate": candidate_output,
                    "comparison": {
                        "evaluation_stage": "shadow",
                        "reviewable": False,
                        "pairing": "observed_stable_vs_candidate_counterfactual",
                        "outcome_revealed": False,
                    },
                }
            )
        dimensions = {
            "input_provenance": "live",
            "execution": "live",
            "delivery": "simulated",
            "review": "none",
            "dataset_role": "hidden_temporal_holdout",
            "pairing": "observed_stable_vs_candidate_counterfactual",
            "outcome_revealed": False,
            "supported_claims": ["runtime_safety", "distribution", "counterfactual_delivery"],
            "observation_scope": "all_live_triage_eligible",
            "window_duration_hours": (dataset.window.to_ms - dataset.window.from_ms) / 3_600_000,
            "eligible_event_n": len(rows),
        }
        return observations, dimensions

    def _collect_canary_observations(
        self,
        *,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Read one-arm production assignment/verdict/receipt facts for canary."""

        activation = self._conn.execute(
            "SELECT * FROM news_canary_activations "
            "WHERE candidate_manifest_sha = %s ORDER BY created_at_ms DESC LIMIT 1",
            (candidate.candidate_sha,),
        ).fetchone()
        if activation is None:
            raise ValueError("news_learning_canary_activation_not_found")
        if str(activation["candidate_bundle_sha"]) != candidate.candidate_arm.bundle_sha:
            raise ValueError("news_learning_canary_candidate_bundle_mismatch")
        if str(activation["baseline_bundle_sha"]) != self._stable.bundle_sha:
            raise ValueError("news_learning_canary_stable_bundle_mismatch")
        rows = self._conn.execute(
            """
            SELECT a.arm, a.bundle_sha, a.selector_version, a.eligibility_reason,
                   a.assigned_at_ms, e.event_id, e.opened_at_ms,
                   s.evidence_version, s.evidence_sha256, s.snapshot AS evidence_snapshot,
                   v.verdict, v.final_decision, v.degraded, v.error_code AS verdict_error_code,
                   v.trace, v.program_version, v.program_sha256,
                   d.state AS delivery_state, d.error_code AS delivery_error_code, d.settled_at_ms
              FROM news_agent_assignments a
              JOIN news_events e ON e.event_id = a.event_id
              LEFT JOIN LATERAL (
                SELECT x.* FROM news_verdicts x
                 WHERE x.event_id = e.event_id AND x.stage = 'triage'
                 ORDER BY x.created_at_ms DESC LIMIT 1
              ) v ON true
              LEFT JOIN LATERAL (
                SELECT x.* FROM news_event_evidence_snapshots x
                 WHERE x.event_id = e.event_id
                   AND x.evidence_version = COALESCE(
                     v.evidence_version,
                     (SELECT max(z.evidence_version) FROM news_event_evidence_snapshots z
                       WHERE z.event_id = e.event_id)
                   )
              ) s ON true
              LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
             WHERE a.activation_id = %s
               AND e.opened_at_ms >= %s AND e.opened_at_ms < %s
             ORDER BY e.opened_at_ms, e.event_id
            """,
            (activation["activation_id"], dataset.window.from_ms, dataset.window.to_ms),
        ).fetchall()
        observations: list[dict[str, Any]] = []
        invariant_breaches: list[str] = []
        candidate_n = 0
        for row in rows:
            arm = str(row["arm"])
            expected_bundle = candidate.candidate_arm.bundle_sha if arm == "candidate" else self._stable.bundle_sha
            trace = dict(row.get("trace") or {})
            assignment_trace = dict(trace.get("agent_assignment") or {})
            if str(row["bundle_sha"]) != expected_bundle or (
                assignment_trace
                and (
                    str(assignment_trace.get("arm") or "") != arm
                    or str(assignment_trace.get("bundle_sha") or "") != expected_bundle
                )
            ):
                invariant_breaches.append(str(row["event_id"]))
            candidate_n += arm == "candidate"
            snapshot = dict(row.get("evidence_snapshot") or {})
            focus = dict(snapshot.get("focus_fact") or {})
            case_id = _sha(
                {
                    "canary": activation["activation_id"],
                    "event_id": row["event_id"],
                    "bundle_sha": row["bundle_sha"],
                }
            )
            observed = _observed_production_output(row)
            observations.append(
                {
                    "case_ref": {
                        "case_id": case_id,
                        "subject_kind": "event",
                        "event_id": row["event_id"],
                        "evidence_version": row.get("evidence_version"),
                        "external_snapshot_id": None,
                        "evidence_sha256": row.get("evidence_sha256") or "0" * 64,
                        "review_id": None,
                        "cluster_id": _fact_cluster(str(focus.get("text") or case_id)),
                        "stratum": f"canary_{arm}",
                        "opened_at_ms": int(row["opened_at_ms"]),
                    },
                    "stable": observed if arm == "stable" else {"not_assigned": True},
                    "candidate": observed if arm == "candidate" else {"not_assigned": True},
                    "comparison": {
                        "evaluation_stage": "canary",
                        "reviewable": False,
                        "assigned_arm": arm,
                        "activation_id": activation["activation_id"],
                        "outcome_revealed": False,
                    },
                }
            )
        ended_at_ms = next(
            (
                int(activation[field])
                for field in ("closed_at_ms", "tripped_at_ms")
                if activation.get(field) is not None
            ),
            self._db_now_ms(),
        )
        observed_from = max(
            int(activation["activated_at_ms"] or activation["created_at_ms"]),
            dataset.window.from_ms,
        )
        observed_until = min(ended_at_ms, dataset.window.to_ms)
        dimensions = {
            "input_provenance": "live",
            "execution": "live",
            "delivery": "observed",
            "review": "none",
            "dataset_role": "hidden_temporal_holdout",
            "pairing": "unpaired",
            "outcome_revealed": False,
            "supported_claims": ["runtime_safety"],
            "activation_id": activation["activation_id"],
            "activation_state": activation["state"],
            "candidate_assignment_n": candidate_n,
            "stable_assignment_n": len(rows) - candidate_n,
            "assignment_invariant_breach_event_ids": invariant_breaches,
            "window_duration_hours": max(0, observed_until - observed_from) / 3_600_000,
        }
        return observations, dimensions

    def _build_context(self, case: Mapping[str, Any], state: _ArmState) -> TriageContext:
        snapshot = case["snapshot"]
        event = dict(snapshot.get("card") or {})
        focus = dict(snapshot.get("focus_fact") or {})
        event["focus_fact_id"] = focus.get("fact_id")
        event["leader_title"] = focus.get("text") or event.get("leader_title")
        event["leader_description"] = focus.get("context") or event.get("leader_description")
        told_rows = [
            {
                "event_id": receipt.event_id,
                "at_ms": receipt.at_ms,
                "storyline_key": receipt.storyline_key,
                "magnitude": receipt.magnitude,
                "direction": receipt.direction,
                "headline_zh": receipt.headline_zh,
            }
            for receipt in reversed(state.receipts)
        ]
        return TriageContext.from_card(
            event,
            watchlist=tuple(str(value) for value in case.get("watchlist") or ()),
            told_rows=told_rows,
            now_ms=int(case["opened_at_ms"]),
            queue_lag_ms=0,
        )

    def _apply_policy(
        self, case: Mapping[str, Any], raw_verdict: Mapping[str, Any], state: _ArmState, arm: ArmManifest
    ) -> dict[str, Any]:
        try:
            verdict = TriageVerdict.model_validate(raw_verdict)
        except Exception as exc:
            return {"error_code": f"schema_invalid:{type(exc).__name__}", "delivered": False}
        snapshot = case["snapshot"]
        event = dict(snapshot.get("card") or {})
        grounded = tuple(str(value) for value in event.get("grounded_assets") or [])
        primaries = [asset.symbol for asset in verdict.assets if asset.role == "primary"]
        storyline = final_storyline_key(
            title=str(event.get("leader_title") or ""),
            headline_zh=verdict.headline_zh,
            scope=verdict.scope,
            verdict_primaries=primaries,
            grounded_assets=grounded,
            family=str(event.get("family") or "general"),
        )
        status = self._status(state, storyline)
        facts = GateFacts(
            grounded_assets=grounded,
            watchlist_symbols=frozenset(str(value) for value in case.get("watchlist") or ()),
            provider_score=event.get("provider_score_max"),
            priority=str(event.get("priority") or "normal"),
            admission=str(event.get("admission") or "candidate"),
        )
        decision = decide(
            verdict,
            facts,
            status,
            muted=bool((case.get("control") or {}).get("muted")),
            policy=DecidePolicy(**arm.policy),
        )
        paused = bool((case.get("control") or {}).get("paused"))
        delivered = decision.final in {"push", "escalate"} and not paused
        return {
            "verdict": verdict.model_dump(mode="json"),
            "final_decision": decision.final,
            "override_rule": decision.override_rule,
            "throttled_by": decision.throttled_by,
            "storyline_key": storyline,
            "delivered": delivered,
            "execution": "simulated",
            "delivery": "paused_drop" if paused and decision.final in {"push", "escalate"} else "simulated",
        }

    @staticmethod
    def _status(state: _ArmState, storyline_key: str) -> StorylineStatus:
        seen = list(reversed(state.receipts))
        return StorylineStatus(
            key=storyline_key,
            told_directions=tuple(r.direction for r in seen[:12]),
            seen_headlines=tuple(r.headline_zh for r in seen),
            seen_event_ids=tuple(r.event_id for r in seen),
            seen_directions=tuple(r.direction for r in seen),
        )

    async def _invoke_and_record(
        self,
        *,
        run_sha: str,
        case_id: str,
        arm_name: str,
        arm: ArmManifest,
        context: TriageContext,
        trial: int,
    ) -> dict[str, Any]:
        judge = self._judges.get(arm.program_sha256)
        if judge is None:
            return {
                "verdict": None,
                "program_version": arm.program_version,
                "program_sha256": arm.program_sha256,
                "runtime_model_bindings_sha256": arm.runtime_model_bindings_sha256,
                "trace": {},
                "usage": {},
                "calls": [],
                "error_code": "news_program_artifact_missing",
            }
        observation: dict[str, Any]
        try:
            judgment = await judge.judge(context)
        except SemanticProgramError as exc:
            if "recording_missing" in exc.code:
                raise RecordReplayMiss(exc.code) from exc
            partial_trace = exc.partial_trace.model_dump(mode="json") if exc.partial_trace is not None else {}
            observation = {
                "verdict": None,
                "program_version": arm.program_version,
                "program_sha256": arm.program_sha256,
                "trace": partial_trace,
                "usage": _usage_from_trace(partial_trace),
                "calls": list(partial_trace.get("calls") or []),
                "error_code": exc.code,
                "retryable": exc.retryable,
                "output_failure": exc.output_failure,
                "attempts": exc.attempts,
            }
        else:
            if (
                judgment.program_version != arm.program_version
                or judgment.program_sha256 != arm.program_sha256
                or judgment.trace.program_version != arm.program_version
                or judgment.trace.program_sha256 != arm.program_sha256
            ):
                raise ValueError("news_program_judgment_identity_mismatch")
            observation = judgment.model_dump(mode="json")
            observation["verdict"] = judgment.verdict.model_dump(mode="json")
            observation["calls"] = list(observation.get("trace", {}).get("calls") or [])
            observation["error_code"] = None
        observation["runtime_model_bindings_sha256"] = arm.runtime_model_bindings_sha256

        context_payload = context.model_dump(mode="json")
        context_sha = _sha(context_payload)
        trace = dict(observation.get("trace") or {})
        trace_context_sha = str(trace.get("context_sha256") or context_sha)
        if trace_context_sha != context_sha:
            raise ValueError("news_program_trace_context_mismatch")
        for call_index, raw_call in enumerate(observation.get("calls") or []):
            self._persist_program_call(
                run_sha=run_sha,
                case_id=case_id,
                arm_name=arm_name,
                trial=trial,
                arm=arm,
                context_sha=context_sha,
                trace=trace,
                call_index=call_index,
                raw_call=raw_call,
            )
        return observation

    def _persist_program_call(
        self,
        *,
        run_sha: str,
        case_id: str,
        arm_name: str,
        trial: int,
        arm: ArmManifest,
        context_sha: str,
        trace: Mapping[str, Any],
        call_index: int,
        raw_call: Mapping[str, Any],
    ) -> None:
        call = dict(raw_call)
        predictor_name = str(call.get("predictor") or "")
        attempt = int(call.get("attempt") or 0)
        route = str(call.get("route") or "")
        model_binding = str(call.get("model_binding") or "")
        provider = str(call.get("provider") or "unknown")
        request_sha = str(call.get("request_sha256") or "")
        input_sha = str(call.get("input_sha256") or "")
        signature_sha = str(call.get("signature_sha256") or "")
        instruction_sha = str(call.get("instruction_sha256") or "")
        demos_sha = str(call.get("demos_sha256") or "")
        model = str(call.get("model") or model_binding)
        model_sha = str(
            call.get("model_sha256")
            or _sha(
                {
                    "runtime_model_bindings_sha256": arm.runtime_model_bindings_sha256,
                    "model_binding": model_binding,
                }
            )
        )
        execution_sha = _sha(
            {
                "program_sha256": arm.program_sha256,
                "runtime_model_bindings_sha256": arm.runtime_model_bindings_sha256,
                "factory_id": trace.get("factory_id"),
                "topology_sha256": trace.get("topology_sha256"),
                "adapter_sha256": trace.get("adapter_sha256"),
                "assembler_sha256": trace.get("assembler_sha256"),
                "signature_sha256": signature_sha,
                "instruction_sha256": instruction_sha,
                "demos_sha256": demos_sha,
                "provider": provider,
            }
        )
        if (
            predictor_name not in {"event_semantics", "reader_card"}
            or call_index < 0
            or attempt < 1
            or route not in {"primary", "fallback"}
            or not model_binding
            or not model
            or len(request_sha) != 64
            or len(input_sha) != 64
            or len(signature_sha) != 64
            or len(instruction_sha) != 64
            or len(demos_sha) != 64
            or len(model_sha) != 64
        ):
            raise ValueError("news_program_call_trace_incomplete")
        request = {
            "program_version": arm.program_version,
            "program_sha256": arm.program_sha256,
            "runtime_model_bindings_sha256": arm.runtime_model_bindings_sha256,
            "context_sha256": context_sha,
            "predictor": predictor_name,
            "call_index": call_index,
            "attempt": attempt,
            "route": route,
            "request_sha256": request_sha,
            "input_sha256": input_sha,
            "signature_sha256": signature_sha,
            "instruction_sha256": instruction_sha,
            "demos_sha256": demos_sha,
            "model_binding": model_binding,
            "provider": provider,
            "upstream_sha256": call.get("upstream_sha256"),
        }
        validated_output = call.get("validated_output")
        response = None
        if isinstance(validated_output, Mapping):
            output_field = "semantics" if predictor_name == "event_semantics" else "card"
            response = {
                "output": {output_field: dict(validated_output)},
                "provider": call.get("provider"),
                "model": call.get("model"),
                "model_sha256": call.get("model_sha256"),
                "latency_ms": int(call.get("latency_ms") or 0),
                "input_tokens": int(call.get("input_tokens") or 0),
                "output_tokens": int(call.get("output_tokens") or 0),
                "cached_tokens": int(call.get("cached_tokens") or 0),
                "total_tokens": int(call.get("total_tokens") or 0),
                "provider_cost_microusd": call.get("provider_cost_microusd"),
                "finish_reason": call.get("finish_reason"),
            }
        if len(_json(request).encode()) > MODEL_RECORDING_BYTES_MAX or (
            response is not None and len(_json(response).encode()) > MODEL_RECORDING_BYTES_MAX
        ):
            raise ValueError("news_model_recording_oversized")
        response_sha = _sha(response) if response is not None else None
        identity = {
            "run_sha": run_sha,
            "case_id": case_id,
            "arm": arm_name,
            "trial": trial,
            "predictor_name": predictor_name,
            "call_index": call_index,
            "attempt": attempt,
            "request_sha256": request_sha,
        }
        self._conn.execute(
            """
            INSERT INTO news_model_recordings (
              recording_sha, run_sha, case_id, arm, trial, predictor_name, call_index, attempt, route,
              request_sha256, response_sha256, request, response, provider, model, model_sha,
              execution_contract_sha, latency_ms, input_tokens, output_tokens, cached_tokens, total_tokens,
              provider_cost_microusd, finish_reason, error_code, created_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s,
              %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s
            ) ON CONFLICT (recording_sha) DO NOTHING
            """,
            (
                _sha(identity),
                run_sha,
                case_id,
                arm_name,
                trial,
                predictor_name,
                call_index,
                attempt,
                route,
                request_sha,
                response_sha,
                _json(request),
                _json(response) if response is not None else None,
                provider,
                model,
                model_sha,
                execution_sha,
                call.get("latency_ms"),
                call.get("input_tokens"),
                call.get("output_tokens"),
                call.get("cached_tokens"),
                call.get("total_tokens")
                if call.get("total_tokens") is not None
                else int(call.get("input_tokens") or 0) + int(call.get("output_tokens") or 0),
                call.get("provider_cost_microusd"),
                call.get("finish_reason"),
                call.get("error_code"),
                self._db_now_ms(),
            ),
        )

    def _evaluate_evidence(
        self,
        *,
        request: EvaluationRequest,
        development: DatasetManifest,
        validation: DatasetManifest,
        candidate: CandidateManifest,
        run_sha: str,
        observations: Sequence[Mapping[str, Any]],
        execution_errors: Sequence[str],
        observation_dimensions: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        failures: list[str] = []
        dev = development.counts
        requirements = _PROFILE["development"]
        if request.stage in {"offline", "holdout"}:
            for field_name, threshold_name in (
                ("boundary_cluster_n", "boundary_clusters_min"),
                ("retention_cluster_n", "retention_clusters_min"),
                ("negative_cluster_n", "negative_clusters_min"),
                ("natural_day_n", "natural_days_min"),
                ("stratum_n", "strata_min"),
            ):
                if int(dev.get(field_name) or 0) < int(requirements[threshold_name]):
                    blockers.append(f"development_{field_name}_insufficient")
            if requirements["safety_required"] and int(dev.get("safety_cluster_n") or 0) == 0:
                blockers.append("development_safety_empty")
        else:
            prior = "holdout" if request.stage == "shadow" else "shadow"
            if not self._has_passed_stage(candidate.candidate_sha, prior):
                blockers.append(f"prior_{prior}_evidence_not_passed")
        if execution_errors:
            blockers.extend(execution_errors)
        reviews = self._reviews_by_id(
            [str(item["case_ref"]["review_id"]) for item in observations if item["case_ref"].get("review_id")]
        )
        correctness = {"stable": 0, "candidate": 0, "scored": 0}
        critical_regressions: list[str] = []
        candidate_errors = 0
        stable_errors = 0
        common_errors = 0
        candidate_only_errors = 0
        stable_only_errors = 0
        stable_tokens: list[int] = []
        candidate_tokens: list[int] = []
        stable_calls: list[int] = []
        candidate_calls: list[int] = []
        stable_costs: list[int] = []
        candidate_costs: list[int] = []
        stable_latencies: list[int] = []
        candidate_latencies: list[int] = []
        candidate_observed_n = 0
        candidate_bad_n = 0
        candidate_schema_errors = 0
        provider_cost_observation_incomplete = False
        program_call_provenance_incomplete = False
        stability: dict[str, list[dict[str, Any]]] = {"stable": [], "candidate": []}
        for item in observations:
            review = reviews.get(str(item["case_ref"]["review_id"]), {})
            expected = _expected_delivery(str(review.get("should_push") or "uncertain"))
            stable_out = item["stable"]
            candidate_out = item["candidate"]
            for arm_name, output in (("stable", stable_out), ("candidate", candidate_out)):
                if output.get("stability"):
                    stability[arm_name].append(
                        {
                            "case_id": str(item["case_ref"]["case_id"]),
                            **dict(output["stability"]),
                        }
                    )
            if not candidate_out.get("not_assigned"):
                candidate_observed_n += 1
                candidate_bad_n += int(bool(candidate_out.get("error_code")) or bool(candidate_out.get("degraded")))
                candidate_schema_errors += int(str(candidate_out.get("error_code") or "").startswith("schema_invalid"))
            if stable_out.get("error_code"):
                stable_errors += 1
            if candidate_out.get("error_code"):
                candidate_errors += 1
            if stable_out.get("error_code") and candidate_out.get("error_code"):
                common_errors += 1
            elif candidate_out.get("error_code"):
                candidate_only_errors += 1
            elif stable_out.get("error_code"):
                stable_only_errors += 1
            for output, tokens, calls, costs, latencies in (
                (stable_out, stable_tokens, stable_calls, stable_costs, stable_latencies),
                (candidate_out, candidate_tokens, candidate_calls, candidate_costs, candidate_latencies),
            ):
                for program_obs in output.get("program") or []:
                    metric = _program_metric(program_obs)
                    if (
                        candidate.target == "program"
                        and request.stage in {"offline", "holdout"}
                        and not _provider_cost_observation_complete(program_obs)
                    ):
                        provider_cost_observation_incomplete = True
                    if (
                        candidate.target == "program"
                        and request.stage in {"offline", "holdout"}
                        and not _program_call_provenance_complete(program_obs)
                    ):
                        program_call_provenance_incomplete = True
                    if metric["total_tokens"] is not None:
                        tokens.append(int(metric["total_tokens"]))
                    if metric["call_count"] is not None:
                        calls.append(int(metric["call_count"]))
                    if metric["provider_cost_microusd"] is not None:
                        costs.append(int(metric["provider_cost_microusd"]))
                    if metric["latency_ms"] is not None:
                        latencies.append(int(metric["latency_ms"]))
            if expected is not None:
                correctness["scored"] += 1
                correctness["stable"] += bool(stable_out.get("delivered")) == expected
                correctness["candidate"] += bool(candidate_out.get("delivered")) == expected
                if (
                    str(review.get("should_push")) == "must_push"
                    and bool(stable_out.get("delivered"))
                    and not bool(candidate_out.get("delivered"))
                ):
                    critical_regressions.append(str(item["case_ref"]["case_id"]))
        if critical_regressions:
            failures.append("must_push_regression")
        if candidate_only_errors and request.stage in {"offline", "holdout"}:
            failures.append("candidate_schema_or_provider_regression")
        # A stable-arm or common provider failure makes the comparison
        # unavailable.  It must never become a vacuous PASS just because both
        # arms failed in the same way.  Candidate-only failures are complete
        # regression evidence and remain FAIL above.
        if (stable_only_errors or common_errors) and request.stage in {"offline", "holdout"}:
            blockers.append("stable_or_common_execution_unavailable")
        if provider_cost_observation_incomplete:
            blockers.append("provider_cost_observation_incomplete")
        if program_call_provenance_incomplete:
            blockers.append("program_call_provenance_incomplete")
        if _mean_regressed(
            stable_tokens,
            candidate_tokens,
            growth_pct=float(_PROFILE["guardrails"]["mean_total_tokens_growth_pct"]),
        ):
            failures.append("candidate_token_cost_regression")
        if _mean_regressed(
            stable_calls,
            candidate_calls,
            growth_pct=float(_PROFILE["guardrails"]["mean_call_growth_pct"]),
        ):
            failures.append("candidate_call_cost_regression")
        if _mean_regressed(
            stable_costs,
            candidate_costs,
            growth_pct=float(_PROFILE["guardrails"]["mean_provider_cost_growth_pct"]),
        ):
            failures.append("candidate_provider_cost_regression")
        candidate_latency_p95 = _percentile95(candidate_latencies)
        if (
            request.stage in {"shadow", "canary"}
            and candidate_latency_p95 is not None
            and candidate_latency_p95 > int(_PROFILE["guardrails"]["candidate_latency_p95_ms_max"])
        ):
            failures.append("candidate_latency_slo_regression")
        candidate_bad_rate = candidate_bad_n / candidate_observed_n if candidate_observed_n else None
        if request.stage in {"shadow", "canary"}:
            if candidate_schema_errors:
                failures.append("candidate_schema_contract_breach")
            if candidate_bad_rate is not None and candidate_bad_rate > float(
                _PROFILE["guardrails"]["candidate_degraded_or_error_rate_max"]
            ):
                failures.append("candidate_degraded_or_error_slo_regression")
        observation_hours = float((observation_dimensions or {}).get("window_duration_hours") or 0) or None
        load = self._reader_load(
            observations,
            development if request.stage == "offline" else validation,
            hours_override=observation_hours,
        )
        # Reader load stays visible in every report, but it is not a release
        # quota. A candidate that correctly recognizes more distinct facts must
        # not fail merely because an hour happened to contain many real events.

        primary = self._primary_result(run_sha, candidate, observations)
        if request.stage == "offline" and candidate.target == "program":
            if int(primary.get("planned_cluster_n") or 0) == 0:
                blockers.append("development_pairwise_review_empty")
            elif int(primary.get("resolved_cluster_n") or 0) < int(primary["planned_cluster_n"]):
                blockers.append("development_pairwise_review_incomplete")
            elif int(primary.get("candidate_win_n") or 0) == 0:
                blockers.append("development_target_improvement_not_observed")
            if int(primary.get("stable_win_n") or 0) > 0:
                failures.append("development_pairwise_regression")
        if primary.get("candidate_only_critical_cluster_ids"):
            failures.append("candidate_critical_error_regression")
        if request.stage == "holdout":
            val = validation.counts
            if float(val.get("window_duration_hours") or 0) < 24:
                blockers.append("validation_duration_insufficient")
            if int(val.get("eligible_event_n") or 0) < 200:
                blockers.append("validation_eligible_events_insufficient")
            planned_n = int(primary.get("planned_cluster_n") or 0)
            resolved_n = int(primary.get("resolved_cluster_n") or 0)
            review_budget_used = int(primary.get("review_budget_used") or 0)
            review_budget_max = int(_PROFILE["validation"]["max_review_budget"])
            if planned_n < int(_PROFILE["validation"]["primary_clusters_min"]):
                blockers.append("validation_primary_review_insufficient")
            elif resolved_n < planned_n:
                blockers.append(
                    "validation_review_budget_exhausted"
                    if review_budget_used >= review_budget_max
                    else "validation_primary_review_incomplete"
                )
            elif not primary.get("interval_95") or float(primary["interval_95"]["lower"]) <= 0:
                blockers.append("validation_primary_interval_crosses_zero")
        elif request.stage in {"shadow", "canary"}:
            if observation_hours is None or observation_hours < 24:
                blockers.append(f"{request.stage}_duration_insufficient")
            if not observations:
                blockers.append(f"{request.stage}_observations_empty")
            if request.stage == "canary":
                candidate_assignment_n = int((observation_dimensions or {}).get("candidate_assignment_n") or 0)
                if candidate_assignment_n < int(_PROFILE["guardrails"]["canary_candidate_min_n"]):
                    blockers.append("canary_candidate_assignment_n_insufficient")
                if (observation_dimensions or {}).get("assignment_invariant_breach_event_ids"):
                    failures.append("canary_one_arm_assignment_invariant_breach")
        if failures:
            outcome = "fail"
        elif blockers:
            outcome = "unknown"
        else:
            outcome = "pass"
        return {
            "evaluator_version": EVALUATOR_VERSION,
            "profile": _PROFILE,
            "trusted_root_sha": self._trusted_root_sha,
            "stable_sha": self._stable.bundle_sha,
            "candidate_sha": candidate.candidate_sha,
            "target": candidate.target,
            "development_dataset_sha": development.artifact_sha,
            "validation_dataset_sha": validation.artifact_sha,
            "observation_n": len(observations),
            "correctness": correctness,
            "primary": primary,
            "reader_load": load,
            "reader_contract_version": READER_CONTRACT_VERSION,
            "reader_contract_sha256": READER_CONTRACT_SHA256,
            "agent_cohort": self._agent_cohort(),
            "stable_error_n": stable_errors,
            "candidate_error_n": candidate_errors,
            "common_error_n": common_errors,
            "candidate_only_error_n": candidate_only_errors,
            "stable_only_error_n": stable_only_errors,
            "execution_incomplete": bool(
                request.stage in {"offline", "holdout"}
                and (
                    stable_only_errors
                    or common_errors
                    or provider_cost_observation_incomplete
                    or program_call_provenance_incomplete
                )
            ),
            "provider_cost_observation_complete": not provider_cost_observation_incomplete,
            "program_call_provenance_complete": not program_call_provenance_incomplete,
            "stable_mean_total_tokens": statistics.mean(stable_tokens) if stable_tokens else None,
            "candidate_mean_total_tokens": statistics.mean(candidate_tokens) if candidate_tokens else None,
            "stable_mean_call_count": statistics.mean(stable_calls) if stable_calls else None,
            "candidate_mean_call_count": statistics.mean(candidate_calls) if candidate_calls else None,
            "stable_mean_provider_cost_microusd": statistics.mean(stable_costs) if stable_costs else None,
            "candidate_mean_provider_cost_microusd": statistics.mean(candidate_costs) if candidate_costs else None,
            "program_cost_by_predictor": _program_cost_by_predictor(observations),
            "stable_latency_p95_ms": _percentile95(stable_latencies),
            "candidate_latency_p95_ms": candidate_latency_p95,
            "candidate_runtime_observation_n": candidate_observed_n,
            "candidate_degraded_or_error_n": candidate_bad_n,
            "candidate_degraded_or_error_rate": candidate_bad_rate,
            "critical_regressions": critical_regressions,
            "stability": stability,
            "blockers": blockers,
            "failures": failures,
            "gate_outcome": outcome,
            "evidence_dimensions": dict(
                observation_dimensions
                or {
                    "input_provenance": "live",
                    "execution": "recorded" if observations else "simulated",
                    "delivery": "simulated",
                    "review": "accepted",
                    "dataset_role": "discovery" if request.stage == "offline" else "hidden_temporal_holdout",
                    "pairing": "paired",
                    "outcome_revealed": False,
                }
            ),
        }

    def _agent_cohort(self) -> dict[str, str]:
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

    def _primary_result(
        self, run_sha: str, candidate: CandidateManifest, observations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        cluster_values: dict[str, list[int]] = {}
        planned_cluster_ids = {
            str(item["case_ref"].get("cluster_id") or item["case_ref"]["case_id"])
            for item in observations
            if bool((item.get("comparison") or {}).get("review_eligible"))
        }
        candidate_critical: dict[str, set[str]] = {}
        stable_critical: dict[str, set[str]] = {}
        resolved_cluster_ids: set[str] = set()
        if candidate.target == "policy":
            reviews = self._reviews_by_id([str(item["case_ref"]["review_id"]) for item in observations])
            for item in observations:
                expected = _expected_delivery(
                    str(reviews.get(str(item["case_ref"]["review_id"]), {}).get("should_push") or "uncertain")
                )
                if expected is None:
                    continue
                stable_ok = bool(item["stable"].get("delivered")) == expected
                candidate_ok = bool(item["candidate"].get("delivered")) == expected
                cluster_id = str(item["case_ref"].get("cluster_id") or item["case_ref"]["case_id"])
                cluster_values.setdefault(cluster_id, []).append(int(candidate_ok) - int(stable_ok))
                resolved_cluster_ids.add(cluster_id)
        else:
            pairwise = self._conn.execute(
                """
                SELECT DISTINCT ON (j.pairwise_case_id) j.pairwise_case_id, j.payload
                  FROM news_reviews a
                  JOIN news_reviews j ON j.review_id = a.accepts_review_id
                 WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'pairwise'
                   AND j.pairwise_case_id LIKE %s
                 ORDER BY j.pairwise_case_id, a.created_at_ms DESC, a.review_id DESC
                """,
                (f"{run_sha}:%",),
            ).fetchall()
            by_id = {str(item["case_ref"]["case_id"]): item for item in observations}
            for row in pairwise:
                case_id = str(row["pairwise_case_id"]).split(":", 1)[-1]
                pair_item = by_id.get(case_id)
                if pair_item is None:
                    continue
                preference = str((row["payload"] or {}).get("preference") or "uncertain")
                order = pair_item["comparison"]["pair_order"]
                cluster_id = str(pair_item["case_ref"].get("cluster_id") or case_id)
                candidate_side = "A" if order == "candidate_A" else "B"
                for tagged_error in (row["payload"] or {}).get("critical_errors") or []:
                    side, _, error = str(tagged_error).partition(":")
                    if not error or side not in {"A", "B"}:
                        continue
                    target = candidate_critical if side == candidate_side else stable_critical
                    target.setdefault(cluster_id, set()).add(error)
                if preference == "uncertain":
                    continue
                resolved_cluster_ids.add(cluster_id)
                if preference in {"tie", "both_bad"}:
                    cluster_values.setdefault(cluster_id, []).append(0)
                elif preference in {"A", "B"}:
                    candidate_won = (preference == "A" and order == "candidate_A") or (
                        preference == "B" and order == "stable_A"
                    )
                    cluster_values.setdefault(cluster_id, []).append(1 if candidate_won else -1)
        # The pre-registered primary sampling unit is one independent fact
        # cluster.  Several provider rows or repeated pairwise cases from the
        # same fact get one equal-weight vote, never an inflated N.
        values = [0 if sum(items) == 0 else (1 if sum(items) > 0 else -1) for items in cluster_values.values()]
        interval = _bootstrap_interval(values) if values else None
        candidate_only_critical = sorted(
            cluster_id
            for cluster_id, errors in candidate_critical.items()
            if errors - stable_critical.get(cluster_id, set())
        )
        review_budget = self._conn.execute(
            "SELECT count(*) AS n FROM news_reviews "
            "WHERE review_kind = 'judgment' AND subject_kind = 'pairwise' AND pairwise_case_id LIKE %s",
            (f"{run_sha}:%",),
        ).fetchone()
        return {
            "endpoint": "paired_delivery_correctness" if candidate.target == "policy" else "blind_net_preference",
            "planned_cluster_n": len(planned_cluster_ids) if candidate.target == "program" else len(cluster_values),
            "resolved_cluster_n": len(resolved_cluster_ids),
            "review_budget_used": int(review_budget["n"] or 0),
            "review_budget_max": int(_PROFILE["validation"]["max_review_budget"]),
            "accepted_case_n": sum(len(items) for items in cluster_values.values()),
            "accepted_cluster_n": len(values),
            "candidate_win_n": sum(value > 0 for value in values),
            "stable_win_n": sum(value < 0 for value in values),
            "tie_or_both_bad_n": sum(value == 0 for value in values),
            "candidate_only_critical_cluster_ids": candidate_only_critical,
            "net_preference": statistics.mean(values) if values else None,
            "interval_95": interval,
        }

    @staticmethod
    def _reader_load(
        observations: Sequence[Mapping[str, Any]],
        dataset: DatasetManifest,
        *,
        hours_override: float | None = None,
    ) -> dict[str, Any]:
        hours = max(
            1.0,
            hours_override
            if hours_override is not None
            else (dataset.window.to_ms - dataset.window.from_ms) / 3_600_000,
        )
        totals: dict[str, list[int]] = {"stable": [], "candidate": []}
        peaks: dict[str, int] = {}
        for arm in totals:
            delivered_at = [
                int(item["case_ref"]["opened_at_ms"]) for item in observations if bool(item[arm].get("delivered"))
            ]
            totals[arm] = delivered_at
            buckets: dict[int, int] = {}
            for stamp in delivered_at:
                bucket = stamp // 3_600_000
                buckets[bucket] = buckets.get(bucket, 0) + 1
            peaks[arm] = max(buckets.values(), default=0)
        return {
            "stable_delivered_n": len(totals["stable"]),
            "candidate_delivered_n": len(totals["candidate"]),
            "stable_mean_per_hour": len(totals["stable"]) / hours,
            "candidate_mean_per_hour": len(totals["candidate"]) / hours,
            "stable_peak_per_hour": peaks["stable"],
            "candidate_peak_per_hour": peaks["candidate"],
        }

    def _load_case(self, case: DatasetCaseRef) -> dict[str, Any]:
        review = self._conn.execute("SELECT * FROM news_reviews WHERE review_id = %s", (case.review_id,)).fetchone()
        if review is None:
            raise ValueError("news_learning_review_missing")
        if case.subject_kind == "event":
            row = self._conn.execute(
                "SELECT * FROM news_review_task_source_v1 WHERE event_id = %s AND evidence_version = %s",
                (case.event_id, case.evidence_version),
            ).fetchone()
            if row is None or row["evidence_sha256"] != case.evidence_sha256:
                raise ValueError("news_learning_evidence_changed")
            return {
                "snapshot": dict(row["evidence_snapshot"] or {}),
                "opened_at_ms": int(row["opened_at_ms"]),
                "receipt_at_ms": int(row["settled_at_ms"]) if row.get("settled_at_ms") is not None else None,
                "production_verdict": dict(row["verdict"] or {}) if row.get("verdict") else None,
                "review": dict(review),
                "watchlist": list((row.get("trace") or {}).get("watchlist") or []),
                "control": dict((row.get("trace") or {}).get("control") or {}),
            }
        row = self._conn.execute(
            "SELECT * FROM news_external_miss_snapshots WHERE snapshot_id = %s", (case.external_snapshot_id,)
        ).fetchone()
        if row is None or row["evidence_sha256"] != case.evidence_sha256:
            raise ValueError("news_learning_external_evidence_changed")
        snapshot = dict(row["snapshot"] or {})
        synthetic = {
            "schema_version": "news_event_evidence_v1",
            "focus_fact": {"fact_id": case.case_id, "text": snapshot["title"], "context": snapshot.get("body", "")},
            "card": {
                "event_id": case.case_id,
                "leader_title": snapshot["title"],
                "leader_description": snapshot.get("body", ""),
                "leader_url": snapshot["source_url"],
                "reporting_origin": snapshot.get("provenance", "operator"),
                "family": "general",
                "admission": "external_miss",
                "priority": "normal",
                "asset_class": "none",
                "grounded_assets": [],
                "storyline_key": "macro:general",
                "opened_at_ms": case.opened_at_ms,
                "member_count": 1,
            },
        }
        return {
            "snapshot": synthetic,
            "opened_at_ms": case.opened_at_ms,
            "receipt_at_ms": None,
            "production_verdict": None,
            "review": dict(review),
            "watchlist": [],
            "control": {"paused": False, "muted": False},
        }

    def _persist_run_cases(
        self,
        run_sha: str,
        dataset: DatasetManifest,
        observations: Sequence[Mapping[str, Any]],
        *,
        stage: str,
    ) -> None:
        now_ms = self._db_now_ms()
        for item in observations:
            case = item["case_ref"]
            self._conn.execute(
                """
                INSERT INTO news_learning_cases (
                  run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind, event_id,
                  evidence_version, external_snapshot_id, review_id, opened_at_ms,
                  evidence_sha256, cluster_id, stratum,
                  stable_observation, candidate_observation, comparison, created_at_ms
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s::jsonb, %s::jsonb, %s::jsonb, %s
                )
                ON CONFLICT (run_sha, case_id) DO NOTHING
                """,
                (
                    run_sha,
                    case["case_id"],
                    dataset.artifact_sha,
                    dataset.role,
                    stage,
                    case["subject_kind"],
                    case.get("event_id"),
                    case.get("evidence_version"),
                    case.get("external_snapshot_id"),
                    case.get("review_id"),
                    case["opened_at_ms"],
                    case["evidence_sha256"],
                    case["cluster_id"],
                    case["stratum"],
                    _json(item["stable"]),
                    _json(item["candidate"]),
                    _json(item["comparison"]),
                    now_ms,
                ),
            )

    def _load_run_cases(self, run_sha: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM news_learning_cases WHERE run_sha = %s ORDER BY case_id", (run_sha,)
        ).fetchall()
        return [
            {
                "case_ref": {
                    "case_id": row["case_id"],
                    "subject_kind": row["subject_kind"],
                    "event_id": row["event_id"],
                    "evidence_version": row["evidence_version"],
                    "external_snapshot_id": row["external_snapshot_id"],
                    "evidence_sha256": row["evidence_sha256"],
                    "cluster_id": row["cluster_id"],
                    "stratum": row["stratum"],
                    "review_id": row["review_id"],
                    "opened_at_ms": row["opened_at_ms"],
                },
                "stable": dict(row["stable_observation"] or {}),
                "candidate": dict(row["candidate_observation"] or {}),
                "comparison": dict(row["comparison"] or {}),
            }
            for row in rows
        ]

    def _persist_candidate(self, candidate: CandidateManifest) -> None:
        self._verify_registration_receipt(candidate.proposal_receipt)
        proposal = candidate.proposal_receipt.model_dump(mode="json")
        proposal_sha = self._persist_artifact("proposal", proposal, parent_sha=candidate.development_dataset_sha)
        payload = {
            "candidate_sha": candidate.candidate_sha,
            "candidate_bundle_sha": candidate.candidate_arm.bundle_sha,
            "proposal_sha": proposal_sha,
            "manifest": candidate.model_dump(mode="json"),
            "exact_diff": _arm_exact_diff(
                self._stable,
                candidate.candidate_arm,
                target=candidate.target,
                proposal=candidate.proposal_receipt,
            ),
        }
        self._persist_artifact("candidate", payload, parent_sha=candidate.parent_stable_sha)

    def _verify_registration_receipt(self, receipt: ProposalReceipt) -> None:
        row = self._conn.execute(
            "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s",
            (receipt.registration_receipt_sha,),
        ).fetchone()
        if row is None or str(row["kind"]) != "candidate_registration":
            raise ValueError("news_learning_candidate_registration_missing")
        payload = dict(row["payload"] or {})
        if payload != receipt.registration_payload:
            raise ValueError("news_learning_candidate_registration_mismatch")
        if _sha({"kind": "candidate_registration", "payload": payload}) != receipt.registration_receipt_sha:
            raise ValueError("news_learning_candidate_registration_hash_mismatch")

    def _candidate(self, candidate_sha: str) -> CandidateManifest:
        candidate = self._candidates.get(candidate_sha)
        if candidate is not None:
            return candidate
        rows = self._conn.execute(
            "SELECT payload FROM news_learning_artifacts WHERE kind = 'candidate' ORDER BY created_at_ms DESC"
        ).fetchall()
        for row in rows:
            payload = dict(row["payload"] or {})
            parsed = CandidateManifest.model_validate(payload.get("manifest") or payload)
            if parsed.candidate_sha == candidate_sha:
                self._candidates[candidate_sha] = parsed
                return parsed
        raise ValueError("news_learning_candidate_not_found")

    def _candidate_registered_at(self, candidate_sha: str) -> int:
        row = self._conn.execute(
            "SELECT created_at_ms FROM news_learning_artifacts "
            "WHERE kind = 'candidate' AND payload ->> 'candidate_sha' = %s "
            "ORDER BY created_at_ms LIMIT 1",
            (candidate_sha,),
        ).fetchone()
        if row is None:
            raise ValueError("news_learning_candidate_registration_missing")
        return int(row["created_at_ms"])

    def _load_dataset(self, artifact_sha: str) -> DatasetManifest:
        row = self._conn.execute(
            "SELECT payload FROM news_learning_artifacts WHERE artifact_sha = %s AND kind = 'dataset'", (artifact_sha,)
        ).fetchone()
        if row is None:
            raise ValueError("news_learning_dataset_not_found")
        payload = dict(row["payload"] or {})
        if payload.get("learning_epoch") != LEARNING_EPOCH:
            raise ValueError("news_learning_epoch_mismatch")
        if payload.get("profile_id") != LEARNING_PROFILE_ID:
            raise ValueError("news_learning_profile_mismatch")
        return DatasetManifest(artifact_sha=artifact_sha, **payload)

    def _load_production_observations(
        self,
        *,
        artifact_sha: str,
        stage: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        expected_kind = "shadow_observation" if stage == "shadow" else "canary_observation"
        row = self._conn.execute(
            "SELECT kind, payload, created_by FROM news_learning_artifacts WHERE artifact_sha = %s",
            (artifact_sha,),
        ).fetchone()
        if row is None or str(row["kind"]) != expected_kind:
            raise ValueError("news_learning_production_observation_not_found")
        payload = dict(row["payload"] or {})
        if _sha({"kind": expected_kind, "payload": payload}) != artifact_sha:
            raise ValueError("news_learning_production_observation_hash_mismatch")
        if str(payload.get("candidate_sha") or "") != candidate.candidate_sha:
            raise ValueError("news_learning_production_observation_candidate_mismatch")
        if str(payload.get("candidate_bundle_sha") or "") != candidate.candidate_arm.bundle_sha:
            raise ValueError("news_learning_production_observation_bundle_mismatch")
        if str(payload.get("stable_bundle_sha") or "") != self._stable.bundle_sha:
            raise ValueError("news_learning_production_observation_stable_mismatch")
        if str(payload.get("dataset_sha") or "") != dataset.artifact_sha:
            raise ValueError("news_learning_production_observation_dataset_mismatch")
        observations = payload.get("observations")
        dimensions = payload.get("evidence_dimensions")
        if observations is None and payload.get("observation_run_sha"):
            observations = self._load_run_cases(str(payload["observation_run_sha"]))
            if int(payload.get("case_n") or 0) != len(observations):
                raise ValueError("news_learning_production_observation_count_mismatch")
            if str(payload.get("observation_root") or "") != _observation_root(observations):
                raise ValueError("news_learning_production_observation_root_mismatch")
        if not isinstance(observations, list) or not observations:
            raise ValueError("news_learning_production_observation_empty")
        if not isinstance(dimensions, Mapping):
            raise ValueError("news_learning_production_observation_dimensions_missing")
        required = {
            "input_provenance",
            "execution",
            "delivery",
            "review",
            "dataset_role",
            "pairing",
            "outcome_revealed",
        }
        if not required <= set(dimensions):
            raise ValueError("news_learning_production_observation_dimensions_incomplete")
        if stage == "shadow" and dimensions.get("delivery") != "simulated":
            raise ValueError("news_learning_shadow_delivery_must_be_simulated")
        if stage == "canary" and dimensions.get("delivery") not in {
            "observed",
            "observed_sent",
            "observed_not_sent",
        }:
            raise ValueError("news_learning_canary_delivery_must_be_observed")
        return [dict(item) for item in observations], dict(dimensions)

    def _persist_observation_manifest(
        self,
        *,
        run_sha: str,
        stage: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
        observations: Sequence[Mapping[str, Any]],
        dimensions: Mapping[str, Any],
    ) -> str:
        kind = "shadow_observation" if stage == "shadow" else "canary_observation"
        payload = {
            "candidate_sha": candidate.candidate_sha,
            "candidate_bundle_sha": candidate.candidate_arm.bundle_sha,
            "stable_bundle_sha": self._stable.bundle_sha,
            "dataset_sha": dataset.artifact_sha,
            "observation_run_sha": run_sha,
            "observation_root": _observation_root(observations),
            "case_n": len(observations),
            "evidence_dimensions": dict(dimensions),
        }
        return self._persist_artifact(kind, payload, parent_sha=candidate.candidate_sha)

    def _generated_observation_manifest(
        self,
        *,
        run_sha: str,
        stage: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
        observations: Sequence[Mapping[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        kind = "shadow_observation" if stage == "shadow" else "canary_observation"
        row = self._conn.execute(
            "SELECT artifact_sha, payload FROM news_learning_artifacts "
            "WHERE kind = %s AND payload->>'observation_run_sha' = %s "
            "ORDER BY created_at_ms DESC LIMIT 1",
            (kind, run_sha),
        ).fetchone()
        if row is None:
            raise ValueError("news_learning_generated_observation_manifest_missing")
        payload = dict(row["payload"] or {})
        if str(payload.get("candidate_sha") or "") != candidate.candidate_sha:
            raise ValueError("news_learning_production_observation_candidate_mismatch")
        if str(payload.get("dataset_sha") or "") != dataset.artifact_sha:
            raise ValueError("news_learning_production_observation_dataset_mismatch")
        if int(payload.get("case_n") or 0) != len(observations):
            raise ValueError("news_learning_production_observation_count_mismatch")
        if str(payload.get("observation_root") or "") != _observation_root(observations):
            raise ValueError("news_learning_production_observation_root_mismatch")
        dimensions = payload.get("evidence_dimensions")
        if not isinstance(dimensions, Mapping):
            raise ValueError("news_learning_production_observation_dimensions_missing")
        return str(row["artifact_sha"]), dict(dimensions)

    def _has_passed_stage(self, candidate_sha: str, stage: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 AS ok
              FROM news_learning_artifacts
             WHERE kind = 'release_evidence'
               AND payload->>'candidate_sha' = %s
               AND payload->>'stage' = %s
               AND payload->>'gate_outcome' = 'pass'
             LIMIT 1
            """,
            (candidate_sha, stage),
        ).fetchone()
        return bool(row)

    def _persist_artifact(self, kind: str, payload: Mapping[str, Any], *, parent_sha: str | None = None) -> str:
        artifact_sha = _sha({"kind": kind, "payload": payload})
        self._conn.execute(
            """
            INSERT INTO news_learning_artifacts (artifact_sha, kind, parent_sha, payload, created_by, created_at_ms)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s) ON CONFLICT (artifact_sha) DO NOTHING
            """,
            (artifact_sha, kind, parent_sha, _json(payload), self._principal, self._db_now_ms()),
        )
        row = self._conn.execute(
            "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s", (artifact_sha,)
        ).fetchone()
        if row is None or row["kind"] != kind or _sha({"kind": kind, "payload": row["payload"]}) != artifact_sha:
            raise ValueError("news_learning_artifact_collision")
        return artifact_sha

    def _active_stable_sha(self) -> str:
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

    def _assert_active_stable(self) -> str:
        active_sha = self._active_stable_sha()
        if active_sha != self._stable.bundle_sha:
            raise ValueError("news_learning_active_stable_mismatch")
        return active_sha

    def _reviews_by_id(self, review_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not review_ids:
            return {}
        rows = self._conn.execute(
            "SELECT * FROM news_reviews WHERE review_id = ANY(%s)", (list(review_ids),)
        ).fetchall()
        return {str(row["review_id"]): dict(row) for row in rows}

    def _db_now_ms(self) -> int:
        row = self._conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
        ).fetchone()
        return int(row["now_ms"])

    @staticmethod
    def _needs_stability_trials(case_id: str, outputs: Mapping[str, Mapping[str, Any]]) -> bool:
        stable = outputs.get("stable") or {}
        candidate = outputs.get("candidate") or {}
        return int(case_id[:4], 16) % 10 == 0 or stable.get("verdict") != candidate.get("verdict")


def _usage_from_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    calls = [dict(item) for item in trace.get("calls") or []]
    costs = [cost for item in calls if (cost := _call_cost_microusd(item)) is not None]
    return {
        "wall_latency_ms": trace.get("wall_latency_ms"),
        "call_count": len(calls),
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in calls),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in calls),
        "cached_tokens": sum(int(item.get("cached_tokens") or 0) for item in calls),
        "total_tokens": sum(
            int(item.get("total_tokens") or 0)
            or (int(item.get("input_tokens") or 0) + int(item.get("output_tokens") or 0))
            for item in calls
        ),
        "provider_cost_microusd": sum(costs) if costs else None,
    }


def _call_cost_microusd(call: Mapping[str, Any]) -> int | None:
    if call.get("provider_cost_microusd") is not None:
        return int(call["provider_cost_microusd"])
    if call.get("provider_cost_usd") is not None:
        return round(float(call["provider_cost_usd"]) * 1_000_000)
    return None


def _program_metric(observation: Mapping[str, Any]) -> dict[str, int | None]:
    usage = dict(observation.get("usage") or {})
    calls = list(observation.get("calls") or (observation.get("trace") or {}).get("calls") or [])
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
    cost_microusd = usage.get("provider_cost_microusd")
    if cost_microusd is None and usage.get("provider_cost_usd") is not None:
        cost_microusd = round(float(usage["provider_cost_usd"]) * 1_000_000)
    call_count = usage.get("call_count")
    wall_latency_ms = usage.get("wall_latency_ms")
    return {
        "total_tokens": int(total_tokens) if total_tokens is not None else None,
        "call_count": int(call_count) if call_count is not None else len(calls),
        "provider_cost_microusd": int(cost_microusd) if cost_microusd is not None else None,
        "latency_ms": int(wall_latency_ms) if wall_latency_ms is not None else None,
    }


def _provider_cost_observation_complete(observation: Mapping[str, Any]) -> bool:
    usage = dict(observation.get("usage") or {})
    trace = observation.get("trace") or {}
    calls = list(observation.get("calls") or (trace.get("calls") if isinstance(trace, Mapping) else ()) or [])
    if not calls or usage.get("provider_cost_microusd") is None:
        return False
    if usage.get("call_count") is not None and int(usage["call_count"]) != len(calls):
        return False
    return all(dict(call).get("provider_cost_microusd") is not None for call in calls)


def _program_call_provenance_complete(observation: Mapping[str, Any]) -> bool:
    trace = observation.get("trace") or {}
    calls = list(observation.get("calls") or (trace.get("calls") if isinstance(trace, Mapping) else ()) or [])
    return bool(calls) and all(str(dict(call).get("provider") or "").strip() for call in calls)


def _mean_regressed(stable: Sequence[int], candidate: Sequence[int], *, growth_pct: float) -> bool:
    if not stable or not candidate:
        return False
    stable_mean = statistics.mean(stable)
    candidate_mean = statistics.mean(candidate)
    if stable_mean == 0:
        return candidate_mean > 0
    return candidate_mean > stable_mean * (1 + float(growth_pct))


def _program_cost_by_predictor(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, dict[str, dict[str, Any]]] = {"stable": {}, "candidate": {}}
    for item in observations:
        for arm in ("stable", "candidate"):
            for program in (item.get(arm) or {}).get("program") or []:
                for raw_call in program.get("calls") or (program.get("trace") or {}).get("calls") or []:
                    call = dict(raw_call)
                    predictor = str(call.get("predictor") or "unknown")
                    route = str(call.get("route") or "unknown")
                    name = f"{predictor}:{route}"
                    bucket = result[arm].setdefault(
                        name,
                        {
                            "call_n": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                            "provider_cost_microusd": 0,
                            "provider_cost_observed_call_n": 0,
                            "latencies_ms": [],
                        },
                    )
                    bucket["call_n"] += 1
                    bucket["input_tokens"] += int(call.get("input_tokens") or 0)
                    bucket["output_tokens"] += int(call.get("output_tokens") or 0)
                    bucket["total_tokens"] += int(call.get("total_tokens") or 0) or (
                        int(call.get("input_tokens") or 0) + int(call.get("output_tokens") or 0)
                    )
                    cost = _call_cost_microusd(call)
                    if cost is not None:
                        bucket["provider_cost_microusd"] += cost
                        bucket["provider_cost_observed_call_n"] += 1
                    if call.get("latency_ms") is not None:
                        bucket["latencies_ms"].append(int(call["latency_ms"]))
    for arm_buckets in result.values():
        for bucket in arm_buckets.values():
            latencies = bucket.pop("latencies_ms")
            bucket["latency_p95_ms"] = _percentile95(latencies)
    return result


def _observed_production_output(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a production verdict + first-delivery receipt without inference."""

    trace = dict(row.get("trace") or {})
    verdict = dict(row.get("verdict") or {}) if row.get("verdict") else None
    error_code = str(row.get("verdict_error_code") or "") or None
    if verdict is None:
        error_code = error_code or "assigned_without_verdict"
    delivery_state = str(row.get("delivery_state") or "")
    if delivery_state == "sent":
        delivery = "observed_sent"
    elif str(row.get("delivery_error_code") or "") == "ambiguous_after_crash":
        delivery = "ambiguous"
    else:
        delivery = "observed_not_sent"
    program_trace = dict(trace.get("program_trace") or {})
    provider_cost_microusd = trace.get("provider_cost_microusd")
    if provider_cost_microusd is None and trace.get("provider_cost_usd") is not None:
        # Historical prompt-era trace compatibility is read-only audit input.
        provider_cost_microusd = round(float(trace["provider_cost_usd"]) * 1_000_000)
    usage = {
        "wall_latency_ms": trace.get("latency_ms"),
        "call_count": trace.get("model_attempts"),
        "input_tokens": trace.get("input_tokens"),
        "output_tokens": trace.get("output_tokens"),
        "cached_tokens": trace.get("cached_tokens"),
        "total_tokens": trace.get("total_tokens"),
        "provider_cost_microusd": provider_cost_microusd,
    }
    program = {
        "program_version": row.get("program_version") or trace.get("program_version"),
        "program_sha256": row.get("program_sha256") or trace.get("program_sha256"),
        "trace": program_trace,
        "calls": list(program_trace.get("calls") or []),
        "usage": usage,
        "error_code": error_code,
    }
    return {
        "verdict": verdict,
        "final_decision": row.get("final_decision"),
        "delivered": delivery_state == "sent",
        "execution": "live",
        "delivery": delivery,
        "degraded": bool(row.get("degraded")),
        "error_code": error_code,
        "program": [program],
    }


def _observation_root(observations: Sequence[Mapping[str, Any]]) -> str:
    persisted_case_fields = (
        "case_id",
        "subject_kind",
        "event_id",
        "evidence_version",
        "external_snapshot_id",
        "evidence_sha256",
        "review_id",
        "cluster_id",
        "stratum",
        "should_push",
        "delivery_truth",
        "opened_at_ms",
    )
    leaves = [
        _sha(
            {
                "case_ref": {field: item.get("case_ref", {}).get(field) for field in persisted_case_fields},
                "stable": item.get("stable") or {},
                "candidate": item.get("candidate") or {},
                "comparison": item.get("comparison") or {},
            }
        )
        for item in observations
    ]
    return _sha({"observation_root_version": "news_observation_root_v1", "leaves": leaves})


def _percentile95(values: Sequence[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(int(value) for value in values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def _expected_delivery(should_push: str) -> bool | None:
    if should_push in {"must_push", "should_push"}:
        return True
    if should_push in {"must_hold", "should_hold"}:
        return False
    return None


def _case_delivery_failed(case: DatasetCaseRef) -> bool:
    """Whether the accepted expected action disagrees with frozen delivery truth."""

    expected = _expected_delivery(case.should_push)
    if expected is None or case.delivery_truth == "unknown":
        return False
    return (case.delivery_truth == "observed_sent") != expected


def _bootstrap_interval(values: Sequence[int]) -> dict[str, float] | None:
    if not values:
        return None
    rng = random.Random(int(_PROFILE["bootstrap"]["seed"]))  # noqa: S311 - deterministic bootstrap
    n = len(values)
    means = [
        sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(int(_PROFILE["bootstrap"]["replicates"]))
    ]
    means.sort()
    alpha = (1 - float(_PROFILE["bootstrap"]["confidence"])) / 2
    lower = means[max(0, math.floor(alpha * len(means)))]
    upper = means[min(len(means) - 1, math.ceil((1 - alpha) * len(means)) - 1)]
    return {"lower": lower, "upper": upper}


def _fact_cluster(text: str) -> str:
    normalized = "".join(str(text or "").lower().split())
    return _text_sha(normalized)


def _connected_fact_clusters(
    drafts: Sequence[tuple[DatasetCaseRef, str, str]],
) -> list[DatasetCaseRef]:
    """Collapse duplicate-of components and identical source facts into one N.

    ``duplicate_of`` may point outside the frozen window.  In that case all
    cases naming the same external target still share a component.  Exact
    normalized text is a deterministic fallback, not a semantic guess.
    """

    parent = list(range(len(drafts)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    event_index = {str(case.event_id): index for index, (case, _, _) in enumerate(drafts) if case.event_id is not None}
    identities: dict[str, int] = {}
    for index, (case, duplicate_of, source_identity) in enumerate(drafts):
        keys = [f"text:{case.cluster_id}"]
        if source_identity.strip():
            keys.append(f"source:{source_identity.strip()}")
        if duplicate_of:
            target = event_index.get(duplicate_of)
            if target is not None:
                union(index, target)
            else:
                keys.append(f"duplicate_of:{duplicate_of}")
        for key in keys:
            previous = identities.setdefault(key, index)
            union(index, previous)

    members: dict[int, list[str]] = {}
    for index, (case, _, _) in enumerate(drafts):
        members.setdefault(find(index), []).append(case.cluster_id)
    cluster_sha = {
        root: _sha({"fact_cluster_version": "news_fact_cluster_v1", "members": sorted(set(values))})
        for root, values in members.items()
    }
    return [
        case.model_copy(update={"cluster_id": cluster_sha[find(index)]}) for index, (case, _, _) in enumerate(drafts)
    ]


def _text_sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _arm_exact_diff(
    stable: ArmManifest,
    candidate: ArmManifest,
    *,
    target: str,
    proposal: ProposalReceipt,
) -> dict[str, Any]:
    """Return the exact, reviewable single-variable delta sealed with a candidate.

    This is operator evidence, not an executable patch. Program candidates
    carry the compiler's content-addressed Predictor diff; policy candidates
    carry the exact changed values. The evaluator's static validator remains
    the authority that rejects mixed changes.
    """

    stable_payload = stable.model_dump(mode="json")
    candidate_payload = candidate.model_dump(mode="json")
    changed_fields = sorted(key for key in stable_payload if stable_payload[key] != candidate_payload[key])
    common = {
        "target": target,
        "changed_fields": changed_fields,
        "stable_bundle_sha": stable.bundle_sha,
        "candidate_bundle_sha": candidate.bundle_sha,
    }
    if target == "program":
        return {
            **common,
            "stable_program_version": stable.program_version,
            "candidate_program_version": candidate.program_version,
            "stable_program_sha256": stable.program_sha256,
            "candidate_program_sha256": candidate.program_sha256,
            "machine_diff": dict(proposal.program_machine_diff or {}),
            "compile_provenance": dict(proposal.compile_provenance or {}),
        }
    changed_keys = sorted(
        key for key in set(stable.policy) | set(candidate.policy) if stable.policy.get(key) != candidate.policy.get(key)
    )
    return {
        **common,
        "stable_policy_sha256": stable.policy_sha256,
        "candidate_policy_sha256": candidate.policy_sha256,
        "values": {
            key: {"stable": stable.policy.get(key), "candidate": candidate.policy.get(key)} for key in changed_keys
        },
    }


def _sha(value: Any) -> str:
    return canonical_sha(value)


def _json(value: Any) -> str:
    return canonical_json(value)


def _proposal_json(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize tuple/list representation before hashing a registration receipt."""

    normalized = json.loads(_json(dict(value)))
    if not isinstance(normalized, dict):  # pragma: no cover - Mapping input guarantees this
        raise TypeError("news_learning_proposal_payload_invalid")
    return normalized


__all__ = [
    "LEARNING_EPOCH",
    "LEARNING_EPOCH_STARTED_AT_MS",
    "TRUSTED_ROOT_SHA",
    "ArmManifest",
    "CandidateEvaluator",
    "CandidateManifest",
    "ClosedWindow",
    "DatasetManifest",
    "DatasetSpec",
    "EvaluationReport",
    "EvaluationRequest",
    "ProposalReceipt",
]
