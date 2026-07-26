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
    handles: list[str]
    replay_limit: int


class StatusData(ExactApiSchema):
    ok: bool
    reasons: list[str]
    handles: list[str]
    store: Literal["postgresql"]
    snapshot_gate: JsonObject
    db: JsonObject
    provider_states: dict[str, JsonObject]
    workers: dict[str, WorkerStatusData]


class ReadinessData(ExactApiSchema):
    ok: bool
    reasons: list[str]
    handles: list[str]
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
    reviewer_notes: list[str]
    audit: JsonObject
    published_at_ms: int | None = None


class MacroResearchRunData(ExactApiSchema):
    session_date: date
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


class MacroLiveCalculationData(ExactApiSchema):
    formula_id: str
    formula: str
    operands: list[str]
    window: Literal["30d", "90d", "1y", "5y"]
    sample_size: int
    result: float | None
    unit: str


class MacroLiveHistoryPointData(ExactApiSchema):
    observed_at: date
    value_numeric: float | None
    source_timestamp: str | None
    received_at_ms: int | None
    source_name: str | None
    series_key: str | None
    source_priority: int | None
    frequency: str | None
    data_quality: str | None
    source_url: str | None


class MacroLiveMetricData(ExactApiSchema):
    concept_key: str
    page_id: (
        Literal[
            "overview",
            "rates-inflation",
            "growth-labor",
            "liquidity-funding",
            "credit",
            "cross-asset",
        ]
        | None
    )
    section_id: str
    section_label: str
    display_label: str
    display_order: int
    summary: bool
    kind: Literal["material", "derived"]
    availability: Literal["available", "missing"]
    value_numeric: float | None
    unit: str | None
    frequency: str | None
    observed_at: date | None
    source_timestamp: str | None
    received_at_ms: int | None
    source_name: str | None
    series_key: str | None
    source_priority: int | None
    data_quality: str | None
    source_url: str | None
    history: list[MacroLiveHistoryPointData]
    calculation: MacroLiveCalculationData | None


class MacroLiveViewData(ExactApiSchema):
    view_id: Literal[
        "overview",
        "rates-inflation",
        "growth-labor",
        "liquidity-funding",
        "credit",
        "cross-asset",
    ]
    title: str
    description: str
    metrics: list[MacroLiveMetricData]
    total_metric_count: int
    available_count: int
    latest_observed_at: date | None
    max_received_at_ms: int | None


class MacroLiveResearchLinkData(ExactApiSchema):
    state: Literal["current", "generating", "failed", "missing"]
    session_date: date
    market_cutoff_ms: int | None
    title: str | None
    executive_summary: str | None
    evidence_gap_summaries: list[str]
    href: Literal["/macro/research"]


class MacroLiveEvidenceReadData(ExactApiSchema):
    schema_version: Literal["macro_live_evidence_v1"]
    view_id: Literal[
        "dashboard",
        "overview",
        "rates-inflation",
        "growth-labor",
        "liquidity-funding",
        "credit",
        "cross-asset",
    ]
    window: Literal["30d", "90d", "1y", "5y"]
    read_at_ms: int
    views: list[MacroLiveViewData]
    unclassified: list[MacroLiveMetricData]
    research: MacroLiveResearchLinkData | None


class RecentData(ExactApiSchema):
    scope: str
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
    scope: str
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

    schema_version: Literal["token_factor_snapshot_v4_transparent_factors"]
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
    scope: str
    venue: str
    targets: list[TokenRadarRowData]
    attention: list[TokenRadarRowData]
    projection: TokenRadarProjectionData


class StocksRadarQueryData(ExactApiSchema):
    window: str
    scope: str
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
    watched_mentions: int
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
    scope: str
    query: StocksRadarQueryData
    rows: list[StocksRadarRowData]
    health: StocksRadarHealthData


