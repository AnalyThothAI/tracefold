"""#760 uses the current ledger, real constraints and production orchestration."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from psycopg.errors import CheckViolation

from tests.e2e.test_executor_recovery import FakeDemo
from tests.integration.test_p0_executor_pending import SLOT, seed_case, signal
from tests.integration.test_p4_trading_ledger import accept
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.executor import ExecutorRunner
from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.integrations.trading.binance import BinanceFailure
from tracefold.platform.config.models import Settings
from tracefold.trading.operator_control import control_entry_block
from tracefold.trading.storage.executor import ExecutorStorage

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def settings() -> Settings:
    return Settings(
        trading={
            "enabled": True,
            "execution": {
                "enabled": True,
                "account_slot": SLOT,
                "binance": {"environment": "DEMO"},
            },
        }
    )


def prepare(conn, now: int, key: str = "a", symbol_name: str = "BTCUSDT") -> ExecutorStorage:
    seed_case(conn)
    db = ExecutorStorage(conn)
    with conn.transaction():
        db.set_control(account_slot=SLOT, paused=False, halted=False, now_ns=now)
        request = signal(key, now).model_copy(update={"native_symbol": symbol_name})
        db.append_signal(request)
        db.accept_entry(
            entry_id=request.signal_id,
            command_id=None,
            account_slot=SLOT,
            native_symbol=symbol_name,
            side="long",
            quantity="1",
            reference_price="100",
            stop_bps=100,
            tp_bps=200,
            max_hold_s=3600,
            now_ns=now,
        )
        db.reserve_order(
            client_id="entry-" + key,
            entry_id=request.signal_id,
            native_symbol=symbol_name,
            leg="entry",
            attempt=1,
            now_ns=now,
        )
    return db


@pytest.mark.parametrize("status", ["unknown", "working", "filled"])
def test_runner_protects_attributable_partial_without_resetting_clock(status: str) -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        proof = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "clientOrderId": "entry-a",
            "orderId": 123,
            "status": "PARTIALLY_FILLED",
            "executedQty": "0.4",
        }
        with conn.transaction():
            db.update_order(client_id="entry-a", status=status, now_ns=now, evidence=proof)
        venue = FakeDemo(now)
        venue.amount = Decimal("0.4")
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)

        async def run():
            await runner._step_plan(
                plan=db.entry("a" * 64),
                position=(await venue.positions())[0],
                open_algos={},
                force_flatten=False,
                now=now,
            )
            assert len(venue.algos) == 1 and next(iter(venue.algos.values()))["leg"] == "sl"
            first_clock = db.entry("a" * 64)["opened_at_ns"]
            assert first_clock == now
            await runner._step_plan(
                plan=db.entry("a" * 64),
                position=(await venue.positions())[0],
                open_algos=venue.algos,
                force_flatten=False,
                now=now + 1,
            )
            assert db.entry("a" * 64)["opened_at_ns"] == first_clock

        asyncio.run(run())


@pytest.mark.parametrize("amount,side,proof_qty", [("0.4", "SELL", "0.4"), ("1.1", "BUY", "1"), ("0.4", "BUY", "0")])
def test_same_symbol_is_not_ownership(amount: str, side: str, proof_qty: str) -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        with conn.transaction():
            db.update_order(
                client_id="entry-a",
                status="working",
                now_ns=now,
                evidence={
                    "clientOrderId": "entry-a",
                    "symbol": "BTCUSDT",
                    "side": side,
                    "executedQty": proof_qty,
                },
            )
        venue = FakeDemo(now)
        venue.amount = Decimal(amount)
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)
        asyncio.run(
            runner._step_plan(
                plan=db.entry("a" * 64), position={"positionAmt": amount}, open_algos={}, force_flatten=False, now=now
            )
        )
        assert not venue.algos and not venue.market_calls
        assert db.account(SLOT)["execution_faults"]["a" * 64]["code"] == "unattributed_position"


@pytest.mark.parametrize("failure", ["reject", "timeout", "bad_reply"])
def test_zero_rejection_and_unknown_send_have_distinct_pnl_and_statistics(failure: str) -> None:
    class Venue(FakeDemo):
        async def market_order(self, **kwargs):
            if failure == "reject":
                raise BinanceFailure(
                    400, -2019, "insufficient margin", evidence={"code": -2019, "msg": "insufficient margin"}
                )
            if failure == "timeout":
                raise httpx.ReadTimeout("ambiguous")
            return {"status": "FILLED", "orderId": 123}

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=Venue(now))
        asyncio.run(
            runner._send_market(
                entry_id="a" * 64,
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal(1),
                client_id="entry-a",
                reduce_only=False,
                now=1,
            )
        )
        order = db.entry_orders("a" * 64)[0]
        assert order["submitted_at_ns"] >= now and order["request"]["quantity"] == "1"
        if failure == "reject":
            assert order["evidence"] == {"code": -2019, "msg": "insufficient margin"}
            assert order["resolution"]["definitely_not_executed"]
            with conn.transaction():
                assert db.settle_pnl(plan=db.entry("a" * 64), now_ns=now) == "complete"
            assert db.entry("a" * 64)["net_pnl"] == 0
            totals = db.console_realized_totals(account_slot=SLOT, day_start_ns=0, day_end_ns=now + 10**9)
            assert totals["closed_total"] == 0
        else:
            assert order["status"] == "unknown" and order["resolution"] is None
            assert db.entry("a" * 64)["terminal_at_ns"] is None


def test_exit_exhaustion_survives_restart_resume_and_heartbeat() -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        with conn.transaction():
            for attempt in range(1, 4):
                db.reserve_order(
                    client_id=f"exit-{attempt}",
                    entry_id="a" * 64,
                    native_symbol="BTCUSDT",
                    leg="safety_flatten",
                    attempt=attempt,
                    now_ns=now,
                )
                db.update_order(client_id=f"exit-{attempt}", status="rejected", now_ns=now, error_code=-2022)
        venue = FakeDemo(now)
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)
        asyncio.run(runner._flatten(db.entry("a" * 64), Decimal(1), reason="protection_failed", now=now))
        with conn.transaction():
            db.apply_control(account_slot=SLOT, action="resume_entries", command_id="b" * 64, now_ns=now + 1)
            runner.heartbeat(now_ns=now + 1)
        recovered = ExecutorRunner(settings=settings(), conn=conn, venue=venue)
        asyncio.run(recovered._flatten(db.entry("a" * 64), Decimal(1), reason="protection_failed", now=now + 2))
        state = db.account(SLOT)
        assert control_entry_block(state) == "execution_fault"
        assert len(state["execution_faults"]) == 1
        assert state["execution_faults"]["a" * 64]["first_observed_at_ns"] == now
        assert not venue.market_calls
        with conn.transaction():
            db.set_control(account_slot=SLOT, paused=True, halted=False, now_ns=now + 3)
            db.clear_fault(account_slot=SLOT, responsibility="a" * 64)
        assert control_entry_block(db.control(SLOT)) == "entries_paused"


def test_history_write_failure_rolls_back_fill_and_cursor() -> None:
    now = time.time_ns()

    class Venue:
        async def user_trades(self, *_args, **_kwargs):
            return [
                {
                    "id": 1,
                    "orderId": 123,
                    "qty": "1",
                    "price": "100",
                    "realizedPnl": "0",
                    "commission": "0",
                    "commissionAsset": "USDT",
                    "time": now // 1_000_000,
                },
                {
                    "id": 2,
                    "orderId": 123,
                    "qty": "-1",
                    "price": "100",
                    "realizedPnl": "0",
                    "commission": "0",
                    "commissionAsset": "USDT",
                    "time": now // 1_000_000,
                },
            ]

    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=Venue())
        with pytest.raises(CheckViolation):
            asyncio.run(runner._sync_trades("BTCUSDT", now))
        assert db.trade_cursor("BTCUSDT", account_slot=SLOT) is None
        assert not db.fill_ledger(since_ns=0, limit=10)


def test_partial_reservation_remainder_is_not_released_by_symbol() -> None:
    class Venue(FakeDemo):
        async def positions(self):
            return [{"symbol": "BTCUSDT", "positionAmt": "0.4", "markPrice": "100", "entryPrice": "100"}]

        async def account(self):
            value = await super().account()
            value["positions"] = [{"symbol": "BTCUSDT", "positionAmt": "0.4"}]
            return value

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        with conn.transaction():
            db.update_order(
                client_id="entry-a",
                status="working",
                now_ns=now,
                evidence={
                    "clientOrderId": "entry-a",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "executedQty": "0.4",
                },
            )
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=Venue(now))
        facts = asyncio.run(runner._entry_facts(signal("b", now).model_copy(update={"native_symbol": "BTCUSDT"})))
        assert facts.active_notional_usdt == Decimal(100)
        assert facts.unreflected_pending_margin_usdt == Decimal(60)  # old NULL conservatively uses 1x
        assert facts.initial_margin_usdt == 0


def test_changed_account_reservation_prevents_second_snapshot_admission() -> None:
    from tests.trading.test_executor_core import facts

    now = time.time_ns()
    with closing(connect_postgres_test()) as first, closing(connect_postgres_test()) as second:
        seed_case(first)
        db = ExecutorStorage(first)
        with first.transaction():
            db.set_control(account_slot=SLOT, paused=False, halted=False, now_ns=now)
            db.append_signal(signal("a", now))
            db.append_signal(signal("b", now).model_copy(update={"native_symbol": "ETHUSDT"}))
        stale = replace(facts(), now_ns=now, quote_at_ns=now, active_entry_versions=())
        runner = ExecutorRunner(settings=settings(), conn=second, venue=FakeDemo(now))
        with first.transaction():
            assert accept(db, "a", now)
        asyncio.run(
            runner._submit_entry(
                signal("b", now).model_copy(update={"native_symbol": "ETHUSDT"}), stale, command_id=None
            )
        )
        assert db.entry("b" * 64)["state"] == "pending"
        assert not runner.venue.market_calls


def test_cancelled_database_operation_waits_for_rollback(tmp_path, postgres_clone_dsn) -> None:
    started = threading.Event()
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        configured = settings()
        configured.storage.postgres.dsn = postgres_settings_storage()["postgres"]["dsn"]
        configured.storage.postgres.password_file = None
        runner = AnalysisRunner(
            settings=configured, market_data=object(), assessor=None, program_sha="0" * 64, raw_root=tmp_path
        )

        def mutate(repos):
            repos.conn.execute("UPDATE trading_accounts SET entries_paused=true WHERE account_slot=%s", (SLOT,))
            started.set()
            repos.conn.execute("SELECT pg_sleep(0.1)")

        async def run():
            task = asyncio.create_task(runner._db_async(mutate, transaction=True))
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        try:
            asyncio.run(run())
            assert not db.account(SLOT)["entries_paused"]
        finally:
            runner._db_pool.shutdown(wait=True)


def test_claim_time_is_checked_after_waiting_for_case_lock() -> None:
    from tracefold.trading.storage.root import TradingRepository

    with closing(connect_postgres_test()) as owner, closing(connect_postgres_test()) as follower:
        seed_case(owner)
        now_ms = time.time_ns() // 1_000_000
        with owner.transaction():
            claim = TradingRepository(owner).claim_case(now_ms=now_ms, lease_ms=80)
        assert claim is not None
        started = threading.Event()

        def check():
            started.set()
            with follower.transaction():
                return TradingRepository(follower).claim_is_current(
                    case_id=claim["case_id"], claim_token=claim["claim_token"], now_ms=None
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            with owner.transaction():
                owner.execute("SELECT 1 FROM trading_cases WHERE case_id=%s FOR UPDATE", (claim["case_id"],))
                result = pool.submit(check)
                assert started.wait(1)
                owner.execute("SELECT pg_sleep(0.1)")
            assert not result.result(timeout=2)
        with follower.transaction():
            renewed = TradingRepository(follower).claim_case(now_ms=time.time_ns() // 1_000_000, lease_ms=1000)
            assert renewed is not None and renewed["claim_token"] != claim["claim_token"]
            assert not TradingRepository(follower).finish_case(
                case_id=claim["case_id"],
                claim_token=claim["claim_token"],
                status="complete",
                failure_code=None,
                now_ms=None,
            )


@pytest.mark.parametrize("protection", ["unknown", "undersized"])
def test_unverified_protection_reduces_risk_without_resending(protection: str) -> None:
    class Venue(FakeDemo):
        async def market_order(self, **request):
            assert request["reduce_only"] and request["quantity"] == Decimal("0.8")
            self.market_calls.append(request["client_id"])
            self.amount = Decimal(0)
            return {
                "orderId": 999,
                "clientOrderId": request["client_id"],
                "symbol": "BTCUSDT",
                "status": "FILLED",
                "side": "SELL",
                "executedQty": "0.8",
            }

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        with conn.transaction():
            db.update_order(
                client_id="entry-a",
                status="filled",
                now_ns=now,
                evidence={
                    "clientOrderId": "entry-a",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "executedQty": "0.8",
                    "status": "FILLED",
                },
            )
            db.reserve_order(
                client_id="old-sl",
                entry_id="a" * 64,
                native_symbol="BTCUSDT",
                leg="sl",
                attempt=1,
                now_ns=now - 61_000_000_000,
            )
            db.begin_send(client_id="old-sl", request={"clientAlgoId": "old-sl"}, now_ns=now - 61_000_000_000)
            if protection == "undersized":
                db.update_order(
                    client_id="old-sl",
                    status="working",
                    now_ns=now,
                    evidence={
                        "clientAlgoId": "old-sl",
                        "symbol": "BTCUSDT",
                        "side": "SELL",
                        "orderType": "STOP_MARKET",
                        "triggerPrice": "99",
                        "reduceOnly": True,
                        "quantity": "0.4",
                        "algoStatus": "NEW",
                    },
                )
        venue = Venue(now)
        venue.amount = Decimal("0.8")
        if protection == "undersized":
            venue.algos["old-sl"] = db.entry_orders("a" * 64)[1]["evidence"]
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)

        async def run():
            await runner._step_plan(
                plan=db.entry("a" * 64),
                position=(await venue.positions())[0],
                open_algos=venue.algos,
                force_flatten=False,
                now=now,
            )
            assert len(venue.market_calls) == 1
            assert len([o for o in db.entry_orders("a" * 64) if o["leg"] == "sl"]) == 1
            await runner._step_plan(
                plan=db.entry("a" * 64),
                position=(await venue.positions())[0],
                open_algos=venue.algos,
                force_flatten=False,
                now=now + 1,
            )
            if protection == "unknown":
                assert db.entry("a" * 64)["terminal_at_ns"] is None
            assert len(venue.market_calls) == 1

        asyncio.run(run())


def test_cancel_ack_does_not_release_entry_before_last_fill_is_observed() -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        proof = {
            "clientOrderId": "entry-a",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "executedQty": "0.4",
            "status": "PARTIALLY_FILLED",
        }
        with conn.transaction():
            db.update_order(client_id="entry-a", status="working", now_ns=now, evidence=proof)
        venue = FakeDemo(now)
        venue.amount = Decimal("0.4")
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)

        async def run():
            await runner._step_plan(
                plan=db.entry("a" * 64),
                position=(await venue.positions())[0],
                open_algos={},
                force_flatten=False,
                now=now,
            )
            await runner._step_plan(
                plan=db.entry("a" * 64),
                position=(await venue.positions())[0],
                open_algos=venue.algos,
                force_flatten=False,
                now=now + 1,
            )
            assert db.entry_orders("a" * 64)[0]["status"] == "working"
            assert db.entry("a" * 64)["terminal_at_ns"] is None
            # A last fill wins the DELETE race. Confirmed closePosition still covers it.
            venue.amount = Decimal("0.8")
            with conn.transaction():
                db.update_order(
                    client_id="entry-a",
                    status="cancelled",
                    now_ns=now + 2,
                    evidence={**proof, "status": "CANCELED", "executedQty": "0.8"},
                )
            await runner._step_plan(
                plan=db.entry("a" * 64),
                position=(await venue.positions())[0],
                open_algos=venue.algos,
                force_flatten=False,
                now=now + 3,
            )
            assert len([a for a in venue.algos.values() if a["leg"] == "sl"]) == 1
            assert len([a for a in venue.algos.values() if a["leg"] == "tp"]) == 1
            assert db.entry("a" * 64)["opened_at_ns"] == now

        asyncio.run(run())


def test_external_flatten_failure_is_local_and_recovery_clears_only_own_symbol() -> None:
    from tests.integration.test_p0_executor_pending import intent

    class Venue:
        def __init__(self):
            self.amounts = {"AAAUSDT": Decimal(1), "BBBUSDT": Decimal(1)}
            self.sent = []

        async def positions(self):
            return [{"symbol": s, "positionAmt": str(q), "positionSide": "BOTH"} for s, q in self.amounts.items()]

        async def open_orders(self, symbol=None):
            return [{"symbol": "AAAUSDT", "clientOrderId": "external-a"}] if symbol in (None, "AAAUSDT") else []

        async def open_algo_orders(self):
            return []

        async def cancel_symbol_orders(self, symbol):
            assert symbol == "AAAUSDT"
            raise httpx.ConnectError("local symbol cancel failure")

        async def market_order(self, **request):
            assert request["symbol"] == "BBBUSDT" and request["reduce_only"]
            self.sent.append(request)
            self.amounts["BBBUSDT"] = Decimal(0)
            return {
                "orderId": 999,
                "symbol": "BBBUSDT",
                "side": request["side"],
                "clientOrderId": request["client_id"],
                "executedQty": "1",
                "status": "FILLED",
            }

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        db = ExecutorStorage(conn)
        command_id = "b" * 64
        with conn.transaction():
            db.append_operator_intent(intent("b", "flatten", now))
            db.apply_control(account_slot=SLOT, action="flatten", command_id=command_id, now_ns=now)
            for symbol in ("AAAUSDT", "BBBUSDT"):
                db.record_fault(
                    account_slot=SLOT,
                    responsibility=f"{command_id}|{symbol}",
                    code="exit_attempts_exhausted",
                    symbol=symbol,
                    client_ids=[],
                    amount=Decimal(1),
                    now_ns=now,
                )
        venue = Venue()
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)
        asyncio.run(runner._reconcile_account_flatten(command_id, [], now))
        assert len(venue.sent) == 1
        faults = db.account(SLOT)["execution_faults"]
        assert f"{command_id}|AAAUSDT" in faults
        assert f"{command_id}|BBBUSDT" not in faults
        assert db.control(SLOT)["flatten_command_id"] == command_id


def test_native_zero_cannot_override_observed_exposure() -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        with conn.transaction():
            db.update_order(client_id="entry-a", status="cancelled", now_ns=now, evidence={"executedQty": "0"})
            db.set_entry_state(entry_id="a" * 64, status="terminal", opened_at_ns=now, now_ns=now)
            assert db.settle_pnl(plan=db.entry("a" * 64), now_ns=now) == "pending"
            assert db.settle_pnl(plan=db.entry("a" * 64), now_ns=now + 61_000_000_000) == "evidence_incomplete"
        assert db.entry("a" * 64)["net_pnl"] is None


def test_admission_write_failure_rolls_back_acceptance_and_never_posts(monkeypatch) -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        db = ExecutorStorage(conn)
        request = signal("a", now)
        with conn.transaction():
            db.set_control(account_slot=SLOT, paused=False, halted=False, now_ns=now)
            db.append_signal(request)
        venue = FakeDemo(now)
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)
        original = runner.db.reserve_order

        def invalid_reservation(**kwargs):
            original(**{**kwargs, "attempt": 0})

        monkeypatch.setattr(runner.db, "reserve_order", invalid_reservation)

        async def run():
            facts = await runner._entry_facts(request)
            with pytest.raises(CheckViolation):
                await runner._submit_entry(request, facts, command_id=None)

        asyncio.run(run())
        assert db.entry("a" * 64)["state"] == "pending"
        assert db.entry("a" * 64)["admission"] is None
        assert db.entry_orders("a" * 64) == []
        assert venue.market_calls == []


def test_root_expiry_between_acceptance_and_post_prevents_send() -> None:
    from unittest.mock import patch

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        # Persist a legitimately short remaining source lifetime, without rewriting the root.
        with patch("tests.integration.test_p0_executor_pending.time.time_ns", return_value=now - 3_599_970_000_000):
            db = prepare(conn, now)
        venue = FakeDemo(now)
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)

        async def run():
            await asyncio.sleep(0.05)
            await runner._send_market(
                entry_id="a" * 64,
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal(1),
                client_id="entry-a",
                reduce_only=False,
                now=now,
            )

        asyncio.run(run())
        order = db.entry_orders("a" * 64)[0]
        assert order["status"] == "not_submitted"
        assert order["request"] is None and order["submitted_at_ns"] is None
        assert order["resolution"]["definitely_not_executed"] is True
        assert venue.market_calls == []


def test_cancel_algo_ack_keeps_unknown_until_original_identity_query() -> None:
    class Venue(FakeDemo):
        async def cancel_algo(self, client_id):
            value = self.algos[client_id]
            self.algos[client_id] = {**value, "algoStatus": "CANCELED"}
            return {"algoId": value["algoId"], "clientAlgoId": client_id, "code": "200", "msg": "success"}

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        venue = Venue(now)
        venue.amount = Decimal("0.4")
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)

        async def run():
            await runner._protect(db.entry("a" * 64), (await venue.positions())[0], leg="sl", now=now)
            await runner._cancel_protection(db.entry("a" * 64), venue.algos, now)
            order = next(o for o in db.entry_orders("a" * 64) if o["leg"] == "sl")
            assert order["status"] == "unknown"
            assert order["evidence"]["code"] == "200" and "algoStatus" not in order["evidence"]
            await runner._refresh_entry_orders(db.entry("a" * 64), {}, now + 1, Decimal(0))
            resolved = next(o for o in db.entry_orders("a" * 64) if o["leg"] == "sl")
            assert resolved["status"] == "cancelled" and resolved["evidence"]["algoStatus"] == "CANCELED"

        asyncio.run(run())


def test_incomplete_algo_success_queries_identity_without_new_attempt() -> None:
    class Venue(FakeDemo):
        async def protection_order(self, **request):
            self.market_calls.append(request["client_id"])
            return {"clientAlgoId": request["client_id"], "algoId": 999}

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = prepare(conn, now)
        with conn.transaction():
            db.update_order(
                client_id="entry-a",
                status="filled",
                now_ns=now,
                evidence={"clientOrderId": "entry-a", "symbol": "BTCUSDT", "side": "BUY", "executedQty": "0.4"},
            )
        venue = Venue(now)
        venue.amount = Decimal("0.4")
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=venue)

        async def run():
            for instant in (now, now + 1):
                await runner._step_plan(
                    plan=db.entry("a" * 64),
                    position=(await venue.positions())[0],
                    open_algos={},
                    force_flatten=False,
                    now=instant,
                )
            assert len(venue.market_calls) == 1
            order = next(o for o in db.entry_orders("a" * 64) if o["leg"] == "sl")
            assert order["status"] == "unknown"
            assert order["request"]["algoType"] == "CONDITIONAL"
            assert order["request"]["type"] == "STOP_MARKET"

        asyncio.run(run())


def test_new_flatten_does_not_replace_unknown_command_or_reset_attempts() -> None:
    from tests.integration.test_p0_executor_pending import intent

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.append_operator_intent(intent("a", "flatten", now))
            db.apply_control(account_slot=SLOT, action="flatten", command_id="a" * 64, now_ns=now)
            db.record_disposition(
                kind="intent",
                input_id="a" * 64,
                account_slot=SLOT,
                disposition="accepted",
                reason="flatten_requested",
                now_ns=now,
            )
            db.reserve_external_flatten(
                client_id="unknown-exit", command_id="a" * 64, symbol="BTCUSDT", attempt=1, now_ns=now
            )
            db.begin_send(client_id="unknown-exit", request={"reduceOnly": True}, now_ns=now)
            db.append_operator_intent(intent("b", "flatten", now + 1))
        runner = ExecutorRunner(settings=settings(), conn=conn, venue=FakeDemo(now))
        asyncio.run(runner._one_intent(now + 1))
        assert db.control(SLOT)["flatten_command_id"] == "a" * 64
        request = db.console_operator_intents(since_ns=0, action="flatten", limit=10)
        second = next(row for row in request if row["command_id"] == "b" * 64)
        assert second["disposition"] == "refused" and second["disposition_reason"] == "flatten_in_progress"
        assert db.external_flatten_orders("a" * 64, "BTCUSDT")[0]["status"] == "unknown"
