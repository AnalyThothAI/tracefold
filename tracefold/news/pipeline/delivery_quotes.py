"""Bounded display reads and price anchors; quote failures never change notification decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from ..market_review.pricing import (
    QUOTE_READ_TIMEOUT_SECONDS,
    Candle,
    PriceInstrument,
    PricePoint,
    QuoteRequest,
    parse_change_pct,
    select_candle,
)
from ..models import MarketAsset
from ..tradability import TradabilityMatch
from .runtime import NewsDatabasePort

_DELIVERY_CANDLE_TIMEOUT_SECONDS = 2.0
_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS = 2.0
_DELIVERY_CANDLE_GAP_MS = 90_000
_ONE_HOUR_MS = 3_600_000


async def read_display_quotes(
    db: NewsDatabasePort,
    requests: Sequence[QuoteRequest],
    *,
    now_ms: int,
    name: str,
) -> list[dict[str, Any]]:
    """One `news_quote_snapshots` row per symbol, on one short session, or nothing at all.

    This is the whole of the quote rule shared by the News first card and the market card (#562 §3):
    one bounded read, no transaction held across it, the pricing domain's own budget, and any
    failure -- admission, overrun, timeout, a repository raising -- degrading to no quote rather than
    to a placeholder, a zero or a retry. What "fresh" and "24 h" mean is *not* restated here: the
    read model applies the freshness and reference-age rules in SQL, and `reader_card.quote_line`
    drops anything not fresh, so there is exactly one place each of those constants is read.
    """

    if not requests:
        return []
    try:
        rows = await db.read(
            name,
            lambda repos: repos.price.quotes_for_symbols(list(requests), now_ms=now_ms),
            timeout_seconds=QUOTE_READ_TIMEOUT_SECONDS,
        )
    except Exception:  # price is display-only; all failures degrade to no line
        return []
    return [dict(row) for row in rows or [] if isinstance(row, Mapping)]


async def read_pushed_news(db: NewsDatabasePort, symbol: str, *, now_ms: int, name: str) -> dict[str, Any]:
    """What News has already told this reader about one instrument, on one short session (#582 §3.3).

    The twin of `read_display_quotes`, deliberately: one bounded read on the same lane, the same
    pricing-domain budget, no transaction held across it, and every failure -- admission, overrun,
    timeout, a repository raising -- degrading to "no news to show" rather than to a placeholder or a
    retry. It lives beside the quote read rather than at the composition site because the budget is
    News's own: a Workers adapter deciding for itself how long a card may wait would be a second
    answer to a question this package has already answered.

    What "already told" and "48 h" mean is not restated here: the two statements in
    `storage/decisions.py` own the window, the delivered-card predicate and the alias resolution.

    One thing is enforced rather than assumed: an entry the read could not put a title on never
    reaches the card. The card prints one line per pushed entry and counts what it printed, so a
    titleless row would be either a line saying only a time or a count with nothing under it.
    """

    requested = str(symbol or "").strip()
    if not requested:
        return {"pushed": [], "total": 0}
    try:
        answer = await db.read(
            name,
            lambda repos: repos.news.pushed_news_for_symbol(requested, now_ms=now_ms),
            timeout_seconds=QUOTE_READ_TIMEOUT_SECONDS,
        )
    except Exception:  # display-only; all failures degrade to no line
        return {"pushed": [], "total": 0}
    if not isinstance(answer, Mapping):
        return {"pushed": [], "total": 0}
    rows = answer.get("pushed")
    return {
        "pushed": [
            dict(row)
            for row in (rows if isinstance(rows, Sequence) and not isinstance(rows, str | bytes) else ())
            if isinstance(row, Mapping) and str(row.get("headline_zh") or "").strip()
        ],
        "total": answer.get("total", 0),
    }


DeliveryCandleFetcher = Callable[[str, int, int], Awaitable[Sequence[Candle]]]
DeliveryCandleFetcherFor = Callable[[str], DeliveryCandleFetcher | None]
DeliveryPriceFetcher = Callable[[str, Sequence[int]], Awaitable[Mapping[int, PricePoint]]]
DeliveryPriceFetcherFor = Callable[[str], DeliveryPriceFetcher | None]


class DeliveryQuotes:
    """One complete venue calculation at a time, with bounded whole-source failover."""

    def __init__(
        self,
        db: NewsDatabasePort,
        *,
        candle_fetcher_for: DeliveryCandleFetcherFor | None = None,
        price_fetcher_for: DeliveryPriceFetcherFor | None = None,
    ) -> None:
        self.db = db
        self._candle_fetcher_for = candle_fetcher_for
        self._price_fetcher_for = price_fetcher_for

    async def market_data(
        self,
        shown: Sequence[MarketAsset],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Fresh push prices plus the two historical anchors rendered on the card.

        The caller passes the same code-verified asset list to the renderer, so
        the facts and quote lines cannot describe different symbols. Resolution
        remains owned by PriceRepository. Every price-plane failure returns an
        empty display value and leaves the already-made send decision untouched.
        """

        if not shown:
            return []
        if self._price_fetcher_for is not None:
            return await self._point_market_data(shown, stamp, news_at_ms=news_at_ms)
        quotes = await read_display_quotes(
            self.db,
            [QuoteRequest(asset.symbol, asset.market_type) for asset in shown],
            now_ms=stamp,
            name="news_delivery_quotes",
        )
        if self._candle_fetcher_for is None:
            return quotes
        news_target_ms = (
            news_at_ms
            if isinstance(news_at_ms, int) and not isinstance(news_at_ms, bool) and 0 < news_at_ms <= stamp
            else None
        )
        tasks: list[Awaitable[tuple[int, Sequence[Candle]] | None]] = []
        for index, quote in enumerate(quotes):
            if quote.get("state") != "fresh":
                continue
            venue = str(quote.get("venue") or "").strip()
            venue_symbol = str(quote.get("venue_symbol") or "").strip()
            fetcher = self._candle_fetcher_for(venue) if venue and venue_symbol else None
            if fetcher is None:
                continue
            targets = [stamp - _ONE_HOUR_MS]
            if news_target_ms is not None:
                targets.append(news_target_ms)
            start_ms = min(targets) - _DELIVERY_CANDLE_GAP_MS
            tasks.append(self._delivery_candles(index, fetcher, venue_symbol, start_ms, stamp))
        if not tasks:
            return quotes
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) or result is None:
                continue
            index, candles = result
            hour = select_candle(candles, target_ms=stamp - _ONE_HOUR_MS, max_gap_ms=_DELIVERY_CANDLE_GAP_MS)
            if hour is not None:
                quotes[index]["price_one_hour_before_push"] = str(hour.close)
            if news_target_ms is not None:
                news = select_candle(candles, target_ms=news_target_ms, max_gap_ms=_DELIVERY_CANDLE_GAP_MS)
                if news is not None:
                    quotes[index]["price_at_news"] = str(news.close)
        return quotes

    async def _point_market_data(
        self,
        shown: Sequence[MarketAsset],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Trade-first anchors with whole-calculation venue failover.

        A candidate is accepted only as one unit: current, news and one-hour prices all retain the same
        ``(venue, venue_symbol)``. Partial values are kept only if no later venue can provide the complete set.
        """

        requests = [QuoteRequest(asset.symbol, asset.market_type) for asset in shown]
        try:
            rows, candidates = await self.db.read(
                "news_delivery_price_sources",
                lambda repos: (
                    repos.price.quotes_for_symbols(requests, now_ms=stamp),
                    repos.price.instruments_for_symbols(requests),
                ),
                timeout_seconds=QUOTE_READ_TIMEOUT_SECONDS,
            )
        except Exception:
            return []
        originals = {
            str(row.get("requested_symbol") or ""): dict(row) for row in rows or [] if isinstance(row, Mapping)
        }
        news_target = (
            news_at_ms
            if isinstance(news_at_ms, int) and not isinstance(news_at_ms, bool) and 0 < news_at_ms <= stamp
            else None
        )
        tasks = [
            self._point_quote(
                request.symbol,
                originals.get(request.symbol, {}),
                tuple(candidates.get(request, ())),
                stamp=stamp,
                news_target_ms=news_target,
            )
            for request in requests
        ]
        resolved = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[dict[str, Any]] = []
        for symbol, result in zip((request.symbol for request in requests), resolved, strict=True):
            if isinstance(result, BaseException):
                fallback = originals.get(symbol)
                if fallback:
                    out.append(dict(fallback))
            elif result:
                out.append(result)
        return out

    async def for_matches(
        self,
        matches: Sequence[TradabilityMatch],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Price a freshly discovered exact contract without waiting for the periodic universe snapshot."""

        if not matches:
            return []
        news_target = (
            news_at_ms
            if isinstance(news_at_ms, int) and not isinstance(news_at_ms, bool) and 0 < news_at_ms <= stamp
            else None
        )
        targets = [stamp, stamp - _ONE_HOUR_MS, stamp - 24 * _ONE_HOUR_MS]
        if news_target is not None:
            targets.append(news_target)
        first_placeholder: dict[str, Any] | None = None
        first_partial: dict[str, Any] | None = None
        for match in matches:
            placeholder = {
                "requested_symbol": match.requested_symbol,
                "symbol": match.base_symbol,
                "base_symbol": match.base_symbol,
                "venue": match.venue,
                "venue_symbol": match.venue_symbol,
                "instrument_class": match.instrument_class,
                "quote_asset": match.quote_asset,
                "state": "unavailable",
                "state_zh": "暂无",
            }
            if first_placeholder is None:
                first_placeholder = placeholder
            fetcher = self._price_fetcher_for(match.venue) if self._price_fetcher_for else None
            if fetcher is None:
                continue
            try:
                points = await asyncio.wait_for(
                    fetcher(match.price_symbol, targets),
                    timeout=_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: S112 - one venue failure must fall through to the next exact match
                continue
            if stamp not in points:
                continue
            instrument = PriceInstrument(
                venue=match.venue,
                venue_symbol=match.price_symbol,
                base_symbol=match.base_symbol,
                instrument_class=match.instrument_class,
                quote_asset=match.quote_asset,
            )
            quote = self._quote_from_points(
                match.requested_symbol,
                {},
                instrument,
                points,
                stamp=stamp,
                news_target_ms=news_target,
            )
            quote["venue_symbol"] = match.venue_symbol
            if first_partial is None:
                first_partial = quote
            if all(target in points for target in targets):
                return [quote]
        fallback = first_partial or first_placeholder
        return [fallback] if fallback is not None else []

    async def _point_quote(
        self,
        symbol: str,
        original: Mapping[str, Any],
        instruments: Sequence[PriceInstrument],
        *,
        stamp: int,
        news_target_ms: int | None,
    ) -> dict[str, Any]:
        targets = [stamp, stamp - _ONE_HOUR_MS, stamp - 24 * _ONE_HOUR_MS]
        if news_target_ms is not None:
            targets.append(news_target_ms)
        expected_class = str(instruments[0].instrument_class) if instruments else ""
        candidates = [
            instrument
            for instrument in instruments
            if not expected_class
            or expected_class == "unknown"
            or instrument.instrument_class in {expected_class, "unknown"}
        ] or list(instruments)
        candidates = self._bounded_price_candidates(candidates)
        first_partial: dict[str, Any] | None = None
        seen_contracts: set[tuple[str, str]] = set()
        for instrument in candidates:
            contract = (instrument.venue, instrument.venue_symbol)
            if contract in seen_contracts:
                continue
            seen_contracts.add(contract)
            fetcher = self._price_fetcher_for(instrument.venue) if self._price_fetcher_for else None
            if fetcher is None:
                continue
            try:
                points = await asyncio.wait_for(
                    fetcher(instrument.venue_symbol, targets),
                    timeout=_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: S112 - one provider failure is the signal to try the next venue
                continue
            current = points.get(stamp)
            if current is None:
                continue
            quote = self._quote_from_points(
                symbol,
                original,
                instrument,
                points,
                stamp=stamp,
                news_target_ms=news_target_ms,
            )
            if first_partial is None:
                first_partial = quote
            if all(target in points for target in targets):
                return quote
        return first_partial or dict(original)

    @staticmethod
    def _bounded_price_candidates(instruments: Sequence[PriceInstrument]) -> list[PriceInstrument]:
        """At most two Binance contracts, then one Hyperliquid and one OKX contract."""

        limits = {"binance": 2, "hl": 1, "okx": 1}
        counts = {family: 0 for family in limits}
        out: list[PriceInstrument] = []
        for instrument in instruments:
            family = instrument.venue.split(".", 1)[0]
            if family not in limits or counts[family] >= limits[family]:
                continue
            counts[family] += 1
            out.append(instrument)
        return out

    @staticmethod
    def _quote_from_points(
        symbol: str,
        original: Mapping[str, Any],
        instrument: PriceInstrument,
        points: Mapping[int, PricePoint],
        *,
        stamp: int,
        news_target_ms: int | None,
    ) -> dict[str, Any]:
        current = points[stamp]
        same_snapshot = (
            str(original.get("venue") or "") == instrument.venue
            and str(original.get("venue_symbol") or "") == instrument.venue_symbol
            and original.get("state") == "fresh"
        )
        quote: dict[str, Any] = {
            "requested_symbol": symbol,
            "symbol": instrument.base_symbol,
            "base_symbol": instrument.base_symbol,
            "venue": instrument.venue,
            "venue_symbol": instrument.venue_symbol,
            "instrument_class": instrument.instrument_class,
            "quote_asset": instrument.quote_asset,
            "price": str(current.price),
            "price_kind": "last",
            "price_kind_zh": "成交价",
            "source_at_ms": current.at_ms,
            "received_at_ms": stamp,
            "age_ms": max(0, stamp - current.at_ms),
            "state": "fresh",
            "state_zh": "实时",
            "delivery_price_basis": current.basis,
            "change_pct": original.get("change_pct") if same_snapshot else None,
            "change_basis": original.get("change_basis") if same_snapshot else None,
            "change_basis_zh": original.get("change_basis_zh") if same_snapshot else None,
        }
        hour = points.get(stamp - _ONE_HOUR_MS)
        if hour is not None:
            quote["price_one_hour_before_push"] = str(hour.price)
            quote["price_one_hour_before_push_basis"] = hour.basis
        day = points.get(stamp - 24 * _ONE_HOUR_MS)
        if day is not None:
            # `pricing.parse_change_pct` is the only place two prices become a day change (#562).
            change_pct = parse_change_pct(current.price, day.price)
            if change_pct is not None:
                quote["change_pct"] = change_pct
                quote["change_basis"] = "rolling_24h"
                quote["change_basis_zh"] = "24 小时"
        if news_target_ms is not None:
            news = points.get(news_target_ms)
            if news is not None:
                quote["price_at_news"] = str(news.price)
                quote["price_at_news_basis"] = news.basis
        return quote

    async def _delivery_candles(
        self,
        index: int,
        fetcher: DeliveryCandleFetcher,
        venue_symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[int, Sequence[Candle]] | None:
        try:
            candles = await asyncio.wait_for(
                fetcher(venue_symbol, start_ms, end_ms),
                timeout=_DELIVERY_CANDLE_TIMEOUT_SECONDS,
            )
        except Exception:
            return None
        return index, candles
