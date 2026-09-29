"""Project durable DEMO executor liveness without treating it as venue proof."""

from __future__ import annotations

from typing import Any

_HEARTBEAT_STALE_AFTER_NS = 5_000_000_000
_FULL_RECONCILE_STALE_AFTER_NS = 70_000_000_000


def execution_readiness_projection(
    execution: Any,
    state: dict[str, Any] | None,
    control: dict[str, Any] | None,
    *,
    now_ns: int,
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
        "last_error": None,
        "heartbeat_at_ms": None,
        "facts_expire_at_ms": None,
        "facts_remaining_ms": None,
        "last_full_reconcile_at_ms": None,
        "signed_account": None,
    }
    if not execution.enabled or state is None:
        return result
    heartbeat = int(state["heartbeat_at_ns"])
    remaining = max(0, heartbeat + _HEARTBEAT_STALE_AFTER_NS - now_ns)
    alive = remaining > 0
    full_at = state.get("last_full_reconcile_at_ns")
    reconciled = (
        full_at is not None
        and state.get("account_snapshot") is not None
        and now_ns - int(full_at) <= _FULL_RECONCILE_STALE_AFTER_NS
    )
    paused = True if control is None else bool(control["entries_paused"])
    halted = False if control is None else bool(control["emergency_halted"])
    error = state.get("last_error")
    unexpected = bool(state["unexpected_exposure"])
    armed = alive and reconciled and not paused and not halted and not unexpected and error is None
    reason = (
        "executor_heartbeat_stale"
        if not alive
        else "executor_error"
        if error
        else "account_reconcile_stale"
        if not reconciled
        else "unexpected_exposure"
        if unexpected
        else "emergency_halt"
        if halted
        else "entries_paused"
        if paused
        else None
    )
    result.update(
        {
            "connection": "DEMO",
            "connection_observed_at_ms": heartbeat // 1_000_000,
            "alive": alive,
            "entries_armed": armed,
            "entry_block_reason": reason,
            "entries_paused": paused,
            "emergency_halted": halted,
            "unexpected_exposure": unexpected,
            "last_error": error,
            "heartbeat_at_ms": heartbeat // 1_000_000,
            "facts_expire_at_ms": (heartbeat + _HEARTBEAT_STALE_AFTER_NS) // 1_000_000,
            "facts_remaining_ms": remaining // 1_000_000,
            "last_full_reconcile_at_ms": None if full_at is None else int(full_at) // 1_000_000,
            "signed_account": state.get("account_snapshot"),
        }
    )
    return result


__all__ = ["execution_readiness_projection"]
