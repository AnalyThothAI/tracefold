from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from tracefold.news import EventKind, FactKind, SourceAuthority
from tracefold.news.updates.contracts import ChangeKind, ContentKind, Mode, Phase, Relation
from tracefold.news.updates.notification import ClaimDecisionValue, ClaimReason, PlanAction, PlanReason

from .common import ExactApiSchema
from .news_common import (
    NewsAssetRefData,
    NewsLegacyVerdictData,
    NewsOutcomeData,
    NewsSymbolNormalizationData,
    NewsTriageAssetData,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


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


class NewsPresentationVerdictData(ExactApiSchema):
    novelty: Literal["new_fact", "progression", "restatement"]
    restates: int = Field(ge=-1)
    assets: list[NewsTriageAssetData] = Field(default_factory=list, max_length=8)
    direction: Literal["bullish", "bearish", "neutral", "unclear"]
    scope: Literal["macro", "sector", "single_name"]
    # `null` on a verdict written under `news_judgment_v2` and on a degraded one (#675 §1). Those rows
    # are audit truth and are never rewritten, so the detail page shows no fact-kind badge for them.
    fact_kind: FactKind | None = None
    evidence_ref: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    headline_zh: str = Field(min_length=1, max_length=60)
    why_zh: str = Field(default="", max_length=140)


class NewsLegacyTaxonomyData(ExactApiSchema):
    """The four retired taxonomy axes exactly as one legacy verdict stored them (#706).

    Audit only. The axes have no current owner or reading, so no vocabulary is applied and nothing is
    validated against the retired enums: a stored code is published as stored, a missing one as ``null``.
    """

    subject_codes: list[str] = Field(default_factory=list)
    event_family: str | None = None
    change_state: str | None = None
    assertion_status: str | None = None


class NewsModelEditorialData(ExactApiSchema):
    """The editorial sibling of one legacy model verdict, in the `news_editorial_v4` read shape.

    ``source_authority`` is code-owned and always present; ``taxonomy`` is the retired taxonomy Predictor's
    stored answer and is ``null`` when that call failed on its own, in which case ``taxonomy_status`` reads
    ``unavailable`` and ``taxonomy_error_code`` names the `news_program_*` code. Verdicts written under
    `news_editorial_v2` are projected into this shape at the storage read boundary, so a historical row
    reads as ``available`` with its authority lifted out of the taxonomy object (#651 §5.3), and a
    `news_editorial_v3` row loses the `relevance` object the Program no longer produces (#675 §1).
    """

    source_authority: SourceAuthority
    source_authority_zh: str = ""
    taxonomy: NewsLegacyTaxonomyData | None = None
    taxonomy_status: Literal["available", "unavailable"] = "available"
    taxonomy_error_code: str | None = None

    @model_validator(mode="after")
    def taxonomy_status_matches_taxonomy(self) -> NewsModelEditorialData:
        available = self.taxonomy_status == "available"
        if available != (self.taxonomy is not None) or available != (self.taxonomy_error_code is None):
            raise ValueError("news_model_editorial_taxonomy_status_mismatch")
        return self


class NewsVerdictData(ExactApiSchema):
    stage: str
    policy_version: str
    judgment_contract_version: Literal["news_judgment_v2", "news_judgment_v3"]
    judgment_origin: Literal["model", "oi", "liquidation", "degraded"]  # historical origins remain readable
    judgment_sha256: str = Field(pattern=_SHA256_PATTERN)
    verdict: NewsPresentationVerdictData
    model_editorial: NewsModelEditorialData | None = None
    rule_baseline_decision: Literal["push", "escalate", "drop", "throttled"]
    final_decision: Literal["push", "escalate", "drop", "throttled"]
    override_rule: str | None = None
    throttled_by: str | None = None
    model: str | None = Field(default=None, min_length=1)
    model_usage_coverage: Literal["complete", "partial", "unknown"] = "unknown"
    model_input_tokens: int | None = Field(default=None, ge=0)
    model_output_tokens: int | None = Field(default=None, ge=0)
    model_provider_cost_microusd: int | None = Field(default=None, ge=0)
    program_version: str = Field(min_length=1)
    program_sha256: str = Field(pattern=_SHA256_PATTERN)
    degraded: bool = False
    error_code: str | None = None
    evidence_version: int = Field(ge=0)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    focus_fact_id: str = Field(min_length=1)
    published_at_ms: int | None = None
    created_at_ms: int

    @model_validator(mode="after")
    def model_identity_matches_origin(self) -> NewsVerdictData:
        is_model = self.judgment_origin == "model"
        if is_model != (self.model_editorial is not None) or is_model != (self.model is not None):
            raise ValueError("news_verdict_model_identity_origin_mismatch")
        if (self.judgment_origin == "degraded") != self.degraded:
            raise ValueError("news_verdict_degraded_origin_mismatch")
        return self


class NewsDeliveryData(ExactApiSchema):
    # A legacy card's deterministic `legacy_intent:*` id, or an EventUpdate intent's `intent:*` id (#706).
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


class NewsAcceptedReviewData(ExactApiSchema):
    """Typed current Review summary; the stored submission payload is audit-only."""

    review_id: str
    subject_kind: Literal["event", "external_miss", "pairwise"]
    event_id: str | None = None
    external_snapshot_id: str | None = None
    should_push: Literal["must_push", "should_push", "should_hold", "must_hold", "uncertain"] | None = None
    first_bad_owner: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    expected_correction: str = ""
    note: str = ""
    reviewer: str
    created_at_ms: int
    rubric_version: str
    reader_contract_version: str
    pairwise_case_id: str | None = None


class NewsEventReviewSummaryData(ExactApiSchema):
    judgment_n: int = 0
    accepted: NewsAcceptedReviewData | None = None
    uncertain: bool = False


class NewsTimelineStepData(ExactApiSchema):
    # `triage`/`decide` are a legacy verdict's steps; `evidence`/`semantic`/`notify` the EventUpdate path's.
    stage: Literal["received", "gate", "triage", "decide", "evidence", "semantic", "notify", "delivery"]
    title_zh: str
    at_ms: int
    summary_zh: str
    facts: dict[str, Any] = Field(default_factory=dict)


class NewsEvidenceSpanData(ExactApiSchema):
    ref_id: str
    material_kind: Literal["current", "related"]
    source_item_id: str
    document_id: str | None = None
    source_artifact_id: str
    content_sha256: str
    extraction_version: str
    text_space: str
    span_start: int
    span_end: int
    text: str
    source: str
    url: str
    reported_published_at_ms: int | None
    available_at_ms: int | None
    selection_reason: str
    coverage_status: str


class NewsEvidenceInputData(ExactApiSchema):
    execution_index: int
    status: str
    selected: bool
    focus_fact_id: str
    input_version: str
    cutoff_at_ms: int
    current_evidence: list[NewsEvidenceSpanData]
    related_evidence: list[NewsEvidenceSpanData]
    missing: list[str]
    exclusions: list[str]
    document_status: str | None = None
    document_receipt: dict[str, Any] | None = None
    candidate_count: int
    selected_count: int
    declared_source_refs: list[str]
    reference_issues: list[str]
    elapsed_ms: int


class NewsLateEvidenceData(ExactApiSchema):
    material_id: str
    material_kind: str
    available_at_ms: int


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
    market_type: Literal["crypto", "equity", "commodity", "index", "forex", "fund", "unknown"]
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


class NewsClaimDecisionData(ExactApiSchema):
    claim_ref: str
    statement: str | None = None
    decision: ClaimDecisionValue
    decision_zh: str = ""
    reason: ClaimReason
    reason_zh: str = ""


class NewsNotificationPlanData(ExactApiSchema):
    action: PlanAction
    action_zh: str = ""
    reason: PlanReason
    reason_zh: str = ""
    key: bool = False
    update_ref: str
    reader_revision: str
    claim_decisions: list[NewsClaimDecisionData] = Field(default_factory=list)


class NewsNotificationWorkData(ExactApiSchema):
    state: Literal["pending", "done"]
    state_zh: str = ""
    content_revision: str
    attempts: int = 0
    next_attempt_at_ms: int | None = None
    updated_at_ms: int
    plan: NewsNotificationPlanData | None = None
    plan_error_code: str | None = None


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


class NewsProcessingData(ExactApiSchema):
    """What the EventUpdate path did with this Event, from its durable work rows and receipts (#706)."""

    semantic: NewsSemanticWorkData | None = None
    observations: list[NewsSemanticObservationData] = Field(default_factory=list)
    notification: NewsNotificationWorkData | None = None
    intents: list[NewsUpdateIntentData] = Field(default_factory=list)
    # Set when the adopted head's stored document does not decode under the current contract.
    update_error_code: str | None = None


class NewsEventDetailData(ExactApiSchema):
    """One Event. ``event_update``/``processing`` are the EventUpdate path (#706); ``legacy_verdict``,
    ``verdicts``, ``evidence_inputs`` and ``late_evidence`` are the history of an Event judged before it,
    and none of them is merged into the other."""

    event: NewsEventData
    outcome: NewsOutcomeData
    event_update: NewsEventUpdateData | None = None
    processing: NewsProcessingData | None = None
    legacy_verdict: NewsLegacyVerdictData | None = None
    timeline: list[NewsTimelineStepData] = Field(default_factory=list)
    members: list[NewsEventMemberData]
    verdicts: list[NewsVerdictData]
    deliveries: list[NewsDeliveryData]
    review: NewsEventReviewSummaryData
    late_evidence: list[NewsLateEvidenceData] = Field(default_factory=list)
    evidence_inputs: list[NewsEvidenceInputData] = Field(default_factory=list)
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
    received_age_ms: int | None
    source_age_ms: int | None
    effective_age_ms: int | None
    freshness_basis: Literal["source_and_received", "received_only"] | None
    reference_at_ms: int | None
    reference_age_ms: int | None
    state: Literal["fresh", "stale", "unavailable", "unlisted"]
    state_zh: str = ""


class NewsQuotesData(ExactApiSchema):
    quotes: list[NewsQuoteData] = Field(default_factory=list)
    measured_at_ms: int


__all__ = [
    "NewsAcceptedReviewData",
    "NewsClaimChangeData",
    "NewsClaimData",
    "NewsClaimDecisionData",
    "NewsDeliveryData",
    "NewsEventData",
    "NewsEventDetailData",
    "NewsEventMemberData",
    "NewsEventReactionData",
    "NewsEventReviewSummaryData",
    "NewsEventUpdateData",
    "NewsEvidenceSnapshotData",
    "NewsLegacyTaxonomyData",
    "NewsModelEditorialData",
    "NewsNotificationPlanData",
    "NewsNotificationWorkData",
    "NewsPresentationVerdictData",
    "NewsProcessingData",
    "NewsQuoteData",
    "NewsQuotesData",
    "NewsReactionSummaryData",
    "NewsReaderReceiptData",
    "NewsSemanticWorkData",
    "NewsTimelineStepData",
    "NewsUpdateEvidenceData",
    "NewsUpdateIntentData",
    "NewsVerdictData",
]
