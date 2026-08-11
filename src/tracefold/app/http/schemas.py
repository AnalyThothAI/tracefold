from __future__ import annotations

import math
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class ProviderOperationalData(ExactApiSchema):
    provider: str
    owned: bool
    status: Literal["ok", "degraded", "inactive"]
    reasons: list[Literal["unowned_backlog", "circuit_open", "source_stale"]]
    freshness: Literal["current", "stale", "no_evidence", "not_applicable"]
    freshness_budget_ms: int | None
    latest_fact_at_ms: int | None
    circuit_status: Literal["open", "closed"] | None
    consecutive_failures: int
    next_probe_at_ms: int | None
    has_backlog: bool


class ProviderOperationsData(ExactApiSchema):
    status: Literal["ok", "degraded", "unavailable"]
    reasons: list[str]
    items: list[ProviderOperationalData]


class StatusData(ExactApiSchema):
    measured_at_ms: int
    runtime: StatusRuntimeData
    providers: ProviderOperationsData


class ReadinessData(ExactApiSchema):
    ok: bool
    reasons: list[str]
    store: Literal["postgresql"]
    db: JsonObject
    composition: JsonObject


NewsLevel = Literal["critical", "high", "medium", "low", "info"]
NewsCategory = Literal[
    "conflict",
    "protest",
    "disaster",
    "diplomatic",
    "economic",
    "terrorism",
    "cyber",
    "health",
    "environmental",
    "military",
    "crime",
    "infrastructure",
    "tech",
    "general",
]
NewsHealthStatus = Literal["ready", "warming", "degraded"]


class NewsProviderCoinData(ExactApiSchema):
    symbol: str
    market_type: str
    match: str | None = None
    score: int | float | None = None
    signal: str | None = None
    grade: str | None = None


class NewsProviderMetadataData(ExactApiSchema):
    score: int | float | None = None
    source: str | None = None
    signal: str | None = None
    grade: str | None = None
    coins: list[NewsProviderCoinData] | None = None


class NewsProviderEvidenceData(ExactApiSchema):
    item_id: str
    url: str | None
    provider_metadata: NewsProviderMetadataData


class NewsImportanceFactorsData(ExactApiSchema):
    severity_level: NewsLevel
    severity_points: int | float
    source_tier: int
    source_points: int | float
    reporting_origin_count: int
    scoring_corroboration_count: int
    corroboration_points: int | float
    recency_points: int | float
    diplomacy_flashpoint_boost: int
    entity_corroboration_boost: int
    total: int


class NewsStoryData(ExactApiSchema):
    story_id: str
    title: str
    description: str
    url: str | None
    source_id: str
    source_name: str
    representative_item_id: str
    scoring_item_id: str
    level: NewsLevel
    category: NewsCategory
    importance_score: int
    importance_factors: NewsImportanceFactorsData
    item_count: int
    source_count: int
    first_published_at_ms: int
    last_published_at_ms: int
    provider_evidence: NewsProviderEvidenceData | None
    push_delivery_state: Literal["pending", "sent", "suppressed", "failed"] | None


class NewsFeedFacetData(ExactApiSchema):
    value: str
    count: int


class NewsFeedSourceFacetData(NewsFeedFacetData):
    label: str


class NewsFeedReportingOriginFacetData(NewsFeedFacetData):
    label: str


class NewsFeedFacetsPageData(ExactApiSchema):
    categories_has_more: bool
    levels_has_more: bool
    sources_has_more: bool
    reporting_origins_has_more: bool


class NewsFeedFacetsData(ExactApiSchema):
    categories: list[NewsFeedFacetData]
    levels: list[NewsFeedFacetData]
    sources: list[NewsFeedSourceFacetData]
    reporting_origins: list[NewsFeedReportingOriginFacetData]
    page: NewsFeedFacetsPageData


class NewsFeedFiltersData(ExactApiSchema):
    category: str | None
    level: str | None
    source_id: str | None
    reporting_origin: str | None
    provider_score_gt: float | None
    q: str | None


class NewsFeedData(ExactApiSchema):
    sort: Literal["importance", "latest"]
    filters: NewsFeedFiltersData
    stories: list[NewsStoryData]
    next_cursor: str | None
    has_more: bool
    facets: NewsFeedFacetsData


