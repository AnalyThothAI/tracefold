from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import Field

from tracefold.macro.assets import MACRO_ASSET_DATASETS, MACRO_THESIS_ASSETS
from tracefold.macro.calculations import natural_change_calculation
from tracefold.macro.domain import MACRO_MODULE_IDS, MACRO_MODULE_LABELS, MacroModuleId
from tracefold.macro.reasons import MacroReason
from tracefold.macro.registry import DATASET_REGISTRY
from tracefold.macro.thesis import (
    ExactMacroModel,
    MacroCondition,
    MacroEvidencePackV3,
    MacroLiveDeltaItem,
    MacroLiveDeltaV1,
    MacroOutcomeReplayV1,
    MacroThesisV1,
)

MacroLiveDeltaStatus = Literal[
    "confirming",
    "weakening",
    "invalidation_triggered",
    "unrelated",
    "insufficient",
]
MacroLiveDeltaScopeKind = Literal["mainline", "alternative", "tension", "asset"]


class MacroLiveDeltaItemRead(ExactMacroModel):
    binding_type: Literal["claim", "falsifier", "checkpoint"]
    binding_id: str
    condition_id: str
    status: MacroLiveDeltaStatus
    dataset_id: str
    dataset_label: str
    metric_name: str
    unit: str | None
    observed_value: float | None
    observed_at_ms: int | None
    observation_cutoff_ms: int = Field(ge=0)
    operator: Literal["gt", "gte", "lt", "lte", "abs_gte"]
    threshold: float
    rationale: str
    source_reason_code: str
    reason: MacroReason


class MacroLiveDeltaScope(ExactMacroModel):
    scope: MacroLiveDeltaScopeKind
    scope_id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=3_000)
    status: MacroLiveDeltaStatus
    matched_binding_ids: tuple[str, ...] = ()
    items: tuple[MacroLiveDeltaItemRead, ...] = ()


class MacroLiveDeltaRead(ExactMacroModel):
    schema_version: Literal["macro_live_delta_read_v1"] = "macro_live_delta_read_v1"
    source_schema_version: Literal["macro_live_delta_v1"] = "macro_live_delta_v1"
    live_delta_id: str
    publication_id: str
    evaluated_at_ms: int = Field(ge=0)
    module_fact_cutoff_ms: int = Field(ge=0)
    mainline_validity: MacroLiveDeltaStatus
    matched_claim_ids: tuple[str, ...] = ()
    matched_falsifier_ids: tuple[str, ...] = ()
    matched_checkpoint_ids: tuple[str, ...] = ()
    scopes: tuple[MacroLiveDeltaScope, ...] = ()
    reason_codes: tuple[str, ...] = ()
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MacroOutcomeAssetResultRead(ExactMacroModel):
    symbol: str
    horizon: Literal["1w", "1m"]
    expires_at_ms: int = Field(ge=0)
    status: Literal["pending", "evaluated", "insufficient"]
    published_direction: Literal["bullish", "bearish", "neutral", "no_call"]
    realized_return_pct: float | None
    direction_correct: bool | None
    source_reason_code: str
    reason: MacroReason


class MacroOutcomeHorizonRead(ExactMacroModel):
    horizon: Literal["1d", "1w", "1m"]
    expires_at_ms: int = Field(ge=0)
    status: Literal["pending", "evaluated", "insufficient"]
    benchmark_symbol: str
    realized_return_pct: float | None
    direction_correct: bool | None
    source_reason_code: str
    reason: MacroReason
    asset_results: tuple[MacroOutcomeAssetResultRead, ...]


class MacroOutcomeReplayRead(ExactMacroModel):
    schema_version: Literal["macro_outcome_replay_read_v1"] = "macro_outcome_replay_read_v1"
    source_schema_version: Literal["macro_outcome_replay_v1"] = "macro_outcome_replay_v1"
    replay_id: str
    publication_id: str
    evaluated_at_ms: int = Field(ge=0)
    horizons: tuple[MacroOutcomeHorizonRead, ...]
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MacroReaderNarrative(ExactMacroModel):
    text: str = Field(min_length=1, max_length=8_000)
    origin: Literal["publication", "structured_fallback"]


class MacroAssetHorizonPresentation(ExactMacroModel):
    horizon: Literal["1w", "1m"]
    momentum_state: Literal["up", "down", "flat", "insufficient"]
    momentum_value: float | None
    outlook_direction: Literal["bullish", "bearish", "neutral", "no_call"]
    reader_rationale: MacroReaderNarrative
    confidence: Literal["low", "medium", "high"]
    supporting_evidence_refs: tuple[str, ...] = ()
    conflicting_evidence_refs: tuple[str, ...] = ()
    confirmation_triggers: tuple[MacroCondition, ...] = ()
    falsifiers: tuple[MacroCondition, ...] = ()
    checkpoints: tuple[MacroCondition, ...] = ()
    reason: MacroReason | None


class MacroAssetPresentation(ExactMacroModel):
    symbol: str
    order: int = Field(ge=1, le=len(MACRO_THESIS_ASSETS))
    group: Literal["actionable", "watch", "evidence_gap"]
    source_dataset_id: str
    as_of: str | None
    claim_ids: tuple[str, ...] = ()
    horizons: tuple[MacroAssetHorizonPresentation, MacroAssetHorizonPresentation]


class MacroCausalEdgePresentation(ExactMacroModel):
    source_label: str
    mechanism: MacroReaderNarrative
    target_label: str


class MacroClaimModuleEvidencePresentation(ExactMacroModel):
    module_id: MacroModuleId
    role: Literal["driver", "confirming", "contradicting", "uncertain"]
    reader_narrative: MacroReaderNarrative
    supporting_evidence_refs: tuple[str, ...] = ()
    conflicting_evidence_refs: tuple[str, ...] = ()


class MacroClaimAssetImplication(ExactMacroModel):
    symbol: str
    horizon: Literal["1w", "1m"]
    direction: Literal["bullish", "bearish", "neutral", "no_call"]
    reader_rationale: MacroReaderNarrative
    confidence: Literal["low", "medium", "high"]
    evidence_links: tuple[str, ...] = ()
    confirmation_triggers: tuple[MacroCondition, ...] = ()
    falsifiers: tuple[MacroCondition, ...] = ()
    checkpoints: tuple[MacroCondition, ...] = ()


class MacroClaimPresentation(ExactMacroModel):
    claim_id: str
    statement: MacroReaderNarrative
    causal_edges: tuple[MacroCausalEdgePresentation, ...]
    module_evidence: tuple[MacroClaimModuleEvidencePresentation, ...]
    supporting_evidence_refs: tuple[str, ...]
    conflicting_evidence_refs: tuple[str, ...] = ()
    conditions: tuple[MacroCondition, ...] = ()
    falsifiers: tuple[MacroCondition, ...] = ()
    checkpoints: tuple[MacroCondition, ...] = ()
    asset_implications: tuple[MacroClaimAssetImplication, ...] = ()


