from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefold.macro import (
    MacroCompiledCondition,
    MacroDraftModuleAssessment,
    MacroLiveDeltaV2,
    MacroOutcomeReplayV2,
    MacroReason,
    MacroRecoveryItem,
    MacroThesisV1,
    MacroThesisV2,
)

JsonObject = dict[str, Any]


class ApiSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ExactApiSchema(ApiSchema):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ApiEnvelope[T](ExactApiSchema):
    ok: bool
    data: T | None = None
    error: str | None = None
    field: str | None = None


class BootstrapData(ExactApiSchema):
    ws_token: str
    replay_limit: int


class StatusDatabaseData(ExactApiSchema):
    ok: bool
    schema_ok: bool
    current_revision: str | None
    expected_revision: str
    error_code: Literal["database_unavailable", "schema_mismatch"] | None


class WorkersRuntimeData(ExactApiSchema):
    runtime_id: str | None
    runtime_version: str | None
    state: Literal[
        "starting",
        "running",
        "stopping",
        "stopped",
        "failed",
        "stale",
        "unavailable",
    ]
    started_at_ms: int | None
    heartbeat_at_ms: int | None
    heartbeat_stale_after_ms: Literal[15000]
    fatal_code: (
        Literal[
            "startup_failed",
            "child_failed",
            "control_failed",
            "singleton_lost",
            "runtime_invariant_failed",
            "resource_operation_overrun",
            "graceful_deadline_exceeded",
            "cleanup_failed",
        ]
        | None
    )
    unavailable_reason: (
        Literal[
            "runtime_status_query_failed",
            "runtime_missing",
            "runtime_heartbeat_stale",
            "runtime_starting",
            "runtime_stopping",
            "runtime_stopped",
            "runtime_failed",
        ]
        | None
    )


class StatusData(ExactApiSchema):
    ok: bool
    reasons: list[
        Literal[
            "database_unavailable",
            "database_schema_mismatch",
            "runtime_status_query_failed",
            "runtime_missing",
            "runtime_heartbeat_stale",
            "runtime_starting",
            "runtime_stopping",
            "runtime_stopped",
            "runtime_failed",
        ]
    ]
    measured_at_ms: int
    db: StatusDatabaseData
    workers_runtime: WorkersRuntimeData


class ReadinessData(ExactApiSchema):
    ok: bool
    reasons: list[str]
    store: Literal["postgresql"]
    db: JsonObject
    composition: JsonObject


class MacroCoverageCapabilityData(ExactApiSchema):
    capability_id: str
    label: str
    requirement: Literal["required", "supporting"]
    state: Literal["available", "missing"]
    dataset_ids: list[str]
    reason: MacroReason | None


class MacroCoverageData(ExactApiSchema):
    state: Literal["complete", "partial"]
    expected_capabilities: int
    available_capabilities: int
    capabilities: list[MacroCoverageCapabilityData]


class MacroCurrentHealthGroupData(ExactApiSchema):
    group_id: str
    label: str
    current_health: Literal["current", "degraded", "unavailable", "mixed"]
    market_state: Literal["open", "closed", "maintenance", "unknown", "not_applicable", "mixed"]
    source_state: Literal["healthy", "degraded", "failed", "not_applicable", "mixed"]
    current_datasets: int
    tracked_datasets: int


class MacroCurrentHealthData(ExactApiSchema):
    state: Literal["current", "degraded", "unavailable"]
    current_datasets: int
    tracked_datasets: int
    as_of_ms: int
    groups: list[MacroCurrentHealthGroupData]


class MacroHistoryDepthData(ExactApiSchema):
    state: Literal["complete", "partial", "insufficient", "not_required"]
    complete_datasets: int
    tracked_datasets: int


class MacroBackfillExecutionData(ExactApiSchema):
    state: Literal["not_required", "complete", "queued", "running", "retry_wait", "failed"]
    total_targets: int
    complete_targets: int
    pending_targets: int
    failed_targets: int
    next_check_at_ms: int | None
    reason: MacroReason | None


class MacroModuleStatusData(ExactApiSchema):
    coverage: MacroCoverageData
    current_health: MacroCurrentHealthData
    history_depth: MacroHistoryDepthData
    backfill_execution: MacroBackfillExecutionData


class MacroImportanceFactorsData(ExactApiSchema):
    standardized_magnitude: float
    surprise_magnitude: float
    revision_magnitude: float
    decision_relevance: int
    trust_tier: Literal["official", "exchange", "untrusted_proxy"]
    fact_clock_ms: int


class MacroChangeData(ExactApiSchema):
    dataset_id: str
    concept_id: str
    source_role: str
    label: str
    as_of: str | None
    value: float
    unit: str
    cadence: str
    metrics: dict[str, float | None]
    metric_unit: str
    primary_change: float | None
    importance_rank: int
    importance_factors: MacroImportanceFactorsData
    importance_explanation: str
    source_url: str


class MacroModuleSummaryStateData(ExactApiSchema):
    headline: str | None
    interpretation: str | None
    top_changes: list[MacroChangeData]


class MacroOverviewModuleSummaryStateData(ExactApiSchema):
    headline: str | None
    interpretation: str | None


