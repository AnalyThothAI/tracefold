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


def _execution(mode: str = "paper") -> SimpleNamespace:
    return SimpleNamespace(mode=mode, account_slot="binance_usdm_primary")


def _account_snapshot(*, complete: bool = True, truncated: bool = False) -> ExecutionAccountSnapshot:
    return ExecutionAccountSnapshot(
        observed_at_ns=9_000_000_000,
        market_observed_at_ns=None,
        equity_usd="1000",
        day_start_equity_usd="1000",
        daily_drawdown_usd="0",
        daily_drawdown_bps=0,
        aggregate_risk_usd="0",
        positions=(),
        orders=(),
        open_orders_count=0,
        inflight_orders_count=0,
        unknown_orders_count=0,
        complete=complete,
        truncated=truncated,
    )


def _state(*, heartbeat_at_ns: int = 10_000_000_000) -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        mode="paper",
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="a" * 64,
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        runtime_revision="b" * 40,
        image_digest="sha256:" + "c" * 64,
        credential_fingerprint="d" * 64,
        lifecycle_state="running",
        alive=True,
        execution_safe=True,
        entries_armed=True,
        startup_reconciled=True,
        unexpected_exposure=False,
        account_flat=True,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        reconciliation_observed_at_ns=9_000_000_000,
        heartbeat_at_ns=heartbeat_at_ns,
        entry_block_reason=None,
        started_at_ns=8_000_000_000,
        updated_at_ns=heartbeat_at_ns,
        account_snapshot=_account_snapshot(),
    )


def _control(*, entries_paused: bool = False) -> ExecutionRuntimeControlState:
    return ExecutionRuntimeControlState(
        account_slot="binance_usdm_primary",
        entries_paused=entries_paused,
        emergency_halted=False,
        last_command_seq=1,
        last_command_id="e" * 64,
        updated_at_ns=9_000_000_000,
    )


def test_disabled_execution_never_projects_a_stale_runtime_as_ready() -> None:
    projection = execution_readiness_projection(_execution("disabled"), _state(), _control(), now_ns=10_000_000_000)

    assert projection["alive"] is False
    assert projection["execution_safe"] is False
    assert projection["entries_armed"] is False
    assert projection["entry_block_reason"] == "disabled"
    assert projection["runtime_release"] is None


def test_active_execution_projects_exact_runtime_gates_and_identity() -> None:
    projection = execution_readiness_projection(_execution(), _state(), _control(), now_ns=10_000_000_000)

    assert projection["alive"] is True
    assert projection["execution_safe"] is True
    assert projection["entries_armed"] is True
    assert projection["entry_block_reason"] is None
    assert projection["runtime_release"] == "nautilus-1.231.0+oi-v1"
    assert projection["credential_fingerprint"] == "d" * 64
    assert projection["reconciliation_age_ms"] == 1_000
    assert projection["account_flat_proven"] is True


def test_flat_proof_requires_a_fresh_private_reconciliation() -> None:
    projection = execution_readiness_projection(
        _execution(),
        replace(
            _state(heartbeat_at_ns=20_000_000_000),
            reconciliation_observed_at_ns=9_000_000_000,
            updated_at_ns=20_000_000_000,
        ),
        _control(),
        now_ns=20_000_000_000,
    )

    assert projection["alive"] is True
    assert projection["execution_safe"] is True
    assert projection["account_flat"] is True
    assert projection["account_flat_proven"] is False
    assert projection["reconciliation_age_ms"] == 11_000
    assert projection["current_account"] is None


def test_current_account_requires_a_non_future_private_reconciliation() -> None:
    projection = execution_readiness_projection(
        _execution(),
        replace(_state(), reconciliation_observed_at_ns=11_000_000_000),
        _control(),
        now_ns=10_000_000_000,
    )

    assert projection["reconciliation_age_ms"] == 0
    assert projection["current_account"] is None


def test_flat_proof_requires_complete_non_truncated_current_account_facts() -> None:
    missing = execution_readiness_projection(
        _execution(),
        replace(_state(), account_snapshot=None),
        _control(),
        now_ns=10_000_000_000,
    )
    partial = execution_readiness_projection(
        _execution(),
        replace(_state(), account_snapshot=_account_snapshot(complete=False)),
        _control(),
        now_ns=10_000_000_000,
    )
    truncated = execution_readiness_projection(
        _execution(),
        replace(_state(), account_snapshot=_account_snapshot(truncated=True)),
        _control(),
        now_ns=10_000_000_000,
    )

    assert missing["account_flat_proven"] is False
    assert partial["account_flat_proven"] is False
    assert truncated["account_flat_proven"] is False


