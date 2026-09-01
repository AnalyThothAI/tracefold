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
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from psycopg.rows import dict_row

from tests.nautilus_oi_runtime_fixtures import NOW_NS, oi_profile
from tracefold.app.nautilus.oi_runtime import (
    flush_audit_once,
    load_unresolved_operator_intents,
    load_unresolved_trade_signals,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.strategy import (
    OiNautilusStrategy,
    RuntimeControlSnapshot,
    RuntimeReadiness,
    RuntimeReconciliationSnapshot,
)


def main() -> None:
    dsn = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "signal"
    if mode not in {"command", "signal"}:
        raise ValueError("nautilus_process_fixture_mode_invalid")
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    try:
        signals = ExecutionSignalClient(
            runtime_profile_id="oi-paper-profile",
            execution_strategy="oi_nautilus_v1",
        )
        repos = repositories_for_connection(conn)
        admitted = signals.poll_once(partial(load_unresolved_trade_signals, repos)) if mode == "signal" else 0
        admitted_commands = (
            signals.poll_commands_once(partial(load_unresolved_operator_intents, repos)) if mode == "command" else 0
        )
        profile = oi_profile()
        factory = ObservationFactory(profile.profile_id, profile.runtime_release, "oi_nautilus_v1")
        audit = AuditSink(factory=factory)
        readiness = RuntimeReadiness()
        readiness.activate()
        strategy = OiNautilusStrategy(
            profile=profile,
            signals=signals,
            audit=audit,
            readiness=readiness,
            singleton_ready=lambda: True,
            day_start=DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS - 1, "4" * 64),
            initial_control_state=RuntimeControlSnapshot(False, False, ()),
            startup_reconciliation=RuntimeReconciliationSnapshot(
                runtime_profile_id=profile.profile_id,
                account_observed_at_ns=NOW_NS,
                reconciliation_observed_at_ns=NOW_NS,
            ),
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
        engine.run()
        flushed = flush_audit_once(
            repos=repos,
            audit=audit,
            signals=signals,
        )
        orders = engine.cache.orders(strategy_id=strategy.id)
        positions = engine.cache.positions_open(strategy_id=strategy.id)
        print(
            json.dumps(
                {
                    "admitted": admitted,
                    "admitted_commands": admitted_commands,
                    "orders": [
                        {
                            "client_order_id": order.client_order_id.value,
                            "order_type": order.order_type.name,
                            "reduce_only": order.is_reduce_only,
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
