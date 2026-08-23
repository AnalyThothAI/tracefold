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
    decision: str | None = None
    symbol: str | None = None
    q: str | None = None
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


__all__ = ["NewsFeedCountsData", "NewsFeedData", "NewsFeedEventData", "NewsFeedFiltersData"]
