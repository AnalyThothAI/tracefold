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

    `None` on every other admission. The server assembles these fields from the typed judgment atom and its
    current source metadata, so the browser never re-runs `oi_signal_parser_v1` over `leader_title`.

    The two shapes are told apart by `parsed`: a judged frame carries the four measurements, an unparseable
    one carries the provider-contract failure instead. Neither carries a threshold any more (#458): the lane
    stopped deciding whether a reader is told, so there is no number left for a frame to say it ran under.
    """

    parsed: bool
    rule: str
    # The frame's parsed subject, from the judge's trace. `assets` separately carries the durable, resolved
    # market identity written after that judgment; `grounded_assets` remains empty provider/Gate evidence.
    symbol: str | None = None
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    whale_long_profit_bps: int | None = None
    whale_oi_ratio_bps: int | None = None
    parser_version: str | None = None
    failure_stage: str | None = None
    title_sha256: str | None = None


class NewsFeedEventData(NewsEventData):
    outcome: NewsOutcomeData
    triage: NewsTriageSummaryData | None = None
    delivery: NewsDeliverySummaryData | None = None
    # #88: the fixed 1H/4H return after this Event. Current quotes are deliberately *not* here — they change
    # every few seconds and would make the feed's ETag useless; the browser reads them from /api/news/quotes.
    reaction: NewsReactionSummaryData | None = None
    oi: NewsFeedOiData | None = None


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
    oi: Literal["all", "pushed", "withheld", "parse_failed"] | None = None
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
    "NewsFeedOiData",
    "NewsFeedSearchData",
]
