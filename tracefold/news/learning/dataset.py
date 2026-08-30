"""Freezing, loading and projecting the immutable corpora the learning plane measures against.

Split out of `CandidateEvaluator` (#202 §8). Three lifecycles reached the same 3,000-line class: freezing
a dataset, evaluating a candidate, and moving a release stage. This is the first — everything that turns
accepted human review into a frozen, content-addressed corpus, and nothing that judges one.

It reaches PostgreSQL only through named repository methods, and it does not import the release plane.
That direction is what `freeze_dataset`'s `admitted` parameter buys: sealing a validation dataset needs a
candidate that has been admitted, and asking the release plane for one from here would close a cycle —
release validation re-derives the Objective Plan from `development_compile_export`, which lives here.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_sha
from ..models import TRIAGE_POLICY_VERSION, TriageVerdict
from ..program.contracts import EditorialEnvelope, ScoredJudgment, TriageContext
from ..review.desk import (
    READER_CONTRACT_SHA256,
    READER_CONTRACT_VERSION,
    REVIEW_RUBRIC_VERSION,
    REVIEW_RUBRIC_VERSIONS,
)
from ..storage.root import NewsRepository
from .contracts import (
    LEARNING_PROFILE_ID,
    ArmManifest,
    ClosedWindow,
    DatasetCaseRef,
    epoch_id_for_bundle,
    is_bundle_sha,
)
from .evaluation_history import ArmState, EvaluationReaderHistory, Receipt
from .ledger import LearningLedger
from .profile import _PROFILE, TRUSTED_ROOT_SHA
from .projection import _connected_fact_clusters

DATASET_VERSION: Literal["news_learning_dataset_v2"] = "news_learning_dataset_v2"
# A window whose tail is still settling is not closed: the outcome loop keeps writing prices for minutes
# after an Event opens, so freezing to "now" seals cases whose scores change after the file is written.
SETTLEMENT_GRACE_MS = 10 * 60_000


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window: ClosedWindow
    role: Literal["development", "validation"]
    profile_id: Literal["news_learning_release_v2"] = LEARNING_PROFILE_ID
    # No `learning_epoch` (#314). It was a declared field defaulting to a module constant, which meant a
    # caller could name an epoch and the freeze would then check the name against the same constant it came
    # from. The epoch a freeze belongs to is a fact about the deployment, and the ledger is the one place
    # that knows it.
    observation_ref: str | None = None


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_sha: str
    dataset_version: Literal["news_learning_dataset_v2"] = DATASET_VERSION
    role: Literal["development", "validation"]
    profile_id: str
    learning_epoch: str = Field(pattern=r"^bundle_[0-9a-f]{8}$")
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
    """Exact read-only corpus projection handed to the offline optimizer.

    It carries the projection root and the epoch it was taken under, rather than leaving each caller to
    recompute them: the export *is* the corpus receipt, and two callers deriving the same identity two ways
    is how the two stopped agreeing before.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_payload: dict[str, Any]
    episodes: tuple[dict[str, Any], ...] = Field(min_length=1)
    episode_projection_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    learning_epoch_started_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _root_addresses_these_episodes(self) -> DevelopmentCompileExport:
        if self.episode_projection_root_sha256 != _sha(list(self.episodes)):
            raise ValueError("news_learning_compile_export_projection_root_mismatch")
        return self


def _fact_cluster(text: str) -> str:
    normalized = "".join(str(text or "").lower().split())
    return _text_sha(normalized)


def _text_sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# The one named extraction seal a dataset may carry. The #300 migration carry
# (`news_learning_migration_freeze_v1`) was deleted in #343 with no migrated dataset ever sealed.
_EXTRACTION_SEAL_SHA = _text_sha("news_learning_freeze_query_v1")


@dataclass(frozen=True)
class AdmittedCandidate:
    """A candidate the release plane has already admitted, and the instant it was registered.

    Two facts, because two facts are all `freeze_dataset` needs: that a candidate was admitted at all, and
    when — a holdout window opening before registration is not future evidence, whatever the manifest
    says. Only `news.release.candidate` produces one; an architecture test says so, because a value anyone
    could construct would make the check it exists for a formality.
    """

    candidate_sha: str
    registered_at_ms: int


