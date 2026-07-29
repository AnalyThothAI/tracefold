"""Offline-only frozen-corpus evaluation for the Macro Thin profile.

This module owns no serving state, worker, model transport, or publication
decision.  It selects the fixed corpus, validates signed human adjudication,
and computes the Issue #27 release evidence that a research owner must review.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefold.macro.domain import MACRO_MODULE_IDS, MacroModuleId
from tracefold.macro.thesis import (
    MacroEvidencePackV3,
    compile_evidence_pack_v3,
    payload_hash,
)
from tracefold.macro.thesis_v2 import (
    MacroResearchInputV1,
    MetricConditionCandidate,
    compile_research_input_v1,
)

MACRO_THIN_EVAL_CORPUS = "macro_thin_profile_eval_v1"
MACRO_THIN_EVAL_SCHEMA_VERSION = "macro_thin_profile_eval_manifest_v1"
MACRO_THIN_EVAL_BASELINE_COMMIT = "810b9acc6fc5ea762fff43f1ce7efb8626960a84"
MACRO_THIN_EVAL_REQUIRED_REAL_SESSIONS: Literal[9] = 9


class _ExactEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MacroEvalLabelsV1(_ExactEvalModel):
    allowed_primary_driver_predicate_ids: tuple[str, ...]
    required_counterevidence_refs: tuple[str, ...]
    allowed_material_assets: tuple[str, ...]
    forbidden_factual_claims: tuple[str, ...]
    allowed_condition_ids: tuple[str, ...]


class MacroEvalCaseSeedV1(_ExactEvalModel):
    case_id: str
    case_kind: Literal["module", "mixed", "gap"]
    module_id: MacroModuleId | None
    session_date: date
    cutoff_ms: int = Field(ge=0)
    evidence_pack: MacroEvidencePackV3
    research_input: MacroResearchInputV1
    derived_from: str | None = None
    removed_evidence_ref: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> MacroEvalCaseSeedV1:
        if self.evidence_pack.session_date != self.session_date:
            raise ValueError("macro_eval_seed_pack_session_mismatch")
        if self.research_input.session_date != self.session_date:
            raise ValueError("macro_eval_seed_input_session_mismatch")
        if self.evidence_pack.cutoff_ms != self.cutoff_ms:
            raise ValueError("macro_eval_seed_pack_cutoff_mismatch")
        if self.research_input.cutoff_ms != self.cutoff_ms:
            raise ValueError("macro_eval_seed_input_cutoff_mismatch")
        if self.research_input.evidence_pack_hash != self.evidence_pack.payload_hash:
            raise ValueError("macro_eval_seed_input_pack_mismatch")
        if self.case_kind == "module" and self.module_id is None:
            raise ValueError("macro_eval_module_case_module_required")
        if self.case_kind != "gap" and (self.derived_from is not None or self.removed_evidence_ref is not None):
            raise ValueError("macro_eval_non_gap_derivation_forbidden")
        if self.case_kind == "gap" and (self.derived_from is None or self.removed_evidence_ref is None):
            raise ValueError("macro_eval_gap_derivation_required")
        return self


class MacroEvalCaseV1(_ExactEvalModel):
    case_id: str
    case_kind: Literal["module", "mixed", "gap"]
    module_id: MacroModuleId | None
    session_date: date
    cutoff_ms: int = Field(ge=0)
    evidence_pack_id: str
    evidence_pack_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    research_input_id: str
    research_input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    derived_from: str | None
    removed_evidence_ref: str | None
    labels: MacroEvalLabelsV1


class MacroEvalManifestV1(_ExactEvalModel):
    schema_version: Literal["macro_thin_profile_eval_manifest_v1"] = "macro_thin_profile_eval_manifest_v1"
    corpus_id: Literal["macro_thin_profile_eval_v1"] = "macro_thin_profile_eval_v1"
    baseline_commit: Literal["810b9acc6fc5ea762fff43f1ce7efb8626960a84"] = "810b9acc6fc5ea762fff43f1ce7efb8626960a84"
    production_model: str = Field(min_length=1, max_length=200)
    cases: tuple[MacroEvalCaseV1, ...]
    research_owner: str = Field(min_length=1, max_length=200)
    signed_at_ms: int = Field(ge=0)
    signature: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_corpus_shape(self) -> MacroEvalManifestV1:
        if len(self.cases) != 12:
            raise ValueError("macro_eval_manifest_requires_twelve_cases")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("macro_eval_manifest_duplicate_case")
        if tuple(case.case_kind for case in self.cases) != (
            *("module" for _ in range(6)),
            *("mixed" for _ in range(3)),
            *("gap" for _ in range(3)),
        ):
            raise ValueError("macro_eval_manifest_case_order")
        if tuple(case.module_id for case in self.cases[:6]) != MACRO_MODULE_IDS:
            raise ValueError("macro_eval_manifest_module_order")
        if len({case.session_date for case in self.cases[:9]}) != 9:
            raise ValueError("macro_eval_manifest_real_sessions_not_distinct")
        if tuple(case.derived_from for case in self.cases[9:]) != tuple(case.case_id for case in self.cases[:3]):
            raise ValueError("macro_eval_manifest_gap_derivation_order")
        return self

    @property
    def manifest_hash(self) -> str:
        return payload_hash(self.model_dump(mode="json", exclude={"signature"}))


class MacroEvalMeasurementV1(_ExactEvalModel):
    case_id: str
    factual_errors: int = Field(ge=0)
    citation_closure_errors: int = Field(ge=0)
    condition_errors: int = Field(ge=0)
    causal_sufficient_edges: int = Field(ge=0)
    causal_edges: int = Field(ge=0)
    recalled_counterevidence: int = Field(ge=0)
    required_counterevidence: int = Field(ge=0)
    recalled_material_assets: int = Field(ge=0)
    allowed_material_assets: int = Field(ge=0)
    duplicate_claim_count: int = Field(ge=0)
    body_characters: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    provider_failed: bool
    selected_material_assets: tuple[str, ...]

    @model_validator(mode="after")
    def validate_denominators(self) -> MacroEvalMeasurementV1:
        if self.causal_sufficient_edges > self.causal_edges:
            raise ValueError("macro_eval_causal_numerator_invalid")
        if self.recalled_counterevidence > self.required_counterevidence:
            raise ValueError("macro_eval_counterevidence_numerator_invalid")
        if self.recalled_material_assets > self.allowed_material_assets:
            raise ValueError("macro_eval_material_asset_numerator_invalid")
        if len(self.selected_material_assets) != len(set(self.selected_material_assets)):
            raise ValueError("macro_eval_material_asset_duplicate")
        return self


class MacroEvalProfileRunV1(_ExactEvalModel):
    profile: Literal["baseline", "candidate"]
    repeat: Literal[1, 2]
    model_name: str = Field(min_length=1, max_length=200)
    measurements: tuple[MacroEvalMeasurementV1, ...]
    adjudicator: str = Field(min_length=1, max_length=200)
    signed_at_ms: int = Field(ge=0)
    signature: str = Field(min_length=1, max_length=1_000)

    def require_manifest(self, manifest: MacroEvalManifestV1) -> None:
        if self.model_name != manifest.production_model:
            raise ValueError("macro_eval_model_mismatch")
        if tuple(item.case_id for item in self.measurements) != tuple(item.case_id for item in manifest.cases):
            raise ValueError("macro_eval_measurement_case_order")


class MacroEvalReadinessV1(_ExactEvalModel):
    """Read-only proof that the frozen 12-case corpus can be selected."""

    schema_version: Literal["macro_thin_eval_readiness_v1"] = "macro_thin_eval_readiness_v1"
    corpus_id: Literal["macro_thin_profile_eval_v1"] = "macro_thin_profile_eval_v1"
    baseline_commit: Literal["810b9acc6fc5ea762fff43f1ce7efb8626960a84"] = "810b9acc6fc5ea762fff43f1ce7efb8626960a84"
    state: Literal["ready", "insufficient_real_sessions", "selection_blocked"]
    required_real_sessions: Literal[9] = MACRO_THIN_EVAL_REQUIRED_REAL_SESSIONS
    available_real_sessions: int = Field(ge=0)
    missing_real_sessions: int = Field(ge=0)
    session_dates: tuple[date, ...]
    selected_case_ids: tuple[str, ...]
    reason_code: str | None


class MacroAblationEvidenceV1(_ExactEvalModel):
    schema_version: Literal["macro_thin_profile_ablation_v1"] = "macro_thin_profile_ablation_v1"
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    baseline_causal_sufficiency_worst: float = Field(ge=0, le=1)
    candidate_causal_sufficiency_worst: float = Field(ge=0, le=1)
    baseline_counterevidence_recall_worst: float = Field(ge=0, le=1)
    candidate_counterevidence_recall_worst: float = Field(ge=0, le=1)
    baseline_material_asset_recall_worst: float = Field(ge=0, le=1)
    candidate_material_asset_recall_worst: float = Field(ge=0, le=1)
    baseline_duplicate_claim_worst: int = Field(ge=0)
    candidate_duplicate_claim_worst: int = Field(ge=0)
    candidate_factual_errors: int = Field(ge=0)
    candidate_citation_closure_errors: int = Field(ge=0)
    candidate_condition_errors: int = Field(ge=0)
    baseline_provider_failures: int = Field(ge=0)
    candidate_provider_failures: int = Field(ge=0)
    baseline_latency_ms: tuple[int, int]
    candidate_latency_ms: tuple[int, int]
    baseline_tokens: tuple[int, int]
    candidate_tokens: tuple[int, int]
    baseline_material_selection_consistency: float = Field(ge=0, le=1)
    candidate_material_selection_consistency: float = Field(ge=0, le=1)
    release_vetoes: tuple[str, ...]
    strict_improvements: tuple[str, ...]
    eligible_for_human_cutover: bool


def inspect_macro_eval_readiness(
    evidence_packs: Sequence[MacroEvidencePackV3],
) -> MacroEvalReadinessV1:
    """Compile deterministic inputs and prove whether the exact corpus is selectable."""

    ordered = tuple(sorted(evidence_packs, key=lambda pack: (pack.session_date, pack.evidence_pack_id)))
    session_dates = tuple(pack.session_date for pack in ordered)
    distinct_dates = tuple(dict.fromkeys(session_dates))
    if len(distinct_dates) != len(session_dates):
        return _eval_readiness(
            state="selection_blocked",
            session_dates=distinct_dates,
            reason_code="macro_eval_duplicate_session_pack",
        )
    if len(distinct_dates) < MACRO_THIN_EVAL_REQUIRED_REAL_SESSIONS:
        return _eval_readiness(
            state="insufficient_real_sessions",
            session_dates=distinct_dates,
            reason_code="macro_eval_real_sessions_insufficient",
        )
    try:
        seeds = select_macro_eval_case_seeds(tuple((pack, compile_research_input_v1(pack)) for pack in ordered))
    except ValueError as exc:
        return _eval_readiness(
            state="selection_blocked",
            session_dates=distinct_dates,
            reason_code=str(exc).split(":", maxsplit=1)[0],
        )
    return _eval_readiness(
        state="ready",
        session_dates=distinct_dates,
        selected_case_ids=tuple(seed.case_id for seed in seeds),
    )


def select_macro_eval_case_seeds(
    sessions: Sequence[tuple[MacroEvidencePackV3, MacroResearchInputV1]],
) -> tuple[MacroEvalCaseSeedV1, ...]:
    """Select the exact 6 module + 3 mixed + 3 derived-gap corpus."""

    eligible = [
        (pack, research_input)
        for pack, research_input in sessions
        if pack.payload_hash == research_input.evidence_pack_hash and _ranked_metric_candidates(research_input)
    ]
    by_session = {research_input.session_date: (pack, research_input) for pack, research_input in eligible}
    if len(by_session) != len(eligible):
        raise ValueError("macro_eval_duplicate_session_input")

    used: set[date] = set()
    selected: list[MacroEvalCaseSeedV1] = []
    for module_id in MACRO_MODULE_IDS:
        candidates = [
            (pack, research_input)
            for pack, research_input in eligible
            if research_input.session_date not in used and _module_extremeness(research_input, module_id) is not None
        ]
        candidates.sort(
            key=lambda item: (
                -float(_module_extremeness(item[1], module_id) or 0),
                -item[1].session_date.toordinal(),
                item[1].input_id,
            )
        )
        if not candidates:
            raise ValueError(f"macro_eval_module_session_missing:{module_id}")
        pack, research_input = candidates[0]
        used.add(research_input.session_date)
        selected.append(
            _seed(
                pack=pack,
                research_input=research_input,
                case_id=f"module:{module_id}:{research_input.session_date.isoformat()}",
                case_kind="module",
                module_id=module_id,
            )
        )

    mixed = [
        (pack, research_input)
        for pack, research_input in eligible
        if research_input.session_date not in used
        and _tail_counts(research_input)[0] >= 2
        and _tail_counts(research_input)[1] >= 2
    ]
    mixed.sort(
        key=lambda item: (
            -sum(_tail_counts(item[1])),
            -item[1].session_date.toordinal(),
            item[1].input_id,
        )
    )
    if len(mixed) < 3:
        raise ValueError("macro_eval_mixed_sessions_insufficient")
    for index, (pack, research_input) in enumerate(mixed[:3], start=1):
        used.add(research_input.session_date)
        selected.append(
            _seed(
                pack=pack,
                research_input=research_input,
                case_id=f"mixed:{index}:{research_input.session_date.isoformat()}",
                case_kind="mixed",
                module_id=None,
            )
        )

    for source in selected[:3]:
        gap_pack, removed_ref = derive_macro_gap_pack(
            source.evidence_pack,
            module_id=source.module_id,
        )
        gap_input = compile_research_input_v1(gap_pack)
        selected.append(
            _seed(
                pack=gap_pack,
                research_input=gap_input,
                case_id=f"gap:{source.module_id}:{source.session_date.isoformat()}",
                case_kind="gap",
                module_id=source.module_id,
                derived_from=source.case_id,
                removed_evidence_ref=removed_ref,
            )
        )
    return tuple(selected)


def _eval_readiness(
    *,
    state: Literal["ready", "insufficient_real_sessions", "selection_blocked"],
    session_dates: tuple[date, ...],
    selected_case_ids: tuple[str, ...] = (),
    reason_code: str | None = None,
) -> MacroEvalReadinessV1:
    return MacroEvalReadinessV1(
        state=state,
        available_real_sessions=len(session_dates),
        missing_real_sessions=max(
            0,
            MACRO_THIN_EVAL_REQUIRED_REAL_SESSIONS - len(session_dates),
        ),
        session_dates=session_dates,
        selected_case_ids=selected_case_ids,
        reason_code=reason_code,
    )


def freeze_macro_eval_manifest(
    *,
    seeds: Sequence[MacroEvalCaseSeedV1],
    labels: Mapping[str, MacroEvalLabelsV1],
    production_model: str,
    research_owner: str,
    signed_at_ms: int,
    signature: str,
) -> MacroEvalManifestV1:
    if len(seeds) != 12:
        raise ValueError("macro_eval_manifest_requires_twelve_seeds")
    if set(labels) != {seed.case_id for seed in seeds}:
        raise ValueError("macro_eval_labels_not_closed")
    return MacroEvalManifestV1(
        production_model=production_model,
        cases=tuple(
            MacroEvalCaseV1(
                case_id=seed.case_id,
                case_kind=seed.case_kind,
                module_id=seed.module_id,
                session_date=seed.session_date,
                cutoff_ms=seed.cutoff_ms,
                evidence_pack_id=seed.evidence_pack.evidence_pack_id,
                evidence_pack_hash=seed.evidence_pack.payload_hash,
                research_input_id=seed.research_input.input_id,
                research_input_hash=seed.research_input.input_hash,
                derived_from=seed.derived_from,
                removed_evidence_ref=seed.removed_evidence_ref,
                labels=labels[seed.case_id],
            )
            for seed in seeds
        ),
        research_owner=research_owner,
        signed_at_ms=signed_at_ms,
        signature=signature,
    )


def compare_macro_eval_profiles(
    *,
    manifest: MacroEvalManifestV1,
    runs: Sequence[MacroEvalProfileRunV1],
) -> MacroAblationEvidenceV1:
    if {(run.profile, run.repeat) for run in runs} != {
        ("baseline", 1),
        ("baseline", 2),
        ("candidate", 1),
        ("candidate", 2),
    }:
        raise ValueError("macro_eval_requires_two_runs_per_profile")
    for run in runs:
        run.require_manifest(manifest)
    baseline = sorted(
        (run for run in runs if run.profile == "baseline"),
        key=lambda item: item.repeat,
    )
    candidate = sorted(
        (run for run in runs if run.profile == "candidate"),
        key=lambda item: item.repeat,
    )

    baseline_causal = _worst_ratio(baseline, "causal_sufficient_edges", "causal_edges")
    candidate_causal = _worst_ratio(candidate, "causal_sufficient_edges", "causal_edges")
    baseline_counter = _worst_ratio(baseline, "recalled_counterevidence", "required_counterevidence")
    candidate_counter = _worst_ratio(candidate, "recalled_counterevidence", "required_counterevidence")
    baseline_assets = _worst_ratio(baseline, "recalled_material_assets", "allowed_material_assets")
    candidate_assets = _worst_ratio(candidate, "recalled_material_assets", "allowed_material_assets")
    baseline_duplicates = max(item.duplicate_claim_count for run in baseline for item in run.measurements)
    candidate_duplicates = max(item.duplicate_claim_count for run in candidate for item in run.measurements)
    candidate_factual = sum(item.factual_errors for run in candidate for item in run.measurements)
    candidate_citations = sum(item.citation_closure_errors for run in candidate for item in run.measurements)
    candidate_conditions = sum(item.condition_errors for run in candidate for item in run.measurements)

    vetoes: list[str] = []
    if candidate_factual:
        vetoes.append("candidate_factual_error")
    if candidate_citations:
        vetoes.append("candidate_citation_closure_error")
    if candidate_conditions:
        vetoes.append("candidate_condition_error")
    if candidate_causal < baseline_causal:
        vetoes.append("causal_sufficiency_regressed")
    if candidate_counter < baseline_counter:
        vetoes.append("counterevidence_recall_regressed")
    if candidate_assets < baseline_assets:
        vetoes.append("material_asset_recall_regressed")

    improvements: list[str] = []
    if candidate_causal > baseline_causal:
        improvements.append("causal_sufficiency")
    if candidate_counter > baseline_counter:
        improvements.append("counterevidence_recall")
    if candidate_duplicates < baseline_duplicates:
        improvements.append("duplicate_claim_count")
    if not improvements:
        vetoes.append("no_required_strict_improvement")

    return MacroAblationEvidenceV1(
        manifest_hash=manifest.manifest_hash,
        baseline_causal_sufficiency_worst=baseline_causal,
        candidate_causal_sufficiency_worst=candidate_causal,
        baseline_counterevidence_recall_worst=baseline_counter,
        candidate_counterevidence_recall_worst=candidate_counter,
        baseline_material_asset_recall_worst=baseline_assets,
        candidate_material_asset_recall_worst=candidate_assets,
        baseline_duplicate_claim_worst=baseline_duplicates,
        candidate_duplicate_claim_worst=candidate_duplicates,
        candidate_factual_errors=candidate_factual,
        candidate_citation_closure_errors=candidate_citations,
        candidate_condition_errors=candidate_conditions,
        baseline_provider_failures=_provider_failures(baseline),
        candidate_provider_failures=_provider_failures(candidate),
        baseline_latency_ms=_run_totals(baseline, "latency_ms"),
        candidate_latency_ms=_run_totals(candidate, "latency_ms"),
        baseline_tokens=_token_totals(baseline),
        candidate_tokens=_token_totals(candidate),
        baseline_material_selection_consistency=_selection_consistency(baseline),
        candidate_material_selection_consistency=_selection_consistency(candidate),
        release_vetoes=tuple(vetoes),
        strict_improvements=tuple(improvements),
        eligible_for_human_cutover=not vetoes,
    )


def derive_macro_gap_pack(
    pack: MacroEvidencePackV3,
    *,
    module_id: MacroModuleId | None,
) -> tuple[MacroEvidencePackV3, str]:
    if module_id is None:
        raise ValueError("macro_eval_gap_module_required")
    research_input = compile_research_input_v1(pack)
    removable = next(
        (item for item in research_input.exact_evidence if item.module_id == module_id and item.required),
        None,
    )
    if removable is None:
        raise ValueError(f"macro_eval_required_fact_missing:{module_id}")
    modules = deepcopy(pack.modules)
    removed = False
    for module in modules:
        if module.get("module_id") != module_id:
            continue
        evidence = module.get("evidence")
        if not isinstance(evidence, dict):
            continue
        facts = list(evidence.get("latest_facts") or ())
        filtered = [fact for fact in facts if str(fact.get("fact_ref") or "") != removable.evidence_ref]
        removed = len(filtered) != len(facts)
        evidence["latest_facts"] = filtered
    if not removed:
        raise ValueError("macro_eval_required_fact_not_in_pack")
    return (
        compile_evidence_pack_v3(
            session_date=pack.session_date,
            cutoff_ms=pack.cutoff_ms,
            sealed_at_ms=pack.sealed_at_ms,
            modules=modules,
            prior_publication=pack.prior_publication,
        ),
        removable.evidence_ref,
    )


def _seed(
    *,
    pack: MacroEvidencePackV3,
    research_input: MacroResearchInputV1,
    case_id: str,
    case_kind: Literal["module", "mixed", "gap"],
    module_id: MacroModuleId | None,
    derived_from: str | None = None,
    removed_evidence_ref: str | None = None,
) -> MacroEvalCaseSeedV1:
    return MacroEvalCaseSeedV1(
        case_id=case_id,
        case_kind=case_kind,
        module_id=module_id,
        session_date=research_input.session_date,
        cutoff_ms=research_input.cutoff_ms,
        evidence_pack=pack,
        research_input=research_input,
        derived_from=derived_from,
        removed_evidence_ref=removed_evidence_ref,
    )


def _ranked_metric_candidates(
    research_input: MacroResearchInputV1,
) -> tuple[MetricConditionCandidate, ...]:
    return tuple(
        item
        for item in research_input.condition_candidates
        if isinstance(item, MetricConditionCandidate) and item.historical_percentile_rank is not None
    )


def _module_extremeness(
    research_input: MacroResearchInputV1,
    module_id: MacroModuleId,
) -> float | None:
    values = []
    for item in _ranked_metric_candidates(research_input):
        rank = item.historical_percentile_rank
        if item.module_id == module_id and rank is not None:
            values.append(abs(float(rank) - 0.5))
    return max(values) if values else None


def _tail_counts(research_input: MacroResearchInputV1) -> tuple[int, int]:
    ranks = [
        float(rank)
        for item in _ranked_metric_candidates(research_input)
        if (rank := item.historical_percentile_rank) is not None
    ]
    return (
        sum(value >= 0.8 for value in ranks),
        sum(value <= 0.2 for value in ranks),
    )


def _worst_ratio(
    runs: Sequence[MacroEvalProfileRunV1],
    numerator: str,
    denominator: str,
) -> float:
    ratios = [
        (
            float(getattr(item, numerator)) / float(getattr(item, denominator))
            if int(getattr(item, denominator))
            else 1.0
        )
        for run in runs
        for item in run.measurements
    ]
    return round(min(ratios), 6)


def _provider_failures(runs: Sequence[MacroEvalProfileRunV1]) -> int:
    return sum(item.provider_failed for run in runs for item in run.measurements)


def _run_totals(
    runs: Sequence[MacroEvalProfileRunV1],
    field: str,
) -> tuple[int, int]:
    return tuple(sum(int(getattr(item, field)) for item in run.measurements) for run in runs)  # type: ignore[return-value]


def _token_totals(runs: Sequence[MacroEvalProfileRunV1]) -> tuple[int, int]:
    return tuple(sum(item.input_tokens + item.output_tokens for item in run.measurements) for run in runs)  # type: ignore[return-value]


def _selection_consistency(runs: Sequence[MacroEvalProfileRunV1]) -> float:
    first, second = runs
    matching = sum(
        set(left.selected_material_assets) == set(right.selected_material_assets)
        for left, right in zip(
            first.measurements,
            second.measurements,
            strict=True,
        )
    )
    return round(matching / len(first.measurements), 6)


__all__ = [
    "MACRO_THIN_EVAL_BASELINE_COMMIT",
    "MACRO_THIN_EVAL_CORPUS",
    "MACRO_THIN_EVAL_SCHEMA_VERSION",
    "MacroAblationEvidenceV1",
    "MacroEvalCaseSeedV1",
    "MacroEvalCaseV1",
    "MacroEvalLabelsV1",
    "MacroEvalManifestV1",
    "MacroEvalMeasurementV1",
    "MacroEvalProfileRunV1",
    "compare_macro_eval_profiles",
    "derive_macro_gap_pack",
    "freeze_macro_eval_manifest",
    "select_macro_eval_case_seeds",
]