def test_flat_proof_requires_no_open_inflight_or_unknown_order_uncertainty() -> None:
    inflight_order = ExecutionAccountOrder(
        client_order_id="entry-query-pending",
        instrument_id="BTCUSDT-PERP.BINANCE",
        state="inflight",
        leg="entry",
        quantity="0.01",
        reduce_only=False,
        trigger_price=None,
        owned=True,
    )
    snapshots = (
        replace(_account_snapshot(), open_orders_count=1),
        replace(_account_snapshot(), orders=(inflight_order,), inflight_orders_count=1),
        replace(_account_snapshot(), unknown_orders_count=1),
    )

    for snapshot in snapshots:
        projection = execution_readiness_projection(
            _execution(),
            replace(_state(), account_snapshot=snapshot),
            _control(),
            now_ns=10_000_000_000,
        )
        assert projection["account_flat_proven"] is False


def test_current_account_projection_remains_a_distinct_read_model() -> None:
    snapshot = ExecutionAccountSnapshot(
        observed_at_ns=9_000_000_000,
        market_observed_at_ns=8_900_000_000,
        equity_usd="995",
        day_start_equity_usd="1000",
        daily_drawdown_usd="5",
        daily_drawdown_bps=50,
        aggregate_risk_usd="2.5",
        positions=(
            ExecutionAccountPosition(
                position_id="position-1",
                instrument_id="BTCUSDT-PERP.BINANCE",
                side="long",
                quantity="0.01",
                entry_price="100000",
                mark_price="100500",
                unrealized_pnl_usd="5",
                owned=True,
                protection_status="protected",
                protection_quantity="0.01",
                protection_trigger_price="99000",
                protection_full_coverage=True,
            ),
        ),
        orders=(),
        open_orders_count=1,
        inflight_orders_count=0,
        unknown_orders_count=0,
        complete=True,
    )
    projection = execution_readiness_projection(
        _execution(),
        replace(_state(), account_flat=False, positions_count=1, account_snapshot=snapshot),
        _control(),
        now_ns=10_000_000_000,
    )

    assert projection["account_flat_proven"] is False
    assert projection["current_account"]["equity_usd"] == "995"
    assert projection["current_account"]["positions"][0]["protection_full_coverage"] is True


def test_active_execution_fails_closed_on_identity_or_heartbeat_drift() -> None:
    mismatch = execution_readiness_projection(
        SimpleNamespace(mode="paper", account_slot="binance_usdm_secondary"),
        _state(),
        _control(),
        now_ns=10_000_000_000,
    )
    stale = execution_readiness_projection(_execution(), _state(), _control(), now_ns=15_000_000_001)

    assert mismatch["entries_armed"] is False
    assert mismatch["entry_block_reason"] == "runtime_identity_mismatch"
    assert stale["alive"] is False
    assert stale["execution_safe"] is False
    assert stale["entries_armed"] is False
    assert stale["entry_block_reason"] == "runtime_heartbeat_stale"


def test_transient_flat_and_unexpected_exposure_facts_remain_fail_closed() -> None:
    state = replace(
        _state(),
        execution_safe=False,
        entries_armed=False,
        unexpected_exposure=True,
        entry_block_reason="unexpected_exposure",
    )

    projection = execution_readiness_projection(_execution(), state, _control(), now_ns=10_000_000_000)

    assert projection["alive"] is True
    assert projection["execution_safe"] is False
    assert projection["entries_armed"] is False
    assert projection["entry_block_reason"] == "unexpected_exposure"
    assert projection["account_flat"] is True
    assert projection["unexpected_exposure"] is True

    paused_projection = execution_readiness_projection(
        _execution(), state, _control(entries_paused=True), now_ns=10_000_000_000
    )
    assert paused_projection["entry_block_reason"] == "unexpected_exposure"


def test_paused_entries_do_not_make_an_alive_safe_runtime_unready() -> None:
    projection = execution_readiness_projection(
        _execution(), _state(), _control(entries_paused=True), now_ns=10_000_000_000
    )

    assert projection["alive"] is True
    assert projection["execution_safe"] is True
    assert projection["entries_armed"] is False
    assert projection["entry_block_reason"] == "entries_paused"