class MacroDatasetStateData(ExactApiSchema):
    dataset_id: str
    concept_id: str
    source_role: str
    required_for_current: bool
    required_for_history: bool
    label: str
    current_health: Literal["current", "degraded", "unavailable"]
    history_depth: Literal["complete", "partial", "insufficient", "not_required"]
    market_state: Literal["open", "closed", "maintenance", "unknown", "not_applicable"]
    source_state: Literal["healthy", "degraded", "failed", "not_applicable"]
    current_reason: MacroReason
    history_reason: MacroReason
    critical: bool
    trust_tier: Literal["official", "exchange", "untrusted_proxy"]
    source_url: str
    latest_reference: str | None
    latest_received_at_ms: int | None
    last_market_at_ms: int | None
    next_open_ms: int | None
    health_group: str


class MacroEvidenceFactData(ExactApiSchema):
    dataset_id: str
    series_id: str | None = None
    contract_code: str | None = None
    fact_ref: str | None
    reference: str | None
    value: float | str | None
    unit: str
    observed_at_ms: int | None
    published_at_ms: int | None
    received_at_ms: int
    source_url: str


class MacroReconciliationObservationData(ExactApiSchema):
    dataset_id: str
    source_role: str
    reference: str | None
    value: float | str | None
    unit: str
    fact_ref: str | None


class MacroReconciliationComparisonData(ExactApiSchema):
    left_dataset_id: str
    right_dataset_id: str
    left_fact_ref: str
    right_fact_ref: str
    aligned_reference: str | None
    left_reference: str | None
    right_reference: str | None
    left_value: float
    right_value: float
    difference: float
    tolerance: float
    unit: str
    status: Literal["reference_mismatch", "within_tolerance", "divergent"]


class MacroReconciliationReceiptData(ExactApiSchema):
    concept_id: str
    state: Literal["complete", "partial", "insufficient"]
    selection_policy: Literal["decision_primary_only_no_fallback"]
    selected_dataset_id: str | None
    identity_policy: Literal["separate_source_facts_no_blend"]
    observations: list[MacroReconciliationObservationData]
    comparisons: list[MacroReconciliationComparisonData]


class MacroModuleEvidenceData(ExactApiSchema):
    dataset_states: list[MacroDatasetStateData]
    latest_facts: list[MacroEvidenceFactData]
    asset_changes: list[MacroChangeData]
    reconciliation_receipts: list[MacroReconciliationReceiptData]


class MacroNextCheckpointData(ExactApiSchema):
    dataset_id: str
    label: str
    current_health: str
    history_depth: str
    reason: MacroReason | None
    next_check_at_ms: int | None


class MacroModuleThesisContextData(ExactApiSchema):
    state: Literal[
        "published",
        "pending",
        "running",
        "retryable",
        "failed",
        "config_error",
        "not_published",
        "missing",
    ]
    session_date: date
    cutoff_ms: int
    role: Literal[
        "driver",
        "confirming",
        "contradicting",
        "uncertain",
        "not_material",
        "unassessed",
    ]
    assessment: MacroDraftModuleAssessment | None
    conditions: list[MacroCompiledCondition]
    recovery: list[MacroRecoveryItem]
    reason: MacroReason | None


class _MacroModulePersistedBaseData(ExactApiSchema):
    label: str
    status: MacroModuleStatusData
    latest_fact_at_ms: int
    summary: MacroModuleSummaryStateData
    contradictions: list[str]
    falsifiers: list[str]
    next_checkpoints: list[MacroNextCheckpointData]
    evidence: MacroModuleEvidenceData


class MacroHistoryPointData(ExactApiSchema):
    date: date
    value: float


class MacroIndicatorData(ExactApiSchema):
    dataset_id: str
    label: str
    latest_value: float
    unit: str
    as_of: date
    change_1w: float | None
    change_1m: float | None
    sample_count: int
    percentile: float | None = None
    history_start: date
    history_end: date
    source_url: str
    history: list[MacroHistoryPointData]


class MacroIndicatorSectionData(ExactApiSchema):
    indicators: list[MacroIndicatorData]


class MacroPositionData(ExactApiSchema):
    contract_code: str
    contract_name: str
    report_date: date
    leveraged_net_pct_oi: float
    asset_manager_net_pct_oi: float
    dealer_net_pct_oi: float
    source_url: str


class MacroMarketFactData(ExactApiSchema):
    dataset_id: str
    latest_value: float
    unit: str
    as_of: date
    market_time_ms: int
    change_1d_pct: float | None
    change_1w_pct: float | None
    change_1m_pct: float | None
    source_url: str


class MacroCurveYieldPointData(ExactApiSchema):
    tenor: str
    years: float
    yield_pct: float
    dataset_id: str
    source_role: Literal["decision_primary"]
    fact_id: str
    source_url: str


class MacroCurveBreakevenPointData(ExactApiSchema):
    tenor: str
    years: float
    breakeven_pct: float
    input_fact_ids: list[str]


class MacroCurveYieldSnapshotData(ExactApiSchema):
    window: Literal["current", "previous", "1w", "mtd", "3m"]
    as_of: date
    points: list[MacroCurveYieldPointData]


class MacroCurveBreakevenSnapshotData(ExactApiSchema):
    window: Literal["current", "previous", "1w", "mtd", "3m"]
    as_of: date
    points: list[MacroCurveBreakevenPointData]


class MacroCurveSpreadPointData(ExactApiSchema):
    date: date
    value_bp: float
    input_fact_ids: list[str]


