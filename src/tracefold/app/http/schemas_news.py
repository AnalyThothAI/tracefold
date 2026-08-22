"""News V3 public API schemas (Event feed / event detail / status)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from tracefold.app.http.schemas import ExactApiSchema


class NewsOutcomeData(ExactApiSchema):
    """One human-readable conclusion per Event; ``kind`` is a stable enum, the texts are Chinese reader copy."""

    kind: Literal[
        "held_recovery",
        "held_gate",
        "queued_publish",
        "queued_triage",
        "dropped",
        "throttled",
        "degraded_dropped",
        "pending_delivery",
        "delivered",
        "delivery_failed",
    ]
    text_zh: str
    reason_zh: str = ""
    group: Literal["pushed", "held", "pending"]


class NewsTriageAssetData(ExactApiSchema):
    symbol: str
    role: str


class NewsAssetRefData(ExactApiSchema):
    """#87: one grounded coin tag, resolved against the #75 instrument universe.

    ``listed`` is the whole point: the provider tags `SPOT` on a Spot Gold headline and `NEAR` on the words
    "near-instant", and until now the console showed those exactly like a real token. ``venue`` is the preferred
    venue when the base trades on several, and is ``None`` when the tag names nothing.
    """

    symbol: str
    base_symbol: str
    venue: str | None = None
    listed: bool = False


class NewsSymbolNormalizationData(ExactApiSchema):
    """#87: the several names one issuer trades under, collapsed to one stable storyline identity."""

    base_symbol: str
    aliases: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class NewsTriageSummaryData(ExactApiSchema):
    """The reader-facing view of one Triage verdict. Every `*_zh` is server-owned copy; the raw enum stays
    beside it so the browser can map it to a visual tone without owning a vocabulary table."""

    final_decision: str
    override_rule: str | None = None
    throttled_by: str | None = None
    degraded: bool = False
    error_code: str | None = None
    direction: str | None = None
    magnitude: int | None = None
    event_type: str | None = None
    scope: str | None = None
    novelty: str | None = None
    audience: str | None = None
    confidence: float | None = None
    actionable: bool | None = None
    model_decision: str | None = None
    headline_zh: str | None = None
    title_zh: str | None = None
    why_zh: str | None = None
    assets: list[NewsTriageAssetData] = Field(default_factory=list)
    direction_zh: str = ""
    magnitude_zh: str = ""
    event_type_zh: str = ""
    scope_zh: str = ""
    novelty_zh: str = ""
    audience_zh: str = ""
    decision_zh: str = ""
    model_decision_zh: str = ""


class NewsDeliverySummaryData(ExactApiSchema):
    state: str
    settled_at_ms: int | None = None
    error_code: str | None = None


class NewsEventData(ExactApiSchema):
    event_id: str
    family: str
    leader_title: str
    leader_url: str | None = None
    leader_description: str = ""
    focus_fact_id: str = ""
    focus_fact_text: str = ""
    focus_fact_context: str = ""
    focus_fact_method: str = ""
    focus_span_start: int = 0
    focus_span_end: int = 0
    reporting_origin: str = ""
    opened_at_ms: int
    last_member_at_ms: int
    member_count: int
    admission: str
    priority: str
    provider_score_max: float | None = None
    engine_type: str
    asset_class: str
    grounded_assets: list[str] = Field(default_factory=list)
    # #87: `grounded_assets` stays the raw provider tags; `assets` is the same list resolved against the
    # instrument universe, so the browser never has to guess whether a tag names something real.
    assets: list[NewsAssetRefData] = Field(default_factory=list)
    watchlist_hits: list[str] = Field(default_factory=list)
    macro_lexicon: bool = False
    storyline_key: str = ""
    context_line: str = ""
    published_at_ms: int | None = None
    ingest_mode: str
    provenance: list[str] = Field(default_factory=list)


