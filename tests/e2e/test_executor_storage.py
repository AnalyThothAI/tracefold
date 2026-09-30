"""The DEMO ledger must survive a new connection and settle only venue fills."""

from __future__ import annotations

import time
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from tracefold.trading.operator_control import prepare_operator_intent
from tracefold.trading.storage.executor import ExecutorStorage


def test_executor_ledger_roundtrip(executor_postgres: str) -> None:
    now = time.time_ns()
    command_id = "a" * 64
    entry_id = "tf" + "a" * 30
    exit_id = "tf" + "b" * 30
    prepared = prepare_operator_intent(
        command_id=command_id,
        account_slot="demo-test",
        action="manual_entry",
        scope="market",
        reason="test",
        operator_identity="test",
        authentication_identity="test",
        requested_at_ns=now,
        expires_at_ns=now + 60_000_000_000,
        market_key="crypto:perp:ETH:USDT",
        direction="long",
    )
    with psycopg.connect(executor_postgres, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        first = db.append_operator_intent(prepared)
        assert db.append_operator_intent(prepared) == first
        assert first[1]["action"] == "manual_entry"
        db.heartbeat(account_slot="demo-test", now_ns=now)
        db.record_full_reconciliation(account_slot="demo-test", now_ns=now, unexpected=True)
        assert db.state("demo-test")["unexpected_exposure"] is True
        db.create_plan(
            plan_id=command_id,
            signal_id=None,
            command_id=command_id,
            account_slot="demo-test",
            native_symbol="ETHUSDT",
            side="long",
            quantity="0.01",
            reference_price="100000",
            stop_bps=100,
            tp_bps=200,
            max_hold_s=3600,
            now_ns=now,
        )
        db.reserve_order(
            client_id=entry_id, plan_id=command_id, native_symbol="ETHUSDT", leg="entry", attempt=1, now_ns=now
        )
        db.set_plan_status(plan_id=command_id, status="open", now_ns=now, opened_at_ns=now)

    # A fresh process reads the same reserved identity and native fill facts.
    with psycopg.connect(executor_postgres, row_factory=dict_row) as conn:
        db = ExecutorStorage(conn)
        assert db.plan(command_id)["status"] == "open"
        assert db.active_client_ids("demo-test") == {entry_id}
        entry_trade = {
            "id": 1,
            "orderId": 123,
            "qty": "0.01",
            "price": "100000",
            "realizedPnl": "0",
            "commission": "0.1",
            "commissionAsset": "USDT",
            "time": now // 1_000_000,
        }
        assert db.record_fill(symbol="ETHUSDT", trade=entry_trade)
        assert not db.record_fill(symbol="ETHUSDT", trade=entry_trade)
        assert db.attribute_unbound_fills(symbol="ETHUSDT", now_ns=now) == 0
        db.update_order(
            client_id=entry_id, status="filled", now_ns=now, venue_order_id="123", evidence={"status": "FILLED"}
        )
        db.reserve_order(
            client_id=exit_id, plan_id=command_id, native_symbol="ETHUSDT", leg="time_exit", attempt=1, now_ns=now
        )
        db.update_order(
            client_id=exit_id, status="filled", now_ns=now, venue_order_id="124", evidence={"status": "FILLED"}
        )
        db.record_fill(
            symbol="ETHUSDT",
            trade={
                "id": 2,
                "orderId": 124,
                "qty": "0.01",
                "price": "101000",
                "realizedPnl": "10",
                "commission": "0.1",
                "commissionAsset": "USDT",
                "time": now // 1_000_000 + 1,
            },
        )
        assert db.attribute_unbound_fills(symbol="ETHUSDT", now_ns=now) == 2
        assert db.attribute_unbound_fills(symbol="ETHUSDT", now_ns=now) == 0
        db.set_plan_status(plan_id=command_id, status="terminal", now_ns=now, terminal_reason="time_exit")
        assert db.settle_pnl(plan=db.plan(command_id), now_ns=now) == "complete"
        assert db.plan(command_id)["net_pnl"] == Decimal("9.8")
        rows = [row for row in db.console_executions(since_ns=0, limit=10) if row["entry_id"] == command_id]
        assert len(rows) == 1
        assert rows[0]["entry_id"] == command_id
        assert rows[0]["fill_quantity"] == "0.01"
        assert rows[0]["pnl_status"] == "complete"
        totals = db.console_realized_totals(account_slot="demo-test", day_start_ns=0, day_end_ns=now + 1_000_000_000)
        assert totals["closed_total"] == 1
        assert totals["net_known_total_usd"] == "9.8"