class MacroAlternativePresentation(ExactMacroModel):
    title: MacroReaderNarrative
    thesis: MacroReaderNarrative
    causal_edges: tuple[MacroCausalEdgePresentation, ...]
    supporting_evidence_refs: tuple[str, ...]
    conflicting_evidence_refs: tuple[str, ...] = ()
    trigger_conditions: tuple[MacroCondition, ...]


class MacroMainlinePresentation(ExactMacroModel):
    title: MacroReaderNarrative
    thesis: MacroReaderNarrative


class MacroConditionAnnotation(ExactMacroModel):
    kind: Literal["confirmation", "falsifier", "checkpoint"]
    binding_type: Literal["claim", "mainline", "alternative", "tension", "asset"]
    binding_id: str
    condition: MacroCondition


class MacroPublicationModuleQualityRead(ExactMacroModel):
    module_id: MacroModuleId
    label: str
    latest_fact_at_ms: int | None
    coverage_state: Literal["complete", "partial"]
    expected_capabilities: int
    available_capabilities: int
    current_health_state: Literal["current", "degraded", "unavailable"]
    current_datasets: int
    tracked_datasets: int
    history_depth_state: Literal["complete", "partial", "insufficient", "not_required"]
    complete_history_datasets: int
    tracked_history_datasets: int
    backfill_state: (
        Literal[
            "not_required",
            "complete",
            "queued",
            "running",
            "paused",
            "retry_wait",
            "failed",
        ]
        | None
    )
    backfill_worker_enabled: bool | None
    reasons: tuple[MacroReason, ...] = ()


class MacroPublicationDataQualityRead(ExactMacroModel):
    coverage_state: Literal["complete", "partial"]
    current_health_state: Literal["current", "degraded", "unavailable"]
    history_depth_state: Literal["complete", "partial", "insufficient", "not_required"]
    coverage_gap_count: int
    current_health_gap_count: int
    history_gap_count: int
    modules: tuple[MacroPublicationModuleQualityRead, ...]


class MacroPublicationSourceLineageRead(ExactMacroModel):
    module_id: MacroModuleId
    module_label: str
    dataset_id: str
    label: str
    source_role: str | None
    reference: str | None
    value: str | float | None
    unit: str | None
    observed_at_ms: int | None
    published_at_ms: int | None
    received_at_ms: int | None
    source_url: str | None
    current_health: Literal["current", "degraded", "unavailable"] | None
    history_depth: Literal["complete", "partial", "insufficient", "not_required"] | None
    current_reason: MacroReason | None
    history_reason: MacroReason | None


class MacroReconciliationObservationRead(ExactMacroModel):
    dataset_id: str
    source_role: str
    reference: str | None
    value: str | float | None
    unit: str
    fact_ref: str | None


class MacroReconciliationComparisonRead(ExactMacroModel):
    left_dataset_id: str
    right_dataset_id: str
    left_fact_ref: str | None
    right_fact_ref: str | None
    aligned_reference: str | None
    left_reference: str | None
    right_reference: str | None
    left_value: float
    right_value: float
    difference: float
    tolerance: float
    unit: str
    status: Literal["reference_mismatch", "within_tolerance", "divergent"]


class MacroReconciliationReceiptRead(ExactMacroModel):
    module_id: MacroModuleId
    module_label: str
    concept_id: str
    state: Literal["complete", "partial", "insufficient"]
    selection_policy: str
    selected_dataset_id: str | None
    identity_policy: str
    observations: tuple[MacroReconciliationObservationRead, ...]
    comparisons: tuple[MacroReconciliationComparisonRead, ...]


class MacroPublicationAppendixRead(ExactMacroModel):
    schema_version: Literal["macro_publication_appendix_v1"] = "macro_publication_appendix_v1"
    publication_id: str
    evidence_pack_id: str
    session_date: str
    cutoff_ms: int = Field(ge=0)
    sealed_at_ms: int = Field(ge=0)
    source_max_received_at_ms: int = Field(ge=0)
    data_quality: MacroPublicationDataQualityRead
    source_lineage: tuple[MacroPublicationSourceLineageRead, ...]
    reconciliation_receipts: tuple[MacroReconciliationReceiptRead, ...]


def project_publication_appendix(
    *,
    publication: Mapping[str, Any],
    evidence_pack: Mapping[str, Any],
) -> MacroPublicationAppendixRead:
    thesis = MacroThesisV1.model_validate(publication)
    pack = MacroEvidencePackV3.model_validate(evidence_pack)
    if thesis.evidence_pack_id != pack.evidence_pack_id:
        raise ValueError("macro_publication_appendix_evidence_pack_id_mismatch")
    if thesis.evidence_pack_hash != pack.payload_hash:
        raise ValueError("macro_publication_appendix_evidence_pack_hash_mismatch")
    if thesis.session_date != pack.session_date or thesis.cutoff_ms != pack.cutoff_ms:
        raise ValueError("macro_publication_appendix_session_boundary_mismatch")

    module_quality = tuple(_publication_module_quality(module) for module in pack.modules)
    return MacroPublicationAppendixRead(
        publication_id=thesis.publication_id,
        evidence_pack_id=pack.evidence_pack_id,
        session_date=pack.session_date.isoformat(),
        cutoff_ms=pack.cutoff_ms,
        sealed_at_ms=pack.sealed_at_ms,
        source_max_received_at_ms=pack.source_max_received_at_ms,
        data_quality=_publication_data_quality(module_quality),
        source_lineage=tuple(item for module in pack.modules for item in _publication_source_lineage(module)),
        reconciliation_receipts=tuple(
            item for module in pack.modules for item in _publication_reconciliation_receipts(module)
        ),
    )