class NewsStoryMemberData(ExactApiSchema):
    item_id: str
    provider_record_id: str | None
    provider_metadata: NewsProviderMetadataData
    source_id: str
    source_name: str
    reporting_origin: str
    tier: int
    title: str
    description: str
    url: str | None
    lang: str
    published_at_ms: int
    last_observed_at_ms: int
    level: NewsLevel
    category: NewsCategory
    importance_score: int
    importance_factors: NewsImportanceFactorsData


class NewsStoryMembersPageData(ExactApiSchema):
    returned_count: int
    has_more: bool
    next_cursor: str | None


class NewsStoryDetailData(NewsStoryData):
    canonical_title: str
    members: list[NewsStoryMemberData]
    members_page: NewsStoryMembersPageData


class NewsBriefSourceData(ExactApiSchema):
    title: str
    source: str
    url: str
    published_at_ms: int | None


class NewsBriefStoryLineData(ExactApiSchema):
    n: int
    text: str


PublicInsightsThreatLevel = Literal["critical", "high", "elevated", "moderate"]
PublicInsightsCategory = Literal[
    "conflict",
    "violence",
    "unrest",
    "geopolitical",
    "crisis",
    "natural_disaster",
    "political",
    "economic",
    "general",
]


class NewsBriefTopStoryData(ExactApiSchema):
    story_id: str
    primary_title: str
    primary_source: str
    primary_link: str | None
    primary_published_at_ms: int
    source_count: int
    unique_source_count: int
    sources: list[str]
    last_updated_ms: int
    member_titles: list[str]
    source_tier: int
    upstream_importance_score: int | float
    entity_corroboration: bool
    corroboration_source_count: int
    importance_score: float
    effective_importance_score: float
    is_alert: bool
    threat_level: PublicInsightsThreatLevel
    category: PublicInsightsCategory


class NewsBriefSourceAgeRangeData(ExactApiSchema):
    newest_ms: int
    oldest_ms: int


class NewsBriefSelectionStatsData(ExactApiSchema):
    considered: int
    admissibility_dropped: int
    source_cap_dropped: int
    overflow_dropped: int
    brief_eligible_considered: int
    brief_eligible_promoted: bool


class NewsBriefProvenanceData(ExactApiSchema):
    projection_revision: str
    selector_evaluated_at_ms: int
    selection_stats: NewsBriefSelectionStatsData


NewsBriefFailureCode = Literal[
    "INSIGHTS_SYNTHESIS_PARSE",
    "INSIGHTS_SYNTHESIS_GATE",
    "INSIGHTS_SYNTHESIS_MISSING_CLUSTER",
    "INSIGHTS_SYNTHESIS_PROVIDER",
]


class NewsBriefL1ValidationData(ExactApiSchema):
    failure_code: None
    stripped_citations: int = Field(ge=0)
    line_fallbacks: list[int] = Field(max_length=8)


class NewsBriefL2ValidationData(ExactApiSchema):
    failure_code: NewsBriefFailureCode
    headline_fallback: bool


class NewsBriefNoneValidationData(ExactApiSchema):
    failure_code: NewsBriefFailureCode


class NewsBriefPublicationData(ExactApiSchema):
    publication_id: str
    slot_at_ms: int
    selection_fingerprint: str
    quality: Literal["ok", "degraded"]
    brief_kind: Literal["l1", "l2", "none"]
    world_brief: str
    brief_story_lines: list[NewsBriefStoryLineData]
    top_stories: list[NewsBriefTopStoryData]
    sources: list[NewsBriefSourceData]
    source_age_range: NewsBriefSourceAgeRangeData
    provider: str
    model: str
    prompt_version: str
    workflow_version: str
    composer_version: str
    schema_version: str
    selector_version: str
    identity_version: str
    locale: Literal["en"]
    validation: NewsBriefL1ValidationData | NewsBriefL2ValidationData | NewsBriefNoneValidationData
    provenance: NewsBriefProvenanceData
    published_at_ms: int


class NewsBriefRunData(ExactApiSchema):
    slot_at_ms: int
    status: Literal["due", "running", "completed"]
    model_outcome: Literal["ok", "l2", "none"] | None
    pointer_action: Literal["advance_ok", "advance_degraded", "preserve_lkg", "none"]
    attempt_count: int
    failure_count: int
    next_due_at_ms: int
    lease_expires_at_ms: int | None
    last_error_code: str | None
    updated_at_ms: int
    last_attempt_at_ms: int | None
    completed_at_ms: int | None


