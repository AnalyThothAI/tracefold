from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tracefold.macro import (
    MacroModuleId,
    MacroReason,
    MacroSeasonalAdjustment,
    MacroSourceRole,
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


class StatusRuntimeData(ExactApiSchema):
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
    db: StatusDatabaseData
    workers_runtime: WorkersRuntimeData


class StatusData(ExactApiSchema):
    measured_at_ms: int
    runtime: StatusRuntimeData


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
    source_role: MacroSourceRole
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
    source_role: MacroSourceRole
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
    source_role: MacroSourceRole
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
    current_health: Literal["current", "degraded", "unavailable"]
    history_depth: Literal["complete", "partial", "insufficient", "not_required"]
    reason: MacroReason | None
    next_check_at_ms: int | None


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


class MacroFedMeetingData(ExactApiSchema):
    meeting_id: str
    start_date: date
    end_date: date
    has_sep: bool
    calendar_published_at_ms: int | None
    received_at_ms: int
    source_url: str


class MacroFedMeetingCalendarData(ExactApiSchema):
    revision_id: str | None
    meetings: list[MacroFedMeetingData]


class MacroFedData(ExactApiSchema):
    meeting_calendar: MacroFedMeetingCalendarData
    institutional_stance: MacroFedInstitutionalStanceData
    officials_distribution: MacroFedOfficialsDistributionData
    timeline: list[MacroFedTimelineEventData]
    roster: MacroFedRosterData


class MacroTreasuryAuctionResultData(ExactApiSchema):
    auction_id: str
    cusip: str
    security_term: str
    auction_date: date
    scheduled_at_ms: int | None
    published_at_ms: int | None
    received_at_ms: int
    source_url: str
    bid_to_cover_ratio: float | None
    high_yield_pct: float | None
    high_discount_rate_pct: float | None
    high_investment_rate_pct: float | None
    offering_amount_usd: float | None
    indirect_award_share_pct: float | None
    direct_award_share_pct: float | None
    primary_dealer_award_share_pct: float | None


class MacroTreasuryAuctionsData(ExactApiSchema):
    recent_results: list[MacroTreasuryAuctionResultData]


class MacroDocumentAnalysisRuntimeData(ExactApiSchema):
    state: Literal["disabled", "unconfigured", "active"]
    enabled: bool
    configured: bool
    worker_active: bool = Field(
        description="Configuration admission (enabled and configured), not observed worker process liveness."
    )
    model: str


class MacroReleaseObservationData(ExactApiSchema):
    reference_period: str
    seasonal_adjustment: MacroSeasonalAdjustment
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
    source_role: MacroSourceRole
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


MacroCorrelationWindow = Literal[
    "30_daily_returns",
    "90_daily_returns",
    "252_daily_returns",
]


class MacroCorrelationContractData(ExactApiSchema):
    default_window: MacroCorrelationWindow
    supported_windows: list[MacroCorrelationWindow]
    minimum_common_observations: int
    presentation_derivation: Literal["undirected_pairs_mirrored_with_unit_diagonal"]


class MacroCorrelationData(ExactApiSchema):
    left: str
    right: str
    correlation: float | None
    sample_count: int
    window: MacroCorrelationWindow


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
    schema_version: Literal["macro_rates_fed_v8"]
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
    treasury_auctions: MacroTreasuryAuctionsData
    positioning: list[MacroPositionData]


class MacroRatesFedReadData(MacroRatesFedPersistedData):
    document_analysis_runtime: MacroDocumentAnalysisRuntimeData
    availability: Literal["available"]
    reason: MacroReason | None


class MacroEconomyInflationPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_economy_inflation_v6"]
    module_id: Literal["economy_inflation"]
    inflation: MacroEconomySectionData
    labor: MacroEconomySectionData
    growth: MacroEconomySectionData


class MacroEconomyInflationReadData(MacroEconomyInflationPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None


class MacroLiquidityFundingPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_liquidity_funding_v5"]
    module_id: Literal["liquidity_funding"]
    balance_sheet: MacroIndicatorSectionData
    funding: MacroLiquidityFundingData


class MacroLiquidityFundingReadData(MacroLiquidityFundingPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None


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


class MacroVolatilityPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_volatility_v7"]
    module_id: Literal["volatility"]
    term_structure: MacroVolatilityTermStructureData
    cross_asset_implied: MacroVolatilityCrossAssetImpliedData


class MacroVolatilityReadData(MacroVolatilityPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None


class MacroCrossAssetPersistedData(_MacroModulePersistedBaseData):
    schema_version: Literal["macro_cross_asset_v8"]
    module_id: Literal["cross_asset"]
    assets: MacroCrossAssetAssetsData
    correlation_contract: MacroCorrelationContractData
    correlations: list[MacroCorrelationData]
    futures: MacroCrossAssetFuturesData


class MacroCrossAssetReadData(MacroCrossAssetPersistedData):
    availability: Literal["available"]
    reason: MacroReason | None


class MacroModuleUnavailableData(ExactApiSchema):
    schema_version: Literal["macro_module_unavailable_v1"] = "macro_module_unavailable_v1"
    module_id: MacroModuleId
    label: str
    availability: Literal["unavailable"] = "unavailable"
    reason: MacroReason
    href: str


class MacroModuleSummaryData(ExactApiSchema):
    module_id: MacroModuleId
    label: str
    availability: Literal["available", "unavailable"]
    reason: MacroReason | None
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
    schema_version: Literal["macro_overview_v9"]
    read_at_ms: int
    transport: MacroTransportStateData
    latest_fact_at_ms: int
    modules: list[MacroModuleSummaryData]
    data_quality: MacroDataQualityOverviewData