def _publication_module_quality(module: Mapping[str, Any]) -> MacroPublicationModuleQualityRead:
    module_id = str(module.get("module_id") or "")
    if module_id not in MACRO_MODULE_IDS:
        raise ValueError("macro_publication_appendix_module_id_invalid")
    status = _required_mapping(module.get("status"), "macro_publication_appendix_status_missing")
    coverage = _required_mapping(
        status.get("coverage"),
        "macro_publication_appendix_coverage_missing",
    )
    current_health = _required_mapping(
        status.get("current_health"),
        "macro_publication_appendix_current_health_missing",
    )
    history_depth = _required_mapping(
        status.get("history_depth"),
        "macro_publication_appendix_history_depth_missing",
    )
    evidence = _required_mapping(
        module.get("evidence"),
        "macro_publication_appendix_evidence_missing",
    )
    dataset_states = tuple(item for item in evidence.get("dataset_states", ()) if isinstance(item, Mapping))
    capabilities = tuple(item for item in coverage.get("capabilities", ()) if isinstance(item, Mapping))

    coverage_state_value = str(coverage.get("state") or "")
    if coverage_state_value not in {"complete", "partial"}:
        raise ValueError("macro_publication_appendix_coverage_state_invalid")
    current_health_state_value = str(current_health.get("state") or "")
    if current_health_state_value not in {"current", "degraded", "unavailable"}:
        raise ValueError("macro_publication_appendix_current_health_state_invalid")
    history_depth_state_value = str(history_depth.get("state") or "")
    if history_depth_state_value not in {"complete", "partial", "insufficient", "not_required"}:
        raise ValueError("macro_publication_appendix_history_depth_state_invalid")

    backfill = status.get("backfill_execution")
    backfill_state: str | None = None
    backfill_worker_enabled: bool | None = None
    if isinstance(backfill, Mapping):
        backfill_state = str(backfill.get("state") or "")
        if backfill_state not in {
            "not_required",
            "complete",
            "queued",
            "running",
            "paused",
            "retry_wait",
            "failed",
        }:
            raise ValueError("macro_publication_appendix_backfill_state_invalid")
        worker_enabled = backfill.get("worker_enabled")
        backfill_worker_enabled = bool(worker_enabled) if isinstance(worker_enabled, bool) else None

    reasons = _publication_module_reasons(
        coverage=coverage,
        dataset_states=dataset_states,
        backfill=backfill if isinstance(backfill, Mapping) else None,
    )
    expected_capabilities = _int_or_default(
        coverage.get("expected_capabilities"),
        len(capabilities),
    )
    available_capabilities = _int_or_default(
        coverage.get("available_capabilities"),
        sum(item.get("state") == "available" for item in capabilities),
    )
    tracked_datasets = _int_or_default(
        current_health.get("tracked_datasets"),
        len(dataset_states),
    )
    current_datasets = _int_or_default(
        current_health.get("current_datasets"),
        sum(item.get("current_health") == "current" for item in dataset_states),
    )
    tracked_history_datasets = _int_or_default(
        history_depth.get("tracked_datasets"),
        len(dataset_states),
    )
    complete_history_datasets = _int_or_default(
        history_depth.get("complete_datasets"),
        sum(item.get("history_depth") in {"complete", "not_required"} for item in dataset_states),
    )
    return MacroPublicationModuleQualityRead(
        module_id=cast(MacroModuleId, module_id),
        label=str(module.get("label") or MACRO_MODULE_LABELS[cast(MacroModuleId, module_id)]),
        latest_fact_at_ms=(int(module["latest_fact_at_ms"]) if module.get("latest_fact_at_ms") is not None else None),
        coverage_state=cast(Literal["complete", "partial"], coverage_state_value),
        expected_capabilities=expected_capabilities,
        available_capabilities=available_capabilities,
        current_health_state=cast(
            Literal["current", "degraded", "unavailable"],
            current_health_state_value,
        ),
        current_datasets=current_datasets,
        tracked_datasets=tracked_datasets,
        history_depth_state=cast(
            Literal["complete", "partial", "insufficient", "not_required"],
            history_depth_state_value,
        ),
        complete_history_datasets=complete_history_datasets,
        tracked_history_datasets=tracked_history_datasets,
        backfill_state=cast(
            Literal[
                "not_required",
                "complete",
                "queued",
                "running",
                "paused",
                "retry_wait",
                "failed",
            ]
            | None,
            backfill_state,
        ),
        backfill_worker_enabled=backfill_worker_enabled,
        reasons=reasons,
    )


def _publication_data_quality(
    modules: tuple[MacroPublicationModuleQualityRead, ...],
) -> MacroPublicationDataQualityRead:
    if tuple(module.module_id for module in modules) != MACRO_MODULE_IDS:
        raise ValueError("macro_publication_appendix_module_order")
    coverage_state: Literal["complete", "partial"] = (
        "complete" if all(module.coverage_state == "complete" for module in modules) else "partial"
    )
    if all(module.current_health_state == "current" for module in modules):
        current_health_state: Literal["current", "degraded", "unavailable"] = "current"
    elif all(module.current_health_state == "unavailable" for module in modules):
        current_health_state = "unavailable"
    else:
        current_health_state = "degraded"
    tracked_history = tuple(module for module in modules if module.history_depth_state != "not_required")
    if not tracked_history:
        history_depth_state: Literal[
            "complete",
            "partial",
            "insufficient",
            "not_required",
        ] = "not_required"
    elif all(module.history_depth_state == "complete" for module in tracked_history):
        history_depth_state = "complete"
    elif all(module.history_depth_state == "insufficient" for module in tracked_history):
        history_depth_state = "insufficient"
    else:
        history_depth_state = "partial"
    return MacroPublicationDataQualityRead(
        coverage_state=coverage_state,
        current_health_state=current_health_state,
        history_depth_state=history_depth_state,
        coverage_gap_count=sum(
            max(
                module.expected_capabilities - module.available_capabilities,
                1 if module.coverage_state == "partial" else 0,
            )
            for module in modules
        ),
        current_health_gap_count=sum(
            max(
                module.tracked_datasets - module.current_datasets,
                1 if module.current_health_state != "current" else 0,
            )
            for module in modules
        ),
        history_gap_count=sum(
            max(
                module.tracked_history_datasets - module.complete_history_datasets,
                1 if module.history_depth_state in {"partial", "insufficient"} else 0,
            )
            for module in modules
        ),
        modules=modules,
    )


def _publication_module_reasons(
    *,
    coverage: Mapping[str, Any],
    dataset_states: tuple[Mapping[str, Any], ...],
    backfill: Mapping[str, Any] | None,
) -> tuple[MacroReason, ...]:
    candidates = [item.get("reason") for item in coverage.get("capabilities", ()) if isinstance(item, Mapping)]
    for state in dataset_states:
        candidates.extend((state.get("current_reason"), state.get("history_reason")))
    if backfill is not None:
        candidates.append(backfill.get("reason"))
    output: list[MacroReason] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        reason = MacroReason.model_validate(candidate)
        identity = (reason.code, reason.message)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(reason)
    return tuple(output)


