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
    decided_push: int = 0
    delivered: int = 0
    received_1h: int = 0
    delivered_1h: int = 0


class NewsReasonCountData(ExactApiSchema):
    stage: Literal["gate", "drop", "throttle", "push", "degraded"]
    key: str
    label_zh: str
    count: int


class NewsInstrumentUniverse(ExactApiSchema):
    """#75: what the last venue snapshot holds. `last_snapshot_ms` is None until the first snapshot lands."""

    trading: int = 0
    delisted: int = 0
    base_symbols: int = 0
    venues: int = 0
    last_snapshot_ms: int | None = None
    by_venue: dict[str, int] = Field(default_factory=dict)


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
    measured_at_ms: int


__all__ = [
    "NewsBrokerQueueData",
    "NewsBrokerStatusData",
    "NewsControlStateData",
    "NewsDeliveryData",
    "NewsDeliveryStatusData",
    "NewsDeliverySummaryData",
    "NewsEventData",
    "NewsEventDetailData",
    "NewsEventMemberData",
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
    "NewsReasonCountData",
    "NewsStatusData",
    "NewsTimelineStepData",
    "NewsTriageSummaryData",
    "NewsVerdictData",
]
