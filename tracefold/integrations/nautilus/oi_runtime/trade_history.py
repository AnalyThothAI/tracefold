"""Bounded, immutable reads of Binance USD-M native trade evidence.

A full time page is not proof of completeness: Binance can return the most recent
rows. Subdivide full windows instead of guessing a forward cursor from that page.
An exact order query uses the native inclusive trade-ID cursor. The caller keeps
unfinished cursors; no partial read is silently presented as a complete history.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.adapters.binance.common.schemas.account import BinanceUserTrade
from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI

TRADE_PAGE_LIMIT = 1_000
TRADE_WINDOW_MS = 7 * 24 * 60 * 60 * 1_000


@dataclass(frozen=True, slots=True)
class TradeHistoryCursor:
    start_ms: int
    end_ms: int
    from_id: int | None = None


@dataclass(frozen=True, slots=True)
class BinanceTradeHistory:
    symbol: str
    order_id: int | None
    requested: tuple[TradeHistoryCursor, ...]
    trades: tuple[BinanceUserTrade, ...]
    remaining: tuple[TradeHistoryCursor, ...]
    requests_used: int

    @property
    def complete(self) -> bool:
        return not self.remaining


class IncompleteTradeHistory(RuntimeError):
    def __init__(self, history: BinanceTradeHistory) -> None:
        super().__init__(f"binance_trade_history_incomplete:{history.symbol}")
        self.history = history


def _validate_trade(trade: BinanceUserTrade, symbol: str, order_id: int | None) -> tuple[int, int]:
    if (
        trade.symbol != symbol
        or trade.id is None
        or trade.id < 0
        or trade.orderId is None
        or trade.orderId <= 0
        or (order_id is not None and trade.orderId != order_id)
        or trade.time is None
        or trade.time <= 0
        or trade.side is None
        or trade.buyer is None
        or trade.maker is None
        or trade.positionSide != "BOTH"
        or (trade.side.value == "BUY") != trade.buyer
    ):
        raise ValueError("binance_native_trade_identity_invalid")
    for value in (trade.qty, trade.price, trade.commission):
        if not Decimal(value).is_finite():
            raise ValueError("binance_native_trade_economics_invalid")
    if Decimal(trade.qty) <= 0 or Decimal(trade.price) <= 0 or not trade.commissionAsset:
        raise ValueError("binance_native_trade_economics_invalid")
    return trade.id, trade.time


def _same_trade(left: BinanceUserTrade, right: BinanceUserTrade) -> bool:
    # Formatting, report UUIDs and retrieval timestamps do not change a trade.
    return (
        left.orderId == right.orderId
        and left.time == right.time
        and left.side == right.side
        and Decimal(left.qty) == Decimal(right.qty)
        and Decimal(left.price) == Decimal(right.price)
        and Decimal(left.commission) == Decimal(right.commission)
        and left.commissionAsset == right.commissionAsset
        and left.maker == right.maker
        and left.positionSide == right.positionSide
    )


async def read_trade_history(
    account: BinanceFuturesAccountHttpAPI,
    *,
    symbol: str,
    cursors: tuple[TradeHistoryCursor, ...],
    max_requests: int,
    order_id: int | None = None,
) -> BinanceTradeHistory:
    """Read at most ``max_requests`` signed pages; exceptions preserve read failure.

    Time windows are inclusive and at most seven days each. Exact-order cursors
    never combine fromId with time filters (Binance rejects that combination).
    Their timestamps bound local output while all that order's IDs are paged.
    """

    if not symbol or not 1 <= max_requests <= 32 or (order_id is not None and order_id <= 0):
        raise ValueError("binance_trade_history_scope_invalid")
    if not cursors or len(cursors) > 64:
        raise ValueError("binance_trade_history_cursor_invalid")
    pending = list(cursors)
    for cursor in pending:
        if (
            cursor.start_ms < 0
            or cursor.end_ms < cursor.start_ms
            or (cursor.from_id is not None and (cursor.from_id < 0 or order_id is None))
        ):
            raise ValueError("binance_trade_history_cursor_invalid")
    trades: dict[int, BinanceUserTrade] = {}
    requests = 0
    while pending and requests < max_requests:
        cursor = pending.pop(0)
        if order_id is None and cursor.end_ms - cursor.start_ms >= TRADE_WINDOW_MS:
            next_start = cursor.start_ms + TRADE_WINDOW_MS
            pending.insert(0, TradeHistoryCursor(next_start, cursor.end_ms))
            cursor = TradeHistoryCursor(cursor.start_ms, next_start - 1)
        page = await account.query_user_trades(
            symbol=symbol,
            order_id=order_id,
            from_id=(cursor.from_id or 0) if order_id is not None else None,
            start_time=cursor.start_ms if order_id is None else None,
            end_time=cursor.end_ms if order_id is None else None,
            limit=TRADE_PAGE_LIMIT,
            recv_window="60000",
        )
        requests += 1
        if len(page) > TRADE_PAGE_LIMIT:
            raise ValueError("binance_trade_history_page_invalid")
        ids: list[int] = []
        for trade in page:
            identity, trade_time = _validate_trade(trade, symbol, order_id)
            ids.append(identity)
            if order_id is not None and identity < (cursor.from_id or 0):
                raise ValueError("binance_trade_history_cursor_regressed")
            if order_id is None and not cursor.start_ms <= trade_time <= cursor.end_ms:
                raise ValueError("binance_trade_history_window_violated")
            previous = trades.get(identity)
            if previous is not None and not _same_trade(previous, trade):
                raise ValueError("binance_native_trade_identity_conflict")
            if cursor.start_ms <= trade_time <= cursor.end_ms:
                trades[identity] = trade
        if len(page) < TRADE_PAGE_LIMIT:
            continue
        if order_id is not None:
            if len(set(ids)) != len(ids):
                raise ValueError("binance_trade_history_page_duplicate")
            pending.insert(0, TradeHistoryCursor(cursor.start_ms, cursor.end_ms, max(ids) + 1))
        elif cursor.start_ms < cursor.end_ms:
            midpoint = (cursor.start_ms + cursor.end_ms) // 2
            pending[:0] = [
                TradeHistoryCursor(cursor.start_ms, midpoint),
                TradeHistoryCursor(midpoint + 1, cursor.end_ms),
            ]
        else:
            # More than one page may share a millisecond. A symbol-wide time
            # query cannot prove the first ID; retain this work for an exact
            # order read instead of losing same-timestamp fills.
            pending.insert(0, cursor)
            break
    return BinanceTradeHistory(
        symbol=symbol,
        order_id=order_id,
        requested=cursors,
        trades=tuple(sorted(trades.values(), key=lambda trade: (trade.time or 0, trade.id or 0))),
        remaining=tuple(pending),
        requests_used=requests,
    )
