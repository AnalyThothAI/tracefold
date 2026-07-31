from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tracefold.macro.assets import (
    MACRO_ASSET_DATASETS,
    MACRO_THESIS_ASSETS,
)
from tracefold.macro.calculations import CALCULATION_REGISTRY
from tracefold.macro.domain import MACRO_MODULE_IDS, MacroModuleId
from tracefold.macro.registry import DATASET_REGISTRY
from tracefold.macro.thesis import (
    MacroEvidencePackV3,
    MacroMomentum,
    MacroThesisV1,
    payload_hash,
)

MACRO_RESEARCH_INPUT_SCHEMA_VERSION = "macro_research_input_v1"
MACRO_THESIS_DRAFT_SCHEMA_VERSION = "macro_thesis_draft_v2"
MACRO_THESIS_SCHEMA_VERSION_V2 = "macro_thesis_v2"
MACRO_LIVE_DELTA_SCHEMA_VERSION_V2 = "macro_live_delta_v2"
MACRO_OUTCOME_REPLAY_SCHEMA_VERSION_V2 = "macro_outcome_replay_v2"

MACRO_THESIS_PROFILE_VERSION = "macro_thesis_thin_v1"
MACRO_THESIS_PROMPT_VERSION = "macro_thesis_sop_v3"

MAX_RESEARCH_INPUT_BYTES = 64 * 1024
MAX_EXACT_EVIDENCE_REFS = 64
MAX_CONDITION_CANDIDATES = 32
MAX_STRUCTURE_MAPPING_ITEMS = 6

ConditionKind = Literal["confirmation", "weakening", "falsifier", "checkpoint"]
ConditionScopeKind = Literal["mainline", "alternative", "tension", "asset"]
PublicationGateCategory = Literal["time_identity", "evidence_closure", "contract_validity", "write_safety"]
CurrentThesisState = Literal[
    "published",
    "pending",
    "running",
    "retryable",
    "failed",
    "config_error",
    "not_published",
    "missing",
]

_MODULE_STRUCTURE_KEYS: dict[MacroModuleId, tuple[str, ...]] = {
    "rates_fed": ("curve", "policy_pricing", "positioning", "fed"),
    "economy_inflation": ("growth", "inflation", "labor"),
    "liquidity_funding": ("balance_sheet", "funding"),
    "credit": (
        "spread_ladder",
        "funding_costs",
        "loan_quality",
        "bank_lending",
        "cycle_dimensions",
        "confirmations",
    ),
    "volatility": ("term_structure", "cross_asset_implied"),
    "cross_asset": ("correlations",),
}

_REGISTRY_ORDER = {
    "rates.curve10y2y": 0,
    "rates.real10y.tail": 1,
    "economy.release_surprise": 2,
    "economy.release_revision": 3,
    "economy.cpi_yoy.tail": 4,
    "economy.payroll.tail": 5,
    "liquidity.net_4w.tail": 6,
    "liquidity.sofr_iorb": 7,
    "credit.hy_ig_gap.tail": 8,
    "credit.ccc_bb_gap.tail": 9,
    "vol.vix_vxv_zero": 10,
    "vol.vx_front2_zero": 11,
    "cross.corr.tail": 12,
    "cross.return1m.tail": 13,
}

_METRIC_FAMILIES = (
    {
        "prefix": "rates.curve10y2y",
        "module_id": "rates_fed",
        "feature_id": "rates.curve_10y2y",
        "unit": "basis_points",
        "predicates": (("lte", 0.0, "le0"), ("gt", 0.0, "gt0")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "rates.real10y.tail",
        "module_id": "rates_fed",
        "feature_id": "rates.real_10y",
        "unit": "percent",
        "predicates": (("lte_q20", None, "leq20"), ("gte_q80", None, "geq80")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "economy.cpi_yoy.tail",
        "module_id": "economy_inflation",
        "feature_id": "inflation.cpi_yoy",
        "unit": "percent",
        "predicates": (("lte_q20", None, "leq20"), ("gte_q80", None, "geq80")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "economy.payroll.tail",
        "module_id": "economy_inflation",
        "feature_id": "labor.payroll_monthly_change",
        "unit": "thousands_persons",
        "predicates": (("lte_q20", None, "leq20"), ("gte_q80", None, "geq80")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "liquidity.net_4w.tail",
        "module_id": "liquidity_funding",
        "feature_id": "liquidity.net_liquidity",
        "metric": "change_4w",
        "unit": "billions_usd",
        "predicates": (("lte_q20", None, "leq20"), ("gte_q80", None, "geq80")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "liquidity.sofr_iorb",
        "module_id": "liquidity_funding",
        "feature_id": "liquidity.sofr_iorb",
        "unit": "basis_points",
        "predicates": (("lte", 0.0, "le0"), ("gt", 0.0, "gt0")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "credit.hy_ig_gap.tail",
        "module_id": "credit",
        "feature_id": "credit.hy_ig_oas_gap",
        "unit": "basis_points",
        "predicates": (("lte_q20", None, "leq20"), ("gte_q80", None, "geq80")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "credit.ccc_bb_gap.tail",
        "module_id": "credit",
        "feature_id": "credit.ccc_bb_gap",
        "unit": "basis_points",
        "predicates": (("lte_q20", None, "leq20"), ("gte_q80", None, "geq80")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "vol.vix_vxv_zero",
        "module_id": "volatility",
        "feature_id": "volatility.vix_term_spread",
        "unit": "index_points",
        "predicates": (("lte", 0.0, "le0"), ("gt", 0.0, "gt0")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
    {
        "prefix": "vol.vx_front2_zero",
        "module_id": "volatility",
        "feature_id": "volatility.vx_front2_spread",
        "unit": "index_points",
        "predicates": (("lte", 0.0, "le0"), ("gt", 0.0, "gt0")),
        "allowed_scopes": ("mainline", "alternative", "tension"),
    },
)

MACRO_CONDITION_FAMILY_PREFIXES: tuple[str, ...] = (
    "rates.curve10y2y",
    "rates.real10y.tail",
    "economy.release_surprise",
    "economy.release_revision",
    "economy.cpi_yoy.tail",
    "economy.payroll.tail",
    "liquidity.net_4w.tail",
    "liquidity.sofr_iorb",
    "credit.hy_ig_gap.tail",
    "credit.ccc_bb_gap.tail",
    "vol.vix_vxv_zero",
    "vol.vx_front2_zero",
    "cross.corr.tail",
    "cross.return1m.tail",
)


class ExactMacroV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExactEvidenceReference(ExactMacroV2Model):
    evidence_ref: str = Field(min_length=1, max_length=300)
    module_id: MacroModuleId
    dataset_id: str = Field(min_length=1, max_length=200)
    source_id: str | None = Field(default=None, max_length=200)
    source_role: str | None = Field(default=None, max_length=80)
    label: str = Field(min_length=1, max_length=300)
    value: float | str | None
    unit: str
    as_of: str | None
    authoritative_at_ms: int = Field(ge=0)
    required: bool
    source_url: str | None = None


class MacroDriverCandidate(ExactMacroV2Model):
    candidate_id: str = Field(min_length=1, max_length=300)
    dataset_id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=300)
    value: float | None
    unit: str
    metrics: dict[str, float | None]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=6)


class MacroMaterialChangeCandidate(ExactMacroV2Model):
    candidate_id: str = Field(min_length=1, max_length=300)
    dataset_id: str = Field(min_length=1, max_length=200)
    status_hint: Literal["new", "strengthened", "weakened", "reversed"] | None = None
    label: str = Field(min_length=1, max_length=300)
    value: float | None
    unit: str
    metrics: dict[str, float | None]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=6)


class MacroCounterSignalCandidate(ExactMacroV2Model):
    candidate_id: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=2_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=6)


class MetricConditionCandidate(ExactMacroV2Model):
    candidate_type: Literal["metric_condition"] = "metric_condition"
    candidate_id: str = Field(min_length=1, max_length=300)
    module_id: MacroModuleId
    dataset_id: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=160)
    unit: str
    operator: Literal["gt", "gte", "lt", "lte"]
    threshold: float
    frozen_value: float
    as_of: str
    historical_percentile_rank: float | None = Field(default=None, ge=0, le=1)
    quantile_window: Literal["five_years"] | None = None
    sample_count: int = Field(ge=0)
    allowed_kinds: tuple[Literal["confirmation", "weakening", "falsifier"], ...]
    allowed_scopes: tuple[ConditionScopeKind, ...]
    meaning: str = Field(min_length=1, max_length=1_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=8)


class EventCheckpointCandidate(ExactMacroV2Model):
    candidate_type: Literal["event_checkpoint"] = "event_checkpoint"
    candidate_id: str = Field(min_length=1, max_length=300)
    module_id: MacroModuleId
    event_id: str = Field(min_length=1, max_length=240)
    scheduled_at_ms: int = Field(ge=0)
    observed_at_ms: int | None = Field(default=None, ge=0)
    allowed_kinds: tuple[Literal["checkpoint"], ...] = ("checkpoint",)
    allowed_scopes: tuple[ConditionScopeKind, ...]
    meaning: str = Field(min_length=1, max_length=1_000)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=8)


ConditionCandidate = MetricConditionCandidate | EventCheckpointCandidate


class MacroResearchModuleCapsule(ExactMacroV2Model):
    module_id: MacroModuleId
    state_as_of_cutoff: Literal["current", "degraded", "unavailable"]
    structure: dict[str, Any]
    driver_candidates: tuple[MacroDriverCandidate, ...] = Field(default=(), max_length=3)
    material_changes: tuple[MacroMaterialChangeCandidate, ...] = Field(default=(), max_length=2)
    counter_signal_candidates: tuple[MacroCounterSignalCandidate, ...] = Field(default=(), max_length=2)
    source_clock_ms: int | None = Field(default=None, ge=0)
    gaps: tuple[dict[str, Any], ...]
    exact_evidence_refs: tuple[str, ...] = Field(default=(), max_length=6)
    condition_candidate_ids: tuple[str, ...] = Field(default=(), max_length=4)
    omitted_count: dict[str, int]


class MacroResearchInputV1(ExactMacroV2Model):
    schema_version: Literal["macro_research_input_v1"] = "macro_research_input_v1"
    evidence_pack_id: str
    evidence_pack_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    session_date: date
    cutoff_ms: int = Field(ge=0)
    profile_version: Literal["macro_thesis_thin_v1"] = "macro_thesis_thin_v1"
    prompt_version: Literal["macro_thesis_sop_v3"] = "macro_thesis_sop_v3"
    modules: tuple[MacroResearchModuleCapsule, ...]
    momentum: tuple[MacroMomentum, ...]
    prior_material_delta: dict[str, Any]
    scheduled_catalysts: tuple[dict[str, Any], ...]
    exact_evidence: tuple[ExactEvidenceReference, ...]
    condition_candidates: tuple[ConditionCandidate, ...]
    allowed_module_ids: tuple[MacroModuleId, ...] = MACRO_MODULE_IDS
    allowed_evidence_ids: tuple[str, ...]
    allowed_condition_ids: tuple[str, ...]
    omitted_count: dict[str, int]

    @model_validator(mode="after")
    def validate_input(self) -> MacroResearchInputV1:
        if tuple(module.module_id for module in self.modules) != MACRO_MODULE_IDS:
            raise ValueError("macro_research_input_module_order")
        if tuple(item.symbol for item in self.momentum) != MACRO_THESIS_ASSETS:
            raise ValueError("macro_research_input_asset_order")
        if len(self.exact_evidence) > MAX_EXACT_EVIDENCE_REFS:
            raise ValueError("macro_research_input_exact_evidence_budget")
        if len(self.condition_candidates) > MAX_CONDITION_CANDIDATES:
            raise ValueError("macro_research_input_condition_budget")
        evidence_ids = tuple(item.evidence_ref for item in self.exact_evidence)
        condition_ids = tuple(item.candidate_id for item in self.condition_candidates)
        if evidence_ids != self.allowed_evidence_ids or len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("macro_research_input_evidence_identity")
        if condition_ids != self.allowed_condition_ids or len(condition_ids) != len(set(condition_ids)):
            raise ValueError("macro_research_input_condition_identity")
        material_change_ids = tuple(item.candidate_id for module in self.modules for item in module.material_changes)
        if len(material_change_ids) != len(set(material_change_ids)):
            raise ValueError("macro_research_input_material_change_identity")
        allowed_evidence = set(self.allowed_evidence_ids)
        if any(
            not set(item.evidence_refs).issubset(allowed_evidence)
            for module in self.modules
            for item in module.material_changes
        ):
            raise ValueError("macro_research_input_material_change_evidence")
        if any(item.authoritative_at_ms > self.cutoff_ms for item in self.exact_evidence):
            raise ValueError("macro_research_input_future_fact")
        encoded = canonical_json_bytes(self.model_dump(mode="json"))
        if len(encoded) > MAX_RESEARCH_INPUT_BYTES:
            raise ValueError("macro_research_input_byte_budget")
        return self

    @property
    def input_hash(self) -> str:
        return payload_hash(self.model_dump(mode="json"))

    @property
    def input_id(self) -> str:
        return "mri1_" + self.input_hash.removeprefix("sha256:")[:32]


class MacroDraftCausalEdge(ExactMacroV2Model):
    edge_id: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=300)
    mechanism: str = Field(min_length=1, max_length=2_000)
    target: str = Field(min_length=1, max_length=300)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=8)


class MacroDraftMainline(ExactMacroV2Model):
    stance: Literal["call", "no_call"]
    title: str = Field(min_length=1, max_length=300)
    thesis: str = Field(min_length=1, max_length=4_000)
    stage: Literal["emerging", "developing", "mature", "reversing", "uncertain"]
    horizon: Literal["1w", "1m", "1w_to_1m"]
    confidence: Literal["low", "medium", "high"] | None = None
    causal_edges: tuple[MacroDraftCausalEdge, ...] = Field(default=(), max_length=3)
    supporting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=12)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=12)
    no_call_reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_directional_shape(self) -> MacroDraftMainline:
        if self.stance == "call" and not 1 <= len(self.causal_edges) <= 3:
            raise ValueError("macro_thesis_v2_directional_edge_count")
        if self.stance == "no_call" and self.causal_edges:
            raise ValueError("macro_thesis_v2_no_call_edges_forbidden")
        if self.stance == "no_call" and not self.no_call_reason:
            raise ValueError("macro_thesis_v2_no_call_reason_required")
        return self


