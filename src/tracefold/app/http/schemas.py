from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class NewsHealthLayerData(ExactApiSchema):
    status: Literal["warming", "ready", "degraded"]
    model_config = ConfigDict(extra="allow")


class NewsHealthLayersData(ExactApiSchema):
    ingest: NewsHealthLayerData
    story: NewsHealthLayerData
    brief: NewsHealthLayerData


class NewsHealthData(ExactApiSchema):
    status: Literal["warming", "ready", "degraded"]
    reasons: list[str]
    layers: NewsHealthLayersData
    measured_at_ms: int


class StatusData(ExactApiSchema):
    ok: bool
    reasons: list[str]
    store: Literal["postgresql"]
    snapshot_gate: JsonObject
    db: JsonObject
    provider_states: dict[str, JsonObject]
    workers: dict[str, WorkerStatusData]
    news: NewsHealthData


class ReadinessData(ExactApiSchema):
    ok: bool
    reasons: list[str]
    store: Literal["postgresql"]
    db: JsonObject
    composition: JsonObject


class WorkerStatusData(ExactApiSchema):
    enabled: bool
    running: bool
    effective_status: Literal[
        "disabled",
        "intentionally_not_started",
        "unavailable",
        "degraded",
        "running",
        "stopped",
        "failed",
    ]
    unavailable_reason: str | None
    last_started_at_ms: int | None
    last_finished_at_ms: int | None
    last_result: JsonObject | None
    last_error: str | None
    iteration_duration_p99_ms: float | None


class MacroResearchSectionData(ExactApiSchema):
    section_id: str
    title: str
    body_markdown: str
    citation_ids: list[str]


class MacroResearchEvidenceGapData(ExactApiSchema):
    gap_id: str
    summary: str
    details: str | None = None
    citation_ids: list[str] = Field(default_factory=list)


class MacroResearchCitationData(ExactApiSchema):
    citation_id: str
    source_type: str
    source_ref: str
    source_label: str
    observed_at: date | None = None
    published_at_ms: int | None = None
    available_at_ms: int | None = None
    source_url: str | None = None
    lineage: JsonObject = Field(default_factory=dict)


class MacroResearchPublicationData(ExactApiSchema):
    schema_version: str
    session_date: date
    market_cutoff_ms: int
    title: str
    executive_summary: str
    sections: list[MacroResearchSectionData]
    evidence_gaps: list[MacroResearchEvidenceGapData]
    citations: list[MacroResearchCitationData]
    reviewer_disposition: Literal["pass", "revise", "block"]
    reviewer_notes: list[str]
    audit: JsonObject
    published_at_ms: int | None = None
    evidence_pack_id: str


class MacroResearchRunData(ExactApiSchema):
    session_date: date
    evidence_pack_id: str
    status: str
    attempt_count: int
    max_attempts: int
    last_error: str | None
    updated_at_ms: int


class MacroResearchReadData(ExactApiSchema):
    state: Literal["current", "historical", "generating", "failed", "missing"]
    requested_session_date: date
    current_session_date: date
    publication: MacroResearchPublicationData | None
    run: MacroResearchRunData | None


class MacroCoverageCapabilityData(ExactApiSchema):
    capability_id: str
    label: str
    requirement: Literal["required", "supporting", "licensed_unavailable"]
    state: Literal["available", "missing", "licensed_unavailable"]
    dataset_ids: list[str]
    reason: str | None


class MacroCoverageData(ExactApiSchema):
    state: Literal["complete", "partial", "licensed_unavailable"]
    expected_capabilities: int
    available_capabilities: int
    capabilities: list[MacroCoverageCapabilityData]


class MacroDataHealthData(ExactApiSchema):
    state: Literal["current", "delayed", "stale", "invalid", "backfilling", "unavailable"]
    current_datasets: int
    tracked_datasets: int
    as_of_ms: int


class MacroJudgmentStateData(ExactApiSchema):
    state: Literal["current", "missing", "blocked"]
    cutoff_ms: int | None


