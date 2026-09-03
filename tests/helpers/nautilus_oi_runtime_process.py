"""Subprocess entrypoint for the real PostgreSQL -> Nautilus callback seam."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from functools import partial

import psycopg
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TriggerType
from nautilus_trader.model.identifiers import ClientOrderId, PositionId, TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from psycopg.rows import dict_row

from tests.nautilus_oi_runtime_fixtures import ACCOUNT_ID, NOW_NS, oi_profile
from tracefold.app.nautilus.oi_runtime import (
    flush_audit_once,
    load_recovery_inputs,
    load_unresolved_operator_intents,
    load_unresolved_trade_signals,
)
from tracefold.app.nautilus.reconciliation import build_runtime_reconciliation_snapshot
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.config import OiRuntimeProfile
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.state import (
    RuntimeControlSnapshot,
    RuntimeReadiness,
    RuntimeReconciliationSnapshot,
    deterministic_client_order_id,
    protection_leg,
)
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy

_COLD_QUANTITY = Decimal("0.049")
_COLD_ENTRY_PRICE = Decimal(10_000)


class _CountingOiStrategy(OiNautilusStrategy):
    """Count what the Runtime asks the venue to stream, before and after #510 PR-5b."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.quote_subscribe_calls = 0
        self.quote_unsubscribe_calls = 0
        self.peak_quote_subscriptions = 0

    def subscribe_quote_ticks(self, instrument_id: object, *args: object, **kwargs: object) -> None:
        self.quote_subscribe_calls += 1
        self.peak_quote_subscriptions = max(
            self.peak_quote_subscriptions,
            self.quote_subscribe_calls - self.quote_unsubscribe_calls,
        )
        super().subscribe_quote_ticks(instrument_id, *args, **kwargs)  # type: ignore[arg-type]

    def unsubscribe_quote_ticks(self, instrument_id: object, *args: object, **kwargs: object) -> None:
        self.quote_unsubscribe_calls += 1
        super().unsubscribe_quote_ticks(instrument_id, *args, **kwargs)  # type: ignore[arg-type]


def _seed_cold_cache(
    *,
    engine: BacktestEngine,
    strategy: OiNautilusStrategy,
    profile: OiRuntimeProfile,
    instrument: object,
    entry_id: str | None,
) -> None:
    """Reproduce a Cache reconciled from Binance private reports after a restart.

    `LiveExecutionEngine._reconcile_order_report` adds reclaimed orders without a position index,
    and the filled entry market order is in no Binance report at all, so a cold Cache holds only the
    synthesised position and the resting stop.
    """

    # The NETTING position identity Nautilus itself derives; the risk engine rejects any other.
    position_id = PositionId(f"{instrument.id}-{strategy.id}")
    external = strategy.order_factory.market(
        instrument_id=instrument.id,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(_COLD_QUANTITY),
        client_order_id=ClientOrderId("EXTERNAL-COLD-ENTRY"),
    )
    engine.cache.add_order(external)
    external.apply(TestEventStubs.order_submitted(external, account_id=ACCOUNT_ID, ts_event=NOW_NS - 2))
    engine.cache.update_order(external)
    fill = TestEventStubs.order_filled(
        order=external,
        instrument=instrument,
        strategy_id=strategy.id,
        account_id=ACCOUNT_ID,
        position_id=position_id,
        last_qty=instrument.make_qty(_COLD_QUANTITY),
        last_px=instrument.make_price(_COLD_ENTRY_PRICE),
        commission=Money(0, instrument.quote_currency),
        ts_event=NOW_NS - 1,
    )
    external.apply(fill)
    engine.cache.update_order(external)
    engine.cache.add_position(Position(instrument, fill), OmsType.NETTING)
    if entry_id is None:
        return
    stop = strategy.order_factory.stop_market(
        instrument_id=instrument.id,
        order_side=OrderSide.SELL,
        quantity=instrument.make_qty(_COLD_QUANTITY),
        trigger_price=instrument.make_price(Decimal("9800")),
        trigger_type=TriggerType.LAST_PRICE,
        reduce_only=True,
        client_order_id=deterministic_client_order_id(
            namespace=profile.client_order_namespace,
            profile_id=profile.profile_id,
            entry_id=entry_id,
            leg=protection_leg(1, _COLD_QUANTITY),
        ),
    )
    # `BacktestEngine.run` replays every cached open order through the matching engine, which
    # rejects a reduce-only order it cannot bind to a position. Binance holds this stop instead, so
    # the index is a harness detail; the unbound cold-Cache shape is covered by the unit seam.
    engine.cache.add_order(stop, position_id=position_id)
    stop.apply(TestEventStubs.order_submitted(stop, account_id=ACCOUNT_ID, ts_event=NOW_NS - 1))
    engine.cache.update_order(stop)
    stop.apply(TestEventStubs.order_accepted(stop, account_id=ACCOUNT_ID, ts_event=NOW_NS - 1))
    engine.cache.update_order(stop)


