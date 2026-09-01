"""One read projection for configured execution Runtime readiness."""

from __future__ import annotations

from typing import Any

from tracefold.trading.storage.execution_stream import ExecutionRuntimeControlState, ExecutionRuntimeState

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
        "profile_id": execution.profile_id,
        "account_slot": execution.account_slot,
        "alive": False,
        "execution_safe": False,
        "entries_armed": False,
        "entry_block_reason": "disabled" if execution.mode == "disabled" else "runtime_state_missing",
        "runtime_release": None,
        "config_sha256": None,
        "runtime_revision": None,
        "image_digest": None,
        "credential_fingerprint": None,
        "lifecycle_state": None,
        "heartbeat_at_ns": None,
        "reconciliation_observed_at_ns": None,
        "reconciliation_age_ms": None,
        "singleton_ready": False,
        "credential_ready": False,
        "activation_ready": False,
        "startup_reconciled": False,
        "portfolio_ready": False,
        "control_plane_ready": False,
        "audit_ready": False,
        "day_start_ready": False,
        "entries_paused": True,
        "emergency_halted": False,
        "unexpected_exposure": False,
        "account_flat": False,
        "positions_count": 0,
        "open_orders_count": 0,
        "protection_status": "unknown",
    }
    if execution.mode == "disabled" or state is None:
        return base
    if (
        state.mode != execution.mode
        or state.runtime_profile_id != execution.profile_id
        or state.account_slot != execution.account_slot
    ):
        base["entry_block_reason"] = "runtime_identity_mismatch"
        return base
    heartbeat_age_ns = max(0, now_ns - state.heartbeat_at_ns)
    reconciliation_age_ns = max(0, now_ns - state.reconciliation_observed_at_ns)
    stale = heartbeat_age_ns > _HEARTBEAT_STALE_AFTER_NS
    current_control = control if control is not None and control.runtime_profile_id == execution.profile_id else None
    entries_paused = True if current_control is None else current_control.entries_paused
    emergency_halted = False if current_control is None else current_control.emergency_halted
    alive = bool(state.alive and not stale)
    execution_safe = bool(state.execution_safe and alive)
    entries_armed = bool(
        state.entries_armed
        and execution_safe
        and current_control is not None
        and not entries_paused
        and not emergency_halted
    )
    entry_block_reason: str | None
    if stale:
        entry_block_reason = "runtime_heartbeat_stale"
    elif not execution_safe:
        entry_block_reason = state.entry_block_reason or "execution_unsafe"
    elif current_control is None:
        entry_block_reason = "runtime_control_state_missing"
    elif emergency_halted:
        entry_block_reason = "emergency_halt"
    elif entries_paused:
        entry_block_reason = "entries_paused"
    else:
        entry_block_reason = state.entry_block_reason
    base.update(
        {
            "alive": alive,
            "execution_safe": execution_safe,
            "entries_armed": entries_armed,
            "entry_block_reason": None if entries_armed else entry_block_reason or "entry_blocked",
            "runtime_release": state.runtime_release,
            "config_sha256": state.config_sha256,
            "runtime_revision": state.runtime_revision,
            "image_digest": state.image_digest,
            "credential_fingerprint": state.credential_fingerprint,
            "lifecycle_state": state.lifecycle_state,
            "heartbeat_at_ns": state.heartbeat_at_ns,
            "reconciliation_observed_at_ns": state.reconciliation_observed_at_ns,
            "reconciliation_age_ms": reconciliation_age_ns // 1_000_000,
            "singleton_ready": state.singleton_ready,
            "credential_ready": state.credential_ready,
            "activation_ready": state.activation_ready,
            "startup_reconciled": state.startup_reconciled,
            "portfolio_ready": state.portfolio_ready,
            "control_plane_ready": state.control_plane_ready,
            "audit_ready": state.audit_ready,
            "day_start_ready": state.day_start_ready,
            "entries_paused": entries_paused,
            "emergency_halted": emergency_halted,
            "unexpected_exposure": state.unexpected_exposure,
            "account_flat": state.account_flat,
            "positions_count": state.positions_count,
            "open_orders_count": state.open_orders_count,
            "protection_status": state.protection_status,
        }
    )
    return base


__all__ = ["execution_readiness_projection"]
