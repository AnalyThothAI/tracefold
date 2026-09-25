from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

from tracefold.app.execution_status import execution_readiness_projection
from tracefold.trading.storage.execution_stream import (
    ExecutionAccountOrder,
    ExecutionAccountPosition,
    ExecutionAccountSnapshot,
    ExecutionRuntimeControlState,
    ExecutionRuntimeState,
)


def _execution(*, enabled: bool = True, environment: str = "DEMO") -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        binance=SimpleNamespace(environment=environment),
        account_slot="binance_usdm_primary",
    )


def _account_snapshot(*, positions: tuple[ExecutionAccountPosition, ...] = ()) -> ExecutionAccountSnapshot:
    return ExecutionAccountSnapshot(
        observed_at_ns=9_000_000_000,
        equity_usd="1000",
        daily_drawdown_usd="0",
        daily_drawdown_bps=0,
        positions=positions,
        orders=(),
        open_orders_count=0,
        inflight_orders_count=0,
        complete=True,
    )


def _state(*, heartbeat_at_ns: int = 10_000_000_000) -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        connection="DEMO",
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        alive=True,
        entries_armed=True,
        unexpected_exposure=False,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        heartbeat_at_ns=heartbeat_at_ns,
        entry_block_reason=None,
        started_at_ns=min(heartbeat_at_ns, 8_000_000_000),
        updated_at_ns=heartbeat_at_ns,
        account_snapshot=_account_snapshot(),
        routes_count=487,
    )


def _control(*, entries_paused: bool = False) -> ExecutionRuntimeControlState:
    return ExecutionRuntimeControlState(
        account_slot="binance_usdm_primary",
        execution_namespace="tracefold:binance_usdm_primary:paper",
        entries_paused=entries_paused,
        emergency_halted=False,
        last_command_seq=1,
        last_command_id="e" * 64,
        updated_at_ns=9_000_000_000,
    )


def test_disabled_execution_never_projects_a_stale_runtime_as_ready() -> None:
    projection = execution_readiness_projection(_execution(enabled=False), _state(), _control(), now_ns=10_000_000_000)

    assert (projection["alive"], projection["entries_armed"]) == (False, False)
    assert projection["entry_block_reason"] == "disabled"
    assert projection["facts_expire_at_ms"] is None
    assert projection["current_account"] is None


def test_a_live_runtime_projects_exactly_its_own_answers_and_the_private_proof_facts_are_gone() -> None:
    projection = execution_readiness_projection(_execution(), _state(), _control(), now_ns=10_000_000_000)

    assert set(projection) == {
        "configured_connection",
        "connection",
        "connection_observed_at_ms",
        "account_slot",
        "alive",
        "entries_armed",
        "entry_block_reason",
        "entries_paused",
        "emergency_halted",
        "unexpected_exposure",
        "protection_status",
        "routes_count",
        "facts_expire_at_ms",
        "current_account",
    }
    assert (projection["alive"], projection["entries_armed"], projection["entry_block_reason"]) == (True, True, None)
    assert projection["routes_count"] == 487
    assert projection["facts_expire_at_ms"] == 15_000
    assert set(projection["current_account"]) == {
        "equity_usd",
        "daily_drawdown_usd",
        "daily_drawdown_bps",
        "positions",
        "orders",
        "open_orders_count",
        "inflight_orders_count",
        "complete",
    }


def test_a_stale_heartbeat_disarms_the_projection_whatever_the_row_says() -> None:
    projection = execution_readiness_projection(
        _execution(), _state(heartbeat_at_ns=1_000_000_000), _control(), now_ns=10_000_000_000
    )

    assert (projection["alive"], projection["entries_armed"]) == (False, False)
    assert projection["entry_block_reason"] == "runtime_heartbeat_stale"
    assert projection["facts_expire_at_ms"] == 6_000


def test_the_runtimes_own_block_reason_and_the_operator_switches_pass_straight_through() -> None:
    blocked = replace(_state(), entries_armed=False, entry_block_reason="unexpected_exposure", unexpected_exposure=True)
    projection = execution_readiness_projection(
        _execution(), blocked, _control(entries_paused=True), now_ns=10_000_000_000
    )

    assert projection["entry_block_reason"] == "unexpected_exposure"
    assert projection["unexpected_exposure"] is True
    assert projection["entries_paused"] is True


def test_a_row_from_another_account_is_not_this_runtime() -> None:
    other = replace(_state(), account_slot="other_connection")
    projection = execution_readiness_projection(_execution(), other, _control(), now_ns=10_000_000_000)
    assert projection["entry_block_reason"] == "runtime_identity_mismatch"
    assert projection["alive"] is False


def test_changed_config_does_not_relabel_the_running_connection() -> None:
    projection = execution_readiness_projection(
        _execution(environment="LIVE"), _state(), _control(), now_ns=10_000_000_000
    )
    assert projection["configured_connection"] == "LIVE"
    assert projection["connection"] == "DEMO"
    assert projection["alive"] is True


def test_the_account_snapshot_round_trips_positions_with_their_protection_and_orders_by_leg() -> None:
    position = ExecutionAccountPosition(
        position_id="BTCUSDT-PERP.BINANCE-OI-RUNTIME-F46",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="long",
        quantity="0.049",
        entry_price="10000",
        mark_price="10010",
        unrealized_pnl_usd="0.49",
        owned=True,
        stop_trigger_price="9900",
        take_profit_trigger_price=None,
    )
    order = ExecutionAccountOrder(
        client_order_id="tf" + "0" * 30,
        instrument_id="BTCUSDT-PERP.BINANCE",
        state="open",
        leg="stop",
        quantity="0.049",
        reduce_only=True,
        trigger_price="9900",
        owned=True,
    )
    snapshot = replace(_account_snapshot(positions=(position,)), orders=(order,), open_orders_count=1)

    assert ExecutionAccountSnapshot.from_payload(snapshot.payload()) == snapshot
    assert not position.protected
    assert replace(position, take_profit_trigger_price="10200").protected