class NewsFeedEventData(NewsEventData):
    title_zh: str | None = None
    outcome: NewsOutcomeData
    triage: NewsTriageSummaryData | None = None
    delivery: NewsDeliverySummaryData | None = None
    # #88: the fixed 1H/4H return after this Event. Current quotes are deliberately *not* here — they change
    # every few seconds and would make the feed's ETag useless; the browser reads them from /api/news/quotes.
    reaction: NewsReactionSummaryData | None = None


class NewsFeedFiltersData(ExactApiSchema):
    family: str | None = None
    admission: str | None = None
    priority: str | None = None
    decision: str | None = None
    symbol: str | None = None
    q: str | None = None
    sort: Literal["latest", "priority"]
    limit: int
    outcome: Literal["pushed", "held", "pending"] | None = None
    hours: int | None = None


class NewsFeedCountsData(ExactApiSchema):
    """How the request's filter and window split across the three outcome groups, for the feed's task tabs.

    ``total`` is the sum of the other three: the groups partition the feed exactly.
    """

    total: int
    pushed: int
    held: int
    pending: int


class NewsFeedData(ExactApiSchema):
    events: list[NewsFeedEventData]
    next_cursor: str | None = None
    # First page only — a paged request reuses the counts the first page already reported.
    counts: NewsFeedCountsData | None = None
    filters: NewsFeedFiltersData


class NewsEventMemberData(ExactApiSchema):
    item_id: str
    title: str
    url: str | None = None
    reporting_origin: str
    published_at_ms: int
    joined_at_ms: int
    match_kind: str
    jaccard_estimate: float | None = None
    provenance: list[str] = Field(default_factory=list)
    description: str = ""
    fact_id: str = ""
    fact_text: str = ""


class NewsVerdictData(ExactApiSchema):
    stage: str
    policy_version: str
    model_decision: str | None = None
    rule_baseline_decision: str
    final_decision: str
    override_rule: str | None = None
    throttled_by: str | None = None
    verdict: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    program_version: str | None = None
    program_sha256: str | None = None
    prompt_version: str | None = None
    degraded: bool = False
    error_code: str | None = None
    trace: dict[str, Any] = Field(default_factory=dict)
    evidence_version: int | None = None
    evidence_sha256: str | None = None
    focus_fact_id: str | None = None
    published_at_ms: int | None = None
    created_at_ms: int


class NewsDeliveryData(ExactApiSchema):
    kind: str
    state: str
    error_code: str | None = None
    attempted_at_ms: int
    settled_at_ms: int | None = None
    card: dict[str, Any] = Field(default_factory=dict)
    receipt: dict[str, Any] | None = None


class NewsEvidenceSnapshotData(ExactApiSchema):
    event_id: str
    evidence_version: int
    focus_fact_id: str
    evidence_sha256: str
    provenance: Literal["observed", "legacy_reconstructed"]
    release_eligible: bool
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int


class NewsReaderReceiptData(ExactApiSchema):
    state: Literal["received", "not_received", "unknown"]
    delivery_state: str | None = None
    error_code: str | None = None
    received_at_ms: int | None = None
    rendered_card: dict[str, Any] | None = None


class NewsAcceptedReviewData(ExactApiSchema):
    review_id: str
    subject_kind: Literal["event", "external_miss", "pairwise", "legacy_label"]
    event_id: str | None = None
    external_snapshot_id: str | None = None
    should_push: Literal["must_push", "should_push", "should_hold", "must_hold", "uncertain"] | None = None
    dimensions: dict[str, str] = Field(default_factory=dict)
    novelty: dict[str, Any] = Field(default_factory=dict)
    first_bad_owner: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    expected_correction: str = ""
    note: str = ""
    reviewer: str
    created_at_ms: int
    rubric_version: str
    reader_contract_version: str
    pairwise_case_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NewsEventReviewSummaryData(ExactApiSchema):
    judgment_n: int = 0
    accepted: NewsAcceptedReviewData | None = None
    uncertain: bool = False


class NewsTimelineStepData(ExactApiSchema):
    stage: Literal["received", "gate", "triage", "decide", "delivery"]
    title_zh: str
    at_ms: int
    summary_zh: str
    facts: dict[str, Any] = Field(default_factory=dict)


