from __future__ import annotations

from typing import Literal

from .common import ExactApiSchema
from .events import NewsEventData, NewsReactionSummaryData
from .news_common import (
    NewsDeliverySummaryData,
    NewsOutcomeData,
    NewsTriageSummaryData,
)


class NewsFeedEventData(NewsEventData):
    outcome: NewsOutcomeData
    triage: NewsTriageSummaryData | None = None
    delivery: NewsDeliverySummaryData | None = None
    # #88: the fixed 1H/4H return after this Event. Current quotes are deliberately *not* here — they change
    # every few seconds and would make the feed's ETag useless; the browser reads them from /api/news/quotes.
    reaction: NewsReactionSummaryData | None = None


class NewsFeedFiltersData(ExactApiSchema):
    event_family: str | None = None
    change_state: str | None = None
    assertion_status: str | None = None
    source_authority: str | None = None
    subject_code: str | None = None
    final_decision: str | None = None
    event_kind: str | None = None
    admission: str | None = None
    symbol: str | None = None
    q: str | None = None
    limit: int
    outcome: Literal["pushed", "held", "pending"] | None = None
    hours: int | None = None
    # Comma-separated canonical values. The server owns normalization and echoes the exact applied selection.
    direction: str | None = None


class NewsFeedCountsData(ExactApiSchema):
    """How the request's filter and window split across the three outcome groups, for the feed's task tabs.

    ``total`` is the sum of the other three: the groups partition the feed exactly.
    """

    total: int
    pushed: int
    held: int
    pending: int


class NewsFeedSearchData(ExactApiSchema):
    mode: Literal["asset", "text"]
    normalized_query: str
    resolved_symbols: list[str]


class NewsFeedData(ExactApiSchema):
    events: list[NewsFeedEventData]
    next_cursor: str | None = None
    # First page only — a paged request reuses the counts the first page already reported.
    counts: NewsFeedCountsData | None = None
    filters: NewsFeedFiltersData
    search: NewsFeedSearchData | None


__all__ = [
    "NewsFeedCountsData",
    "NewsFeedData",
    "NewsFeedEventData",
    "NewsFeedFiltersData",
    "NewsFeedSearchData",
]
