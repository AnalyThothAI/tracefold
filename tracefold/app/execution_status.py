"""One read projection for configured execution Runtime readiness.

It re-derives nothing the Runtime already decided. `entries_armed` and `entry_block_reason` come off
the projection row exactly as `RuntimeReadiness.snapshot` wrote them — that snapshot already folds in
the control row the Runtime is holding — and the only thing added here is this reader's own freshness
rule: a heartbeat past its budget makes the whole row a claim about a Runtime that may not be running,
so `alive`, `execution_safe` and `entries_armed` all fall to false together (#537 PR-3).

It also published six identity facts -- `runtime_release`, `config_sha256`, `runtime_revision`,
`image_digest`, `credential_fingerprint` and `lifecycle_state` -- straight through to `/status` and to
`tracefold trading status`. No page rendered one, no operator command took one, and the projection
already answers what an operator acts on: whether entries are armed, why not, and what the account
holds (#537 PR-4).

One field per operator question, and the same dict for both readers. The two raw observation clocks,
the readiness-level position and order counts, and raw `account_flat` were published beside the
answers derived from them: `facts_expire_at_ms` and `reconciliation_age_ms` are the two ages measured
here, `current_account` carries the positions and orders row by row, and `account_flat_proven` is the
only flat an operator acts on. The account snapshot's own two clocks, its day-start equity baseline
and its `truncated` flag went the same way -- the drawdown is already measured against that baseline
and a truncated snapshot is already not `complete` (#537 PR-5).
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
        "reconciliation_age_ms": None,
        "startup_reconciled": False,
        "entries_paused": True,
        "emergency_halted": False,
        "unexpected_exposure": False,
        "account_flat_proven": False,
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
    # The operator's own switches, rendered as the durable control row states them. The Runtime reads
    # the same row and its `entries_armed` already accounts for them; this is what an operator toggled,
    # not a second derivation of what the Runtime did with it.
    current_control = control if control is not None and control.account_slot == execution.account_slot else None
    entries_paused = True if current_control is None else current_control.entries_paused
    emergency_halted = False if current_control is None else current_control.emergency_halted
    alive = bool(state.alive and not stale)
    execution_safe = bool(state.execution_safe and alive)
    entries_armed = bool(state.entries_armed and alive)
    entry_block_reason = "runtime_heartbeat_stale" if stale else state.entry_block_reason
    base.update(
        {
            "alive": alive,
            "execution_safe": execution_safe,
            "entries_armed": entries_armed,
            "entry_block_reason": None if entries_armed else entry_block_reason or "entry_blocked",
            "reconciliation_age_ms": reconciliation_age_ns // 1_000_000,
            "startup_reconciled": state.startup_reconciled,
            "entries_paused": entries_paused,
            "emergency_halted": emergency_halted,
            "unexpected_exposure": state.unexpected_exposure,
            "account_flat_proven": bool(
                alive
                and execution_safe
                and private_reconciliation_fresh
                and account_snapshot_flat
                and state.account_flat
            ),
            "protection_status": state.protection_status,
            "routes_count": state.routes_count,
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
                _account(state.account_snapshot)
                if private_reconciliation_fresh and state.account_snapshot is not None
                else None
            ),
        }
    )
    return base


def _account(snapshot: ExecutionAccountSnapshot) -> dict[str, Any]:
    """The stored snapshot minus the four facts the answers above it already carry.

    `observed_at_ns` and `market_observed_at_ns` are the clocks `facts_expire_at_ms` is derived from,
    `day_start_equity_usd` is the baseline `daily_drawdown_usd` was measured against, and `truncated`
    is one of the conditions that makes `complete` false.
    """

    published = asdict(snapshot)
    for key in ("observed_at_ns", "market_observed_at_ns", "day_start_equity_usd", "truncated"):
        del published[key]
    return published


__all__ = ["execution_readiness_projection"]
