from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ExactApiSchema


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


class NewsSourceContractStageCountsData(ExactApiSchema):
    received: int = 0
    parsed: int = 0
    parse_failed: int = 0
    unsupported: int = 0
    verdict: int = 0


class NewsSourceContracts24hData(ExactApiSchema):
    news_v1: NewsSourceContractStageCountsData = Field(default_factory=NewsSourceContractStageCountsData)
    listing_v1: NewsSourceContractStageCountsData = Field(default_factory=NewsSourceContractStageCountsData)
    oi_v1: NewsSourceContractStageCountsData = Field(default_factory=NewsSourceContractStageCountsData)
    liquidation_v1: NewsSourceContractStageCountsData = Field(default_factory=NewsSourceContractStageCountsData)
    unsupported_market: NewsSourceContractStageCountsData = Field(default_factory=NewsSourceContractStageCountsData)


class NewsPipelineStatusData(ExactApiSchema):
    events_1h: int = 0
    events_24h: int = 0
    candidates_24h: int = 0
    source_classifier_version: str = ""
    source_contracts_24h: NewsSourceContracts24hData = Field(default_factory=NewsSourceContracts24hData)
    triage_24h: int = 0
    # The funnel counts every judgment; model health counts only the model's (#137).
    model_triage_24h: int = 0
    triage_degraded_24h: int = 0
    decided_push_24h: int = 0
    telemetry_push_24h: int = 0
    telemetry_received_24h: int = 0
    telemetry_parsed_24h: int = 0
    telemetry_parse_failed_24h: int = 0
    # #207: Events on the deterministic admission, which is the 持仓异动 table's own universe. `received`
    # counts provider items before the Gate and so names frames no row can reach.
    telemetry_events_24h: int = 0
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
    admitted_24h: int = 0
    # Event-feed funnel: one cohort selected by Event.opened_at_ms, then tested for each durable stage.
    funnel_received_24h: int = 0
    funnel_parsed_24h: int = 0
    funnel_admitted_24h: int = 0
    funnel_triaged_24h: int = 0
    funnel_delivered_24h: int = 0
    triage_degraded_by_code_24h: dict[str, int] = Field(default_factory=dict)


class NewsOiPolicyData(ExactApiSchema):
    """`news.oi` as it is running right now — the operator-owned thresholds the judge applies."""

    window_ms: int
    max_rank_in_window: int
    whale_oi_ratio_above_bps: int
    oi_change_at_least_bps: int


class NewsOiTradeFloorsData(ExactApiSchema):
    """The capital lane's own floors, shown beside the News gates and never mixed with them (#207).

    Two different questions read the same frame: News asks whether a reader should be told, Trading asks
    whether the lane may open exposure. Their thresholds are separate config and reaching either number is no
    evidence about the other, so the monitor renders both and labels which is which.

    `enabled` is here because `tracefold.trading` ships disabled: a floor from a lane that is not running is a
    published research band, not a gate anything is currently passing. Read from platform configuration —
    `tracefold.news` never imports `tracefold.trading` and this endpoint does not either.
    """

    enabled: bool = False
    mode: str = "paper"
    allow_short: bool = False
    min_whale_long_profit_bps: int = 0
    min_oi_value_usd: int = 0
    min_price_move_bps: int = 0
    max_price_move_bps: int = 0
    pre_move_lookback_ms: int = 0


class NewsOiWindowSymbolData(ExactApiSchema):
    """One symbol's spent rank slots inside the live window. `full` means the next frame hits the ceiling."""

    symbol: str
    used: int
    max_rank_in_window: int
    full: bool


class NewsOiStatusData(ExactApiSchema):
    """#137's deterministic telemetry lane, as the 持仓异动 monitor reads it.

    `by_rule_24h` is keyed on the judge's own rule names. It cannot come from `pipeline.dropped_by_rule`:
    `decide()` writes `override_rule = 'telemetry_deterministic'` for every OI verdict — push and withhold
    alike — so that map can say how many frames were held but never by which gate.
    """

    policy: NewsOiPolicyData | None = None
    trade_floors: NewsOiTradeFloorsData = Field(default_factory=NewsOiTradeFloorsData)
    by_rule_24h: dict[str, int] = Field(default_factory=dict)
    # Measured over `policy.window_ms` ending at `measured_at_ms`. The window's start is deliberately not a
    # field: it moves with every read and would churn the status ETag on a 15 s poll for every reader, while
    # the two numbers it is the difference of are already here.
    window_occupancy: list[NewsOiWindowSymbolData] = Field(default_factory=list)


class NewsDeliveryStatusData(ExactApiSchema):
    sent_24h: int = 0
    sent_1h: int = 0
    terminal_24h: int = 0
    last_error_code: str | None = None
    e2e_p50_ms: float | None = None
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
    parsed: int = 0
    admitted: int = 0
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
    oi: NewsOiStatusData = Field(default_factory=NewsOiStatusData)
    delivery: NewsDeliveryStatusData
    learning_retention: NewsLearningRetentionStatusData
    watchlist: list[str] = Field(default_factory=list)
    instruments: NewsInstrumentUniverse = Field(default_factory=NewsInstrumentUniverse)
    price: NewsPriceStatusData = Field(default_factory=NewsPriceStatusData)
    measured_at_ms: int


__all__ = [
    "NewsBrokerQueueData",
    "NewsBrokerStatusData",
    "NewsDeliveryStatusData",
    "NewsFunnelData",
    "NewsHealthData",
    "NewsHealthItemData",
    "NewsIncidentData",
    "NewsIngestStatusData",
    "NewsInstrumentUniverse",
    "NewsLearningRetentionStatusData",
    "NewsOiPolicyData",
    "NewsOiStatusData",
    "NewsOiTradeFloorsData",
    "NewsOiWindowSymbolData",
    "NewsPipelineStatusData",
    "NewsPriceStatusData",
    "NewsQuoteVenueData",
    "NewsReasonCountData",
    "NewsSourceContractStageCountsData",
    "NewsSourceContracts24hData",
    "NewsStatusData",
]