def main() -> None:
    dsn = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "signal"
    if mode not in {"command", "signal", "signal_replay", "cold_recovery", "cold_unclaimed"}:
        raise ValueError("nautilus_process_fixture_mode_invalid")
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    try:
        signals = ExecutionSignalClient(
            runtime_profile_id="oi-paper-profile",
            execution_strategy="oi_nautilus_v1",
        )
        repos = repositories_for_connection(conn)
        admitted = (
            signals.poll_once(partial(load_unresolved_trade_signals, repos))
            if mode in {"signal", "signal_replay", "cold_recovery", "cold_unclaimed"}
            else 0
        )
        replay_admitted = (
            signals.poll_once(partial(load_unresolved_trade_signals, repos)) if mode == "signal_replay" else None
        )
        admitted_commands = (
            signals.poll_commands_once(partial(load_unresolved_operator_intents, repos))
            if mode in {"command", "cold_unclaimed"}
            else 0
        )
        profile = oi_profile()
        factory = ObservationFactory(profile.profile_id, profile.runtime_release, "oi_nautilus_v1")
        audit = AuditSink(factory=factory)
        readiness = RuntimeReadiness()
        readiness.activate()
        strategy = _CountingOiStrategy(
            profile=profile,
            signals=signals,
            audit=audit,
            readiness=readiness,
            # One `BacktestEngine` thread, no event loop: the timer callback already is the
            # callback thread, and marshalling would have nowhere to marshal to.
            dispatch_pump=lambda pump: pump(),
            singleton_ready=lambda: True,
            control_plane_ready=lambda: True,
            day_start=DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS - 1, "4" * 64),
            request_reconciliation=lambda _reason: None,
            initial_control_state=RuntimeControlSnapshot(False, False, ()),
        )
        instrument = TestInstrumentProvider.btcusdt_perp_binance()
        engine = BacktestEngine(
            BacktestEngineConfig(
                trader_id=TraderId("OI-PROCESS"),
                logging=LoggingConfig(bypass_logging=True),
            )
        )
        engine.add_venue(
            venue=instrument.id.venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(1_000, instrument.quote_currency)],
            base_currency=None,
            default_leverage=Decimal(2),
        )
        engine.add_instrument(instrument)
        engine.add_data(
            [
                TestDataStubs.quote_tick(
                    instrument=instrument,
                    bid_price=9_999,
                    ask_price=10_000,
                    ts_event=NOW_NS,
                    ts_init=NOW_NS,
                ),
                TestDataStubs.quote_tick(
                    instrument=instrument,
                    bid_price=9_999,
                    ask_price=10_000,
                    ts_event=NOW_NS + 200_000_000,
                    ts_init=NOW_NS + 200_000_000,
                ),
            ]
        )
        engine.add_strategy(strategy)
        if mode in {"cold_recovery", "cold_unclaimed"}:
            recovery_signals, recovery_manual_entries = load_recovery_inputs(repos, profile.profile_id, NOW_NS)
            _seed_cold_cache(
                engine=engine,
                strategy=strategy,
                profile=profile,
                instrument=instrument,
                entry_id=recovery_signals[0].signal_id if recovery_signals else None,
            )
            snapshot = build_runtime_reconciliation_snapshot(
                profile=profile,
                signals=recovery_signals,
                manual_entries=recovery_manual_entries,
                cache=engine.cache,
                account_observed_at_ns=NOW_NS,
                reconciliation_observed_at_ns=NOW_NS,
            )
            recovered = strategy.reconcile_runtime(snapshot)
        else:
            recovery_signals = ()
            snapshot = RuntimeReconciliationSnapshot(
                runtime_profile_id=profile.profile_id,
                account_observed_at_ns=NOW_NS,
                reconciliation_observed_at_ns=NOW_NS,
            )
            recovered = strategy.reconcile_runtime(snapshot)
        engine.run()
        flushed = flush_audit_once(
            repos=repos,
            audit=audit,
            signals=signals,
        )
        orders = engine.cache.orders(strategy_id=strategy.id)
        positions = engine.cache.positions_open(strategy_id=strategy.id)
        readiness_snapshot = strategy.readiness()
        print(
            json.dumps(
                {
                    "admitted": admitted,
                    "recovered": recovered,
                    "quote_subscribe_calls": strategy.quote_subscribe_calls,
                    "quote_unsubscribe_calls": strategy.quote_unsubscribe_calls,
                    "quote_subscriptions": strategy.peak_quote_subscriptions,
                    "route_catalogue": len(profile.routes),
                    "recovered_seeds": len(snapshot.executions),
                    "recovery_signals": len(recovery_signals),
                    "execution_safe": readiness_snapshot.execution_safe,
                    "unexpected_exposure": readiness_snapshot.unexpected_exposure,
                    "positions_count": len(positions),
                    "protection_status": strategy.protection_status(
                        positions_count=len(positions),
                        unexpected_exposure=readiness_snapshot.unexpected_exposure,
                    ),
                    **({"replay_admitted": replay_admitted} if replay_admitted is not None else {}),
                    "admitted_commands": admitted_commands,
                    "orders": [
                        {
                            "client_order_id": order.client_order_id.value,
                            "order_type": order.order_type.name,
                            "quantity": str(order.quantity),
                            "reduce_only": order.is_reduce_only,
                            "side": order.side.name,
                            "status": order.status.name,
                        }
                        for order in orders
                    ],
                    "open_position_quantity": str(positions[0].quantity) if positions else None,
                    "flushed": flushed,
                    "pending": sorted(signals.pending_ids),
                    "pending_commands": sorted(signals.pending_command_ids),
                    "control": {
                        "entries_paused": strategy.control_state().entries_paused,
                        "emergency_halted": strategy.control_state().emergency_halted,
                        "flatten_pending": list(strategy.control_state().flatten_pending),
                    },
                },
                sort_keys=True,
            )
        )
        engine.dispose()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
