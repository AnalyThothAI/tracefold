"""On-demand delivery price points: trade first, closed one-minute candle second."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from tracefold.news.market_review.pricing import Candle, PricePoint, Trade, select_candle, select_trade

from .candles import candle_fetcher_for
from .trades import (
    fetch_binance_trade_before,
    fetch_bitget_recent_trades,
    fetch_hyperliquid_recent_trades,
    fetch_lighter_recent_trades,
    fetch_okx_recent_trades,
)

_TRADE_MAX_GAP_MS = 60_000
_CANDLE_MAX_GAP_MS = 90_000


async def fetch_delivery_price_points(
    venue_symbol: str,
    *,
    venue: str,
    targets_ms: Sequence[int],
) -> Mapping[int, PricePoint]:
    """Resolve each anchor independently while keeping every result on one venue and contract."""

    targets = tuple(dict.fromkeys(int(target) for target in targets_ms if int(target) > 0))
    if not targets:
        return {}
    trades = await _trades_for_targets(venue_symbol, venue=venue, targets=targets)
    points: dict[int, PricePoint] = {}
    for target in targets:
        trade = select_trade(trades.get(target, ()), target_ms=target, max_gap_ms=_TRADE_MAX_GAP_MS)
        if trade is not None:
            points[target] = PricePoint(at_ms=trade.traded_at_ms, price=trade.price, basis="trade")
    missing = [target for target in targets if target not in points]
    if not missing:
        return points
    candles = await _candles_for_targets(venue_symbol, venue=venue, targets=missing)
    for target in missing:
        candle = select_candle(candles, target_ms=target, max_gap_ms=_CANDLE_MAX_GAP_MS)
        if candle is not None:
            points[target] = PricePoint(at_ms=candle.close_at_ms, price=candle.close, basis="candle_1m")
    return points


async def _trades_for_targets(
    venue_symbol: str,
    *,
    venue: str,
    targets: Sequence[int],
) -> Mapping[int, Sequence[Trade]]:
    if venue.startswith("binance."):
        calls = [fetch_binance_trade_before(venue_symbol, venue=venue, target_ms=target) for target in targets]
        results = await asyncio.gather(*calls, return_exceptions=True)
        return {
            target: result
            for target, result in zip(targets, results, strict=True)
            if not isinstance(result, BaseException)
        }
    try:
        if venue.startswith("hl."):
            recent = await fetch_hyperliquid_recent_trades(venue_symbol, venue=venue)
        elif venue.startswith("okx."):
            recent = await fetch_okx_recent_trades(venue_symbol, venue=venue)
        elif venue.startswith("lighter."):
            recent = await fetch_lighter_recent_trades(venue_symbol, venue=venue)
        elif venue.startswith("bitget."):
            recent = await fetch_bitget_recent_trades(venue_symbol, venue=venue)
        else:
            return {}
    except Exception:
        return {}
    return {target: recent for target in targets}


async def _candles_for_targets(venue_symbol: str, *, venue: str, targets: Sequence[int]) -> Sequence[Candle]:
    calls = [
        _candles_for_target(venue_symbol, venue=venue, target_ms=target)
        for target in dict.fromkeys(int(value) for value in targets)
    ]
    results = await asyncio.gather(*calls, return_exceptions=True)
    return tuple(candle for result in results if not isinstance(result, BaseException) for candle in result)


async def _candles_for_target(venue_symbol: str, *, venue: str, target_ms: int) -> Sequence[Candle]:
    """Every venue this package can fetch candles from: a delivery price is a historical fact about the
    market the instrument actually trades on, and there is no rank to fall back down."""

    fetcher = candle_fetcher_for(venue)
    if fetcher is None:
        return ()
    return await fetcher(
        venue_symbol,
        venue=venue,
        start_ms=int(target_ms) - _CANDLE_MAX_GAP_MS,
        end_ms=int(target_ms),
        interval="1m",
    )


__all__ = ["fetch_delivery_price_points"]
