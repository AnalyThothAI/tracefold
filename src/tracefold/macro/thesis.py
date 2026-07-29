from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefold.macro.assets import (
    MACRO_ASSET_DATASETS,
    MACRO_THESIS_ASSETS,
    MACRO_THESIS_OUTCOME_DATASETS,
)
from tracefold.macro.domain import MACRO_MODULE_IDS, MacroModuleId

MACRO_EVIDENCE_PACK_SCHEMA_VERSION = "macro_evidence_pack_v3"
MACRO_THESIS_SCHEMA_VERSION = "macro_thesis_v1"
MACRO_LIVE_DELTA_SCHEMA_VERSION = "macro_live_delta_v1"
MACRO_OUTCOME_REPLAY_SCHEMA_VERSION = "macro_outcome_replay_v1"

_ASSET_DATASETS = MACRO_ASSET_DATASETS


@dataclass(frozen=True)
class _EvidenceFactClock:
    observed_at_ms: int | None
    published_at_ms: int | None
    received_at_ms: int | None
    authoritative_at_ms: int | None


class ExactMacroModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MacroMomentum(ExactMacroModel):
    symbol: str
    momentum_1w: Literal["up", "down", "flat", "insufficient"]
    momentum_1m: Literal["up", "down", "flat", "insufficient"]
    return_1w_pct: float | None
    return_1m_pct: float | None
    source_dataset_id: str | None
    as_of: str | None


class MacroEvidencePackV3(ExactMacroModel):
    schema_version: Literal["macro_evidence_pack_v3"] = "macro_evidence_pack_v3"
    session_date: date
    cutoff_ms: int = Field(ge=0)
    sealed_at_ms: int = Field(ge=0)
    source_max_received_at_ms: int = Field(ge=0)
    modules: tuple[dict[str, Any], ...]
    prior_publication: dict[str, Any] | None
    delta_pack: dict[str, Any]
    catalyst_pack: dict[str, Any]
    momentum: tuple[MacroMomentum, ...]
    evidence_refs: tuple[str, ...]

    @model_validator(mode="after")
    def validate_pack(self) -> MacroEvidencePackV3:
        if self.sealed_at_ms < self.cutoff_ms:
            raise ValueError("macro_evidence_pack_sealed_before_cutoff")
        if self.source_max_received_at_ms > self.cutoff_ms:
            raise ValueError("macro_evidence_pack_future_fact")
        module_ids = tuple(str(module.get("module_id") or "") for module in self.modules)
        if module_ids != MACRO_MODULE_IDS:
            raise ValueError("macro_evidence_pack_module_order")
        if tuple(item.symbol for item in self.momentum) != MACRO_THESIS_ASSETS:
            raise ValueError("macro_evidence_pack_asset_order")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("macro_evidence_pack_duplicate_evidence_ref")
        return self

    @property
    def payload_hash(self) -> str:
        return payload_hash(self.model_dump(mode="json", exclude={"sealed_at_ms"}))

    @property
    def evidence_pack_id(self) -> str:
        return "mep3_" + self.payload_hash.removeprefix("sha256:")[:32]


class MacroCondition(ExactMacroModel):
    condition_id: str = Field(min_length=1, max_length=120)
    module_id: MacroModuleId
    dataset_id: str = Field(min_length=1, max_length=200)
    metric_name: str = Field(min_length=1, max_length=120)
    operator: Literal["gt", "gte", "lt", "lte", "abs_gte"]
    threshold: float
    effect: Literal["confirming", "weakening", "invalidation_triggered"]
    rationale: str = Field(min_length=1, max_length=2_000)


class MacroCausalEdge(ExactMacroModel):
    source: str = Field(min_length=1, max_length=300)
    mechanism: str = Field(min_length=1, max_length=2_000)
    target: str = Field(min_length=1, max_length=300)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)


class MacroThesisClaim(ExactMacroModel):
    claim_id: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=4_000)
    causal_edges: tuple[MacroCausalEdge, ...] = Field(min_length=1, max_length=6)
    supporting_evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)
    conditions: tuple[MacroCondition, ...] = Field(default=(), max_length=6)


class MacroMainline(ExactMacroModel):
    stance: Literal["call", "no_call"]
    title: str = Field(min_length=1, max_length=300)
    thesis: str = Field(min_length=1, max_length=8_000)
    stage: Literal["emerging", "developing", "mature", "reversing", "uncertain"]
    confidence: Literal["low", "medium", "high"]
    horizon: Literal["1w", "1m", "1w_to_1m"]
    claims: tuple[MacroThesisClaim, ...] = Field(min_length=1, max_length=6)
    supporting_evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=30)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=30)
    falsifiers: tuple[MacroCondition, ...] = Field(min_length=1, max_length=6)
    checkpoints: tuple[MacroCondition, ...] = Field(min_length=1, max_length=6)


class MacroAlternative(ExactMacroModel):
    title: str = Field(min_length=1, max_length=300)
    thesis: str = Field(min_length=1, max_length=4_000)
    causal_edges: tuple[MacroCausalEdge, ...] = Field(min_length=1, max_length=4)
    supporting_evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)
    trigger_conditions: tuple[MacroCondition, ...] = Field(min_length=1, max_length=6)


class MacroTensionSide(ExactMacroModel):
    label: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=2_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)


class MacroTension(ExactMacroModel):
    tension_id: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=3_000)
    side_a: MacroTensionSide
    side_b: MacroTensionSide
    leading_side: Literal["side_a", "side_b", "balanced", "uncertain"]
    lagging_signal: str = Field(min_length=1, max_length=1_000)
    unresolved_reason: str = Field(min_length=1, max_length=2_000)
    resolution_triggers: tuple[MacroCondition, ...] = Field(min_length=1, max_length=6)


class MacroModuleRole(ExactMacroModel):
    module_id: MacroModuleId
    role: Literal["driver", "confirming", "contradicting", "uncertain"]
    analysis: str = Field(min_length=1, max_length=4_000)
    claim_ids: tuple[str, ...] = Field(default=(), max_length=6)
    supporting_evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)


class MacroChangeFromPrior(ExactMacroModel):
    change_id: str = Field(min_length=1, max_length=120)
    status: Literal["new", "strengthened", "weakened", "reversed", "unchanged"]
    statement: str = Field(min_length=1, max_length=3_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)


class MacroHorizonOutlook(ExactMacroModel):
    horizon: Literal["1w", "1m"]
    direction: Literal["bullish", "bearish", "neutral", "no_call"]
    causal_channel: str = Field(min_length=1, max_length=2_000)
    supporting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)
    confirmation_triggers: tuple[MacroCondition, ...] = Field(default=(), max_length=6)
    falsifiers: tuple[MacroCondition, ...] = Field(default=(), max_length=6)
    checkpoints: tuple[MacroCondition, ...] = Field(default=(), max_length=6)
    confidence: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def validate_directional_call(self) -> MacroHorizonOutlook:
        if self.direction == "no_call":
            if self.confidence != "low":
                raise ValueError("macro_thesis_no_call_confidence_must_be_low")
            return self
        if not self.supporting_evidence_refs:
            raise ValueError("macro_thesis_outlook_supporting_evidence_required")
        if not self.confirmation_triggers:
            raise ValueError("macro_thesis_outlook_confirmation_required")
        if not self.falsifiers:
            raise ValueError("macro_thesis_outlook_falsifier_required")
        if not self.checkpoints:
            raise ValueError("macro_thesis_outlook_checkpoint_required")
        return self


