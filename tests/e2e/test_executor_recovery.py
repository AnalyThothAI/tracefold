"""REST reconciliation recovers an ambiguous entry and a stop fill after restart."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from tracefold.app.executor import ExecutorRunner
from tracefold.platform.config.models import Settings
from tracefold.trading.operator_control import prepare_operator_intent
from tracefold.trading.storage.executor import ExecutorStorage


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
        return {"totalMarginBalance": "1000"}

    async def position_mode(self) -> dict[str, Any]:
        return {"dualSidePosition": False}

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

    async def user_trades(self, _symbol: str, *, from_id: int | None = None) -> list[dict[str, Any]]:
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


def test_restart_queries_timeout_then_protects_and_settles(e2e_postgres: str) -> None:
    asyncio.run(_exercise_recovery(e2e_postgres))


async def _exercise_recovery(e2e_postgres: str) -> None:
    now = time.time_ns()
    settings = Settings()
    settings.trading.enabled = True
    settings.trading.execution.enabled = True
    settings.trading.execution.binance.environment = "DEMO"
    settings.trading.execution.risk.max_leverage = 5
    venue = FakeDemo(now)
    command_id = "d" * 64
    intent = prepare_operator_intent(
        command_id=command_id,
        account_slot=settings.trading.execution.account_slot,
        action="manual_entry",
        scope="market",
        reason="recovery test",
        operator_identity="test",
        authentication_identity="test",
        requested_at_ns=now,
        expires_at_ns=now + 120_000_000_000,
        market_key="crypto:perp:BTC:USDT",
        direction="long",
    )
    with psycopg.connect(e2e_postgres, autocommit=True, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.ensure_account(settings.trading.execution.account_slot)
            db.set_control(account_slot=settings.trading.execution.account_slot, paused=False, halted=False, now_ns=now)
            db.record_full_reconciliation(
                account_slot=settings.trading.execution.account_slot, now_ns=now, unexpected=False
            )
            db.append_operator_intent(intent)
        first = ExecutorRunner(settings=settings, conn=conn, venue=venue)
        await first._one_intent(now)
        assert db.entry(command_id)["state"] == "accepted"
        assert db.entry_orders(command_id)[0]["status"] == "unknown"

    with psycopg.connect(e2e_postgres, autocommit=True, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        recovered = ExecutorRunner(settings=settings, conn=conn, venue=venue)
        for seconds in (8, 13, 18):
            await recovered._reconcile(now + seconds * 1_000_000_000)
        assert len(venue.market_calls) == 1
        assert {value["leg"] for value in venue.algos.values()} == {"sl", "tp"}
        assert all(len(value) == 32 for value in venue.algos)
        assert len(db.entry_orders(command_id)) == 3
        assert sum(row["native_symbol"] == "BTCUSDT" for row in db.fill_ledger(since_ns=0, limit=20)) == 12
        await recovered._full_account_check(now + 18_000_000_000)
        snapshot = db.account(settings.trading.execution.account_slot)["account_snapshot"]
        assert snapshot["positions_total"] == 1 and snapshot["algos_total"] == 2
        assert snapshot["positions"][0]["owned"] is True

        venue.trigger_stop()
        for seconds in (23, 28):
            await recovered._reconcile(now + seconds * 1_000_000_000)
        plan = db.entry(command_id)
        assert plan["state"] == "terminal" and plan["terminal_reason"] == "stop_filled"
        assert len(venue.cancelled) == 1
        assert db.settle_pnl(plan=plan, now_ns=now + 28_000_000_000) == "complete"
        assert sum(row["native_symbol"] == "BTCUSDT" for row in db.fill_ledger(since_ns=0, limit=20)) == 13


def test_unknown_order_absent_after_window_terminates_without_resend(e2e_postgres: str) -> None:
    asyncio.run(_exercise_absent_order(e2e_postgres))


async def _exercise_absent_order(e2e_postgres: str) -> None:
    now = time.time_ns()
    settings = Settings()
    settings.trading.enabled = True
    settings.trading.execution.enabled = True
    settings.trading.execution.binance.environment = "DEMO"
    venue = MissingDemo(now)
    command_id = "b" * 64
    intent = prepare_operator_intent(
        command_id=command_id,
        account_slot=settings.trading.execution.account_slot,
        action="manual_entry",
        scope="market",
        reason="unknown order test",
        operator_identity="test",
        authentication_identity="test",
        requested_at_ns=now,
        expires_at_ns=now + 120_000_000_000,
        market_key="crypto:perp:BTC:USDT",
        direction="long",
    )
    with psycopg.connect(e2e_postgres, autocommit=True, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.ensure_account(settings.trading.execution.account_slot)
            db.set_control(account_slot=settings.trading.execution.account_slot, paused=False, halted=False, now_ns=now)
            db.append_operator_intent(intent)
        runner = ExecutorRunner(settings=settings, conn=conn, venue=venue)
        await runner._one_intent(now)
        assert db.entry_orders(command_id)[0]["status"] == "unknown"
        await runner._reconcile(now + 8_000_000_000)
        plan = db.entry(command_id)
        assert plan["state"] == "terminal" and plan["terminal_reason"] == "not_submitted"
        assert db.entry_orders(command_id)[0]["status"] == "not_submitted"
        assert len(venue.market_calls) == 1


def test_account_flatten_closes_unclaimed_position_with_durable_order(e2e_postgres: str) -> None:
    asyncio.run(_exercise_account_flatten(e2e_postgres))


async def _exercise_account_flatten(e2e_postgres: str) -> None:
    now = time.time_ns()
    settings = Settings()
    settings.trading.enabled = True
    settings.trading.execution.enabled = True
    settings.trading.execution.binance.environment = "DEMO"
    venue = ExternalDemo(now)
    command_id = "c" * 64
    intent = prepare_operator_intent(
        command_id=command_id,
        account_slot=settings.trading.execution.account_slot,
        action="flatten",
        scope="account",
        reason="external flatten test",
        operator_identity="test",
        authentication_identity="test",
        requested_at_ns=now,
        expires_at_ns=now + 120_000_000_000,
        market_key=None,
        direction=None,
    )
    with psycopg.connect(e2e_postgres, autocommit=True, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.ensure_account(settings.trading.execution.account_slot)
            db.append_operator_intent(intent)
        runner = ExecutorRunner(settings=settings, conn=conn, venue=venue)
        await runner._one_intent(now)
        assert db.control(settings.trading.execution.account_slot)["flatten_command_id"] == command_id
        await runner._reconcile(now + 1_000_000_000)
        assert len(venue.market_calls) == 1
        assert db.external_flatten_orders(command_id, "BTCUSDT")[0]["status"] == "filled"
        assert any(fill["client_order_id"] == venue.market_calls[0] for fill in db.fill_ledger(since_ns=0, limit=20))
        await runner._reconcile(now + 6_000_000_000)
        assert db.control(settings.trading.execution.account_slot)["flatten_command_id"] is None