def _publication_source_lineage(
    module: Mapping[str, Any],
) -> tuple[MacroPublicationSourceLineageRead, ...]:
    module_id = cast(MacroModuleId, str(module["module_id"]))
    module_label = str(module.get("label") or MACRO_MODULE_LABELS[module_id])
    evidence = _required_mapping(
        module.get("evidence"),
        "macro_publication_appendix_evidence_missing",
    )
    states = {
        str(item["dataset_id"]): item
        for item in evidence.get("dataset_states", ())
        if isinstance(item, Mapping) and item.get("dataset_id")
    }
    facts = {
        str(item["dataset_id"]): item
        for item in evidence.get("latest_facts", ())
        if isinstance(item, Mapping) and item.get("dataset_id")
    }
    dataset_ids = tuple(dict.fromkeys((*states, *facts)))
    output = []
    for dataset_id in dataset_ids:
        state = states.get(dataset_id, {})
        fact = facts.get(dataset_id, {})
        current_health_value = state.get("current_health")
        if current_health_value not in {None, "current", "degraded", "unavailable"}:
            raise ValueError("macro_publication_appendix_source_current_health_invalid")
        history_depth_value = state.get("history_depth")
        if history_depth_value not in {
            None,
            "complete",
            "partial",
            "insufficient",
            "not_required",
        }:
            raise ValueError("macro_publication_appendix_source_history_depth_invalid")
        output.append(
            MacroPublicationSourceLineageRead(
                module_id=module_id,
                module_label=module_label,
                dataset_id=dataset_id,
                label=str(state.get("label") or dataset_id),
                source_role=(str(state["source_role"]) if state.get("source_role") is not None else None),
                reference=(
                    str(fact.get("reference") or state.get("latest_reference"))
                    if fact.get("reference") is not None or state.get("latest_reference") is not None
                    else None
                ),
                value=_lineage_value(fact.get("value")),
                unit=str(fact["unit"]) if fact.get("unit") is not None else None,
                observed_at_ms=(int(fact["observed_at_ms"]) if fact.get("observed_at_ms") is not None else None),
                published_at_ms=(int(fact["published_at_ms"]) if fact.get("published_at_ms") is not None else None),
                received_at_ms=(
                    _int_or_default(
                        fact.get("received_at_ms")
                        if fact.get("received_at_ms") is not None
                        else state.get("latest_received_at_ms"),
                        0,
                    )
                    if fact.get("received_at_ms") is not None or state.get("latest_received_at_ms") is not None
                    else None
                ),
                source_url=(
                    str(fact.get("source_url") or state.get("source_url"))
                    if fact.get("source_url") is not None or state.get("source_url") is not None
                    else None
                ),
                current_health=cast(
                    Literal["current", "degraded", "unavailable"] | None,
                    current_health_value,
                ),
                history_depth=cast(
                    Literal["complete", "partial", "insufficient", "not_required"] | None,
                    history_depth_value,
                ),
                current_reason=_optional_reason(state.get("current_reason")),
                history_reason=_optional_reason(state.get("history_reason")),
            )
        )
    return tuple(output)


def _publication_reconciliation_receipts(
    module: Mapping[str, Any],
) -> tuple[MacroReconciliationReceiptRead, ...]:
    module_id = cast(MacroModuleId, str(module["module_id"]))
    module_label = str(module.get("label") or MACRO_MODULE_LABELS[module_id])
    evidence = _required_mapping(
        module.get("evidence"),
        "macro_publication_appendix_evidence_missing",
    )
    return tuple(
        MacroReconciliationReceiptRead(
            module_id=module_id,
            module_label=module_label,
            concept_id=str(receipt["concept_id"]),
            state=str(receipt["state"]),
            selection_policy=str(receipt["selection_policy"]),
            selected_dataset_id=(
                str(receipt["selected_dataset_id"]) if receipt.get("selected_dataset_id") is not None else None
            ),
            identity_policy=str(receipt["identity_policy"]),
            observations=tuple(
                MacroReconciliationObservationRead.model_validate(observation)
                for observation in receipt.get("observations", ())
            ),
            comparisons=tuple(
                MacroReconciliationComparisonRead.model_validate(comparison)
                for comparison in receipt.get("comparisons", ())
            ),
        )
        for receipt in evidence.get("reconciliation_receipts", ())
        if isinstance(receipt, Mapping)
    )


