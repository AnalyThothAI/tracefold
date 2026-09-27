from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ExactApiSchema
from .events import NewsEventData, NewsReactionSummaryData
from .news_common import (
    NewsDeliverySummaryData,
    NewsLegacyVerdictData,
    NewsOutcomeData,
)


class NewsFeedUpdateData(ExactApiSchema):
    """The adopted EventUpdate head of one feed row (#706), in the slim shape a list needs.

    ``headline`` is the card headline a reader actually received for this Event's latest sent update, else
    the first claim the head has not retired. It is ``null`` only when every claim is retired.
    """

    content_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    adopted_at_ms: int
    claim_n: int = Field(ge=0)
    headline: str | None = None
    headline_source: Literal["sent_card", "claim"] | None = None


class NewsFeedEventData(NewsEventData):
    outcome: NewsOutcomeData
    update: NewsFeedUpdateData | None = None
    # History only: the Triage verdict of an Event judged before #706.
    legacy_verdict: NewsLegacyVerdictData | None = None
    delivery: NewsDeliverySummaryData | None = None
    # #88: the fixed 1H/4H return after this Event. Current quotes are deliberately *not* here — they change
    # every few seconds and would make the feed's ETag useless; the browser reads them from /api/news/quotes.
    reaction: NewsReactionSummaryData | None = None


class NewsFeedFiltersData(ExactApiSchema):
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
    "NewsFeedUpdateData",
]
