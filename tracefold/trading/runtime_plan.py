"""One fail-closed lifecycle answer for the Binance Demo Nautilus process."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .contracts import CapitalRuntimeV1, VenueBinding, VenueBindingRuntimeV1

NAUTILUS_EXECUTION_ENVIRONMENT: Literal["binance_usdm_demo"] = "binance_usdm_demo"
NautilusRuntimeDecision = Literal["blocked", "optional", "required"]


@dataclass(frozen=True, slots=True)
class NautilusRuntimePlanV1:
    """The deployment decision and the binding-local readiness behind it."""

    decision: NautilusRuntimeDecision
    reason: str
    execution_environment: Literal["binance_usdm_demo"]
    enabled_bindings: tuple[VenueBinding, ...]
    disabled_bindings: tuple[VenueBinding, ...]
    ready: bool
    readiness_reason: str


def nautilus_runtime_plan(
    *,
    capital: CapitalRuntimeV1,
    bindings: Sequence[VenueBindingRuntimeV1],
    active_intents: int,
    active_intent_bindings: Sequence[VenueBinding] = (),
) -> NautilusRuntimePlanV1:
    """Decide process ownership without consulting Docker, ports, or provider clients."""

    by_binding = {row.binding: row for row in bindings}
    binance = by_binding.get("BINANCE_USDM")
    hyperliquid = by_binding.get("HYPERLIQUID_PERP")
    if binance is None:
        return _plan("blocked", "binance_demo_binding_runtime_missing", False, "binding_runtime_missing")
    if hyperliquid is None or _hyperliquid_execution_state_present(hyperliquid):
        return _plan("blocked", "hyperliquid_execution_state_present", False, "execution_binding_disabled")
    if "HYPERLIQUID_PERP" in active_intent_bindings:
        return _plan(
            "blocked",
            "hyperliquid_recovery_requires_disabled_adapter",
            False,
            "execution_binding_disabled",
        )
    recovery_required = binance.account_state == "exposure_present" or active_intents > 0
    if recovery_required and binance.reason in {
        "recovery_blocked_account_identity_unproven",
        "recovery_blocked_credential_changed",
    }:
        return _plan("blocked", binance.reason, False, binance.reason)
    if binance.credential_state == "invalid":
        return _plan(
            "blocked",
            "recovery_blocked_credentials_invalid" if recovery_required else "binance_demo_credentials_invalid",
            False,
            "credentials_invalid",
        )
    if binance.credential_state != "configured":
        if recovery_required:
            return _plan(
                "blocked",
                "recovery_blocked_credentials_missing",
                False,
                "credentials_unconfigured",
            )
        if capital.control != "PAUSED":
            return _plan(
                "blocked",
                "capital_runtime_credentials_missing",
                False,
                "credentials_unconfigured",
            )
        return _plan(
            "optional",
            "binance_demo_credentials_unconfigured",
            False,
            "runtime_not_required",
        )

    readiness_reason = _binance_readiness_reason(binance)
    return _plan(
        "required",
        "binance_demo_recovery_required" if recovery_required else "binance_demo_credentials_configured",
        readiness_reason == "ready",
        readiness_reason,
    )


def _hyperliquid_execution_state_present(row: VenueBindingRuntimeV1) -> bool:
    return bool(
        row.credential_state != "unconfigured"
        or row.credential_fingerprint is not None
        or row.runtime_state != "stopped"
        or row.account_state != "unknown"
        or row.capability_snapshot_sha256 is not None
        or row.execution_binding_sha256 is not None
        or row.active_arm_receipt_sha256 is not None
    )


def _binance_readiness_reason(row: VenueBindingRuntimeV1) -> str:
    if row.catalog_state != "ready":
        return f"catalog_{row.catalog_state}"
    if row.capability_state != "ready" or row.capability_snapshot_sha256 is None:
        return f"capability_{row.capability_state}"
    if row.account_state != "reconciled_flat":
        return f"account_{row.account_state}"
    if row.execution_binding_sha256 is None:
        return "execution_binding_missing"
    if row.runtime_state != "ready":
        return f"runtime_{row.runtime_state}"
    return "ready"


def _plan(
    decision: NautilusRuntimeDecision,
    reason: str,
    ready: bool,
    readiness_reason: str,
) -> NautilusRuntimePlanV1:
    return NautilusRuntimePlanV1(
        decision=decision,
        reason=reason,
        execution_environment=NAUTILUS_EXECUTION_ENVIRONMENT,
        enabled_bindings=("BINANCE_USDM",),
        disabled_bindings=("HYPERLIQUID_PERP",),
        ready=ready,
        readiness_reason=readiness_reason,
    )


__all__ = [
    "NAUTILUS_EXECUTION_ENVIRONMENT",
    "NautilusRuntimeDecision",
    "NautilusRuntimePlanV1",
    "nautilus_runtime_plan",
]
