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
    """#87: the several names one issuer trades under, collapsed to the base the storyline throttle buckets by."""

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
    prompt_version: str | None = None
    degraded: bool = False
    error_code: str | None = None
    trace: dict[str, Any] = Field(default_factory=dict)
    published_at_ms: int | None = None
    created_at_ms: int


class NewsDeliveryData(ExactApiSchema):
    kind: str
    state: str
    error_code: str | None = None
    attempted_at_ms: int
    settled_at_ms: int | None = None
    receipt: dict[str, Any] | None = None


class NewsLabelData(ExactApiSchema):
    label_version: str
    source: str
    label: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int
    labeled_by: str = "operator"
    subject: str = ""


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
    labels: list[NewsLabelData] = Field(default_factory=list)
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
    """`scored` marks the rows that carry hit-rate: neutral and unclear report N and returns, never accuracy."""

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
    median_bps: int | None = None
    median_abs_bps: int | None = None


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


class NewsReviewMetaData(ExactApiSchema):
    hours: int
    window_start_ms: int
    window_end_ms: int
    metric_version: str
    measured_at_ms: int


class NewsReviewSummaryData(ExactApiSchema):
    """The topbar figure. A percentage without a priced denominator is not shown at all."""

    hit_1h_pct: float | None = None
    hit_1h_n: int = 0
    coverage_1h_pct: float | None = None


class NewsReviewData(ExactApiSchema):
    meta: NewsReviewMetaData
    coverage: list[NewsReviewCoverageData] = Field(default_factory=list)
    directions: list[NewsReviewDirectionData] = Field(default_factory=list)
    magnitudes: list[NewsReviewMagnitudeData] = Field(default_factory=list)
    event_types: list[NewsReviewEventTypeData] = Field(default_factory=list)
    potential_misses: list[NewsReviewMissData] = Field(default_factory=list)
    summary: NewsReviewSummaryData


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
    configured_strategy_ids: list[str] = Field(default_factory=list)
    provider_enabled_strategy_ids: list[str] | None = None
    strategy_warnings: list[str] = Field(default_factory=list)
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
    triage_fallback_model: str | None = None
    suppressed_by_reason: dict[str, int] = Field(default_factory=dict)
    dropped_by_rule: dict[str, int] = Field(default_factory=dict)
    throttled_by_key: dict[str, int] = Field(default_factory=dict)
    pushed_by_rule: dict[str, int] = Field(default_factory=dict)
    labeled_missed_24h: int = 0
    labeled_missed_without_event_24h: int = 0
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
    hourly_cap: int


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
    "NewsLabelData",
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