class NewsRepresentativeEvidenceData(ExactApiSchema):
    article_id: str
    revision_id: str
    title: str
    source_id: str
    source_name: str
    source_domain: str
    source_published_at_ms: int


class NewsAnalysisAvailabilityData(ExactApiSchema):
    status: Literal["available", "unavailable"]
    publication_id: str | None
    published_at_ms: int | None
    short_conclusion: str | None


class NewsEventCoreData(ExactApiSchema):
    entities: list[str]
    actor_entities: list[str]
    target_entities: list[str]
    actions: list[str]
    event_objects: list[str]
    locations: list[str]
    stages: list[str]
    named_event_keys: list[str]


class NewsBriefEligibilityReasonData(ExactApiSchema):
    eligible: bool
    reasons: list[str]
    version: str


class NewsStorySummaryData(ExactApiSchema):
    story_id: str
    title: str
    snippet: str
    languages: list[str]
    first_seen_at_ms: int
    last_material_evidence_at_ms: int
    last_presentation_at_ms: int
    breaking: bool
    breaking_reason: str
    evidence_posture: Literal[
        "single_origin_reported",
        "independently_corroborated",
        "primary_source_confirmed",
        "contested",
        "corrected",
        "withdrawn",
    ]
    evidence_factors: JsonObject
    lifecycle: Literal["emerging", "developing", "stable", "fading", "dormant", "reactivated"]
    material_evolution_state: str
    impact_score: int
    impact_profile: JsonObject
    priority_score: int
    priority_profile: JsonObject
    primary_member_count: int
    contextual_member_count: int
    independent_origin_count: int
    source_count: int
    brief_eligible: bool
    representative_evidence: NewsRepresentativeEvidenceData
    analysis: NewsAnalysisAvailabilityData


class NewsStoryListData(ExactApiSchema):
    items: list[NewsStorySummaryData]
    next_cursor: str | None


class NewsStoryMembershipData(ExactApiSchema):
    story_id: str
    article_id: str
    revision_id: str
    membership_kind: Literal["primary", "contextual"]
    identity_version: str
    verdict: str
    match_method: str
    match_score: float
    runner_up_margin: float
    match_reason: JsonObject
    content_form: Literal["report", "analysis", "opinion", "live", "static", "unknown"]
    origin_relation: Literal["originating", "independent", "syndicated", "derived", "unresolved"]
    development_relation: Literal["initial", "follow_up", "correction", "background", "retrospective"]
    epistemic_use: Literal["fact_evidence", "context", "viewpoint", "non_evidence"]
    reporting_origin_id: str | None
    origin_confidence: float
    semantics_reason: JsonObject
    admitted_at_ms: int
    updated_at_ms: int


class NewsArticleRevisionData(ExactApiSchema):
    article_id: str
    publisher_organization_id: str
    canonical_url: str
    incarnation_key: str
    first_observation_id: str
    first_seen_at_ms: int
    identity_version: str
    identity_status: Literal["active", "ended", "revision_identity_ambiguous"]
    created_at_ms: int
    updated_at_ms: int
    revision_id: str
    revision_number: int
    title: str
    snippet: str
    source_published_at_ms: int
    observed_at_ms: int
    language: str
    content_hash: str
    material_change_kind: Literal["initial", "title", "summary", "source_time", "content", "correction", "url_reuse"]
    is_current: bool
    observation_id: str
    source_id: str
    raw_url: str
    source_entry_key: str
    source_name: str
    source_domain: str
    source_role: Literal["original_publisher", "wire_service", "official_authority", "trusted_aggregator"]
    trust_tier: Literal["authoritative", "trusted", "standard", "low"]
    source_chain_id: str
    source_publisher_organization_id: str
    membership_kind: Literal["primary", "contextual"]
    content_form: Literal["report", "analysis", "opinion", "live", "static", "unknown"]
    origin_relation: Literal["originating", "independent", "syndicated", "derived", "unresolved"]
    development_relation: Literal["initial", "follow_up", "correction", "background", "retrospective"]
    epistemic_use: Literal["fact_evidence", "context", "viewpoint", "non_evidence"]
    reporting_origin_id: str | None
    origin_confidence: float
    content_snapshot_id: str | None
    content_snapshot_status: str | None
    snapshot_content_hash: str | None
    content_fetched_at_ms: int | None
    content_failure_reason: str | None
    content_extractor_version: str | None
    content_byte_count: int | None


