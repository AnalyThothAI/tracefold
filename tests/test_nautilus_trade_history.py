"""Native trade history completeness across the real Binance row decoder."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from typing import Any

import msgspec
import pytest
from nautilus_trader.adapters.binance.common.schemas.account import BinanceUserTrade

from tracefold.integrations.nautilus.oi_runtime.trade_history import (
    TRADE_PAGE_LIMIT,
    TRADE_WINDOW_MS,
    TradeHistoryCursor,
    read_trade_history,
)


class TradeEndpoint:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []
        self.fail_at: int | None = None

    async def query_user_trades(self, **kwargs: Any) -> list[BinanceUserTrade]:
        self.calls.append(kwargs)
        if self.fail_at == len(self.calls):
            raise TimeoutError("signed trade request timed out")
        assert not (kwargs["from_id"] is not None and kwargs["start_time"] is not None)
        selected = [
            row
            for row in self.rows
            if (kwargs["start_time"] is None or row["time"] >= kwargs["start_time"])
            and (kwargs["end_time"] is None or row["time"] <= kwargs["end_time"])
            and (kwargs["from_id"] is None or row["id"] >= kwargs["from_id"])
            and (kwargs["order_id"] is None or row["orderId"] == kwargs["order_id"])
        ]
        selected = sorted(selected, key=lambda row: row["id"])
        # Returning the last page of a time interval must not silently drop the
        # first trades. fromId, in contrast, requests IDs starting at its cursor.
        selected = selected[-kwargs["limit"] :] if kwargs["from_id"] is None else selected[: kwargs["limit"]]
        return msgspec.json.decode(msgspec.json.encode(selected), type=list[BinanceUserTrade])


def trade(identity: int, *, time: int | None = None, **changes: Any) -> dict[str, Any]:
    return {
        "symbol": "INJUSDT",
        "id": identity,
        "orderId": 308654865,
        "time": time or (1_000 + identity),
        "side": "SELL",
        "positionSide": "BOTH",
        "buyer": False,
        "maker": False,
        "qty": "1.0",
        "price": "0.8562",
        "commission": "0.1",
        "commissionAsset": "USDT",
        **changes,
    }


def test_full_time_page_is_subdivided_and_every_native_id_is_kept_once() -> None:
    endpoint: Any = TradeEndpoint([trade(i) for i in range(1, 1_003)])
    result = asyncio.run(
        read_trade_history(endpoint, symbol="INJUSDT", cursors=(TradeHistoryCursor(1_000, 3_000),), max_requests=8)
    )
    assert result.complete
    assert [row.id for row in result.trades] == list(range(1, 1_003))
    assert result.requests_used == 5
    assert all(call["limit"] == 1_000 and call["recv_window"] == "60000" for call in endpoint.calls)
    with pytest.raises(FrozenInstanceError):
        result.symbol = "BTCUSDT"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.trades[0].qty = "2"  # type: ignore[misc]


def test_request_budget_leaves_explicit_resumable_windows() -> None:
    endpoint: Any = TradeEndpoint([trade(i) for i in range(1, 1_003)])
    first = asyncio.run(
        read_trade_history(endpoint, symbol="INJUSDT", cursors=(TradeHistoryCursor(1_000, 3_000),), max_requests=1)
    )
    assert not first.complete and first.requests_used == 1
    resumed = asyncio.run(read_trade_history(endpoint, symbol="INJUSDT", cursors=first.remaining, max_requests=8))
    assert resumed.complete
    assert {row.id for row in (*first.trades, *resumed.trades)} == set(range(1, 1_003))


def test_dense_same_millisecond_requires_exact_order_pagination() -> None:
    endpoint: Any = TradeEndpoint([trade(i, time=1_000) for i in range(1, 1_002)])
    truncated = asyncio.run(
        read_trade_history(endpoint, symbol="INJUSDT", cursors=(TradeHistoryCursor(1_000, 1_000),), max_requests=8)
    )
    assert not truncated.complete and truncated.requests_used == 1
    complete = asyncio.run(
        read_trade_history(
            endpoint,
            symbol="INJUSDT",
            order_id=308654865,
            cursors=(TradeHistoryCursor(1_000, 1_000),),
            max_requests=8,
        )
    )
    assert complete.complete
    assert [row.id for row in complete.trades] == list(range(1, 1_002))
    assert [call["from_id"] for call in endpoint.calls[1:]] == [0, 1_001]
    assert all(call["start_time"] is None and call["end_time"] is None for call in endpoint.calls[1:])


def test_exact_order_cursor_can_resume_without_losing_same_timestamp_trades() -> None:
    endpoint: Any = TradeEndpoint([trade(i, time=1_000) for i in range(1, 1_002)])
    first = asyncio.run(
        read_trade_history(
            endpoint,
            symbol="INJUSDT",
            order_id=308654865,
            cursors=(TradeHistoryCursor(1_000, 1_000),),
            max_requests=1,
        )
    )
    assert not first.complete
    second = asyncio.run(
        read_trade_history(endpoint, symbol="INJUSDT", order_id=308654865, cursors=first.remaining, max_requests=1)
    )
    assert second.complete
    assert [row.id for row in (*first.trades, *second.trades)] == list(range(1, 1_002))


def test_a_later_page_failure_is_not_a_complete_partial_answer() -> None:
    endpoint: Any = TradeEndpoint([trade(i) for i in range(1, 1_003)])
    endpoint.fail_at = 2
    with pytest.raises(TimeoutError):
        asyncio.run(
            read_trade_history(endpoint, symbol="INJUSDT", cursors=(TradeHistoryCursor(1_000, 3_000),), max_requests=8)
        )


def test_large_time_ranges_are_partitioned_without_overlapping_boundaries() -> None:
    endpoint: Any = TradeEndpoint([trade(1, time=TRADE_WINDOW_MS - 1), trade(2, time=TRADE_WINDOW_MS)])
    result = asyncio.run(
        read_trade_history(
            endpoint, symbol="INJUSDT", cursors=(TradeHistoryCursor(0, TRADE_WINDOW_MS),), max_requests=2
        )
    )
    assert result.complete and len(result.trades) == 2
    assert [(call["start_time"], call["end_time"]) for call in endpoint.calls] == [
        (0, TRADE_WINDOW_MS - 1),
        (TRADE_WINDOW_MS, TRADE_WINDOW_MS),
    ]


@pytest.mark.parametrize("changes", [{"orderId": 7}, {"qty": "2"}, {"price": "2"}, {"commission": "2"}])
def test_same_native_trade_with_conflicting_economics_or_order_is_refused(changes: dict[str, Any]) -> None:
    endpoint: Any = TradeEndpoint([trade(1), trade(1, **changes)])
    with pytest.raises(ValueError, match="identity_conflict"):
        asyncio.run(
            read_trade_history(endpoint, symbol="INJUSDT", cursors=(TradeHistoryCursor(1_000, 3_000),), max_requests=1)
        )


def test_decimal_formatting_does_not_create_another_native_trade() -> None:
    endpoint: Any = TradeEndpoint([trade(1), trade(1, qty="1.00", price="0.856200", commission="0.100")])
    result = asyncio.run(
        read_trade_history(endpoint, symbol="INJUSDT", cursors=(TradeHistoryCursor(1_000, 3_000),), max_requests=1)
    )
    assert result.complete and len(result.trades) == 1


def test_wrong_native_symbol_is_not_silently_reassigned_to_the_requested_instrument() -> None:
    endpoint: Any = TradeEndpoint([trade(1, symbol="APTUSDT")])
    with pytest.raises(ValueError, match="identity_invalid"):
        asyncio.run(
            read_trade_history(endpoint, symbol="INJUSDT", cursors=(TradeHistoryCursor(1_000, 3_000),), max_requests=1)
        )


def test_exactly_full_last_order_page_needs_an_empty_page_to_confirm_completion() -> None:
    endpoint: Any = TradeEndpoint([trade(i) for i in range(1, TRADE_PAGE_LIMIT + 1)])
    result = asyncio.run(
        read_trade_history(
            endpoint,
            symbol="INJUSDT",
            order_id=308654865,
            cursors=(TradeHistoryCursor(1_000, 3_000),),
            max_requests=2,
        )
    )
    assert result.complete and len(result.trades) == TRADE_PAGE_LIMIT and result.requests_used == 2
