"""Pinned Nautilus Strategy seam for the OI Runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OmsType, OrderSide, OrderType, PositionSide, TriggerType
from nautilus_trader.model.identifiers import ClientId, PositionId, VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs

from tests.nautilus_oi_runtime_fixtures import (
    ACCOUNT_ID,
    NOW_NS,
    CommandRows,
    SignalRows,
    operator_intent,
    registered_oi_strategy,
    trade_signal,
)
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.strategy import (
    RecoveredExecutionSeed,
    RecoveredProtectionSeed,
    RuntimeReconciliationSnapshot,
    deterministic_client_order_id,
)


def _accepted(context: SimpleNamespace, order: object, *, position_id: PositionId | None = None) -> object:
    context.cache.add_order(order, position_id=position_id, client_id=ClientId("BINANCE"))
    order.apply(TestEventStubs.order_submitted(order, account_id=ACCOUNT_ID, ts_event=NOW_NS))
    context.cache.update_order(order)
    event = TestEventStubs.order_accepted(
        order,
        account_id=ACCOUNT_ID,
        venue_order_id=VenueOrderId(f"venue-{order.client_order_id.value}"),
        ts_event=NOW_NS + 1,
    )
    order.apply(event)
    context.cache.update_order(order)
    return event


def _open_position(context: SimpleNamespace, *, quantity: str = "0.05") -> tuple[object, PositionId]:
    entry = context.strategy.submitted[0][0]
    position_id = PositionId("BTCUSDT-PERP.BINANCE-OI-RUNTIME")
    context.strategy.on_position_opened(
        SimpleNamespace(
            instrument_id=context.instrument.id,
            account_id=ACCOUNT_ID,
            strategy_id=context.strategy.id,
            opening_order_id=entry.client_order_id,
            side=PositionSide.LONG,
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal(quantity)),
            avg_px_open=10_000.0,
            ts_opened=NOW_NS + 2,
        )
    )
    return entry, position_id


def test_signal_submits_one_native_entry_and_emits_unique_disposition() -> None:
    signal = trade_signal()
    context = registered_oi_strategy(values=(signal,))

    context.strategy.on_timer(None)
    context.strategy.on_timer(None)

    assert len(context.strategy.submitted) == 1
    order = context.strategy.submitted[0][0]
    assert order.order_type == OrderType.MARKET
    assert order.side == OrderSide.BUY
    assert order.is_reduce_only is False
    assert order.quantity.as_decimal() == Decimal("0.049")
    assert order.client_order_id.value.startswith("tf")
    assert context.signals.pending_ids == {signal.signal_id}
    assert context.audit.queued_count == 2


def test_short_signal_uses_same_strategy_and_opposite_native_sides() -> None:
    signal = trade_signal(signal_id="7" * 64).model_copy(update={"direction": "short"})
    context = registered_oi_strategy(values=(signal,))
    context.strategy.on_timer(None)
    entry = context.strategy.submitted[0][0]
    position_id = PositionId("BTCUSDT-PERP.BINANCE-OI-SHORT")
    context.strategy.on_position_opened(
        SimpleNamespace(
            instrument_id=context.instrument.id,
            account_id=ACCOUNT_ID,
            strategy_id=context.strategy.id,
            opening_order_id=entry.client_order_id,
            side=PositionSide.SHORT,
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal("0.05")),
            avg_px_open=10_000.0,
            ts_opened=NOW_NS + 2,
        )
    )

    stop = context.strategy.submitted[1][0]
    assert entry.side == OrderSide.SELL
    assert stop.side == OrderSide.BUY
    assert stop.is_reduce_only is True


def test_expired_duplicate_and_entry_gate_rejections_never_submit() -> None:
    expired = registered_oi_strategy(values=(trade_signal(expires_at_ns=NOW_NS),))
    expired.strategy.on_timer(None)
    assert expired.strategy.submitted == []

    singleton = [False]
    blocked = registered_oi_strategy(values=(trade_signal(signal_id="5" * 64),), singleton=singleton)
    blocked.strategy.on_timer(None)
    assert blocked.strategy.submitted == []
    assert blocked.strategy.readiness().reason == "singleton_unavailable"


def test_pause_resume_and_halt_are_distinct_and_never_bypass_entry_risk() -> None:
    pause = operator_intent(command_id="5" * 64)
    paused = registered_oi_strategy(values=(trade_signal(),), commands=(pause,))
    paused.strategy.on_timer(None)
    assert paused.strategy.submitted == []
    assert paused.strategy.control_state().entries_paused is True
    assert paused.strategy.control_state().emergency_halted is False

    resume = operator_intent(command_id="6" * 64, action="resume_entries")
    resumed = registered_oi_strategy(values=(trade_signal(),), commands=(pause, resume))
    resumed.strategy.on_timer(None)
    assert len(resumed.strategy.submitted) == 1
    assert resumed.strategy.control_state().entries_paused is False
    assert resumed.strategy.control_state().emergency_halted is False

    halt = operator_intent(command_id="7" * 64, action="emergency_halt", scope="account")
    halted = registered_oi_strategy(values=(trade_signal(),), commands=(halt, resume))
    halted.strategy.on_timer(None)
    assert halted.strategy.submitted == []
    assert halted.strategy.control_state().entries_paused is True
    assert halted.strategy.control_state().emergency_halted is True
    observations = halted.audit.flush_once(lambda _values: None)
    resume_observation = next(value for value in observations if value.command_id == resume.command_id)
    assert resume_observation.summary == {
        "action": "resume_entries",
        "disposition": "rejected",
        "reason": "emergency_halt_sticky",
    }


def test_signal_waits_until_the_persisted_command_backlog_is_drained() -> None:
    commands = tuple(
        operator_intent(
            command_id=f"{index:064x}",
            action="pause_entries" if index == 17 else "resume_entries",
        )
        for index in range(1, 18)
    )
    context = registered_oi_strategy(values=(trade_signal(),), commands=commands)

    context.strategy.on_timer(None)

    assert context.signals.queued_command_count == 1
    assert context.strategy.submitted == []

    context.strategy.on_timer(None)

    assert context.signals.queued_command_count == 0
    assert context.strategy.control_state().entries_paused is True
    assert context.strategy.submitted == []


def test_signal_full_queue_yields_to_a_later_durable_pause_scan() -> None:
    client = ExecutionSignalClient(
        runtime_profile_id="oi-paper-profile",
        execution_strategy="oi_nautilus_v1",
        max_count=2,
    )
    first = trade_signal(signal_id="1" * 64)
    second = trade_signal(signal_id="2" * 64)
    assert client.poll_once(SignalRows(first, second)) == 2
    pause = operator_intent(command_id="3" * 64)
    assert client.poll_commands_once(CommandRows(pause)) == 1
    context = registered_oi_strategy(signal_client=client)

    context.strategy.on_timer(None)

    assert context.strategy.control_state().entries_paused is True
    assert context.signals.queued_command_count == 0
    assert context.signals.command_scan_complete is False
    assert context.strategy.submitted == []

    assert context.signals.poll_commands_once(CommandRows()) == 0
    context.strategy.on_timer(None)

    assert context.signals.command_scan_complete is True
    assert context.strategy.submitted == []


def test_manual_entry_uses_the_same_sizing_risk_and_native_order_path() -> None:
    manual = operator_intent(
        action="manual_entry",
        scope="market",
        market_key="crypto:perp:BTC:USDT",
        direction="long",
    )
    context = registered_oi_strategy(commands=(manual,))

    context.strategy.on_timer(None)

    observations = context.audit.flush_once(lambda _values: None)
    order = context.strategy.submitted[0][0]
    assert order.quantity.as_decimal() == Decimal("0.049")
    assert order.order_type == OrderType.MARKET
    assert order.is_reduce_only is False
    assert all(value.signal_id is None for value in observations)
    assert all(value.command_id == manual.command_id for value in observations)
    disposition = next(value for value in observations if value.normalized_kind == "control_disposition")
    assert disposition.summary == {
        "action": "manual_entry",
        "disposition": "accepted",
        "reason": "accepted",
    }


def test_manual_entry_replay_queries_the_same_client_id_without_resubmit() -> None:
    manual = operator_intent(
        action="manual_entry",
        scope="market",
        market_key="crypto:perp:BTC:USDT",
        direction="long",
    )
    first = registered_oi_strategy(commands=(manual,))
    first.strategy.on_timer(None)
    entry = first.strategy.submitted[0][0]
    _accepted(first, entry)

    restarted = registered_oi_strategy(commands=(manual,))
    restarted.cache.add_order(entry, client_id=ClientId("BINANCE"))
    restarted.strategy.on_timer(None)

    assert restarted.strategy.submitted == []
    assert restarted.strategy.queried == [entry]
    observations = restarted.audit.flush_once(lambda _values: None)
    assert all(value.command_id == manual.command_id for value in observations)
    assert all(value.signal_id is None for value in observations)


def test_flatten_is_runtime_accepted_but_not_complete_until_fresh_flat_reconciliation() -> None:
    flatten = operator_intent(
        command_id="8" * 64,
        action="flatten",
        scope="account",
        requested_at_ns=NOW_NS - 1,
    )
    context = registered_oi_strategy(commands=(flatten,))

    context.strategy.on_timer(None)

    accepted = context.audit.flush_once(lambda _values: None)
    assert len(accepted) == 1
    assert accepted[0].normalized_kind == "readiness"
    assert accepted[0].summary == {"action": "flatten", "control_stage": "runtime_accepted"}
    assert context.strategy.control_state().flatten_pending == (flatten.command_id,)
    assert context.signals.pending_command_ids == {flatten.command_id}

    assert context.strategy.reconcile_runtime(
        RuntimeReconciliationSnapshot(
            runtime_profile_id=context.profile.profile_id,
            account_observed_at_ns=NOW_NS + 1,
            reconciliation_observed_at_ns=NOW_NS + 2,
        )
    )
    completed = context.audit.flush_once(lambda _values: None)
    assert len(completed) == 1
    assert completed[0].normalized_kind == "control_disposition"
    assert completed[0].summary == {"disposition": "completed", "reason": "binance_account_flat"}
    assert context.strategy.control_state().flatten_pending == ()


def test_flatten_submits_only_owned_reduce_only_exit_and_does_not_equal_halt() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)
    flatten = operator_intent(command_id="9" * 64, action="flatten", scope="account")
    context.signals.poll_commands_once(CommandRows(flatten))

    context.strategy.on_timer(None)

    exit_order = context.strategy.submitted[-1][0]
    assert context.strategy.submitted[-1][1] == position_id
    assert exit_order.order_type == OrderType.MARKET
    assert exit_order.is_reduce_only is True
    assert context.strategy.canceled == [context.strategy.submitted[0][0]]
    assert context.strategy.control_state().entries_paused is True
    assert context.strategy.control_state().emergency_halted is False


def test_restart_replay_queries_same_deterministic_client_id_without_resubmit() -> None:
    signal = trade_signal()
    first = registered_oi_strategy(values=(signal,))
    first.strategy.on_timer(None)
    entry = first.strategy.submitted[0][0]
    _accepted(first, entry)

    restarted = registered_oi_strategy(values=(signal,))
    restarted.cache.add_order(entry, client_id=ClientId("BINANCE"))
    restarted.strategy.on_timer(None)

    assert restarted.strategy.submitted == []
    assert restarted.strategy.queried == [entry]
    assert entry.client_order_id == first.strategy.submitted[0][0].client_order_id


def test_unresolved_signal_replay_rejects_wrong_cached_entry_shape() -> None:
    signal = trade_signal()
    first = registered_oi_strategy()
    entry_id = deterministic_client_order_id(
        namespace=first.profile.client_order_namespace,
        profile_id=first.profile.profile_id,
        signal_id=signal.signal_id,
        leg="entry",
    )
    wrong_entry = first.strategy.order_factory.market(
        instrument_id=first.instrument.id,
        order_side=OrderSide.SELL,
        quantity=first.instrument.make_qty(Decimal("0.01")),
        client_order_id=entry_id,
    )
    _accepted(first, wrong_entry)
    canceled = TestEventStubs.order_canceled(wrong_entry, account_id=ACCOUNT_ID, ts_event=NOW_NS + 2)
    wrong_entry.apply(canceled)
    first.cache.update_order(wrong_entry)
    restarted = registered_oi_strategy(values=(signal,), cache=first.cache)

    restarted.strategy.on_timer(None)

    assert restarted.strategy.submitted == []
    assert restarted.strategy.queried == []
    assert restarted.strategy.readiness().unexpected_exposure is True


def test_expired_unresolved_signal_reclaims_cached_filled_position_and_flattens() -> None:
    signal = trade_signal(expires_at_ns=NOW_NS)
    first = registered_oi_strategy()
    entry_id = deterministic_client_order_id(
        namespace=first.profile.client_order_namespace,
        profile_id=first.profile.profile_id,
        signal_id=signal.signal_id,
        leg="entry",
    )
    entry = first.strategy.order_factory.market(
        instrument_id=first.instrument.id,
        order_side=OrderSide.BUY,
        quantity=first.instrument.make_qty(Decimal("0.01")),
        client_order_id=entry_id,
    )
    _accepted(first, entry)
    position_id = PositionId("BTCUSDT-PERP.BINANCE-OI-EXPIRED-RECOVERY")
    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=first.instrument,
        strategy_id=first.strategy.id,
        account_id=ACCOUNT_ID,
        venue_order_id=entry.venue_order_id,
        position_id=position_id,
        last_qty=first.instrument.make_qty(Decimal("0.01")),
        last_px=first.instrument.make_price(Decimal("10000")),
        commission=Money(0, first.instrument.quote_currency),
        ts_event=NOW_NS + 2,
    )
    entry.apply(fill)
    first.cache.update_order(entry)
    first.cache.add_position(Position(first.instrument, fill), OmsType.NETTING)
    restarted = registered_oi_strategy(values=(signal,), cache=first.cache)

    restarted.strategy.on_timer(None)

    flatten = restarted.strategy.submitted[0][0]
    assert restarted.strategy.queried == [entry, flatten]
    assert flatten.order_type == OrderType.MARKET
    assert flatten.is_reduce_only is True
    assert restarted.strategy.readiness().unexpected_exposure is True


def test_closed_replayed_entry_does_not_keep_instrument_busy() -> None:
    first_signal = trade_signal()
    next_signal = trade_signal(signal_id="9" * 64)
    first = registered_oi_strategy()
    entry_id = deterministic_client_order_id(
        namespace=first.profile.client_order_namespace,
        profile_id=first.profile.profile_id,
        signal_id=first_signal.signal_id,
        leg="entry",
    )
    entry = first.strategy.order_factory.market(
        instrument_id=first.instrument.id,
        order_side=OrderSide.BUY,
        quantity=first.instrument.make_qty(Decimal("0.01")),
        client_order_id=entry_id,
    )
    _accepted(first, entry)
    canceled = TestEventStubs.order_canceled(entry, account_id=ACCOUNT_ID, ts_event=NOW_NS + 2)
    entry.apply(canceled)
    first.cache.update_order(entry)
    restarted = registered_oi_strategy(values=(first_signal,), cache=first.cache)
    restarted.strategy.on_timer(None)

    assert restarted.signals.poll_once(SignalRows(next_signal)) == 1
    restarted.strategy.on_timer(None)

    assert len(restarted.strategy.submitted) == 1
    assert restarted.strategy.submitted[0][0].client_order_id != entry.client_order_id


def test_restart_reconciliation_rejects_wrong_entry_shape() -> None:
    signal = trade_signal()
    first = registered_oi_strategy()
    entry_id = deterministic_client_order_id(
        namespace=first.profile.client_order_namespace,
        profile_id=first.profile.profile_id,
        signal_id=signal.signal_id,
        leg="entry",
    )
    wrong_entry = first.strategy.order_factory.market(
        instrument_id=first.instrument.id,
        order_side=OrderSide.SELL,
        quantity=first.instrument.make_qty(Decimal("0.01")),
        client_order_id=entry_id,
    )
    _accepted(first, wrong_entry)
    snapshot = RuntimeReconciliationSnapshot(
        runtime_profile_id=first.profile.profile_id,
        account_observed_at_ns=NOW_NS,
        reconciliation_observed_at_ns=NOW_NS,
        executions=(RecoveredExecutionSeed(signal=signal, entry_client_order_id=entry_id),),
    )
    restarted = registered_oi_strategy(
        cache=first.cache,
        startup_reconciliation=snapshot,
        mark_reconciled=False,
    )

    restarted.strategy.on_start()

    readiness = restarted.strategy.readiness()
    assert readiness.ready is False
    assert readiness.unexpected_exposure is True


@pytest.mark.parametrize(
    ("replacement_side", "replacement_accepted", "expected_ready"),
    [
        (OrderSide.SELL, True, True),
        (OrderSide.SELL, False, True),
        (OrderSide.BUY, True, False),
    ],
)
def test_restart_reconciliation_validates_stop_shape_and_reclaims_overlap(
    replacement_side: OrderSide,
    replacement_accepted: bool,
    expected_ready: bool,
) -> None:
    signal = trade_signal()
    first = registered_oi_strategy(values=(signal,))
    first.strategy.on_timer(None)
    entry = first.strategy.submitted[0][0]
    _accepted(first, entry)
    position_id = PositionId("BTCUSDT-PERP.BINANCE-OI-RECOVERY")
    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=first.instrument,
        strategy_id=first.strategy.id,
        account_id=ACCOUNT_ID,
        venue_order_id=entry.venue_order_id,
        position_id=position_id,
        last_qty=first.instrument.make_qty(Decimal("0.05")),
        last_px=first.instrument.make_price(Decimal("10000")),
        commission=Money(0, first.instrument.quote_currency),
        ts_event=NOW_NS + 2,
    )
    entry.apply(fill)
    first.cache.update_order(entry)
    first.cache.add_position(Position(first.instrument, fill), OmsType.NETTING)
    first.strategy.on_position_opened(
        SimpleNamespace(
            instrument_id=first.instrument.id,
            account_id=ACCOUNT_ID,
            strategy_id=first.strategy.id,
            opening_order_id=entry.client_order_id,
            side=PositionSide.LONG,
            position_id=position_id,
            quantity=first.instrument.make_qty(Decimal("0.05")),
            avg_px_open=10_000.0,
            ts_opened=NOW_NS + 2,
        )
    )
    old_stop_id = deterministic_client_order_id(
        namespace=first.profile.client_order_namespace,
        profile_id=first.profile.profile_id,
        signal_id=signal.signal_id,
        leg="protection:1:0.04",
    )
    old_stop = first.strategy.order_factory.stop_market(
        instrument_id=first.instrument.id,
        order_side=OrderSide.SELL,
        quantity=first.instrument.make_qty(Decimal("0.04")),
        trigger_price=first.instrument.make_price(Decimal("9850")),
        trigger_type=TriggerType.LAST_PRICE,
        reduce_only=True,
        client_order_id=old_stop_id,
    )
    _accepted(first, old_stop, position_id=position_id)
    replacement_id = deterministic_client_order_id(
        namespace=first.profile.client_order_namespace,
        profile_id=first.profile.profile_id,
        signal_id=signal.signal_id,
        leg="protection:2:0.05",
    )
    replacement = first.strategy.order_factory.stop_market(
        instrument_id=first.instrument.id,
        order_side=replacement_side,
        quantity=first.instrument.make_qty(Decimal("0.05")),
        trigger_price=first.instrument.make_price(Decimal("9800")),
        trigger_type=TriggerType.LAST_PRICE,
        reduce_only=True,
        client_order_id=replacement_id,
    )
    if replacement_accepted:
        _accepted(first, replacement, position_id=position_id)
    else:
        first.cache.add_order(replacement, position_id=position_id, client_id=ClientId("BINANCE"))
        replacement.apply(TestEventStubs.order_submitted(replacement, account_id=ACCOUNT_ID, ts_event=NOW_NS + 2))
        first.cache.update_order(replacement)
    first.strategy.flatten_position(position_id)
    exit_order = first.strategy.submitted[2][0]
    first.cache.add_order(exit_order, position_id=position_id, client_id=ClientId("BINANCE"))
    exit_order.apply(TestEventStubs.order_submitted(exit_order, account_id=ACCOUNT_ID, ts_event=NOW_NS + 3))
    first.cache.update_order(exit_order)
    snapshot = RuntimeReconciliationSnapshot(
        runtime_profile_id=first.profile.profile_id,
        account_observed_at_ns=NOW_NS + 3,
        reconciliation_observed_at_ns=NOW_NS + 3,
        executions=(
            RecoveredExecutionSeed(
                signal=signal,
                entry_client_order_id=entry.client_order_id,
                position_id=position_id,
                protections=(
                    RecoveredProtectionSeed(
                        role="retiring" if replacement_accepted else "active",
                        client_order_id=old_stop.client_order_id,
                        quantity=Decimal("0.040"),
                        trigger_price=Decimal("9850"),
                        generation=1,
                    ),
                    RecoveredProtectionSeed(
                        role="active" if replacement_accepted else "pending",
                        client_order_id=replacement.client_order_id,
                        quantity=Decimal("0.050"),
                        trigger_price=Decimal("9800"),
                        generation=2,
                    ),
                ),
                exit_client_order_id=exit_order.client_order_id,
            ),
        ),
    )
    restarted = registered_oi_strategy(
        values=(signal,),
        cache=first.cache,
        startup_reconciliation=snapshot,
        mark_reconciled=False,
    )

    restarted.strategy.on_start()
    if not expected_ready:
        assert restarted.strategy.readiness().ready is False
        assert restarted.strategy.submitted == []
        return
    restarted.strategy.on_timer(None)
    restarted.strategy.flatten_position(position_id)

    assert restarted.strategy.readiness().ready is True
    assert restarted.strategy.submitted == []
    expected_queries = [exit_order, exit_order]
    if not replacement_accepted:
        expected_queries.insert(0, replacement)
    assert restarted.strategy.queried == expected_queries
    assert restarted.strategy.canceled == ([old_stop] if replacement_accepted else [])
    written: list[object] = []
    restarted.audit.flush_once(written.extend)
    assert [value.normalized_kind for value in written].count("signal_disposition") == 1


def test_continuous_reconciliation_refreshes_clock_before_stale_entry_check() -> None:
    refreshed_at_ns = NOW_NS + 11_000_000_000
    snapshots = [
        RuntimeReconciliationSnapshot(
            runtime_profile_id=registered_oi_strategy().profile.profile_id,
            account_observed_at_ns=refreshed_at_ns,
            reconciliation_observed_at_ns=refreshed_at_ns,
        )
    ]
    context = registered_oi_strategy(
        values=(trade_signal(signal_id="8" * 64),),
        continuous_reconciliation=lambda: snapshots.pop(0) if snapshots else None,
    )
    context.clock.set_time(refreshed_at_ns)
    context.cache.add_quote_tick(
        TestDataStubs.quote_tick(
            instrument=context.instrument,
            bid_price=9_999,
            ask_price=10_000,
            ts_event=refreshed_at_ns,
            ts_init=refreshed_at_ns,
        )
    )

    context.strategy.on_timer(None)

    assert len(context.strategy.submitted) == 1


def test_utc_day_rollover_blocks_until_background_updates_durable_baseline() -> None:
    context = registered_oi_strategy()
    rollover_ns = NOW_NS + 86_400_000_000_000
    context.clock.set_time(rollover_ns)

    assert context.strategy.readiness().reason == "day_start_baseline_missing"

    utc_day = datetime.fromtimestamp(rollover_ns // 1_000_000_000, tz=UTC).date().isoformat()
    context.strategy.update_day_start(
        DayStartBaseline(
            utc_day=utc_day,
            equity_usd=Decimal("975"),
            recorded_at_ns=rollover_ns,
            event_id="5" * 64,
        )
    )

    assert context.strategy.readiness().ready is True


@pytest.mark.parametrize(
    "callback",
    ["on_order_rejected", "on_order_denied", "on_order_expired", "on_order_canceled"],
)
def test_terminal_exit_failure_advances_deterministic_generation_and_retries(callback: str) -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)
    context.strategy.flatten_position(position_id)
    first_exit = context.strategy.submitted[2][0]

    getattr(context.strategy, callback)(
        SimpleNamespace(
            client_order_id=first_exit.client_order_id,
            reason="known terminal failure",
            ts_event=NOW_NS + 4,
        )
    )
    context.strategy.on_timer(None)

    retried_exit = context.strategy.submitted[3][0]
    assert retried_exit.is_reduce_only is True
    assert retried_exit.client_order_id != first_exit.client_order_id

    getattr(context.strategy, callback)(
        SimpleNamespace(
            client_order_id=retried_exit.client_order_id,
            reason="persistent terminal failure",
            ts_event=NOW_NS + 5,
        )
    )
    context.strategy.on_timer(None)

    assert len(context.strategy.submitted) == 4


def test_accepted_entry_does_not_remain_on_periodic_ambiguity_query_path() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    entry = context.strategy.submitted[0][0]
    context.strategy.on_order_accepted(
        SimpleNamespace(
            client_order_id=entry.client_order_id,
            venue_order_id=VenueOrderId("venue-entry"),
            ts_event=NOW_NS + 1,
        )
    )
    context.clock.set_time(NOW_NS + 6_000_000_000)

    context.strategy.on_timer(None)

    assert context.strategy.queried == []


def test_wide_spread_disposes_signal_without_entry() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.clock.set_time(NOW_NS + 1)
    context.cache.add_quote_tick(
        TestDataStubs.quote_tick(
            instrument=context.instrument,
            bid_price=9_900,
            ask_price=10_000,
            ts_event=NOW_NS + 1,
            ts_init=NOW_NS + 1,
        )
    )

    context.strategy.on_timer(None)

    written: list[object] = []
    context.audit.flush_once(written.extend)
    dispositions = [value for value in written if value.normalized_kind == "signal_disposition"]
    assert context.strategy.submitted == []
    assert dispositions[0].summary["disposition"] == "spread_limit"


def test_partial_position_change_cancel_replaces_explicit_reduce_only_stop() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)
    first_stop = context.strategy.submitted[1][0]

    assert first_stop.order_type == OrderType.STOP_MARKET
    assert first_stop.is_reduce_only is True
    assert first_stop.quantity.as_decimal() == Decimal("0.05")
    context.strategy.on_order_accepted(_accepted(context, first_stop))
    context.strategy.on_position_changed(
        SimpleNamespace(
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal("0.08")),
            avg_px_open=10_000.0,
            ts_event=NOW_NS + 3,
        )
    )
    replacement = context.strategy.submitted[2][0]
    assert context.strategy.canceled == []
    assert replacement.order_type == OrderType.STOP_MARKET
    assert replacement.is_reduce_only is True
    assert replacement.quantity.as_decimal() == Decimal("0.08")
    assert replacement.client_order_id != first_stop.client_order_id

    context.strategy.on_order_accepted(_accepted(context, replacement))
    assert context.strategy.canceled == [first_stop]


def test_cached_same_id_invalid_protection_flattens_instead_of_replaying() -> None:
    signal = trade_signal()
    context = registered_oi_strategy(values=(signal,))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)
    first_stop = context.strategy.submitted[1][0]
    context.strategy.on_order_accepted(_accepted(context, first_stop, position_id=position_id))
    cached_id = deterministic_client_order_id(
        namespace=context.profile.client_order_namespace,
        profile_id=context.profile.profile_id,
        signal_id=signal.signal_id,
        leg="protection:2:0.08",
    )
    cached = context.strategy.order_factory.stop_market(
        instrument_id=context.instrument.id,
        order_side=OrderSide.BUY,
        quantity=context.instrument.make_qty(Decimal("0.08")),
        trigger_price=context.instrument.make_price(Decimal("9800")),
        trigger_type=TriggerType.LAST_PRICE,
        reduce_only=True,
        client_order_id=cached_id,
    )
    _accepted(context, cached, position_id=position_id)

    context.strategy.on_position_changed(
        SimpleNamespace(
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal("0.08")),
            avg_px_open=10_000.0,
            ts_event=NOW_NS + 3,
        )
    )

    assert context.strategy.queried == [cached]
    exit_order = context.strategy.submitted[2][0]
    assert exit_order.order_type == OrderType.MARKET
    assert exit_order.is_reduce_only is True


def test_native_partial_fill_is_normalized_without_callback_io() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    entry = context.strategy.submitted[0][0]
    _accepted(context, entry)
    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=context.instrument,
        strategy_id=context.strategy.id,
        account_id=ACCOUNT_ID,
        venue_order_id=entry.venue_order_id,
        position_id=PositionId("BTCUSDT-PERP.BINANCE-OI-RUNTIME"),
        last_qty=context.instrument.make_qty(Decimal("0.02")),
        last_px=context.instrument.make_price(Decimal("10000")),
        commission=Money(0, context.instrument.quote_currency),
        ts_event=NOW_NS + 2,
    )

    context.strategy.on_order_filled(fill)
    written: list[object] = []
    context.audit.flush_once(written.extend)

    fills = [value for value in written if value.normalized_kind == "fill"]
    assert len(fills) == 1
    assert fills[0].summary == {
        "leg": "entry",
        "last_quantity": "0.020",
        "last_price": "10000.0",
    }


def test_repeated_exit_query_observation_is_idempotent() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)
    context.strategy.flatten_position(position_id)
    context.clock.set_time(NOW_NS + 3)
    context.strategy.flatten_position(position_id)
    context.clock.set_time(NOW_NS + 4)
    context.strategy.flatten_position(position_id)

    written: list[object] = []
    context.audit.flush_once(written.extend)
    replayed = [
        value
        for value in written
        if value.normalized_kind == "order" and value.summary.get("status") == "replayed_query_first"
    ]

    assert len(replayed) == 1


def test_revisited_position_quantity_has_distinct_audit_identity() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)
    for offset, quantity in enumerate(("0.04", "0.05", "0.04"), start=3):
        context.strategy.on_position_changed(
            SimpleNamespace(
                position_id=position_id,
                quantity=context.instrument.make_qty(Decimal(quantity)),
                avg_px_open=10_000.0,
                ts_event=NOW_NS + offset,
            )
        )

    written: list[object] = []
    context.audit.flush_once(written.extend)
    changes = [
        value for value in written if value.normalized_kind == "position" and value.summary["status"] == "changed"
    ]

    assert len(changes) == 3
    assert len({value.event_id for value in changes}) == 3


@pytest.mark.parametrize("reason", ["503 unavailable", "-1007 timeout", "response unknown"])
def test_ambiguous_provider_outcome_is_query_first_and_never_changes_id(reason: str) -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    entry = context.strategy.submitted[0][0]

    context.strategy.on_order_rejected(
        SimpleNamespace(client_order_id=entry.client_order_id, reason=reason, ts_event=NOW_NS + 2)
    )

    assert len(context.strategy.submitted) == 1
    assert context.strategy.queried == [entry]


def test_audit_failure_blocks_new_entries_but_not_protection_or_flatten() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)
    first_stop = context.strategy.submitted[1][0]

    def fail_writer(_values: object) -> None:
        raise RuntimeError("postgres-down")

    with pytest.raises(RuntimeError, match="postgres-down"):
        context.audit.flush_once(fail_writer)
    assert context.audit.healthy is False
    context.strategy.on_order_accepted(_accepted(context, first_stop))

    context.strategy.on_position_changed(
        SimpleNamespace(
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal("0.06")),
            avg_px_open=10_000.0,
            ts_event=NOW_NS + 5,
        )
    )
    replacement = context.strategy.submitted[2][0]
    context.strategy.on_order_accepted(_accepted(context, replacement))
    context.strategy.flatten_position(position_id)

    flatten = context.strategy.submitted[3][0]
    assert replacement.order_type == OrderType.STOP_MARKET
    assert replacement.is_reduce_only is True
    assert flatten.order_type == OrderType.MARKET
    assert flatten.is_reduce_only is True


def test_canceled_pending_protection_flattens_instead_of_opening_unprotected() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, _ = _open_position(context)
    pending_stop = context.strategy.submitted[1][0]

    context.strategy.on_order_canceled(
        SimpleNamespace(client_order_id=pending_stop.client_order_id, ts_event=NOW_NS + 4)
    )

    flatten = context.strategy.submitted[2][0]
    assert flatten.order_type == OrderType.MARKET
    assert flatten.is_reduce_only is True


def test_periodic_cache_check_flattens_when_active_protection_disappears() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)
    stop = context.strategy.submitted[1][0]
    context.strategy.on_order_accepted(_accepted(context, stop, position_id=position_id))
    canceled = TestEventStubs.order_canceled(stop, account_id=ACCOUNT_ID, ts_event=NOW_NS + 4)
    stop.apply(canceled)
    context.cache.update_order(stop)

    context.strategy.on_timer(None)

    flatten = context.strategy.submitted[2][0]
    assert flatten.order_type == OrderType.MARKET
    assert flatten.is_reduce_only is True
    assert context.strategy.readiness().unexpected_exposure is True


def test_repeated_flatten_queries_same_exit_instead_of_submitting_again() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_timer(None)
    _, position_id = _open_position(context)

    context.strategy.flatten_position(position_id)
    exit_order = context.strategy.submitted[2][0]
    context.strategy.flatten_position(position_id)

    assert len(context.strategy.submitted) == 3
    assert context.strategy.queried == [exit_order]


def test_cached_exit_with_wrong_shape_is_never_reclaimed() -> None:
    signal = trade_signal()
    context = registered_oi_strategy(values=(signal,))
    context.strategy.on_timer(None)
    entry = context.strategy.submitted[0][0]
    _accepted(context, entry)
    position_id = PositionId("BTCUSDT-PERP.BINANCE-OI-BAD-EXIT")
    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=context.instrument,
        strategy_id=context.strategy.id,
        account_id=ACCOUNT_ID,
        venue_order_id=entry.venue_order_id,
        position_id=position_id,
        last_qty=context.instrument.make_qty(Decimal("0.05")),
        last_px=context.instrument.make_price(Decimal("10000")),
        commission=Money(0, context.instrument.quote_currency),
        ts_event=NOW_NS + 2,
    )
    entry.apply(fill)
    context.cache.update_order(entry)
    context.cache.add_position(Position(context.instrument, fill), OmsType.NETTING)
    context.strategy.on_position_opened(
        SimpleNamespace(
            instrument_id=context.instrument.id,
            account_id=ACCOUNT_ID,
            strategy_id=context.strategy.id,
            opening_order_id=entry.client_order_id,
            side=PositionSide.LONG,
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal("0.05")),
            avg_px_open=10_000.0,
            ts_opened=NOW_NS + 2,
        )
    )
    bad_exit_id = deterministic_client_order_id(
        namespace=context.profile.client_order_namespace,
        profile_id=context.profile.profile_id,
        signal_id=signal.signal_id,
        leg="exit",
    )
    bad_exit = context.strategy.order_factory.market(
        instrument_id=context.instrument.id,
        order_side=OrderSide.BUY,
        quantity=context.instrument.make_qty(Decimal("0.05")),
        reduce_only=True,
        client_order_id=bad_exit_id,
    )
    _accepted(context, bad_exit, position_id=position_id)

    context.strategy.flatten_position(position_id)

    assert len(context.strategy.submitted) == 2
    assert context.strategy.queried == []
    assert context.strategy.readiness().unexpected_exposure is True


def test_failed_audit_writer_rejects_later_signal_before_any_new_exposure() -> None:
    profile = registered_oi_strategy().profile
    factory = ObservationFactory(profile.profile_id, profile.runtime_release, "oi_nautilus_v1")
    audit = AuditSink(factory=factory)
    audit.offer(
        factory.create(
            normalized_kind="readiness",
            occurred_at_ns=NOW_NS,
            observed_at_ns=NOW_NS,
            summary={"ready": True},
            payload={"ready": True},
        )
    )

    def fail_writer(_values: object) -> None:
        raise RuntimeError("postgres-down")

    with pytest.raises(RuntimeError, match="postgres-down"):
        audit.flush_once(fail_writer)
    context = registered_oi_strategy(values=(trade_signal(signal_id="6" * 64),), audit=audit)

    context.strategy.on_timer(None)

    assert context.strategy.submitted == []
    assert context.audit.failure_reason == "audit_append_failed"


def test_callback_module_has_no_postgres_or_telegram_io() -> None:
    import inspect

    from tracefold.integrations.nautilus.oi_runtime import strategy

    source = inspect.getsource(strategy).lower()
    assert "psycopg" not in source
    assert "repositories" not in source
    assert "telegram" not in source
