"""What one `base_symbol` *is*, for the token page's identity card (#207 PR-W1).

Deliberately identity only. Everything else the page shows already has an endpoint: the Events come from
`/api/news/feed?symbol=`, the price from `/api/news/quotes`, the rank window from `/api/news/status`. A
second copy of any of those here would be a second answer to a question that already has one.

`underlying_key` is deliberately absent even though the design's card shows it. `crypto:{BASE}` is a
Trading identity — `tracefold.trading.contracts.underlying_key` owns it — and a News route emitting it
would be News asserting something about the Signal lane. The card gets it from the trading section, whose
own endpoint resolved it (#207 PR-W4).
"""

from __future__ import annotations

from pydantic import Field

from .common import ExactApiSchema
from .news_common import NewsSymbolNormalizationData


class NewsSymbolContractData(ExactApiSchema):
    """One contract the base names on one venue.

    `reference_only` is the #91 distinction kept visible rather than filtered away: `us.listed` proves a
    ticker exists, not that anyone can trade it, and the page is where an operator asks the first question.
    """

    venue: str
    venue_symbol: str
    instrument_class: str
    quote_asset: str | None = None
    reference_only: bool = False


class NewsSymbolData(ExactApiSchema):
    """The identity card. `known` is false for a base no venue we poll has ever listed — a real answer, and
    not the same as an error: the provider tags symbols the universe has never seen, and the page says so
    rather than 404-ing on a name the reader just clicked."""

    base_symbol: str
    known: bool
    tradeable: bool
    venues: list[str] = Field(default_factory=list)
    contracts: list[NewsSymbolContractData] = Field(default_factory=list)
    normalization: NewsSymbolNormalizationData | None = None


__all__ = ["NewsSymbolContractData", "NewsSymbolData"]
