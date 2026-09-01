"""One read projection for configured execution Runtime readiness."""

from __future__ import annotations

from typing import Any

from tracefold.trading.storage.execution_stream import ExecutionRuntimeState

_HEARTBEAT_STALE_AFTER_NS = 5_000_000_000


def execution_readiness_projection(
    execution: Any,
    state: ExecutionRuntimeState | None,
    *,
    now_ns: int,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": execution.mode,
        "profile_id": execution.profile_id,
        "account_slot": execution.account_slot,
        "ready": False,
        "reason": "disabled" if execution.mode == "disabled" else "runtime_state_missing",
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
        "audit_ready": False,
        "unexpected_exposure": False,
        "account_flat": False,
    }
    if execution.mode == "disabled" or state is None:
        return base
    if (
        state.mode != execution.mode
        or state.runtime_profile_id != execution.profile_id
        or state.account_slot != execution.account_slot
    ):
        base["reason"] = "runtime_identity_mismatch"
        return base
    heartbeat_age_ns = max(0, now_ns - state.heartbeat_at_ns)
    reconciliation_age_ns = max(0, now_ns - state.reconciliation_observed_at_ns)
    stale = heartbeat_age_ns > _HEARTBEAT_STALE_AFTER_NS
    base.update(
        {
            "ready": state.ready and not stale,
            "reason": "runtime_heartbeat_stale" if stale else state.unavailable_reason or "ready",
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
            "audit_ready": state.audit_ready,
            "unexpected_exposure": state.unexpected_exposure,
            "account_flat": state.account_flat,
        }
    )
    return base


__all__ = ["execution_readiness_projection"]
