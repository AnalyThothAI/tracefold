from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import ExactApiSchema
from .events import NewsAcceptedReviewData, NewsEventReactionData


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


__all__ = [
    "NewsMarketReviewData",
    "NewsReviewCoverageBucketData",
    "NewsReviewCoverageData",
    "NewsReviewCoverageIntervalData",
    "NewsReviewData",
    "NewsReviewDirectionData",
    "NewsReviewEventTypeData",
    "NewsReviewEvidenceData",
    "NewsReviewFunnelV2Data",
    "NewsReviewHoldoutData",
    "NewsReviewMagnitudeData",
    "NewsReviewMetaData",
    "NewsReviewMissData",
    "NewsReviewReceiptTruthData",
    "NewsReviewSelectionData",
    "NewsReviewSubmissionReceiptData",
    "NewsReviewSubmitData",
    "NewsReviewSummaryData",
    "NewsReviewTaskData",
    "NewsReviewUnavailableData",
]
