"""Project durable DEMO executor liveness without treating it as venue proof."""

from __future__ import annotations

from typing import Any

from tracefold.trading.operator_control import control_entry_block

_HEARTBEAT_STALE_AFTER_MS = 5_000
_FULL_RECONCILE_STALE_AFTER_NS = 70_000_000_000


def execution_readiness_projection(
    execution: Any,
    state: dict[str, Any] | None,
    control: dict[str, Any] | None,
    *,
    now_ns: int,
    process: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "configured_connection": execution.binance.environment or "LIVE",
        "connection": None,
        "connection_observed_at_ms": None,
        "account_slot": execution.account_slot,
        "alive": False,
        "entries_armed": False,
        "entry_block_reason": "disabled" if not execution.enabled else "executor_state_missing",
        "entries_paused": True,
        "emergency_halted": False,
        "unexpected_exposure": False,
        "execution_faults": {},
        "last_error": None,
        "heartbeat_at_ms": None,
        "facts_expire_at_ms": None,
        "facts_remaining_ms": None,
        "last_full_reconcile_at_ms": None,
        "signed_account": None,
    }
    if not execution.enabled or state is None:
        return result
    full_at = state.get("last_full_reconcile_at_ns")
    paused = True if control is None else bool(control["entries_paused"])
    halted = False if control is None else bool(control["emergency_halted"])
    unexpected = bool(state["unexpected_exposure"])
    result.update(
        {
            "connection": "DEMO",
            "entries_paused": paused,
            "emergency_halted": halted,
            "unexpected_exposure": unexpected,
            "execution_faults": state.get("execution_faults", {}),
            "last_full_reconcile_at_ms": None if full_at is None else int(full_at) // 1_000_000,
            "signed_account": state.get("account_snapshot"),
        }
    )
    if process is None:
        return result
    heartbeat = int(process["heartbeat_at_ms"])
    remaining = max(0, heartbeat + _HEARTBEAT_STALE_AFTER_MS - now_ns // 1_000_000)
    alive = remaining > 0 and process["lifecycle_state"] == "running"
    reconciled = (
        full_at is not None
        and state.get("account_snapshot") is not None
        and now_ns - int(full_at) <= _FULL_RECONCILE_STALE_AFTER_NS
    )
    error = process.get("fault_code")
    blocked = control_entry_block(
        {**(control or result), "execution_faults": result["execution_faults"]}, unexpected_exposure=unexpected
    )
    armed = alive and reconciled and blocked is None and error is None
    reason = (
        "executor_heartbeat_stale"
        if not alive
        else "executor_error"
        if error
        else "account_reconcile_stale"
        if not reconciled
        else blocked
    )
    result.update(
        {
            "connection_observed_at_ms": heartbeat,
            "alive": alive,
            "entries_armed": armed,
            "entry_block_reason": reason,
            "last_error": error,
            "heartbeat_at_ms": heartbeat,
            "facts_expire_at_ms": heartbeat + _HEARTBEAT_STALE_AFTER_MS,
            "facts_remaining_ms": remaining,
        }
    )
    return result


__all__ = ["execution_readiness_projection"]
