from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from tracefold.news import EventKind, SourceContractReason

from .common import ExactApiSchema
from .news_common import (
    NewsAssetRefData,
    NewsOutcomeData,
    NewsSymbolNormalizationData,
    NewsTriageSummaryData,
)


class NewsReactionSummaryData(ExactApiSchema):
    """The compact event-level Event Reaction: one sample per Event, median over its priceable primaries.

    This is a fixed historical measurement anchored at the Event, not a current rolling window. A pending
    horizon says pending; it is never zero.
    """

    state: Literal["pending", "partial", "complete", "unavailable"]
    state_zh: str = ""
    # Only populated when the Event has exactly one priceable primary. It is the Event-anchored mark, never
    # a current quote; a multi-asset Event has no meaningful shared price and therefore returns null.
    p0: str | None = None
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


class NewsEventData(ExactApiSchema):
    event_id: str
    family: str
    event_kind: EventKind
    source_contract_reason: SourceContractReason | None
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
    provider_score_max: float | None = None
    engine_type: str
    asset_class: str
    grounded_assets: list[str] = Field(default_factory=list)
    # `grounded_assets` stays the raw provider/Gate evidence. `assets` resolves the Event's durable
    # `news_event_assets` ledger, which also carries deterministic-judge assets when that evidence is empty.
    assets: list[NewsAssetRefData] = Field(default_factory=list)
    watchlist_hits: list[str] = Field(default_factory=list)
    macro_lexicon: bool = False
    storyline_key: str = ""
    context_line: str = ""
    published_at_ms: int | None = None
    ingest_mode: str
    provenance: list[str] = Field(default_factory=list)


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
    received_age_ms: int | None = None
    source_age_ms: int | None = None
    effective_age_ms: int | None = None
    freshness_basis: Literal["source_and_received", "received_only"] | None = None
    reference_at_ms: int | None = None
    reference_age_ms: int | None = None
    state: Literal["fresh", "stale", "unavailable", "unlisted"]
    state_zh: str = ""


class NewsQuotesData(ExactApiSchema):
    quotes: list[NewsQuoteData] = Field(default_factory=list)
    measured_at_ms: int


__all__ = [
    "NewsAcceptedReviewData",
    "NewsDeliveryData",
    "NewsEventData",
    "NewsEventDetailData",
    "NewsEventMemberData",
    "NewsEventReactionData",
    "NewsEventReviewSummaryData",
    "NewsEvidenceSnapshotData",
    "NewsQuoteData",
    "NewsQuotesData",
    "NewsReactionSummaryData",
    "NewsReaderReceiptData",
    "NewsTimelineStepData",
    "NewsVerdictData",
]