class NewsEventDetailData(ExactApiSchema):
    event: NewsEventData
    outcome: NewsOutcomeData
    triage: NewsTriageSummaryData | None = None
    timeline: list[NewsTimelineStepData] = Field(default_factory=list)
    members: list[NewsEventMemberData]
    verdicts: list[NewsVerdictData]
    deliveries: list[NewsDeliveryData]
    review: NewsEventReviewSummaryData
    evidence_snapshots: list[NewsEvidenceSnapshotData] = Field(default_factory=list)
    reader_receipt: NewsReaderReceiptData
    normalization: list[NewsSymbolNormalizationData] = Field(default_factory=list)
    reaction: NewsReactionSummaryData | None = None
    reactions: list[NewsEventReactionData] = Field(default_factory=list)


class NewsQuoteData(ExactApiSchema):
    """One current quote (#88). `state` is derived when read, never maintained by a timer write.

    `unlisted` and `unavailable` answer different questions — "no venue we poll lists this tag" versus "we have
    not managed to quote it yet" — and neither ever renders as a price of zero. `price_kind` and `change_basis`
    are explicit because a derivative mid must never be presented as a cash-equity last price.
    """

    requested_symbol: str
    symbol: str
    base_symbol: str
    venue: str | None = None
    venue_symbol: str | None = None
    instrument_class: str | None = None
    quote_asset: str | None = None
    price: str | None = None
    price_kind: str | None = None
    price_kind_zh: str = ""
    change_pct: float | None = None
    change_basis: str | None = None
    change_basis_zh: str = ""
    source_at_ms: int | None = None
    received_at_ms: int | None = None
    age_ms: int | None = None
    state: Literal["fresh", "stale", "unavailable", "unlisted"]
    state_zh: str = ""


class NewsQuotesData(ExactApiSchema):
    quotes: list[NewsQuoteData] = Field(default_factory=list)
    measured_at_ms: int


class NewsReactionSummaryData(ExactApiSchema):
    """The compact event-level Event Reaction: one sample per Event, median over its priceable primaries.

    This is a fixed historical measurement anchored at the Event, not a current rolling window. A pending
    horizon says pending; it is never zero.
    """

    state: Literal["pending", "partial", "complete", "unavailable"]
    state_zh: str = ""
    return_1h_bps: int | None = None
    return_4h_bps: int | None = None
    asset_n: int = 0
    priced_n: int = 0
    unavailable_reason: str | None = None
    unavailable_reason_zh: str = ""
    metric_version: str


class NewsEventReactionData(ExactApiSchema):
    """One per-asset Reaction with the raw closes it was computed from, for audit on the detail page."""

    symbol: str
    metric_version: str
    venue: str | None = None
    venue_symbol: str | None = None
    instrument_class: str = "unknown"
    anchor_at_ms: int
    p0: str | None = None
    p0_at_ms: int | None = None
    p1: str | None = None
    p1_at_ms: int | None = None
    p4: str | None = None
    p4_at_ms: int | None = None
    return_1h_bps: int | None = None
    return_4h_bps: int | None = None
    is_primary: bool
    state: Literal["pending", "partial", "complete", "unavailable"]
    state_zh: str = ""
    unavailable_reason: str | None = None
    unavailable_reason_zh: str = ""
    updated_at_ms: int | None = None


class NewsReviewUnavailableData(ExactApiSchema):
    reason: str
    reason_zh: str = ""
    n: int


class NewsReviewCoverageData(ExactApiSchema):
    """Coverage before accuracy: every percentage on the page is paired with the N it came from."""

    horizon: Literal["1h", "4h"]
    horizon_zh: str = ""
    eligible_n: int
    priced_n: int
    coverage_pct: float | None = None
    no_primary_n: int = 0
    degraded_n: int = 0
    unavailable: list[NewsReviewUnavailableData] = Field(default_factory=list)