def _required_mapping(value: object, error_code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(error_code)
    return value


def _optional_reason(value: object) -> MacroReason | None:
    return MacroReason.model_validate(value) if isinstance(value, Mapping) else None


def _lineage_value(value: object) -> str | float | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return str(value)


def _int_or_default(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError("macro_publication_appendix_integer_invalid")
    return int(value)


def project_live_delta_for_read(
    *,
    payload: Mapping[str, Any],
    publication: Mapping[str, Any],
) -> MacroLiveDeltaRead:
    delta = MacroLiveDeltaV1.model_validate(payload)
    thesis = MacroThesisV1.model_validate(publication)
    if delta.publication_id != thesis.publication_id:
        raise ValueError("macro_live_delta_publication_mismatch")

    mainline_claim_ids = {claim.claim_id for claim in thesis.mainline.claims}
    mainline_falsifier_ids = {condition.condition_id for condition in thesis.mainline.falsifiers}
    mainline_checkpoint_ids = {condition.condition_id for condition in thesis.mainline.checkpoints}
    mainline_condition_ids = mainline_falsifier_ids | mainline_checkpoint_ids
    conditions_by_id = _publication_conditions(thesis)
    affected_claim_ids_by_condition = _publication_condition_claim_ids(thesis)
    ordered_scopes: list[tuple[MacroLiveDeltaScopeKind, str, str]] = [("mainline", "mainline", "整体主线")]
    if thesis.alternative_explanation is not None:
        ordered_scopes.append(("alternative", "alternative_explanation", thesis.alternative_explanation.title))
    ordered_scopes.extend(
        ("tension", f"tension:{tension.tension_id}", tension.statement) for tension in thesis.core_tensions
    )
    ordered_scopes.extend(
        (
            "asset",
            f"asset:{asset.symbol}:{horizon}",
            f"{asset.symbol} · {'1 周' if horizon == '1w' else '1 个月'}",
        )
        for asset in thesis.assets
        for horizon in ("1w", "1m")
    )
    items_by_scope: dict[tuple[MacroLiveDeltaScopeKind, str], list[MacroLiveDeltaItem]] = defaultdict(list)
    for item in delta.items:
        key = _live_delta_scope_key(
            item,
            mainline_claim_ids=mainline_claim_ids,
            mainline_condition_ids=mainline_condition_ids,
        )
        items_by_scope[key].append(item)

    scopes = tuple(
        MacroLiveDeltaScope(
            scope=scope,
            scope_id=scope_id,
            label=label,
            status=_aggregate_live_delta_status(items_by_scope[(scope, scope_id)]),
            matched_binding_ids=tuple(
                dict.fromkeys(
                    item.binding_id
                    for item in items_by_scope[(scope, scope_id)]
                    if item.status in {"confirming", "weakening", "invalidation_triggered"}
                )
            ),
            items=tuple(
                _live_delta_item_for_read(
                    item,
                    condition=conditions_by_id[item.condition_id],
                    observation_cutoff_ms=thesis.cutoff_ms,
                    affected_claim_ids=affected_claim_ids_by_condition[item.condition_id],
                )
                for item in items_by_scope[(scope, scope_id)]
            ),
        )
        for scope, scope_id, label in ordered_scopes
        if items_by_scope[(scope, scope_id)] or scope == "mainline"
    )
    mainline = next(scope for scope in scopes if scope.scope == "mainline")
    return MacroLiveDeltaRead(
        live_delta_id=delta.live_delta_id,
        publication_id=delta.publication_id,
        evaluated_at_ms=delta.evaluated_at_ms,
        module_fact_cutoff_ms=delta.module_fact_cutoff_ms,
        mainline_validity=mainline.status,
        matched_claim_ids=tuple(
            binding_id for binding_id in delta.matched_claim_ids if binding_id in mainline_claim_ids
        ),
        matched_falsifier_ids=tuple(
            binding_id for binding_id in delta.matched_falsifier_ids if binding_id in mainline_falsifier_ids
        ),
        matched_checkpoint_ids=tuple(
            binding_id for binding_id in delta.matched_checkpoint_ids if binding_id in mainline_checkpoint_ids
        ),
        scopes=scopes,
        reason_codes=delta.reason_codes,
        input_hash=delta.input_hash,
    )


def project_asset_presentation(publication: Mapping[str, Any] | None) -> tuple[MacroAssetPresentation, ...]:
    if publication is None:
        return ()
    thesis = MacroThesisV1.model_validate(publication)
    output = []
    for order, asset in enumerate(thesis.assets, start=1):
        evidence_gap = (
            asset.momentum.source_dataset_id is None
            or asset.momentum.momentum_1w == "insufficient"
            or asset.momentum.momentum_1m == "insufficient"
        )
        actionable = any(outlook.direction != "no_call" for outlook in (asset.outlook_1w, asset.outlook_1m))
        group: Literal["actionable", "watch", "evidence_gap"] = (
            "evidence_gap" if evidence_gap else "actionable" if actionable else "watch"
        )
        outlook_refs = {
            *asset.outlook_1w.supporting_evidence_refs,
            *asset.outlook_1w.conflicting_evidence_refs,
            *asset.outlook_1m.supporting_evidence_refs,
            *asset.outlook_1m.conflicting_evidence_refs,
        }
        claim_ids = tuple(
            claim.claim_id
            for claim in thesis.mainline.claims
            if outlook_refs
            & {
                *claim.supporting_evidence_refs,
                *claim.conflicting_evidence_refs,
                *(ref for edge in claim.causal_edges for ref in edge.evidence_refs),
                *(ref for edge in claim.causal_edges for ref in edge.conflicting_evidence_refs),
            }
        )
        output.append(
            MacroAssetPresentation(
                symbol=asset.symbol,
                order=order,
                group=group,
                source_dataset_id=asset.momentum.source_dataset_id or MACRO_ASSET_DATASETS[asset.symbol],
                as_of=asset.momentum.as_of,
                claim_ids=claim_ids,
                horizons=(
                    _asset_horizon_presentation(asset, "1w"),
                    _asset_horizon_presentation(asset, "1m"),
                ),
            )
        )
    if tuple(item.symbol for item in output) != MACRO_THESIS_ASSETS:
        raise ValueError("macro_asset_presentation_order")
    return tuple(output)


def project_claim_presentation(publication: Mapping[str, Any] | None) -> tuple[MacroClaimPresentation, ...]:
    if publication is None:
        return ()
    thesis = MacroThesisV1.model_validate(publication)
    condition_claim_ids = _publication_condition_claim_ids(thesis)
    output = []
    for claim in thesis.mainline.claims:
        claim_refs = {
            *claim.supporting_evidence_refs,
            *claim.conflicting_evidence_refs,
            *(ref for edge in claim.causal_edges for ref in edge.evidence_refs),
            *(ref for edge in claim.causal_edges for ref in edge.conflicting_evidence_refs),
        }
        implications = []
        for asset in thesis.assets:
            for outlook in (asset.outlook_1w, asset.outlook_1m):
                outlook_refs = {
                    *outlook.supporting_evidence_refs,
                    *outlook.conflicting_evidence_refs,
                }
                evidence_links = tuple(sorted(claim_refs & outlook_refs))
                if not evidence_links:
                    continue
                implications.append(
                    MacroClaimAssetImplication(
                        symbol=asset.symbol,
                        horizon=outlook.horizon,
                        direction=outlook.direction,
                        reader_rationale=_reader_asset_rationale(asset, outlook),
                        confidence=outlook.confidence,
                        evidence_links=evidence_links,
                        confirmation_triggers=outlook.confirmation_triggers,
                        falsifiers=outlook.falsifiers,
                        checkpoints=outlook.checkpoints,
                    )
                )
        output.append(
            MacroClaimPresentation(
                claim_id=claim.claim_id,
                statement=_reader_narrative(
                    claim.statement,
                    fallback="本论点的完整冻结原文含技术身份；可读论证请结合下方因果链与证据引用。",
                ),
                causal_edges=tuple(_reader_causal_edge(edge) for edge in claim.causal_edges),
                module_evidence=tuple(
                    MacroClaimModuleEvidencePresentation(
                        module_id=assessment.module_id,
                        role=assessment.role,
                        reader_narrative=_reader_module_assessment(assessment),
                        supporting_evidence_refs=assessment.supporting_evidence_refs,
                        conflicting_evidence_refs=assessment.conflicting_evidence_refs,
                    )
                    for assessment in thesis.module_assessments
                    if claim.claim_id in assessment.claim_ids
                ),
                supporting_evidence_refs=claim.supporting_evidence_refs,
                conflicting_evidence_refs=claim.conflicting_evidence_refs,
                conditions=claim.conditions,
                falsifiers=_conditions_for_claim(
                    thesis.mainline.falsifiers,
                    claim_id=claim.claim_id,
                    condition_claim_ids=condition_claim_ids,
                ),
                checkpoints=_conditions_for_claim(
                    thesis.mainline.checkpoints,
                    claim_id=claim.claim_id,
                    condition_claim_ids=condition_claim_ids,
                ),
                asset_implications=tuple(implications),
            )
        )
    return tuple(output)


def project_mainline_presentation(
    publication: Mapping[str, Any] | None,
) -> MacroMainlinePresentation | None:
    if publication is None:
        return None
    mainline = MacroThesisV1.model_validate(publication).mainline
    return MacroMainlinePresentation(
        title=_reader_narrative(
            mainline.title,
            fallback="本期宏观主线",
        ),
        thesis=_reader_narrative(
            mainline.thesis,
            fallback="冻结主线原文含技术身份；请从论点、证据、反证与条件链阅读本期判断。",
        ),
    )


def project_alternative_presentation(
    publication: Mapping[str, Any] | None,
) -> MacroAlternativePresentation | None:
    if publication is None:
        return None
    alternative = MacroThesisV1.model_validate(publication).alternative_explanation
    if alternative is None:
        return None
    return MacroAlternativePresentation(
        title=_reader_narrative(
            alternative.title,
            fallback="已登记的备选解释",
        ),
        thesis=_reader_narrative(
            alternative.thesis,
            fallback="备选解释原文含技术身份；其成立条件与证据引用如下。",
        ),
        causal_edges=tuple(_reader_causal_edge(edge) for edge in alternative.causal_edges),
        supporting_evidence_refs=alternative.supporting_evidence_refs,
        conflicting_evidence_refs=alternative.conflicting_evidence_refs,
        trigger_conditions=alternative.trigger_conditions,
    )


def project_module_reader_narrative(
    publication: Mapping[str, Any] | None,
    *,
    module_id: MacroModuleId,
) -> MacroReaderNarrative | None:
    if publication is None:
        return None
    thesis = MacroThesisV1.model_validate(publication)
    assessment = next(
        (item for item in thesis.module_assessments if item.module_id == module_id),
        None,
    )
    return _reader_module_assessment(assessment) if assessment is not None else None


def project_module_annotations(
    publication: Mapping[str, Any] | None,
    *,
    module_id: str,
) -> tuple[MacroConditionAnnotation, ...]:
    if publication is None:
        return ()
    thesis = MacroThesisV1.model_validate(publication)
    annotations: list[MacroConditionAnnotation] = []

    def add(
        conditions: tuple[MacroCondition, ...],
        *,
        kind: Literal["confirmation", "falsifier", "checkpoint"],
        binding_type: Literal["claim", "mainline", "alternative", "tension", "asset"],
        binding_id: str,
    ) -> None:
        annotations.extend(
            MacroConditionAnnotation(
                kind=kind,
                binding_type=binding_type,
                binding_id=binding_id,
                condition=condition,
            )
            for condition in conditions
            if condition.module_id == module_id
        )

    for claim in thesis.mainline.claims:
        add(claim.conditions, kind="confirmation", binding_type="claim", binding_id=claim.claim_id)
    add(thesis.mainline.falsifiers, kind="falsifier", binding_type="mainline", binding_id="mainline")
    add(thesis.mainline.checkpoints, kind="checkpoint", binding_type="mainline", binding_id="mainline")
    if thesis.alternative_explanation is not None:
        add(
            thesis.alternative_explanation.trigger_conditions,
            kind="confirmation",
            binding_type="alternative",
            binding_id="alternative_explanation",
        )
    for tension in thesis.core_tensions:
        add(
            tension.resolution_triggers,
            kind="checkpoint",
            binding_type="tension",
            binding_id=tension.tension_id,
        )
    for asset in thesis.assets:
        for outlook in (asset.outlook_1w, asset.outlook_1m):
            binding_id = f"asset:{asset.symbol}:{outlook.horizon}"
            add(
                outlook.confirmation_triggers,
                kind="confirmation",
                binding_type="asset",
                binding_id=binding_id,
            )
            add(outlook.falsifiers, kind="falsifier", binding_type="asset", binding_id=binding_id)
            add(outlook.checkpoints, kind="checkpoint", binding_type="asset", binding_id=binding_id)
    return tuple(annotations)


def project_outcome_replay_for_read(
    payload: Mapping[str, Any],
) -> MacroOutcomeReplayRead:
    replay = MacroOutcomeReplayV1.model_validate(payload)
    return MacroOutcomeReplayRead(
        replay_id=replay.replay_id,
        publication_id=replay.publication_id,
        evaluated_at_ms=replay.evaluated_at_ms,
        horizons=tuple(
            MacroOutcomeHorizonRead(
                horizon=horizon.horizon,
                expires_at_ms=horizon.expires_at_ms,
                status=horizon.status,
                benchmark_symbol=horizon.benchmark_symbol,
                realized_return_pct=horizon.realized_return_pct,
                direction_correct=horizon.direction_correct,
                source_reason_code=horizon.reason_code,
                reason=_outcome_reason(
                    reason_code=horizon.reason_code,
                    expires_at_ms=horizon.expires_at_ms,
                    dataset_id=MACRO_ASSET_DATASETS["SPY"],
                    subject=f"{horizon.horizon} SPY 基准",
                ),
                asset_results=tuple(
                    MacroOutcomeAssetResultRead(
                        symbol=result.symbol,
                        horizon=result.horizon,
                        expires_at_ms=result.expires_at_ms,
                        status=result.status,
                        published_direction=result.published_direction,
                        realized_return_pct=result.realized_return_pct,
                        direction_correct=result.direction_correct,
                        source_reason_code=result.reason_code,
                        reason=_outcome_reason(
                            reason_code=result.reason_code,
                            expires_at_ms=result.expires_at_ms,
                            dataset_id=MACRO_ASSET_DATASETS[result.symbol],
                            subject=f"{result.symbol} {result.horizon}",
                        ),
                    )
                    for result in horizon.asset_results
                ),
            )
            for horizon in replay.horizons
        ),
        input_hash=replay.input_hash,
    )


def _asset_horizon_presentation(asset: Any, horizon: Literal["1w", "1m"]) -> MacroAssetHorizonPresentation:
    outlook = asset.outlook_1w if horizon == "1w" else asset.outlook_1m
    momentum_state = asset.momentum.momentum_1w if horizon == "1w" else asset.momentum.momentum_1m
    momentum_value = asset.momentum.return_1w_pct if horizon == "1w" else asset.momentum.return_1m_pct
    dataset_id = asset.momentum.source_dataset_id or MACRO_ASSET_DATASETS[asset.symbol]
    reason = None
    if momentum_state == "insufficient":
        reason = MacroReason(
            code="asset_momentum_evidence_missing",
            message=f"{asset.symbol} {horizon} 缺少可计算的资产动量证据。",
            impact="blocked",
            affected_dataset_ids=(dataset_id,),
            retryable=True,
            recovery="automatic",
            next_action="等待权威资产事实补齐后重新生成下一交易日主线档案。",
        )
    elif outlook.direction == "no_call":
        reason = MacroReason(
            code="asset_outlook_watch",
            message=f"{asset.symbol} {horizon} 动量证据存在，但已发布判断仍为“证据不足，暂不判断”。",
            impact="limited",
            affected_dataset_ids=(dataset_id,),
            retryable=True,
            recovery="next_session",
            next_action="按已发布检查点观察，下一交易日由新主线档案决定是否形成方向。",
        )
    return MacroAssetHorizonPresentation(
        horizon=horizon,
        momentum_state=momentum_state,
        momentum_value=momentum_value,
        outlook_direction=outlook.direction,
        reader_rationale=_reader_asset_rationale(asset, outlook),
        confidence=outlook.confidence,
        supporting_evidence_refs=outlook.supporting_evidence_refs,
        conflicting_evidence_refs=outlook.conflicting_evidence_refs,
        confirmation_triggers=outlook.confirmation_triggers,
        falsifiers=outlook.falsifiers,
        checkpoints=outlook.checkpoints,
        reason=reason,
    )


_READER_CAUSAL_NODE_LABELS: dict[str, str] = {
    **{str(module_id): label for module_id, label in MACRO_MODULE_LABELS.items()},
    "equity_valuation": "股票估值",
    "duration_assets": "久期资产",
    "risk_assets": "风险资产",
}

_READER_RESERVED_FRAGMENTS: tuple[str, ...] = (
    "macro-module:",
    "Evidence ref ",
    "no_call",
    "degraded",
    "unavailable",
    "insufficient",
    "binding_id",
    "claim_id",
    "dataset_id",
    "analysis:",
    "sha256:",
    "worker:",
    *MACRO_MODULE_IDS,
    *_READER_CAUSAL_NODE_LABELS,
)


def _reader_text_is_safe(value: str) -> bool:
    text = value.strip()
    return bool(text) and "_" not in text and all(fragment not in text for fragment in _READER_RESERVED_FRAGMENTS)


def _reader_narrative(value: str, *, fallback: str) -> MacroReaderNarrative:
    if _reader_text_is_safe(value):
        return MacroReaderNarrative(text=value.strip(), origin="publication")
    return MacroReaderNarrative(text=fallback, origin="structured_fallback")


def _reader_causal_node(value: str, *, fallback: str) -> str:
    if value in _READER_CAUSAL_NODE_LABELS:
        return _READER_CAUSAL_NODE_LABELS[value]
    return value.strip() if _reader_text_is_safe(value) else fallback


def _reader_causal_edge(edge: Any) -> MacroCausalEdgePresentation:
    return MacroCausalEdgePresentation(
        source_label=_reader_causal_node(edge.source, fallback="宏观驱动"),
        mechanism=_reader_narrative(edge.mechanism, fallback="通过已登记的宏观传导机制"),
        target_label=_reader_causal_node(edge.target, fallback="资产定价结果"),
    )


def _reader_module_assessment(assessment: Any) -> MacroReaderNarrative:
    role_label = {
        "driver": "驱动证据",
        "confirming": "确认信号",
        "contradicting": "反向证据",
        "uncertain": "待确认信号",
    }[assessment.role]
    fallback = (
        f"{MACRO_MODULE_LABELS[assessment.module_id]}作为本论点的{role_label}；"
        f"本次档案登记 {len(assessment.supporting_evidence_refs)} 条支持证据"
        f"与 {len(assessment.conflicting_evidence_refs)} 条反向证据。"
    )
    return _reader_narrative(assessment.analysis, fallback=fallback)


def _reader_asset_rationale(asset: Any, outlook: Any) -> MacroReaderNarrative:
    horizon_label = "1 周" if outlook.horizon == "1w" else "1 个月"
    momentum_state = asset.momentum.momentum_1w if outlook.horizon == "1w" else asset.momentum.momentum_1m
    momentum_value = asset.momentum.return_1w_pct if outlook.horizon == "1w" else asset.momentum.return_1m_pct
    if momentum_state == "insufficient":
        return MacroReaderNarrative(
            text=f"{asset.symbol} 的{horizon_label}事实动量缺失，因此本期不形成方向判断。",
            origin="structured_fallback",
        )
    momentum_label = {"up": "上行", "down": "下行", "flat": "横盘"}[momentum_state]
    value_text = f"（{momentum_value:+.2f}%）" if momentum_value is not None else ""
    direction_label = {
        "bullish": "偏多",
        "bearish": "偏空",
        "neutral": "中性",
        "no_call": "证据不足，暂不判断",
    }[outlook.direction]
    fallback = (
        f"{asset.symbol} 的{horizon_label}事实动量{momentum_label}{value_text}；"
        f"已发布判断为{direction_label}，登记 {len(outlook.supporting_evidence_refs)} 条支持证据"
        f"与 {len(outlook.conflicting_evidence_refs)} 条反向证据。"
    )
    if outlook.direction == "no_call":
        return MacroReaderNarrative(text=fallback, origin="structured_fallback")
    return _reader_narrative(outlook.causal_channel, fallback=fallback)


def _live_delta_scope_key(
    item: MacroLiveDeltaItem,
    *,
    mainline_claim_ids: set[str],
    mainline_condition_ids: set[str],
) -> tuple[MacroLiveDeltaScopeKind, str]:
    if item.binding_id in mainline_claim_ids or item.binding_id in mainline_condition_ids:
        return "mainline", "mainline"
    if item.binding_id == "alternative_explanation":
        return "alternative", item.binding_id
    if item.binding_id.startswith("tension:"):
        return "tension", item.binding_id
    if item.binding_id.startswith("asset:"):
        return "asset", item.binding_id
    raise ValueError("macro_live_delta_binding_scope_unknown:" + item.binding_id)


def _publication_conditions(thesis: MacroThesisV1) -> dict[str, MacroCondition]:
    conditions: list[MacroCondition] = []
    for claim in thesis.mainline.claims:
        conditions.extend(claim.conditions)
    conditions.extend(thesis.mainline.falsifiers)
    conditions.extend(thesis.mainline.checkpoints)
    if thesis.alternative_explanation is not None:
        conditions.extend(thesis.alternative_explanation.trigger_conditions)
    for tension in thesis.core_tensions:
        conditions.extend(tension.resolution_triggers)
    for asset in thesis.assets:
        for outlook in (asset.outlook_1w, asset.outlook_1m):
            conditions.extend(outlook.confirmation_triggers)
            conditions.extend(outlook.falsifiers)
            conditions.extend(outlook.checkpoints)
    return {condition.condition_id: condition for condition in conditions}


def _publication_condition_claim_ids(
    thesis: MacroThesisV1,
) -> dict[str, tuple[str, ...]]:
    ordered_claim_ids = tuple(claim.claim_id for claim in thesis.mainline.claims)
    claims_by_module = {
        assessment.module_id: frozenset(assessment.claim_ids) for assessment in thesis.module_assessments
    }
    output: dict[str, tuple[str, ...]] = {}
    for condition_id, condition in _publication_conditions(thesis).items():
        explicit_claim_ids = claims_by_module.get(condition.module_id, frozenset())
        output[condition_id] = tuple(claim_id for claim_id in ordered_claim_ids if claim_id in explicit_claim_ids)
    for claim in thesis.mainline.claims:
        for condition in claim.conditions:
            output[condition.condition_id] = tuple(dict.fromkeys((*output[condition.condition_id], claim.claim_id)))
    return output


def _conditions_for_claim(
    conditions: tuple[MacroCondition, ...],
    *,
    claim_id: str,
    condition_claim_ids: Mapping[str, tuple[str, ...]],
) -> tuple[MacroCondition, ...]:
    output: list[MacroCondition] = []
    seen: set[str] = set()
    for condition in conditions:
        if claim_id not in condition_claim_ids[condition.condition_id]:
            continue
        if condition.condition_id in seen:
            continue
        seen.add(condition.condition_id)
        output.append(condition)
    return tuple(output)


def _live_delta_item_for_read(
    item: MacroLiveDeltaItem,
    *,
    condition: MacroCondition,
    observation_cutoff_ms: int,
    affected_claim_ids: tuple[str, ...],
) -> MacroLiveDeltaItemRead:
    spec = DATASET_REGISTRY.get(item.dataset_id)
    try:
        unit = natural_change_calculation(item.dataset_id).output_unit
    except ValueError:
        unit = None
    message, impact, retryable, recovery, next_action = {
        "condition_threshold_matched": (
            "当前观测满足已发布条件。",
            "none",
            False,
            "none",
            None,
        ),
        "condition_threshold_not_matched": (
            "当前观测未满足已发布条件；该事实与此绑定暂不相关。",
            "none",
            False,
            "none",
            None,
        ),
        "post_cutoff_fact_missing": (
            "发布截点后尚无该数据集的新事实。",
            "limited",
            True,
            "automatic",
            "等待该数据集写入下一条发布截点后的事实。",
        ),
        "condition_metric_missing": (
            "新事实存在，但无法计算已发布条件所需指标。",
            "limited",
            True,
            "automatic",
            "等待满足自然频率最小观测要求后重新评估新增事实。",
        ),
    }[item.reason_code]
    return MacroLiveDeltaItemRead(
        binding_type=item.binding_type,
        binding_id=item.binding_id,
        condition_id=item.condition_id,
        status=item.status,
        dataset_id=item.dataset_id,
        dataset_label=spec.label if spec is not None else item.dataset_id,
        metric_name=item.metric_name,
        unit=unit,
        observed_value=item.observed_value,
        observed_at_ms=item.observed_at_ms,
        observation_cutoff_ms=observation_cutoff_ms,
        operator=item.operator,
        threshold=item.threshold,
        rationale=condition.rationale,
        source_reason_code=item.reason_code,
        reason=MacroReason(
            code=item.reason_code,
            message=message,
            impact=impact,
            affected_dataset_ids=(item.dataset_id,),
            affected_claim_ids=affected_claim_ids,
            retryable=retryable,
            recovery=recovery,
            next_action=next_action,
        ),
    )


def _aggregate_live_delta_status(items: list[MacroLiveDeltaItem]) -> MacroLiveDeltaStatus:
    statuses = {item.status for item in items}
    if "invalidation_triggered" in statuses:
        return "invalidation_triggered"
    if "weakening" in statuses:
        return "weakening"
    if "confirming" in statuses:
        return "confirming"
    if "insufficient" in statuses:
        return "insufficient"
    if not items:
        return "insufficient"
    return "unrelated"


def _outcome_reason(
    *,
    reason_code: str,
    expires_at_ms: int,
    dataset_id: str,
    subject: str,
) -> MacroReason:
    if reason_code == "horizon_not_expired":
        return MacroReason(
            code=reason_code,
            message=f"{subject} 的评估窗口尚未结束。",
            impact="none",
            affected_dataset_ids=(dataset_id,),
            retryable=True,
            recovery="automatic",
            next_action="等待评估窗口到期后，由结果复盘任务读取首个合格收盘事实。",
            next_check_at_ms=expires_at_ms,
        )
    if reason_code in {"benchmark_observation_missing", "asset_observation_missing"}:
        return MacroReason(
            code=reason_code,
            message=f"{subject} 缺少主线发布起点或到期后的合格收盘事实。",
            impact="limited",
            affected_dataset_ids=(dataset_id,),
            retryable=True,
            recovery="automatic",
            next_action="补齐权威日线事实后重新计算该评估窗口。",
        )
    if reason_code == "directional_outlook_evaluated":
        return MacroReason(
            code=reason_code,
            message=f"{subject} 的方向判断已按实际收益完成评估。",
            impact="none",
            affected_dataset_ids=(dataset_id,),
            retryable=False,
            recovery="none",
        )
    if reason_code == "no_directional_outlook":
        return MacroReason(
            code=reason_code,
            message=f"{subject} 已有实际收益，但发布判断为中性或“证据不足，暂不判断”，不计算方向命中。",
            impact="none",
            affected_dataset_ids=(dataset_id,),
            retryable=False,
            recovery="none",
        )
    raise ValueError("macro_outcome_reason_code_unknown:" + reason_code)


__all__ = [
    "MacroAlternativePresentation",
    "MacroAssetHorizonPresentation",
    "MacroAssetPresentation",
    "MacroCausalEdgePresentation",
    "MacroClaimAssetImplication",
    "MacroClaimModuleEvidencePresentation",
    "MacroClaimPresentation",
    "MacroConditionAnnotation",
    "MacroLiveDeltaItemRead",
    "MacroLiveDeltaRead",
    "MacroLiveDeltaScope",
    "MacroMainlinePresentation",
    "MacroOutcomeAssetResultRead",
    "MacroOutcomeHorizonRead",
    "MacroOutcomeReplayRead",
    "MacroReaderNarrative",
    "project_alternative_presentation",
    "project_asset_presentation",
    "project_claim_presentation",
    "project_live_delta_for_read",
    "project_mainline_presentation",
    "project_module_annotations",
    "project_module_reader_narrative",
    "project_outcome_replay_for_read",
]