class MacroAssetOutlook(ExactMacroModel):
    symbol: str
    outlook_1w: MacroHorizonOutlook
    outlook_1m: MacroHorizonOutlook

    @model_validator(mode="after")
    def validate_horizons(self) -> MacroAssetOutlook:
        if (self.outlook_1w.horizon, self.outlook_1m.horizon) != ("1w", "1m"):
            raise ValueError("macro_thesis_asset_outlook_horizon_order")
        return self


class MacroNarrativeSection(ExactMacroModel):
    section_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    markdown: str = Field(min_length=1, max_length=8_000)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=30)


class MacroThesisBodyDraft(ExactMacroModel):
    mainline: MacroMainline
    alternative_explanation: MacroAlternative | None = None
    core_tensions: tuple[MacroTension, ...] = Field(default=(), max_length=3)
    module_assessments: tuple[MacroModuleRole, ...]
    changes_from_prior: tuple[MacroChangeFromPrior, ...] = Field(default=(), max_length=12)
    asset_outlooks: tuple[MacroAssetOutlook, ...]
    narrative_sections: tuple[MacroNarrativeSection, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_shape(self) -> MacroThesisBodyDraft:
        if tuple(item.module_id for item in self.module_assessments) != MACRO_MODULE_IDS:
            raise ValueError("macro_thesis_module_role_order")
        if tuple(item.symbol for item in self.asset_outlooks) != MACRO_THESIS_ASSETS:
            raise ValueError("macro_thesis_asset_outlook_order")
        identifiers = [
            *(claim.claim_id for claim in self.mainline.claims),
            *(condition.condition_id for condition in self.mainline.falsifiers),
            *(condition.condition_id for condition in self.mainline.checkpoints),
            *(
                condition.condition_id
                for condition in (
                    self.alternative_explanation.trigger_conditions if self.alternative_explanation else ()
                )
            ),
            *(tension.tension_id for tension in self.core_tensions),
            *(condition.condition_id for tension in self.core_tensions for condition in tension.resolution_triggers),
            *(change.change_id for change in self.changes_from_prior),
            *(section.section_id for section in self.narrative_sections),
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("macro_thesis_duplicate_identifier")
        return self

    @property
    def evidence_refs(self) -> frozenset[str]:
        refs: set[str] = set()
        refs.update(self.mainline.supporting_evidence_refs)
        refs.update(self.mainline.conflicting_evidence_refs)
        for claim in self.mainline.claims:
            refs.update(claim.supporting_evidence_refs)
            refs.update(claim.conflicting_evidence_refs)
            for edge in claim.causal_edges:
                refs.update(edge.evidence_refs)
                refs.update(edge.conflicting_evidence_refs)
        if self.alternative_explanation is not None:
            refs.update(self.alternative_explanation.supporting_evidence_refs)
            refs.update(self.alternative_explanation.conflicting_evidence_refs)
            for edge in self.alternative_explanation.causal_edges:
                refs.update(edge.evidence_refs)
                refs.update(edge.conflicting_evidence_refs)
        for tension in self.core_tensions:
            refs.update(tension.side_a.evidence_refs)
            refs.update(tension.side_b.evidence_refs)
        for module_role in self.module_assessments:
            refs.update(module_role.supporting_evidence_refs)
            refs.update(module_role.conflicting_evidence_refs)
        for change in self.changes_from_prior:
            refs.update(change.evidence_refs)
        for asset_outlook in self.asset_outlooks:
            for outlook in (asset_outlook.outlook_1w, asset_outlook.outlook_1m):
                refs.update(outlook.supporting_evidence_refs)
                refs.update(outlook.conflicting_evidence_refs)
        for section in self.narrative_sections:
            refs.update(section.evidence_refs)
        return frozenset(refs)


class MacroThesisReviewV1(ExactMacroModel):
    draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    disposition: Literal["pass", "revise", "block"]
    findings: tuple[str, ...] = Field(default=(), max_length=20)
    required_changes: tuple[str, ...] = Field(default=(), max_length=20)
    invocation_id: str = Field(min_length=1, max_length=200)
    model_name: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_disposition(self) -> MacroThesisReviewV1:
        if self.disposition in {"revise", "block"} and not self.required_changes:
            raise ValueError("macro_thesis_review_required_changes_missing")
        return self


class MacroAssetView(ExactMacroModel):
    symbol: str
    momentum: MacroMomentum
    outlook_1w: MacroHorizonOutlook
    outlook_1m: MacroHorizonOutlook


class MacroCitation(ExactMacroModel):
    evidence_ref: str = Field(min_length=1, max_length=300)
    module_id: MacroModuleId
    dataset_id: str | None
    source_role: str | None
    label: str
    reference: str | None
    published_at_ms: int | None
    received_at_ms: int | None
    source_url: str | None


class MacroEvidenceGap(ExactMacroModel):
    gap_id: str = Field(min_length=1, max_length=300)
    module_id: MacroModuleId
    dataset_id: str
    axis: Literal["coverage", "current_health", "history_depth"]
    state: str
    reason: str
    affected_claim_ids: tuple[str, ...] = ()


class MacroThesisProvenance(ExactMacroModel):
    draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    research_invocation_id: str = Field(min_length=1, max_length=200)
    research_model: str = Field(min_length=1, max_length=200)
    reviewer_model: str = Field(min_length=1, max_length=200)
    research_prompt_version: str = Field(min_length=1, max_length=200)
    reviewer_prompt_version: str = Field(min_length=1, max_length=200)
    workflow_version: Literal["macro_thesis_workflow_v1"] = "macro_thesis_workflow_v1"


class MacroThesisV1(ExactMacroModel):
    schema_version: Literal["macro_thesis_v1"] = "macro_thesis_v1"
    publication_id: str = Field(min_length=1, max_length=200)
    session_date: date
    cutoff_ms: int = Field(ge=0)
    evidence_pack_id: str = Field(min_length=1, max_length=200)
    evidence_pack_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prior_publication_id: str | None = None
    mainline: MacroMainline
    alternative_explanation: MacroAlternative | None = None
    core_tensions: tuple[MacroTension, ...] = Field(default=(), max_length=3)
    changes_from_prior: tuple[MacroChangeFromPrior, ...] = ()
    module_assessments: tuple[MacroModuleRole, ...]
    assets: tuple[MacroAssetView, ...]
    gaps: tuple[MacroEvidenceGap, ...]
    citations: tuple[MacroCitation, ...]
    narrative_sections: tuple[MacroNarrativeSection, ...]
    review: MacroThesisReviewV1
    provenance: MacroThesisProvenance
    published_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_publication(self) -> MacroThesisV1:
        if self.review.disposition != "pass":
            raise ValueError("macro_thesis_publication_requires_reviewer_pass")
        if tuple(item.module_id for item in self.module_assessments) != MACRO_MODULE_IDS:
            raise ValueError("macro_thesis_publication_module_order")
        if tuple(item.symbol for item in self.assets) != MACRO_THESIS_ASSETS:
            raise ValueError("macro_thesis_publication_asset_order")
        if self.provenance.draft_hash != self.review.draft_hash:
            raise ValueError("macro_thesis_publication_draft_hash_mismatch")
        if self.provenance.research_invocation_id == self.review.invocation_id:
            raise ValueError("macro_thesis_reviewer_not_independent")
        if self.published_at_ms < self.cutoff_ms:
            raise ValueError("macro_thesis_published_before_cutoff")
        return self

    @property
    def content_hash(self) -> str:
        return payload_hash(self.model_dump(mode="json", exclude={"published_at_ms"}))


class MacroLiveDeltaItem(ExactMacroModel):
    binding_type: Literal["claim", "falsifier", "checkpoint"]
    binding_id: str = Field(min_length=1, max_length=120)
    condition_id: str = Field(min_length=1, max_length=120)
    status: Literal[
        "confirming",
        "weakening",
        "invalidation_triggered",
        "unrelated",
        "insufficient",
    ]
    dataset_id: str = Field(min_length=1, max_length=200)
    metric_name: str = Field(min_length=1, max_length=120)
    observed_value: float | None = None
    observed_at_ms: int | None = Field(ge=0)
    operator: Literal["gt", "gte", "lt", "lte", "abs_gte"]
    threshold: float
    reason_code: str


class MacroLiveDeltaV1(ExactMacroModel):
    schema_version: Literal["macro_live_delta_v1"] = "macro_live_delta_v1"
    live_delta_id: str
    publication_id: str
    evaluated_at_ms: int = Field(ge=0)
    module_fact_cutoff_ms: int = Field(ge=0)
    status: Literal[
        "confirming",
        "weakening",
        "invalidation_triggered",
        "unrelated",
        "insufficient",
    ]
    matched_claim_ids: tuple[str, ...] = ()
    matched_falsifier_ids: tuple[str, ...] = ()
    matched_checkpoint_ids: tuple[str, ...] = ()
    items: tuple[MacroLiveDeltaItem, ...] = ()
    reason_codes: tuple[str, ...] = ()
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MacroOutcomeAssetResult(ExactMacroModel):
    symbol: str
    horizon: Literal["1w", "1m"]
    expires_at_ms: int = Field(ge=0)
    status: Literal["pending", "evaluated", "insufficient"]
    published_direction: Literal["bullish", "bearish", "neutral", "no_call"]
    realized_return_pct: float | None
    direction_correct: bool | None
    reason_code: str


class MacroOutcomeHorizon(ExactMacroModel):
    horizon: Literal["1d", "1w", "1m"]
    expires_at_ms: int = Field(ge=0)
    status: Literal["pending", "evaluated", "insufficient"]
    benchmark_symbol: str
    realized_return_pct: float | None
    direction_correct: bool | None
    reason_code: str
    asset_results: tuple[MacroOutcomeAssetResult, ...]


class MacroOutcomeReplayV1(ExactMacroModel):
    schema_version: Literal["macro_outcome_replay_v1"] = "macro_outcome_replay_v1"
    replay_id: str
    publication_id: str
    evaluated_at_ms: int = Field(ge=0)
    horizons: tuple[MacroOutcomeHorizon, ...]
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_horizons(self) -> MacroOutcomeReplayV1:
        if tuple(item.horizon for item in self.horizons) != ("1d", "1w", "1m"):
            raise ValueError("macro_outcome_replay_horizon_order")
        return self


class MacroThesisAgent(Protocol):
    async def draft(
        self,
        *,
        evidence_pack: MacroEvidencePackV3,
        revision_feedback: tuple[str, ...] = (),
        prior_draft: MacroThesisBodyDraft | None = None,
    ) -> tuple[MacroThesisBodyDraft, dict[str, Any]]: ...


class MacroThesisReviewer(Protocol):
    async def review(
        self,
        *,
        evidence_pack: MacroEvidencePackV3,
        draft: MacroThesisBodyDraft,
        draft_hash: str,
    ) -> MacroThesisReviewV1: ...


class MacroThesisReviewFailure(ValueError):
    def __init__(
        self,
        message: str,
        *,
        reviews: Sequence[MacroThesisReviewV1],
    ) -> None:
        super().__init__(message)
        self.reviews = tuple(reviews)


def validate_draft_against_pack(
    draft: MacroThesisBodyDraft,
    *,
    evidence_pack: MacroEvidencePackV3,
) -> None:
    unknown = sorted(draft.evidence_refs - set(evidence_pack.evidence_refs))
    if unknown:
        raise ValueError("macro_thesis_unknown_evidence_ref:" + ",".join(unknown))
    metric_names_by_dataset = {
        str(change.get("dataset_id") or ""): {
            str(metric_name) for metric_name, value in dict(change.get("metrics") or {}).items() if value is not None
        }
        for module in evidence_pack.modules
        for change in _module_decision_changes(module)
        if isinstance(change, Mapping)
    }
    for condition in _draft_conditions(draft):
        if condition.dataset_id not in metric_names_by_dataset:
            raise ValueError("macro_thesis_condition_unknown_dataset:" + condition.dataset_id)
        if condition.metric_name not in metric_names_by_dataset[condition.dataset_id]:
            raise ValueError(
                "macro_thesis_condition_unknown_metric:" + condition.dataset_id + ":" + condition.metric_name
            )
    claim_ids = {claim.claim_id for claim in draft.mainline.claims}
    for module_role in draft.module_assessments:
        unknown_claim_ids = sorted(set(module_role.claim_ids) - claim_ids)
        if unknown_claim_ids:
            raise ValueError("macro_thesis_module_role_unknown_claim:" + ",".join(unknown_claim_ids))
    if evidence_pack.delta_pack.get("state") == "bootstrap":
        if draft.changes_from_prior:
            raise ValueError("macro_thesis_bootstrap_prior_changes_forbidden")
    elif not draft.changes_from_prior:
        raise ValueError("macro_thesis_prior_changes_required")


def build_publication(
    *,
    evidence_pack: MacroEvidencePackV3,
    draft: MacroThesisBodyDraft,
    review: MacroThesisReviewV1,
    research_provenance: Mapping[str, Any],
    published_at_ms: int,
) -> MacroThesisV1:
    validate_draft_against_pack(draft, evidence_pack=evidence_pack)
    draft_hash = payload_hash(draft.model_dump(mode="json"))
    if review.draft_hash != draft_hash:
        raise ValueError("macro_thesis_review_draft_hash_mismatch")
    if review.disposition != "pass":
        raise ValueError("macro_thesis_publication_requires_reviewer_pass")
    assets = tuple(
        MacroAssetView(
            symbol=symbol,
            momentum=momentum,
            outlook_1w=outlook.outlook_1w,
            outlook_1m=outlook.outlook_1m,
        )
        for symbol, momentum, outlook in zip(
            MACRO_THESIS_ASSETS,
            evidence_pack.momentum,
            draft.asset_outlooks,
            strict=True,
        )
    )
    prior_publication_id = (
        str(evidence_pack.prior_publication.get("publication_id"))
        if evidence_pack.prior_publication is not None
        else None
    )
    publication_seed = {
        "session_date": evidence_pack.session_date.isoformat(),
        "cutoff_ms": evidence_pack.cutoff_ms,
        "evidence_pack_hash": evidence_pack.payload_hash,
        "draft_hash": draft_hash,
        "research_model": str(research_provenance.get("model_name") or ""),
        "research_prompt_version": str(research_provenance.get("prompt_version") or ""),
        "reviewer_model": review.model_name,
        "reviewer_prompt_version": review.prompt_version,
    }
    publication_id = "mth_" + payload_hash(publication_seed).removeprefix("sha256:")[:32]
    citations = _citations_for_draft(
        draft=draft,
        evidence_pack=evidence_pack,
    )
    gaps = _gaps_for_draft(
        draft=draft,
        evidence_pack=evidence_pack,
    )
    return MacroThesisV1(
        publication_id=publication_id,
        session_date=evidence_pack.session_date,
        cutoff_ms=evidence_pack.cutoff_ms,
        evidence_pack_id=evidence_pack.evidence_pack_id,
        evidence_pack_hash=evidence_pack.payload_hash,
        prior_publication_id=prior_publication_id,
        mainline=draft.mainline,
        alternative_explanation=draft.alternative_explanation,
        core_tensions=draft.core_tensions,
        changes_from_prior=draft.changes_from_prior,
        module_assessments=draft.module_assessments,
        assets=assets,
        gaps=gaps,
        citations=citations,
        narrative_sections=draft.narrative_sections,
        review=review,
        provenance=MacroThesisProvenance(
            draft_hash=draft_hash,
            research_invocation_id=str(research_provenance.get("invocation_id") or ""),
            research_model=str(research_provenance.get("model_name") or ""),
            reviewer_model=review.model_name,
            research_prompt_version=str(research_provenance.get("prompt_version") or ""),
            reviewer_prompt_version=review.prompt_version,
        ),
        published_at_ms=published_at_ms,
    )


def compile_evidence_pack_v3(
    *,
    session_date: date,
    cutoff_ms: int,
    sealed_at_ms: int,
    modules: Sequence[Mapping[str, Any]],
    prior_publication: Mapping[str, Any] | None,
    prior_evidence_pack: Mapping[str, Any] | None = None,
) -> MacroEvidencePackV3:
    normalized_modules = tuple(dict(module) for module in modules)
    current_changes = {
        str(module.get("module_id")): list(dict(module.get("summary") or {}).get("top_changes") or ())
        for module in normalized_modules
    }
    prior_summary = dict(prior_publication) if prior_publication is not None else None
    evidence_refs: list[str] = []
    for module in normalized_modules:
        module_id = str(module.get("module_id") or "")
        evidence_refs.append(f"macro-module:{session_date.isoformat()}:{module_id}")
        evidence = module.get("evidence")
        latest_facts = evidence.get("latest_facts", ()) if isinstance(evidence, Mapping) else ()
        evidence_refs.extend(
            str(fact["fact_ref"]) for fact in latest_facts if isinstance(fact, Mapping) and fact.get("fact_ref")
        )
    source_max_received_at_ms = max(
        (
            int(fact.get("received_at_ms") or 0)
            for module in normalized_modules
            for fact in (
                dict(module.get("evidence") or {}).get("latest_facts", ())
                if isinstance(module.get("evidence"), Mapping)
                else ()
            )
            if isinstance(fact, Mapping)
        ),
        default=0,
    )
    catalysts = [
        {
            "module_id": module.get("module_id"),
            "checkpoints": list(module.get("next_checkpoints") or ())[:6],
        }
        for module in normalized_modules
    ]
    return MacroEvidencePackV3(
        session_date=session_date,
        cutoff_ms=cutoff_ms,
        sealed_at_ms=sealed_at_ms,
        source_max_received_at_ms=source_max_received_at_ms,
        modules=normalized_modules,
        prior_publication=prior_summary,
        delta_pack=_compile_delta_pack(
            current_changes=current_changes,
            prior_publication=prior_summary,
            prior_evidence_pack=prior_evidence_pack,
        ),
        catalyst_pack={
            "session_date": session_date.isoformat(),
            "module_checkpoints": catalysts,
            "scheduled_releases": _scheduled_release_catalysts(
                normalized_modules,
                cutoff_ms=cutoff_ms,
            ),
        },
        momentum=_compile_momentum(normalized_modules),
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
    )


async def run_thesis_review_cycle(
    *,
    evidence_pack: MacroEvidencePackV3,
    agent: MacroThesisAgent,
    reviewer: MacroThesisReviewer,
    published_at_ms: int,
) -> tuple[MacroThesisV1, tuple[MacroThesisReviewV1, ...]]:
    draft, first_audit = await agent.draft(evidence_pack=evidence_pack)
    validate_draft_against_pack(draft, evidence_pack=evidence_pack)
    first_hash = payload_hash(draft.model_dump(mode="json"))
    first_review = await reviewer.review(
        evidence_pack=evidence_pack,
        draft=draft,
        draft_hash=first_hash,
    )
    if first_review.draft_hash != first_hash:
        raise ValueError("macro_thesis_review_draft_hash_mismatch")
    reviews = [first_review]
    audits = [dict(first_audit)]
    if first_review.disposition == "block":
        raise MacroThesisReviewFailure(
            "macro_thesis_reviewer_block",
            reviews=reviews,
        )
    if first_review.disposition == "revise":
        revised, revision_audit = await agent.draft(
            evidence_pack=evidence_pack,
            revision_feedback=first_review.required_changes,
            prior_draft=draft,
        )
        validate_draft_against_pack(revised, evidence_pack=evidence_pack)
        revised_hash = payload_hash(revised.model_dump(mode="json"))
        second_review = await reviewer.review(
            evidence_pack=evidence_pack,
            draft=revised,
            draft_hash=revised_hash,
        )
        if second_review.draft_hash != revised_hash:
            raise ValueError("macro_thesis_review_draft_hash_mismatch")
        reviews.append(second_review)
        audits.append(dict(revision_audit))
        if second_review.disposition != "pass":
            raise MacroThesisReviewFailure(
                "macro_thesis_reviewer_not_passed_after_revision",
                reviews=reviews,
            )
        draft = revised
        final_review = second_review
    else:
        final_review = first_review
    publication = build_publication(
        evidence_pack=evidence_pack,
        draft=draft,
        review=final_review,
        research_provenance=audits[-1],
        published_at_ms=published_at_ms,
    )
    return publication, tuple(reviews)


def evaluate_live_delta(
    *,
    publication: MacroThesisV1,
    modules: Sequence[Mapping[str, Any]],
    evaluated_at_ms: int,
) -> MacroLiveDeltaV1:
    metric_values = _module_metric_values(modules)
    fact_clocks_by_dataset = _latest_evidence_fact_clocks(modules)
    matched_claims: list[str] = []
    matched_falsifiers: list[str] = []
    matched_checkpoints: list[str] = []
    effects: list[str] = []
    items: list[MacroLiveDeltaItem] = []

    def evaluate(
        condition: MacroCondition,
        *,
        binding_type: Literal["claim", "falsifier", "checkpoint"],
        owner_id: str,
        bucket: list[str],
    ) -> None:
        fact_clock = fact_clocks_by_dataset.get(condition.dataset_id)
        authoritative_at_ms = fact_clock.authoritative_at_ms if fact_clock is not None else None
        if authoritative_at_ms is None or authoritative_at_ms <= publication.cutoff_ms:
            items.append(
                MacroLiveDeltaItem(
                    binding_type=binding_type,
                    binding_id=owner_id,
                    condition_id=condition.condition_id,
                    status="insufficient",
                    dataset_id=condition.dataset_id,
                    metric_name=condition.metric_name,
                    observed_value=None,
                    observed_at_ms=authoritative_at_ms,
                    operator=condition.operator,
                    threshold=condition.threshold,
                    reason_code="post_cutoff_fact_missing",
                )
            )
            return
        key = (condition.dataset_id, condition.metric_name)
        if key not in metric_values:
            items.append(
                MacroLiveDeltaItem(
                    binding_type=binding_type,
                    binding_id=owner_id,
                    condition_id=condition.condition_id,
                    status="insufficient",
                    dataset_id=condition.dataset_id,
                    metric_name=condition.metric_name,
                    observed_value=None,
                    observed_at_ms=authoritative_at_ms,
                    operator=condition.operator,
                    threshold=condition.threshold,
                    reason_code="condition_metric_missing",
                )
            )
            return
        observed = float(metric_values[key])
        item_status: Literal[
            "confirming",
            "weakening",
            "invalidation_triggered",
            "unrelated",
            "insufficient",
        ]
        if _condition_matches(observed, condition):
            bucket.append(owner_id)
            effects.append(condition.effect)
            item_status = condition.effect
            reason_code = "condition_threshold_matched"
        else:
            item_status = "unrelated"
            reason_code = "condition_threshold_not_matched"
        items.append(
            MacroLiveDeltaItem(
                binding_type=binding_type,
                binding_id=owner_id,
                condition_id=condition.condition_id,
                status=item_status,
                dataset_id=condition.dataset_id,
                metric_name=condition.metric_name,
                observed_value=observed,
                observed_at_ms=authoritative_at_ms,
                operator=condition.operator,
                threshold=condition.threshold,
                reason_code=reason_code,
            )
        )

    for claim in publication.mainline.claims:
        for condition in claim.conditions:
            evaluate(
                condition,
                binding_type="claim",
                owner_id=claim.claim_id,
                bucket=matched_claims,
            )
    for condition in publication.mainline.falsifiers:
        evaluate(
            condition,
            binding_type="falsifier",
            owner_id=condition.condition_id,
            bucket=matched_falsifiers,
        )
    for condition in publication.mainline.checkpoints:
        evaluate(
            condition,
            binding_type="checkpoint",
            owner_id=condition.condition_id,
            bucket=matched_checkpoints,
        )
    if publication.alternative_explanation is not None:
        for condition in publication.alternative_explanation.trigger_conditions:
            evaluate(
                condition,
                binding_type="claim",
                owner_id="alternative_explanation",
                bucket=matched_claims,
            )
    for tension in publication.core_tensions:
        for condition in tension.resolution_triggers:
            evaluate(
                condition,
                binding_type="checkpoint",
                owner_id=f"tension:{tension.tension_id}",
                bucket=matched_checkpoints,
            )
    for asset in publication.assets:
        for outlook in (asset.outlook_1w, asset.outlook_1m):
            owner_id = f"asset:{asset.symbol}:{outlook.horizon}"
            for condition in outlook.confirmation_triggers:
                evaluate(
                    condition,
                    binding_type="claim",
                    owner_id=owner_id,
                    bucket=matched_claims,
                )
            for condition in outlook.falsifiers:
                evaluate(
                    condition,
                    binding_type="falsifier",
                    owner_id=owner_id,
                    bucket=matched_falsifiers,
                )
            for condition in outlook.checkpoints:
                evaluate(
                    condition,
                    binding_type="checkpoint",
                    owner_id=owner_id,
                    bucket=matched_checkpoints,
                )

    if "invalidation_triggered" in effects:
        status = "invalidation_triggered"
    elif "weakening" in effects:
        status = "weakening"
    elif "confirming" in effects:
        status = "confirming"
    elif items and all(item.status == "insufficient" for item in items):
        status = "insufficient"
    else:
        status = "unrelated"
    module_fact_cutoff_ms = max(
        (
            clock.authoritative_at_ms
            for clock in fact_clocks_by_dataset.values()
            if clock.authoritative_at_ms is not None
        ),
        default=0,
    )
    inputs = {
        "publication_id": publication.publication_id,
        "module_fact_cutoff_ms": module_fact_cutoff_ms,
        "dataset_fact_clocks": sorted(
            (
                dataset_id,
                clock.observed_at_ms,
                clock.published_at_ms,
                clock.received_at_ms,
                clock.authoritative_at_ms,
            )
            for dataset_id, clock in fact_clocks_by_dataset.items()
        ),
        "metric_values": sorted((f"{dataset}:{metric}", value) for (dataset, metric), value in metric_values.items()),
    }
    input_hash = payload_hash(inputs)
    return MacroLiveDeltaV1(
        live_delta_id="mld:" + publication.publication_id,
        publication_id=publication.publication_id,
        evaluated_at_ms=evaluated_at_ms,
        module_fact_cutoff_ms=module_fact_cutoff_ms,
        status=status,
        matched_claim_ids=tuple(dict.fromkeys(matched_claims)),
        matched_falsifier_ids=tuple(dict.fromkeys(matched_falsifiers)),
        matched_checkpoint_ids=tuple(dict.fromkeys(matched_checkpoints)),
        items=tuple(items),
        reason_codes=(
            ("condition_threshold_matched",)
            if effects
            else ("condition_metrics_missing",)
            if status == "insufficient"
            else ("no_bound_condition_matched",)
        ),
        input_hash=input_hash,
    )


def pending_outcome_replay(
    *,
    publication: MacroThesisV1,
    evaluated_at_ms: int,
) -> MacroOutcomeReplayV1:
    day_ms = 86_400_000
    offsets = (("1d", day_ms), ("1w", 7 * day_ms), ("1m", 30 * day_ms))
    input_hash = payload_hash(
        {
            "publication_id": publication.publication_id,
            "published_at_ms": publication.published_at_ms,
            "state": "pending",
        }
    )
    return MacroOutcomeReplayV1(
        replay_id="mor_" + input_hash.removeprefix("sha256:")[:32],
        publication_id=publication.publication_id,
        evaluated_at_ms=evaluated_at_ms,
        horizons=tuple(
            MacroOutcomeHorizon(
                horizon=horizon,
                expires_at_ms=publication.published_at_ms + offset,
                status="pending",
                benchmark_symbol="SPY",
                realized_return_pct=None,
                direction_correct=None,
                reason_code="horizon_not_expired",
                asset_results=(
                    tuple(
                        MacroOutcomeAssetResult(
                            symbol=asset.symbol,
                            horizon=horizon,
                            expires_at_ms=publication.published_at_ms + offset,
                            status="pending",
                            published_direction=(
                                asset.outlook_1w.direction if horizon == "1w" else asset.outlook_1m.direction
                            ),
                            realized_return_pct=None,
                            direction_correct=None,
                            reason_code="horizon_not_expired",
                        )
                        for asset in publication.assets
                    )
                    if horizon in {"1w", "1m"}
                    else ()
                ),
            )
            for horizon, offset in offsets
        ),
        input_hash=input_hash,
    )


def evaluate_outcome_replay(
    *,
    publication: MacroThesisV1,
    market_rows: Sequence[Mapping[str, Any]],
    evaluated_at_ms: int,
) -> MacroOutcomeReplayV1:
    day_ms = 86_400_000
    offsets = (("1d", day_ms), ("1w", 7 * day_ms), ("1m", 30 * day_ms))
    spy_rows = sorted(
        (
            row
            for row in market_rows
            if str(row.get("dataset_id") or "") == "nasdaq.spy.daily"
            and isinstance(row.get("observed_at_ms"), int)
            and isinstance(row.get("value_numeric"), int | float)
        ),
        key=lambda row: (int(row["observed_at_ms"]), int(row.get("received_at_ms") or 0)),
    )
    start_candidates = [row for row in spy_rows if int(row["observed_at_ms"]) <= publication.published_at_ms]
    start = start_candidates[-1] if start_candidates else None
    spy = next(item for item in publication.assets if item.symbol == "SPY")
    horizons = []
    for horizon, offset in offsets:
        expires_at_ms = publication.published_at_ms + offset
        asset_results = (
            _asset_outcome_results(
                publication=publication,
                market_rows=market_rows,
                horizon=cast(Literal["1w", "1m"], horizon),
                expires_at_ms=expires_at_ms,
                evaluated_at_ms=evaluated_at_ms,
            )
            if horizon in {"1w", "1m"}
            else ()
        )
        if evaluated_at_ms < expires_at_ms:
            horizons.append(
                MacroOutcomeHorizon(
                    horizon=horizon,
                    expires_at_ms=expires_at_ms,
                    status="pending",
                    benchmark_symbol="SPY",
                    realized_return_pct=None,
                    direction_correct=None,
                    reason_code="horizon_not_expired",
                    asset_results=asset_results,
                )
            )
            continue
        end_candidates = [row for row in spy_rows if expires_at_ms <= int(row["observed_at_ms"]) <= evaluated_at_ms]
        end = end_candidates[0] if end_candidates else None
        if start is None or end is None or float(start["value_numeric"]) == 0:
            horizons.append(
                MacroOutcomeHorizon(
                    horizon=horizon,
                    expires_at_ms=expires_at_ms,
                    status="insufficient",
                    benchmark_symbol="SPY",
                    realized_return_pct=None,
                    direction_correct=None,
                    reason_code="benchmark_observation_missing",
                    asset_results=asset_results,
                )
            )
            continue
        realized = round(
            (float(end["value_numeric"]) / float(start["value_numeric"]) - 1) * 100,
            6,
        )
        spy_outlook = spy.outlook_1w.direction if horizon in {"1d", "1w"} else spy.outlook_1m.direction
        direction_correct = (
            realized > 0 if spy_outlook == "bullish" else realized < 0 if spy_outlook == "bearish" else None
        )
        horizons.append(
            MacroOutcomeHorizon(
                horizon=horizon,
                expires_at_ms=expires_at_ms,
                status="evaluated",
                benchmark_symbol="SPY",
                realized_return_pct=realized,
                direction_correct=direction_correct,
                reason_code=(
                    "directional_outlook_evaluated" if direction_correct is not None else "no_directional_outlook"
                ),
                asset_results=asset_results,
            )
        )
    inputs = {
        "publication_id": publication.publication_id,
        "horizons": [horizon.model_dump(mode="json") for horizon in horizons],
    }
    input_hash = payload_hash(inputs)
    return MacroOutcomeReplayV1(
        replay_id="mor_" + input_hash.removeprefix("sha256:")[:32],
        publication_id=publication.publication_id,
        evaluated_at_ms=evaluated_at_ms,
        horizons=tuple(horizons),
        input_hash=input_hash,
    )


def _asset_outcome_results(
    *,
    publication: MacroThesisV1,
    market_rows: Sequence[Mapping[str, Any]],
    horizon: Literal["1w", "1m"],
    expires_at_ms: int,
    evaluated_at_ms: int,
) -> tuple[MacroOutcomeAssetResult, ...]:
    results: list[MacroOutcomeAssetResult] = []
    for asset in publication.assets:
        outlook = asset.outlook_1w if horizon == "1w" else asset.outlook_1m
        if evaluated_at_ms < expires_at_ms:
            results.append(
                MacroOutcomeAssetResult(
                    symbol=asset.symbol,
                    horizon=horizon,
                    expires_at_ms=expires_at_ms,
                    status="pending",
                    published_direction=outlook.direction,
                    realized_return_pct=None,
                    direction_correct=None,
                    reason_code="horizon_not_expired",
                )
            )
            continue
        dataset_id = _ASSET_DATASETS[asset.symbol]
        rows = sorted(
            (
                row
                for row in market_rows
                if str(row.get("dataset_id") or "") == dataset_id
                and isinstance(row.get("observed_at_ms"), int)
                and isinstance(row.get("value_numeric"), int | float)
            ),
            key=lambda row: (
                int(row["observed_at_ms"]),
                int(row.get("received_at_ms") or 0),
            ),
        )
        starts = [row for row in rows if int(row["observed_at_ms"]) <= publication.published_at_ms]
        ends = [row for row in rows if expires_at_ms <= int(row["observed_at_ms"]) <= evaluated_at_ms]
        start = starts[-1] if starts else None
        end = ends[0] if ends else None
        if start is None or end is None or float(start["value_numeric"]) == 0:
            results.append(
                MacroOutcomeAssetResult(
                    symbol=asset.symbol,
                    horizon=horizon,
                    expires_at_ms=expires_at_ms,
                    status="insufficient",
                    published_direction=outlook.direction,
                    realized_return_pct=None,
                    direction_correct=None,
                    reason_code="asset_observation_missing",
                )
            )
            continue
        realized = round(
            (float(end["value_numeric"]) / float(start["value_numeric"]) - 1) * 100,
            6,
        )
        direction_correct = (
            realized > 0 if outlook.direction == "bullish" else realized < 0 if outlook.direction == "bearish" else None
        )
        results.append(
            MacroOutcomeAssetResult(
                symbol=asset.symbol,
                horizon=horizon,
                expires_at_ms=expires_at_ms,
                status="evaluated",
                published_direction=outlook.direction,
                realized_return_pct=realized,
                direction_correct=direction_correct,
                reason_code=(
                    "directional_outlook_evaluated" if direction_correct is not None else "no_directional_outlook"
                ),
            )
        )
    return tuple(results)


def payload_hash(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _draft_conditions(draft: MacroThesisBodyDraft) -> tuple[MacroCondition, ...]:
    conditions: list[MacroCondition] = []
    for claim in draft.mainline.claims:
        conditions.extend(claim.conditions)
    conditions.extend(draft.mainline.falsifiers)
    conditions.extend(draft.mainline.checkpoints)
    if draft.alternative_explanation is not None:
        conditions.extend(draft.alternative_explanation.trigger_conditions)
    for tension in draft.core_tensions:
        conditions.extend(tension.resolution_triggers)
    for asset in draft.asset_outlooks:
        for outlook in (asset.outlook_1w, asset.outlook_1m):
            conditions.extend(outlook.confirmation_triggers)
            conditions.extend(outlook.falsifiers)
            conditions.extend(outlook.checkpoints)
    identifiers = [condition.condition_id for condition in conditions]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("macro_thesis_duplicate_condition_id")
    return tuple(conditions)


def _citations_for_draft(
    *,
    draft: MacroThesisBodyDraft,
    evidence_pack: MacroEvidencePackV3,
) -> tuple[MacroCitation, ...]:
    referenced = draft.evidence_refs
    records: dict[str, MacroCitation] = {}
    for module in evidence_pack.modules:
        module_id = str(module.get("module_id") or "")
        module_ref = f"macro-module:{evidence_pack.session_date.isoformat()}:{module_id}"
        if module_ref in referenced:
            records[module_ref] = MacroCitation(
                evidence_ref=module_ref,
                module_id=module_id,
                dataset_id=None,
                source_role=None,
                label=str(module.get("label") or module_id),
                reference=evidence_pack.session_date.isoformat(),
                published_at_ms=None,
                received_at_ms=int(module.get("latest_fact_at_ms") or 0) or None,
                source_url=None,
            )
        evidence = module.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        states = {
            str(state.get("dataset_id") or ""): state
            for state in evidence.get("dataset_states", ())
            if isinstance(state, Mapping)
        }
        for fact in evidence.get("latest_facts", ()):
            if not isinstance(fact, Mapping):
                continue
            fact_ref = str(fact.get("fact_ref") or "")
            if fact_ref not in referenced:
                continue
            dataset_id = str(fact.get("dataset_id") or "")
            state = states.get(dataset_id, {})
            records[fact_ref] = MacroCitation(
                evidence_ref=fact_ref,
                module_id=module_id,
                dataset_id=dataset_id or None,
                source_role=_optional_text(fact.get("source_role") or state.get("source_role")),
                label=str(fact.get("label") or state.get("label") or dataset_id or fact_ref),
                reference=_optional_text(
                    fact.get("reference_period") or fact.get("reference") or fact.get("observed_at")
                ),
                published_at_ms=_optional_int(fact.get("published_at_ms")),
                received_at_ms=_optional_int(fact.get("received_at_ms")),
                source_url=_optional_text(fact.get("source_url") or fact.get("url")),
            )
    missing = sorted(referenced - set(records))
    if missing:
        raise ValueError("macro_thesis_citation_record_missing:" + ",".join(missing))
    return tuple(records[ref] for ref in evidence_pack.evidence_refs if ref in records)


def _gaps_for_draft(
    *,
    draft: MacroThesisBodyDraft,
    evidence_pack: MacroEvidencePackV3,
) -> tuple[MacroEvidenceGap, ...]:
    affected_by_dataset: dict[str, set[str]] = {}
    for condition in _draft_conditions(draft):
        affected_by_dataset.setdefault(condition.dataset_id, set()).add(condition.condition_id)
    for claim in draft.mainline.claims:
        for condition in claim.conditions:
            affected_by_dataset.setdefault(condition.dataset_id, set()).add(claim.claim_id)
    gaps: list[MacroEvidenceGap] = []
    for module in evidence_pack.modules:
        module_id = str(module.get("module_id") or "")
        status = module.get("status")
        status = status if isinstance(status, Mapping) else {}
        evidence = module.get("evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        dataset_states = [state for state in evidence.get("dataset_states", ()) if isinstance(state, Mapping)]
        coverage_payload = status.get("coverage")
        coverage_payload = coverage_payload if isinstance(coverage_payload, Mapping) else {}
        coverage_state = str(coverage_payload.get("state") or "unknown")
        if coverage_state != "complete":
            gaps.append(
                MacroEvidenceGap(
                    gap_id=f"{module_id}:module:{module_id}:coverage",
                    module_id=module_id,
                    dataset_id=f"module:{module_id}",
                    axis="coverage",
                    state=coverage_state,
                    reason="required_free_capability_missing",
                )
            )
        for state_payload in dataset_states:
            dataset_id = str(state_payload.get("dataset_id") or "")
            if not dataset_id:
                continue
            affected = tuple(sorted(affected_by_dataset.get(dataset_id, ())))
            current_state = str(
                state_payload.get("current_health") or state_payload.get("current_health_state") or "unknown"
            )
            source_role = str(state_payload.get("source_role") or "")
            if current_state != "current" and (source_role != "history" or affected):
                gaps.append(
                    MacroEvidenceGap(
                        gap_id=f"{module_id}:{dataset_id}:current_health",
                        module_id=module_id,
                        dataset_id=dataset_id,
                        axis="current_health",
                        state=current_state,
                        reason=_gap_reason_text(
                            state_payload.get("current_reason"),
                            fallback=f"current_health:{current_state}",
                        ),
                        affected_claim_ids=affected,
                    )
                )
            history_state = str(
                state_payload.get("history_depth") or state_payload.get("history_depth_state") or "unknown"
            )
            if history_state not in {"complete", "not_required"}:
                gaps.append(
                    MacroEvidenceGap(
                        gap_id=f"{module_id}:{dataset_id}:history_depth",
                        module_id=module_id,
                        dataset_id=dataset_id,
                        axis="history_depth",
                        state=history_state,
                        reason=_gap_reason_text(
                            state_payload.get("history_reason"),
                            fallback=f"history_depth:{history_state}",
                        ),
                        affected_claim_ids=affected,
                    )
                )
    return tuple(
        sorted(
            gaps,
            key=lambda gap: (
                MACRO_MODULE_IDS.index(gap.module_id),
                gap.dataset_id,
                gap.axis,
            ),
        )
    )


def _gap_reason_text(value: object, *, fallback: str) -> str:
    if isinstance(value, Mapping):
        message = str(value.get("message") or "").strip()
        code = str(value.get("code") or "").strip()
        if message and code:
            return f"{message} [{code}]"
        if message or code:
            return message or code
        return fallback
    text = str(value or "").strip()
    return text or fallback


def _compile_delta_pack(
    *,
    current_changes: Mapping[str, Sequence[Mapping[str, Any]]],
    prior_publication: Mapping[str, Any] | None,
    prior_evidence_pack: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if prior_publication is None:
        return {
            "state": "bootstrap",
            "prior_publication_id": None,
            "comparison_basis": "none",
            "changes": [],
        }
    prior_changes: dict[str, Sequence[Mapping[str, Any]]] = {}
    if isinstance(prior_evidence_pack, Mapping):
        for module in prior_evidence_pack.get("modules", ()):
            if not isinstance(module, Mapping):
                continue
            module_id = str(module.get("module_id") or "")
            summary = module.get("summary")
            prior_changes[module_id] = tuple(
                change
                for change in (summary.get("top_changes", ()) if isinstance(summary, Mapping) else ())
                if isinstance(change, Mapping)
            )

    def keyed(
        source: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (module_id, str(change.get("dataset_id") or "")): dict(change)
            for module_id, changes in source.items()
            for change in changes
            if change.get("dataset_id")
        }

    current = keyed(current_changes)
    previous = keyed(prior_changes)
    changes = []
    for module_id, dataset_id in sorted(
        set(current) | set(previous),
        key=lambda key: (MACRO_MODULE_IDS.index(key[0]), key[1]),
    ):
        current_value = current.get((module_id, dataset_id))
        prior_value = previous.get((module_id, dataset_id))
        if current_value is None:
            status = "removed"
        elif prior_value is None:
            status = "added"
        elif payload_hash(current_value) == payload_hash(prior_value):
            status = "unchanged"
        else:
            status = "changed"
        changes.append(
            {
                "delta_id": f"{module_id}:{dataset_id}",
                "module_id": module_id,
                "dataset_id": dataset_id,
                "status": status,
                "current": current_value,
                "prior": prior_value,
            }
        )
    return {
        "state": "compared",
        "prior_publication_id": str(prior_publication.get("publication_id") or ""),
        "comparison_basis": (
            "prior_evidence_pack" if isinstance(prior_evidence_pack, Mapping) else "prior_publication_only"
        ),
        "changes": changes,
    }


def _scheduled_release_catalysts(
    modules: Sequence[Mapping[str, Any]],
    *,
    cutoff_ms: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def walk(value: object, *, module_id: str) -> None:
        if isinstance(value, Mapping):
            scheduled = _optional_int(
                value.get("scheduled_at_ms") or value.get("release_at_ms") or value.get("expected_at_ms")
            )
            if scheduled is not None and scheduled > cutoff_ms:
                candidates.append(
                    {
                        "module_id": module_id,
                        "dataset_id": _optional_text(value.get("dataset_id")),
                        "scheduled_at_ms": scheduled,
                        "label": str(
                            value.get("label") or value.get("title") or value.get("name") or "scheduled release"
                        ),
                        "estimate_value": value.get("estimate_value"),
                        "reference_period": _optional_text(value.get("reference_period")),
                        "evidence_ref": _optional_text(value.get("fact_ref")),
                    }
                )
            for nested in value.values():
                walk(nested, module_id=module_id)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for nested in value:
                walk(nested, module_id=module_id)

    for module in modules:
        walk(module, module_id=str(module.get("module_id") or ""))
    unique = {
        (
            item["module_id"],
            item["dataset_id"],
            item["scheduled_at_ms"],
            item["label"],
        ): item
        for item in candidates
    }
    return [
        unique[key]
        for key in sorted(
            unique,
            key=lambda key: (key[2], key[0], str(key[1]), key[3]),
        )
    ]


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _module_metric_values(modules: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    for module in modules:
        for change in _module_decision_changes(module):
            if not isinstance(change, Mapping):
                continue
            dataset_id = str(change.get("dataset_id") or "")
            metrics = change.get("metrics")
            if not dataset_id or not isinstance(metrics, Mapping):
                continue
            for metric_name, value in metrics.items():
                if isinstance(value, int | float):
                    values[(dataset_id, str(metric_name))] = float(value)
    return values


def _latest_evidence_fact_clocks(
    modules: Sequence[Mapping[str, Any]],
) -> dict[str, _EvidenceFactClock]:
    clocks: dict[str, _EvidenceFactClock] = {}
    for module in modules:
        evidence = module.get("evidence")
        facts = evidence.get("latest_facts", ()) if isinstance(evidence, Mapping) else ()
        for fact in facts:
            if not isinstance(fact, Mapping):
                continue
            dataset_id = str(fact.get("dataset_id") or "")
            if not dataset_id:
                continue
            observed_at_ms = _optional_int(fact.get("observed_at_ms"))
            published_at_ms = _optional_int(fact.get("published_at_ms"))
            received_at_ms = _optional_int(fact.get("received_at_ms"))
            candidate = _EvidenceFactClock(
                observed_at_ms=observed_at_ms,
                published_at_ms=published_at_ms,
                received_at_ms=received_at_ms,
                authoritative_at_ms=next(
                    (value for value in (observed_at_ms, published_at_ms, received_at_ms) if value is not None),
                    None,
                ),
            )
            current = clocks.get(dataset_id)
            if current is None or _evidence_fact_clock_rank(candidate) > _evidence_fact_clock_rank(current):
                clocks[dataset_id] = candidate
    return clocks


def _evidence_fact_clock_rank(clock: _EvidenceFactClock) -> tuple[int, int, int, int]:
    return (
        clock.authoritative_at_ms if clock.authoritative_at_ms is not None else -1,
        clock.observed_at_ms if clock.observed_at_ms is not None else -1,
        clock.published_at_ms if clock.published_at_ms is not None else -1,
        clock.received_at_ms if clock.received_at_ms is not None else -1,
    )


def _condition_matches(value: float, condition: MacroCondition) -> bool:
    return {
        "gt": value > condition.threshold,
        "gte": value >= condition.threshold,
        "lt": value < condition.threshold,
        "lte": value <= condition.threshold,
        "abs_gte": abs(value) >= condition.threshold,
    }[condition.operator]


def _compile_momentum(modules: Sequence[Mapping[str, Any]]) -> tuple[MacroMomentum, ...]:
    changes_by_dataset = {
        str(change.get("dataset_id") or ""): change
        for module in modules
        for change in _module_asset_changes(module)
        if isinstance(change, Mapping)
    }
    output = []
    for symbol in MACRO_THESIS_ASSETS:
        dataset_id = _ASSET_DATASETS[symbol]
        change = changes_by_dataset.get(dataset_id)
        metrics = change.get("metrics") if isinstance(change, Mapping) else None
        one_week = _metric_value(metrics, ("return_1w_pct", "change_1w_bp", "change_wow_pct", "change_wow_bp"))
        one_month = _metric_value(metrics, ("return_1m_pct", "change_1m_bp", "change_4w_pct", "change_4w_bp"))
        output.append(
            MacroMomentum(
                symbol=symbol,
                momentum_1w=_momentum_direction(one_week),
                momentum_1m=_momentum_direction(one_month),
                return_1w_pct=one_week,
                return_1m_pct=one_month,
                source_dataset_id=dataset_id if change is not None else None,
                as_of=str(change.get("as_of")) if isinstance(change, Mapping) and change.get("as_of") else None,
            )
        )
    return tuple(output)


def _module_asset_changes(module: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    evidence = module.get("evidence")
    if not isinstance(evidence, Mapping):
        return ()
    return tuple(change for change in evidence.get("asset_changes", ()) if isinstance(change, Mapping))


def _module_decision_changes(module: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    summary = module.get("summary")
    top_changes = (
        tuple(change for change in summary.get("top_changes", ()) if isinstance(change, Mapping))
        if isinstance(summary, Mapping)
        else ()
    )
    by_dataset: dict[str, Mapping[str, Any]] = {
        str(change.get("dataset_id") or ""): change
        for change in (*top_changes, *_module_asset_changes(module))
        if change.get("dataset_id")
    }
    return tuple(by_dataset.values())


def _momentum_direction(
    value: float | None,
) -> Literal["up", "down", "flat", "insufficient"]:
    if value is None:
        return "insufficient"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _metric_value(metrics: object, names: Sequence[str]) -> float | None:
    if not isinstance(metrics, Mapping):
        return None
    for name in names:
        value = metrics.get(name)
        if isinstance(value, int | float):
            return float(value)
    return None


__all__ = [
    "MACRO_EVIDENCE_PACK_SCHEMA_VERSION",
    "MACRO_LIVE_DELTA_SCHEMA_VERSION",
    "MACRO_OUTCOME_REPLAY_SCHEMA_VERSION",
    "MACRO_THESIS_ASSETS",
    "MACRO_THESIS_OUTCOME_DATASETS",
    "MACRO_THESIS_SCHEMA_VERSION",
    "MacroAlternative",
    "MacroAssetOutlook",
    "MacroAssetView",
    "MacroChangeFromPrior",
    "MacroCondition",
    "MacroEvidencePackV3",
    "MacroLiveDeltaV1",
    "MacroMainline",
    "MacroModuleRole",
    "MacroMomentum",
    "MacroOutcomeHorizon",
    "MacroOutcomeReplayV1",
    "MacroTension",
    "MacroThesisAgent",
    "MacroThesisBodyDraft",
    "MacroThesisClaim",
    "MacroThesisReviewFailure",
    "MacroThesisReviewV1",
    "MacroThesisReviewer",
    "MacroThesisV1",
    "build_publication",
    "compile_evidence_pack_v3",
    "evaluate_live_delta",
    "evaluate_outcome_replay",
    "payload_hash",
    "pending_outcome_replay",
    "run_thesis_review_cycle",
    "validate_draft_against_pack",
]
