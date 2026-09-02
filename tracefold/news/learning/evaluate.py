"""Judging one registered candidate against the stable arm, and returning evidence.

`CandidateEvaluator` was 3,031 lines and three lifecycles (#202 §8). Freezing a corpus moved to
`learning/dataset.py`; admitting a candidate moved to `news/release/candidate.py`; the release profile,
the epoch identity and the ledger have their own modules. What is left here is the judging: run both arms
over a frozen corpus, compare them, and publish what was observed.

It decides no state. `evaluate` returns a sealed report with a gate outcome and a recommended action; the
release plane is what acts on it. That separation is the point of the split — while judging and advancing
shared a class, "the evidence says pass" and "the candidate advances" were one statement.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_json, canonical_sha
from ..events.storyline import final_storyline_key
from ..program.contracts import ProgramCallTrace, ScoredJudgment, SemanticJudge, SemanticJudgeError, TriageContext
from ..program.identity import EXECUTION_ENVELOPE_SHA256
from ..program.lm import RecordedLM, RuntimeModelIdentity
from ..release.candidate import CandidateRegistry
from ..review.desk import (
    READER_CONTRACT_SHA256,
    READER_CONTRACT_VERSION,
    REVIEW_RUBRIC_VERSION,
)
from ..storage.root import NewsRepository
from ..taxonomy import ModelTaxonomyV1
from .contracts import (
    LEARNING_PROFILE_ID,
    ArmManifest,
    CandidateManifest,
    ClosedWindow,
    DatasetCaseRef,
    ProposalReceipt,
)
from .dataset import (
    DatasetManifest,
    DatasetSpec,
    DevelopmentDatasetStore,
)
from .dataset import _fact_cluster as _dataset_fact_cluster
from .evaluation_history import ArmState, EvaluationReaderHistory, Receipt, receipt_from_output
from .ledger import LearningLedger
from .metric import (
    METRIC_ID,
    PRODUCTION_REGRESSION_GATES,
    ProductionRegressionGateEvidenceV1,
    metric_contract_sha256,
    production_regression_measurements,
)
from .objective import (
    _expected_delivery,
    development_split_profile_counts,
    production_decision,
)
from .profile import _PROFILE, EVALUATOR_VERSION, TRUSTED_ROOT_SHA, development_coverage_blockers
from .projection import (
    _call_cost_microusd,
    _observation_root,
    _observed_production_output,
    _percentile95,
    _program_call_identity_complete,
    _program_cost_by_predictor,
    _program_metric,
)
from .taxonomy_metric import summarize_taxonomy

# Re-exported, not restated. A second literal here would be one more copy of the identity #193 exists to
# stop duplicating — and since #314 there is no literal to copy: the value is computed from the code the
# judgment ran under, so a stale alias is not a shape this module can have.
LEARNING_EXECUTION_ENVELOPE_SHA256 = EXECUTION_ENVELOPE_SHA256
MODEL_RECORDING_BYTES_MAX = 64 * 1024
ArmName = Literal["stable", "candidate"]
ArmJudgeKey = tuple[ArmName, str]

_TAXONOMY_RELEASE_AXES = (
    "subject_codes_set_f1",
    "event_family_accuracy",
    "change_state_accuracy",
    "assertion_status_accuracy",
    "four_axis_exact_accuracy",
)


def _output_taxonomy(output: Mapping[str, Any]) -> Mapping[str, Any] | None:
    editorial = output.get("editorial")
    if not isinstance(editorial, Mapping):
        scored = output.get("scored_judgment")
        editorial = scored.get("editorial") if isinstance(scored, Mapping) else None
    taxonomy = editorial.get("taxonomy") if isinstance(editorial, Mapping) else None
    return taxonomy if isinstance(taxonomy, Mapping) else None


def _review_taxonomy(review: Mapping[str, Any]) -> dict[str, Any] | None:
    taxonomy = dict(dict(review.get("payload") or {}).get("taxonomy") or {})
    model_axes = {field: taxonomy[field] for field in ModelTaxonomyV1.model_fields if field in taxonomy}
    try:
        return ModelTaxonomyV1.model_validate(model_axes).model_dump(mode="json")
    except ValueError:
        return None


def _taxonomy_release_evidence(
    observations: Sequence[Mapping[str, Any]],
    reviews: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate accepted-Gold taxonomy once per arm and expose per-axis deltas.

    #501 removed the "every Stable-correct control must stay exact" failure: the absolute per-cluster rule
    was the same logic that made selection unreachable, and the per-axis delta gate below already refuses
    a candidate that is worse than Stable on any axis.
    """

    rows: dict[str, list[dict[str, Any]]] = {"stable": [], "candidate": []}
    for item in observations:
        case_ref = dict(item.get("case_ref") or {})
        review = reviews.get(str(case_ref.get("review_id") or ""), {})
        gold = _review_taxonomy(review)
        if gold is None:
            continue
        case_id = str(case_ref.get("case_id") or "")
        cluster_id = str(case_ref.get("cluster_id") or "")
        stable = _output_taxonomy(dict(item.get("stable") or {}))
        candidate = _output_taxonomy(dict(item.get("candidate") or {}))
        if stable is not None:
            rows["stable"].append({"case_id": case_id, "cluster_id": cluster_id, "gold": gold, "predicted": stable})
        if candidate is not None:
            rows["candidate"].append(
                {"case_id": case_id, "cluster_id": cluster_id, "gold": gold, "predicted": candidate}
            )

    stable_summary = summarize_taxonomy(rows["stable"])
    candidate_summary = summarize_taxonomy(rows["candidate"])
    delta: dict[str, float | None] = {
        axis: (
            None
            if stable_summary[axis] is None or candidate_summary[axis] is None
            else round(float(candidate_summary[axis]) - float(stable_summary[axis]), 6)
        )
        for axis in ("taxonomy_overall", *_TAXONOMY_RELEASE_AXES)
    }
    regressed_axes = [
        axis for axis in _TAXONOMY_RELEASE_AXES if (axis_delta := delta[axis]) is not None and axis_delta < 0
    ]
    return {
        "schema": "tracefold.news.taxonomy_release_evidence.v2",
        "stable": stable_summary,
        "candidate": candidate_summary,
        "delta": delta,
        "regressed_axes": regressed_axes,
    }


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