class MacroModuleStatusData(ExactApiSchema):
    coverage: MacroCoverageData
    data_health: MacroDataHealthData
    judgment: MacroJudgmentStateData


class MacroModuleSummaryStateData(ExactApiSchema):
    headline: str
    interpretation: str
    top_changes: list[MacroChangeData]


class MacroModuleEvidenceData(ExactApiSchema):
    dataset_states: list[MacroDatasetStateData]
    latest_facts: list[MacroEvidenceFactData]


class MacroChangeData(ExactApiSchema):
    dataset_id: str
    label: str
    as_of: str | None
    value: float
    unit: str
    change_1w: float | None
    change_1m: float | None
    magnitude: float
    source_url: str


class MacroDatasetStateData(ExactApiSchema):
    dataset_id: str
    label: str
    state: Literal["current", "delayed", "stale", "invalid", "backfilling", "unavailable"]
    reason: str
    critical: bool
    trust_tier: Literal["official", "exchange", "untrusted_proxy"]
    source_url: str
    latest_reference: str | None
    latest_received_at_ms: int | None


class MacroEvidenceFactData(ExactApiSchema):
    dataset_id: str
    series_id: str | None = None
    contract_code: str | None = None
    fact_ref: str | None
    reference: str | None
    value: float | str | None
    unit: str
    published_at_ms: int | None
    received_at_ms: int
    source_url: str


class MacroNextCheckpointData(ExactApiSchema):
    dataset_id: str
    label: str
    state: str
    next_check: str


class MacroHistoryPointData(ExactApiSchema):
    date: str
    value: float


class MacroIndicatorData(ExactApiSchema):
    dataset_id: str
    label: str
    latest_value: float
    unit: str
    as_of: str
    change_1w: float | None
    change_1m: float | None
    sample_count: int
    history_start: str
    history_end: str
    source_url: str
    percentile: float | None = None
    history: list[MacroHistoryPointData]


class MacroAssetData(ExactApiSchema):
    dataset_id: str
    symbol: str
    label: str
    instrument_type: str
    asset_class: str
    latest_value: float
    unit: str
    as_of: str | None
    market_time_ms: int
    change_1d_pct: float | None
    change_1w_pct: float | None
    change_1m_pct: float | None
    trust_tier: Literal["official", "exchange", "untrusted_proxy"]
    source_url: str


class _MacroModuleBaseData(ExactApiSchema):
    label: str
    status: MacroModuleStatusData
    latest_fact_at_ms: int
    summary: MacroModuleSummaryStateData
    contradictions: list[str]
    falsifiers: list[str]
    next_checkpoints: list[MacroNextCheckpointData]
    evidence: MacroModuleEvidenceData


class MacroCurvePointData(ExactApiSchema):
    tenor: str
    years: float
    yield_pct: float


class MacroCurveSnapshotData(ExactApiSchema):
    window: Literal["current", "1w", "1m", "3m"]
    as_of: str
    points: list[MacroCurvePointData]


class MacroBreakevenPointData(ExactApiSchema):
    tenor: str
    years: float
    breakeven_pct: float


class MacroBreakevenSnapshotData(ExactApiSchema):
    window: Literal["current", "1w", "1m", "3m"]
    as_of: str
    points: list[MacroBreakevenPointData]


class MacroCurveSpreadPointData(ExactApiSchema):
    date: str
    value_bp: float


class MacroCurveClassificationData(ExactApiSchema):
    state: str
    label: str
    formula_version: str
    inputs: JsonObject


class MacroRatesCurveData(ExactApiSchema):
    nominal_snapshots: list[MacroCurveSnapshotData]
    real_snapshots: list[MacroCurveSnapshotData]
    breakeven_snapshots: list[MacroBreakevenSnapshotData]
    spreads: dict[str, list[MacroCurveSpreadPointData]]
    classification: MacroCurveClassificationData