class NewsReviewDirectionData(ExactApiSchema):
    """`scored` marks the rows that carry hit-rate: neutral and unclear report their N, never accuracy.

    Direction rows are counts by design (#88 §8). Return distributions belong to the magnitude and
    event-type sections, which is where the median columns live.
    """

    direction: str
    direction_zh: str = ""
    horizon: Literal["1h", "4h"]
    horizon_zh: str = ""
    scored: bool
    eligible_n: int
    priced_n: int
    hits: int | None = None
    hit_pct: float | None = None
    coverage_pct: float | None = None


class NewsReviewMagnitudeData(ExactApiSchema):
    magnitude: int
    magnitude_zh: str = ""
    eligible_n: int
    share_pct: float | None = None
    priced_1h_n: int
    priced_4h_n: int
    coverage_1h_pct: float | None = None
    mean_abs_1h_bps: int | None = None
    mean_abs_4h_bps: int | None = None
    median_abs_1h_bps: int | None = None
    median_abs_4h_bps: int | None = None


class NewsReviewEventTypeData(ExactApiSchema):
    event_type: str
    event_type_zh: str = ""
    eligible_n: int
    pushed_n: int
    escalated_n: int
    pushed_pct: float | None = None
    held_n: int
    priced_1h_n: int
    coverage_1h_pct: float | None = None
    median_1h_bps: int | None = None
    median_abs_1h_bps: int | None = None
    median_4h_bps: int | None = None
    median_abs_4h_bps: int | None = None


class NewsReviewMissData(ExactApiSchema):
    """A review queue, not a verdict: movement never proves the Event caused it or should have been pushed."""

    event_id: str
    opened_at_ms: int
    headline_zh: str | None = None
    leader_title: str = ""
    storyline_key: str = ""
    final_decision: str
    decision_zh: str = ""
    override_rule: str | None = None
    override_rule_zh: str = ""
    throttled_by: str | None = None
    throttled_by_zh: str = ""
    direction: str | None = None
    direction_zh: str = ""
    magnitude: int | None = None
    magnitude_zh: str = ""
    event_type: str | None = None
    event_type_zh: str = ""
    return_1h_bps: int | None = None
    return_4h_bps: int | None = None
    asset_n: int = 0
    assets: list[NewsEventReactionData] = Field(default_factory=list)
    fact_cluster_key: str = ""
    fact_cluster_n: int = 1
    related_event_ids: list[str] = Field(default_factory=list)


class NewsReviewMetaData(ExactApiSchema):
    hours: int
    window_start_ms: int
    window_end_ms: int
    discovery_window_start_ms: int
    metric_version: str
    measured_at_ms: int
    cohort: str | None = None


class NewsReviewSummaryData(ExactApiSchema):
    """The topbar figure. A percentage without a priced denominator is not shown at all."""

    hit_1h_pct: float | None = None
    hit_1h_n: int = 0
    coverage_1h_pct: float | None = None


class NewsMarketReviewData(ExactApiSchema):
    meta: NewsReviewMetaData
    coverage: list[NewsReviewCoverageData] = Field(default_factory=list)
    directions: list[NewsReviewDirectionData] = Field(default_factory=list)
    magnitudes: list[NewsReviewMagnitudeData] = Field(default_factory=list)
    event_types: list[NewsReviewEventTypeData] = Field(default_factory=list)
    potential_misses: list[NewsReviewMissData] = Field(default_factory=list)
    summary: NewsReviewSummaryData


class NewsReviewSelectionData(ExactApiSchema):
    stratum: str
    stratum_zh: str = ""
    reason: str | None = None
    reason_zh: str = ""
    sampling_probability: float
    selection_version: str


class NewsReviewReceiptTruthData(ExactApiSchema):
    truth: Literal["received", "not_received", "unknown"]
    truth_zh: str = ""
    state: str | None = None
    settled_at_ms: int | None = None
    rendered_card: dict[str, Any] | None = None
    error_code: str | None = None


