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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_json, canonical_sha
from ..events.storyline import final_storyline_key
from ..learning.replay import RecordingReplayCapability, RecordingReplayMiss
from ..models import TRIAGE_POLICY_VERSION, TriageVerdict
from ..program.contracts import EditorialEnvelope, ScoredJudgment, SemanticJudge, SemanticJudgeError, TriageContext
from ..program.runtime import PROGRAM_FACTORY_ID
from .compiler.security import CompileRecordV1, validate_compile_record
from .contracts import (
    LEARNING_EPOCH,
    LEARNING_PROFILE_ID,
    LEARNING_PROGRAM_VERSION,
    ArmManifest,
    CandidateManifest,
    ClosedWindow,
    DatasetCaseRef,
    ProposalReceipt,
)
from .evaluation_history import ArmState, EvaluationReaderHistory, Receipt, receipt_from_output
from .objective import DevelopmentEpisode, build_gepa_objective_plan, production_decision
from .projection import (
    _arm_exact_diff,
    _call_cost_microusd,
    _connected_fact_clusters,
    _observation_root,
    _observed_production_output,
    _percentile95,
    _program_call_identity_complete,
    _program_cost_by_predictor,
    _program_metric,
    _recording_verification_roots,
)
from .review import (
    READER_CONTRACT_SHA256,
    READER_CONTRACT_VERSION,
    REVIEW_RUBRIC_VERSION,
    REVIEW_RUBRIC_VERSIONS,
)

DATASET_VERSION: Literal["news_learning_dataset_v1"] = "news_learning_dataset_v1"
EVALUATOR_VERSION = "news_candidate_evaluator_v1"
# Must equal the `reset_reason` migration 0302 wrote for `program_v7`. The evaluator validates the
# epoch row field by field, so a bumped epoch with a stale reason here fails every evaluation.
LEARNING_EPOCH_RESET_REASON = "program_learning_package_split_identity_migration"
# What migration 0303 wrote when it opened `program_v7` — deliberately not the runtime constants. The
# Program root is re-issued *inside* an epoch whenever its serialization or factory changes without
# changing which evidence is eligible (#173/#174, #190, #193), so the row keeps naming what it was
# opened with, exactly as `baseline_program_sha256` already did. Asserting these still detects migration
# drift and ledger corruption; asserting today's values against them would fail a correctly migrated
# database on every in-epoch re-issue.
LEARNING_EPOCH_OPENED_FACTORY_ID = "tracefold.news.program.factory_v5"
LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION = "news_semantic_program_artifact_v2"
# Re-exported, not restated. A second literal here would be one more copy of the identity #193 exists to
# stop duplicating, and it would drift silently the first time the factory is bumped.
LEARNING_PROGRAM_FACTORY_ID = PROGRAM_FACTORY_ID
SETTLEMENT_GRACE_MS = 10 * 60_000
MODEL_RECORDING_BYTES_MAX = 64 * 1024
ArmName = Literal["stable", "candidate"]
ArmJudgeKey = tuple[ArmName, str]

