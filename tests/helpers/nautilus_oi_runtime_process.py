"""Subprocess entrypoint for the real PostgreSQL -> Nautilus callback seam."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from typing import Any

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
    OiRuntimeDatabaseBridge,
    flush_audit_once,
    load_recovery_inputs,
    load_runtime_control_state,
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


# Test-side readers of bounded-queue and bridge private state (#589 PR-2). Production reads none of
# these: the Runtime drains its queues through `next_nowait` / `next_command_nowait` and decides on
# `queued_command_count` plus `command_scan_complete`, and nothing at all reads whether the database
# bridge currently holds a session. Six public properties existed only so assertions could name what a
# bound had done, which is a reader of the object's internals wherever it is spelled -- so they are
# spelled here, once, instead of on the production classes.
def audit_queued_count(sink: AuditSink) -> int:
    with sink._lock:
        return len(sink._values)


def audit_queued_bytes(sink: AuditSink) -> int:
    with sink._lock:
        return sink._bytes


def signal_queued_count(client: ExecutionSignalClient) -> int:
    with client._lock:
        return len(client._values) + len(client._commands)


def signal_queued_bytes(client: ExecutionSignalClient) -> int:
    with client._lock:
        return client._bytes


def signal_pending_ids(client: ExecutionSignalClient) -> frozenset[str]:
    with client._lock:
        return frozenset(client._pending_ids)


def signal_pending_command_ids(client: ExecutionSignalClient) -> frozenset[str]:
    with client._lock:
        return frozenset(client._pending_command_ids)


def bridge_connected(bridge: OiRuntimeDatabaseBridge) -> bool:
    with bridge._lock:
        return bridge._connected


class _CountingOiStrategy(OiNautilusStrategy):
    """Count what the Runtime asks the venue to stream, before and after #510 PR-5b."""

    def __init__(self, *, on_position_opened_hook: Callable[[], None] | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.quote_subscribe_calls = 0
        self.quote_unsubscribe_calls = 0
        self.peak_quote_subscriptions = 0
        self._on_position_opened_hook = on_position_opened_hook

    def on_position_opened(self, event: object) -> None:
        """`flatten_owned` needs the Command to arrive *after* the entry, as an operator's does.

        A flatten routed before the Signal pauses entries, so the position the exit is supposed to
        close would never open. The hook polls the same durable Command read the bridge polls.
        """

        super().on_position_opened(event)
        hook, self._on_position_opened_hook = self._on_position_opened_hook, None
        if hook is not None:
            hook()

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
            namespace=profile.namespace,
            entry_id=entry_id,
            leg=protection_leg(1),
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


def _manual_entries_only(reader: Callable[[str, str, int], Any]) -> Callable[[str, str, int], Any]:
    """The first Command poll of a manual-entry run sees the entry and not the exit.

    An operator issues `/flatten` after watching the position open, so the flatten Command is polled
    from `on_position_opened` -- the same shape `flatten_owned` runs for a Signal, with the entry
    itself now arriving as a Command too.
    """

    def read(account_slot: str, execution_strategy: str, limit: int) -> list[Any]:
        values = reader(account_slot, execution_strategy, limit)
        return [value for value in values if value.action == "manual_entry"]

    return read


def main() -> None:
    dsn = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "signal"
    if mode not in {
        "command",
        "signal",
        "signal_replay",
        "cold_recovery",
        "cold_unclaimed",
        "rolling_restart",
        "stop_filled",
        "flatten_owned",
        "manual_entry_flatten",
    }:
        raise ValueError("nautilus_process_fixture_mode_invalid")
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    try:
        signals = ExecutionSignalClient(
            account_slot=oi_profile().account_slot,
            execution_strategy="oi_nautilus_v1",
        )
        repos = repositories_for_connection(conn)
        admitted = (
            signals.poll_once(partial(load_unresolved_trade_signals, repos))
            if mode
            in {
                "signal",
                "signal_replay",
                "cold_recovery",
                "cold_unclaimed",
                "rolling_restart",
                "stop_filled",
                "flatten_owned",
            }
            else 0
        )
        replay_admitted = (
            signals.poll_once(partial(load_unresolved_trade_signals, repos)) if mode == "signal_replay" else None
        )
        unresolved_commands = partial(load_unresolved_operator_intents, repos)
        admitted_commands = 0
        if mode in {"command", "cold_unclaimed"}:
            admitted_commands = signals.poll_commands_once(unresolved_commands)
        elif mode == "manual_entry_flatten":
            admitted_commands = signals.poll_commands_once(_manual_entries_only(unresolved_commands))
        profile = oi_profile()
        control_state = RuntimeControlSnapshot(False, False, ())
        if mode == "rolling_restart":
            # A rolling restart after a code or configuration change: same account slot, same
            # deployment, a new build. #520 PR-A: this simply starts, keeps whatever control state
            # the operator last set, and never demands a flat account.
            control_state = load_runtime_control_state(repos, profile.account_slot, now_ns=NOW_NS)
        factory = ObservationFactory(profile.account_slot, "oi_nautilus_v1")
        audit = AuditSink(factory=factory)
        readiness = RuntimeReadiness(reconciliation_stale_after_ns=profile.risk.reconciliation_stale_after_ns)
        poll_commands = partial(signals.poll_commands_once, unresolved_commands)
        strategy = _CountingOiStrategy(
            on_position_opened_hook=(poll_commands if mode in {"flatten_owned", "manual_entry_flatten"} else None),
            profile=profile,
            signals=signals,
            audit=audit,
            readiness=readiness,
            # One `BacktestEngine` thread, no event loop: the timer callback already is the
            # callback thread, and marshalling would have nowhere to marshal to.
            dispatch_pump=lambda pump: pump(),
            singleton_ready=lambda: True,
            day_start=DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS - 1, "4" * 64),
            request_reconciliation=lambda _reason: None,
            initial_control_state=control_state,
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
        tape: list[Any] = [
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
        if mode == "stop_filled":
            # The route's stop is 200 bps under a ~10 000 entry, so 9 700 is through it. `LAST_PRICE`
            # is what the Runtime arms the stop with, and only a trade print carries a last price.
            tape.append(
                TestDataStubs.trade_tick(
                    instrument=instrument,
                    price=9_700,
                    size=1,
                    ts_event=NOW_NS + 300_000_000,
                    ts_init=NOW_NS + 300_000_000,
                )
            )
            tape.append(
                TestDataStubs.quote_tick(
                    instrument=instrument,
                    bid_price=9_699,
                    ask_price=9_700,
                    ts_event=NOW_NS + 400_000_000,
                    ts_init=NOW_NS + 400_000_000,
                )
            )
        if mode in {"flatten_owned", "manual_entry_flatten"}:
            tape.append(
                TestDataStubs.quote_tick(
                    instrument=instrument,
                    bid_price=9_999,
                    ask_price=10_000,
                    ts_event=NOW_NS + 400_000_000,
                    ts_init=NOW_NS + 400_000_000,
                )
            )
        engine.add_data(tape)
        engine.add_strategy(strategy)
        if mode in {"cold_recovery", "cold_unclaimed", "rolling_restart"}:
            recovery_signals, recovery_manual_entries = load_recovery_inputs(repos, profile.account_slot, NOW_NS)
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
                account_slot=profile.account_slot,
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
                    "pending": sorted(signal_pending_ids(signals)),
                    "pending_commands": sorted(signal_pending_command_ids(signals)),
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