class MacroUnavailableData(ExactApiSchema):
    state: Literal["licensed_unavailable"]
    reason: str


class MacroPolicyPricingData(ExactApiSchema):
    rates: list[MacroIndicatorData]
    cme_policy_probabilities: MacroUnavailableData


class MacroFedInstitutionalStanceData(ExactApiSchema):
    state: str
    direction: str
    change_from_prior: str
    reason: str


class MacroFedOfficialsDistributionData(ExactApiSchema):
    state: str
    window_days: int
    as_of: str | None
    hawkish: int
    neutral: int
    dovish: int
    mixed: int
    not_policy_signal: int
    uncertain: int
    analyzed_events: int


class MacroFedEvidenceData(ExactApiSchema):
    excerpt: str
    claim: str


class MacroFedTimelineAnalysisData(ExactApiSchema):
    state: str
    policy_relevance: str
    stance: str
    confidence: float | None
    change_from_prior: str | None
    evidence: list[MacroFedEvidenceData]
    analysis_id: str | None
    model_name: str | None
    prompt_version: str | None
    reviewer_disposition: str | None


class MacroFedTimelineEventData(ExactApiSchema):
    document_id: str
    document_type: str
    title: str
    effective_date: str
    published_at_ms: int
    source_url: str
    speaker_name: str | None
    official_id: str | None
    role_title: str | None
    fomc_voter: bool | None
    analysis: MacroFedTimelineAnalysisData


class MacroFedRosterOfficialData(ExactApiSchema):
    official_id: str
    official_name: str
    role_title: str
    organization: str
    effective_start: str
    effective_end: str | None
    fomc_participant: bool
    fomc_voter: bool
    source_url: str
    role_fact_id: str


class MacroFedRosterData(ExactApiSchema):
    state: str
    reason: str | None
    officials: list[MacroFedRosterOfficialData]


class MacroFedCommunicationData(ExactApiSchema):
    institutional_stance: MacroFedInstitutionalStanceData
    officials_distribution: MacroFedOfficialsDistributionData
    timeline: list[MacroFedTimelineEventData]
    roster: MacroFedRosterData


class MacroRatesFedReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_rates_fed_v2"]
    module_id: Literal["rates_fed"]
    curve: MacroRatesCurveData
    policy_pricing: MacroPolicyPricingData
    fed: MacroFedCommunicationData
    positioning: list[JsonObject]


class MacroIndicatorSectionData(ExactApiSchema):
    indicators: list[MacroIndicatorData]


class MacroReleaseIndicatorSectionData(MacroIndicatorSectionData):
    official_releases: list[JsonObject]


class MacroEconomyInflationReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_economy_inflation_v2"]
    module_id: Literal["economy_inflation"]
    inflation: MacroReleaseIndicatorSectionData
    labor: MacroReleaseIndicatorSectionData
    growth: MacroIndicatorSectionData


class MacroLiquidityFundingReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_liquidity_funding_v2"]
    module_id: Literal["liquidity_funding"]
    balance_sheet: MacroIndicatorSectionData
    funding: MacroIndicatorSectionData


class MacroCreditSpreadLadderData(ExactApiSchema):
    rows: list[MacroIndicatorData]
    tail_gap: float | None
    tail_gap_unit: Literal["basis_points"]


class MacroCreditFundingCostsData(ExactApiSchema):
    corporate_yields: list[MacroIndicatorData]
    reference_rates: list[MacroIndicatorData]
    comparisons: list[MacroFundingComparisonData]


class MacroFundingComparisonData(ExactApiSchema):
    label: str
    corporate_dataset_id: str
    reference_dataset_id: str
    as_of: str
    value_bp: float
    formula_version: str
    input_fact_ids: list[str]


class MacroCreditConfirmationsData(ExactApiSchema):
    etfs: list[MacroAssetData]
    positions: list[JsonObject]
    trace_nav: MacroUnavailableData