_PROFILE: dict[str, Any] = {
    "profile_id": LEARNING_PROFILE_ID,
    "learning_epoch": LEARNING_EPOCH,
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


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window: ClosedWindow
    role: Literal["development", "validation"]
    profile_id: Literal["news_learning_release_v1"] = LEARNING_PROFILE_ID
    learning_epoch: Literal["program_v7"] = LEARNING_EPOCH
    observation_ref: str | None = None


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_sha: str
    dataset_version: Literal["news_learning_dataset_v1"] = DATASET_VERSION
    role: Literal["development", "validation"]
    profile_id: str
    learning_epoch: Literal["program_v7"]
    learning_epoch_started_at_ms: int = Field(ge=0)
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


class DevelopmentCompileExport(BaseModel):
    """Exact trusted corpus projection handed to the cold compiler seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_payload: dict[str, Any]
    episodes: tuple[dict[str, Any], ...] = Field(min_length=1)


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
    ) -> None:
        if not trusted_root_sha or trusted_root_sha != TRUSTED_ROOT_SHA:
            raise ValueError("news_learning_trusted_root_invalid")
        self._conn = conn
        self._stable = stable
        self._judges = dict(judges)
        self._candidates = {candidate.candidate_sha: candidate for candidate in candidate_catalog}
        self._principal = principal
        self._trusted_root_sha = trusted_root_sha
        self._history = EvaluationReaderHistory(conn)

    async def freeze_dataset(self, spec: DatasetSpec) -> DatasetManifest:
        self._assert_active_stable()
        if spec.learning_epoch != LEARNING_EPOCH:
            raise ValueError("news_learning_epoch_mismatch")
        epoch_started_at_ms = self._learning_epoch_started_at_ms()
        if spec.window.from_ms < epoch_started_at_ms:
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

        cases = self._accepted_cases(
            spec.window,
            freeze_as_of_ms=freeze_as_of_ms,
            epoch_started_at_ms=epoch_started_at_ms,
        )
        seed = self._seed_receipts(spec.window.from_ms, epoch_started_at_ms=epoch_started_at_ms)
        counts = self._dataset_counts(spec, cases)
        payload = {
            "dataset_version": DATASET_VERSION,
            "role": spec.role,
            "profile_id": spec.profile_id,
            "learning_epoch": spec.learning_epoch,
            "learning_epoch_started_at_ms": epoch_started_at_ms,
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
                "learning_epoch_sha": _sha({"epoch": LEARNING_EPOCH, "started_at_ms": epoch_started_at_ms}),
                "rubric_sha": _text_sha(REVIEW_RUBRIC_VERSION),
                "reader_contract_sha": READER_CONTRACT_SHA256,
                "agent_bundle_sha": self._stable.bundle_sha,
                "extraction_sha": _text_sha("news_learning_freeze_query_v1"),
            },
        }
        artifact_sha = self._persist_artifact("dataset", payload)
        return DatasetManifest(artifact_sha=artifact_sha, **payload)

    def development_compile_export(self, dataset_sha: str) -> DevelopmentCompileExport:
        """Seal the sole read-only development export for the cold compiler."""

        self._assert_active_stable()
        dataset_payload = self._load_dataset_payload(dataset_sha)
        dataset = self._validate_dataset_payload(dataset_sha, dataset_payload)
        if dataset.role != "development":
            raise ValueError("news_learning_compile_requires_development_dataset")
        if dataset.agent_cohort != self._agent_cohort():
            raise ValueError("news_learning_dataset_agent_cohort_mismatch")

        episodes = self._project_episodes(
            sorted(dataset.cases, key=lambda item: (item.opened_at_ms, item.case_id)),
            dataset.seed_receipts,
        )
        frozen_episodes = tuple(episodes)
        return DevelopmentCompileExport(
            dataset_sha=dataset_sha,
            dataset_payload=dataset_payload,
            episodes=frozen_episodes,
        )

    def baseline_episodes(
        self,
        window: ClosedWindow,
        *,
        cohort: bool = True,
        limit: int = 500,
    ) -> tuple[dict[str, Any], ...]:
        """Project accepted reviews in a window for the offline baseline. Freezes nothing, writes nothing.

        ``cohort=True`` applies the release-plane eligibility for the exact active Program bundle — the
        population a candidate would later be judged on. ``cohort=False`` drops it and takes every accepted
        event review in the window, which is what a metric-wiring proof needs: the only labelled corpus this
        project has was produced by an arm that has since been retired, and refusing to read it would mean the
        baseline could never be checked against a known number.

        Each episode carries the persisted ``DecisionResult`` projection so a caller can score history as it
        happened instead of as today's ``decide()`` would replay it.  A final-action string is insufficient:
        the same shared ruler also reports the rule and duplicate outcome that produced that action.
        """

        if limit <= 0:
            raise ValueError("news_program_baseline_limit_invalid")
        epoch_started_at_ms = self._learning_epoch_started_at_ms() if cohort else 0
        cases = (
            self._accepted_cases(window, freeze_as_of_ms=self._db_now_ms(), epoch_started_at_ms=epoch_started_at_ms)
            if cohort
            else self._baseline_cases(window)
        )
        cases = tuple(sorted(cases, key=lambda case: (case.opened_at_ms, case.case_id))[:limit])
        if not cases:
            return ()
        seed = self._seed_receipts(window.from_ms, epoch_started_at_ms=epoch_started_at_ms, cohort=cohort)
        decisions = self._recorded_decisions([case.event_id for case in cases if case.event_id])
        return tuple(
            {
                **episode,
                "recorded_decision_result": decisions.get(str(episode.get("event_id") or "")),
            }
            if episode.get("event_id")
            else episode
            for episode in self._with_event_ids(cases, self._project_episodes(cases, seed))
        )

    @staticmethod
    def _with_event_ids(
        cases: Sequence[DatasetCaseRef], episodes: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        by_case = {case.case_id: case for case in cases}
        return tuple(
            {**dict(episode), "event_id": by_case[str(episode["case_id"])].event_id or ""} for episode in episodes
        )

    def _recorded_decisions(self, event_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """The persisted production decision, projected into the one shared ``DecisionResult`` contract."""

        if not event_ids:
            return {}
        rows = self._conn.execute(
            """
            SELECT DISTINCT ON (v.event_id)
                   v.event_id, v.final_decision, v.override_rule, v.throttled_by,
                   v.rule_baseline_decision, v.trace, e.watchlist_hits
              FROM news_verdicts v
             JOIN news_events e ON e.event_id = v.event_id
             WHERE v.stage = 'triage' AND v.event_id = ANY(%s)
             ORDER BY v.event_id, v.created_at_ms DESC
            """,
            (list(event_ids),),
        ).fetchall()
        decisions: dict[str, dict[str, Any]] = {}
        for row in rows:
            trace = dict(row.get("trace") or {})
            decisions[str(row["event_id"])] = {
                "final": str(row["final_decision"] or ""),
                "override_rule": str(row["override_rule"] or "") or None,
                "throttled_by": str(row["throttled_by"] or "") or None,
                "rule_baseline": str(row["rule_baseline_decision"] or ""),
                "watchlist_hits": [str(value) for value in row.get("watchlist_hits") or ()],
                "seen_similarity": trace.get("seen_similarity"),
                # Production persists the matched Event rather than an unstable list offset.  The offset is
                # not needed to score a recorded action, so the typed projection uses its declared sentinel.
                "seen_against": -1,
                "seen_scope": str(trace.get("seen_scope") or ""),
            }
        return decisions

    def _baseline_cases(self, window: ClosedWindow) -> tuple[DatasetCaseRef, ...]:
        """Every accepted event review in the window, with no cohort filter. Baseline-only."""

        rows = self._conn.execute(
            """
            WITH accepted AS (
              SELECT DISTINCT ON (j.event_id) j.*
                FROM news_reviews a
                JOIN news_reviews j ON j.review_id = a.accepts_review_id
               WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'event'
                 AND a.release_eligible AND j.release_eligible
                 AND j.rubric_version = ANY(%s) AND j.reader_contract_version = %s
               ORDER BY j.event_id, a.created_at_ms DESC, a.review_id DESC
            )
            SELECT accepted.*, source.evidence_sha256, source.opened_at_ms,
                   source.final_decision, source.delivery_state, source.evidence_snapshot
              FROM accepted
              JOIN news_review_task_source_v1 source
                ON source.event_id = accepted.event_id
               AND source.evidence_version = accepted.evidence_version
             WHERE source.opened_at_ms >= %s AND source.opened_at_ms < %s AND source.ingest_mode = 'live'
            """,
            (list(REVIEW_RUBRIC_VERSIONS), READER_CONTRACT_VERSION, window.from_ms, window.to_ms),
        ).fetchall()
        drafts: list[tuple[DatasetCaseRef, str, str]] = []
        for row in rows:
            snapshot = dict(row["evidence_snapshot"] or {})
            selection = dict(row.get("selection") or {})
            case_id = _sha(
                {
                    "subject_kind": "event",
                    "event_id": row.get("event_id"),
                    "external_snapshot_id": None,
                    "evidence_sha256": row["evidence_sha256"],
                    "review_id": row["review_id"],
                }
            )
            novelty = dict(row.get("novelty") or {})
            drafts.append(
                (
                    DatasetCaseRef(
                        case_id=case_id,
                        subject_kind="event",
                        event_id=row.get("event_id"),
                        evidence_version=row.get("evidence_version"),
                        evidence_sha256=row["evidence_sha256"],
                        review_id=row["review_id"],
                        cluster_id=_fact_cluster(str((snapshot.get("focus_fact") or {}).get("text") or "")),
                        stratum=str(selection.get("stratum") or "eventless_miss"),
                        should_push=str(row.get("should_push") or "uncertain"),
                        opened_at_ms=int(row["opened_at_ms"]),
                        delivery_truth=(
                            "observed_sent" if str(row.get("delivery_state") or "") == "sent" else "observed_not_sent"
                        ),
                    ),
                    str(novelty.get("duplicate_of") or "")
                    if str(novelty.get("judgment") or "") == "restatement"
                    else "",
                    _sha(
                        {
                            "url": (snapshot.get("card") or {}).get("leader_url"),
                            "focus_fact_id": (snapshot.get("focus_fact") or {}).get("fact_id"),
                        }
                    ),
                )
            )
        return tuple(_connected_fact_clusters(drafts))

    def _project_episodes(
        self,
        cases: Sequence[DatasetCaseRef],
        seed_receipts: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Replay the sent ledger forward over ordered cases and project each one for scoring.

        The compiler export and the offline baseline both call this. Two implementations would let the number
        an operator reads drift from the number GEPA maximizes, which is the whole reason #143 exists.
        """

        state = ArmState(deque(Receipt(**receipt) for receipt in seed_receipts))
        pending: list[Receipt] = []
        episodes: list[dict[str, Any]] = []
        for case_ref in sorted(cases, key=lambda item: (item.opened_at_ms, item.case_id)):
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
                    "policy_metric": self._policy_metric_projection(case, state, context=context),
                    "accepted_review": {
                        "review_id": case_ref.review_id,
                        "should_push": review.get("should_push"),
                        "dimensions": dict(review.get("dimensions") or {}),
                        "novelty": dict(review.get("novelty") or {}),
                        "first_bad_owner": review.get("first_bad_owner"),
                        # The column is `submission.first_bad_owner or _derive_owner(submission)` and cannot
                        # tell the two apart. The submission itself is persisted verbatim, so the payload —
                        # not a new column — is what says whether a human actually blamed the Prompt (#199).
                        "first_bad_owner_explicit": (dict(review.get("payload") or {}).get("first_bad_owner")),
                        "evidence_refs": list(review.get("evidence_refs") or []),
                        "expected": dict((dict(review.get("payload") or {}).get("expected")) or {}),
                        "expected_correction": str(review.get("expected_correction") or ""),
                        "note": str(review.get("note") or ""),
                    },
                    "production_judgment": case.get("production_judgment"),
                }
            )
            judgment_payload = case.get("production_judgment")
            receipt_at_ms = case.get("receipt_at_ms")
            if case_ref.delivery_truth == "observed_sent" and judgment_payload and receipt_at_ms is not None:
                verdict = ScoredJudgment.model_validate(judgment_payload).verdict
                card = dict((case.get("snapshot") or {}).get("card") or {})
                pending.append(
                    Receipt(
                        event_id=str(case_ref.event_id or case_ref.case_id),
                        at_ms=int(receipt_at_ms),
                        storyline_key=str(card.get("storyline_key") or "macro:general"),
                        magnitude=verdict.magnitude,
                        direction=verdict.direction,
                        headline_zh=verdict.headline_zh,
                        comparison_title=str(card.get("comparison_title") or ""),
                        comparison_fingerprint=str(card.get("comparison_fingerprint") or ""),
                        family=str(card.get("family") or "general"),
                        event_type=verdict.event_type,
                        grounded_assets=tuple(str(value) for value in card.get("grounded_assets") or ()),
                        assets=tuple(asset.symbol for asset in verdict.assets),
                        canonical_assets=self._history.canonical_assets(
                            tuple(str(value) for value in card.get("grounded_assets") or ())
                        ),
                    )
                )
        return tuple(episodes)

    def _policy_metric_projection(
        self,
        case: Mapping[str, Any],
        state: ArmState,
        *,
        context: TriageContext,
        arm: ArmManifest | None = None,
    ) -> dict[str, Any]:
        """Everything the cold metric needs to run the exact production ``decide()``, and nothing else.

        The optimizer scores the action the reader would have seen, not the model's intermediate ``decision``
        field, so the compiler needs the Gate facts and the ordered sent ledger that ``decide()`` reads. None of
        it is model-visible: it never reaches a rendered payload, and it carries no control state — operational
        mute and pause are excluded on purpose, because a card muted for operational reasons must not teach the
        Program that its editorial judgment was wrong.
        """

        event = dict((case.get("snapshot") or {}).get("card") or {})
        policy_arm = self._stable if arm is None else arm
        return {
            "gate": {
                "grounded_assets": [str(value) for value in event.get("grounded_assets") or ()],
                "watchlist_symbols": sorted(str(value) for value in case.get("watchlist") or ()),
                "admission": str(event.get("admission") or "candidate"),
                # #154: the optimizer metric has to see what production `decide()` saw, or it is rewarded for
                # an action production would not have taken.
                "source_age_s": event.get("source_age_s"),
            },
            "storyline": {
                "title": str(event.get("leader_title") or ""),
                "family": str(event.get("family") or "general"),
            },
            "seen": [row.as_told_row() for row in self._history.build(case, state).recent_seen_rows],
            "told": [
                {
                    "event_id": entry.event_id,
                    "at_ms": entry.at_ms,
                    "storyline_key": entry.storyline_key,
                    "event_type": entry.event_type,
                    "magnitude": entry.magnitude,
                    "direction": entry.direction,
                    "dir": entry.direction,
                    "headline_zh": entry.headline_zh,
                    "assets": list(entry.symbols),
                }
                for entry in context.told.entries
            ],
            # The policy frozen into the example. Production builds `DecidePolicy(**arm.policy)`; the metric
            # used to import `DEFAULT_POLICY` and call itself a production-action metric regardless, so an
            # operator changing `similarity_max` would have made every offline score describe a policy
            # production never used.
            #
            # `policy_source` is the honest part. This is the *active* arm manifest, which is the arm that
            # ran only for current-cohort episodes. `--all-cohorts` deliberately reaches retired cohorts
            # whose own policy was never sealed, so replaying `decide()` over them applies today's rules to
            # yesterday's corpus. That is a legitimate question and a different one, and the receipt has to
            # say which was asked rather than let a verified hash imply the arm's own policy.
            "policy_version": TRIAGE_POLICY_VERSION,
            "policy_values": dict(policy_arm.policy),
            "policy_source": "active_arm_manifest",
            # The manifest already validated this against its own `policy`; reusing it keeps one convention.
            "policy_sha256": policy_arm.policy_sha256,
        }

    def development_compile_episodes(self, dataset_sha: str) -> tuple[dict[str, Any], ...]:
        """Return the ordered episodes from the sealed compiler export."""

        return self.development_compile_export(dataset_sha).episodes

    async def evaluate(
        self,
        request: EvaluationRequest,
        *,
        recording_replay: RecordingReplayCapability | None = None,
    ) -> EvaluationReport:
        if recording_replay is not None and not isinstance(recording_replay, RecordingReplayCapability):
            raise ValueError("news_learning_recording_replay_capability_invalid")
        if recording_replay is not None and request.stage not in {"offline", "holdout"}:
            raise ValueError(f"news_learning_recording_verification_stage_unsupported:{request.stage}")
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
        if recording_replay is not None and candidate.target != "program":
            raise ValueError("news_learning_recording_verification_target_unsupported:policy")
        self._persist_candidate(candidate)
        prior_stage = {"holdout": "offline", "shadow": "holdout", "canary": "shadow"}.get(request.stage)
        if prior_stage and not self._has_passed_stage(candidate.candidate_sha, prior_stage):
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
        recording_replay_missed = False
        observation_dimensions: dict[str, Any] | None = None
        observation_manifest_sha = request.observation_manifest_sha
        recording_verification = None
        if recording_replay is not None:
            try:
                recording_replay.assert_for_run(run_sha)
                recording_verification = await self._verify_recorded_run(
                    request=request,
                    run_sha=run_sha,
                    dataset=dataset,
                    candidate=candidate,
                    existing=existing,
                    recording_replay=recording_replay,
                )
            except RecordingReplayMiss as exc:
                recording_replay_missed = True
                execution_errors.append(str(exc))
        if not existing and recording_replay is None:
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
        if recording_verification is not None:
            evidence["recording_verification"] = recording_verification
        if recording_replay_missed:
            evidence["execution_incomplete"] = True
            evidence["gate_outcome"] = "unknown"
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
        if self._stable.program_version != LEARNING_PROGRAM_VERSION:
            raise ValueError("news_learning_program_v1_unsupported")
        if candidate.parent_stable_sha != self._stable.bundle_sha:
            raise ValueError("news_learning_candidate_parent_stable_mismatch")
        if candidate.target == "program" and candidate.candidate_arm.program_version != LEARNING_PROGRAM_VERSION:
            raise ValueError("news_learning_program_v1_unsupported")
        stable = self._stable.model_dump(mode="json")
        proposed = candidate.candidate_arm.model_dump(mode="json")
        changed = {key for key in stable if stable[key] != proposed[key]}
        allowed = {"program_sha256"} if candidate.target == "program" else {"policy", "policy_sha256"}
        if not changed or not changed <= allowed:
            raise ValueError(f"news_learning_exact_one_variable_violation:{','.join(sorted(changed))}")
        development = self._load_dataset(candidate.development_dataset_sha)
        if development.role != "development":
            raise ValueError("news_learning_proposal_requires_development_dataset")
        # Re-projected once, here, for both branches: the corpus binding below and the Objective Plan are
        # the same question asked of the same episodes, and projecting them twice would let a review edited
        # between the two reads pass one check and fail the other.
        projected = self.development_compile_export(candidate.development_dataset_sha).episodes
        plan = build_gepa_objective_plan(tuple(DevelopmentEpisode.model_validate(episode) for episode in projected))
        receipt = candidate.proposal_receipt
        if candidate.target == "program":
            if receipt.generator_kind != "model":
                raise ValueError("news_learning_program_generator_must_be_model")
            if candidate.candidate_arm.program_sha256 == self._stable.program_sha256:
                raise ValueError("news_learning_program_sha_unchanged")
            if receipt.program_parent_sha256 != self._stable.program_sha256:
                raise ValueError("news_learning_program_parent_mismatch")
            if receipt.program_candidate_sha256 != candidate.candidate_arm.program_sha256:
                raise ValueError("news_learning_program_candidate_mismatch")
            # One record, checked once. This was five documents cross-bound by hash — a provenance record,
            # a machine diff, a receipt chain, seven content-addressed receipts and a runner receipt — and
            # the same four identities appeared in most of them. What the compile actually has to prove is
            # unchanged: it ran against this parent, this dataset, this runtime target, and produced this
            # Program.
            record = self._compile_record(candidate)
            if record.learning_epoch_started_at_ms != self._learning_epoch_started_at_ms():
                raise ValueError("news_learning_program_compile_epoch_mismatch")
            episodes = list(projected)
            # Not the count: the episodes themselves. `development_compile_export` re-projects them from
            # live reviews and recorded decisions, so a review edited between compile and evaluate leaves
            # the count identical and the corpus different — and the candidate would then be judged
            # against evidence it never compiled on.
            if record.episode_count != len(episodes) or record.episode_projection_root_sha256 != _sha(episodes):
                raise ValueError("news_learning_program_compile_corpus_mismatch")
            if receipt.generator_execution_sha != record.compile_record_sha256:
                raise ValueError("news_learning_program_generator_execution_mismatch")
            # The Objective Plan, rebuilt from the same frozen corpus the compile ran on. A candidate that
            # declares clusters the plan does not call targets was optimized against something else.
            if set(candidate.proposal_receipt.failure_cluster_ids) != set(plan.target_failure_cluster_ids):
                unknown = ",".join(
                    sorted(set(candidate.proposal_receipt.failure_cluster_ids) ^ set(plan.target_failure_cluster_ids))
                )
                raise ValueError(f"news_learning_proposal_failure_cluster_unverified:{unknown}")
            if tuple(candidate.target_dimensions) != plan.target_dimensions:
                raise ValueError("news_learning_proposal_target_dimensions_unverified")
            # The split roots, not the split's shape: `readiness`, the dataset-bound baseline, this record
            # and this re-projection must all name the same train and development-selection halves, or the
            # "before" number a release reads was measured on a different corpus than the winner was picked on.
            if record.run.split != plan.split:
                raise ValueError("news_learning_proposal_split_roots_unverified")
        elif any(
            value is not None
            for value in (
                receipt.program_parent_sha256,
                receipt.program_candidate_sha256,
                receipt.compile_record_sha256,
            )
        ):
            raise ValueError("news_learning_policy_receipt_contains_program_change")
        if candidate.development_dataset_sha != candidate.proposal_receipt.development_dataset_sha:
            raise ValueError("news_learning_proposal_dataset_mismatch")
        if tuple(candidate.target_dimensions) != tuple(candidate.proposal_receipt.declared_target_dimensions):
            raise ValueError("news_learning_target_dimensions_mismatch")
        if candidate.target != "program" and not set(candidate.proposal_receipt.failure_cluster_ids) <= set(
            plan.observed_failure_cluster_ids
        ):
            # A policy candidate is not GEPA's output and its clusters are not Prompt-owned targets, so it is
            # held to the plan's owner-blind superset — the same rule, from the same module, asked a
            # different question. It used to be a second inline heuristic here that also counted a delivery
            # failure as a review failure, which no accepted review ever says.
            unknown = ",".join(
                sorted(set(candidate.proposal_receipt.failure_cluster_ids) - set(plan.observed_failure_cluster_ids))
            )
            raise ValueError(f"news_learning_proposal_failure_cluster_unverified:{unknown}")
        self._verify_registration_receipt(candidate.proposal_receipt)

    def _accepted_cases(
        self,
        window: ClosedWindow,
        *,
        freeze_as_of_ms: int,
        epoch_started_at_ms: int,
    ) -> tuple[DatasetCaseRef, ...]:
        rows = self._conn.execute(
            """
            WITH accepted AS (
              SELECT DISTINCT ON (j.event_id) j.*, a.created_at_ms AS accepted_at_ms
                FROM news_reviews a
                JOIN news_reviews j ON j.review_id = a.accepts_review_id
               WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'event'
                 AND a.release_eligible AND j.release_eligible
                 AND a.created_at_ms >= %s AND j.created_at_ms >= %s
                 AND a.created_at_ms <= %s AND j.rubric_version = ANY(%s)
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
               AND source.program_version = %s AND source.program_sha256 = %s
               AND source.policy_version = %s
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
                epoch_started_at_ms,
                epoch_started_at_ms,
                freeze_as_of_ms,
                list(REVIEW_RUBRIC_VERSIONS),
                READER_CONTRACT_VERSION,
                window.from_ms,
                window.to_ms,
                self._stable.program_version,
                self._stable.program_sha256,
                TRIAGE_POLICY_VERSION,
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
               AND a.release_eligible AND j.release_eligible
               AND a.created_at_ms >= %s AND j.created_at_ms >= %s
               AND a.created_at_ms <= %s AND j.rubric_version = ANY(%s)
               AND j.reader_contract_version = %s
               AND x.created_at_ms >= %s
               AND x.occurred_at_ms >= %s AND x.occurred_at_ms < %s
             ORDER BY j.external_snapshot_id, a.created_at_ms DESC, a.review_id DESC
            """,
            (
                epoch_started_at_ms,
                epoch_started_at_ms,
                freeze_as_of_ms,
                list(REVIEW_RUBRIC_VERSIONS),
                READER_CONTRACT_VERSION,
                epoch_started_at_ms,
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
        # A release cohort is the whole runtime bundle. Mixing model bindings or retrieval identity into a
        # Program/policy cohort would score a candidate against evidence produced by a different executable arm.
        eligible = self._conn.execute(
            "SELECT count(*) AS n FROM news_review_task_source_v1 "
            "WHERE opened_at_ms >= %s AND opened_at_ms < %s AND ingest_mode = 'live' "
            "AND program_version = %s AND program_sha256 = %s AND policy_version = %s "
            "AND trace #>> '{agent_assignment,bundle_sha}' = %s",
            (
                spec.window.from_ms,
                spec.window.to_ms,
                self._stable.program_version,
                self._stable.program_sha256,
                TRIAGE_POLICY_VERSION,
                self._stable.bundle_sha,
            ),
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
            "eligibility": {
                "unit": "agent_bundle_sha",
                "bundle_sha": self._stable.bundle_sha,
                "program_sha256": self._stable.program_sha256,
                "policy_version": TRIAGE_POLICY_VERSION,
                "rubric_versions": list(REVIEW_RUBRIC_VERSIONS),
            },
            "window_duration_hours": round((spec.window.to_ms - spec.window.from_ms) / 3_600_000, 3),
        }

    def _seed_receipts(
        self, from_ms: int, *, epoch_started_at_ms: int, cohort: bool = True
    ) -> tuple[dict[str, Any], ...]:
        """The 48 h receipt source the first cases replay against.

        `cohort=False` drops the arm filter for the same reason `_baseline_cases` does. The ledger is what
        `decide()` reads for the restatement drop and the similarity throttle; scoping it to the current arm
        while the corpus is not would hand the earliest cases an empty ledger and bias every
        `--action-source policy` score toward push, silently.
        """

        return self._history.seed_receipts(
            from_ms=from_ms,
            epoch_started_at_ms=epoch_started_at_ms,
            cohort=cohort,
            program_version=self._stable.program_version,
            program_sha256=self._stable.program_sha256,
            bundle_sha=self._stable.bundle_sha,
        )

    async def _run_sequential(
        self,
        *,
        run_sha: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
        recording_replay: RecordingReplayCapability | None = None,
    ) -> list[dict[str, Any]]:
        states: dict[ArmName, ArmState] = {
            "stable": ArmState(deque(Receipt(**receipt) for receipt in dataset.seed_receipts)),
            "candidate": ArmState(deque(Receipt(**receipt) for receipt in dataset.seed_receipts)),
        }
        arms: dict[ArmName, ArmManifest] = {"stable": self._stable, "candidate": candidate.candidate_arm}
        review_case_ids = self._review_case_ids(dataset, candidate=candidate)
        observations: list[dict[str, Any]] = []
        for case_ref in dataset.cases:
            case = self._load_case(case_ref)
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
                frozen_policy_screen = (
                    candidate.target == "policy"
                    and dataset.role == "development"
                    and case.get("production_judgment") is not None
                )
                # Both branches need it: the policy screen skips the model, not the told context, because
                # `decide()` reads the same selected ledger the model would have been shown.
                context = self._build_context(case, state)
                if frozen_policy_screen:
                    scored_judgment = case.get("production_judgment")
                    program_observations: list[dict[str, Any]] = []
                    if scored_judgment is None:
                        case_outputs[arm_name] = {
                            "error_code": "frozen_scored_judgment_missing",
                            "delivered": False,
                        }
                        continue
                else:
                    first = await self._invoke_and_record(
                        run_sha=run_sha,
                        case_id=case_ref.case_id,
                        arm_name=arm_name,
                        arm=arm,
                        context=context,
                        trial=1,
                        persist_recordings=recording_replay is None,
                        recording_replay=recording_replay,
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
            if candidate.target != "policy" and self._needs_stability_trials(case_ref.case_id, case_outputs):
                for arm_name in order:
                    if not case_outputs.get(arm_name, {}).get("scored_judgment"):
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
                            persist_recordings=recording_replay is None,
                            recording_replay=recording_replay,
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
                                self._build_context(case, state),
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

    async def _verify_recorded_run(
        self,
        *,
        request: EvaluationRequest,
        run_sha: str,
        dataset: DatasetManifest,
        candidate: CandidateManifest,
        existing: Sequence[Mapping[str, Any]],
        recording_replay: RecordingReplayCapability,
    ) -> dict[str, Any]:
        """Re-execute an existing corpus through its supplied replay judges without appending model truth."""

        if not existing:
            raise RecordingReplayMiss("news_learning_recording_verification_cases_missing")
        try:
            replayed = await self._run_sequential(
                run_sha=run_sha,
                dataset=dataset,
                candidate=candidate,
                recording_replay=recording_replay,
            )
        except RecordReplayMiss as exc:
            raise RecordingReplayMiss(f"news_learning_recording_verification_miss:{exc}") from exc

        expected_roots, expected_root = _recording_verification_roots(existing)
        actual_roots, actual_root = _recording_verification_roots(replayed)
        if expected_roots.keys() != actual_roots.keys():
            raise ValueError("news_learning_recording_verification_case_set_mismatch")
        for case_id, expected in expected_roots.items():
            if actual_roots[case_id] != expected:
                raise ValueError(f"news_learning_recording_verification_mismatch:{case_id}")
        if actual_root != expected_root:
            raise ValueError("news_learning_recording_verification_root_mismatch")
        replay_receipt = recording_replay.sealed_receipt()
        return {
            "mode": "strict_record_replay_v1",
            **replay_receipt,
            "case_n": len(expected_roots),
            "observation_root": expected_root,
        }

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
               AND trace #>> '{agent_assignment,arm}' = 'stable'
               AND trace #>> '{agent_assignment,bundle_sha}' = %s
               AND program_version = %s AND program_sha256 = %s
             ORDER BY opened_at_ms, event_id, evidence_version
            """,
            (
                dataset.window.from_ms,
                dataset.window.to_ms,
                self._stable.bundle_sha,
                self._stable.program_version,
                self._stable.program_sha256,
            ),
        ).fetchall()
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
                   v.verdict, v.editorial, v.scored_judgment_sha256, v.runtime_manifest_sha,
                   v.final_decision, v.degraded, v.error_code AS verdict_error_code,
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

    def _build_context(self, case: Mapping[str, Any], state: ArmState) -> TriageContext:
        snapshot = case["snapshot"]
        event = dict(snapshot.get("card") or {})
        focus = dict(snapshot.get("focus_fact") or {})
        event["focus_fact_id"] = focus.get("fact_id")
        event["leader_title"] = focus.get("text") or event.get("leader_title")
        event["leader_description"] = focus.get("context") or event.get("leader_description")
        told_rows = [row.as_told_row() for row in self._history.build(case, state).told_source_rows]
        return TriageContext.from_card(
            event,
            watchlist=tuple(str(value) for value in case.get("watchlist") or ()),
            told_rows=told_rows,
            now_ms=int(case["opened_at_ms"]),
            queue_lag_ms=0,
        )

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
            family=str(event.get("family") or "general"),
        )
        decision = production_decision(
            judgment,
            self._policy_metric_projection(case, state, context=context, arm=arm),
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
            "family": str(event.get("family") or "general"),
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
        persist_recordings: bool = True,
        recording_replay: RecordingReplayCapability | None = None,
    ) -> dict[str, Any]:
        judge = self._judges.get((arm_name, arm.bundle_sha)) if recording_replay is None else None
        if judge is None and recording_replay is None:
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
        try:
            if recording_replay is not None:
                judgment = await recording_replay.judge(
                    arm=arm_name,
                    bundle_sha=arm.bundle_sha,
                    case_id=case_id,
                    trial=trial,
                    context=context,
                )
            else:
                if judge is None:  # pragma: no cover - guarded by the artifact-missing return above
                    raise RuntimeError("news_program_artifact_missing")
                judgment = await judge.judge(context)
        except SemanticJudgeError as exc:
            if "recording_missing" in exc.code:
                raise RecordReplayMiss(exc.code) from exc
            partial_trace = exc.partial_trace.model_dump(mode="json") if exc.partial_trace is not None else {}
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
                or judgment.trace.factory_id != LEARNING_PROGRAM_FACTORY_ID
            ):
                raise ValueError("news_program_judgment_identity_mismatch")
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
        if persist_recordings:
            for call_index, raw_call in enumerate(observation.get("calls") or []):
                if not bool(raw_call.get("physical_provider_call")):
                    continue
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
        arm_name: ArmName,
        trial: int,
        arm: ArmManifest,
        context_sha: str,
        trace: Mapping[str, Any],
        call_index: int,
        raw_call: Mapping[str, Any],
    ) -> None:
        call = dict(raw_call)
        if call_index < 0 or not _program_call_identity_complete(call):
            raise ValueError("news_program_call_trace_incomplete")
        predictor_name = str(call.get("predictor") or "")
        attempt = int(call.get("attempt") or 0)
        route = str(call.get("route") or "")
        model_binding = str(call.get("model_binding") or "")
        runtime_provider = str(call.get("runtime_provider") or "")
        runtime_model = str(call.get("runtime_model") or "")
        runtime_model_sha = str(call.get("runtime_model_sha256") or "")
        runtime_binding_sha = str(call.get("runtime_binding_sha256") or "")
        response_provider = str(call.get("provider") or "")
        response_model = str(call.get("model") or "")
        response_model_sha = str(call.get("model_sha256") or "")
        provider = response_provider or "unobserved"
        request_sha = str(call.get("request_sha256") or "")
        input_sha = str(call.get("input_sha256") or "")
        model = response_model or "unobserved"
        model_sha = response_model_sha or _sha({"provider": provider, "model": model})
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
                "factory_id": trace.get("factory_id"),
                "runtime_binding_sha256": runtime_binding_sha,
                "provider": provider,
            }
        )
        if not model or runtime_binding_sha != expected_runtime_binding_sha:
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
            "model_binding": model_binding,
            "runtime_provider": runtime_provider,
            "runtime_model": runtime_model,
            "runtime_model_sha256": runtime_model_sha,
            "runtime_binding_sha256": runtime_binding_sha,
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
                "runtime_binding_sha256": runtime_binding_sha,
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
            "response": response,
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
            ) ON CONFLICT DO NOTHING
            """,
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
                _json(response) if response is not None else None,
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
                self._db_now_ms(),
            ),
        )
        persisted = self._conn.execute(
            """
            SELECT recording_sha, run_sha, case_id, arm, trial, predictor_name, call_index, attempt, route,
                   request_sha256, response_sha256, request, response, provider, model, model_sha,
                   execution_contract_sha, latency_ms, input_tokens, output_tokens, cached_tokens, total_tokens,
                   provider_cost_microusd, finish_reason, error_code
              FROM news_model_recordings
             WHERE recording_sha = %s
            """,
            (recording_sha,),
        ).fetchone()
        if persisted is None:
            # A different recording_sha can still collide with the composite
            # run/case/arm/trial/Predictor identity.  Never expose the backend's
            # unique-constraint name as the evaluator's behavioral contract.
            raise ValueError("news_model_recording_conflict")
        actual_recording = {key: persisted[key] for key in expected_recording}
        actual_recording["request"] = dict(actual_recording["request"])
        if actual_recording["response"] is not None:
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
        stable_trace_entries: list[int] = []
        candidate_trace_entries: list[int] = []
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
            for output, tokens, calls, trace_entries, costs, latencies in (
                (
                    stable_out,
                    stable_tokens,
                    stable_calls,
                    stable_trace_entries,
                    stable_costs,
                    stable_latencies,
                ),
                (
                    candidate_out,
                    candidate_tokens,
                    candidate_calls,
                    candidate_trace_entries,
                    candidate_costs,
                    candidate_latencies,
                ),
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
                    if metric["trace_entry_count"] is not None:
                        trace_entries.append(int(metric["trace_entry_count"]))
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
            production_judgment: dict[str, Any] | None = None
            if row.get("verdict") is not None and row.get("editorial") is not None:
                scored = ScoredJudgment.issue(
                    verdict=TriageVerdict.model_validate(row["verdict"]),
                    editorial=EditorialEnvelope.model_validate(row["editorial"]),
                )
                if str(row.get("scored_judgment_sha256") or "") != scored.scored_judgment_sha256:
                    raise ValueError("news_learning_scored_judgment_identity_mismatch")
                production_judgment = scored.model_dump(mode="json")
            return {
                "snapshot": dict(row["evidence_snapshot"] or {}),
                "opened_at_ms": int(row["opened_at_ms"]),
                "receipt_at_ms": int(row["settled_at_ms"]) if row.get("settled_at_ms") is not None else None,
                "production_judgment": production_judgment,
                "review": dict(review),
                "watchlist": list((row.get("trace") or {}).get("watchlist") or []),
            }
        row = self._conn.execute(
            "SELECT * FROM news_external_miss_snapshots WHERE snapshot_id = %s", (case.external_snapshot_id,)
        ).fetchone()
        if row is None or row["evidence_sha256"] != case.evidence_sha256:
            raise ValueError("news_learning_external_evidence_changed")
        snapshot = dict(row["snapshot"] or {})
        synthetic = {
            "schema_version": "news_event_evidence_v2",
            "focus_fact": {"fact_id": case.case_id, "text": snapshot["title"], "context": snapshot.get("body", "")},
            "card": {
                "event_id": case.case_id,
                "evidence_version": 0,
                "evidence_sha256": case.evidence_sha256,
                "focus_fact_id": case.case_id,
                "leader_title": snapshot["title"],
                "leader_description": snapshot.get("body", ""),
                "leader_url": snapshot["source_url"],
                "reporting_origin": snapshot.get("provenance", "operator"),
                "family": "general",
                "admission": "external_miss",
                "queue_priority": "normal",
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
            "production_judgment": None,
            "review": dict(review),
            "watchlist": [],
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

    def _compile_record(self, candidate: CandidateManifest) -> CompileRecordV1:
        """Load the one persisted record this candidate names, and re-verify it against the candidate."""

        receipt = candidate.proposal_receipt
        rows = self._conn.execute(
            "SELECT artifact_sha, parent_sha, payload FROM news_learning_artifacts "
            "WHERE kind = 'compile_record' AND artifact_sha = %s",
            (receipt.compile_record_sha256,),
        ).fetchall()
        if not rows:
            raise ValueError("news_learning_program_compile_record_missing")
        row = rows[0]
        payload = dict(row["payload"] or {})
        if str(row.get("parent_sha") or "") != candidate.development_dataset_sha:
            raise ValueError("news_learning_program_compile_record_parent_mismatch")
        try:
            record = validate_compile_record(
                payload,
                parent_program_sha256=str(receipt.program_parent_sha256),
                program_sha256=str(receipt.program_candidate_sha256),
                development_dataset_sha256=candidate.development_dataset_sha,
                target_runtime_manifest_sha256=candidate.candidate_arm.runtime_model_bindings_sha256,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("news_learning_program_compile_record_invalid") from exc
        # The record is stored under its own root, so a byte changed in the payload either stops the
        # document validating or stops it answering to the key the receipt points at.
        if record.compile_record_sha256 != str(row["artifact_sha"]):
            raise ValueError("news_learning_program_compile_record_identity_mismatch")
        if record.model_dump(mode="json") != payload:
            raise ValueError("news_learning_program_compile_record_noncanonical")
        return record

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

    def _load_dataset_payload(self, artifact_sha: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM news_learning_artifacts WHERE artifact_sha = %s AND kind = 'dataset'", (artifact_sha,)
        ).fetchone()
        if row is None:
            raise ValueError("news_learning_dataset_not_found")
        payload = dict(row["payload"] or {})
        if _sha({"kind": "dataset", "payload": payload}) != artifact_sha:
            raise ValueError("news_learning_dataset_artifact_hash_mismatch")
        return payload

    def _validate_dataset_payload(self, artifact_sha: str, payload: Mapping[str, Any]) -> DatasetManifest:
        exact_payload = dict(payload)
        if exact_payload.get("learning_epoch") != LEARNING_EPOCH:
            raise ValueError("news_learning_epoch_mismatch")
        epoch_started_at_ms = self._learning_epoch_started_at_ms()
        if exact_payload.get("learning_epoch_started_at_ms") != epoch_started_at_ms:
            raise ValueError("news_learning_epoch_deployment_mismatch")
        hashes = dict(exact_payload.get("hashes") or {})
        expected_epoch_sha = _sha({"epoch": LEARNING_EPOCH, "started_at_ms": epoch_started_at_ms})
        if hashes.get("learning_epoch_sha") != expected_epoch_sha:
            raise ValueError("news_learning_epoch_hash_mismatch")
        if exact_payload.get("profile_id") != LEARNING_PROFILE_ID:
            raise ValueError("news_learning_profile_mismatch")
        expected_hashes = {
            "trusted_root_sha": self._trusted_root_sha,
            "learning_epoch_sha": expected_epoch_sha,
            "rubric_sha": _text_sha(REVIEW_RUBRIC_VERSION),
            "reader_contract_sha": READER_CONTRACT_SHA256,
            "agent_bundle_sha": self._stable.bundle_sha,
            "extraction_sha": _text_sha("news_learning_freeze_query_v1"),
        }
        if hashes != expected_hashes:
            raise ValueError("news_learning_dataset_contract_hash_mismatch")
        if exact_payload.get("reader_contract_version") != READER_CONTRACT_VERSION:
            raise ValueError("news_learning_dataset_reader_contract_mismatch")
        return DatasetManifest(artifact_sha=artifact_sha, **exact_payload)

    def _load_dataset(self, artifact_sha: str) -> DatasetManifest:
        return self._validate_dataset_payload(artifact_sha, self._load_dataset_payload(artifact_sha))

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
        if self._stable.program_version != LEARNING_PROGRAM_VERSION:
            raise ValueError("news_learning_program_v1_unsupported")
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

    def _learning_epoch_started_at_ms(self) -> int:
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

    def _db_now_ms(self) -> int:
        row = self._conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
        ).fetchone()
        return int(row["now_ms"])

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

    Every clause here is trivially true of a judgment this process just built, `factory_id` included.
    That is the point: the function validates a mapping whose provenance the evaluator does not own —
    replayed, stored, or handed in — and its job is to refuse a shape that has lost its identity, not to
    re-derive one. What pins a release cohort to a generation is `_accepted_cases`, which filters on the
    active arm's `program_sha256` and `bundle_sha`; the factory id is a coarser cross-check beside it.
    """

    usage = dict(observation.get("usage") or {})
    trace = observation.get("trace") or {}
    calls = list(observation.get("calls") or (trace.get("calls") if isinstance(trace, Mapping) else ()) or [])
    physical_calls = [call for call in calls if isinstance(call, Mapping) and call.get("physical_provider_call")]
    factory_id = str(trace.get("factory_id") or "") if isinstance(trace, Mapping) else ""
    return (
        usage.get("physical_call_count") is not None
        and int(usage["physical_call_count"]) == len(physical_calls)
        and (not physical_calls or factory_id == LEARNING_PROGRAM_FACTORY_ID)
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


def _expected_delivery(should_push: str) -> bool | None:
    if should_push in {"must_push", "should_push"}:
        return True
    if should_push in {"must_hold", "should_hold"}:
        return False
    return None


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


def _text_sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
    "LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION",
    "LEARNING_EPOCH_OPENED_FACTORY_ID",
    "LEARNING_EPOCH_RESET_REASON",
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