class NewsBriefData(ExactApiSchema):
    state: Literal["unavailable", "current", "degraded", "last_known_good"]
    slot_at_ms: int | None
    next_due_at_ms: int
    publication: NewsBriefPublicationData | None
    latest_run: NewsBriefRunData | None


class NewsSourceData(ExactApiSchema):
    source_id: str
    name: str
    source_kind: Literal["rss", "opennews"]
    tier: int
    enabled: bool
    feed_url: str | None
    refresh_interval_seconds: int | None
    next_fetch_at_ms: int | None
    claim_lease_expires_at_ms: int | None
    last_fetch_started_at_ms: int | None
    last_fetch_finished_at_ms: int | None
    live_connected: bool
    last_live_at_ms: int | None
    last_recovery_at_ms: int | None
    last_success_at_ms: int | None
    last_http_status: int | None
    consecutive_failures: int
    last_outcome: str | None
    last_error: str | None
    last_rejection_counts: dict[str, int]
    last_items_seen: int
    last_items_accepted: int


class NewsSourcesPageData(ExactApiSchema):
    returned_count: int
    has_more: bool
    next_cursor: str | None


class NewsSourcesData(ExactApiSchema):
    items: list[NewsSourceData]
    page: NewsSourcesPageData


class NewsOpenNewsStatusData(ExactApiSchema):
    source_id: str
    name: str
    live_connected: bool
    last_live_at_ms: int | None
    last_recovery_at_ms: int | None
    last_outcome: str | None
    last_error: str | None
    last_http_status: int | None
    last_success_at_ms: int | None
    consecutive_failures: int
    last_rejection_counts: dict[str, int]
    last_items_seen: int
    last_items_accepted: int


class NewsRssStatusData(ExactApiSchema):
    enabled: bool
    source_count: int
    successful_source_count: int
    failed_source_count: int
    claimed_source_count: int
    next_due_at_ms: int | None
    latest_success_at_ms: int | None


class NewsIngestStatusData(ExactApiSchema):
    status: NewsHealthStatus
    reasons: list[str]
    rss: NewsRssStatusData
    opennews: NewsOpenNewsStatusData | None


class NewsStoryStatusData(ExactApiSchema):
    status: NewsHealthStatus
    reasons: list[str]
    active_items: int
    active_stories: int
    newest_item_at_ms: int | None
    newest_story_at_ms: int | None
    last_material_change_at_ms: int | None
    invalid_owner_count: int
    invalid_story_aggregate_count: int
    invariant_error_count: int
    identity_version: str
    classifier_version: str
    importance_version: str
    last_attempt_at_ms: int | None
    last_success_at_ms: int | None
    last_error: str | None


class NewsBriefStatusData(ExactApiSchema):
    status: NewsHealthStatus
    reasons: list[str]
    public_state: Literal["unavailable", "current", "degraded", "last_known_good"]
    slot_at_ms: int | None
    next_due_at_ms: int
    publication_id: str | None
    latest_run: NewsBriefRunData | None


class NewsPushTranslation24hData(ExactApiSchema):
    attempted: int
    succeeded: int
    success_ratio: float | None
    latency_p95_ms: int | None
    failure_counts: dict[str, int]
    slo_met: bool | None


class NewsPushDelivery24hData(ExactApiSchema):
    completed: int
    latency_p95_ms: int | None
    over_120s: int
    slo_met: bool | None


class NewsPushStatusData(ExactApiSchema):
    status: Literal["disabled", "ready", "warming", "degraded"]
    reasons: list[str]
    enabled: bool
    feishu_webhook_url_configured: bool
    feishu_signing_secret_configured: bool
    initialized: bool
    baseline_at_ms: int | None
    total_count: int
    suppressed_count: int
    pending_count: int
    retry_count: int
    sent_count: int
    terminal_count: int
    oldest_due_at_ms: int | None
    latest_sent_at_ms: int | None
    latest_error: str | None
    latest_error_at_ms: int | None
    translation_24h: NewsPushTranslation24hData
    delivery_24h: NewsPushDelivery24hData
    measured_at_ms: int


class NewsStatusLayersData(ExactApiSchema):
    ingest: NewsIngestStatusData
    story: NewsStoryStatusData
    brief: NewsBriefStatusData
    push: NewsPushStatusData


