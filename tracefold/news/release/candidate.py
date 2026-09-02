"""Registering one Prompt candidate, and admitting it to a release stage.

Split out of `CandidateEvaluator` (#202 §4.3, §8). Registration is not evaluation and it is not dataset
freezing: it decides whether a candidate may be *considered*, on evidence a party that did not produce it
re-derives — the patch re-applied to the running stable, the corpus re-projected, the #199 Objective Plan
rebuilt. `evaluate` then returns evidence and this layer decides what state follows it.

It reads datasets; datasets never reach back. `freeze_dataset` takes an `AdmittedCandidate` this module
produces rather than calling into it, which is what keeps that one-way — candidate validation re-derives
the Objective Plan from `development_compile_export`, so the reverse edge would close a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..artifact_identity import canonical_sha
from ..learning.contracts import (
    LEARNING_PROGRAM_VERSION,
    ArmManifest,
    CandidateManifest,
    PromptCandidateV1,
    ProposalReceipt,
)
from ..learning.dataset import AdmittedCandidate, DevelopmentDatasetStore
from ..learning.ledger import LearningLedger
from ..learning.objective import (
    DevelopmentEpisode,
    GepaObjectivePlan,
    build_gepa_objective_plan,
    optimizer_population_identity,
)
from ..learning.projection import _arm_exact_diff
from ..program.artifact import apply_program_patch, load_stable_program_artifact
from ..storage.root import NewsRepository


def validate_declared_objective_summary(
    objective_summary: Mapping[str, Any],
    *,
    episode_projection_root_sha256: str,
    plan: GepaObjectivePlan,
) -> None:
    """Hold a declared optimizer population to the release plane's re-derived plan."""

    declared_root = str(objective_summary.get("episode_projection_root_sha256") or "")
    if declared_root and declared_root != episode_projection_root_sha256:
        raise ValueError("news_learning_candidate_corpus_mismatch")
    declared_split = objective_summary.get("split")
    if not declared_split:
        return
    if str(objective_summary.get("plan_schema") or "") != plan.schema_version:
        raise ValueError("news_learning_proposal_objective_schema_unverified")
    expected_population = {
        "optimizer_case_ids": list(plan.optimizer_case_ids),
        **optimizer_population_identity(plan),
    }
    declared_population = {key: objective_summary.get(key) for key in expected_population}
    if declared_population != expected_population:
        raise ValueError("news_learning_proposal_optimizer_population_unverified")
    if declared_split != plan.split:
        raise ValueError("news_learning_proposal_split_roots_unverified")