def evaluation_run_sha(
    request: EvaluationRequest,
    *,
    stable_bundle_sha: str,
    candidate_sha: str,
    trusted_root_sha: str = TRUSTED_ROOT_SHA,
) -> str:
    """Return the stable identity of one evaluation run."""

    return _sha(
        {
            "request": request.model_dump(mode="json"),
            "stable": stable_bundle_sha,
            "candidate": candidate_sha,
            "trusted_root": trusted_root_sha,
            "evaluator": EVALUATOR_VERSION,
        }
    )


class CandidateEvaluator:
    """Freeze reviewed evidence and compare stable/candidate; never publish."""

    def __init__(
        self,
        conn: Any,
        *,
        stable: ArmManifest,
        judges: Mapping[ArmJudgeKey, SemanticJudge],
        candidate_catalog: Sequence[CandidateManifest] = (),
        principal: str = "operator",
        trusted_root_sha: str = TRUSTED_ROOT_SHA,
        ledger: LearningLedger | None = None,
    ) -> None:
        if not trusted_root_sha or trusted_root_sha != TRUSTED_ROOT_SHA:
            raise ValueError("news_learning_trusted_root_invalid")
        self._stable = stable
        self._judges = dict(judges)
        self._candidates = {candidate.candidate_sha: candidate for candidate in candidate_catalog}
        self._principal = principal
        # Every read and write against `news_learning_*` and `news_reviews` goes through one object
        # (#202 §8). It is composed rather than inherited: freezing a dataset, evaluating a candidate and
        # moving a release stage are three lifecycles, and while they shared a class they shared
        # everything — which is why a change to any objective, dataset, metric or release boundary edited
        # this one file.
        # Injectable so a caller that needs a different clock — a fixture pinning "now" beyond a closed
        # window — swaps one object rather than subclassing the evaluator to override a private method.
        self._ledger = ledger or LearningLedger(conn, stable=stable, principal=principal)
        # Named repository methods, not raw SQL. `docs/DEVELOPMENT.md` puts business SQL in the owning
        # package's storage module; this class had 25 statements inlined, which is why splitting it three
        # ways (#202 §8) would otherwise have copied one violation into three files.
        self._repository = NewsRepository(conn)
        self._history = EvaluationReaderHistory(conn)
        # The three lifecycles, composed rather than merged (#202 §8). Freezing a corpus, admitting a
        # candidate and judging one are separate objects now; what is left in this class is the judging.
        self._datasets = DevelopmentDatasetStore(
            conn,
            stable=stable,
            history=self._history,
            ledger=self._ledger,
            principal=principal,
            trusted_root_sha=trusted_root_sha,
        )
        self._registry = CandidateRegistry(
            conn,
            datasets=self._datasets,
            ledger=self._ledger,
            stable=stable,
            catalog=self._candidates,
        )
        self._trusted_root_sha = trusted_root_sha
        self._metric_sha256 = metric_contract_sha256(review_rubric_version=REVIEW_RUBRIC_VERSION)

    async def evaluate(self, request: EvaluationRequest) -> EvaluationReport:
        # Reject a stale constructor arm before loading data or spending one
        # model call. Re-read after execution as well, because a deployment can
        # legitimately change the active root while a long evaluation runs.
        self._ledger.assert_active_stable()
        development = self._datasets.load_dataset(request.development_dataset_sha)
        validation = (
            development
            if request.stage == "offline"
            else self._datasets.load_dataset(str(request.validation_dataset_sha))
        )
        if development.role != "development" or (request.stage != "offline" and validation.role != "validation"):
            raise ValueError("news_learning_dataset_role_invalid")
        if development.agent_cohort != self._ledger.agent_cohort() or (
            request.stage != "offline" and validation.agent_cohort != self._ledger.agent_cohort()
        ):
            raise ValueError("news_learning_dataset_agent_cohort_mismatch")
        if development.reader_contract_version != READER_CONTRACT_VERSION or (
            request.stage != "offline" and validation.reader_contract_version != READER_CONTRACT_VERSION
        ):
            raise ValueError("news_learning_dataset_reader_contract_mismatch")
        candidate = self._registry.load(request.candidate_sha)
        candidate_plan = self._registry.validate(candidate)
        self._registry.persist(candidate)
        prior_stage = {"holdout": "offline", "shadow": "holdout", "canary": "shadow"}.get(request.stage)
        if prior_stage and not self._registry.has_passed_stage(candidate.candidate_sha, prior_stage):
            raise ValueError(f"news_learning_prior_{prior_stage}_evidence_not_passed")
        if candidate.development_dataset_sha != development.artifact_sha:
            raise ValueError("news_learning_candidate_development_dataset_mismatch")
        if request.stage != "offline" and validation.observation_ref != candidate.candidate_sha:
            raise ValueError("news_learning_validation_candidate_mismatch")

        run_sha = evaluation_run_sha(
            request,
            stable_bundle_sha=self._stable.bundle_sha,
            candidate_sha=candidate.candidate_sha,
            trusted_root_sha=self._trusted_root_sha,
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
            development_profile_counts={
                **development.counts,
                **development_split_profile_counts(candidate_plan),
            },
        )
        if observation_manifest_sha:
            evidence["observation_manifest_sha"] = observation_manifest_sha
        outcome = str(evidence["gate_outcome"])
        active_sha = self._ledger.active_stable_sha()
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
        report_sha = self._ledger.persist_artifact(
            "evaluation_report", report_payload, parent_sha=candidate.candidate_sha
        )
        self._ledger.persist_artifact(
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

    async def _run_sequential(
        self,
        *,
        run_sha: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
    ) -> list[dict[str, Any]]:
        states: dict[ArmName, ArmState] = {
            "stable": ArmState(deque(Receipt(**receipt) for receipt in dataset.seed_receipts)),
            "candidate": ArmState(deque(Receipt(**receipt) for receipt in dataset.seed_receipts)),
        }
        arms: dict[ArmName, ArmManifest] = {"stable": self._stable, "candidate": candidate.candidate_arm}
        review_case_ids = self._review_case_ids(dataset, candidate=candidate)
        observations: list[dict[str, Any]] = []
        for case_ref in dataset.cases:
            case = self._datasets.load_case(case_ref)
            case_outputs: dict[str, dict[str, Any]] = {}
            order: list[ArmName] = ["stable", "candidate"]
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
                context = self._datasets.build_context(case, state)
                first = await self._invoke_and_record(
                    run_sha=run_sha,
                    case_id=case_ref.case_id,
                    arm_name=arm_name,
                    arm=arm,
                    context=context,
                    trial=1,
                )
                program_observations = [first]
                scored_judgment = first.get("scored_judgment")
                if scored_judgment is None:
                    case_outputs[arm_name] = {
                        "error_code": first.get("error_code") or "program_output_missing",
                        "delivered": False,
                        "program": [first],
                    }
                    continue
                result = self._apply_policy(case, scored_judgment, state, arm, context)
                result["program"] = list(program_observations)
                case_outputs[arm_name] = result
            # A pre-registered stability subset plus every first-trial disagreement gets k=3.
            if self._needs_stability_trials(case_ref.case_id, case_outputs):
                for arm_name in order:
                    if not case_outputs.get(arm_name, {}).get("scored_judgment"):
                        continue
                    state = states[arm_name]
                    context = self._datasets.build_context(case, state)
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
                    first_judgment = case_outputs[arm_name]["scored_judgment"]
                    serialized_observations: list[dict[str, Any]] = list(case_outputs[arm_name].get("program") or [])
                    serialized_observations.extend(trials)
                    case_outputs[arm_name]["program"] = serialized_observations
                    trial_results: list[dict[str, Any]] = [
                        {
                            "trial": 1,
                            "error_code": case_outputs[arm_name].get("error_code"),
                            "delivered": bool(case_outputs[arm_name].get("delivered")),
                            "scored_judgment_sha256": _sha(first_judgment),
                        }
                    ]
                    for trial_number, trial_observation in zip((2, 3), trials, strict=True):
                        trial_result: dict[str, Any] = {
                            "trial": trial_number,
                            "error_code": trial_observation.get("error_code"),
                            "delivered": False,
                            "scored_judgment_sha256": None,
                        }
                        if trial_observation.get("scored_judgment") is not None:
                            replayed = self._apply_policy(
                                case,
                                trial_observation["scored_judgment"],
                                state,
                                arms[arm_name],
                                self._datasets.build_context(case, state),
                            )
                            trial_result.update(
                                delivered=bool(replayed.get("delivered")),
                                scored_judgment_sha256=_sha(trial_observation["scored_judgment"]),
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
                            item.get("scored_judgment") == first_judgment
                            for item in trials
                            if item.get("scored_judgment") is not None
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
                        receipt_from_output(
                            event_id=case_ref.case_id,
                            at_ms=case_ref.opened_at_ms,
                            output=output,
                            verdict=verdict,
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

        rows = self._repository.stable_arm_review_sources(
            from_ms=dataset.window.from_ms,
            to_ms=dataset.window.to_ms,
            bundle_sha=self._stable.bundle_sha,
            program_version=self._stable.program_version,
            program_sha256=self._stable.program_sha256,
        )
        state = ArmState(deque(Receipt(**receipt) for receipt in dataset.seed_receipts))
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
                "cluster_id": _dataset_fact_cluster(str(focus.get("text") or case_id)),
                "stratum": "shadow_distribution",
                "opened_at_ms": opened_at_ms,
            }
            case = {"snapshot": snapshot, "opened_at_ms": opened_at_ms}
            context = self._datasets.build_context(case, state)
            program_observation = await self._invoke_and_record(
                run_sha=run_sha,
                case_id=case_id,
                arm_name="candidate",
                arm=candidate.candidate_arm,
                context=context,
                trial=1,
            )
            if program_observation.get("scored_judgment") is None:
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
                    program_observation["scored_judgment"],
                    state,
                    candidate.candidate_arm,
                    context,
                )
                candidate_output["execution"] = "live"
                candidate_output["program"] = [program_observation]
            if candidate_output.get("delivered"):
                verdict = dict(candidate_output.get("verdict") or {})
                state.receipts.append(
                    receipt_from_output(
                        event_id=case_id,
                        at_ms=opened_at_ms,
                        output=candidate_output,
                        verdict=verdict,
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

        activation = self._repository.newest_canary_activation_for_candidate(candidate.candidate_sha)
        if activation is None:
            raise ValueError("news_learning_canary_activation_not_found")
        if str(activation["candidate_bundle_sha"]) != candidate.candidate_arm.bundle_sha:
            raise ValueError("news_learning_canary_candidate_bundle_mismatch")
        if str(activation["baseline_bundle_sha"]) != self._stable.bundle_sha:
            raise ValueError("news_learning_canary_stable_bundle_mismatch")
        rows = self._repository.canary_arm_observations(
            activation_id=str(activation["activation_id"]),
            from_ms=dataset.window.from_ms,
            to_ms=dataset.window.to_ms,
        )
        observations: list[dict[str, Any]] = []
        invariant_breaches: list[str] = []
        candidate_n = 0
        for row in rows:
            arm = str(row["arm"])
            expected_agent = candidate.candidate_arm if arm == "candidate" else self._stable
            expected_bundle = expected_agent.bundle_sha
            trace = dict(row.get("trace") or {})
            assignment_trace = dict(trace.get("agent_assignment") or {})
            if (
                arm not in {"stable", "candidate"}
                or str(row["bundle_sha"]) != expected_bundle
                or str(row.get("program_version") or "") != expected_agent.program_version
                or str(row.get("program_sha256") or "") != expected_agent.program_sha256
                or (
                    assignment_trace
                    and (
                        str(assignment_trace.get("arm") or "") != arm
                        or str(assignment_trace.get("bundle_sha") or "") != expected_bundle
                    )
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
                        "cluster_id": _dataset_fact_cluster(str(focus.get("text") or case_id)),
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
            self._ledger.now_ms(),
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

    def _apply_policy(
        self,
        case: Mapping[str, Any],
        raw_judgment: Mapping[str, Any],
        state: ArmState,
        arm: ArmManifest,
        context: TriageContext,
    ) -> dict[str, Any]:
        try:
            judgment = ScoredJudgment.model_validate(raw_judgment)
        except Exception as exc:
            return {"error_code": f"schema_invalid:{type(exc).__name__}", "delivered": False}
        verdict = judgment.verdict
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
            dedupe_family=str(event.get("dedupe_family") or "general"),
        )
        decision = production_decision(
            judgment,
            self._datasets._policy_metric_projection(case, state, context=context, arm=arm),
            member_count=context.evidence.member_count,
            now_ms=context.now_ms,
        )
        delivered = decision.final in {"push", "escalate"}
        return {
            "scored_judgment": judgment.model_dump(mode="json"),
            "verdict": verdict.model_dump(mode="json"),
            "editorial": judgment.editorial.model_dump(mode="json"),
            "final_decision": decision.final,
            "override_rule": decision.override_rule,
            "throttled_by": decision.throttled_by,
            "storyline_key": storyline,
            "comparison_title": str(event.get("comparison_title") or ""),
            "comparison_fingerprint": str(event.get("comparison_fingerprint") or ""),
            "dedupe_family": str(event.get("dedupe_family") or "general"),
            "grounded_assets": list(grounded),
            "canonical_assets": list(self._history.canonical_assets(grounded)),
            "delivered": delivered,
            "execution": "simulated",
            "delivery": "simulated",
        }

    async def _invoke_and_record(
        self,
        *,
        run_sha: str,
        case_id: str,
        arm_name: ArmName,
        arm: ArmManifest,
        context: TriageContext,
        trial: int,
    ) -> dict[str, Any]:
        judge = self._judges.get((arm_name, arm.bundle_sha))
        if judge is None:
            return {
                "verdict": None,
                "scored_judgment": None,
                "program_version": arm.program_version,
                "program_sha256": arm.program_sha256,
                "runtime_model_bindings_sha256": arm.runtime_model_bindings_sha256,
                "trace": {},
                "usage": {},
                "calls": [],
                "error_code": "news_program_artifact_missing",
            }
        observation: dict[str, Any]
        trace_calls: tuple[ProgramCallTrace, ...]
        try:
            judgment = await judge.judge(context)
        except SemanticJudgeError as exc:
            if "recording_missing" in exc.code:
                raise RecordReplayMiss(exc.code) from exc
            partial_trace = exc.partial_trace.model_dump(mode="json") if exc.partial_trace is not None else {}
            trace_calls = exc.partial_trace.calls if exc.partial_trace is not None else ()
            observation = {
                "verdict": None,
                "scored_judgment": None,
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
                or judgment.trace.envelope_sha256 != LEARNING_EXECUTION_ENVELOPE_SHA256
            ):
                raise ValueError("news_program_judgment_identity_mismatch")
            trace_calls = judgment.trace.calls
            observation = judgment.model_dump(mode="json")
            observation["verdict"] = judgment.verdict.model_dump(mode="json")
            observation["scored_judgment"] = judgment.scored().model_dump(mode="json")
            observation["calls"] = list(observation.get("trace", {}).get("calls") or [])
            observation["error_code"] = None
        observation["runtime_model_bindings_sha256"] = arm.runtime_model_bindings_sha256

        context_payload = context.model_dump(mode="json")
        context_sha = _sha(context_payload)
        trace = dict(observation.get("trace") or {})
        trace_context_sha = str(trace.get("context_sha256") or context_sha)
        if trace_context_sha != context_sha:
            raise ValueError("news_program_trace_context_mismatch")
        serialized_calls = list(observation.get("calls") or [])
        if len(serialized_calls) != len(trace_calls):
            raise ValueError("news_program_call_trace_incomplete")
        for call_index, raw_call in enumerate(serialized_calls):
            if not bool(raw_call.get("physical_provider_call")):
                continue
            self._persist_program_call(
                run_sha=run_sha,
                case_id=case_id,
                arm_name=arm_name,
                trial=trial,
                arm=arm,
                trace=trace,
                call_index=call_index,
                raw_call=raw_call,
                recording=trace_calls[call_index].recording,
            )
        return observation

    def _persist_program_call(
        self,
        *,
        run_sha: str,
        case_id: str,
        arm_name: ArmName,
        trial: int,
        arm: ArmManifest,
        trace: Mapping[str, Any],
        call_index: int,
        raw_call: Mapping[str, Any],
        recording: Mapping[str, Any] | None,
    ) -> None:
        call = dict(raw_call)
        if call_index < 0 or not _program_call_identity_complete(call):
            raise ValueError("news_program_call_trace_incomplete")
        predictor_name = str(call.get("predictor") or "")
        attempt = int(call.get("attempt") or 0)
        route = str(call.get("route") or "")
        runtime_provider = str(call.get("runtime_provider") or "")
        runtime_model = str(call.get("runtime_model") or "")
        runtime_model_sha = str(call.get("runtime_model_sha256") or "")
        runtime_binding_sha = str(call.get("runtime_binding_sha256") or "")
        provider = runtime_provider
        request_sha = str(call.get("request_sha256") or "")
        model = runtime_model
        model_sha = runtime_model_sha
        expected_runtime_binding_sha = _sha(
            {
                "provider": runtime_provider,
                "model": runtime_model,
                "model_sha256": runtime_model_sha,
            }
        )
        execution_sha = _sha(
            {
                "program_sha256": arm.program_sha256,
                "runtime_model_bindings_sha256": arm.runtime_model_bindings_sha256,
                "envelope_sha256": trace.get("envelope_sha256"),
                "runtime_binding_sha256": runtime_binding_sha,
                "provider": provider,
            }
        )
        if not model or runtime_binding_sha != expected_runtime_binding_sha:
            raise ValueError("news_program_call_trace_incomplete")
        if recording is None:
            raise ValueError("news_program_call_recording_missing")
        terminal = dict(recording)
        recorded_request = terminal.get("request")
        if not isinstance(recorded_request, Mapping):
            raise ValueError("news_program_call_recording_missing")
        request = dict(recorded_request)
        # Public replay validation proves schema, request address, and exactly
        # one success/error terminal before append-only persistence.
        RecordedLM(
            {request_sha: terminal},
            model=runtime_model,
            runtime_identity=RuntimeModelIdentity.issue(
                provider=runtime_provider,
                model=runtime_model,
                model_sha256=runtime_model_sha,
            ),
            model_binding=str(call.get("model_binding") or ""),
        )
        if (
            len(_json(request).encode()) > MODEL_RECORDING_BYTES_MAX
            or len(_json(terminal).encode()) > MODEL_RECORDING_BYTES_MAX
        ):
            raise ValueError("news_model_recording_oversized")
        response_sha = _sha(terminal)
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
        recording_sha = _sha(identity)
        total_tokens = (
            call.get("total_tokens")
            if call.get("total_tokens") is not None
            else int(call.get("input_tokens") or 0) + int(call.get("output_tokens") or 0)
        )
        expected_recording = {
            "recording_sha": recording_sha,
            "run_sha": run_sha,
            "case_id": case_id,
            "arm": arm_name,
            "trial": trial,
            "predictor_name": predictor_name,
            "call_index": call_index,
            "attempt": attempt,
            "route": route,
            "request_sha256": request_sha,
            "response_sha256": response_sha,
            "request": request,
            "response": terminal,
            "provider": provider,
            "model": model,
            "model_sha": model_sha,
            "execution_contract_sha": execution_sha,
            "latency_ms": call.get("latency_ms"),
            "input_tokens": call.get("input_tokens"),
            "output_tokens": call.get("output_tokens"),
            "cached_tokens": call.get("cached_tokens"),
            "total_tokens": total_tokens,
            "provider_cost_microusd": call.get("provider_cost_microusd"),
            "finish_reason": call.get("finish_reason"),
            "error_code": call.get("error_code"),
        }
        self._repository.append_model_recording(
            (
                recording_sha,
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
                _json(terminal),
                provider,
                model,
                model_sha,
                execution_sha,
                call.get("latency_ms"),
                call.get("input_tokens"),
                call.get("output_tokens"),
                call.get("cached_tokens"),
                total_tokens,
                call.get("provider_cost_microusd"),
                call.get("finish_reason"),
                call.get("error_code"),
                self._ledger.now_ms(),
            )
        )
        persisted = self._repository.model_recording(recording_sha)
        if persisted is None:
            # A different recording_sha can still collide with the composite
            # run/case/arm/trial/Predictor identity.  Never expose the backend's
            # unique-constraint name as the evaluator's behavioral contract.
            raise ValueError("news_model_recording_conflict")
        actual_recording = {key: persisted[key] for key in expected_recording}
        actual_recording["request"] = dict(actual_recording["request"])
        actual_recording["response"] = dict(actual_recording["response"])
        if actual_recording != expected_recording:
            raise ValueError("news_model_recording_conflict")

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
        development_profile_counts: Mapping[str, Any],
    ) -> dict[str, Any]:
        blockers: list[str] = []
        failures: list[str] = []
        if request.stage in {"offline", "holdout"}:
            blockers.extend(development_coverage_blockers(development_profile_counts))
        else:
            prior = "holdout" if request.stage == "shadow" else "shadow"
            if not self._registry.has_passed_stage(candidate.candidate_sha, prior):
                blockers.append(f"prior_{prior}_evidence_not_passed")
        if execution_errors:
            blockers.extend(execution_errors)
        reviews = self._ledger.reviews_by_id(
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
        stable_trace_entries: list[int] = []
        candidate_trace_entries: list[int] = []
        stable_costs: list[int] = []
        candidate_costs: list[int] = []
        stable_latencies: list[int] = []
        candidate_latencies: list[int] = []
        candidate_observed_n = 0
        candidate_bad_n = 0
        candidate_schema_errors = 0
        # [incomplete_n, total_n] per arm; the #292 exemption needs to tell total blindness from partial,
        # and total blindness is a call-level fact — one priced fallback inside an otherwise unpriced
        # observation is still partial pricing.
        provider_cost_obs: dict[str, list[int]] = {"stable": [0, 0], "candidate": [0, 0]}
        provider_cost_any_priced: dict[str, bool] = {"stable": False, "candidate": False}
        program_call_provenance_incomplete = False
        stability: dict[str, list[dict[str, Any]]] = {"stable": [], "candidate": []}
        regression_totals: dict[str, dict[str, Any]] = {
            name: {
                "denominator_n": 0,
                "stable_failure_n": 0,
                "candidate_failure_n": 0,
                "candidate_only_regression_n": 0,
                "candidate_only_case_ids": set(),
            }
            for name in PRODUCTION_REGRESSION_GATES
        }
        for item in observations:
            review = reviews.get(str(item["case_ref"]["review_id"]), {})
            expected = _expected_delivery(str(review.get("should_push") or "uncertain"))
            stable_out = item["stable"]
            candidate_out = item["candidate"]
            for gate, measurement in production_regression_measurements(review, stable_out, candidate_out).items():
                totals = regression_totals[gate]
                totals["denominator_n"] += measurement.denominator_n
                totals["stable_failure_n"] += measurement.stable_failure_n
                totals["candidate_failure_n"] += measurement.candidate_failure_n
                totals["candidate_only_regression_n"] += measurement.candidate_only_regression_n
                if measurement.candidate_only_regression_n:
                    totals["candidate_only_case_ids"].add(str(item["case_ref"]["case_id"]))
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
            stable_errored = bool(stable_out.get("error_code"))
            candidate_errored = bool(candidate_out.get("error_code"))
            if stable_errored:
                stable_errors += 1
            if candidate_errored:
                candidate_errors += 1
            # The only/common partition is comparison language, so it speaks for assigned pairs only;
            # the raw per-arm counts above stay unscoped.
            if not candidate_out.get("not_assigned"):
                if stable_errored and candidate_errored:
                    common_errors += 1
                elif candidate_errored:
                    candidate_only_errors += 1
                elif stable_errored:
                    stable_only_errors += 1
            # An errored arm renders no resource evidence, so counting the healthy side alone would
            # compare a truncated mean against a full one across every resource guardrail below.
            metric_sources: tuple[
                tuple[str, Mapping[str, Any], list[int], list[int], list[int], list[int], list[int]], ...
            ] = ()
            if not (request.stage in {"offline", "holdout"} and (stable_errored or candidate_errored)):
                metric_sources = (
                    (
                        "stable",
                        stable_out,
                        stable_tokens,
                        stable_calls,
                        stable_trace_entries,
                        stable_costs,
                        stable_latencies,
                    ),
                    (
                        "candidate",
                        candidate_out,
                        candidate_tokens,
                        candidate_calls,
                        candidate_trace_entries,
                        candidate_costs,
                        candidate_latencies,
                    ),
                )
            for arm, output, tokens, calls, trace_entries, costs, latencies in metric_sources:
                for program_obs in output.get("program") or []:
                    metric = _program_metric(program_obs)
                    if request.stage in {"offline", "holdout"}:
                        provider_cost_obs[arm][1] += 1
                        if not _provider_cost_observation_complete(program_obs):
                            provider_cost_obs[arm][0] += 1
                        if _any_priced_physical_call(program_obs):
                            provider_cost_any_priced[arm] = True
                    if request.stage in {"offline", "holdout"} and not _program_call_provenance_complete(program_obs):
                        program_call_provenance_incomplete = True
                    if metric["total_tokens"] is not None:
                        tokens.append(int(metric["total_tokens"]))
                    if metric["call_count"] is not None:
                        calls.append(int(metric["call_count"]))
                    if metric["trace_entry_count"] is not None:
                        trace_entries.append(int(metric["trace_entry_count"]))
                    if metric["provider_cost_microusd"] is not None:
                        costs.append(int(metric["provider_cost_microusd"]))
                    if metric["latency_ms"] is not None:
                        latencies.append(int(metric["latency_ms"]))
            if expected is not None and not stable_errored:
                correctness["scored"] += 1
                correctness["stable"] += bool(stable_out.get("delivered")) == expected
                correctness["candidate"] += bool(candidate_out.get("delivered")) == expected
                if (
                    str(review.get("should_push")) == "must_push"
                    and bool(stable_out.get("delivered"))
                    and not bool(candidate_out.get("delivered"))
                ):
                    critical_regressions.append(str(item["case_ref"]["case_id"]))
        taxonomy_evidence: dict[str, Any] | None = None
        if request.stage in {"offline", "holdout"}:
            taxonomy_evidence = _taxonomy_release_evidence(observations, reviews)
            if not int(taxonomy_evidence["stable"]["cluster_n"]):
                blockers.append("taxonomy_release_evidence_empty")
            if taxonomy_evidence["regressed_axes"]:
                failures.append("candidate_taxonomy_axis_regression")
        if critical_regressions:
            failures.append("must_push_regression")
        if candidate_only_errors and request.stage in {"offline", "holdout"}:
            failures.append("candidate_schema_or_provider_regression")
        # A stable-arm or common provider failure makes the comparison unavailable for that pair, and a
        # mass failure must never become a vacuous PASS just because both arms failed the same way. But a
        # handful of transient failures cannot veto a live corpus-scale comparison either: an errored pair
        # is excluded from correctness, from every resource-mean guardrail above and from the pairwise
        # primary below, so the same rate cap that bounds candidate degradation bounds this gap (#294).
        # Candidate-only failures are complete regression evidence and remain FAIL above.
        unavailable_execution_rate, execution_unavailability_blocked = stable_or_common_execution_unavailability(
            stable_only_errors + common_errors, candidate_observed_n
        )
        if request.stage in {"offline", "holdout"} and execution_unavailability_blocked:
            blockers.append("stable_or_common_execution_unavailable")
        # Neither endpoint this deployment runs on reports a resolvable price, and the gate is a delta:
        # two *totally* blind arms lose no comparative information, and the token guardrail above bounds
        # the same spend concern (#292). Any partial observability is different — the cost-mean guardrail
        # would then compare arms over silently different call subsets — so only total symmetric
        # blindness is exempt.
        provider_cost_incomplete_arms = sorted(arm for arm, (inc, _total) in provider_cost_obs.items() if inc)
        provider_cost_symmetrically_unobservable = all(
            total > 0 and inc == total for inc, total in provider_cost_obs.values()
        ) and not any(provider_cost_any_priced.values())
        provider_cost_blocked = bool(provider_cost_incomplete_arms) and not provider_cost_symmetrically_unobservable
        if provider_cost_blocked:
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
        if request.stage == "offline":
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
        regression_gates = {}
        for gate in PRODUCTION_REGRESSION_GATES:
            totals = regression_totals[gate]
            gate_outcome = (
                "unknown"
                if not totals["denominator_n"]
                else "fail"
                if totals["candidate_only_regression_n"]
                else "pass"
            )
            regression_gates[gate] = ProductionRegressionGateEvidenceV1(
                gate=gate,
                metric_sha256=self._metric_sha256,
                denominator_n=totals["denominator_n"],
                stable_failure_n=totals["stable_failure_n"],
                candidate_failure_n=totals["candidate_failure_n"],
                candidate_only_regression_n=totals["candidate_only_regression_n"],
                candidate_only_case_ids=tuple(sorted(totals["candidate_only_case_ids"])),
                outcome=gate_outcome,
            ).model_dump(mode="json")
        return {
            "evaluator_version": EVALUATOR_VERSION,
            "profile": _PROFILE,
            "trusted_root_sha": self._trusted_root_sha,
            "metric_id": METRIC_ID,
            "metric_sha256": self._metric_sha256,
            "regression_gates": regression_gates,
            "taxonomy": taxonomy_evidence,
            "stable_sha": self._stable.bundle_sha,
            "candidate_sha": candidate.candidate_sha,
            "candidate_kind": "prompt",
            "development_dataset_sha": development.artifact_sha,
            "validation_dataset_sha": validation.artifact_sha,
            "observation_n": len(observations),
            "correctness": correctness,
            "primary": primary,
            "reader_load": load,
            "reader_contract_version": READER_CONTRACT_VERSION,
            "reader_contract_sha256": READER_CONTRACT_SHA256,
            "agent_cohort": self._ledger.agent_cohort(),
            "stable_error_n": stable_errors,
            "candidate_error_n": candidate_errors,
            "common_error_n": common_errors,
            "candidate_only_error_n": candidate_only_errors,
            "stable_only_error_n": stable_only_errors,
            "stable_or_common_error_rate": unavailable_execution_rate,
            "execution_incomplete": bool(
                request.stage in {"offline", "holdout"}
                and (execution_unavailability_blocked or provider_cost_blocked or program_call_provenance_incomplete)
            ),
            "provider_cost_observation_complete": not provider_cost_incomplete_arms,
            "provider_cost_observation_incomplete_arms": provider_cost_incomplete_arms,
            "provider_cost_symmetrically_unobservable": provider_cost_symmetrically_unobservable,
            "program_call_provenance_complete": not program_call_provenance_incomplete,
            "stable_mean_total_tokens": statistics.mean(stable_tokens) if stable_tokens else None,
            "candidate_mean_total_tokens": statistics.mean(candidate_tokens) if candidate_tokens else None,
            "stable_mean_call_count": statistics.mean(stable_calls) if stable_calls else None,
            "candidate_mean_call_count": statistics.mean(candidate_calls) if candidate_calls else None,
            "stable_mean_trace_entry_count": (statistics.mean(stable_trace_entries) if stable_trace_entries else None),
            "candidate_mean_trace_entry_count": (
                statistics.mean(candidate_trace_entries) if candidate_trace_entries else None
            ),
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

    def _primary_result(
        self, run_sha: str, candidate: CandidateManifest, observations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        cluster_values: dict[str, list[int]] = {}
        # A pair with an errored arm renders a card against an absence: a blind reviewer preferring the
        # card is reporting the execution failure, not a preference (#294). Such pairs neither count nor
        # stay planned — a cluster whose only eligible cases errored must not hold resolution hostage.
        errored_case_ids = {
            str(item["case_ref"]["case_id"])
            for item in observations
            if item["stable"].get("error_code") or item["candidate"].get("error_code")
        }
        planned_cluster_ids = {
            str(item["case_ref"].get("cluster_id") or item["case_ref"]["case_id"])
            for item in observations
            if bool((item.get("comparison") or {}).get("review_eligible"))
            and str(item["case_ref"]["case_id"]) not in errored_case_ids
        }
        candidate_critical: dict[str, set[str]] = {}
        stable_critical: dict[str, set[str]] = {}
        resolved_cluster_ids: set[str] = set()
        pairwise = self._repository.accepted_pairwise_judgments(run_sha)
        by_id = {str(item["case_ref"]["case_id"]): item for item in observations}
        for row in pairwise:
            case_id = str(row["pairwise_case_id"]).split(":", 1)[-1]
            pair_item = by_id.get(case_id)
            if pair_item is None or case_id in errored_case_ids:
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
        review_budget = self._repository.pairwise_review_budget_used(run_sha)
        return {
            "endpoint": "blind_net_preference",
            "planned_cluster_n": len(planned_cluster_ids),
            "resolved_cluster_n": len(resolved_cluster_ids),
            "review_budget_used": int((review_budget or {}).get("n") or 0),
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

    def _persist_run_cases(
        self,
        run_sha: str,
        dataset: DatasetManifest,
        observations: Sequence[Mapping[str, Any]],
        *,
        stage: str,
    ) -> None:
        now_ms = self._ledger.now_ms()
        for item in observations:
            self._repository.append_learning_run_case(
                run_sha=run_sha,
                case=item["case_ref"],
                dataset_sha=dataset.artifact_sha,
                dataset_role=dataset.role,
                stage=stage,
                stable_observation=item["stable"],
                candidate_observation=item["candidate"],
                comparison=item["comparison"],
                now_ms=now_ms,
            )

    def _load_run_cases(self, run_sha: str) -> list[dict[str, Any]]:
        rows = self._repository.learning_run_cases(run_sha)
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

    def _load_production_observations(
        self,
        *,
        artifact_sha: str,
        stage: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        expected_kind = "shadow_observation" if stage == "shadow" else "canary_observation"
        row = self._repository.learning_artifact(artifact_sha)
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
        return self._ledger.persist_artifact(kind, payload, parent_sha=candidate.candidate_sha)

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
        row = self._repository.newest_observation_manifest(kind=kind, observation_run_sha=run_sha)
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

    @staticmethod
    def _needs_stability_trials(case_id: str, outputs: Mapping[str, Mapping[str, Any]]) -> bool:
        stable = outputs.get("stable") or {}
        candidate = outputs.get("candidate") or {}
        return int(case_id[:4], 16) % 10 == 0 or stable.get("scored_judgment") != candidate.get("scored_judgment")


def _usage_from_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    calls = [dict(item) for item in trace.get("calls") or []]
    physical_calls = [item for item in calls if item.get("physical_provider_call") is True]
    costs = [cost for item in physical_calls if (cost := _call_cost_microusd(item)) is not None]
    return {
        "wall_latency_ms": trace.get("wall_latency_ms"),
        "call_count": len(calls),
        "physical_call_count": len(physical_calls),
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in calls),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in calls),
        "cached_tokens": sum(int(item.get("cached_tokens") or 0) for item in calls),
        "total_tokens": sum(
            int(item.get("total_tokens") or 0)
            or (int(item.get("input_tokens") or 0) + int(item.get("output_tokens") or 0))
            for item in calls
        ),
        "provider_cost_microusd": sum(costs) if len(costs) == len(physical_calls) else None,
    }


def _any_priced_physical_call(observation: Mapping[str, Any]) -> bool:
    """Whether one Program observation carries any priced physical call at all.

    The #292 exemption is for *total* blindness, and observation-level completeness cannot see it: an
    unpriced primary attempt followed by a priced fallback marks the whole observation incomplete while
    real prices exist. Total blindness is a property of the calls, not of the aggregates.
    """

    trace = observation.get("trace") or {}
    calls = list(observation.get("calls") or (trace.get("calls") if isinstance(trace, Mapping) else ()) or [])
    return any(
        isinstance(call, Mapping) and call.get("physical_provider_call") and _call_cost_microusd(dict(call)) is not None
        for call in calls
    )


def _provider_cost_observation_complete(observation: Mapping[str, Any]) -> bool:
    usage = dict(observation.get("usage") or {})
    trace = observation.get("trace") or {}
    calls = list(observation.get("calls") or (trace.get("calls") if isinstance(trace, Mapping) else ()) or [])
    physical_calls = [dict(call) for call in calls if isinstance(call, Mapping) and call.get("physical_provider_call")]
    if usage.get("physical_call_count") is None:
        return False
    if int(usage["physical_call_count"]) != len(physical_calls):
        return False
    if not physical_calls:
        return usage.get("provider_cost_microusd") in {None, 0}
    costs = [_call_cost_microusd(call) for call in physical_calls]
    if any(cost is None for cost in costs) or usage.get("provider_cost_microusd") is None:
        return False
    return int(usage["provider_cost_microusd"]) == sum(int(cost) for cost in costs if cost is not None)


def _program_call_provenance_complete(observation: Mapping[str, Any]) -> bool:
    """Whether one observation *dict* carries the identity a release decision needs.

    Every clause here is trivially true of a judgment this process just built, `envelope_sha256` included.
    That is the point: the function validates a mapping whose provenance the evaluator does not own —
    replayed, stored, or handed in — and its job is to refuse a shape that has lost its identity, not to
    re-derive one. What pins a release cohort to a generation is `_accepted_cases`, which filters on the
    active arm's `program_sha256` and `bundle_sha`; the envelope hash is the code half of that pair.
    """

    usage = dict(observation.get("usage") or {})
    trace = observation.get("trace") or {}
    calls = list(observation.get("calls") or (trace.get("calls") if isinstance(trace, Mapping) else ()) or [])
    physical_calls = [call for call in calls if isinstance(call, Mapping) and call.get("physical_provider_call")]
    envelope_sha256 = str(trace.get("envelope_sha256") or "") if isinstance(trace, Mapping) else ""
    return (
        usage.get("physical_call_count") is not None
        and int(usage["physical_call_count"]) == len(physical_calls)
        and (not physical_calls or envelope_sha256 == LEARNING_EXECUTION_ENVELOPE_SHA256)
        and all(_program_call_identity_complete(call) for call in physical_calls)
    )


def _mean_regressed(stable: Sequence[int], candidate: Sequence[int], *, growth_pct: float) -> bool:
    if not stable or not candidate:
        return False
    stable_mean = statistics.mean(stable)
    candidate_mean = statistics.mean(candidate)
    if stable_mean == 0:
        return candidate_mean > 0
    return candidate_mean > stable_mean * (1 + float(growth_pct))


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


def stable_or_common_execution_unavailability(unavailable_n: int, assigned_pair_n: int) -> tuple[float, bool]:
    """#294: the unavailable-comparison rate and its verdict, from one derivation.

    A stable-arm or common failure removes its pair from every comparison denominator, so a handful of
    transient ones cannot bias the verdict — but a mass failure is still an evidence gap, never a vacuous
    PASS. The cap is the same `candidate_degraded_or_error_rate_max` that bounds candidate degradation;
    a second knob would let the two drift apart while guarding one concern. The denominator is assigned
    pairs, not raw observations: only an assigned pair can produce the numerator, and shadow-shaped
    corpora carry unassigned rows that would otherwise dilute the reported rate.
    """

    if not unavailable_n:
        return 0.0, False
    if not assigned_pair_n:
        return 1.0, True
    rate = unavailable_n / assigned_pair_n
    return rate, rate > float(_PROFILE["guardrails"]["candidate_degraded_or_error_rate_max"])


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
    "evaluation_run_sha",
]