class MacroDraftAlternative(ExactMacroV2Model):
    alternative_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    thesis: str = Field(min_length=1, max_length=3_000)
    causal_edges: tuple[MacroDraftCausalEdge, ...] = Field(min_length=1, max_length=3)
    supporting_evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=12)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=12)


class MacroDraftTensionSide(ExactMacroV2Model):
    statement: str = Field(min_length=1, max_length=1_500)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=8)


class MacroDraftTension(ExactMacroV2Model):
    tension_id: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=2_000)
    side_a: MacroDraftTensionSide
    side_b: MacroDraftTensionSide
    leading_side: Literal["side_a", "side_b", "balanced", "uncertain"]
    unresolved_reason: str = Field(min_length=1, max_length=1_500)


class MacroDraftModuleAssessment(ExactMacroV2Model):
    module_id: MacroModuleId
    role: Literal["driver", "confirming", "contradicting", "uncertain"]
    analysis: str = Field(min_length=1, max_length=2_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=8)


class MacroDraftMaterialChange(ExactMacroV2Model):
    candidate_id: str = Field(min_length=1, max_length=300)
    status: Literal["new", "strengthened", "weakened", "reversed"]
    statement: str = Field(min_length=1, max_length=2_000)


class MacroPublishedMaterialChange(ExactMacroV2Model):
    candidate_id: str = Field(min_length=1, max_length=300)
    status: Literal["new", "strengthened", "weakened", "reversed"]
    statement: str = Field(min_length=1, max_length=2_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=8)


class MacroDraftConditionUse(ExactMacroV2Model):
    candidate_id: str = Field(min_length=1, max_length=300)
    kind: ConditionKind
    scope_kind: ConditionScopeKind
    scope_id: str = Field(min_length=1, max_length=200)
    symbol: str | None = None
    horizon: Literal["1w", "1m"] | None = None
    rationale: str = Field(min_length=1, max_length=1_500)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_asset_scope(self) -> MacroDraftConditionUse:
        if self.scope_kind == "asset":
            if self.symbol not in MACRO_THESIS_ASSETS or self.horizon is None:
                raise ValueError("macro_thesis_v2_asset_condition_binding")
        elif self.symbol is not None or self.horizon is not None:
            raise ValueError("macro_thesis_v2_non_asset_condition_binding")
        return self