class MacroCurveSpreadsData(ExactApiSchema):
    two_s_ten_s: list[MacroCurveSpreadPointData] = Field(alias="2s10s")
    ten_s_thirty_s: list[MacroCurveSpreadPointData] = Field(alias="10s30s")
    three_m_ten_s: list[MacroCurveSpreadPointData] = Field(alias="3m10s")
    five_s_thirty_s: list[MacroCurveSpreadPointData] = Field(alias="5s30s")


class MacroRatesWindowChangeData(ExactApiSchema):
    window: Literal["1d", "1w", "mtd", "3m", "past_30d"]
    state: Literal["available", "baseline", "unavailable"]
    current_date: date | None
    baseline_date: date | None
    change_bp: float | None
    selection_policy: Literal[
        "previous_treasury_observation",
        "bounded_previous_observation_4_calendar_days",
        "first_available_treasury_observation_in_calendar_month",
    ]
    input_fact_ids: list[str]


class MacroRatesTenorCurrentData(ExactApiSchema):
    tenor: Literal["2Y", "10Y", "30Y"]
    reference_date: date
    yield_pct: float
    dataset_id: Literal["treasury.daily_nominal_curve"]
    source_role: Literal["decision_primary"]
    fact_id: str
    source_url: str


class MacroRatesTenorDecisionData(ExactApiSchema):
    tenor: Literal["2Y", "10Y", "30Y"]
    current: MacroRatesTenorCurrentData | None
    windows: list[MacroRatesWindowChangeData]


class MacroRatesLatestObservationData(ExactApiSchema):
    tenor: Literal["2Y", "10Y", "30Y"]
    reference_date: date | None
    fact_id: str | None


class MacroRatesSessionCompletenessData(ExactApiSchema):
    state: Literal["complete", "unaligned", "incomplete"]
    reference_date: date | None
    required_tenors: list[Literal["2Y", "10Y", "30Y"]]
    latest_observations: list[MacroRatesLatestObservationData]
    reason: str | None


class MacroRatesSpreadSummaryData(ExactApiSchema):
    spread_id: Literal["2s10s", "10s30s"]
    label: str
    state: Literal["available", "unaligned", "incomplete", "insufficient_history"]
    current_date: date | None
    prior_date: date | None
    value_bp: float | None
    change_1d_bp: float | None
    input_fact_ids: list[str]


class MacroRatesDecompositionData(ExactApiSchema):
    tenor: Literal["10Y", "30Y"]
    state: Literal["available", "unaligned", "insufficient_history"]
    current_date: date | None
    prior_date: date | None
    nominal_change_bp: float | None
    real_change_bp: float | None
    breakeven_change_bp: float | None
    assessment_state: (
        Literal[
            "inflation_compensation_dominant",
            "real_yield_dominant",
            "mixed_real_and_inflation_compensation",
        ]
        | None
    )
    assessment: str | None
    input_fact_ids: list[str]
    gap: str | None


class MacroCurveClassificationInputsData(ExactApiSchema):
    current_as_of: date | None = None
    prior_as_of: date | None = None
    two_year_change_bp: float | None = Field(default=None, alias="2y_change_bp")
    ten_year_change_bp: float | None = Field(default=None, alias="10y_change_bp")
    thirty_year_change_bp: float | None = Field(default=None, alias="30y_change_bp")
    level_change_bp: float | None = None
    slope_change_bp: float | None = None
    curvature_change_bp: float | None = None
    current_2s10s_bp: float | None = None
    current_10s30s_bp: float | None = None


class MacroCurveClassificationData(ExactApiSchema):
    window: Literal["1d", "1w", "mtd", "3m"]
    state: Literal[
        "unaligned",
        "insufficient_history",
        "insufficient_tenors",
        "twist_steepening",
        "parallel_up",
        "parallel_down",
        "bear_steepening",
        "bull_steepening",
        "bear_flattening",
        "bull_flattening",
        "stable",
    ]
    label: str
    formula_version: Literal["level_slope_curvature_classification_v3"]
    inputs: MacroCurveClassificationInputsData


class MacroRatesExplanationStatementData(ExactApiSchema):
    statement: str
    input_fact_ids: list[str]


class MacroRatesBoundedAssessmentData(MacroRatesExplanationStatementData):
    assessment_id: str
    uncertainty: str


class MacroRatesExplanationData(ExactApiSchema):
    facts: list[MacroRatesExplanationStatementData]
    bounded_assessments: list[MacroRatesBoundedAssessmentData]
    hypotheses: list[MacroRatesExplanationStatementData]


class MacroRatesSourcePolicyData(ExactApiSchema):
    decision_primary_dataset_ids: list[Literal["treasury.daily_nominal_curve", "treasury.daily_real_curve"]]
    history_only_dataset_ids: list[Literal["fred.dgs2", "fred.dgs10", "fred.dgs30", "fred.dfii10", "fred.t10yie"]]
    selection_policy: Literal["treasury_completed_session_primary_fred_history_only"]


class MacroRatesDecisionData(ExactApiSchema):
    state: Literal["available", "unaligned", "incomplete", "insufficient_history"]
    reference_date: date | None
    headline: str | None
    session_completeness: MacroRatesSessionCompletenessData
    tenor_matrix: list[MacroRatesTenorDecisionData]
    spread_summary: list[MacroRatesSpreadSummaryData]
    decompositions: list[MacroRatesDecompositionData]
    classifications: list[MacroCurveClassificationData]
    explanation: MacroRatesExplanationData
    source_policy: MacroRatesSourcePolicyData


