from __future__ import annotations

from types import SimpleNamespace

from tracefold.app.execution_status import execution_readiness_projection


def _execution(enabled: bool = True):
    return SimpleNamespace(
        enabled=enabled,
        binance=SimpleNamespace(environment="DEMO"),
        account_slot="binance_usdm_primary",
    )


def _state(**overrides):
    return {
        "last_full_reconcile_at_ns": 9_000_000_000,
        "account_snapshot": {"observed_at_ns": 9_000_000_000},
        "unexpected_exposure": False,
        **overrides,
    }


def _process(**overrides):
    return {"heartbeat_at_ms": 10_000, "lifecycle_state": "running", "fault_code": None, **overrides}


def _control(**overrides):
    return {"entries_paused": False, "emergency_halted": False, **overrides}


def test_disabled_execution_is_never_armed():
    result = execution_readiness_projection(
        _execution(False), _state(), _control(), now_ns=10_000_000_000, process=_process()
    )
    assert not result["alive"] and not result["entries_armed"]
    assert result["entry_block_reason"] == "disabled"


def test_fresh_heartbeat_and_reconciliation_arm_executor():
    result = execution_readiness_projection(
        _execution(), _state(), _control(), now_ns=10_000_000_000, process=_process()
    )
    assert result["connection"] == "DEMO"
    assert result["alive"] and result["entries_armed"]
    assert result["entry_block_reason"] is None
    assert result["facts_expire_at_ms"] == 15_000


def test_stale_or_unexpected_venue_facts_disarm_executor():
    stale = execution_readiness_projection(
        _execution(), _state(), _control(), now_ns=10_000_000_000, process=_process(heartbeat_at_ms=1_000)
    )
    assert stale["entry_block_reason"] == "executor_heartbeat_stale"
    unexpected = execution_readiness_projection(
        _execution(), _state(unexpected_exposure=True), _control(), now_ns=10_000_000_000, process=_process()
    )
    assert unexpected["entry_block_reason"] == "unexpected_exposure"
    assert not unexpected["entries_armed"]
    unreconciled = execution_readiness_projection(
        _execution(), _state(last_full_reconcile_at_ns=None), _control(), now_ns=10_000_000_000, process=_process()
    )
    assert unreconciled["entry_block_reason"] == "account_reconcile_stale"


def test_operator_pause_and_halt_disarm_executor():
    paused = execution_readiness_projection(
        _execution(), _state(), _control(entries_paused=True), now_ns=10_000_000_000, process=_process()
    )
    halted = execution_readiness_projection(
        _execution(), _state(), _control(emergency_halted=True), now_ns=10_000_000_000, process=_process()
    )
    assert paused["entry_block_reason"] == "entries_paused"
    assert halted["entry_block_reason"] == "emergency_halt"


def test_account_facts_remain_visible_before_executor_establishes_its_runtime():
    result = execution_readiness_projection(
        _execution(),
        _state(unexpected_exposure=True),
        _control(entries_paused=True, emergency_halted=True),
        now_ns=10_000_000_000,
        process=None,
    )
    assert not result["alive"] and not result["entries_armed"]
    assert result["entry_block_reason"] == "executor_state_missing"
    assert result["entries_paused"] and result["emergency_halted"] and result["unexpected_exposure"]
    assert result["signed_account"] == {"observed_at_ns": 9_000_000_000}
    assert result["last_full_reconcile_at_ms"] == 9_000
    assert result["heartbeat_at_ms"] is None and result["connection_observed_at_ms"] is None
