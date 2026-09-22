"""One read projection for configured execution Runtime readiness.

It re-derives nothing the Runtime already decided. `entries_armed` and `entry_block_reason` come off
the projection row exactly as the Runtime wrote them, and the only thing added here is this reader's
own freshness rule: a heartbeat past its budget makes the whole row a claim about a Runtime that may not
be running, so `alive` and `entries_armed` both fall to false and the account is withheld.

The private account-proof answers that stood here -- `execution_safe`, `startup_reconciled`,
`reconciliation_age_ms` and `account_flat_proven` -- went with the proof (#680). Nautilus reconciles
the venue before a Runtime's Strategy starts and every five seconds after, so a live heartbeat is the
freshness of the account the Runtime reports; `current_account` is that account, row by row.
"""

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
        "entries_paused": True,
        "emergency_halted": False,
        "unexpected_exposure": False,
        "protection_status": "not_applicable",
        "routes_count": 0,
        "facts_expire_at_ms": None,
        "current_account": None,
    }
    if execution.mode == "disabled" or state is None:
        return base
    if state.mode != execution.mode or state.account_slot != execution.account_slot:
        base["entry_block_reason"] = "runtime_identity_mismatch"
        return base
    stale = max(0, now_ns - state.heartbeat_at_ns) > _HEARTBEAT_STALE_AFTER_NS
    # The operator's own switches, rendered as the durable control row states them. The Runtime reads
    # the same row and its `entries_armed` already accounts for them.
    current_control = control if control is not None and control.account_slot == execution.account_slot else None
    alive = bool(state.alive and not stale)
    entries_armed = bool(state.entries_armed and alive)
    entry_block_reason = "runtime_heartbeat_stale" if stale else state.entry_block_reason
    base.update(
        {
            "alive": alive,
            "entries_armed": entries_armed,
            "entry_block_reason": None if entries_armed else entry_block_reason or "entry_blocked",
            "entries_paused": True if current_control is None else current_control.entries_paused,
            "emergency_halted": False if current_control is None else current_control.emergency_halted,
            "unexpected_exposure": state.unexpected_exposure,
            "protection_status": state.protection_status,
            "routes_count": state.routes_count,
            # When this projection stops being current, so a reader compares one instant against its
            # own clock instead of running a timer (#528 PR-2 block 1).
            "facts_expire_at_ms": (state.heartbeat_at_ns + _HEARTBEAT_STALE_AFTER_NS) // 1_000_000,
            "current_account": None if state.account_snapshot is None else _account(state.account_snapshot),
        }
    )
    return base


def _account(snapshot: ExecutionAccountSnapshot) -> dict[str, Any]:
    """The stored snapshot minus its own clock, which `facts_expire_at_ms` already carries."""

    published = asdict(snapshot)
    del published["observed_at_ns"]
    return published


__all__ = ["execution_readiness_projection"]