class DevelopmentDatasetStore:
    """Frozen corpora: seal one, load one, and project its cases into scorable episodes."""

    def __init__(
        self,
        conn: Any,
        *,
        stable: ArmManifest,
        history: EvaluationReaderHistory | None = None,
        ledger: LearningLedger | None = None,
        principal: str = "operator",
        trusted_root_sha: str = TRUSTED_ROOT_SHA,
        profile: Mapping[str, Any] = _PROFILE,
    ) -> None:
        """Constructible on its own: freezing and projecting a corpus needs no judge and no Program."""

        if not trusted_root_sha or trusted_root_sha != TRUSTED_ROOT_SHA:
            raise ValueError("news_learning_trusted_root_invalid")
        history = history if history is not None else EvaluationReaderHistory(conn)
        ledger = ledger if ledger is not None else LearningLedger(conn, stable=stable, principal=principal)
        self._repository = NewsRepository(conn)
        self._history = history
        self._ledger = ledger
        self._stable = stable
        self._trusted_root_sha = trusted_root_sha
        self._profile = profile

    async def freeze_dataset(self, spec: DatasetSpec, *, admitted: AdmittedCandidate | None = None) -> DatasetManifest:
        self._ledger.assert_active_stable()
        epoch_started_at_ms = self._ledger.epoch_started_at_ms()
        if spec.window.from_ms < epoch_started_at_ms:
            raise ValueError("news_learning_window_precedes_program_epoch")
        freeze_as_of_ms = self._ledger.now_ms()
        if spec.window.to_ms > freeze_as_of_ms - SETTLEMENT_GRACE_MS:
            raise ValueError("news_learning_window_not_settled")
        if spec.role == "validation":
            if not spec.observation_ref:
                raise ValueError("news_learning_validation_candidate_required")
            # Admitted by the release plane, not here. Sealing a validation dataset needs exactly two facts
            # about a candidate — that it was admitted at all, and when — and taking them as a value the
            # release plane produces is what keeps the dependency one-way. Until #202 this method reached
            # into candidate validation, which reached back into `development_compile_export` to rebuild
            # the Objective Plan: a cycle, and the reason freezing a dataset and admitting a candidate
            # could not be told apart.
            if admitted is None or admitted.candidate_sha != spec.observation_ref:
                raise ValueError("news_learning_validation_candidate_not_admitted")
            if spec.window.from_ms <= admitted.registered_at_ms:
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
            "learning_epoch": self._ledger.epoch_id(),
            "learning_epoch_started_at_ms": epoch_started_at_ms,
            "window": spec.window.model_dump(mode="json"),
            "freeze_as_of_ms": freeze_as_of_ms,
            "settlement_grace_ms": SETTLEMENT_GRACE_MS,
            "reader_contract_version": READER_CONTRACT_VERSION,
            "agent_cohort": self._ledger.agent_cohort(),
            "observation_ref": spec.observation_ref,
            "cases": [case.model_dump(mode="json") for case in cases],
            "seed_receipts": seed,
            "counts": counts,
            "hashes": {
                "trusted_root_sha": self._trusted_root_sha,
                "learning_epoch_sha": _sha({"epoch": self._ledger.epoch_id(), "started_at_ms": epoch_started_at_ms}),
                "rubric_sha": _text_sha(REVIEW_RUBRIC_VERSION),
                "reader_contract_sha": READER_CONTRACT_SHA256,
                "agent_bundle_sha": self._stable.bundle_sha,
                "extraction_sha": _text_sha("news_learning_freeze_query_v1"),
            },
        }
        artifact_sha = self._ledger.persist_artifact("dataset", payload)
        return DatasetManifest(artifact_sha=artifact_sha, **payload)

    def development_compile_export(self, dataset_sha: str) -> DevelopmentCompileExport:
        """Seal the sole read-only development export for the cold compiler."""

        self._ledger.assert_active_stable()
        dataset_payload = self._load_dataset_payload(dataset_sha)
        dataset = self._validate_dataset_payload(dataset_sha, dataset_payload)
        if dataset.role != "development":
            raise ValueError("news_learning_compile_requires_development_dataset")
        if dataset.agent_cohort != self._ledger.agent_cohort():
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
            episode_projection_root_sha256=_sha(list(frozen_episodes)),
            learning_epoch_started_at_ms=self._ledger.epoch_started_at_ms(),
        )

    def baseline_episodes(
        self,
        window: ClosedWindow,
        *,
        limit: int = 500,
    ) -> tuple[dict[str, Any], ...]:
        """Project accepted reviews in a window for the offline baseline. Freezes nothing, writes nothing.

        The exact active Program bundle is the only current population.  Rows
        from earlier epochs remain audit evidence and are not projected.

        Each episode carries the persisted ``DecisionResult`` projection so a caller can score history as it
        happened instead of as today's ``decide()`` would replay it.  A final-action string is insufficient:
        the same shared ruler also reports the rule and duplicate outcome that produced that action.
        """

        if limit <= 0:
            raise ValueError("news_program_baseline_limit_invalid")
        epoch_started_at_ms = self._ledger.epoch_started_at_ms()
        cases = self._accepted_cases(
            window,
            freeze_as_of_ms=self._ledger.now_ms(),
            epoch_started_at_ms=epoch_started_at_ms,
        )
        cases = tuple(sorted(cases, key=lambda case: (case.opened_at_ms, case.case_id))[:limit])
        if not cases:
            return ()
        seed = self._seed_receipts(window.from_ms, epoch_started_at_ms=epoch_started_at_ms)
        return self._with_recorded_decisions(cases, self._project_episodes(cases, seed))

    def _with_recorded_decisions(
        self, cases: Sequence[DatasetCaseRef], episodes: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        """Attach the Event id and the persisted ``DecisionResult`` the ``recorded`` arm scores against."""

        decisions = self._recorded_decisions([case.event_id for case in cases if case.event_id])
        return tuple(
            {**episode, "recorded_decision_result": decisions.get(str(episode.get("event_id") or ""))}
            if episode.get("event_id")
            else episode
            for episode in self._with_event_ids(cases, episodes)
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
        rows = self._repository.recorded_triage_decisions(event_ids)
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
            case = self.load_case(case_ref)
            context = self.build_context(case, state)
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
                        why_zh=verdict.why_zh,
                        comparison_title=str(card.get("comparison_title") or ""),
                        comparison_fingerprint=str(card.get("comparison_fingerprint") or ""),
                        dedupe_family=str(card.get("dedupe_family") or "general"),
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
                "dedupe_family": str(event.get("dedupe_family") or "general"),
            },
            "seen": [row.as_told_row() for row in self._history.build(case, state).recent_seen_rows],
            "told": [
                {
                    "event_id": entry.event_id,
                    "at_ms": entry.at_ms,
                    "storyline_key": entry.storyline_key,
                    "magnitude": entry.magnitude,
                    "direction": entry.direction,
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
            # ran for this exact current cohort.
            "policy_version": TRIAGE_POLICY_VERSION,
            "policy_values": dict(policy_arm.policy),
            "policy_source": "active_arm_manifest",
            # The manifest already validated this against its own `policy`; reusing it keeps one convention.
            "policy_sha256": policy_arm.policy_sha256,
        }

    def development_compile_episodes(self, dataset_sha: str) -> tuple[dict[str, Any], ...]:
        """Return the ordered episodes from the sealed compiler export."""

        return self.development_compile_export(dataset_sha).episodes

    def _accepted_cases(
        self,
        window: ClosedWindow,
        *,
        freeze_as_of_ms: int,
        epoch_started_at_ms: int,
    ) -> tuple[DatasetCaseRef, ...]:
        rows = self._repository.accepted_event_reviews_in_window(
            epoch_started_at_ms=epoch_started_at_ms,
            freeze_as_of_ms=freeze_as_of_ms,
            rubric_versions=REVIEW_RUBRIC_VERSIONS,
            reader_contract_version=READER_CONTRACT_VERSION,
            from_ms=window.from_ms,
            to_ms=window.to_ms,
            program_version=self._stable.program_version,
            program_sha256=self._stable.program_sha256,
            policy_version=TRIAGE_POLICY_VERSION,
            bundle_sha=self._stable.bundle_sha,
        )
        external = self._repository.accepted_external_miss_reviews_in_window(
            epoch_started_at_ms=epoch_started_at_ms,
            freeze_as_of_ms=freeze_as_of_ms,
            rubric_versions=REVIEW_RUBRIC_VERSIONS,
            reader_contract_version=READER_CONTRACT_VERSION,
            from_ms=window.from_ms,
            to_ms=window.to_ms,
        )
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
        reviews = self._ledger.reviews_by_id([case.review_id for case in cases])
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
        eligible = self._repository.eligible_stable_arm_event_count(
            from_ms=spec.window.from_ms,
            to_ms=spec.window.to_ms,
            program_version=self._stable.program_version,
            program_sha256=self._stable.program_sha256,
            policy_version=TRIAGE_POLICY_VERSION,
            bundle_sha=self._stable.bundle_sha,
        )
        return {
            "case_n": len(cases),
            "independent_cluster_n": len({case.cluster_id for case in cases}),
            "boundary_cluster_n": len(boundary),
            "retention_cluster_n": len(retention),
            "negative_cluster_n": len(negative),
            "safety_cluster_n": len(safety),
            # Diagnostics, not gates (#259). `natural_day_n` is how many distinct UTC dates the accepted
            # *cases* opened on — not a property of the window, which `window_duration_hours` reports
            # separately, and the two can disagree freely: a 72 h freeze whose reviews all landed in one
            # afternoon reads 1 and 72.0. That is exactly what makes the pair worth publishing and worth
            # refusing to gate on. Neither is read by `development_coverage_blockers`.
            "natural_day_n": len(days),
            "stratum_n": len(strata),
            "strata": sorted(strata),
            "eligible_event_n": int((eligible or {}).get("n") or 0),
            "eligibility": {
                "unit": "agent_bundle_sha",
                "bundle_sha": self._stable.bundle_sha,
                "program_sha256": self._stable.program_sha256,
                "policy_version": TRIAGE_POLICY_VERSION,
                "rubric_versions": list(REVIEW_RUBRIC_VERSIONS),
            },
            "window_duration_hours": round((spec.window.to_ms - spec.window.from_ms) / 3_600_000, 3),
        }

    def _seed_receipts(self, from_ms: int, *, epoch_started_at_ms: int) -> tuple[dict[str, Any], ...]:
        """The 48 h receipt source the first cases replay against.

        The ledger uses the exact current arm and epoch.
        """

        return self._history.seed_receipts(
            from_ms=from_ms,
            epoch_started_at_ms=epoch_started_at_ms,
            program_version=self._stable.program_version,
            program_sha256=self._stable.program_sha256,
            bundle_sha=self._stable.bundle_sha,
        )

    def build_context(self, case: Mapping[str, Any], state: ArmState) -> TriageContext:
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

    def load_case(self, case: DatasetCaseRef) -> dict[str, Any]:
        review = self._repository.review(case.review_id)
        if review is None:
            raise ValueError("news_learning_review_missing")
        if case.subject_kind == "event":
            row = self._repository.review_task_source(
                event_id=str(case.event_id), evidence_version=int(case.evidence_version or 0)
            )
            if row is None or row["evidence_sha256"] != case.evidence_sha256:
                raise ValueError("news_learning_evidence_changed")
            production_judgment: dict[str, Any] | None = None
            if row.get("verdict") is not None and row.get("model_editorial") is not None:
                scored = ScoredJudgment.issue(
                    verdict=TriageVerdict.model_validate(row["verdict"]),
                    editorial=EditorialEnvelope.model_validate(row["model_editorial"]),
                )
                if str(row.get("judgment_sha256") or "") != scored.scored_judgment_sha256:
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
        row = self._repository.external_miss_snapshot(str(case.external_snapshot_id))
        if row is None or row["evidence_sha256"] != case.evidence_sha256:
            raise ValueError("news_learning_external_evidence_changed")
        snapshot = dict(row["snapshot"] or {})
        synthetic = {
            "schema_version": "news_event_evidence_v3",
            "event_id": case.case_id,
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
                "dedupe_family": "general",
                "admission": "external_miss",
                "queue_priority": "normal",
                "asset_class": "none",
                "grounded_assets": [],
                "storyline_key": "macro:general",
                "opened_at_ms": case.opened_at_ms,
                "member_count": 1,
            },
            "members": [],
            "provenance": "observed",
        }
        return {
            "snapshot": synthetic,
            "opened_at_ms": case.opened_at_ms,
            "receipt_at_ms": None,
            "production_judgment": None,
            "review": dict(review),
            "watchlist": [],
        }

    def _load_dataset_payload(self, artifact_sha: str) -> dict[str, Any]:
        row = self._repository.learning_artifact(artifact_sha, kind="dataset")
        if row is None:
            raise ValueError("news_learning_dataset_not_found")
        payload = dict(row["payload"] or {})
        if _sha({"kind": "dataset", "payload": payload}) != artifact_sha:
            raise ValueError("news_learning_dataset_artifact_hash_mismatch")
        return payload

    def _validate_dataset_payload(self, artifact_sha: str, payload: Mapping[str, Any]) -> DatasetManifest:
        """Integrity and contract of one sealed dataset — deliberately not authorization.

        Whether the *reader* may use this corpus against the active arm is each reader's own check with
        its own honest error — the compile export and the evaluator compare `agent_cohort`, and candidate
        admission compares the parent chain. Folding the active arm into this validator made every stale
        dataset die here as `contract_hash_mismatch` before the real refusal could name itself.
        """

        exact_payload = dict(payload)
        # Self-agreement, not authorization (#314). An epoch is derived from the bundle that opened it, so
        # a seal naming an epoch its own `agent_cohort` could not have produced is corrupt whoever reads
        # it — while a seal that is merely *old* stays valid here and reaches its reader's own honest
        # refusal. Comparing against the running ledger instead would kill every stale corpus as an epoch
        # mismatch before `news_learning_dataset_agent_cohort_mismatch` could name the real problem, which
        # is the mistake this docstring already records once.
        #
        # Pre-#314 seals cannot pass this validator at all, and no branch here changes that. Removing the
        # declared epoch from `_PROFILE` moved `TRUSTED_ROOT_SHA`, which every seal carries and this
        # function compares below — so a `program_vN` corpus fails as a contract-hash mismatch before any
        # epoch branch could speak. An earlier draft accepted legacy labels here for the #300 migration
        # reader; review showed that reader could never reach the branch, and #343 then deleted the
        # migration path entirely — dead code claiming to enable something is worse than its absence.
        #
        # The genesis removed every pre-hard-cut dataset, and the predecessor of this check already
        # refused one for naming an epoch that was not current.
        # Carry-forward works from #314 onward, bundle to bundle, which is the case it exists for.
        sealed_epoch = str(exact_payload.get("learning_epoch") or "")
        sealed_bundle = (dict(exact_payload.get("agent_cohort") or {})).get("bundle_sha")
        if not is_bundle_sha(sealed_bundle) or sealed_epoch != epoch_id_for_bundle(str(sealed_bundle)):
            raise ValueError("news_learning_epoch_mismatch")
        hashes = dict(exact_payload.get("hashes") or {})
        expected_epoch_sha = _sha(
            {"epoch": sealed_epoch, "started_at_ms": exact_payload.get("learning_epoch_started_at_ms")}
        )
        if hashes.get("learning_epoch_sha") != expected_epoch_sha:
            raise ValueError("news_learning_epoch_hash_mismatch")
        if exact_payload.get("profile_id") != LEARNING_PROFILE_ID:
            raise ValueError("news_learning_profile_mismatch")
        expected_hashes = {
            "trusted_root_sha": self._trusted_root_sha,
            "learning_epoch_sha": expected_epoch_sha,
            "rubric_sha": _text_sha(REVIEW_RUBRIC_VERSION),
            "reader_contract_sha": READER_CONTRACT_SHA256,
            # The seal names the arm that made it and must agree with itself; which arm is *acceptable*
            # is the reader's question, not the payload's.
            "agent_bundle_sha": str((dict(exact_payload.get("agent_cohort") or {})).get("bundle_sha") or ""),
        }
        if {name: hashes.get(name) for name in expected_hashes} != expected_hashes:
            raise ValueError("news_learning_dataset_contract_hash_mismatch")
        # One named seal exists: the freeze query. Lineage, not freedom.
        if hashes.get("extraction_sha") != _EXTRACTION_SEAL_SHA:
            raise ValueError("news_learning_dataset_contract_hash_mismatch")
        if set(hashes) != {*expected_hashes, "extraction_sha"}:
            raise ValueError("news_learning_dataset_contract_hash_mismatch")
        if exact_payload.get("reader_contract_version") != READER_CONTRACT_VERSION:
            raise ValueError("news_learning_dataset_reader_contract_mismatch")
        return DatasetManifest(artifact_sha=artifact_sha, **exact_payload)

    def load_dataset(self, artifact_sha: str) -> DatasetManifest:
        return self._validate_dataset_payload(artifact_sha, self._load_dataset_payload(artifact_sha))


def _sha(value: Any) -> str:
    return canonical_sha(value)


__all__ = [
    "DATASET_VERSION",
    "SETTLEMENT_GRACE_MS",
    "AdmittedCandidate",
    "DatasetManifest",
    "DatasetSpec",
    "DevelopmentCompileExport",
    "DevelopmentDatasetStore",
]