class MacroCurveData(ExactApiSchema):
    nominal_snapshots: list[MacroCurveYieldSnapshotData]
    real_snapshots: list[MacroCurveYieldSnapshotData]
    breakeven_snapshots: list[MacroCurveBreakevenSnapshotData]
    spreads: MacroCurveSpreadsData


class MacroPolicyPricingData(ExactApiSchema):
    rates: list[MacroIndicatorData]


class MacroFedStanceCountsData(ExactApiSchema):
    hawkish: int
    neutral: int
    dovish: int
    mixed: int


class MacroFedInstitutionalStanceData(ExactApiSchema):
    state: Literal["current", "no_call"]
    direction: Literal["hawkish", "neutral", "dovish", "mixed", "no_call"]
    change_from_prior: Literal[
        "more_hawkish",
        "unchanged",
        "more_dovish",
        "mixed_change",
        "no_prior",
        "no_call",
    ]
    reason: str
    analysis_id: str | None


class MacroFedOfficialsDistributionData(ExactApiSchema):
    state: Literal["current", "no_call"]
    window_days: int
    as_of: date | None
    stance_event_counts: MacroFedStanceCountsData
    stance_unique_official_counts: MacroFedStanceCountsData
    not_policy_signal_event_count: int
    uncertain_event_count: int
    analyzed_event_count: int
    unique_official_count: int


class MacroFedAnalysisEvidenceData(ExactApiSchema):
    excerpt: str
    claim: str


class MacroFedTimelineAnalysisData(ExactApiSchema):
    state: Literal["analyzed", "not_analyzed"]
    policy_relevance: Literal["policy_signal", "not_policy_signal", "uncertain", "unknown"]
    stance: Literal["hawkish", "neutral", "dovish", "mixed", "no_call"]
    confidence: float | None
    change_from_prior: (
        Literal[
            "more_hawkish",
            "unchanged",
            "more_dovish",
            "mixed_change",
            "no_prior",
            "no_call",
        ]
        | None
    )
    rationale: str | None
    evidence: list[MacroFedAnalysisEvidenceData]
    analysis_id: str | None
    model_name: str | None
    prompt_version: str | None
    reviewer_disposition: Literal["pass"] | None


class MacroFedTimelineEventData(ExactApiSchema):
    document_id: str
    document_type: str
    title: str
    effective_date: date
    published_at_ms: int
    source_url: str
    speaker_name: str | None
    official_id: str | None
    role_title: str | None
    fomc_voter: bool | None
    analysis: MacroFedTimelineAnalysisData


class MacroFedOfficialData(ExactApiSchema):
    official_id: str
    official_name: str
    role_title: str
    organization: str
    effective_start: date
    effective_end: date | None
    fomc_participant: bool
    fomc_voter: bool
    source_url: str
    role_fact_id: str


class MacroFedRosterData(ExactApiSchema):
    state: Literal["current", "unavailable"]
    reason: Literal["effective_dated_roster_not_ingested"] | None
    officials: list[MacroFedOfficialData]


class MacroFedData(ExactApiSchema):
    institutional_stance: MacroFedInstitutionalStanceData
    officials_distribution: MacroFedOfficialsDistributionData
    timeline: list[MacroFedTimelineEventData]
    roster: MacroFedRosterData


class MacroReleaseObservationData(ExactApiSchema):
    reference_period: str
    scheduled_at_ms: int | None
    actual_value: float | None
    estimate_value: float | None
    prior_value: float | None
    revised_prior_value: float | None
    surprise: float | None
    revision: float | None
    unit: str
    published_at_ms: int | None
    received_at_ms: int
    source_url: str


class MacroOfficialReleaseSummaryData(MacroReleaseObservationData):
    dataset_id: str
    label: str
    observations: list[MacroReleaseObservationData]


class MacroEconomySectionData(ExactApiSchema):
    indicators: list[MacroIndicatorData]
    official_releases: list[MacroOfficialReleaseSummaryData]


class MacroLiquidityFundingData(ExactApiSchema):
    indicators: list[MacroIndicatorData]
    sofr_minus_iorb_bp_history: list[MacroHistoryPointData]


class MacroCreditCycleDimensionData(ExactApiSchema):
    dimension_id: Literal[
        "spread_level_velocity",
        "funding_cost",
        "credit_supply",
        "credit_quality",
    ]
    label: str
    state: Literal[
        "insufficient",
        "stressed",
        "tightening",
        "easing",
        "neutral",
        "expensive",
        "cheap",
        "normal",
        "restrictive",
        "weak_demand",
        "deteriorating",
        "improving",
        "stable",
    ]
    driver: str
    evidence_dataset_ids: list[str]
    conflicts: list[str]


class MacroCreditSpreadLadderData(ExactApiSchema):
    rows: list[MacroIndicatorData]
    tail_gap: float | None
    tail_gap_unit: Literal["basis_points"]


class MacroCreditFundingComparisonData(ExactApiSchema):
    label: str
    corporate_dataset_id: str
    reference_dataset_id: str
    as_of: date
    value_bp: float
    formula_version: Literal["matched_rate_difference_v1"]
    input_fact_ids: list[str]


