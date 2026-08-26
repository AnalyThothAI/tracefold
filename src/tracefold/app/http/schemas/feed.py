from __future__ import annotations

from typing import Literal

from .common import ExactApiSchema
from .events import NewsEventData, NewsReactionSummaryData
from .news_common import (
    NewsDeliverySummaryData,
    NewsOutcomeData,
    NewsTriageSummaryData,
)


class NewsFeedOiData(ExactApiSchema):
    """#207: the deterministic open-interest judgment behind one `telemetry_deterministic` row.

    `None` on every other admission. These are `oi_judgment_trace()` / `oi_parse_failure()` fields read back
    verbatim so the browser never re-runs `oi_signal_parser_v1` over `leader_title` — a second parser in the
    page would drift from the judge the moment either changed, and every figure here keys a stored verdict.

    The two shapes are told apart by `parsed`: a judged frame carries the four measurements and its rank, an
    unparseable one carries the provider-contract failure instead. `window_ms` / `max_rank_in_window` /
    `whale_oi_ratio_above_bps` / `oi_change_at_least_bps` are the thresholds *this frame ran under*, from its
    own trace, so a retuned `news.oi` never rewrites the history of a decision it did not make.
    """

    parsed: bool
    rule: str
    # The frame's own subject, from the judge's trace. It is not on `assets`/`grounded_assets`: the Gate
    # grounds those from provider coin tags at admission and a 1019 frame ships none, so this is the only
    # place the feed carries it.
    symbol: str | None = None
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    whale_long_profit_bps: int | None = None
    whale_oi_ratio_bps: int | None = None
    eligible_rank_in_window: int | None = None
    rank_semantics: str | None = None
    window_ms: int | None = None
    max_rank_in_window: int | None = None
    whale_oi_ratio_above_bps: int | None = None
    oi_change_at_least_bps: int | None = None
    parser_version: str | None = None
    failure_stage: str | None = None
    title_sha256: str | None = None


class NewsFeedEventData(NewsEventData):
    title_zh: str | None = None
    outcome: NewsOutcomeData
    triage: NewsTriageSummaryData | None = None
    delivery: NewsDeliverySummaryData | None = None
    # #88: the fixed 1H/4H return after this Event. Current quotes are deliberately *not* here — they change
    # every few seconds and would make the feed's ETag useless; the browser reads them from /api/news/quotes.
    reaction: NewsReactionSummaryData | None = None
    oi: NewsFeedOiData | None = None


class NewsFeedFiltersData(ExactApiSchema):
    family: str | None = None
    admission: str | None = None
    decision: str | None = None
    symbol: str | None = None
    q: str | None = None
    limit: int
    outcome: Literal["pushed", "held", "pending"] | None = None
    hours: int | None = None
    oi: Literal["all", "pushed", "withheld", "parse_failed"] | None = None
    # Comma-separated canonical values. The feed toolbar treats each axis as a multi-select and the server
    # remains the authority over the result set; echoing the normalized query keeps the response auditable.
    direction: str | None = None
    channel: str | None = None


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


__all__ = [
    "NewsFeedCountsData",
    "NewsFeedData",
    "NewsFeedEventData",
    "NewsFeedFiltersData",
    "NewsFeedOiData",
]