class NewsReviewTaskData(ExactApiSchema):
    task_id: str
    task_version: str
    mode: Literal["event", "pairwise"]
    event_id: str | None = None
    evidence_version: int | None = None
    verdict_evidence_version: int | None = None
    opened_at_ms: int | None = None
    headline: str | None = None
    agent_headline: str | None = None
    agent_why: str | None = None
    final_decision: str | None = None
    final_decision_zh: str = ""
    reader_receipt: NewsReviewReceiptTruthData | None = None
    cohort: str | None = None
    agent_cohort: dict[str, str] | None = None
    selection: NewsReviewSelectionData
    evidence_ready: bool | None = None
    disclosure: dict[str, Any] | None = None
    review_status: Literal["pending", "accepted"]
    accepted_review: NewsAcceptedReviewData | None = None


class NewsReviewCoverageIntervalData(ExactApiSchema):
    lower_pct: float
    upper_pct: float


class NewsReviewCoverageBucketData(ExactApiSchema):
    cohort: str | None = None
    legacy_cohort: str | None = None
    agent: dict[str, str] | None = None
    stratum: str | None = None
    stratum_zh: str | None = None
    events: int
    accepted: int
    received: int | None = None
    reviewed: int | None = None
    accepted_pct: float | None = None
    accepted_interval_95: NewsReviewCoverageIntervalData | None = None


class NewsReviewFunnelV2Data(ExactApiSchema):
    received: int
    replayable: int
    reviewed: int
    accepted: int
    holdout_ready: int
    total: int
    external_misses: int


class NewsReviewHoldoutData(ExactApiSchema):
    status: Literal["ready", "insufficient_evidence"]
    case_n: int
    cluster_n: int
    accepted_case_n: int
    accepted_cluster_n: int
    coverage_pct: float | None = None
    coverage_interval_95: NewsReviewCoverageIntervalData | None = None


class NewsReviewData(ExactApiSchema):
    view: Literal["queue", "coverage", "proposals", "market"]
    status: str | None = None
    mode: Literal["event", "pairwise"] | None = None
    message_zh: str | None = None
    title_zh: str | None = None
    disclaimer_zh: str | None = None
    reader_contract_version: str | None = None
    reader_contract_sha256: str | None = None
    rubric_version: str | None = None
    tasks: list[NewsReviewTaskData] = Field(default_factory=list)
    next_cursor: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    window: dict[str, int] | None = None
    funnel: NewsReviewFunnelV2Data | None = None
    cohorts: list[NewsReviewCoverageBucketData] = Field(default_factory=list)
    strata: list[NewsReviewCoverageBucketData] = Field(default_factory=list)
    holdout: NewsReviewHoldoutData | None = None
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    reaction: NewsMarketReviewData | None = None
    disclosure: dict[str, Any] | None = None


class NewsReviewSubmissionReceiptData(ExactApiSchema):
    review_id: str
    acceptance_id: str | None = None
    external_snapshot_id: str | None = None
    task_id: str
    task_version: str
    created_at_ms: int | None = None


class NewsReviewSubmitData(ExactApiSchema):
    idempotent: bool
    receipt: NewsReviewSubmissionReceiptData
    next_task: NewsReviewTaskData | None = None
    updated_queue_counts: dict[str, int] = Field(default_factory=dict)


class NewsReviewEvidenceData(ExactApiSchema):
    task: NewsReviewTaskData
    disclosure: dict[str, Any]
    evidence: dict[str, Any] | None = None
    agent: dict[str, Any] | None = None
    reader_receipt: NewsReviewReceiptTruthData | None = None
    market_reactions: list[NewsEventReactionData] = Field(default_factory=list)
    accepted_review: NewsAcceptedReviewData | None = None
    rubric: dict[str, Any] = Field(default_factory=dict)
    versions: dict[str, Any] = Field(default_factory=dict)
    source_evidence: dict[str, Any] | None = None
    output_A: dict[str, Any] | None = None
    output_B: dict[str, Any] | None = None
    reveal: dict[str, Any] | None = None


class NewsQuoteVenueData(ExactApiSchema):
    source_key: str
    target_count: int
    quote_count: int
    age_ms: int
    state: str
    source_at_ms: int | None = None
    received_at_ms: int