class NewsStatusData(ExactApiSchema):
    status: NewsHealthStatus
    operating_state: Literal["live", "recovering", "stalled"]
    last_success_at_ms: int | None
    reasons: list[str]
    layers: NewsStatusLayersData
    measured_at_ms: int


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


class TokenRadarTargetData(ExactApiSchema):
    target_type: Literal["Asset", "CexToken"]
    target_id: str
    symbol: str
    name: str | None
    logo_url: str | None
    chain: str | None
    exchange: str | None
    address: str | None

    @model_validator(mode="after")
    def validate_target(self) -> TokenRadarTargetData:
        prefix = "/api/token-images/"
        if self.logo_url is not None:
            image_id = self.logo_url.removeprefix(prefix)
            if (
                not self.logo_url.startswith(prefix)
                or len(image_id) != 64
                or any(char not in "0123456789abcdef" for char in image_id)
            ):
                raise ValueError("token_radar_logo_url_invalid")
        asset_identity_valid = (
            self.target_type == "Asset"
            and bool(self.chain and self.chain.strip())
            and bool(self.address and self.address.strip())
            and self.exchange is None
        )
        cex_identity_valid = (
            self.target_type == "CexToken"
            and bool(self.exchange and self.exchange.strip())
            and self.chain is None
            and self.address is None
        )
        if not asset_identity_valid and not cex_identity_valid:
            raise ValueError("token_radar_target_identity_invalid")
        return self


class TokenRadarWhyNowData(ExactApiSchema):
    current_mentions: int = Field(ge=0)
    prior_mentions: int = Field(ge=0)
    mention_delta: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_delta(self) -> TokenRadarWhyNowData:
        if self.current_mentions - self.prior_mentions != self.mention_delta:
            raise ValueError("token_radar_mention_delta_invalid")
        return self


class TokenRadarEvidenceData(ExactApiSchema):
    new_independent_author_count: int = Field(ge=0)
    independent_text_count: int = Field(ge=0)
    time_to_nth_author_ms: int = Field(ge=0)
    duplicate_share: float = Field(ge=0, le=1)


class TokenRadarMarketData(ExactApiSchema):
    status: Literal["confirmed", "unavailable"]
    price_change_since_signal: float | None
    price_usd: float | None = Field(gt=0)
    market_cap_usd: float | None = Field(gt=0)
    observed_at_ms: int | None = Field(ge=0)

    @model_validator(mode="after")
    def validate_market_state(self) -> TokenRadarMarketData:
        metrics = (self.price_usd, self.market_cap_usd)
        if any(value is not None and not math.isfinite(value) for value in metrics):
            raise ValueError("token_radar_market_metric_invalid")
        if any(value is not None for value in metrics) and self.observed_at_ms is None:
            raise ValueError("token_radar_market_observation_required")
        if all(value is None for value in metrics) and self.observed_at_ms is not None:
            raise ValueError("token_radar_market_observation_without_metrics")
        if self.status == "confirmed":
            if (
                self.price_change_since_signal is None
                or not math.isfinite(self.price_change_since_signal)
                or self.price_usd is None
                or self.observed_at_ms is None
            ):
                raise ValueError("token_radar_confirmed_market_change_required")
        elif self.price_change_since_signal is not None:
            raise ValueError("token_radar_unavailable_market_change_forbidden")
        return self


class TokenRadarItemData(ExactApiSchema):
    target: TokenRadarTargetData
    trigger_event_id: str
    triggered_at_ms: int = Field(ge=0)
    why_now: TokenRadarWhyNowData
    evidence: TokenRadarEvidenceData
    market: TokenRadarMarketData
    counter_evidence: Literal["market_confirmation_unavailable"] | None


class TokenRadarData(ExactApiSchema):
    schema_version: Literal["token_radar_snapshot_v2"]
    evidence_as_of_ms: int = Field(ge=0)
    eligible_total: int = Field(ge=0)
    items: list[TokenRadarItemData] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_selection_count(self) -> TokenRadarData:
        if self.eligible_total < len(self.items):
            raise ValueError("token_radar_eligible_total_invalid")
        if any(
            item.market.observed_at_ms is not None and item.market.observed_at_ms > self.evidence_as_of_ms
            for item in self.items
        ):
            raise ValueError("token_radar_market_observation_after_evidence")
        return self


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