class NewsStoryIdentityDecisionData(ExactApiSchema):
    decision_id: str
    revision_id: str
    article_id: str
    identity_version: str
    selected_story_id: str | None
    verdict: str
    candidates: list[JsonObject]
    decision_reason: JsonObject
    decided_at_ms: int


class NewsStoryMaterialEventData(ExactApiSchema):
    material_event_id: str
    story_id: str
    revision_id: str | None
    event_kind: Literal[
        "first_report",
        "new_independent_origin",
        "material_follow_up",
        "material_correction",
        "conflict_detected",
        "conflict_resolved",
        "retraction",
    ]
    event_factors: JsonObject
    occurred_at_ms: int


class NewsStorySelectionAuditData(ExactApiSchema):
    selection_snapshot_id: str
    selection_fingerprint: str
    policy_version: str
    cutoff_at_ms: int
    status: Literal["planned", "debounced", "publishable", "published", "superseded"]
    critical: bool
    decision: JsonObject


class NewsEvidenceBackedFactData(ExactApiSchema):
    text: str
    evidence_references: list[str]


class NewsConditionalTransmissionData(ExactApiSchema):
    condition: str
    mechanism: str
    possible_effect: str
    confidence: Literal["low", "medium", "high"]


class NewsStoryAnalysisPayloadData(ExactApiSchema):
    what_happened: list[NewsEvidenceBackedFactData]
    why_it_matters: str
    political_impact: str
    economic_market_impact: str
    disagreements_unknowns: list[str]
    transmission_scenarios: list[NewsConditionalTransmissionData]
    next_checkpoint: str


class NewsStoryAnalysisRequestDetailData(ExactApiSchema):
    request_id: str
    story_id: str
    material_evidence_hash: str
    request_kind: Literal["automatic", "on_demand"]
    reason: JsonObject
    status: Literal["pending", "claimed", "published", "failed", "insufficient"]
    requested_at_ms: int
    updated_at_ms: int


class NewsStoryAnalysisPublicationData(ExactApiSchema):
    publication_id: str
    story_id: str
    material_evidence_hash: str
    model: str
    prompt_version: str
    workflow_version: str
    schema_version: str
    locale: str
    payload: NewsStoryAnalysisPayloadData
    evidence_references: list[str]
    receipt: JsonObject
    published_at_ms: int


class NewsStoryAnalysisData(ExactApiSchema):
    status: Literal["available", "unavailable", "pending", "claimed", "failed", "insufficient"]
    request: NewsStoryAnalysisRequestDetailData | None
    current: NewsStoryAnalysisPublicationData | None
    history: list[NewsStoryAnalysisPublicationData]


class NewsStoryDetailData(ExactApiSchema):
    story_id: str
    seed_article_id: str
    identity_version: str
    identity_status: str
    event_core: NewsEventCoreData
    title: str
    snippet: str
    languages: list[str]
    representative_revision_id: str
    first_seen_at_ms: int
    last_material_evidence_at_ms: int
    last_presentation_at_ms: int
    breaking: bool
    breaking_reason: str
    material_evidence_hash: str
    presentation_state_hash: str
    evidence_posture: str
    evidence_factors: JsonObject
    lifecycle: str
    lifecycle_version: str
    material_evolution_state: str
    impact_score: int
    impact_profile: JsonObject
    priority_score: int
    priority_profile: JsonObject
    scoring_version: str
    primary_member_count: int
    contextual_member_count: int
    independent_origin_count: int
    source_count: int
    brief_eligible: bool
    brief_eligibility_reason: NewsBriefEligibilityReasonData
    memberships: list[NewsStoryMembershipData]
    articles: list[NewsArticleRevisionData]
    identity_decisions: list[NewsStoryIdentityDecisionData]
    material_events: list[NewsStoryMaterialEventData]
    selection_audit: list[NewsStorySelectionAuditData]
    analysis: NewsStoryAnalysisData