class MacroCreditFundingCostsData(ExactApiSchema):
    corporate_yields: list[MacroIndicatorData]
    reference_rates: list[MacroIndicatorData]
    comparisons: list[MacroCreditFundingComparisonData]


class MacroCrossAssetSourceSelectionData(ExactApiSchema):
    dataset_id: str
    label: str
    source_role: str
    fact: MacroMarketFactData | MacroIndicatorData | None


class MacroCrossAssetReturnMatrixRowData(ExactApiSchema):
    display_order: int
    group_id: str
    group_label: str
    symbol: str
    label: str
    identity_policy: Literal["separate_source_facts_no_blend"]
    selection_policy: Literal["intraday_latest_and_daily_returns_exact"]
    latest_source: MacroCrossAssetSourceSelectionData
    return_source: MacroCrossAssetSourceSelectionData


class MacroCrossAssetNormalizedPointData(ExactApiSchema):
    date: date
    normalized_value: float


class MacroCrossAssetNormalizedSeriesData(ExactApiSchema):
    display_order: int
    symbol: str
    label: str
    source: MacroCrossAssetSourceSelectionData
    points: list[MacroCrossAssetNormalizedPointData]


class MacroCrossAssetNormalizedGroupData(ExactApiSchema):
    display_order: int
    group_id: str
    label: str
    series: list[MacroCrossAssetNormalizedSeriesData]


class MacroCrossAssetSourceIdentityData(ExactApiSchema):
    display_order: int
    symbol: str
    label: str
    evidence_kind: str
    identity_policy: Literal["separate_source_facts_no_blend"]
    selection_policy: str
    sources: list[MacroCrossAssetSourceSelectionData]


class MacroCreditConfirmationsData(ExactApiSchema):
    return_matrix: list[MacroCrossAssetReturnMatrixRowData]
    source_identity: list[MacroCrossAssetSourceIdentityData]
    positions: list[MacroPositionData]


class MacroVolatilityTermStructureData(ExactApiSchema):
    spot_and_three_month: list[MacroIndicatorData]
    spread_history: list[MacroHistoryPointData]
    official_vx_curve: list[MacroSettlementData]


class MacroVolatilityCrossAssetImpliedData(ExactApiSchema):
    indicators: list[MacroIndicatorData]
    normalized_groups: list[MacroCrossAssetNormalizedGroupData]


class MacroCrossAssetAssetsData(ExactApiSchema):
    return_matrix: list[MacroCrossAssetReturnMatrixRowData]
    normalized_groups: list[MacroCrossAssetNormalizedGroupData]
    source_identity: list[MacroCrossAssetSourceIdentityData]


class MacroCorrelationData(ExactApiSchema):
    left: str
    right: str
    correlation: float | None
    sample_count: int
    window: Literal["up_to_120_daily_returns"]


class MacroSettlementData(ExactApiSchema):
    trade_date: date
    contract_code: str
    contract_expiration_date: date
    settlement_price: float
    open_interest: int | None
    volume: int | None
    published_at_ms: int | None
    received_at_ms: int
    source_url: str


class MacroCrossAssetFuturesData(ExactApiSchema):
    return_matrix: list[MacroCrossAssetReturnMatrixRowData]
    positions: list[MacroPositionData]


class MacroRatesFedPersistedData(ExactApiSchema):
    schema_version: Literal["macro_rates_fed_v6"]
    module_id: Literal["rates_fed"]
    label: str
    status: MacroModuleStatusData
    latest_fact_at_ms: int
    next_checkpoints: list[MacroNextCheckpointData]
    evidence: MacroModuleEvidenceData
    decision: MacroRatesDecisionData
    curve: MacroCurveData
    policy_pricing: MacroPolicyPricingData
    fed: MacroFedData
    positioning: list[MacroPositionData]


