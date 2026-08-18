"""News V3 public API schemas (Event feed / event detail / status)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from tracefold.app.http.schemas import ExactApiSchema


class NewsTriageSummaryData(ExactApiSchema):
    final_decision: str
    override_rule: str | None = None
    throttled_by: str | None = None
    degraded: bool = False
    direction: str | None = None
    magnitude: int | None = None
    event_type: str | None = None
    scope: str | None = None
    headline_zh: str | None = None
    title_zh: str | None = None


class NewsDeliverySummaryData(ExactApiSchema):
    state: str
    settled_at_ms: int | None = None


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


class NewsFeedData(ExactApiSchema):
    events: list[NewsFeedEventData]
    next_cursor: str | None = None
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


class NewsEventDetailData(ExactApiSchema):
    event: NewsEventData
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
    deep_24h: int = 0
    decided_push_24h: int = 0
    throttled_24h: int = 0
    triage_p50_ms: float | None = None
    triage_p95_ms: float | None = None
    queue_lag_p95_ms: float | None = None
    triage_model: str | None = None
    analyst_model: str | None = None


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


class NewsStatusData(ExactApiSchema):
    state: Literal["ready", "degraded", "warming", "unavailable"]
    workers_state: str | None = None
    ingest: NewsIngestStatusData
    broker: NewsBrokerStatusData
    pipeline: NewsPipelineStatusData
    delivery: NewsDeliveryStatusData
    control: NewsControlStateData
    watchlist: list[str] = Field(default_factory=list)
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
    "NewsFeedData",
    "NewsFeedEventData",
    "NewsFeedFiltersData",
    "NewsIncidentData",
    "NewsIngestStatusData",
    "NewsLabelData",
    "NewsPipelineStatusData",
    "NewsStatusData",
    "NewsTriageSummaryData",
    "NewsVerdictData",
]
