"""Account evidence, partial exposure and failure isolation on a real isolated ledger."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from tests.trading.demo_fixture import ExternalDemo, FakeDemo, MissingDemo
from tracefold.app.executor import ExecutorRunner
from tracefold.integrations.trading.binance import BinanceFailure
from tracefold.platform.config.models import Settings
from tracefold.trading.operator_control import prepare_operator_intent
from tracefold.trading.storage.executor import ExecutorStorage


def _runner(conn: Any, venue: Any, slot: str, now: int) -> ExecutorRunner:
    settings = Settings()
    settings.trading.execution.enabled = True
    settings.trading.execution.binance.environment = "DEMO"
    settings.trading.execution.account_slot = slot
    runner = ExecutorRunner(settings=settings, conn=conn, venue=venue)
    with conn.transaction():
        runner.db.heartbeat(account_slot=slot, now_ns=now)
        runner.db.set_control(account_slot=slot, paused=False, halted=False, now_ns=now)
    return runner


def _intent(db: ExecutorStorage, slot: str, key: str, symbol: str, now: int) -> int:
    prepared = prepare_operator_intent(
        command_id=key,
        account_slot=slot,
        action="manual_entry",
        scope="market",
        reason="fixture",
        operator_identity="fixture",
        authentication_identity="fixture",
        requested_at_ns=now,
        expires_at_ns=now + 120_000_000_000,
        market_key=f"crypto:perp:{symbol.removesuffix('USDT')}:USDT",
        direction="long",
    )
    return db.append_operator_intent(prepared)[0]


class RefusedDemo(FakeDemo):
    def __init__(self, now: int, code: int) -> None:
        super().__init__(now)
        self.code = code

    async def market_order(self, **kwargs: Any) -> dict[str, Any]:
        self.market_calls.append(kwargs["client_id"])
        raise BinanceFailure(400, self.code, "fixture")


@pytest.mark.parametrize("code", [-2019, -1007])
def test_rejection_is_zero_but_ambiguous_failure_remains_unsettled(executor_postgres: str, code: int) -> None:
    async def run() -> None:
        now = time.time_ns()
        key = ("e" if code == -2019 else "f") * 64
        slot = f"rejection-{abs(code)}"
        venue = RefusedDemo(now, code)
        with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
            runner = _runner(conn, venue, slot, now)
            with conn.transaction():
                _intent(runner.db, slot, key, "BTCUSDT", now)
            await runner._one_intent(now)
            plan = runner.db.plan(key)
            order = runner.db.plan_orders(key)[0]
            assert plan["admitted_at_ns"] and plan["reserved_margin"] > 0
            assert plan["admission_snapshot"]["facts"]["available_margin_usdt"] == "1000"
            assert order["submitted_at_ns"] is not None and order["evidence"]["error_code"] == code
            assert len(venue.market_calls) == 1
            if code == -2019:
                assert plan["terminal_reason"] == "entry_rejected"
                with conn.transaction():
                    assert runner.db.settle_pnl(plan=plan, now_ns=now) == "complete"
                assert runner.db.plan(key)["net_pnl"] == 0
                assert runner.db.last_stop_at_ns(slot, "BTCUSDT") is None
            else:
                assert plan["terminal_at_ns"] is None
                assert plan["net_pnl"] is None and order["status"] == "unknown"

    asyncio.run(run())


@pytest.mark.parametrize("started", [False, True])
def test_account_flatten_recovery_uses_dispatch_evidence(executor_postgres: str, started: bool) -> None:
    async def run() -> None:
        now = time.time_ns()
        slot, command, client = "flatten-checkpoint", "8" * 64, "tf" + "9" * 30
        venue = ExternalDemo(now)
        with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
            runner = _runner(conn, venue, slot, now)
            with conn.transaction():
                runner.db.append_operator_intent(
                    prepare_operator_intent(
                        command_id=command,
                        account_slot=slot,
                        action="flatten",
                        scope="account",
                        reason="fixture",
                        operator_identity="fixture",
                        authentication_identity="fixture",
                        requested_at_ns=now,
                        expires_at_ns=now + 120_000_000_000,
                    )
                )
                runner.db.request_flatten(account_slot=slot, command_id=command, now_ns=now)
                runner.db.reserve_external_flatten(
                    client_id=client,
                    command_id=command,
                    symbol="BTCUSDT",
                    attempt=1,
                    now_ns=now,
                )
                if started:
                    runner.db.update_order(
                        client_id=client,
                        status="unknown",
                        now_ns=now,
                        evidence={"submission": "started"},
                    )
            await runner._reconcile(now + 8_000_000_000)
            order = runner.db.external_flatten_orders(command, "BTCUSDT")[0]
            if started:
                assert not venue.market_calls
                assert order["status"] == "unknown"
                assert (
                    runner.db.state(slot)["faults"][command + ":BTCUSDT"]["reason"] == "flatten_submission_unresolved"
                )
            else:
                assert venue.market_calls == [client]
                assert order["evidence"]["request"] == {
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "quantity": "1",
                    "reduce_only": True,
                }
                assert order["evidence"]["submission"] == "started"
                assert order["updated_at_ns"] >= order["submitted_at_ns"]
                assert order["resolved_at_ns"] >= order["submitted_at_ns"]
                await runner._reconcile(now + 9_000_000_000)
                assert venue.market_calls == [client]
                assert runner.db.control(slot)["flatten_command_id"] is None

    asyncio.run(run())


class PartialDemo(FakeDemo):
    def __init__(self, now: int) -> None:
        super().__init__(now)
        self.remote_status = "PARTIALLY_FILLED"
        self.entry_cancellations: list[str] = []

    async def market_order(self, **kwargs: Any) -> dict[str, Any]:
        self.market_calls.append(kwargs["client_id"])
        self.entry_id = kwargs["client_id"]
        self.amount = kwargs["quantity"] / 2
        self.trades = [
            {
                "id": 77,
                "orderId": 789,
                "qty": str(self.amount),
                "price": "100",
                "realizedPnl": "0",
                "commission": "0.01",
                "commissionAsset": "USDT",
                "time": self.now_ns // 1_000_000,
            }
        ]
        return {"orderId": 789, "status": self.remote_status, "executedQty": str(self.amount)}

    async def query_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        if client_id != self.entry_id:
            return None
        return {"orderId": 789, "status": self.remote_status, "executedQty": str(self.amount)}

    async def cancel_order(self, symbol: str, client_id: str) -> dict[str, Any]:
        assert self.algos  # A closePosition stop exists before cancelling a partial entry.
        self.entry_cancellations.append(client_id)
        self.remote_status = "CANCELED"
        return {"orderId": 789, "status": self.remote_status, "executedQty": str(self.amount)}


def test_partial_fill_is_protected_then_cancelled_without_resending(executor_postgres: str) -> None:
    async def run() -> None:
        now = time.time_ns()
        venue = PartialDemo(now)
        with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
            runner = _runner(conn, venue, "partial-demo", now)
            with conn.transaction():
                _intent(runner.db, "partial-demo", "3" * 64, "BTCUSDT", now)
            await runner._one_intent(now)
            await runner._reconcile(now + 1_000_000_000)
            assert {row["leg"] for row in venue.algos.values()} == {"sl"}
            assert not venue.entry_cancellations
            await runner._reconcile(now + 6_000_000_000)
            assert venue.entry_cancellations == [venue.entry_id]
            await runner._reconcile(now + 11_000_000_000)
            assert {row["leg"] for row in venue.algos.values()} == {"sl", "tp"}
            assert len(venue.market_calls) == 1

    asyncio.run(run())


def _seed_open(runner: ExecutorRunner, key: str, symbol: str, order_id: int, now: int) -> dict[str, Any]:
    db, slot = runner.db, runner.account_slot
    seq = _intent(db, slot, key, symbol, now)
    db.create_plan(
        plan_id=key,
        signal_id=None,
        command_id=key,
        account_slot=slot,
        native_symbol=symbol,
        side="long",
        quantity="1",
        reference_price="100",
        stop_bps=100,
        tp_bps=200,
        max_hold_s=3600,
        now_ns=now,
    )
    client = "tf" + key[:30]
    db.reserve_order(client_id=client, plan_id=key, native_symbol=symbol, leg="entry", attempt=1, now_ns=now)
    db.update_order(
        client_id=client,
        status="filled",
        venue_order_id=str(order_id),
        now_ns=now,
        evidence={"status": "FILLED", "executedQty": "1"},
    )
    db.set_plan_status(plan_id=key, status="open", opened_at_ns=now, now_ns=now)
    db.record_disposition(
        kind="intent",
        input_id=key,
        account_slot=slot,
        disposition="accepted",
        reason="accepted",
        plan_id=key,
        now_ns=now,
    )
    db.advance_cursor(account_slot=slot, kind="intent", seq=seq)
    return db.plan(key)


class TwoSymbolsDemo(FakeDemo):
    def __init__(self, now: int) -> None:
        super().__init__(now)
        self.operations: list[str] = []

    async def positions(self) -> list[dict[str, Any]]:
        return [
            {"symbol": symbol, "positionAmt": "1", "markPrice": "100", "entryPrice": "100"}
            for symbol in ("BTCUSDT", "ETHUSDT")
        ]

    async def exchange_info(self) -> dict[str, Any]:
        catalog = await super().exchange_info()
        catalog["symbols"].append({**catalog["symbols"][0], "symbol": "ETHUSDT"})
        return catalog

    async def query_order(self, symbol: str, client_id: str) -> dict[str, Any]:
        return {"orderId": 10001 if symbol == "BTCUSDT" else 10002, "status": "FILLED", "executedQty": "1"}

    async def protection_order(self, **kwargs: Any) -> dict[str, Any]:
        self.operations.append("protect:" + kwargs["symbol"])
        assert kwargs["quantity"] is None
        value = {
            "symbol": kwargs["symbol"],
            "clientAlgoId": kwargs["client_id"],
            "algoStatus": "NEW",
            "leg": kwargs["leg"],
        }
        self.algos[kwargs["client_id"]] = value
        return value

    async def user_trades(self, symbol: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.operations.append("fills:" + symbol)
        if symbol == "BTCUSDT":
            raise httpx.ReadTimeout("fixture")
        return []


def test_fill_failure_does_not_prevent_any_position_protection(executor_postgres: str) -> None:
    async def run() -> None:
        now = time.time_ns()
        venue = TwoSymbolsDemo(now)
        with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
            runner = _runner(conn, venue, "isolation-demo", now)
            with conn.transaction():
                _seed_open(runner, "4" * 64, "BTCUSDT", 10001, now)
                _seed_open(runner, "5" * 64, "ETHUSDT", 10002, now)
            await runner._reconcile(now + 1_000_000_000)
            assert venue.operations[:2] == ["protect:BTCUSDT", "protect:ETHUSDT"]
            assert len(venue.algos) == 2
            assert venue.operations[2:] == ["fills:BTCUSDT", "fills:ETHUSDT"]

    asyncio.run(run())


def test_exhausted_exit_fault_survives_heartbeat_and_clears_only_when_flat(executor_postgres: str) -> None:
    async def run() -> None:
        now = time.time_ns()
        venue = FakeDemo(now)
        venue.amount = Decimal("1")
        slot, key = "exhausted-demo", "6" * 64
        with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
            runner = _runner(conn, venue, slot, now)
            with conn.transaction():
                plan = _seed_open(runner, key, "BTCUSDT", 10003, now)
                for attempt in range(1, 4):
                    client = f"exhausted-{attempt}"
                    runner.db.reserve_order(
                        client_id=client,
                        plan_id=key,
                        native_symbol="BTCUSDT",
                        leg="safety_flatten",
                        attempt=attempt,
                        now_ns=now,
                    )
                    runner.db.update_order(client_id=client, status="rejected", error_code=-2019, now_ns=now)
            await runner._flatten(plan, Decimal("1"), reason="protection_failed", now=now)
            assert runner.db.state(slot)["faults"][key]["reason"] == "flatten_exhausted"
            with conn.transaction():
                runner.db.heartbeat(account_slot=slot, now_ns=now, error="fixture_failure")
                runner.db.heartbeat(account_slot=slot, now_ns=now + 1)
                _intent(runner.db, slot, "7" * 64, "BTCUSDT", now)
            assert runner.db.state(slot)["last_error"] == "fixture_failure"
            await runner._one_intent(now)
            assert runner.db.disposition(kind="intent", input_id="7" * 64)["reason"] == "entries_paused"
            assert runner.db.state(slot)["faults"]
            venue.amount = Decimal(0)
            await runner._reconcile(now + 1_000_000_000)
            assert runner.db.plan(key)["terminal_at_ns"] is not None
            assert not runner.db.state(slot)["faults"]
            assert not venue.market_calls

    asyncio.run(run())


class LateEntryDemo(FakeDemo):
    def __init__(self, now: int) -> None:
        super().__init__(now)
        self.resolved = False
        self.requested = Decimal(0)

    async def market_order(self, **kwargs: Any) -> dict[str, Any]:
        self.market_calls.append(kwargs["client_id"])
        self.entry_id = kwargs["client_id"]
        self.requested = kwargs["quantity"]
        raise httpx.TimeoutException("fixture")

    async def query_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        if not self.resolved:
            return None
        return await super().query_order(symbol, client_id)


def test_late_entry_is_protected_after_unknown_deadline_without_resubmission(executor_postgres: str) -> None:
    async def run() -> None:
        now = time.time_ns()
        key, slot = "4" * 64, "late-entry-rootfix"
        venue = LateEntryDemo(now)
        with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
            runner = _runner(conn, venue, slot, now)
            with conn.transaction():
                _intent(runner.db, slot, key, "BTCUSDT", now)
            await runner._one_intent(now)
            await runner._reconcile(now + 8000000000)
            assert runner.db.plan(key)["terminal_at_ns"] is None
            assert runner.db.state(slot)["faults"][key + ":entry"]["reason"] == "submission_unresolved"
            venue.amount = venue.requested
            venue.resolved = True
            await runner._reconcile(now + 10000000000)
            assert key + ":entry" not in runner.db.state(slot)["faults"]
            assert runner.db.plan(key)["opened_at_ns"] is not None
            assert any(order["leg"] == "sl" and order["status"] == "working" for order in runner.db.plan_orders(key))
            assert len(venue.market_calls) == 1

    asyncio.run(run())


def test_unknown_entry_prevents_a_second_same_symbol_entry_before_fault_window(executor_postgres: str) -> None:
    async def run() -> None:
        now = time.time_ns()
        venue = MissingDemo(now)
        with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
            runner = _runner(conn, venue, "pending-symbol", now)
            with conn.transaction():
                _intent(runner.db, runner.account_slot, "a" * 64, "BTCUSDT", now)
                _intent(runner.db, runner.account_slot, "b" * 64, "BTCUSDT", now)
            await runner._one_intent(now)
            assert runner.db.plan_orders("a" * 64)[0]["status"] == "unknown"
            assert not runner.db.state(runner.account_slot)["faults"]
            await runner._one_intent(now + 1_000_000_000)
            refusal = runner.db.disposition(kind="intent", input_id="b" * 64)
            assert refusal["reason"] == "symbol_exposure"
            assert refusal["admission_snapshot"]["facts"]["symbol_active_plans"] == 1
            assert len(venue.market_calls) == 1

    asyncio.run(run())


def test_slow_order_query_does_not_delay_other_symbol_protection(executor_postgres: str) -> None:
    async def run() -> None:
        now = time.time_ns()
        protected = asyncio.Event()

        class SlowQueryDemo(TwoSymbolsDemo):
            async def query_order(self, symbol: str, client_id: str) -> dict[str, Any]:
                if symbol == "BTCUSDT":
                    await asyncio.wait_for(protected.wait(), timeout=1)
                return await super().query_order(symbol, client_id)

            async def protection_order(self, **kwargs: Any) -> dict[str, Any]:
                result = await super().protection_order(**kwargs)
                if kwargs["symbol"] == "ETHUSDT":
                    protected.set()
                return result

        venue = SlowQueryDemo(now)
        with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
            runner = _runner(conn, venue, "slow-order-isolation", now)
            with conn.transaction():
                _seed_open(runner, "c" * 64, "BTCUSDT", 10001, now)
                _seed_open(runner, "d" * 64, "ETHUSDT", 10002, now)
            await asyncio.wait_for(runner._reconcile(now + 1_000_000_000), timeout=2)
            assert venue.operations[:2] == ["protect:ETHUSDT", "protect:BTCUSDT"]
            assert venue.operations[2:] == ["fills:BTCUSDT", "fills:ETHUSDT"]

    asyncio.run(run())