class MacroRatesFedReadData(MacroRatesFedPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None
    thesis_context: MacroModuleThesisContextData


class MacroEconomyInflationPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_economy_inflation_v5"]
    module_id: Literal["economy_inflation"]
    inflation: MacroEconomySectionData
    labor: MacroEconomySectionData
    growth: MacroEconomySectionData


class MacroEconomyInflationReadData(MacroEconomyInflationPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None
    thesis_context: MacroModuleThesisContextData


class MacroLiquidityFundingPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_liquidity_funding_v5"]
    module_id: Literal["liquidity_funding"]
    balance_sheet: MacroIndicatorSectionData
    funding: MacroLiquidityFundingData


class MacroLiquidityFundingReadData(MacroLiquidityFundingPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None
    thesis_context: MacroModuleThesisContextData


class MacroCreditPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_credit_v7"]
    module_id: Literal["credit"]
    cycle_dimensions: list[MacroCreditCycleDimensionData]
    spread_ladder: MacroCreditSpreadLadderData
    funding_costs: MacroCreditFundingCostsData
    bank_lending: MacroIndicatorSectionData
    loan_quality: MacroIndicatorSectionData
    confirmations: MacroCreditConfirmationsData


class MacroCreditReadData(MacroCreditPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None
    thesis_context: MacroModuleThesisContextData


class MacroVolatilityPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_volatility_v7"]
    module_id: Literal["volatility"]
    term_structure: MacroVolatilityTermStructureData
    cross_asset_implied: MacroVolatilityCrossAssetImpliedData


class MacroVolatilityReadData(MacroVolatilityPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None
    thesis_context: MacroModuleThesisContextData


class MacroCrossAssetPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_cross_asset_v7"]
    module_id: Literal["cross_asset"]
    assets: MacroCrossAssetAssetsData
    correlations: list[MacroCorrelationData]
    futures: MacroCrossAssetFuturesData


class MacroCrossAssetReadData(MacroCrossAssetPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None
    thesis_context: MacroModuleThesisContextData


class MacroModuleUnavailableData(ExactApiSchema):
    schema_version: Literal["macro_module_unavailable_v1"] = "macro_module_unavailable_v1"
    module_id: str
    label: str
    availability: Literal["unavailable"] = "unavailable"
    reason: MacroReason
    href: str
    thesis_context: MacroModuleThesisContextData


class MacroModuleSummaryData(ExactApiSchema):
    module_id: str
    label: str
    availability: Literal["available", "unavailable"]
    reason: MacroReason | None
    role: Literal[
        "driver",
        "confirming",
        "contradicting",
        "uncertain",
        "not_material",
        "unassessed",
    ]
    coverage_state: Literal["complete", "partial"] | None
    current_health_state: Literal["current", "degraded", "unavailable"] | None
    history_depth_state: Literal["complete", "partial", "insufficient", "not_required"] | None
    backfill_execution: MacroBackfillExecutionData | None
    latest_fact_at_ms: int | None
    summary: MacroOverviewModuleSummaryStateData | None
    coverage_gap_count: int
    current_health_gap_count: int
    history_gap_count: int
    href: str
    thesis_context: MacroModuleThesisContextData


class MacroTransportStateData(ExactApiSchema):
    state: Literal["current", "stale"]
    last_successful_read_at_ms: int
    reason: MacroReason | None


class MacroDataQualityOverviewData(ExactApiSchema):
    coverage_state: Literal["complete", "partial"]
    current_health_state: Literal["current", "degraded", "unavailable"]
    history_depth_state: Literal["complete", "partial", "insufficient", "not_required"]
    coverage_gap_count: int
    current_health_gap_count: int
    history_gap_count: int


class MacroOverviewReadData(ExactApiSchema):
    schema_version: Literal["macro_overview_v8"]
    read_at_ms: int
    transport: MacroTransportStateData
    session_date: date
    cutoff_ms: int
    latest_fact_at_ms: int
    thesis_state: Literal[
        "published",
        "pending",
        "running",
        "retryable",
        "failed",
        "config_error",
        "not_published",
        "missing",
    ]
    thesis_reason: MacroReason | None
    thesis: MacroThesisV2 | None
    run: MacroThesisRunData | None
    live_delta: MacroLiveDeltaV2 | None
    outcome_replay: MacroOutcomeReplayV2 | None
    recovery: list[MacroRecoveryItem]
    modules: list[MacroModuleSummaryData]
    data_quality: MacroDataQualityOverviewData

    @model_validator(mode="after")
    def validate_current_contract(self) -> MacroOverviewReadData:
        if (self.thesis_state == "published") != (self.thesis is not None):
            raise ValueError("macro_overview_v8_publication_state_mismatch")
        if self.thesis is not None and self.thesis.session_date != self.session_date:
            raise ValueError("macro_overview_v8_current_session_mismatch")
        return self


class MacroThesisRunData(ExactApiSchema):
    session_date: date
    status: str
    evidence_pack_id: str
    research_input_id: str | None
    attempt_count: int
    max_attempts: int
    error_code: str | None
    gate_category: (
        Literal[
            "time_identity",
            "evidence_closure",
            "contract_validity",
            "write_safety",
        ]
        | None
    )
    candidate_hash: str | None
    reason: MacroReason | None
    updated_at_ms: int


class MacroPublicationHistoryItemData(ExactApiSchema):
    schema_version: Literal["macro_publication_history_item_v2"] = "macro_publication_history_item_v2"
    publication_schema_version: Literal["macro_thesis_v1", "macro_thesis_v2"]
    publication_id: str
    session_date: date
    cutoff_ms: int
    published_at_ms: int
    title: str
    stance: Literal["call", "no_call"]
    confidence: Literal["low", "medium", "high"] | None
    horizon: Literal["1w", "1m", "1w_to_1m"]


class MacroThesisDetailReadData(ExactApiSchema):
    schema_version: Literal["macro_thesis_detail_v4"]
    state: Literal[
        "published",
        "pending",
        "running",
        "retryable",
        "config_error",
        "not_published",
        "failed",
        "missing",
    ]
    session_date: date
    cutoff_ms: int
    reason: MacroReason | None
    thesis: MacroThesisV2 | None
    live_delta: MacroLiveDeltaV2 | None
    outcome_replay: MacroOutcomeReplayV2 | None
    recovery: list[MacroRecoveryItem]
    run: MacroThesisRunData | None
    history: list[MacroPublicationHistoryItemData]

    @model_validator(mode="after")
    def validate_current_contract(self) -> MacroThesisDetailReadData:
        if (self.state == "published") != (self.thesis is not None):
            raise ValueError("macro_thesis_detail_v4_publication_state_mismatch")
        if self.thesis is not None and self.thesis.session_date != self.session_date:
            raise ValueError("macro_thesis_detail_v4_current_session_mismatch")
        return self


class MacroThesisArchiveDetailReadData(ExactApiSchema):
    schema_version: Literal["macro_thesis_archive_detail_v2"] = "macro_thesis_archive_detail_v2"
    state: Literal["historical", "missing"]
    requested_session_date: date
    current_session_date: date
    reason: MacroReason | None
    thesis: MacroThesisV1 | MacroThesisV2 | None
    recovery: list[MacroRecoveryItem]
    run: MacroThesisRunData | None
    history: list[MacroPublicationHistoryItemData]

    @model_validator(mode="after")
    def validate_archive_contract(self) -> MacroThesisArchiveDetailReadData:
        if (self.state == "historical") != (self.thesis is not None):
            raise ValueError("macro_thesis_archive_v2_state_mismatch")
        if self.thesis is not None and self.thesis.session_date != self.requested_session_date:
            raise ValueError("macro_thesis_archive_v2_session_mismatch")
        return self


class SearchPageData(ExactApiSchema):
    returned_count: int
    has_more: bool
    next_cursor: str | None


class RecentData(ExactApiSchema):
    events: list[JsonObject]
    items: list[JsonObject]
    page: SearchPageData


class SearchData(ExactApiSchema):
    query: JsonObject
    page: SearchPageData
    target_candidates: list[JsonObject]
    items: list[JsonObject]


class SearchInspectQueryData(ExactApiSchema):
    q: str
    normalized_q: str
    window: str
    result_kind: Literal["token_result", "topic_result", "ambiguous_result", "empty_result"]


class SearchInspectResolverData(ExactApiSchema):
    target_candidates: list[JsonObject]
    selected_target: JsonObject | None
    reasons: list[str]


class SearchInspectTopicSummaryData(ExactApiSchema):
    posts: int
    authors: int


class SearchInspectTopicData(ExactApiSchema):
    summary: SearchInspectTopicSummaryData
    items: list[JsonObject]


class SearchInspectAmbiguousData(ExactApiSchema):
    candidates: list[JsonObject]
    summary: SearchInspectTopicSummaryData
    items: list[JsonObject]


class SearchInspectData(ExactApiSchema):
    query: SearchInspectQueryData
    resolver: SearchInspectResolverData
    token_result: TokenCaseData | None
    topic_result: SearchInspectTopicData | None
    ambiguous_result: SearchInspectAmbiguousData | None


class TokenCaseData(ExactApiSchema):
    target: JsonObject
    profile: JsonObject | None
    timeline: JsonObject
    posts: JsonObject
    market_live: JsonObject
    current_radar: TokenRadarFactRowData | None


class TokenRadarIntentData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    intent_id: str
    event_id: str
    display_symbol: str | None = None
    display_name: str | None = None
    evidence: list[Any]


class TokenRadarMetaData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    lane: str | None = None
    rank: int | None = None
    listed_at_ms: int | None = None
    computed_at_ms: int | None = None
    source_max_received_at_ms: int | None = None


class TokenRadarResolutionData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    status: str
    target_type: str | None
    target_id: str | None
    pricefeed_id: str | None
    reason_codes: list[str]
    candidate_ids: list[str]
    lookup_keys: list[str]
    discovery: list[JsonObject]


class TokenRadarQualityData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    status: str
    degraded_reasons: list[str]


class TokenFactorSubjectData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    target_type: str | None
    target_id: str | None
    symbol: str | None
    target_market_type: str | None
    chain: str | None
    address: str | None
    pricefeed_id: str | None


class TokenFactorMarketReadinessData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    anchor_status: str
    latest_status: str
    dex_floor_status: str
    missing_fields: list[str]
    stale_fields: list[str]


class TokenFactorMarketData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    event_anchor: JsonObject | None
    decision_latest: JsonObject | None
    readiness: TokenFactorMarketReadinessData
    capture_method: str | None = None
    capture_reason: str | None = None
    tick_lag_ms: int | float | None = None


class TokenFactorFamilyData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    raw_score: int | float
    score: int | float
    weight: int | float
    data_health: str
    facts: JsonObject
    factors: JsonObject


class TokenFactorFamiliesData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    social_heat: TokenFactorFamilyData
    social_propagation: TokenFactorFamilyData
    timing_risk: TokenFactorFamilyData


class TokenFactorGatesData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    eligible_for_high_alert: bool
    max_decision: Literal["discard", "watch", "high_alert"]
    blocked_reasons: list[str]
    risk_reasons: list[str]


class TokenFactorFamilyValuesData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    social_heat: int | float
    social_propagation: int | float
    timing_risk: int | float


class TokenFactorRankValuesData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    social_heat: int | float | None
    social_propagation: int | float | None
    timing_risk: int | float | None


class TokenFactorNormalizationData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    status: str
    cohort_status: str
    cohort: JsonObject
    factor_ranks: TokenFactorRankValuesData
    alpha_rank: int | float | None


class TokenFactorCompositeData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    raw_alpha_score: int | float
    rank_score: int | float
    family_scores: TokenFactorFamilyValuesData
    recommended_decision: Literal["discard", "watch", "high_alert"]


class TokenFactorProvenanceData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    source_event_ids: list[str]
    computed_at_ms: int


class TokenFactorSnapshotData(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["token_factor_snapshot_v5_provider_neutral"]
    subject: TokenFactorSubjectData
    market: TokenFactorMarketData
    gates: TokenFactorGatesData
    data_health: JsonObject
    families: TokenFactorFamiliesData
    normalization: TokenFactorNormalizationData
    composite: TokenFactorCompositeData
    provenance: TokenFactorProvenanceData


class TokenRadarFactRowData(ExactApiSchema):
    model_config = ConfigDict(extra="forbid")

    intent: TokenRadarIntentData
    radar: TokenRadarMetaData
    resolution: TokenRadarResolutionData
    quality: TokenRadarQualityData
    factor_snapshot: TokenFactorSnapshotData


class TokenRadarRowData(TokenRadarFactRowData):
    profile: JsonObject | None = None


class TokenRadarAnchorCoverageData(ExactApiSchema):
    status: str
    ready: int
    missing: int
    total: int


class TokenRadarUnresolvedData(ExactApiSchema):
    identity_missing_count: int
    nil_count: int
    ambiguous_count: int
    sample_symbols: list[str]


class TokenRadarProjectionData(ExactApiSchema):
    status: Literal["fresh", "stale", "pending", "failed"]
    version: str
    source: Literal["token_radar_current_rows"]
    venue: str
    reason: str | None
    latest_attempt_status: str
    row_count: int
    source_rows: int
    source_max_received_at_ms: int
    source_frontier_ms: int | None
    computed_at_ms: int | None
    error: str | None
    anchor_coverage: TokenRadarAnchorCoverageData
    quality_status: Literal["ready", "degraded", "insufficient", "failed"]
    degraded_reasons: list[str]
    unresolved: TokenRadarUnresolvedData


class TokenRadarData(ExactApiSchema):
    window: str
    venue: str
    targets: list[TokenRadarRowData]
    attention: list[TokenRadarRowData]
    projection: TokenRadarProjectionData


class StocksRadarQueryData(ExactApiSchema):
    window: str
    limit: int
    window_start_ms: int
    window_end_ms: int


class StocksRadarTargetData(ExactApiSchema):
    target_type: Literal["MarketInstrument"]
    target_id: str | None
    symbol: str | None
    market: Literal["us_equity"]
    exchange: str | None
    instrument_type: str | None
    name: str | None


class StocksRadarAttentionData(ExactApiSchema):
    mentions: int
    unique_authors: int
    latest_seen_ms: int | None


class StocksRadarLatestEventData(ExactApiSchema):
    event_id: str | None
    author_handle: str | None
    text: str | None
    received_at_ms: int | None


class StocksRadarQuoteData(ExactApiSchema):
    status: Literal["ready", "unavailable"]
    price: int | float | None
    reference_close_price: int | float | None
    change_pct: int | float | None
    asof: str | None
    provider: str | None
    provider_symbol: str | None
    latency_class: str | None
    freshness_class: str | None
    error: str | None


class StocksRadarRowData(ExactApiSchema):
    target: StocksRadarTargetData
    attention: StocksRadarAttentionData
    latest_event: StocksRadarLatestEventData
    quote: StocksRadarQuoteData
    source_event_ids: list[str]
    row_health: list[str]


class StocksRadarHealthData(ExactApiSchema):
    returned_count: int
    quote_ready_count: int
    quote_unavailable_count: int


class StocksRadarData(ExactApiSchema):
    window: str
    query: StocksRadarQueryData
    rows: list[StocksRadarRowData]
    health: StocksRadarHealthData


class LiveMarketData(ExactApiSchema):
    target_type: str
    target_id: str
    status: Literal["live", "stale", "missing"]
    price_usd: float | None
    price_quote: float | None
    quote_symbol: str | None
    price_basis: Literal["usd", "quote_as_usd", "unavailable"]
    market_cap_usd: float | None
    liquidity_usd: float | None
    holders: int | None
    volume_24h_usd: float | None
    open_interest_usd: float | None
    observed_at_ms: int | None
    received_at_ms: int | None
    age_ms: int | None
    provider: str | None


class TargetPostsQueryData(ExactApiSchema):
    target_type: str
    target_id: str
    window: str
    post_range: str = Field(alias="range")


class TargetPostsScoreWindowData(ExactApiSchema):
    window: str


class TargetPostsData(ExactApiSchema):
    query: TargetPostsQueryData
    score_window: TargetPostsScoreWindowData
    total_count: int
    returned_count: int
    has_more: bool
    next_cursor: str | None
    items: list[JsonObject]


class TargetSocialTimelineQueryData(ExactApiSchema):
    target_type: str
    target_id: str
    window: str
    bucket: str


class TargetSocialTimelineData(ExactApiSchema):
    query: TargetSocialTimelineQueryData
    summary: JsonObject
    market_candles: JsonObject | None
    stages: list[JsonObject]
    buckets: list[JsonObject]
    authors: list[JsonObject]
    posts: list[JsonObject]
    cascade: JsonObject
    returned_count: int
    has_more: bool
    next_cursor: str | None


class SourceEventDetail(ExactApiSchema):
    event_id: str
    timestamp_ms: int
    source_provider: str
    channel: str
    action: str
    author_handle: str | None
    author_name: str | None
    author_followers: int | None
    text_clean: str | None
    canonical_url: str | None


class SourceEventsByIdsData(ExactApiSchema):
    events: list[SourceEventDetail]
    not_found: list[str]