class NewsStoryAnalysisRequestData(ExactApiSchema):
    request_id: str
    story_id: str
    material_evidence_hash: str
    status: str


class NewsPublicationContractData(ExactApiSchema):
    model: str
    prompt_version: str
    workflow_version: str
    schema_version: str
    locale: str


class NewsBriefItemPayloadData(ExactApiSchema):
    story_id: str
    what_happened: list[NewsEvidenceBackedFactData]
    why_it_matters: str
    transmission_scenarios: list[NewsConditionalTransmissionData]
    uncertainties: list[str]
    watchpoints: list[str]


class NewsBriefPayloadData(ExactApiSchema):
    headline: str
    executive_summary: str
    items: list[NewsBriefItemPayloadData]
    narratives: list[str]
    global_watchpoints: list[str]


class NewsBriefPublicationData(ExactApiSchema):
    publication_id: str
    selection_snapshot_id: str
    selection_fingerprint: str
    evidence_bundle_hash: str
    cutoff_at_ms: int
    published_at_ms: int
    contract: NewsPublicationContractData
    payload: NewsBriefPayloadData
    evidence_references: list[str]
    selected_story_ids: list[str]
    selection_decisions: list[JsonObject]
    narrative_groups: list[JsonObject]
    evidence_bundle: NewsBriefEvidenceBundleData
    receipt: JsonObject


class NewsBriefEvidenceBundleData(ExactApiSchema):
    selection_snapshot_id: str
    selection_fingerprint: str
    evidence_bundle_hash: str
    cutoff_at_ms: int
    stories: list[JsonObject]
    narrative_groups: list[JsonObject]
    selection_policy_version: str


class NewsBriefFallbackData(ExactApiSchema):
    selection_snapshot_id: str
    selection_fingerprint: str
    cutoff_at_ms: int
    status: Literal["planned", "debounced", "publishable", "published", "superseded"]
    selected_story_ids: list[str]
    decisions: list[JsonObject]
    evidence_bundle: NewsBriefEvidenceBundleData


class NewsBriefFailureData(ExactApiSchema):
    last_error: str | None
    validation_errors: list[str]
    updated_at_ms: int


class NewsGlobalBriefData(ExactApiSchema):
    current: NewsBriefPublicationData | None
    fallback: NewsBriefFallbackData | None
    latest_failure: NewsBriefFailureData | None


class NewsGlobalBriefHistoryData(ExactApiSchema):
    items: list[NewsBriefPublicationData]


class NewsSourceData(ExactApiSchema):
    source_id: str
    name: str
    feed_url: str
    source_domain: str
    source_role: Literal["original_publisher", "wire_service", "official_authority", "trusted_aggregator"]
    trust_tier: Literal["authoritative", "trusted", "standard", "low"]
    source_chain_id: str
    publisher_organization_id: str
    parent_organization_id: str | None
    canonical_domains: list[str]
    known_relationships: list[JsonObject]
    source_quality_factors: JsonObject
    registry_version: str
    coverage_tags: list[str]
    default_language: str
    enabled: bool
    refresh_interval_seconds: int
    etag: str | None
    last_modified: str | None
    last_fetch_started_at_ms: int | None
    last_fetch_finished_at_ms: int | None
    last_success_at_ms: int | None
    last_http_status: int | None
    consecutive_failures: int
    last_error: str | None
    next_fetch_at_ms: int
    created_at_ms: int
    updated_at_ms: int
    latest_fetch_receipt_id: str | None
    latest_entries_seen: int | None
    latest_entries_admitted: int | None
    latest_duplicate_seen_count: int | None
    latest_rejection_counts: JsonObject | None
    latest_error_code: str | None


