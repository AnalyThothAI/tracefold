"""Recorded-shape DEMO fixture; no network or real account credentials."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx


class FakeDemo:
    def __init__(self, now_ns: int) -> None:
        self.now_ns = now_ns
        self.amount = Decimal(0)
        self.entry_id: str | None = None
        self.market_calls: list[str] = []
        self.algos: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def positions(self) -> list[dict[str, Any]]:
        return [{"symbol": "BTCUSDT", "positionAmt": str(self.amount), "markPrice": "100", "entryPrice": "100"}]

    async def open_orders(self) -> list[dict[str, Any]]:
        return []

    async def open_algo_orders(self) -> list[dict[str, Any]]:
        return [value for value in self.algos.values() if value["algoStatus"] == "NEW"]

    async def account(self) -> dict[str, Any]:
        return {"totalMarginBalance": "1000", "availableBalance": str(1000 - abs(self.amount) * 100 / 5)}

    async def account_config(self) -> dict[str, Any]:
        return {"dualSidePosition": False, "multiAssetsMargin": False, "canTrade": True}

    async def symbol_config(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "marginType": "CROSSED", "leverage": 5, "maxNotionalValue": "100000"}

    async def mark_price(self, _symbol: str) -> Decimal:
        return Decimal("100")

    async def commission_rate(self, _symbol: str) -> Decimal:
        return Decimal("0.0005")

    async def book_ticker(self, _symbol: str) -> dict[str, Any]:
        return {"bidPrice": "100", "askPrice": "100.01"}

    async def live_book_ticker(self, symbol: str) -> dict[str, Any]:
        return await self.book_ticker(symbol)

    async def exchange_info(self) -> dict[str, Any]:
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    async def market_order(
        self, *, symbol: str, side: str, quantity: Decimal, client_id: str, reduce_only: bool
    ) -> dict[str, Any]:
        assert symbol == "BTCUSDT" and side == "BUY" and not reduce_only
        self.market_calls.append(client_id)
        self.entry_id = client_id
        self.amount = quantity
        for trade_id in range(1, 13):
            self.trades.append(
                {
                    "id": trade_id,
                    "orderId": 123,
                    "qty": str(quantity / 12),
                    "price": "100",
                    "realizedPnl": "0",
                    "commission": "0.01",
                    "commissionAsset": "USDT",
                    "time": self.now_ns // 1_000_000,
                }
            )
        raise httpx.TimeoutException("ambiguous", request=httpx.Request("POST", "https://demo-fapi.binance.com"))

    async def query_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        assert symbol == "BTCUSDT"
        if client_id == self.entry_id:
            return {"orderId": 123, "status": "FILLED", "clientOrderId": client_id}
        return None

    async def protection_order(
        self,
        *,
        symbol: str,
        side: str,
        leg: str,
        trigger_price: Decimal,
        client_id: str,
        quantity: Decimal | None = None,
    ) -> dict[str, Any]:
        assert symbol == "BTCUSDT" and side == "SELL" and quantity is None
        value = {
            "symbol": symbol,
            "clientAlgoId": client_id,
            "algoId": len(self.algos) + 1,
            "algoStatus": "NEW",
            "triggerPrice": str(trigger_price),
            "leg": leg,
        }
        self.algos[client_id] = value
        return value

    async def query_algo(self, client_id: str) -> dict[str, Any] | None:
        return self.algos.get(client_id)

    async def cancel_algo(self, client_id: str) -> dict[str, Any]:
        self.cancelled.append(client_id)
        self.algos[client_id] = {**self.algos[client_id], "algoStatus": "CANCELED"}
        return self.algos[client_id]

    async def user_trades(
        self,
        _symbol: str,
        *,
        from_id: int | None = None,
        start_time_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        return [trade for trade in self.trades if from_id is None or trade["id"] >= from_id]

    def trigger_stop(self) -> None:
        stop = next(value for value in self.algos.values() if value["leg"] == "sl")
        self.algos[stop["clientAlgoId"]] = {**stop, "algoStatus": "TRIGGERED", "actualOrderId": 456}
        self.trades.append(
            {
                "id": 13,
                "orderId": 456,
                "qty": str(self.amount),
                "price": "99",
                "realizedPnl": str(-self.amount),
                "commission": "0.01",
                "commissionAsset": "USDT",
                "time": self.now_ns // 1_000_000 + 23_000,
            }
        )
        self.amount = Decimal(0)


class MissingDemo(FakeDemo):
    async def market_order(
        self, *, symbol: str, side: str, quantity: Decimal, client_id: str, reduce_only: bool
    ) -> dict[str, Any]:
        self.market_calls.append(client_id)
        raise httpx.TimeoutException("absent", request=httpx.Request("POST", "https://demo-fapi.binance.com"))

    async def query_order(self, symbol: str, client_id: str) -> None:
        return None


class ExternalDemo(FakeDemo):
    def __init__(self, now_ns: int) -> None:
        super().__init__(now_ns)
        self.amount = Decimal("1")

    async def market_order(
        self, *, symbol: str, side: str, quantity: Decimal, client_id: str, reduce_only: bool
    ) -> dict[str, Any]:
        assert symbol == "BTCUSDT" and side == "SELL" and reduce_only and quantity == 1
        self.market_calls.append(client_id)
        self.amount = Decimal(0)
        self.trades.append(
            {
                "id": 1000,
                "orderId": 900,
                "qty": "1",
                "price": "100",
                "realizedPnl": "0",
                "commission": "0.01",
                "commissionAsset": "USDT",
                "time": self.now_ns // 1_000_000,
            }
        )
        return {"orderId": 900, "status": "FILLED", "clientOrderId": client_id}

    async def cancel_symbol_orders(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol}

    async def cancel_symbol_algo_orders(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol}