class MacroCreditCycleDimensionData(ExactApiSchema):
    dimension_id: Literal[
        "spread_level_velocity",
        "funding_cost",
        "credit_supply",
        "credit_quality",
        "market_liquidity",
    ]
    label: str
    state: str
    driver: str
    evidence_dataset_ids: list[str]
    conflicts: list[str]


class MacroCreditReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_credit_v3"]
    module_id: Literal["credit"]
    cycle_dimensions: list[MacroCreditCycleDimensionData]
    spread_ladder: MacroCreditSpreadLadderData
    funding_costs: MacroCreditFundingCostsData
    bank_lending: MacroIndicatorSectionData
    loan_quality: MacroIndicatorSectionData
    confirmations: MacroCreditConfirmationsData


class MacroVolatilityTermData(ExactApiSchema):
    spot_and_three_month: list[MacroIndicatorData]
    spread_history: list[MacroHistoryPointData]


class MacroVolatilityReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_volatility_v2"]
    module_id: Literal["volatility"]
    term_structure: MacroVolatilityTermData
    cross_asset_implied: MacroIndicatorSectionData


class MacroBenchmarkData(ExactApiSchema):
    label: str
    asset_class: str
    dataset_id: str
    evidence_kind: str
    latest_value: float | None
    unit: str | None
    as_of: str | None
    change_1w: float | None
    change_1m: float | None
    source_url: str | None


class MacroNormalizedAssetPointData(ExactApiSchema):
    symbol: str
    date: str
    normalized_value: float


class MacroCrossAssetsData(ExactApiSchema):
    benchmarks: list[MacroBenchmarkData]
    proxies: list[MacroAssetData]
    normalized: list[MacroNormalizedAssetPointData]


class MacroCorrelationData(ExactApiSchema):
    left: str
    right: str
    correlation: float | None
    sample_count: int
    window: str


class MacroFuturesConfirmationData(ExactApiSchema):
    market: list[MacroAssetData]
    vix_settlements: list[JsonObject]
    positions: list[JsonObject]


class MacroCrossAssetReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_cross_asset_v3"]
    module_id: Literal["cross_asset"]
    assets: MacroCrossAssetsData
    correlations: list[MacroCorrelationData]
    futures: MacroFuturesConfirmationData


class MacroModuleSummaryData(ExactApiSchema):
    module_id: str
    label: str
    coverage_state: Literal["complete", "partial", "licensed_unavailable", "missing"]
    data_health_state: Literal[
        "current",
        "delayed",
        "stale",
        "invalid",
        "backfilling",
        "unavailable",
        "missing",
    ]
    judgment_state: Literal["current", "missing", "blocked"]
    latest_fact_at_ms: int
    summary: MacroModuleSummaryStateData | None
    top_changes: list[JsonObject]
    coverage_gap_count: int
    health_gap_count: int
    href: str


class MacroJudgmentPublicationStatusData(ExactApiSchema):
    session_date: date
    judgment_cutoff_ms: int
    state: Literal["blocked", "current"]
    reason_code: str
    details: JsonObject
    attempted_at_ms: int


class MacroOverviewReadData(ExactApiSchema):
    schema_version: Literal["macro_overview_v3"]
    read_at_ms: int
    judgment_cutoff_ms: int | None
    latest_fact_at_ms: int
    coverage_state: Literal["complete", "partial", "licensed_unavailable"]
    data_health_state: Literal["current", "delayed", "stale", "invalid", "backfilling", "unavailable"]
    judgment_state: Literal["current", "missing", "blocked"]
    judgment_status: MacroJudgmentPublicationStatusData | None
    daily_judgment: JsonObject | None
    modules: list[MacroModuleSummaryData]
    changes_since_judgment: list[JsonObject]
    research: JsonObject


class RecentData(ExactApiSchema):
    events: list[JsonObject]
    items: list[JsonObject]


class SearchPageData(ExactApiSchema):
    returned_count: int
    has_more: bool
    next_cursor: str | None


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
