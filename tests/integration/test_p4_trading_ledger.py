"""Real PostgreSQL entry claims, immutable facts and platform liveness fences."""

from __future__ import annotations

import asyncio
import time
from contextlib import closing
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation

from tests.e2e.test_executor_recovery import FakeDemo
from tests.integration.test_p0_executor_pending import SLOT, intent, seed_case, signal
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.executor import ExecutorRunner
from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.platform.config.models import Settings
from tracefold.platform.postgres.runtime_processes import RuntimeProcesses
from tracefold.trading.storage.executor import ExecutorStorage

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def accept(db, key, now):
    return db.accept_entry(
        entry_id=key * 64,
        command_id=None,
        account_slot=SLOT,
        native_symbol="BTCUSDT",
        side="long",
        quantity="1",
        reference_price="100",
        stop_bps=100,
        tp_bps=200,
        max_hold_s=3600,
        now_ns=now,
    )


def test_pending_accept_is_once_and_reserves_order_in_the_winning_transaction():
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.append_signal(signal("a", now))
        with conn.transaction():
            assert accept(db, "a", now)
            db.reserve_order(
                client_id="p4-entry", entry_id="a" * 64, native_symbol="BTCUSDT", leg="entry", attempt=1, now_ns=now
            )
        with conn.transaction():
            assert not accept(db, "a", now + 1)
        assert len(db.entry_orders("a" * 64)) == 1
        assert db.next_signal(account_slot=SLOT) is None


def test_second_accept_for_active_symbol_records_symbol_exposure_without_sending():
    class LaggingDemo(FakeDemo):
        async def positions(self):
            return []

    now = time.time_ns()
    settings = Settings(
        trading={
            "enabled": True,
            "execution": {
                "enabled": True,
                "account_slot": SLOT,
                "binance": {"environment": "DEMO"},
                "risk": {"max_leverage": 5},
            },
        }
    )
    venue = LaggingDemo(now)
    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.set_control(account_slot=SLOT, paused=False, halted=False, now_ns=now)
            db.append_signal(signal("a", now))
            db.append_signal(signal("b", now + 1))
        runner = ExecutorRunner(settings=settings, conn=conn, venue=venue)
        asyncio.run(runner._one_signal(now + 2))
        asyncio.run(runner._one_signal(now + 3))
        assert len(venue.market_calls) == 1
        assert db.entry("a" * 64)["state"] == "accepted"
        refused = db.disposition(kind="signal", input_id="b" * 64)
        assert (refused["disposition"], refused["reason"]) == ("refused", "symbol_exposure")
        assert refused["decided_at_ns"] >= now + 3


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE trading_entries SET request='{}'",
        "UPDATE trading_entries SET quantity=2",
        "UPDATE trading_entries SET reason='rewrite'",
        "DELETE FROM trading_entries",
        "UPDATE trading_operator_intents SET payload='{}'",
        "UPDATE trading_operator_intents SET disposition_reason='rewrite'",
        "DELETE FROM trading_operator_intents",
        "UPDATE trading_fills SET price=200",
        "UPDATE trading_fills SET client_order_id=NULL",
        "DELETE FROM trading_fills",
    ],
)
def test_write_once_rejects_fact_rewrite_and_delete(statement):
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.append_signal(signal("a", now))
            assert accept(db, "a", now)
            db.append_operator_intent(intent("b", "pause_entries", now))
            db.record_disposition(
                kind="intent",
                input_id="b" * 64,
                account_slot=SLOT,
                disposition="accepted",
                reason="accepted",
                now_ns=now,
            )
            db.reserve_order(
                client_id="p4-entry", entry_id="a" * 64, native_symbol="BTCUSDT", leg="entry", attempt=1, now_ns=now
            )
            db.update_order(client_id="p4-entry", status="filled", now_ns=now, venue_order_id="123")
            db.record_fill(
                symbol="BTCUSDT",
                trade={
                    "id": 1,
                    "orderId": 123,
                    "qty": "1",
                    "price": "100",
                    "realizedPnl": "0",
                    "commission": "0.01",
                    "commissionAsset": "USDT",
                    "time": now // 1_000_000,
                },
            )
            assert db.attribute_unbound_fills(symbol="BTCUSDT", now_ns=now) == 1
        with pytest.raises(CheckViolation, match="trading_fact_immutable"), conn.transaction():
            conn.execute(statement)


@pytest.mark.parametrize(
    ("age", "state", "fault"), [(5001, "running", None), (0, "stopped", None), (0, "running", "fixture")]
)
def test_publication_requires_fresh_running_executor(tmp_path, age, state, fault):
    now = int(time.time() * 1000)
    settings = Settings(
        storage=postgres_settings_storage(),
        trading={
            "enabled": True,
            "analysis": {"publish_signals": True},
            "execution": {"enabled": True, "account_slot": SLOT, "binance": {"environment": "DEMO"}},
        },
    )
    with closing(connect_postgres_test()) as conn:
        runtime = RuntimeProcesses(conn, kind="executor", key=SLOT)
        instance = str(uuid4())
        with conn.transaction():
            assert runtime.begin(instance_id=instance, started_at_ms=now - age, now_ms=now - age)
            runtime.transition(instance_id=instance, lifecycle_state=state, now_ms=now - age)
            runtime.heartbeat(instance_id=instance, now_ms=now - age, fault_code=fault)
    # No HTTP request occurs: the actual platform row closes the admission fence first.
    runner = AnalysisRunner(
        settings=settings, market_data=object(), assessor=None, program_sha="a" * 64, raw_root=tmp_path
    )
    try:
        assert (
            asyncio.run(runner._publication_reason({"native_symbol": "BTCUSDT", "asset_id": "crypto:BTC"}))
            == "runtime_unavailable"
        )
    finally:
        runner._db_pool.shutdown(wait=True)


def test_platform_process_takeover_fences_old_instance_and_does_not_change_account():
    with closing(connect_postgres_test()) as conn:
        db = ExecutorStorage(conn)
        runtime = RuntimeProcesses(conn, kind="executor", key=SLOT)
        first, second = str(uuid4()), str(uuid4())
        with conn.transaction():
            db.ensure_account(SLOT)
            db.set_control(account_slot=SLOT, paused=True, halted=True, now_ns=1)
            assert runtime.begin(instance_id=first, started_at_ms=1000, now_ms=1000)
            runtime.transition(instance_id=first, lifecycle_state="running", now_ms=1000)
            assert not runtime.begin(instance_id=second, started_at_ms=6000, now_ms=6000)
            assert runtime.begin(instance_id=second, started_at_ms=6001, now_ms=6001)
        with pytest.raises(RuntimeError, match="runtime_process_identity_lost"), conn.transaction():
            runtime.heartbeat(instance_id=first, now_ms=7000)
        assert db.control(SLOT)["emergency_halted"] is True