class NewsPriceStatusData(ExactApiSchema):
    """#88 §11: per-source freshness and Reaction backlog, so congestion is visible before the UI shows it."""

    metric_version: str = ""
    # The backlog SLO (#88 §14): how far behind the oldest Event-asset still waiting for a horizon is.
    oldest_due_age_ms: int = 0
    sources: list[NewsQuoteVenueData] = Field(default_factory=list)
    fresh_sources: int = 0
    quotes: int = 0
    reaction_partial_7d: int = 0
    reaction_complete_7d: int = 0
    reaction_unavailable_7d: int = 0


class NewsIncidentData(ExactApiSchema):
    incident_id: int
    cause_class: str
    opened_at_ms: int
    planned: bool


class NewsIngestStatusData(ExactApiSchema):
    connected: bool
    last_frame_at_ms: int | None = None
    last_publish_at_ms: int | None = None
    last_error_code: str | None = None
    open_incidents: list[NewsIncidentData] = Field(default_factory=list)
    token_configured: bool


class NewsBrokerQueueData(ExactApiSchema):
    messages: int
    consumers: int


class NewsBrokerStatusData(ExactApiSchema):
    configured: bool
    connected: bool | None = None
    queues: dict[str, NewsBrokerQueueData] = Field(default_factory=dict)
    error_code: str | None = None
    observed_at_ms: int | None = None


class NewsPipelineStatusData(ExactApiSchema):
    events_1h: int = 0
    events_24h: int = 0
    candidates_24h: int = 0
    triage_24h: int = 0
    triage_degraded_24h: int = 0
    decided_push_24h: int = 0
    throttled_24h: int = 0
    triage_p50_ms: float | None = None
    triage_p95_ms: float | None = None
    queue_lag_p95_ms: float | None = None
    reasked_24h: int = 0
    novelty_defaulted_24h: int = 0
    triage_model: str | None = None
    reader_card_model: str | None = None
    reader_card_dedicated: bool = False
    triage_fallback_model: str | None = None
    reader_card_fallback_model: str | None = None
    reader_card_fallback_dedicated: bool = False
    suppressed_by_reason: dict[str, int] = Field(default_factory=dict)
    dropped_by_rule: dict[str, int] = Field(default_factory=dict)
    throttled_by_key: dict[str, int] = Field(default_factory=dict)
    pushed_by_rule: dict[str, int] = Field(default_factory=dict)
    reviewed_should_push_24h: int = 0
    reviewed_external_miss_24h: int = 0
    # #100: {"throttled": n, "all": n} — duplicates withheld, by the path that measured the card.
    duplicates_withheld_24h: dict[str, int] = Field(default_factory=dict)
    tagged_24h: int = 0
    grounded_24h: int = 0
    ungrounded_by_symbol_24h: dict[str, int] = Field(default_factory=dict)
    candidate_share_24h: float | None = None
    triage_degraded_by_code_24h: dict[str, int] = Field(default_factory=dict)


class NewsDeliveryStatusData(ExactApiSchema):
    sent_24h: int = 0
    sent_1h: int = 0
    terminal_24h: int = 0
    last_error_code: str | None = None
    e2e_p95_ms: float | None = None
    delivery_available: bool


class NewsLearningRetentionStatusData(ExactApiSchema):
    last_run_at_ms: int | None = None
    eligible_recordings: int = 0
    eligible_cases: int = 0
    eligible_artifacts: int = 0
    deleted_recordings: int = 0
    deleted_cases: int = 0
    deleted_artifacts: int = 0
    oldest_recording_age_ms: int | None = None
    oldest_case_age_ms: int | None = None
    oldest_artifact_age_ms: int | None = None
    last_error_code: str | None = None
    updated_at_ms: int | None = None


class NewsControlStateData(ExactApiSchema):
    paused: bool = False
    mutes: list[dict[str, Any]] = Field(default_factory=list)


class NewsHealthItemData(ExactApiSchema):
    level: Literal["ok", "warn", "bad", "off"]
    summary_zh: str
    detail_zh: str = ""


