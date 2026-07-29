from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tracefold.macro import MacroLiveDeltaV1, MacroOutcomeReplayV1, MacroThesisV1

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


class MacroCoverageCapabilityData(ExactApiSchema):
    capability_id: str
    label: str
    requirement: Literal["required", "supporting"]
    state: Literal["available", "missing"]
    dataset_ids: list[str]
    reason: str | None


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


class MacroModuleStatusData(ExactApiSchema):
    coverage: MacroCoverageData
    current_health: MacroCurrentHealthData
    history_depth: MacroHistoryDepthData


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
    headline: str
    interpretation: str
    top_changes: list[MacroChangeData]


class MacroDatasetStateData(ExactApiSchema):
    dataset_id: str
    concept_id: str
    source_role: str
    label: str
    current_health: Literal["current", "degraded", "unavailable"]
    history_depth: Literal["complete", "partial", "insufficient", "not_required"]
    market_state: Literal["open", "closed", "maintenance", "unknown", "not_applicable"]
    source_state: Literal["healthy", "degraded", "failed", "not_applicable"]
    current_reason: str
    history_reason: str
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
    published_at_ms: int | None
    received_at_ms: int
    source_url: str


class MacroModuleEvidenceData(ExactApiSchema):
    dataset_states: list[MacroDatasetStateData]
    latest_facts: list[MacroEvidenceFactData]
    reconciliation_receipts: list[JsonObject]


class MacroNextCheckpointData(ExactApiSchema):
    dataset_id: str
    label: str
    current_health: str
    history_depth: str
    next_check: str


class _MacroModuleBaseData(ExactApiSchema):
    label: str
    status: MacroModuleStatusData
    latest_fact_at_ms: int
    summary: MacroModuleSummaryStateData
    contradictions: list[str]
    falsifiers: list[str]
    next_checkpoints: list[MacroNextCheckpointData]
    evidence: MacroModuleEvidenceData


class MacroRatesFedReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_rates_fed_v4"]
    module_id: Literal["rates_fed"]
    curve: JsonObject
    policy_pricing: JsonObject
    fed: JsonObject
    positioning: list[JsonObject]


class MacroEconomyInflationReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_economy_inflation_v4"]
    module_id: Literal["economy_inflation"]
    inflation: JsonObject
    labor: JsonObject
    growth: JsonObject


class MacroLiquidityFundingReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_liquidity_funding_v4"]
    module_id: Literal["liquidity_funding"]
    balance_sheet: JsonObject
    funding: JsonObject


class MacroCreditReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_credit_v5"]
    module_id: Literal["credit"]
    cycle_dimensions: list[JsonObject]
    spread_ladder: JsonObject
    funding_costs: JsonObject
    bank_lending: JsonObject
    loan_quality: JsonObject
    confirmations: JsonObject


class MacroVolatilityReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_volatility_v4"]
    module_id: Literal["volatility"]
    term_structure: JsonObject
    cross_asset_implied: JsonObject


class MacroCrossAssetReadData(_MacroModuleBaseData):
    schema_version: Literal["macro_cross_asset_v5"]
    module_id: Literal["cross_asset"]
    assets: JsonObject
    correlations: list[JsonObject]
    futures: JsonObject


class MacroModuleSummaryData(ExactApiSchema):
    module_id: str
    label: str
    role: Literal["driver", "confirming", "contradicting", "uncertain"] | None
    coverage_state: Literal["complete", "partial"]
    current_health_state: Literal["current", "degraded", "unavailable"]
    history_depth_state: Literal["complete", "partial", "insufficient", "not_required"]
    latest_fact_at_ms: int
    summary: MacroModuleSummaryStateData
    coverage_gap_count: int
    current_health_gap_count: int
    history_gap_count: int
    href: str


class MacroTransportStateData(ExactApiSchema):
    state: Literal["current", "stale"]
    last_successful_read_at_ms: int
    reason: str | None


class MacroDataQualityOverviewData(ExactApiSchema):
    coverage_state: Literal["complete", "partial"]
    current_health_state: Literal["current", "degraded", "unavailable"]
    history_depth_state: Literal["complete", "partial", "insufficient", "not_required"]
    coverage_gap_count: int
    current_health_gap_count: int
    history_gap_count: int


class MacroOverviewReadData(ExactApiSchema):
    schema_version: Literal["macro_overview_v5"]
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
    thesis: MacroThesisV1 | None
    live_delta: MacroLiveDeltaV1 | None
    outcome_replay: MacroOutcomeReplayV1 | None
    modules: list[MacroModuleSummaryData]
    data_quality: MacroDataQualityOverviewData


class MacroThesisRunData(ExactApiSchema):
    session_date: date
    status: str
    evidence_pack_id: str
    attempt_count: int
    max_attempts: int
    error_code: str | None
    error_message: str | None
    updated_at_ms: int


class MacroThesisDetailReadData(ExactApiSchema):
    state: Literal[
        "current",
        "historical",
        "generating",
        "not_published",
        "failed",
        "missing",
    ]
    requested_session_date: date
    current_session_date: date
    thesis: MacroThesisV1 | None
    live_delta: MacroLiveDeltaV1 | None
    outcome_replay: MacroOutcomeReplayV1 | None
    run: MacroThesisRunData | None
    history: list[JsonObject]


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
