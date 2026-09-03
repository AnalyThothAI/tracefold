"""One read projection for configured execution Runtime readiness."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from tracefold.trading.storage.execution_stream import ExecutionRuntimeControlState, ExecutionRuntimeState

_HEARTBEAT_STALE_AFTER_NS = 5_000_000_000
_PRIVATE_RECONCILIATION_STALE_AFTER_NS = 10_000_000_000


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
        "startup_reconciled": False,
        "entries_paused": True,
        "emergency_halted": False,
        "unexpected_exposure": False,
        "account_flat": False,
        "account_flat_proven": False,
        "positions_count": 0,
        "open_orders_count": 0,
        "protection_status": "unknown",
        "routes_count": 0,
        "facts_expire_at_ms": None,
        "current_account": None,
    }
    if execution.mode == "disabled" or state is None:
        return base
    if state.mode != execution.mode or state.account_slot != execution.account_slot:
        base["entry_block_reason"] = "runtime_identity_mismatch"
        return base
    heartbeat_age_ns = max(0, now_ns - state.heartbeat_at_ns)
    reconciliation_age_ns = max(0, now_ns - state.reconciliation_observed_at_ns)
    stale = heartbeat_age_ns > _HEARTBEAT_STALE_AFTER_NS
    private_reconciliation_fresh = (
        state.startup_reconciled
        and state.reconciliation_observed_at_ns <= now_ns
        and reconciliation_age_ns <= _PRIVATE_RECONCILIATION_STALE_AFTER_NS
    )
    account_snapshot_flat = bool(
        state.account_snapshot is not None
        and state.account_snapshot.complete
        and not state.account_snapshot.truncated
        and not state.account_snapshot.positions
        and not state.account_snapshot.orders
        and state.account_snapshot.open_orders_count == 0
        and state.account_snapshot.inflight_orders_count == 0
        and state.account_snapshot.unknown_orders_count == 0
    )
    current_control = control if control is not None and control.account_slot == execution.account_slot else None
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
        entry_block_reason = "emergency_halted"
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
            "startup_reconciled": state.startup_reconciled,
            "entries_paused": entries_paused,
            "emergency_halted": emergency_halted,
            "unexpected_exposure": state.unexpected_exposure,
            "account_flat": state.account_flat,
            "account_flat_proven": bool(
                alive
                and execution_safe
                and private_reconciliation_fresh
                and account_snapshot_flat
                and state.account_flat
            ),
            "positions_count": state.positions_count,
            "open_orders_count": state.open_orders_count,
            "protection_status": state.protection_status,
            "routes_count": len(state.routes),
            # When this projection stops being current, so a reader can compare one instant against
            # its own clock instead of running a timer per freshness rule (#528 PR-2 block 1).
            # The earlier of the two budgets below is the whole answer: past it, `alive` or
            # `account_flat_proven` is no longer what this response says it is.
            "facts_expire_at_ms": min(
                state.heartbeat_at_ns + _HEARTBEAT_STALE_AFTER_NS,
                state.reconciliation_observed_at_ns + _PRIVATE_RECONCILIATION_STALE_AFTER_NS,
            )
            // 1_000_000,
            "current_account": (
                asdict(state.account_snapshot)
                if private_reconciliation_fresh and state.account_snapshot is not None
                else None
            ),
        }
    )
    return base


__all__ = ["execution_readiness_projection"]