class NewsHealthData(ExactApiSchema):
    ingest: NewsHealthItemData
    broker: NewsHealthItemData
    model: NewsHealthItemData
    delivery: NewsHealthItemData
    overall: Literal["ok", "warn", "bad", "off"]


class NewsFunnelData(ExactApiSchema):
    received: int = 0
    candidates: int = 0
    triaged: int = 0
    tagged: int = 0
    grounded: int = 0
    decided_push: int = 0
    delivered: int = 0
    received_1h: int = 0
    delivered_1h: int = 0


class NewsReasonCountData(ExactApiSchema):
    stage: Literal["gate", "drop", "throttle", "push", "degraded", "ungrounded"]
    key: str
    label_zh: str
    count: int


class NewsInstrumentUniverse(ExactApiSchema):
    """#75: what the last venue snapshot holds. `last_snapshot_ms` is None until the first snapshot lands.

    `dangling_aliases` counts seed aliases pointing at a symbol no venue lists — each one is a provider tag that
    silently resolves to nothing, which is how `1810.HK -> XIAOMI` went unnoticed for a week (#89). It should be 0.

    Every other figure counts contracts on venues we poll. `reference_symbols` is the separate US listed-symbol
    directory (#91): those tickers tell the Gate a headline is about a stock, but nobody can trade them here, so
    folding them into `trading` would make that number mean something else.
    """

    trading: int = 0
    delisted: int = 0
    base_symbols: int = 0
    venues: int = 0
    last_snapshot_ms: int | None = None
    by_venue: dict[str, int] = Field(default_factory=dict)
    by_class: dict[str, int] = Field(default_factory=dict)
    dangling_aliases: int = 0
    reference_symbols: int = 0


class NewsStatusData(ExactApiSchema):
    state: Literal["ready", "degraded", "warming", "unavailable"]
    workers_state: str | None = None
    health: NewsHealthData
    funnel_24h: NewsFunnelData
    reasons_24h: list[NewsReasonCountData] = Field(default_factory=list)
    ingest: NewsIngestStatusData
    broker: NewsBrokerStatusData
    pipeline: NewsPipelineStatusData
    delivery: NewsDeliveryStatusData
    learning_retention: NewsLearningRetentionStatusData
    control: NewsControlStateData
    watchlist: list[str] = Field(default_factory=list)
    instruments: NewsInstrumentUniverse = Field(default_factory=NewsInstrumentUniverse)
    price: NewsPriceStatusData = Field(default_factory=NewsPriceStatusData)
    measured_at_ms: int


__all__ = [
    "NewsAssetRefData",
    "NewsBrokerQueueData",
    "NewsBrokerStatusData",
    "NewsControlStateData",
    "NewsDeliveryData",
    "NewsDeliveryStatusData",
    "NewsDeliverySummaryData",
    "NewsEventData",
    "NewsEventDetailData",
    "NewsEventMemberData",
    "NewsEventReactionData",
    "NewsFeedCountsData",
    "NewsFeedData",
    "NewsFeedEventData",
    "NewsFeedFiltersData",
    "NewsFunnelData",
    "NewsHealthData",
    "NewsHealthItemData",
    "NewsIncidentData",
    "NewsIngestStatusData",
    "NewsLearningRetentionStatusData",
    "NewsOutcomeData",
    "NewsPipelineStatusData",
    "NewsPriceStatusData",
    "NewsQuoteData",
    "NewsQuoteVenueData",
    "NewsQuotesData",
    "NewsReactionSummaryData",
    "NewsReasonCountData",
    "NewsReviewCoverageData",
    "NewsReviewData",
    "NewsReviewDirectionData",
    "NewsReviewEventTypeData",
    "NewsReviewMagnitudeData",
    "NewsReviewMetaData",
    "NewsReviewMissData",
    "NewsReviewSummaryData",
    "NewsReviewUnavailableData",
    "NewsStatusData",
    "NewsSymbolNormalizationData",
    "NewsTimelineStepData",
    "NewsTriageSummaryData",
    "NewsVerdictData",
]
