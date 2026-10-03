"""Current-ledger counterexamples from #760; exercise production decisions and runners."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from tests.trading.test_executor_core import facts, signal
from tracefold.integrations.trading.binance import BinanceFailure
from tracefold.trading.executor.core import EntryLifecycleFacts, admit, step


def test_available_margin_and_actual_leverage_bound_quantity() -> None:
    constrained = replace(
        facts(),
        available_margin_usdt=Decimal(200),
        actual_leverage=1,
        mark_price=Decimal(100),
        taker_fee_rate=Decimal("0.001"),
        symbol_notional_cap=Decimal(10000),
        margin_type="crossed",
    )
    verdict = admit(signal(), constrained)
    assert verdict.accepted and verdict.quantity is not None
    assert verdict.quantity < Decimal(2)


@pytest.mark.parametrize("unknown,status", [(True, None), (False, "NEW"), (False, "PARTIALLY_FILLED")])
def test_confirmed_exposure_receives_stop_before_entry_completes(unknown: bool, status: str | None) -> None:
    decision = step(
        EntryLifecycleFacts(
            now_ns=10,
            entered_at_ns=1,
            max_hold_s=100,
            position_amount=Decimal(1),
            entry_order_status=status,
            entry_submission_unknown=unknown,
            sl_status=None,
            tp_status=None,
            sl_attempts=0,
            tp_attempts=0,
            sl_submission_unknown=False,
            tp_submission_unknown=False,
            flatten_status=None,
            exit_fill_client_id=None,
            sl_client_ids=frozenset(),
            tp_client_ids=frozenset(),
        )
    )
    assert decision.action == "submit_sl"


@pytest.mark.parametrize("status,code", [(200, None), (400, None), (400, -9999), (503, -1000)])
def test_bad_or_unknown_response_does_not_prove_rejection(status: int, code: int | None) -> None:
    assert not BinanceFailure(status, code, "invalid_json").definitely_not_executed


def test_history_failure_is_local_and_does_not_advance_cursor() -> None:
    from tracefold.app.executor import ExecutorRunner

    class Venue:
        async def user_trades(self, *_args: object, **_kwargs: object) -> object:
            raise httpx.ConnectError("offline")

    class Store:
        def trade_cursor(self, *_args: object, **_kwargs: object) -> None:
            return None

    runner = object.__new__(ExecutorRunner)
    runner.venue = Venue()
    runner.db = Store()
    runner.account_slot = "demo-primary"
    asyncio.run(runner._sync_trades("BTCUSDT", 1))


def test_eight_cases_two_model_slots_claim_only_available_turns(tmp_path, monkeypatch) -> None:
    from dataclasses import asdict

    from tests.trading.test_trading_assessor import _RecordedProgram, _view
    from tracefold.app.trading_analysis import AnalysisRunner
    from tracefold.app.trading_assessor import TradingAssessor
    from tracefold.platform.config.models import Settings

    async def run() -> None:
        completed_calls = 0
        clock = 1_900_000_000_000
        claimed = 0
        finished = 0
        maximum = 0
        leases = {}
        monkeypatch.setattr("tracefold.app.trading_analysis._clock_ms", lambda: clock)

        class Program(_RecordedProgram):
            async def acall(self, **kwargs):
                nonlocal clock, completed_calls
                await asyncio.sleep(0.02)
                completed_calls += 1
                clock = 1_900_000_000_000 + ((completed_calls + 1) // 2) * 55_000
                return await super().acall(**kwargs)

        class Store:
            def claim_case(self, *, now_ms, lease_ms):
                nonlocal claimed, maximum
                if claimed == 8:
                    return None
                claimed += 1
                maximum = max(maximum, claimed - finished)
                key = str(claimed)
                leases[key] = now_ms + lease_ms
                return {
                    "case_id": key,
                    "claim_token": key,
                    "view": asdict(_view()),
                    "reference_price": "100",
                    "root_expires_at_ms": now_ms + 600_000,
                }

            def claim_is_current(self, *, case_id, claim_token, now_ms):
                return leases[case_id] > (clock if now_ms is None else now_ms)

            def finish_case(self, *, case_id, **_kwargs):
                nonlocal finished
                assert leases[case_id] > clock
                finished += 1
                return True

            def record_forecast(self, **_kwargs):
                pass

            def record_policy_actions(self, **_kwargs):
                pass

            def set_publication(self, **_kwargs):
                pass

        configured = Settings(trading={"analysis": {"max_active_cases": 8, "publish_signals": False}})
        program = Program(
            {
                "long": {"p_tp": "0.6", "p_sl": "0.2", "p_timeout": "0.2"},
                "short": {"p_tp": "0.2", "p_sl": "0.6", "p_timeout": "0.2"},
            }
        )
        assessor = TradingAssessor(program=program, lm=object(), timeout_s=60, concurrent=2)
        runner = AnalysisRunner(
            settings=configured, market_data=object(), assessor=assessor, program_sha="0" * 64, raw_root=tmp_path
        )
        repos = SimpleNamespace(trading=Store())

        async def db(fn, **_kwargs):
            return fn(repos)

        runner._db_async = db
        try:
            await asyncio.gather(*(runner.analyze_one() for _ in range(8)))
            assert claimed == finished == program.calls == 8
            assert maximum == 2
            assert clock == 1_900_000_220_000
        finally:
            runner._db_pool.shutdown(wait=True)

    asyncio.run(run())


def test_slow_terminal_history_cannot_delay_active_risk_actions(monkeypatch) -> None:
    from contextlib import nullcontext

    from tracefold.app.executor import ExecutorRunner

    async def run() -> None:
        actions = []

        class Store:
            def active_entries(self, _slot):
                return [{"entry_id": "a", "native_symbol": "BTCUSDT"}, {"entry_id": "b", "native_symbol": "ETHUSDT"}]

            def entries_awaiting_fills(self, _slot):
                return [{"entry_id": "c", "native_symbol": "SOLUSDT"}]

            def control(self, _slot):
                return {}

            def trade_cursor(self, *_args, **_kwargs):
                return None

            def attribute_unbound_fills(self, **_kwargs):
                pass

        class Venue:
            async def positions(self):
                return []

            async def open_algo_orders(self):
                return []

            async def user_trades(self, symbol, **_kwargs):
                assert actions == ["a", "b"]
                if symbol == "SOLUSDT":
                    await asyncio.sleep(10)
                return []

        runner = object.__new__(ExecutorRunner)
        runner.account_slot = "demo-primary"
        runner._history_offset = 0
        runner.db = Store()
        runner.venue = Venue()
        runner.conn = SimpleNamespace(transaction=nullcontext)

        async def refresh(*_args):
            pass

        async def manage(*, plan, **_kwargs):
            actions.append(plan["entry_id"])

        runner._refresh_entry_orders = refresh
        runner._step_plan = manage
        started = asyncio.get_running_loop().time()
        await runner._reconcile(1)
        assert actions == ["a", "b"]
        assert asyncio.get_running_loop().time() - started < 3

    asyncio.run(run())


@pytest.mark.parametrize("margin_type", ["crossed", "isolated"])
def test_supported_single_asset_opening_uses_available_cash(margin_type: str) -> None:
    constrained = replace(
        facts(),
        margin_type=margin_type,
        available_margin_usdt=Decimal(200),
        actual_leverage=1,
        initial_margin_usdt=Decimal(800),
    )
    verdict = admit(signal(), constrained)
    assert verdict.accepted and verdict.quantity < Decimal(2)
    assert verdict.reserved_margin_usdt <= constrained.available_margin_usdt
