"""Read the one durable Runtime row without promoting old account observations to fresh facts."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from tracefold.trading.storage.execution_stream import (
    ExecutionAccountSnapshot,
    ExecutionRuntimeControlState,
    ExecutionRuntimeState,
)

_HEARTBEAT_STALE_AFTER_NS = 5_000_000_000


def execution_readiness_projection(
    execution: Any,
    state: ExecutionRuntimeState | None,
    control: ExecutionRuntimeControlState | None,
    *,
    now_ns: int,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": execution.mode,
        "account_slot": execution.account_slot,
        "alive": False,
        "entries_armed": False,
        "entry_block_reason": "disabled" if execution.mode == "disabled" else "runtime_state_missing",
        "reported_entry_block_reason": None,
        "entries_paused": True,
        "emergency_halted": False,
        "unexpected_exposure": False,
        "protection_status": "not_applicable",
        "routes_count": 0,
        "heartbeat_at_ms": None,
        "facts_expire_at_ms": None,
        "facts_remaining_ms": None,
        "account_projection_failure": None,
        "convergence_checked_at_ms": None,
        "convergence_failure": None,
        "venue_read_started_at_ms": None,
        "venue_read_completed_at_ms": None,
        "venue_read_failure": None,
        "recovery_attempted_at_ms": None,
        "recovery_result": None,
        "current_account": None,
    }
    if execution.mode == "disabled" or state is None:
        return base
    if state.mode != execution.mode or state.account_slot != execution.account_slot:
        base["entry_block_reason"] = "runtime_identity_mismatch"
        return base
    remaining_ns = max(0, state.heartbeat_at_ns + _HEARTBEAT_STALE_AFTER_NS - now_ns)
    stale = remaining_ns == 0
    current_control = control if control is not None and control.account_slot == execution.account_slot else None
    alive = bool(state.alive and not stale)
    entries_armed = bool(state.entries_armed and alive)
    entry_block_reason = "runtime_heartbeat_stale" if stale else state.entry_block_reason
    base.update(
        {
            "alive": alive,
            "entries_armed": entries_armed,
            "entry_block_reason": None if entries_armed else entry_block_reason or "entry_blocked",
            "reported_entry_block_reason": state.entry_block_reason,
            "entries_paused": True if current_control is None else current_control.entries_paused,
            "emergency_halted": False if current_control is None else current_control.emergency_halted,
            "unexpected_exposure": state.unexpected_exposure,
            "protection_status": state.protection_status,
            "routes_count": state.routes_count,
            "heartbeat_at_ms": state.heartbeat_at_ns // 1_000_000,
            "facts_expire_at_ms": (state.heartbeat_at_ns + _HEARTBEAT_STALE_AFTER_NS) // 1_000_000,
            "facts_remaining_ms": remaining_ns // 1_000_000,
            "account_projection_failure": state.account_projection_failure,
            "convergence_checked_at_ms": _ms(state.convergence_checked_at_ns),
            "convergence_failure": state.convergence_failure,
            "venue_read_started_at_ms": _ms(state.venue_read_started_at_ns),
            "venue_read_completed_at_ms": _ms(state.venue_read_completed_at_ns),
            "venue_read_failure": state.venue_read_failure,
            "recovery_attempted_at_ms": _ms(state.recovery_attempted_at_ns),
            "recovery_result": state.recovery_result,
            "current_account": None if state.account_snapshot is None else _account(state.account_snapshot),
        }
    )
    return base


def _ms(value: int | None) -> int | None:
    return None if value is None else value // 1_000_000


def _account(snapshot: ExecutionAccountSnapshot) -> dict[str, Any]:
    published = asdict(snapshot)
    published["observed_at_ms"] = published.pop("observed_at_ns") // 1_000_000
    for finding in published["findings"]:
        finding["observed_at_ms"] = finding.pop("observed_at_ns") // 1_000_000
    return published


__all__ = ["execution_readiness_projection"]