class MacroDraftAssetOutlook(ExactMacroV2Model):
    outlook_id: str = Field(min_length=1, max_length=200)
    symbol: str
    horizon: Literal["1w", "1m"]
    outlook_context: Literal["mainline", "alternative", "tension", "local"]
    direction: Literal["bullish", "bearish", "neutral"]
    causal_transmission: str = Field(min_length=1, max_length=2_000)
    supporting_evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    conflicting_evidence_refs: tuple[str, ...] = Field(default=(), max_length=8)
    confidence: Literal["low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def validate_symbol(self) -> MacroDraftAssetOutlook:
        if self.symbol not in MACRO_THESIS_ASSETS:
            raise ValueError("macro_thesis_v2_asset_unknown")
        return self


class MacroThesisDraftV2(ExactMacroV2Model):
    schema_version: Literal["macro_thesis_draft_v2"] = "macro_thesis_draft_v2"
    session_date: date
    cutoff_ms: int = Field(ge=0)
    evidence_pack_id: str
    research_input_id: str
    mainline: MacroDraftMainline
    alternative: MacroDraftAlternative | None = None
    tensions: tuple[MacroDraftTension, ...] = Field(default=(), max_length=3)
    module_assessments: tuple[MacroDraftModuleAssessment, ...] = Field(default=(), max_length=6)
    material_changes: tuple[MacroDraftMaterialChange, ...] = Field(max_length=8)
    asset_outlooks: tuple[MacroDraftAssetOutlook, ...] = Field(default=(), max_length=12)
    condition_uses: tuple[MacroDraftConditionUse, ...] = Field(default=(), max_length=24)

    @model_validator(mode="after")
    def validate_draft_identity(self) -> MacroThesisDraftV2:
        identifiers = [
            *(item.edge_id for item in self.mainline.causal_edges),
            *((item.edge_id for item in self.alternative.causal_edges) if self.alternative else ()),
            *(item.tension_id for item in self.tensions),
            *(item.candidate_id for item in self.material_changes),
            *(item.outlook_id for item in self.asset_outlooks),
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("macro_thesis_v2_duplicate_identity")
        assessment_ids = [item.module_id for item in self.module_assessments]
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("macro_thesis_v2_duplicate_module_assessment")
        outlook_keys = [(item.symbol, item.horizon) for item in self.asset_outlooks]
        if len(outlook_keys) != len(set(outlook_keys)):
            raise ValueError("macro_thesis_v2_duplicate_asset_outlook")
        use_keys = [(item.scope_kind, item.scope_id, item.candidate_id) for item in self.condition_uses]
        if len(use_keys) != len(set(use_keys)):
            raise ValueError("macro_thesis_v2_duplicate_condition_use")
        return self

    @property
    def evidence_refs(self) -> frozenset[str]:
        refs: set[str] = set(self.mainline.supporting_evidence_refs)
        refs.update(self.mainline.conflicting_evidence_refs)
        for edge in self.mainline.causal_edges:
            refs.update(edge.evidence_refs)
            refs.update(edge.conflicting_evidence_refs)
        if self.alternative is not None:
            refs.update(self.alternative.supporting_evidence_refs)
            refs.update(self.alternative.conflicting_evidence_refs)
            for edge in self.alternative.causal_edges:
                refs.update(edge.evidence_refs)
                refs.update(edge.conflicting_evidence_refs)
        for tension in self.tensions:
            refs.update(tension.side_a.evidence_refs)
            refs.update(tension.side_b.evidence_refs)
        for assessment in self.module_assessments:
            refs.update(assessment.evidence_refs)
        for outlook in self.asset_outlooks:
            refs.update(outlook.supporting_evidence_refs)
            refs.update(outlook.conflicting_evidence_refs)
        for condition in self.condition_uses:
            refs.update(condition.evidence_refs)
        return frozenset(refs)


class CandidateDraftEnvelope(ExactMacroV2Model):
    envelope_version: Literal["candidate_draft_envelope_v1"] = "candidate_draft_envelope_v1"
    attempt_id: str = Field(min_length=1, max_length=240)
    provider_response_id: str = Field(min_length=1, max_length=240)
    provider_name: str = Field(min_length=1, max_length=160)
    model_name: str = Field(min_length=1, max_length=200)
    profile_version: str = Field(min_length=1, max_length=160)
    prompt_version: str = Field(min_length=1, max_length=160)
    research_input_id: str = Field(min_length=1, max_length=200)
    research_input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    raw_structured_mapping: dict[str, Any]
    received_at_ms: int = Field(ge=0)
    model_calls: Literal[1] = 1

    @property
    def candidate_hash(self) -> str:
        return payload_hash(self.raw_structured_mapping)


class MacroCompiledCondition(ExactMacroV2Model):
    condition_id: str
    candidate_type: Literal["metric_condition", "event_checkpoint"]
    candidate_id: str
    kind: ConditionKind
    scope_kind: ConditionScopeKind
    scope_id: str
    symbol: str | None
    horizon: Literal["1w", "1m"] | None
    rationale: str
    evidence_refs: tuple[str, ...]
    module_id: MacroModuleId
    dataset_id: str | None
    metric: str | None
    unit: str | None
    operator: Literal["gt", "gte", "lt", "lte"] | None
    threshold: float | None
    frozen_value: float | None
    as_of: str | None
    event_id: str | None
    scheduled_at_ms: int | None


class MacroCitationV2(ExactMacroV2Model):
    evidence_ref: str
    module_id: MacroModuleId
    dataset_id: str
    source_id: str | None
    source_role: str | None
    label: str
    value: float | str | None
    unit: str
    as_of: str | None
    authoritative_at_ms: int
    source_url: str | None


class MacroEvidenceGapV2(ExactMacroV2Model):
    gap_id: str
    scope_kind: Literal["module", "claim", "asset", "dataset"]
    scope_id: str
    module_id: MacroModuleId
    dataset_id: str | None
    state: str
    reason: str


class MacroFrozenAssetSnapshotV2(ExactMacroV2Model):
    symbol: str
    display_order: int = Field(ge=0, le=11)
    momentum_1w: Literal["up", "down", "flat", "insufficient"]
    momentum_1m: Literal["up", "down", "flat", "insufficient"]
    return_1w_pct: float | None
    return_1m_pct: float | None
    source_dataset_id: str | None
    as_of: str | None


class MacroThesisProvenanceV2(ExactMacroV2Model):
    research_input_id: str
    research_input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    attempt_id: str
    provider_response_id: str
    provider_name: str
    research_model: str
    profile_version: str
    prompt_version: str
    workflow_version: Literal["macro_thesis_workflow_v2"] = "macro_thesis_workflow_v2"


class MacroThesisV2(ExactMacroV2Model):
    schema_version: Literal["macro_thesis_v2"] = "macro_thesis_v2"
    publication_id: str
    session_date: date
    cutoff_ms: int = Field(ge=0)
    evidence_pack_id: str
    evidence_pack_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    research_input_id: str
    research_input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prior_publication_id: str | None
    mainline: MacroDraftMainline
    alternative: MacroDraftAlternative | None
    tensions: tuple[MacroDraftTension, ...]
    material_changes: tuple[MacroPublishedMaterialChange, ...]
    module_assessments: tuple[MacroDraftModuleAssessment, ...]
    assets: tuple[MacroFrozenAssetSnapshotV2, ...]
    asset_outlooks: tuple[MacroDraftAssetOutlook, ...]
    citations: tuple[MacroCitationV2, ...]
    conditions: tuple[MacroCompiledCondition, ...]
    gaps: tuple[MacroEvidenceGapV2, ...]
    catalysts: tuple[dict[str, Any], ...]
    provenance: MacroThesisProvenanceV2
    published_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_publication(self) -> MacroThesisV2:
        if self.published_at_ms < self.cutoff_ms:
            raise ValueError("macro_thesis_v2_published_before_cutoff")
        if tuple(asset.symbol for asset in self.assets) != MACRO_THESIS_ASSETS:
            raise ValueError("macro_thesis_v2_asset_order")
        if tuple(asset.display_order for asset in self.assets) != tuple(range(len(MACRO_THESIS_ASSETS))):
            raise ValueError("macro_thesis_v2_asset_display_order")
        return self

    @property
    def content_hash(self) -> str:
        return payload_hash(self.model_dump(mode="json", exclude={"published_at_ms"}))


def parse_current_thesis_v2(row: Mapping[str, Any] | None) -> MacroThesisV2 | None:
    """Validate the one schema allowed on current Thesis surfaces."""

    if (
        row is None
        or row.get("status") != "published"
        or row.get("schema_version") != "macro_thesis_v2"
        or not isinstance(row.get("thesis_json"), Mapping)
    ):
        return None
    try:
        return MacroThesisV2.model_validate(row["thesis_json"])
    except ValidationError:
        return None


def classify_current_thesis_state(
    row: Mapping[str, Any] | None,
    thesis: MacroThesisV2 | None = None,
) -> CurrentThesisState:
    current = thesis if thesis is not None else parse_current_thesis_v2(row)
    if current is not None:
        return "published"
    if row is None:
        return "missing"
    status = str(row.get("status") or "")
    if status == "published":
        return "not_published"
    if status in {
        "pending",
        "running",
        "retryable",
        "failed",
        "config_error",
        "not_published",
    }:
        return cast(CurrentThesisState, status)
    return "missing"


class PublicationGateFailure(ValueError):
    def __init__(
        self,
        *,
        category: PublicationGateCategory,
        code: str,
        session_date: date,
        cutoff_ms: int,
        evidence_pack_id: str,
        candidate_hash: str,
        retryable: bool,
        recovery_action: str,
        diagnostics: Sequence[str] = (),
    ) -> None:
        super().__init__(code)
        self.category = category
        self.code = code
        self.session_date = session_date
        self.cutoff_ms = cutoff_ms
        self.evidence_pack_id = evidence_pack_id
        self.candidate_hash = candidate_hash
        self.retryable = retryable
        self.recovery_action = recovery_action
        self.diagnostics = tuple(diagnostics)


class MacroThesisAgent(Protocol):
    async def draft(
        self,
        *,
        research_input: MacroResearchInputV1,
        attempt_id: str,
        on_model_submitted: Callable[[], None],
    ) -> CandidateDraftEnvelope: ...


class MacroRecoverySide(ExactMacroV2Model):
    dataset_id: str | None
    source_id: str | None
    value: float | str | None
    unit: str | None
    as_of: str | None


class MacroRecoveryItem(ExactMacroV2Model):
    scope_kind: Literal["module", "claim", "asset", "dataset"]
    scope_id: str
    state: Literal["unchanged", "recovered", "still_missing", "degraded"]
    publication: MacroRecoverySide
    current: MacroRecoverySide
    reason: str


class MacroMetricDeltaItemV2(ExactMacroV2Model):
    item_type: Literal["metric_condition"] = "metric_condition"
    condition_id: str
    candidate_id: str
    scope_kind: ConditionScopeKind
    scope_id: str
    kind: Literal["confirmation", "weakening", "falsifier"]
    state: Literal["confirming", "weakening", "invalidation_triggered", "unrelated", "insufficient"]
    dataset_id: str
    metric: str
    observed_value: float | None
    observed_at_ms: int | None
    operator: Literal["gt", "gte", "lt", "lte"]
    threshold: float
    reason_code: str


class MacroEventDeltaItemV2(ExactMacroV2Model):
    item_type: Literal["event_checkpoint"] = "event_checkpoint"
    condition_id: str
    candidate_id: str
    scope_kind: ConditionScopeKind
    scope_id: str
    kind: Literal["checkpoint"] = "checkpoint"
    state: Literal["upcoming", "due", "observed", "missed", "insufficient"]
    event_id: str
    scheduled_at_ms: int
    observed_at_ms: int | None
    reason_code: str


MacroLiveDeltaItemV2 = MacroMetricDeltaItemV2 | MacroEventDeltaItemV2


class MacroLiveDeltaV2(ExactMacroV2Model):
    schema_version: Literal["macro_live_delta_v2"] = "macro_live_delta_v2"
    live_delta_id: str
    publication_id: str
    evaluated_at_ms: int = Field(ge=0)
    module_fact_cutoff_ms: int = Field(ge=0)
    mainline_validity: Literal[
        "confirming",
        "weakening",
        "invalidation_triggered",
        "unrelated",
        "insufficient",
    ]
    items: tuple[MacroLiveDeltaItemV2, ...]
    reason_codes: tuple[str, ...]
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MacroOutcomeAssetResultV2(ExactMacroV2Model):
    symbol: str
    horizon: Literal["1w", "1m"]
    expires_at_ms: int = Field(ge=0)
    status: Literal["pending", "evaluated", "insufficient"]
    published_direction: Literal["bullish", "bearish", "neutral"]
    realized_return_pct: float | None
    direction_correct: bool | None
    reason_code: str


class MacroOutcomeHorizonV2(ExactMacroV2Model):
    horizon: Literal["1w", "1m"]
    expires_at_ms: int = Field(ge=0)
    status: Literal["pending", "evaluated", "insufficient"]
    asset_results: tuple[MacroOutcomeAssetResultV2, ...]
    reason_code: str


class MacroOutcomeReplayV2(ExactMacroV2Model):
    schema_version: Literal["macro_outcome_replay_v2"] = "macro_outcome_replay_v2"
    replay_id: str
    publication_id: str
    evaluated_at_ms: int = Field(ge=0)
    horizons: tuple[MacroOutcomeHorizonV2, ...]
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_horizons(self) -> MacroOutcomeReplayV2:
        horizons = tuple(item.horizon for item in self.horizons)
        if horizons not in {("1w",), ("1m",), ("1w", "1m")}:
            raise ValueError("macro_outcome_replay_v2_horizon_order")
        return self


def canonical_json_bytes(payload: Mapping[str, Any] | Sequence[Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compile_research_input_v1(
    evidence_pack: MacroEvidencePackV3,
    *,
    profile_version: str = MACRO_THESIS_PROFILE_VERSION,
    prompt_version: str = MACRO_THESIS_PROMPT_VERSION,
) -> MacroResearchInputV1:
    if profile_version != MACRO_THESIS_PROFILE_VERSION:
        raise ValueError("macro_research_input_profile_version")
    if prompt_version != MACRO_THESIS_PROMPT_VERSION:
        raise ValueError("macro_research_input_prompt_version")

    exact_by_module = _exact_evidence_by_module(evidence_pack)
    all_candidates = _condition_candidates(evidence_pack, exact_by_module)
    candidates_by_module: dict[str, list[ConditionCandidate]] = defaultdict(list)
    for candidate in all_candidates:
        candidates_by_module[candidate.module_id].append(candidate)

    local_ref_order: dict[str, list[str]] = {}
    capsules: list[MacroResearchModuleCapsule] = []
    selected_candidates: list[ConditionCandidate] = []
    for module in evidence_pack.modules:
        module_id = cast(MacroModuleId, str(module["module_id"]))
        exact_rows = exact_by_module[module_id]
        module_candidates = sorted(candidates_by_module[module_id], key=_condition_candidate_sort_key)
        chosen_candidates = module_candidates[:4]
        selected_candidates.extend(chosen_candidates)
        changes = _module_change_candidates(module, exact_rows)
        candidate_refs = [
            ref
            for refs in (
                *(change.evidence_refs for change in changes[:3]),
                *(candidate.evidence_refs for candidate in chosen_candidates),
            )
            for ref in refs
            if ref in {item.evidence_ref for item in exact_rows}
        ]
        remaining_refs = [
            item.evidence_ref
            for item in sorted(exact_rows, key=_evidence_sort_key)
            if item.evidence_ref not in candidate_refs
        ]
        local_refs = list(dict.fromkeys([*candidate_refs, *remaining_refs]))[:6]
        local_ref_set = set(local_refs)
        selected_changes = [change for change in changes if set(change.evidence_refs).issubset(local_ref_set)]
        local_ref_order[module_id] = local_refs
        counter_signals = _module_counter_signals(module, local_refs)
        structure_source = {
            key: module.get(key) for key in _MODULE_STRUCTURE_KEYS[module_id] if module.get(key) is not None
        }
        capsules.append(
            MacroResearchModuleCapsule(
                module_id=module_id,
                state_as_of_cutoff=_module_current_state(module),
                structure=_bounded_structure(structure_source),
                driver_candidates=tuple(
                    MacroDriverCandidate(
                        candidate_id=f"driver:{module_id}:{change.candidate_id}",
                        dataset_id=change.dataset_id,
                        label=change.label,
                        value=change.value,
                        unit=change.unit,
                        metrics={},
                        evidence_refs=change.evidence_refs,
                    )
                    for change in selected_changes[:3]
                ),
                material_changes=tuple(selected_changes[:2]),
                counter_signal_candidates=tuple(counter_signals[:2]),
                source_clock_ms=max(
                    (item.authoritative_at_ms for item in exact_rows),
                    default=0,
                )
                or None,
                gaps=tuple(_required_gaps(module)),
                exact_evidence_refs=tuple(local_refs),
                condition_candidate_ids=tuple(item.candidate_id for item in chosen_candidates),
                omitted_count={
                    "drivers": max(0, len(changes) - len(selected_changes[:3])),
                    "material_changes": max(0, len(changes) - len(selected_changes[:2])),
                    "counter_signals": max(0, len(counter_signals) - 2),
                    "exact_evidence": max(0, len(exact_rows) - len(local_refs)),
                    "condition_candidates": max(0, len(module_candidates) - len(chosen_candidates)),
                    "structure_items": _bounded_structure_omissions(structure_source),
                },
            )
        )

    global_refs = _round_robin_refs(local_ref_order, limit=MAX_EXACT_EVIDENCE_REFS)
    selected_ref_set = set(global_refs)
    exact_lookup = {item.evidence_ref: item for rows in exact_by_module.values() for item in rows}
    exact_evidence = tuple(exact_lookup[ref] for ref in global_refs)
    selected_candidates = [
        candidate for candidate in selected_candidates if set(candidate.evidence_refs).issubset(selected_ref_set)
    ][:MAX_CONDITION_CANDIDATES]
    candidate_ids = {item.candidate_id for item in selected_candidates}
    normalized_capsules = tuple(
        capsule.model_copy(
            update={
                "condition_candidate_ids": tuple(
                    candidate_id for candidate_id in capsule.condition_candidate_ids if candidate_id in candidate_ids
                )
            }
        )
        for capsule in capsules
    )
    scheduled = tuple(_scheduled_catalysts(evidence_pack, selected_ref_set))
    event_candidates = [
        EventCheckpointCandidate(
            candidate_id=f"event:{item['event_id']}",
            module_id=item["module_id"],
            event_id=item["event_id"],
            scheduled_at_ms=item["scheduled_at_ms"],
            observed_at_ms=item.get("observed_at_ms"),
            allowed_scopes=("mainline", "alternative", "tension", "asset"),
            meaning=item["meaning"],
            evidence_refs=tuple(item.get("evidence_refs") or ()),
        )
        for item in scheduled
        if f"event:{item['event_id']}" not in candidate_ids
    ]
    remaining_budget = MAX_CONDITION_CANDIDATES - len(selected_candidates)
    selected_candidates.extend(event_candidates[:remaining_budget])

    return MacroResearchInputV1(
        evidence_pack_id=evidence_pack.evidence_pack_id,
        evidence_pack_hash=evidence_pack.payload_hash,
        session_date=evidence_pack.session_date,
        cutoff_ms=evidence_pack.cutoff_ms,
        profile_version=profile_version,
        prompt_version=prompt_version,
        modules=normalized_capsules,
        momentum=evidence_pack.momentum,
        prior_material_delta=_bounded_structure(evidence_pack.delta_pack),
        scheduled_catalysts=scheduled,
        exact_evidence=exact_evidence,
        condition_candidates=tuple(selected_candidates),
        allowed_evidence_ids=tuple(item.evidence_ref for item in exact_evidence),
        allowed_condition_ids=tuple(item.candidate_id for item in selected_candidates),
        omitted_count={
            "exact_evidence": max(
                0,
                sum(len(rows) for rows in exact_by_module.values()) - len(exact_evidence),
            ),
            "condition_candidates": max(
                0,
                len(all_candidates) + len(event_candidates) - len(selected_candidates),
            ),
        },
    )


def compile_candidate_publication_v2(
    *,
    envelope: CandidateDraftEnvelope,
    research_input: MacroResearchInputV1,
    evidence_pack: MacroEvidencePackV3,
    published_at_ms: int,
) -> MacroThesisV2:
    try:
        draft = MacroThesisDraftV2.model_validate(envelope.raw_structured_mapping)
    except ValidationError as exc:
        raise _gate_failure(
            "contract_validity",
            "macro_thesis_contract_schema_invalid",
            envelope=envelope,
            research_input=research_input,
            diagnostics=(str(exc)[:2_000],),
        ) from exc

    time_diagnostics: list[str] = []
    if envelope.research_input_id != research_input.input_id:
        time_diagnostics.append("envelope_research_input_id_mismatch")
    if envelope.research_input_hash != research_input.input_hash:
        time_diagnostics.append("envelope_research_input_hash_mismatch")
    if envelope.profile_version != research_input.profile_version:
        time_diagnostics.append("profile_version_mismatch")
    if envelope.prompt_version != research_input.prompt_version:
        time_diagnostics.append("prompt_version_mismatch")
    if draft.session_date != research_input.session_date:
        time_diagnostics.append("session_date_mismatch")
    if draft.cutoff_ms != research_input.cutoff_ms:
        time_diagnostics.append("cutoff_mismatch")
    if draft.evidence_pack_id != evidence_pack.evidence_pack_id:
        time_diagnostics.append("evidence_pack_id_mismatch")
    if draft.research_input_id != research_input.input_id:
        time_diagnostics.append("research_input_id_mismatch")
    if research_input.evidence_pack_hash != evidence_pack.payload_hash:
        time_diagnostics.append("evidence_pack_hash_mismatch")
    if any(item.authoritative_at_ms > research_input.cutoff_ms for item in research_input.exact_evidence):
        time_diagnostics.append("future_fact")
    if time_diagnostics:
        raise _gate_failure(
            "time_identity",
            "macro_thesis_time_identity_invalid",
            envelope=envelope,
            research_input=research_input,
            diagnostics=time_diagnostics,
        )

    unknown_refs = sorted(draft.evidence_refs - set(research_input.allowed_evidence_ids))
    if unknown_refs:
        raise _gate_failure(
            "evidence_closure",
            "macro_thesis_evidence_closure_invalid",
            envelope=envelope,
            research_input=research_input,
            diagnostics=tuple(unknown_refs),
        )

    try:
        compiled_conditions = _compile_selected_conditions(draft, research_input)
        compiled_material_changes = _compile_material_changes(draft, research_input)
        _validate_draft_scopes(draft)
    except ValueError as exc:
        raise _gate_failure(
            "contract_validity",
            "macro_thesis_contract_binding_invalid",
            envelope=envelope,
            research_input=research_input,
            diagnostics=(str(exc),),
        ) from exc

    draft_hash = payload_hash(draft.model_dump(mode="json"))
    seed = {
        "session_date": research_input.session_date.isoformat(),
        "cutoff_ms": research_input.cutoff_ms,
        "evidence_pack_hash": research_input.evidence_pack_hash,
        "research_input_hash": research_input.input_hash,
        "draft_hash": draft_hash,
        "profile_version": envelope.profile_version,
        "prompt_version": envelope.prompt_version,
        "model_name": envelope.model_name,
    }
    publication_id = "mth2_" + payload_hash(seed).removeprefix("sha256:")[:32]
    citation_lookup = {item.evidence_ref: item for item in research_input.exact_evidence}
    citation_refs = draft.evidence_refs | frozenset(
        ref for change in compiled_material_changes for ref in change.evidence_refs
    )
    citations = tuple(
        MacroCitationV2(
            **citation_lookup[ref].model_dump(
                mode="python",
                exclude={"required"},
            )
        )
        for ref in research_input.allowed_evidence_ids
        if ref in citation_refs
    )
    prior_publication_id = (
        str(evidence_pack.prior_publication.get("publication_id"))
        if evidence_pack.prior_publication is not None
        else None
    )
    return MacroThesisV2(
        publication_id=publication_id,
        session_date=research_input.session_date,
        cutoff_ms=research_input.cutoff_ms,
        evidence_pack_id=research_input.evidence_pack_id,
        evidence_pack_hash=research_input.evidence_pack_hash,
        research_input_id=research_input.input_id,
        research_input_hash=research_input.input_hash,
        draft_hash=draft_hash,
        prior_publication_id=prior_publication_id,
        mainline=draft.mainline,
        alternative=draft.alternative,
        tensions=draft.tensions,
        material_changes=compiled_material_changes,
        module_assessments=draft.module_assessments,
        assets=tuple(
            MacroFrozenAssetSnapshotV2(
                symbol=item.symbol,
                display_order=index,
                momentum_1w=item.momentum_1w,
                momentum_1m=item.momentum_1m,
                return_1w_pct=item.return_1w_pct,
                return_1m_pct=item.return_1m_pct,
                source_dataset_id=item.source_dataset_id,
                as_of=item.as_of,
            )
            for index, item in enumerate(research_input.momentum)
        ),
        asset_outlooks=draft.asset_outlooks,
        citations=citations,
        conditions=compiled_conditions,
        gaps=_publication_gaps(research_input),
        catalysts=research_input.scheduled_catalysts,
        provenance=MacroThesisProvenanceV2(
            research_input_id=research_input.input_id,
            research_input_hash=research_input.input_hash,
            draft_hash=draft_hash,
            candidate_hash=envelope.candidate_hash,
            attempt_id=envelope.attempt_id,
            provider_response_id=envelope.provider_response_id,
            provider_name=envelope.provider_name,
            research_model=envelope.model_name,
            profile_version=envelope.profile_version,
            prompt_version=envelope.prompt_version,
        ),
        published_at_ms=published_at_ms,
    )


def evaluate_live_delta_v2(
    *,
    publication: MacroThesisV2,
    modules: Sequence[Mapping[str, Any]],
    evaluated_at_ms: int,
) -> MacroLiveDeltaV2:
    values = _current_metric_values(modules)
    event_observations = _current_event_observations(modules)
    fact_cutoff = max(
        (int(module.get("latest_fact_at_ms") or 0) for module in modules),
        default=publication.cutoff_ms,
    )
    input_payload = {
        "publication_id": publication.publication_id,
        "module_fact_cutoff_ms": fact_cutoff,
        "values": sorted((f"{dataset}:{metric}", value) for (dataset, metric), value in values.items()),
        "events": sorted(event_observations.items()),
    }
    input_hash = payload_hash(input_payload)
    items: list[MacroLiveDeltaItemV2] = []
    for condition in publication.conditions:
        if condition.candidate_type == "event_checkpoint":
            observed_at = event_observations.get(condition.event_id or "")
            scheduled_at = int(condition.scheduled_at_ms or 0)
            if observed_at is not None:
                state: Literal["upcoming", "due", "observed", "missed", "insufficient"] = "observed"
                reason = "event_observed"
            elif evaluated_at_ms < scheduled_at:
                state = "upcoming"
                reason = "event_upcoming"
            elif evaluated_at_ms <= scheduled_at + 86_400_000:
                state = "due"
                reason = "event_due"
            else:
                state = "missed"
                reason = "event_not_observed_after_due"
            items.append(
                MacroEventDeltaItemV2(
                    condition_id=condition.condition_id,
                    candidate_id=condition.candidate_id,
                    scope_kind=condition.scope_kind,
                    scope_id=condition.scope_id,
                    state=state,
                    event_id=condition.event_id or "",
                    scheduled_at_ms=scheduled_at,
                    observed_at_ms=observed_at,
                    reason_code=reason,
                )
            )
            continue
        value = values.get((condition.dataset_id or "", condition.metric or ""))
        matched = _predicate_matches(value, condition.operator, condition.threshold) if value is not None else None
        if matched is None:
            metric_state: Literal["confirming", "weakening", "invalidation_triggered", "unrelated", "insufficient"] = (
                "insufficient"
            )
        elif condition.kind == "falsifier":
            metric_state = "invalidation_triggered" if matched else "unrelated"
        elif condition.kind == "weakening":
            metric_state = "weakening" if matched else "unrelated"
        else:
            metric_state = "confirming" if matched else "weakening"
        items.append(
            MacroMetricDeltaItemV2(
                condition_id=condition.condition_id,
                candidate_id=condition.candidate_id,
                scope_kind=condition.scope_kind,
                scope_id=condition.scope_id,
                kind=condition.kind,
                state=metric_state,
                dataset_id=condition.dataset_id or "",
                metric=condition.metric or "",
                observed_value=value,
                observed_at_ms=fact_cutoff if value is not None else None,
                operator=condition.operator or "gte",
                threshold=float(condition.threshold or 0),
                reason_code=f"metric_condition_{metric_state}",
            )
        )
    mainline_states = [
        item.state for item in items if isinstance(item, MacroMetricDeltaItemV2) and item.scope_kind == "mainline"
    ]
    validity = _aggregate_mainline_validity(mainline_states)
    live_delta_id = (
        "mld2_"
        + payload_hash({"publication_id": publication.publication_id, "input_hash": input_hash}).removeprefix(
            "sha256:"
        )[:40]
    )
    return MacroLiveDeltaV2(
        live_delta_id=live_delta_id,
        publication_id=publication.publication_id,
        evaluated_at_ms=evaluated_at_ms,
        module_fact_cutoff_ms=max(publication.cutoff_ms, fact_cutoff),
        mainline_validity=validity,
        items=tuple(items),
        reason_codes=tuple(sorted({item.reason_code for item in items})),
        input_hash=input_hash,
    )


def evaluate_outcome_replay_v2(
    *,
    publication: MacroThesisV2,
    market_rows: Sequence[Mapping[str, Any]],
    evaluated_at_ms: int,
) -> MacroOutcomeReplayV2:
    horizons = _declared_horizons(publication)
    relevant_symbols = {outlook.symbol for outlook in publication.asset_outlooks if outlook.horizon in horizons}
    relevant_datasets = {MACRO_ASSET_DATASETS[symbol] for symbol in relevant_symbols}
    rows = [
        row
        for row in market_rows
        if str(row.get("dataset_id") or "") in relevant_datasets and row.get("value_numeric") is not None
    ]
    rows.sort(key=lambda row: (_market_row_time(row), str(row.get("dataset_id") or "")))
    input_hash = payload_hash(
        {
            "publication_id": publication.publication_id,
            "rows": [
                {
                    "dataset_id": row.get("dataset_id"),
                    "time": _market_row_time(row),
                    "value": float(row["value_numeric"]),
                }
                for row in rows
            ],
        }
    )
    horizon_rows: list[MacroOutcomeHorizonV2] = []
    for horizon in horizons:
        expires_at_ms = publication.cutoff_ms + (7 if horizon == "1w" else 30) * 86_400_000
        asset_results = tuple(
            _asset_outcome_result_v2(
                publication=publication,
                outlook=outlook,
                rows=rows,
                expires_at_ms=expires_at_ms,
                evaluated_at_ms=evaluated_at_ms,
            )
            for outlook in publication.asset_outlooks
            if outlook.horizon == horizon
        )
        statuses = {item.status for item in asset_results}
        status: Literal["pending", "evaluated", "insufficient"]
        if evaluated_at_ms < expires_at_ms:
            status = "pending"
        elif asset_results and statuses == {"evaluated"}:
            status = "evaluated"
        else:
            status = "insufficient"
        horizon_rows.append(
            MacroOutcomeHorizonV2(
                horizon=horizon,
                expires_at_ms=expires_at_ms,
                status=status,
                asset_results=asset_results,
                reason_code=f"outcome_{status}",
            )
        )
    replay_id = (
        "mor2_"
        + payload_hash({"publication_id": publication.publication_id, "input_hash": input_hash}).removeprefix(
            "sha256:"
        )[:40]
    )
    return MacroOutcomeReplayV2(
        replay_id=replay_id,
        publication_id=publication.publication_id,
        evaluated_at_ms=evaluated_at_ms,
        horizons=tuple(horizon_rows),
        input_hash=input_hash,
    )


def project_current_recovery(
    *,
    publication: MacroThesisV1 | MacroThesisV2,
    modules: Sequence[Mapping[str, Any]],
) -> tuple[MacroRecoveryItem, ...]:
    current_momentum = {item.symbol: item for item in _compile_current_momentum(modules)}
    output: list[MacroRecoveryItem] = []
    frozen_assets = (
        tuple(
            (
                asset.symbol,
                asset.return_1m_pct,
                asset.source_dataset_id,
                asset.as_of,
            )
            for asset in publication.assets
        )
        if isinstance(publication, MacroThesisV2)
        else tuple(
            (
                asset.symbol,
                asset.momentum.return_1m_pct,
                asset.momentum.source_dataset_id,
                asset.momentum.as_of,
            )
            for asset in publication.assets
        )
    )
    for symbol, frozen_value, source_dataset_id, frozen_as_of in frozen_assets:
        current = current_momentum[symbol]
        current_value = current.return_1m_pct
        state = _recovery_state(frozen_value, current_value)
        output.append(
            MacroRecoveryItem(
                scope_kind="asset",
                scope_id=symbol,
                state=state,
                publication=MacroRecoverySide(
                    dataset_id=source_dataset_id,
                    source_id=_source_id(source_dataset_id),
                    value=frozen_value,
                    unit="percent",
                    as_of=frozen_as_of,
                ),
                current=MacroRecoverySide(
                    dataset_id=current.source_dataset_id,
                    source_id=_source_id(current.source_dataset_id),
                    value=current_value,
                    unit="percent",
                    as_of=current.as_of,
                ),
                reason=f"asset_fact_{state}",
            )
        )
    current_facts = {
        str(fact.get("dataset_id") or ""): fact for module in modules for fact in _module_latest_facts(module)
    }
    gap_scopes: list[tuple[Literal["module", "claim", "asset", "dataset"], str, str]] = []
    if isinstance(publication, MacroThesisV2):
        gap_scopes.extend(
            (gap.scope_kind, gap.scope_id, gap.dataset_id) for gap in publication.gaps if gap.dataset_id is not None
        )
    else:
        for gap in publication.gaps:
            if gap.affected_claim_ids:
                gap_scopes.extend(("claim", claim_id, gap.dataset_id) for claim_id in gap.affected_claim_ids)
            else:
                gap_scopes.append(("dataset", gap.dataset_id, gap.dataset_id))
    for scope_kind, scope_id, dataset_id in gap_scopes:
        current_fact = current_facts.get(dataset_id)
        current_value = current_fact.get("value") if current_fact is not None else None
        state = "recovered" if current_value is not None else "still_missing"
        output.append(
            MacroRecoveryItem(
                scope_kind=scope_kind,
                scope_id=scope_id,
                state=state,
                publication=MacroRecoverySide(
                    dataset_id=dataset_id,
                    source_id=_source_id(dataset_id),
                    value=None,
                    unit=None,
                    as_of=None,
                ),
                current=MacroRecoverySide(
                    dataset_id=dataset_id,
                    source_id=_source_id(dataset_id),
                    value=current_value,
                    unit=str(current_fact.get("unit") or "") if current_fact is not None else None,
                    as_of=(
                        str(current_fact.get("reference") or "")
                        if current_fact is not None and current_fact.get("reference")
                        else None
                    ),
                ),
                reason=f"publication_gap_{state}",
            )
        )
    return tuple(output)


def _gate_failure(
    category: PublicationGateCategory,
    code: str,
    *,
    envelope: CandidateDraftEnvelope,
    research_input: MacroResearchInputV1,
    diagnostics: Sequence[str],
) -> PublicationGateFailure:
    recovery = {
        "time_identity": "rebuild_candidate_from_the_bound_frozen_input",
        "evidence_closure": "select_only_exact_evidence_ids_from_the_frozen_input",
        "contract_validity": "return_the_provider_native_macro_thesis_draft_v2_schema",
        "write_safety": "resolve_the_transaction_or_identity_conflict",
    }[category]
    return PublicationGateFailure(
        category=category,
        code=code,
        session_date=research_input.session_date,
        cutoff_ms=research_input.cutoff_ms,
        evidence_pack_id=research_input.evidence_pack_id,
        candidate_hash=envelope.candidate_hash,
        retryable=False,
        recovery_action=recovery,
        diagnostics=diagnostics,
    )


def _compile_selected_conditions(
    draft: MacroThesisDraftV2,
    research_input: MacroResearchInputV1,
) -> tuple[MacroCompiledCondition, ...]:
    candidates = {item.candidate_id: item for item in research_input.condition_candidates}
    output: list[MacroCompiledCondition] = []
    for use in draft.condition_uses:
        candidate = candidates.get(use.candidate_id)
        if candidate is None:
            raise ValueError(f"unknown_condition_candidate:{use.candidate_id}")
        if use.kind not in candidate.allowed_kinds:
            raise ValueError(f"condition_kind_not_allowed:{use.candidate_id}:{use.kind}")
        if use.scope_kind not in candidate.allowed_scopes:
            raise ValueError(f"condition_scope_not_allowed:{use.candidate_id}:{use.scope_kind}")
        if not set(use.evidence_refs).issubset(set(candidate.evidence_refs)):
            raise ValueError(f"condition_evidence_binding_invalid:{use.candidate_id}")
        if (
            isinstance(candidate, MetricConditionCandidate)
            and use.kind == "falsifier"
            and _predicate_matches(candidate.frozen_value, candidate.operator, candidate.threshold)
        ):
            raise ValueError(f"condition_falsifier_already_triggered:{use.candidate_id}")
        if isinstance(candidate, MetricConditionCandidate):
            candidate_type = "metric_condition"
            dataset_id = candidate.dataset_id
            metric = candidate.metric
            unit = candidate.unit
            operator = candidate.operator
            threshold = candidate.threshold
            frozen_value = candidate.frozen_value
            as_of = candidate.as_of
            event_id = None
            scheduled_at_ms = None
        else:
            candidate_type = "event_checkpoint"
            dataset_id = None
            metric = None
            unit = None
            operator = None
            threshold = None
            frozen_value = None
            as_of = None
            event_id = candidate.event_id
            scheduled_at_ms = candidate.scheduled_at_ms
        condition_id = (
            "cond2_"
            + payload_hash(
                {
                    "candidate_id": candidate.candidate_id,
                    "kind": use.kind,
                    "scope_kind": use.scope_kind,
                    "scope_id": use.scope_id,
                    "symbol": use.symbol,
                    "horizon": use.horizon,
                }
            ).removeprefix("sha256:")[:32]
        )
        output.append(
            MacroCompiledCondition(
                condition_id=condition_id,
                candidate_type=candidate_type,
                candidate_id=candidate.candidate_id,
                kind=use.kind,
                scope_kind=use.scope_kind,
                scope_id=use.scope_id,
                symbol=use.symbol,
                horizon=use.horizon,
                rationale=use.rationale,
                evidence_refs=use.evidence_refs,
                module_id=candidate.module_id,
                dataset_id=dataset_id,
                metric=metric,
                unit=unit,
                operator=operator,
                threshold=threshold,
                frozen_value=frozen_value,
                as_of=as_of,
                event_id=event_id,
                scheduled_at_ms=scheduled_at_ms,
            )
        )
    return tuple(output)


def _compile_material_changes(
    draft: MacroThesisDraftV2,
    research_input: MacroResearchInputV1,
) -> tuple[MacroPublishedMaterialChange, ...]:
    candidates = {item.candidate_id: item for module in research_input.modules for item in module.material_changes}
    output: list[MacroPublishedMaterialChange] = []
    for change in draft.material_changes:
        candidate = candidates.get(change.candidate_id)
        if candidate is None:
            raise ValueError(f"unknown_material_change_candidate:{change.candidate_id}")
        output.append(
            MacroPublishedMaterialChange(
                candidate_id=candidate.candidate_id,
                status=change.status,
                statement=change.statement,
                evidence_refs=candidate.evidence_refs,
            )
        )
    return tuple(output)


def _validate_draft_scopes(draft: MacroThesisDraftV2) -> None:
    scope_ids: dict[ConditionScopeKind, set[str]] = {
        "mainline": {"mainline"},
        "alternative": {draft.alternative.alternative_id} if draft.alternative is not None else set(),
        "tension": {item.tension_id for item in draft.tensions},
        "asset": {item.outlook_id for item in draft.asset_outlooks},
    }
    outlooks = {item.outlook_id: item for item in draft.asset_outlooks}
    for use in draft.condition_uses:
        if use.scope_id not in scope_ids[use.scope_kind]:
            raise ValueError(f"condition_scope_id_unknown:{use.scope_kind}:{use.scope_id}")
        if use.scope_kind == "asset":
            outlook = outlooks[use.scope_id]
            if (use.symbol, use.horizon) != (outlook.symbol, outlook.horizon):
                raise ValueError(f"condition_asset_scope_mismatch:{use.scope_id}")
    if draft.mainline.stance == "call" and not draft.mainline.supporting_evidence_refs:
        raise ValueError("macro_thesis_v2_mainline_support_required")
    if draft.mainline.stance == "call" and not any(
        use.scope_kind == "mainline" and use.scope_id == "mainline" and use.kind == "falsifier"
        for use in draft.condition_uses
    ):
        raise ValueError("macro_thesis_v2_mainline_falsifier_required")
    if draft.mainline.stance == "no_call":
        local_outlook_ids = {item.outlook_id for item in draft.asset_outlooks if item.outlook_context == "local"}
        for item in draft.asset_outlooks:
            if item.outlook_id not in local_outlook_ids:
                raise ValueError("macro_thesis_v2_no_call_outlook_must_be_local")
            if not any(use.scope_kind == "asset" and use.scope_id == item.outlook_id for use in draft.condition_uses):
                raise ValueError("macro_thesis_v2_local_outlook_condition_required")


def _publication_gaps(research_input: MacroResearchInputV1) -> tuple[MacroEvidenceGapV2, ...]:
    output: list[MacroEvidenceGapV2] = []
    for capsule in research_input.modules:
        for index, gap in enumerate(capsule.gaps):
            dataset_id = str(gap.get("dataset_id") or "") or None
            scope_id = dataset_id or capsule.module_id
            output.append(
                MacroEvidenceGapV2(
                    gap_id=f"gap2:{capsule.module_id}:{scope_id}:{index}",
                    scope_kind="dataset" if dataset_id else "module",
                    scope_id=scope_id,
                    module_id=capsule.module_id,
                    dataset_id=dataset_id,
                    state=str(gap.get("state") or "unavailable"),
                    reason=str(gap.get("reason") or "required_evidence_missing"),
                )
            )
    return tuple(output)


def _exact_evidence_by_module(
    evidence_pack: MacroEvidencePackV3,
) -> dict[str, list[ExactEvidenceReference]]:
    output: dict[str, list[ExactEvidenceReference]] = {}
    for module in evidence_pack.modules:
        module_id = str(module["module_id"])
        states = {str(item.get("dataset_id") or ""): item for item in _module_dataset_states(module)}
        rows: list[ExactEvidenceReference] = []
        for fact in _module_latest_facts(module):
            evidence_ref = str(fact.get("fact_ref") or "")
            dataset_id = str(fact.get("dataset_id") or "")
            if not evidence_ref or not dataset_id:
                continue
            state = states.get(dataset_id, {})
            authoritative_at_ms = max(
                int(fact.get("observed_at_ms") or 0),
                int(fact.get("published_at_ms") or 0),
                int(fact.get("received_at_ms") or 0),
            )
            if authoritative_at_ms > evidence_pack.cutoff_ms:
                raise ValueError(f"macro_research_input_future_fact:{evidence_ref}")
            spec = DATASET_REGISTRY.get(dataset_id)
            rows.append(
                ExactEvidenceReference(
                    evidence_ref=evidence_ref,
                    module_id=module_id,
                    dataset_id=dataset_id,
                    source_id=spec.source_id if spec is not None else None,
                    source_role=str(state.get("source_role") or "") or None,
                    label=str(state.get("label") or fact.get("label") or dataset_id),
                    value=fact.get("value"),
                    unit=str(fact.get("unit") or (spec.unit if spec is not None else "")),
                    as_of=str(fact.get("reference") or "") or None,
                    authoritative_at_ms=authoritative_at_ms,
                    required=bool(state.get("critical")) or str(state.get("source_role") or "") == "decision_primary",
                    source_url=str(fact.get("source_url") or "") or None,
                )
            )
        output[module_id] = list({item.evidence_ref: item for item in rows}.values())
    return output


def _condition_candidates(
    evidence_pack: MacroEvidencePackV3,
    exact_by_module: Mapping[str, Sequence[ExactEvidenceReference]],
) -> list[ConditionCandidate]:
    histories = _numeric_histories(evidence_pack.modules)
    features = _feature_histories(evidence_pack, histories)
    output: list[ConditionCandidate] = []
    for family in _METRIC_FAMILIES:
        module_id = str(family["module_id"])
        feature_id = str(family["feature_id"])
        series = features.get(feature_id, ())
        if not series:
            continue
        current_date, current_value = series[-1]
        window_start = datetime.fromtimestamp(evidence_pack.cutoff_ms / 1_000, tz=UTC).date() - timedelta(days=1_827)
        five_year_values = [value for point_date, value in series if point_date >= window_start]
        percentile = _percentile_rank(five_year_values, current_value)
        refs = _feature_evidence_refs(
            feature_id,
            exact_by_module.get(module_id, ()),
        )
        if not refs:
            continue
        for raw_predicate, fixed_threshold, suffix in family["predicates"]:
            predicate = str(raw_predicate)
            threshold: float | None
            operator: Literal["gt", "gte", "lt", "lte"]
            quantile_window: Literal["five_years"] | None = None
            if predicate == "lte_q20":
                threshold = _quantile(five_year_values, 0.2) if len(five_year_values) >= 20 else None
                operator = "lte"
                quantile_window = "five_years"
            elif predicate == "gte_q80":
                threshold = _quantile(five_year_values, 0.8) if len(five_year_values) >= 20 else None
                operator = "gte"
                quantile_window = "five_years"
            else:
                if not isinstance(fixed_threshold, int | float):
                    continue
                if predicate not in {"gt", "gte", "lt", "lte"}:
                    continue
                threshold = float(fixed_threshold)
                operator = cast(Literal["gt", "gte", "lt", "lte"], predicate)
            if threshold is None:
                continue
            stable_dataset = feature_id
            output.append(
                MetricConditionCandidate(
                    candidate_id=f"{family['prefix']}:{stable_dataset}:{suffix}",
                    module_id=module_id,
                    dataset_id=feature_id,
                    metric=str(family.get("metric") or "value"),
                    unit=str(family["unit"]),
                    operator=operator,
                    threshold=threshold,
                    frozen_value=current_value,
                    as_of=current_date.isoformat(),
                    historical_percentile_rank=percentile,
                    quantile_window=quantile_window,
                    sample_count=len(five_year_values),
                    allowed_kinds=("confirmation", "weakening", "falsifier"),
                    allowed_scopes=family["allowed_scopes"],
                    meaning=f"{feature_id} {operator} {threshold:g}",
                    evidence_refs=refs,
                )
            )
    output.extend(_release_condition_candidates(evidence_pack, exact_by_module))
    output.extend(_correlation_condition_candidates(evidence_pack, exact_by_module))
    output.extend(_asset_condition_candidates(evidence_pack, exact_by_module, histories))
    return output


def _release_condition_candidates(
    evidence_pack: MacroEvidencePackV3,
    exact_by_module: Mapping[str, Sequence[ExactEvidenceReference]],
) -> list[ConditionCandidate]:
    output: list[ConditionCandidate] = []
    module = next(item for item in evidence_pack.modules if item["module_id"] == "economy_inflation")
    exact = exact_by_module["economy_inflation"]
    for row in _walk_mappings(module):
        dataset_id = str(row.get("dataset_id") or "")
        if not dataset_id:
            continue
        evidence_refs = tuple(item.evidence_ref for item in exact if item.dataset_id == dataset_id)[:2]
        if not evidence_refs:
            continue
        estimate = row.get("estimate_value")
        revised = row.get("revised_prior_value")
        for metric, prefix, required in (
            ("surprise", "economy.release_surprise", estimate),
            ("revision", "economy.release_revision", revised),
        ):
            value = row.get(metric)
            if not isinstance(value, int | float) or not isinstance(required, int | float) or float(value) == 0:
                continue
            as_of = str(row.get("reference_period") or row.get("reference") or evidence_pack.session_date)
            for operator, suffix in (("lt", "lt0"), ("gt", "gt0")):
                output.append(
                    MetricConditionCandidate(
                        candidate_id=f"{prefix}:{dataset_id}:{suffix}",
                        module_id="economy_inflation",
                        dataset_id=dataset_id,
                        metric=metric,
                        unit=str(row.get("unit") or ""),
                        operator=operator,
                        threshold=0.0,
                        frozen_value=float(value),
                        as_of=as_of,
                        historical_percentile_rank=None,
                        sample_count=1,
                        allowed_kinds=("confirmation", "weakening", "falsifier"),
                        allowed_scopes=("mainline", "alternative", "tension"),
                        meaning=f"{dataset_id} explicit {metric} {operator} 0",
                        evidence_refs=evidence_refs,
                    )
                )
    return output


def _correlation_condition_candidates(
    evidence_pack: MacroEvidencePackV3,
    exact_by_module: Mapping[str, Sequence[ExactEvidenceReference]],
) -> list[ConditionCandidate]:
    module = next(item for item in evidence_pack.modules if item["module_id"] == "cross_asset")
    refs = tuple(item.evidence_ref for item in exact_by_module["cross_asset"])
    if not refs:
        return []
    output: list[ConditionCandidate] = []
    for row in _walk_mappings(module.get("correlations")):
        value = row.get("correlation")
        left = str(row.get("left") or "")
        right = str(row.get("right") or "")
        if not left or not right or not isinstance(value, int | float):
            continue
        pair = f"{left.lower()}-{right.lower()}"
        history: list[float] = []
        for item in _walk_mappings(row.get("history")):
            correlation = item.get("correlation")
            if isinstance(correlation, int | float):
                history.append(float(correlation))
        percentile = _percentile_rank(history, float(value))
        if len(history) < 20:
            continue
        for operator, q, suffix in (("lte", 0.2, "leq20"), ("gte", 0.8, "geq80")):
            threshold = _quantile(history, q)
            output.append(
                MetricConditionCandidate(
                    candidate_id=f"cross.corr.tail:{pair}:{suffix}",
                    module_id="cross_asset",
                    dataset_id=f"cross_asset.return_correlations:{pair}",
                    metric="correlation",
                    unit="correlation",
                    operator=operator,
                    threshold=threshold,
                    frozen_value=float(value),
                    as_of=evidence_pack.session_date.isoformat(),
                    historical_percentile_rank=percentile,
                    quantile_window="five_years",
                    sample_count=len(history),
                    allowed_kinds=("confirmation", "weakening", "falsifier"),
                    allowed_scopes=("mainline", "alternative", "tension"),
                    meaning=f"{pair} correlation {operator} {threshold:g}",
                    evidence_refs=refs[:4],
                )
            )
    return output


def _asset_condition_candidates(
    evidence_pack: MacroEvidencePackV3,
    exact_by_module: Mapping[str, Sequence[ExactEvidenceReference]],
    histories: Mapping[str, Sequence[tuple[date, float]]],
) -> list[ConditionCandidate]:
    output: list[ConditionCandidate] = []
    exact = exact_by_module["cross_asset"]
    for momentum in evidence_pack.momentum:
        if momentum.return_1m_pct is None or momentum.source_dataset_id is None:
            continue
        price_history = histories.get(momentum.source_dataset_id, ())
        return_history = [
            (point_date, (value / price_history[index - 21][1] - 1) * 100)
            for index, (point_date, value) in enumerate(price_history)
            if index >= 21 and price_history[index - 21][1] != 0
        ]
        values = [value for _, value in return_history[-1_260:]]
        if len(values) < 20:
            continue
        refs = tuple(item.evidence_ref for item in exact if item.dataset_id == momentum.source_dataset_id)
        if not refs:
            continue
        percentile = _percentile_rank(values, momentum.return_1m_pct)
        for operator, q, suffix in (("lte", 0.2, "leq20"), ("gte", 0.8, "geq80")):
            threshold = _quantile(values, q)
            output.append(
                MetricConditionCandidate(
                    candidate_id=f"cross.return1m.tail:{momentum.symbol.lower()}:{suffix}",
                    module_id="cross_asset",
                    dataset_id=momentum.source_dataset_id,
                    metric="return_1m_pct",
                    unit="percent",
                    operator=operator,
                    threshold=threshold,
                    frozen_value=momentum.return_1m_pct,
                    as_of=momentum.as_of or evidence_pack.session_date.isoformat(),
                    historical_percentile_rank=percentile,
                    quantile_window="five_years",
                    sample_count=len(values),
                    allowed_kinds=("confirmation", "weakening"),
                    allowed_scopes=("asset",),
                    meaning=f"{momentum.symbol} 1m return {operator} {threshold:g}",
                    evidence_refs=refs,
                )
            )
    return output


def _numeric_histories(
    modules: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[tuple[date, float], ...]]:
    output: dict[str, tuple[tuple[date, float], ...]] = {}
    for module in modules:
        for row in _walk_mappings(module):
            dataset_id = str(row.get("dataset_id") or "")
            history = row.get("history")
            if not dataset_id or not isinstance(history, Sequence) or isinstance(history, str | bytes):
                continue
            points: list[tuple[date, float]] = []
            for point in history:
                if not isinstance(point, Mapping):
                    continue
                point_date = _parse_date(point.get("date") or point.get("reference_date"))
                value = _first_numeric(point, ("value", "value_numeric", "close", "normalized_value"))
                if point_date is not None and value is not None:
                    points.append((point_date, value))
            normalized = tuple(sorted(dict(points).items()))
            if len(normalized) > len(output.get(dataset_id, ())):
                output[dataset_id] = normalized
    return output


def _feature_histories(
    evidence_pack: MacroEvidencePackV3,
    histories: Mapping[str, Sequence[tuple[date, float]]],
) -> dict[str, tuple[tuple[date, float], ...]]:
    output: dict[str, tuple[tuple[date, float], ...]] = {}
    for feature_id, spec in CALCULATION_REGISTRY.items():
        inputs = [histories.get(dataset_id, ()) for dataset_id in spec.input_dataset_ids]
        if any(not rows for rows in inputs):
            continue
        if spec.operation == "identity":
            points = tuple(inputs[0])
        elif spec.operation == "difference":
            rows = tuple(inputs[0])
            points = tuple((rows[index][0], rows[index][1] - rows[index - 1][1]) for index in range(1, len(rows)))
        elif spec.operation == "yoy_pct":
            rows = tuple(inputs[0])
            points = tuple(
                (
                    rows[index][0],
                    (rows[index][1] / rows[index - 12][1] - 1) * 100,
                )
                for index in range(12, len(rows))
                if rows[index - 12][1] != 0
            )
        else:
            maps = [dict(rows) for rows in inputs]
            common_dates = set(maps[0])
            for value_map in maps[1:]:
                common_dates &= set(value_map)
            points_list: list[tuple[date, float]] = []
            for point_date in sorted(common_dates):
                point_values = [items[point_date] for items in maps]
                if spec.operation == "difference_x100":
                    value = (point_values[0] - point_values[1]) * 100
                elif spec.operation == "net_liquidity":
                    value = point_values[0] / 1_000 - point_values[1] - point_values[2]
                else:
                    continue
                points_list.append((point_date, value))
            points = tuple(points_list)
        if points:
            output[feature_id] = points
    derived = {
        "liquidity.sofr_iorb": (("fred.sofr", "fred.iorb"), 100.0),
        "credit.ccc_bb_gap": (("fred.bamlh0a3hyc", "fred.bamlh0a1hybb"), 100.0),
    }
    for feature_id, (dataset_ids, scale) in derived.items():
        left, right = (dict(histories.get(item, ())) for item in dataset_ids)
        points = tuple(
            (point_date, (left[point_date] - right[point_date]) * scale)
            for point_date in sorted(set(left) & set(right))
        )
        if points:
            output[feature_id] = points
    vx_rows = _current_vx_curve(evidence_pack.modules)
    if len(vx_rows) >= 2:
        output["volatility.vx_front2_spread"] = (
            (
                _parse_date(vx_rows[0].get("trade_date")) or evidence_pack.session_date,
                float(vx_rows[0]["settlement_price"]) - float(vx_rows[1]["settlement_price"]),
            ),
        )
    return output


def _feature_evidence_refs(
    feature_id: str,
    exact: Sequence[ExactEvidenceReference],
) -> tuple[str, ...]:
    if feature_id in CALCULATION_REGISTRY:
        datasets = set(CALCULATION_REGISTRY[feature_id].input_dataset_ids)
    else:
        datasets = {
            "liquidity.sofr_iorb": {"fred.sofr", "fred.iorb"},
            "credit.ccc_bb_gap": {"fred.bamlh0a3hyc", "fred.bamlh0a1hybb"},
            "volatility.vx_front2_spread": {"cboe.cfe.vx.settlement"},
        }.get(feature_id, set())
    return tuple(item.evidence_ref for item in exact if item.dataset_id in datasets)[:6]


def _module_change_candidates(
    module: Mapping[str, Any],
    exact: Sequence[ExactEvidenceReference],
) -> list[MacroMaterialChangeCandidate]:
    ref_by_dataset: dict[str, list[str]] = defaultdict(list)
    for item in exact:
        ref_by_dataset[item.dataset_id].append(item.evidence_ref)
    if module.get("module_id") == "rates_fed":
        decision = module.get("decision")
        matrix = decision.get("tenor_matrix", ()) if isinstance(decision, Mapping) else ()
        rates_output: list[MacroMaterialChangeCandidate] = []
        for row in matrix:
            if not isinstance(row, Mapping):
                continue
            current = row.get("current")
            if not isinstance(current, Mapping):
                continue
            one_day = next(
                (
                    item
                    for item in row.get("windows", ())
                    if isinstance(item, Mapping)
                    and item.get("window") == "1d"
                    and item.get("state") in {"available", "baseline"}
                ),
                None,
            )
            if one_day is None:
                continue
            dataset_id = str(current.get("dataset_id") or "")
            fact_ids = {str(value) for value in one_day.get("input_fact_ids", ())}
            refs = tuple(value for value in ref_by_dataset.get(dataset_id, ()) if value in fact_ids)[:2]
            if not dataset_id or not refs:
                continue
            tenor = str(row.get("tenor") or "")
            rates_output.append(
                MacroMaterialChangeCandidate(
                    candidate_id=f"{dataset_id}:{tenor}:{current.get('reference_date')}",
                    dataset_id=dataset_id,
                    label=f"{tenor} 美国财政部名义国债收益率",
                    value=(float(current["yield_pct"]) if isinstance(current.get("yield_pct"), int | float) else None),
                    unit="percent",
                    metrics={
                        "change_1d_bp": (
                            float(one_day["change_bp"]) if isinstance(one_day.get("change_bp"), int | float) else None
                        )
                    },
                    evidence_refs=refs,
                )
            )
        return rates_output
    summary = module.get("summary")
    changes = summary.get("top_changes", ()) if isinstance(summary, Mapping) else ()
    output: list[MacroMaterialChangeCandidate] = []
    for index, row in enumerate(changes):
        if not isinstance(row, Mapping):
            continue
        dataset_id = str(row.get("dataset_id") or "")
        refs = tuple(ref_by_dataset.get(dataset_id, ()))[:2]
        if not dataset_id or not refs:
            continue
        metrics = {
            str(key): float(value) if isinstance(value, int | float) else None
            for key, value in dict(row.get("metrics") or {}).items()
        }
        output.append(
            MacroMaterialChangeCandidate(
                candidate_id=f"{dataset_id}:{row.get('as_of') or index}",
                dataset_id=dataset_id,
                label=str(row.get("label") or dataset_id),
                value=float(row["value"]) if isinstance(row.get("value"), int | float) else None,
                unit=str(row.get("unit") or ""),
                metrics=metrics,
                evidence_refs=refs,
            )
        )
    return output


def _module_counter_signals(
    module: Mapping[str, Any],
    local_refs: Sequence[str],
) -> list[MacroCounterSignalCandidate]:
    output: list[MacroCounterSignalCandidate] = []
    for index, item in enumerate(module.get("contradictions") or ()):
        statement = (
            str(item.get("statement") or item.get("message") or "") if isinstance(item, Mapping) else str(item or "")
        ).strip()
        refs = (
            tuple(str(ref) for ref in item.get("evidence_refs") or () if ref in local_refs)
            if isinstance(item, Mapping)
            else ()
        )
        if statement and (refs or local_refs):
            output.append(
                MacroCounterSignalCandidate(
                    candidate_id=f"counter:{module['module_id']}:{index}",
                    statement=statement,
                    evidence_refs=refs or tuple(local_refs[:2]),
                )
            )
    return output


def _required_gaps(module: Mapping[str, Any]) -> list[dict[str, Any]]:
    output = []
    for state in _module_dataset_states(module):
        current = str(state.get("current_health") or "unavailable")
        history = str(state.get("history_depth") or "not_required")
        if bool(state.get("required_for_current")) and current != "current":
            output.append(
                {
                    "dataset_id": str(state.get("dataset_id") or ""),
                    "axis": "current_health",
                    "state": current,
                    "reason": _reason_code(state.get("current_reason"), fallback=current),
                }
            )
        if bool(state.get("required_for_history")) and history != "complete":
            output.append(
                {
                    "dataset_id": str(state.get("dataset_id") or ""),
                    "axis": "history_depth",
                    "state": history,
                    "reason": _reason_code(state.get("history_reason"), fallback=history),
                }
            )
    return output


def _scheduled_catalysts(
    evidence_pack: MacroEvidencePackV3,
    selected_refs: set[str],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    scheduled = evidence_pack.catalyst_pack.get("scheduled_releases", ())
    for index, item in enumerate(scheduled):
        if not isinstance(item, Mapping):
            continue
        scheduled_at_ms = item.get("scheduled_at_ms")
        if not isinstance(scheduled_at_ms, int):
            continue
        module_id = str(item.get("module_id") or "economy_inflation")
        if module_id not in MACRO_MODULE_IDS:
            continue
        event_id = str(item.get("event_id") or item.get("dataset_id") or f"scheduled-{index}")
        refs = tuple(str(ref) for ref in item.get("evidence_refs") or () if ref in selected_refs)
        output.append(
            {
                "event_id": event_id,
                "module_id": module_id,
                "scheduled_at_ms": scheduled_at_ms,
                "observed_at_ms": item.get("observed_at_ms"),
                "meaning": str(item.get("label") or item.get("meaning") or event_id),
                "evidence_refs": refs,
            }
        )
    return sorted(output, key=lambda item: (item["scheduled_at_ms"], item["event_id"]))


def _condition_candidate_sort_key(candidate: ConditionCandidate) -> tuple[int, int, float, str]:
    if isinstance(candidate, EventCheckpointCandidate):
        return (0, 100, float(candidate.scheduled_at_ms), candidate.candidate_id)
    prefix = candidate.candidate_id.split(":", maxsplit=1)[0]
    priority = _REGISTRY_ORDER.get(prefix, 999)
    percentile_distance = (
        abs(candidate.historical_percentile_rank - 0.5) if candidate.historical_percentile_rank is not None else -1
    )
    return (0, priority, -percentile_distance, candidate.candidate_id)


def _evidence_sort_key(item: ExactEvidenceReference) -> tuple[int, str, str]:
    return (0 if item.required else 1, item.dataset_id, item.evidence_ref)


def _round_robin_refs(
    local_ref_order: Mapping[str, Sequence[str]],
    *,
    limit: int,
) -> list[str]:
    output: list[str] = []
    for index in range(max((len(items) for items in local_ref_order.values()), default=0)):
        for module_id in MACRO_MODULE_IDS:
            refs = local_ref_order.get(module_id, ())
            if index < len(refs) and refs[index] not in output:
                output.append(refs[index])
                if len(output) == limit:
                    return output
    return output


def _module_current_state(
    module: Mapping[str, Any],
) -> Literal["current", "degraded", "unavailable"]:
    status = module.get("status")
    current = status.get("current_health") if isinstance(status, Mapping) else None
    state = current.get("state") if isinstance(current, Mapping) else None
    return state if state in {"current", "degraded", "unavailable"} else "unavailable"


def _module_latest_facts(module: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    evidence = module.get("evidence")
    values = evidence.get("latest_facts", ()) if isinstance(evidence, Mapping) else ()
    return tuple(item for item in values if isinstance(item, Mapping))


def _module_dataset_states(module: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    evidence = module.get("evidence")
    values = evidence.get("dataset_states", ()) if isinstance(evidence, Mapping) else ()
    return tuple(item for item in values if isinstance(item, Mapping))


def _bounded_structure(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, Mapping):
        items = sorted(
            (
                (str(key), item)
                for key, item in value.items()
                if key
                not in {
                    "analysis_evidence",
                    "documents",
                    "history",
                    "markdown",
                    "narrative",
                    "raw_data",
                    "raw_data_json",
                    "receipts",
                    "source_url",
                    "timeline",
                }
            ),
            key=lambda item: item[0],
        )[:MAX_STRUCTURE_MAPPING_ITEMS]
        return {key: _bounded_structure(item, depth=depth + 1) for key, item in items}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_bounded_structure(item, depth=depth + 1) for item in list(value)[:2]]
    if isinstance(value, str):
        return value[:1_000]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _bounded_structure_omissions(value: Any, *, depth: int = 0) -> int:
    if depth > 5:
        return 1
    if isinstance(value, Mapping):
        retained = [
            item
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if key
            not in {
                "analysis_evidence",
                "documents",
                "history",
                "markdown",
                "narrative",
                "raw_data",
                "raw_data_json",
                "receipts",
                "source_url",
                "timeline",
            }
        ]
        return max(0, len(retained) - MAX_STRUCTURE_MAPPING_ITEMS) + sum(
            _bounded_structure_omissions(item, depth=depth + 1) for item in retained[:MAX_STRUCTURE_MAPPING_ITEMS]
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        retained = list(value)
        return max(0, len(retained) - 2) + sum(
            _bounded_structure_omissions(item, depth=depth + 1) for item in retained[:2]
        )
    return 0


def _walk_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    output: list[Mapping[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            output.append(item)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(output)


def _percentile_rank(values: Sequence[float], current: float) -> float | None:
    finite = sorted(value for value in values if math.isfinite(value))
    if len(finite) < 20:
        return None
    return sum(value <= current for value in finite) / len(finite)


def _quantile(values: Sequence[float], quantile: float) -> float:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        raise ValueError("macro_condition_quantile_empty")
    index = math.floor((len(finite) - 1) * quantile)
    return float(finite[index])


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _first_numeric(row: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, int | float) and math.isfinite(float(value)):
            return float(value)
    return None


def _reason_code(value: Any, *, fallback: str) -> str:
    if isinstance(value, Mapping):
        return str(value.get("code") or value.get("message") or fallback)
    return str(value or fallback)


def _current_vx_curve(modules: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    module = next((item for item in modules if item.get("module_id") == "volatility"), None)
    if module is None:
        return []
    rows = [
        item
        for item in _walk_mappings(module.get("term_structure"))
        if item.get("contract_expiration_date") and isinstance(item.get("settlement_price"), int | float)
    ]
    return sorted(rows, key=lambda item: str(item["contract_expiration_date"]))


def _current_metric_values(
    modules: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], float]:
    output: dict[tuple[str, str], float] = {}
    for module in modules:
        for row in _walk_mappings(module):
            dataset_id = str(row.get("dataset_id") or row.get("feature_id") or "")
            if not dataset_id:
                continue
            for key, value in row.items():
                if isinstance(value, int | float) and math.isfinite(float(value)):
                    output[(dataset_id, str(key))] = float(value)
            metrics = row.get("metrics")
            if isinstance(metrics, Mapping):
                for key, value in metrics.items():
                    if isinstance(value, int | float) and math.isfinite(float(value)):
                        output[(dataset_id, str(key))] = float(value)
            if isinstance(row.get("value_numeric"), int | float):
                output[(dataset_id, "value")] = float(row["value_numeric"])
            elif isinstance(row.get("value"), int | float):
                output[(dataset_id, "value")] = float(row["value"])
    return output


def _current_event_observations(
    modules: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    output: dict[str, int] = {}
    for module in modules:
        for row in _walk_mappings(module):
            event_id = str(row.get("event_id") or "")
            observed_at_ms = row.get("observed_at_ms") or row.get("published_at_ms")
            if event_id and isinstance(observed_at_ms, int):
                output[event_id] = observed_at_ms
    return output


def _predicate_matches(
    value: float,
    operator: Literal["gt", "gte", "lt", "lte"] | None,
    threshold: float | None,
) -> bool:
    if operator is None or threshold is None:
        return False
    return {
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
    }[operator]


def _aggregate_mainline_validity(
    states: Sequence[str],
) -> Literal["confirming", "weakening", "invalidation_triggered", "unrelated", "insufficient"]:
    if "invalidation_triggered" in states:
        return "invalidation_triggered"
    if "weakening" in states:
        return "weakening"
    if "confirming" in states:
        return "confirming"
    if states and set(states) == {"unrelated"}:
        return "unrelated"
    return "insufficient"


def _declared_horizons(publication: MacroThesisV2) -> tuple[Literal["1w", "1m"], ...]:
    if publication.mainline.horizon == "1w":
        return ("1w",)
    if publication.mainline.horizon == "1m":
        return ("1m",)
    return ("1w", "1m")


def _market_row_time(row: Mapping[str, Any]) -> int:
    value = row.get("observed_at_ms")
    if isinstance(value, int):
        return value
    reference = row.get("reference_date")
    parsed = _parse_date(reference)
    if parsed is None:
        return 0
    return int(datetime.combine(parsed, datetime.min.time(), tzinfo=UTC).timestamp() * 1_000)


def _asset_outcome_result_v2(
    *,
    publication: MacroThesisV2,
    outlook: MacroDraftAssetOutlook,
    rows: Sequence[Mapping[str, Any]],
    expires_at_ms: int,
    evaluated_at_ms: int,
) -> MacroOutcomeAssetResultV2:
    dataset_id = MACRO_ASSET_DATASETS[outlook.symbol]
    matching = [row for row in rows if row.get("dataset_id") == dataset_id]
    before = [row for row in matching if _market_row_time(row) <= publication.cutoff_ms]
    after = [row for row in matching if _market_row_time(row) <= min(expires_at_ms, evaluated_at_ms)]
    if evaluated_at_ms < expires_at_ms:
        status: Literal["pending", "evaluated", "insufficient"] = "pending"
        realized = None
        correct = None
        reason = "outcome_pending"
    elif not before or not after:
        status = "insufficient"
        realized = None
        correct = None
        reason = "outcome_market_fact_missing"
    else:
        start = float(before[-1]["value_numeric"])
        end = float(after[-1]["value_numeric"])
        if start == 0:
            status = "insufficient"
            realized = None
            correct = None
            reason = "outcome_zero_baseline"
        else:
            status = "evaluated"
            realized = round((end / start - 1) * 100, 6)
            correct = {
                "bullish": realized > 0,
                "bearish": realized < 0,
                "neutral": abs(realized) < 0.5,
            }[outlook.direction]
            reason = "outcome_evaluated"
    return MacroOutcomeAssetResultV2(
        symbol=outlook.symbol,
        horizon=outlook.horizon,
        expires_at_ms=expires_at_ms,
        status=status,
        published_direction=outlook.direction,
        realized_return_pct=realized,
        direction_correct=correct,
        reason_code=reason,
    )


def _compile_current_momentum(
    modules: Sequence[Mapping[str, Any]],
) -> tuple[MacroMomentum, ...]:
    changes = {
        str(row.get("dataset_id") or ""): row
        for module in modules
        for row in (
            (module.get("evidence") or {}).get("asset_changes", ())
            if isinstance(module.get("evidence"), Mapping)
            else ()
        )
        if isinstance(row, Mapping)
    }
    output = []
    for symbol in MACRO_THESIS_ASSETS:
        dataset_id = MACRO_ASSET_DATASETS[symbol]
        change = changes.get(dataset_id)
        raw_metrics = change.get("metrics") if isinstance(change, Mapping) else None
        metrics = raw_metrics if isinstance(raw_metrics, Mapping) else {}
        one_week = _first_numeric(metrics, ("return_1w_pct", "change_1w_pct"))
        one_month = _first_numeric(metrics, ("return_1m_pct", "change_1m_pct"))
        output.append(
            MacroMomentum(
                symbol=symbol,
                momentum_1w=_momentum_direction(one_week),
                momentum_1m=_momentum_direction(one_month),
                return_1w_pct=one_week,
                return_1m_pct=one_month,
                source_dataset_id=dataset_id if change is not None else None,
                as_of=str(change.get("as_of") or "") or None if change is not None else None,
            )
        )
    return tuple(output)


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


def _recovery_state(
    publication_value: object,
    current_value: object,
) -> Literal["unchanged", "recovered", "still_missing", "degraded"]:
    if publication_value is None and current_value is None:
        return "still_missing"
    if publication_value is None:
        return "recovered"
    if current_value is None:
        return "degraded"
    return "unchanged"


def _source_id(dataset_id: str | None) -> str | None:
    if dataset_id is None:
        return None
    spec = DATASET_REGISTRY.get(dataset_id)
    return spec.source_id if spec is not None else None


__all__ = [
    "MACRO_CONDITION_FAMILY_PREFIXES",
    "MACRO_LIVE_DELTA_SCHEMA_VERSION_V2",
    "MACRO_OUTCOME_REPLAY_SCHEMA_VERSION_V2",
    "MACRO_RESEARCH_INPUT_SCHEMA_VERSION",
    "MACRO_THESIS_DRAFT_SCHEMA_VERSION",
    "MACRO_THESIS_PROFILE_VERSION",
    "MACRO_THESIS_PROMPT_VERSION",
    "MACRO_THESIS_SCHEMA_VERSION_V2",
    "CandidateDraftEnvelope",
    "ConditionCandidate",
    "EventCheckpointCandidate",
    "MacroCompiledCondition",
    "MacroDraftAssetOutlook",
    "MacroDraftConditionUse",
    "MacroLiveDeltaV2",
    "MacroOutcomeReplayV2",
    "MacroRecoveryItem",
    "MacroResearchInputV1",
    "MacroThesisAgent",
    "MacroThesisDraftV2",
    "MacroThesisV2",
    "MetricConditionCandidate",
    "PublicationGateFailure",
    "compile_candidate_publication_v2",
    "compile_research_input_v1",
    "evaluate_live_delta_v2",
    "evaluate_outcome_replay_v2",
    "project_current_recovery",
]
