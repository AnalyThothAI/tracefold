from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from tracefold.news import EventKind, SourceAuthority
from tracefold.news.models import MarketType
from tracefold.news.notifications.contracts import ClaimDecisionValue, ClaimReason, PlanAction, PlanReason, PlanTimings
from tracefold.news.notifications.novelty import Novelty, Render
from tracefold.news.notifications.ports import DeliveryTimings
from tracefold.news.notifications.reader import ReaderBackend
from tracefold.news.update_view import LegacyClaimReason
from tracefold.news.updates.contracts import ChangeKind, ContentKind, Mode, Phase, Relation

from .common import ExactApiSchema
from .news_common import (
    NewsAssetRefData,
    NewsOutcomeData,
    NewsSymbolNormalizationData,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class NewsEventData(ExactApiSchema):
    event_id: str
    event_kind: EventKind
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
    # Raw provider/Gate evidence stays separate. Once adopted, assets are the current claims' primary
    # assets, including unresolved ones; before adoption they come from the source asset ledger.
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


class NewsDeliveryData(ExactApiSchema):
    # The EventUpdate delivery intent identity.
    intent_id: str
    kind: str
    state: str
    error_code: str | None = None
    attempted_at_ms: int
    settled_at_ms: int | None = None
    card: dict[str, Any] = Field(default_factory=dict)
    receipt: dict[str, Any] | None = None
    pending_card: dict[str, Any] | None = None
    edit_state: Literal["editing", "edited", "ambiguous"] | None = None
    edit_error_code: str | None = None
    edit_attempted_at_ms: int | None = None
    edit_settled_at_ms: int | None = None


class NewsEvidenceSnapshotData(ExactApiSchema):
    """Current evidence identity only; raw evidence bytes remain in PostgreSQL audit storage."""

    event_id: str
    evidence_version: int
    focus_fact_id: str
    evidence_sha256: str
    provenance: Literal["observed"]
    release_eligible: bool
    created_at_ms: int


class NewsReaderReceiptData(ExactApiSchema):
    state: Literal["received", "not_received", "unknown"]
    delivery_state: str | None = None
    error_code: str | None = None
    received_at_ms: int | None = None
    rendered_card: dict[str, Any] | None = None


class NewsTimelineStepData(ExactApiSchema):
    stage: Literal["received", "gate", "evidence", "semantic", "notify", "delivery"]
    title_zh: str
    at_ms: int
    summary_zh: str
    facts: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------------------------------ EventUpdate (#706)
class NewsUpdateSourceData(ExactApiSchema):
    """Where one piece of evidence came from, as ingestion knew it; the authority is code-owned."""

    publisher_id: str
    artifact_id: str
    origin_id: str | None = None
    attribution: str | None = None
    published_at_ms: int | None = None
    first_available_at_ms: int
    url: str | None = None
    source_authority: SourceAuthority
    source_authority_zh: str = ""


class NewsClaimCitationData(ExactApiSchema):
    evidence_ref: str
    quote: str
    source: NewsUpdateSourceData | None = None


class NewsClaimQuantityData(ExactApiSchema):
    name: str
    # The source's exact decimal text; the console never does arithmetic on it.
    value: str
    unit: str
    period: str | None = None


class NewsClaimAssetData(ExactApiSchema):
    symbol: str
    market_type: MarketType
    role: Literal["primary", "mentioned"]


class NewsRelationCountsData(ExactApiSchema):
    supports: int = 0
    refutes: int = 0
    reports: int = 0
    not_addressed: int = 0
    unresolved: int = 0


class NewsClaimData(ExactApiSchema):
    """One adopted claim with its own mode, phase, time, conditions and quantities -- never merged."""

    ref: str
    statement: str
    retired: bool = False
    superseded: bool = False
    subject: str
    action: str
    object: str = ""
    speaker: str | None = None
    conditions: list[str] = Field(default_factory=list)
    quantities: list[NewsClaimQuantityData] = Field(default_factory=list)
    effective_at: str | None = None
    occurred_at: str | None = None
    statistical_period: str | None = None
    polarity: Literal["affirmative", "negative", "unknown"]
    polarity_zh: str = ""
    mode: Mode
    mode_zh: str = ""
    actor_role: str | None = None
    actor_role_zh: str = ""
    # `null` for a claim that is not an action; a future `effective_at` never changes it.
    phase: Phase | None = None
    phase_zh: str = ""
    content_kind: ContentKind
    content_kind_zh: str = ""
    assets: list[NewsClaimAssetData] = Field(default_factory=list)
    citations: list[NewsClaimCitationData] = Field(min_length=1)
    first_available_at_ms: int
    antecedent_refs: list[str] = Field(default_factory=list)
    relation_counts: NewsRelationCountsData
    # One source refutes what another supports or reports.
    disputed: bool = False


class NewsClaimChangeData(ExactApiSchema):
    """What this revision changed relative to an earlier claim. An unfound earlier claim stays unknown."""

    kind: ChangeKind
    kind_zh: str = ""
    current_ref: str
    current_statement: str = ""
    previous_ref: str | None = None
    previous_content_ref: str | None = None
    previous_statement: str | None = None
    previous_event_id: str | None = None
    relation: Relation | None = None
    relation_zh: str = ""


class NewsEvidenceRelationData(ExactApiSchema):
    claim_ref: str
    evidence_ref: str
    relation: Literal["supports", "refutes", "reports", "not_addressed", "unresolved"]
    relation_zh: str = ""
    claim_statement: str = ""


class NewsUpdateEvidenceData(ExactApiSchema):
    """One cited source and how it bears on each claim; a relation describes the material, not trust."""

    evidence_ref: str
    text: str
    text_truncated: bool = False
    source: NewsUpdateSourceData
    relations: list[NewsEvidenceRelationData] = Field(default_factory=list)


class NewsImplicationData(ExactApiSchema):
    """An explanatory hypothesis, never a fact, a price call or a corroboration. ``origin`` says whose."""

    claim_refs: list[str]
    channel: str
    explanation: str
    conditions: list[str] = Field(default_factory=list)
    origin: Literal["reported_causality", "system_hypothesis"]
    origin_zh: str = ""


class NewsOpenQuestionData(ExactApiSchema):
    question: str
    claim_refs: list[str]
    target_ref: str | None = None


class NewsTopicData(ExactApiSchema):
    code: str
    label_zh: str


class NewsEventUpdateData(ExactApiSchema):
    """The Event's adopted EventUpdate head: what happened, what changed, who says so, what is missing."""

    update_ref: str
    content_revision: str = Field(pattern=_SHA256_PATTERN)
    input_revision: int = Field(ge=1)
    previous_content_revision: str | None = None
    adopted_at_ms: int
    scope_repair_id: str | None = None
    # The card headline a reader actually received for the latest sent update, else the first claim the
    # head has not retired.
    headline: str | None = None
    headline_source: Literal["sent_card", "claim"] | None = None
    topics: list[NewsTopicData] = Field(default_factory=list)
    claims: list[NewsClaimData]
    retired_claim_refs: list[str] = Field(default_factory=list)
    disputed_claim_refs: list[str] = Field(default_factory=list)
    changes: list[NewsClaimChangeData] = Field(default_factory=list)
    sources: list[NewsUpdateEvidenceData] = Field(default_factory=list)
    implications: list[NewsImplicationData] = Field(default_factory=list)
    open_questions: list[NewsOpenQuestionData] = Field(default_factory=list)


class NewsSemanticWorkData(ExactApiSchema):
    state: Literal["pending", "done", "failed"]
    state_zh: str = ""
    wanted_revision: int
    done_revision: int | None = None
    attempts: int = 0
    last_outcome: str | None = None
    last_error_code: str | None = None
    next_attempt_at_ms: int | None = None
    extra_read_state: str | None = None
    extra_read_state_zh: str = ""
    updated_at_ms: int


class NewsSemanticObservationData(ExactApiSchema):
    result_id: str
    input_revision: int
    program_identity: str
    completed_at_ms: int
    # `null`: the observation changed no business content and the head stayed where it was.
    adopted_content_revision: str | None = None


class NewsEarlierNotificationData(ExactApiSchema):
    intent_id: str
    event_id: str
    headline_zh: str
    body: str
    received_at_ms: int


class NewsClaimDecisionData(ExactApiSchema):
    claim_ref: str
    statement: str | None = None
    decision: ClaimDecisionValue
    decision_zh: str = ""
    # `reader_v2` reasons, or the named reasons of an `editorial_v1` decision shown as history.
    reason: Literal[ClaimReason, LegacyClaimReason]
    reason_zh: str = ""
    # Reader rows only: what the reader already held, how the card is written, the incremental importance
    # (0..4) and its distribution, and which backend answered.
    novelty: Novelty | None = None
    novelty_zh: str = ""
    render: Render | None = None
    earlier_intent_id: str | None = None
    earlier: NewsEarlierNotificationData | None = None
    importance_threshold: float | None = None
    importance: float | None = None
    importance_probabilities: list[float] | None = None
    reader_backend: ReaderBackend | None = None


class NewsNotificationPlanData(ExactApiSchema):
    origin: Literal["reader_v2", "editorial_v1"]
    action: PlanAction
    action_zh: str = ""
    reason: Literal[PlanReason, "send_outcome_unresolved"]
    reason_zh: str = ""
    key: bool = False
    update_ref: str
    reader_revision: str
    decision_ref: str | None = None
    reader_identity: str | None = None
    claim_decisions: list[NewsClaimDecisionData] = Field(default_factory=list)
    timings: PlanTimings | None = None


class NewsNotificationWorkData(ExactApiSchema):
    state: Literal["pending", "done", "failed"]
    state_zh: str = ""
    content_revision: str
    attempts: int = 0
    # Present on failed work: the error that ended it, kept until the work completes.
    last_error_code: str | None = None
    next_attempt_at_ms: int | None = None
    updated_at_ms: int
    plan: NewsNotificationPlanData | None = None
    plan_error_code: str | None = None


class NewsSentLineData(ExactApiSchema):
    claim_ref: str
    text_zh: str


class NewsUpdateIntentData(ExactApiSchema):
    """One notification intent: what was selected, what happened to it, and the exact text sent."""

    intent_id: str
    content_revision: str | None = None
    claim_refs: list[str] = Field(default_factory=list)
    key: bool = False
    state: Literal["queued", "dead", "sending", "sent", "terminal", "ambiguous"]
    state_zh: str = ""
    error_code: str | None = None
    attempts: int | None = None
    enqueued_at_ms: int | None = None
    attempted_at_ms: int | None = None
    settled_at_ms: int | None = None
    headline_zh: str | None = None
    body: str | None = None
    payload_sha256: str | None = None
    receipt: dict[str, Any] | None = None
    lines: list[NewsSentLineData] = Field(default_factory=list)
    timings: DeliveryTimings | None = None
    plan_timings: PlanTimings | None = None


class NewsProcessingData(ExactApiSchema):
    """What the EventUpdate path did with this Event, from its durable work rows and receipts (#706)."""

    semantic: NewsSemanticWorkData | None = None
    observations: list[NewsSemanticObservationData] = Field(default_factory=list)
    notification: NewsNotificationWorkData | None = None
    intents: list[NewsUpdateIntentData] = Field(default_factory=list)
    # Set when the adopted head's stored document does not decode under the current contract.
    update_error_code: str | None = None


class NewsStoryEventData(ExactApiSchema):
    event_id: str
    headline: str
    reporting_origin: str
    published_at_ms: int | None = None
    opened_at_ms: int
    outcome: NewsOutcomeData
    received_at_ms: int | None = None


class NewsEventStoryData(ExactApiSchema):
    storyline_key: str
    from_ms: int
    to_ms: int
    events: list[NewsStoryEventData] = Field(default_factory=list)
    has_more: bool = False


class NewsEventDetailData(ExactApiSchema):
    """Current EventUpdate, source facts, feedback and actual reader receipts."""

    event: NewsEventData
    outcome: NewsOutcomeData
    event_update: NewsEventUpdateData | None = None
    processing: NewsProcessingData | None = None
    timeline: list[NewsTimelineStepData] = Field(default_factory=list)
    members: list[NewsEventMemberData]
    deliveries: list[NewsDeliveryData]
    evidence_snapshots: list[NewsEvidenceSnapshotData] = Field(default_factory=list)
    reader_receipt: NewsReaderReceiptData
    normalization: list[NewsSymbolNormalizationData] = Field(default_factory=list)
    story: NewsEventStoryData | None = None


class NewsItemRelatedEventData(ExactApiSchema):
    event_id: str
    leader_item_id: str
    member_scopes: list[str]
    match_kinds: list[str]
    focus_fact_text: str
    focus_fact_method: str
    wanted_revision: int | None = None
    done_revision: int | None = None
    semantic_outcome: str | None = None
    semantic_error_code: str | None = None
    adopted_content_revision: str | None = None
    notification_state: str | None = None
    notification_action: str | None = None
    intent_state: str | None = None
    sent_count: int


class NewsItemRelatedEventsData(ExactApiSchema):
    item_id: str
    total_events: int
    events: list[NewsItemRelatedEventData]
    next_cursor: str | None = None


class NewsQuoteRequestData(ExactApiSchema):
    symbol: str = Field(min_length=1, max_length=32)
    market_type: MarketType


class NewsQuoteData(ExactApiSchema):
    """One current quote (#88). `state` is derived when read, never maintained by a timer write.

    `unlisted` and `unavailable` answer different questions — "no venue we poll lists this tag" versus "we have
    not managed to quote it yet" — and neither ever renders as a price of zero. `price_kind` and `change_basis`
    are explicit because a derivative mid must never be presented as a cash-equity last price.
    """

    requested_symbol: str
    market_type: MarketType
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
    received_age_ms: int | None
    source_age_ms: int | None
    effective_age_ms: int | None
    freshness_basis: Literal["source_and_received", "received_only"] | None
    reference_age_ms: int | None
    reference_at_ms: int | None
    state: Literal["fresh", "stale", "unavailable", "unlisted"]
    state_zh: str = ""


class NewsQuotesData(ExactApiSchema):
    quotes: list[NewsQuoteData] = Field(default_factory=list)
    measured_at_ms: int


__all__ = [
    "NewsClaimChangeData",
    "NewsClaimData",
    "NewsClaimDecisionData",
    "NewsDeliveryData",
    "NewsEarlierNotificationData",
    "NewsEventData",
    "NewsEventDetailData",
    "NewsEventMemberData",
    "NewsEventStoryData",
    "NewsEventUpdateData",
    "NewsEvidenceSnapshotData",
    "NewsItemRelatedEventData",
    "NewsItemRelatedEventsData",
    "NewsNotificationPlanData",
    "NewsNotificationWorkData",
    "NewsProcessingData",
    "NewsQuoteData",
    "NewsQuoteRequestData",
    "NewsQuotesData",
    "NewsReaderReceiptData",
    "NewsSemanticWorkData",
    "NewsSentLineData",
    "NewsStoryEventData",
    "NewsTimelineStepData",
    "NewsUpdateEvidenceData",
    "NewsUpdateIntentData",
]