class NewsSourcesData(ExactApiSchema):
    items: list[NewsSourceData]


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
    scope: str
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
    scope: str
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


class AccountAlertsData(ExactApiSchema):
    window: str
    alert_type: str | None
    items: list[JsonObject]


class NotificationSummary(ExactApiSchema):
    subscriber_key: str
    unread_count: int
    high_unread_count: int
    critical_unread_count: int
    highest_unread_severity: str | None
    account_unread_counts: dict[str, int]


class NotificationItemData(ExactApiSchema):
    notification_id: str
    dedup_key: str
    rule_id: str
    severity: str
    title: str
    body: str
    entity_type: str | None
    entity_key: str | None
    author_handle: str | None
    symbol: str | None
    chain: str | None
    address: str | None
    event_id: str | None
    source_table: str
    source_id: str
    occurrence_count: int
    first_seen_at_ms: int
    last_seen_at_ms: int
    created_at_ms: int
    updated_at_ms: int
    read_at_ms: int | None
    payload: JsonObject
    channels: list[str]


class NotificationsData(ExactApiSchema):
    items: list[NotificationItemData]
    summary: NotificationSummary


class NotificationDeliveriesData(ExactApiSchema):
    items: list[JsonObject]


class NotificationReadData(ExactApiSchema):
    notification_id: str
    updated: bool


class NotificationReadAllData(ExactApiSchema):
    updated_count: int


class SourceEventDetail(ExactApiSchema):
    event_id: str
    timestamp_ms: int
    source_provider: str
    channel: str
    action: str
    author_handle: str | None
    author_name: str | None
    author_followers: int | None
    author_watched: bool
    text_clean: str | None
    canonical_url: str | None


class SourceEventsByIdsData(ExactApiSchema):
    events: list[SourceEventDetail]
    not_found: list[str]


class WatchlistHandleRowOverview(ExactApiSchema):
    handle: str
    last_source_event_at_ms: int | None
    recent_source_event_count: int


class WatchlistHandlesOverviewData(ExactApiSchema):
    window: str
    items: list[WatchlistHandleRowOverview]


class WatchlistOverviewQuery(ExactApiSchema):
    handle: str
    window: str


class WatchlistOverviewMetrics(ExactApiSchema):
    source_event_count: int
    resolved_token_count: int
    candidate_mention_count: int
    hashtag_count: int
    last_source_event_at_ms: int | None


class WatchlistOverviewCluster(ExactApiSchema):
    label: str
    count: int
    query: str
    kind: Literal["resolved_token", "candidate_mention", "hashtag"]
    target_type: str | None
    target_id: str | None
    symbol: str | None
    source: str


class WatchlistHandleOverviewData(ExactApiSchema):
    query: WatchlistOverviewQuery
    metrics: WatchlistOverviewMetrics
    resolved_token_clusters: list[WatchlistOverviewCluster]
    candidate_mention_clusters: list[WatchlistOverviewCluster]
    hashtag_clusters: list[WatchlistOverviewCluster]
    clusters_truncated: bool
    risk_notes: list[str]


class WatchlistTimelineQuery(ExactApiSchema):
    handle: str
    limit: int


class WatchlistTimelineItem(ExactApiSchema):
    event_id: str
    received_at_ms: int
    author_handle: str | None
    action: str
    text_clean: str | None
    canonical_url: str | None
    cashtags: list[str]
    hashtags: list[str]
    mentions: list[str]
    event: JsonObject
    token_resolutions: list[JsonObject]


class WatchlistHandleTimelineData(ExactApiSchema):
    query: WatchlistTimelineQuery
    items: list[WatchlistTimelineItem]
    has_more: bool
    next_cursor: str | None