class CandidateRegistry:
    """The one place a candidate is admitted, and the one place that says when it was."""

    def __init__(
        self,
        conn: Any,
        *,
        datasets: DevelopmentDatasetStore,
        ledger: LearningLedger,
        stable: ArmManifest,
        catalog: dict[str, CandidateManifest] | None = None,
    ) -> None:
        self._repository = NewsRepository(conn)
        self._datasets = datasets
        self._ledger = ledger
        self._stable = stable
        self._candidates = catalog if catalog is not None else {}

    def admit_for_validation(self, candidate_sha: str) -> AdmittedCandidate:
        """Validate and persist a candidate, then say when it was registered.

        The only producer of `AdmittedCandidate`. `freeze_dataset` cannot construct one, which is how a
        validation dataset is stopped from being sealed against a candidate nobody admitted.
        """

        candidate = self.load(candidate_sha)
        self.validate(candidate)
        self.persist(candidate)
        return AdmittedCandidate(
            candidate_sha=candidate.candidate_sha,
            registered_at_ms=max(
                candidate.proposal_receipt.registered_at_ms,
                self._candidate_registered_at(candidate.candidate_sha),
            ),
        )

    def validate(self, candidate: CandidateManifest) -> GepaObjectivePlan:
        if self._stable.program_version != LEARNING_PROGRAM_VERSION:
            raise ValueError("news_learning_program_v1_unsupported")
        if candidate.parent_stable_sha != self._stable.bundle_sha:
            raise ValueError("news_learning_candidate_parent_stable_mismatch")
        if candidate.candidate_arm.program_version != LEARNING_PROGRAM_VERSION:
            raise ValueError("news_learning_program_v1_unsupported")
        stable = self._stable.model_dump(mode="json")
        proposed = candidate.candidate_arm.model_dump(mode="json")
        changed = {key for key in stable if stable[key] != proposed[key]}
        allowed = {"program_sha256"}
        if not changed or not changed <= allowed:
            raise ValueError(f"news_learning_exact_one_variable_violation:{','.join(sorted(changed))}")
        development = self._datasets.load_dataset(candidate.development_dataset_sha)
        if development.role != "development":
            raise ValueError("news_learning_proposal_requires_development_dataset")
        receipt = candidate.proposal_receipt
        if candidate.candidate_arm.program_sha256 == self._stable.program_sha256:
            raise ValueError("news_learning_program_sha_unchanged")
        if receipt.program_parent_sha256 != self._stable.program_sha256:
            raise ValueError("news_learning_program_parent_mismatch")
        if receipt.program_candidate_sha256 != candidate.candidate_arm.program_sha256:
            raise ValueError("news_learning_program_candidate_mismatch")
        # The write-set itself, re-applied. Until #202 this position held a `CompileRecordV1` carrying a
        # sandbox launch receipt, a proxy call ledger, a three-party build attestation and a tariff — a
        # proof about *where* two strings were produced. What actually has to hold is that these two
        # strings, applied to the running stable Program, are the Program this arm will execute; a patch a
        # person wrote and a patch GEPA wrote are then admissible on identical terms (§7).
        prompt = self._prompt_candidate(candidate)
        if prompt.parent_program_sha256 != self._stable.program_sha256:
            raise ValueError("news_learning_prompt_candidate_parent_mismatch")
        if prompt.development_dataset_sha256 != candidate.development_dataset_sha:
            raise ValueError("news_learning_prompt_candidate_dataset_mismatch")
        if prompt.target_runtime_manifest_sha256 != self._stable.runtime_model_bindings_sha256:
            raise ValueError("news_learning_prompt_candidate_runtime_manifest_mismatch")
        parent_artifact = load_stable_program_artifact()
        rebuilt = apply_program_patch(parent_artifact, prompt.patch.applied_to(parent_artifact))
        if rebuilt.program_sha256 != candidate.candidate_arm.program_sha256:
            raise ValueError("news_learning_prompt_candidate_program_identity_mismatch")
        episodes = list(self._datasets.development_compile_export(candidate.development_dataset_sha).episodes)
        plan = build_gepa_objective_plan(tuple(DevelopmentEpisode.model_validate(episode) for episode in episodes))
        # Not the count: the episodes themselves. `development_compile_export` re-projects them from live
        # reviews and recorded decisions, so a review edited between registration and evaluation leaves the
        # dataset SHA and the case count identical and the corpus different — and the candidate would then
        # be judged against evidence nobody generated it from.
        if receipt.development_episode_projection_root_sha256 != _sha(episodes):
            raise ValueError("news_learning_candidate_corpus_mismatch")
        # A GEPA candidate names the projection and representative split it optimized on; an external
        # proposal names neither and is bound to what registration re-projected. The shared release
        # validator keeps this later gate identical to the pre-write registration gate.
        validate_declared_objective_summary(
            prompt.objective_summary,
            episode_projection_root_sha256=receipt.development_episode_projection_root_sha256,
            plan=plan,
        )
        # The Objective Plan, rebuilt from the same frozen corpus. A candidate that declares clusters the
        # plan does not include was optimized against something else.
        if set(receipt.optimizer_cluster_ids) != set(plan.optimizer_cluster_ids):
            unknown = ",".join(sorted(set(receipt.optimizer_cluster_ids) ^ set(plan.optimizer_cluster_ids)))
            raise ValueError(f"news_learning_proposal_optimizer_cluster_unverified:{unknown}")
        if tuple(candidate.target_dimensions) != plan.target_dimensions:
            raise ValueError("news_learning_proposal_target_dimensions_unverified")
        if candidate.development_dataset_sha != candidate.proposal_receipt.development_dataset_sha:
            raise ValueError("news_learning_proposal_dataset_mismatch")
        if tuple(candidate.target_dimensions) != tuple(candidate.proposal_receipt.declared_target_dimensions):
            raise ValueError("news_learning_target_dimensions_mismatch")
        self._verify_registration_receipt(candidate.proposal_receipt)
        return plan

    def persist(self, candidate: CandidateManifest) -> None:
        self._verify_registration_receipt(candidate.proposal_receipt)
        proposal = candidate.proposal_receipt.model_dump(mode="json")
        proposal_sha = self._ledger.persist_artifact("proposal", proposal, parent_sha=candidate.development_dataset_sha)
        payload = {
            "candidate_sha": candidate.candidate_sha,
            "candidate_bundle_sha": candidate.candidate_arm.bundle_sha,
            "proposal_sha": proposal_sha,
            "manifest": candidate.model_dump(mode="json"),
            "exact_diff": _arm_exact_diff(
                self._stable,
                candidate.candidate_arm,
                proposal=candidate.proposal_receipt,
            ),
        }
        self._ledger.persist_artifact("candidate", payload, parent_sha=candidate.parent_stable_sha)

    def _verify_registration_receipt(self, receipt: ProposalReceipt) -> None:
        row = self._repository.learning_artifact(receipt.registration_receipt_sha)
        if row is None or str(row["kind"]) != "candidate_registration":
            raise ValueError("news_learning_candidate_registration_missing")
        payload = dict(row["payload"] or {})
        if payload != receipt.registration_payload:
            raise ValueError("news_learning_candidate_registration_mismatch")
        if _sha({"kind": "candidate_registration", "payload": payload}) != receipt.registration_receipt_sha:
            raise ValueError("news_learning_candidate_registration_hash_mismatch")

    def _prompt_candidate(self, candidate: CandidateManifest) -> PromptCandidateV1:
        """Load the typed write-set this candidate names, and re-verify it against the candidate.

        Stored under its own hash, so a byte changed in the payload either stops the document validating
        or stops it answering to the key the receipt points at. That is the whole provenance requirement
        now: the document is the two instructions, and what makes them admissible is what registration and
        evaluation check about them — not the process that emitted them.
        """

        receipt = candidate.proposal_receipt
        rows = self._repository.learning_artifacts_of_kind("prompt_candidate", receipt.prompt_candidate_sha256)
        if not rows:
            raise ValueError("news_learning_prompt_candidate_missing")
        row = rows[0]
        payload = dict(row["payload"] or {})
        if str(row.get("parent_sha") or "") != candidate.development_dataset_sha:
            raise ValueError("news_learning_prompt_candidate_parent_mismatch")
        try:
            prompt = PromptCandidateV1.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("news_learning_prompt_candidate_invalid") from exc
        if prompt.candidate_sha256 != str(row["artifact_sha"]):
            raise ValueError("news_learning_prompt_candidate_identity_mismatch")
        if prompt.model_dump(mode="json") != payload:
            raise ValueError("news_learning_prompt_candidate_noncanonical")
        return prompt

    def load(self, candidate_sha: str) -> CandidateManifest:
        candidate = self._candidates.get(candidate_sha)
        if candidate is not None:
            return candidate
        for payload in self._repository.registered_candidate_payloads():
            parsed = CandidateManifest.model_validate(payload.get("manifest") or payload)
            if parsed.candidate_sha == candidate_sha:
                self._candidates[candidate_sha] = parsed
                return parsed
        raise ValueError("news_learning_candidate_not_found")

    def _candidate_registered_at(self, candidate_sha: str) -> int:
        registered_at_ms = self._repository.candidate_registered_at_ms(candidate_sha)
        if registered_at_ms is None:
            raise ValueError("news_learning_candidate_registration_missing")
        return registered_at_ms

    def has_passed_stage(self, candidate_sha: str, stage: str) -> bool:
        return self._repository.candidate_passed_stage(candidate_sha, stage)


def _sha(value: Any) -> str:
    return canonical_sha(value)


__all__ = ["CandidateRegistry"]
