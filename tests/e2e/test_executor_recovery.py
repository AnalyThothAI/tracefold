"""REST reconciliation recovers an ambiguous entry and a stop fill after restart."""

from __future__ import annotations

import asyncio
import time

import psycopg
from psycopg.rows import dict_row

from tests.trading.demo_fixture import ExternalDemo, FakeDemo, MissingDemo
from tracefold.app.executor import ExecutorRunner
from tracefold.platform.config.models import Settings
from tracefold.trading.operator_control import prepare_operator_intent
from tracefold.trading.storage.executor import ExecutorStorage


def test_restart_queries_timeout_then_protects_and_settles(executor_postgres: str) -> None:
    asyncio.run(_exercise_recovery(executor_postgres))


async def _exercise_recovery(executor_postgres: str) -> None:
    now = time.time_ns()
    settings = Settings()
    settings.trading.execution.account_slot = "recovery-demo"
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
    with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.heartbeat(account_slot=settings.trading.execution.account_slot, now_ns=now)
            db.set_control(account_slot=settings.trading.execution.account_slot, paused=False, halted=False, now_ns=now)
            db.record_full_reconciliation(
                account_slot=settings.trading.execution.account_slot, now_ns=now, unexpected=False
            )
            db.append_operator_intent(intent)
        first = ExecutorRunner(settings=settings, conn=conn, venue=venue)
        await first._one_intent(now)
        assert db.plan(command_id)["status"] == "accepted"
        assert db.plan_orders(command_id)[0]["status"] == "unknown"

    with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        recovered = ExecutorRunner(settings=settings, conn=conn, venue=venue)
        for seconds in (8, 13, 18):
            await recovered._reconcile(now + seconds * 1_000_000_000)
        assert len(venue.market_calls) == 1
        assert {value["leg"] for value in venue.algos.values()} == {"sl", "tp"}
        assert all(len(value) == 32 for value in venue.algos)
        assert len(db.plan_orders(command_id)) == 3
        assert sum(row["native_symbol"] == "BTCUSDT" for row in db.fill_ledger(since_ns=0, limit=20)) == 12
        await recovered._full_account_check(now + 18_000_000_000)
        snapshot = db.state(settings.trading.execution.account_slot)["account_snapshot"]
        assert snapshot["positions_total"] == 1 and snapshot["algos_total"] == 2
        assert snapshot["positions"][0]["owned"] is True

        venue.trigger_stop()
        for seconds in (23, 28):
            await recovered._reconcile(now + seconds * 1_000_000_000)
        plan = db.plan(command_id)
        assert plan["status"] == "terminal" and plan["terminal_reason"] == "stop_filled"
        assert len(venue.cancelled) == 1
        assert db.settle_pnl(plan=plan, now_ns=now + 28_000_000_000) == "complete"
        assert sum(row["native_symbol"] == "BTCUSDT" for row in db.fill_ledger(since_ns=0, limit=20)) == 13
        assert (
            db.last_stop_at_ns(settings.trading.execution.account_slot, "BTCUSDT")
            == (now // 1_000_000 + 23_000) * 1_000_000
        )
        second = prepare_operator_intent(
            command_id="8" * 64,
            account_slot=settings.trading.execution.account_slot,
            action="manual_entry",
            scope="market",
            reason="cooldown proof",
            operator_identity="test",
            authentication_identity="test",
            requested_at_ns=now,
            expires_at_ns=now + 120_000_000_000,
            market_key="crypto:perp:BTC:USDT",
            direction="long",
        )
        with conn.transaction():
            db.append_operator_intent(second)
        await recovered._one_intent(now + 29_000_000_000)
        disposition = db.disposition(kind="intent", input_id="8" * 64)
        assert disposition["reason"] == "post_stop_cooldown"
        assert disposition["admission_snapshot"]["facts"]["cooldown_until_ns"] > now
        assert len(venue.market_calls) == 1


def test_unknown_order_absent_after_window_remains_unsettled_without_resend(executor_postgres: str) -> None:
    asyncio.run(_exercise_absent_order(executor_postgres))


async def _exercise_absent_order(executor_postgres: str) -> None:
    now = time.time_ns()
    settings = Settings()
    settings.trading.execution.account_slot = "absent-demo"
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
    with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.heartbeat(account_slot=settings.trading.execution.account_slot, now_ns=now)
            db.set_control(account_slot=settings.trading.execution.account_slot, paused=False, halted=False, now_ns=now)
            db.append_operator_intent(intent)
        runner = ExecutorRunner(settings=settings, conn=conn, venue=venue)
        await runner._one_intent(now)
        assert db.plan_orders(command_id)[0]["status"] == "unknown"
        await runner._reconcile(now + 8_000_000_000)
        plan = db.plan(command_id)
        assert plan["status"] != "terminal"
        assert db.plan_orders(command_id)[0]["status"] == "unknown"
        assert (
            db.state(settings.trading.execution.account_slot)["faults"][command_id + ":entry"]["reason"]
            == "submission_unresolved"
        )
        assert plan["pnl_status"] == "pending" and plan["net_pnl"] is None
        assert len(venue.market_calls) == 1


def test_account_flatten_closes_unclaimed_position_with_durable_order(executor_postgres: str) -> None:
    asyncio.run(_exercise_account_flatten(executor_postgres))


async def _exercise_account_flatten(executor_postgres: str) -> None:
    now = time.time_ns()
    settings = Settings()
    settings.trading.execution.account_slot = "flatten-demo"
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
    with psycopg.connect(executor_postgres, autocommit=True, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.heartbeat(account_slot=settings.trading.execution.account_slot, now_ns=now)
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
