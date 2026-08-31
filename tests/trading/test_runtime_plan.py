from __future__ import annotations

from dataclasses import replace

from tracefold.trading import CapitalRuntimeV1, VenueBindingRuntimeV1, nautilus_runtime_plan


def _binding(binding: str, **overrides: object) -> VenueBindingRuntimeV1:
    values: dict[str, object] = {
        "binding": binding,
        "credential_state": "unconfigured",
        "credential_fingerprint": None,
        "runtime_state": "stopped",
        "account_state": "unknown",
        "account_generation": 0,
        "catalog_state": "ready",
        "catalog_snapshot_sha256": "1" * 64,
        "catalog_captured_at_ms": 1,
        "capability_state": "missing",
        "capability_snapshot_sha256": None,
        "capability_compiled_at_ms": None,
        "capability_compile_error": None,
        "execution_binding_sha256": None,
        "active_arm_receipt_sha256": None,
        "heartbeat_at_ms": None,
        "reason": "credentials_unconfigured",
        "updated_at_ms": 1,
    }
    return VenueBindingRuntimeV1(**(values | overrides))  # type: ignore[arg-type]


def _plan(*rows: VenueBindingRuntimeV1, active_intents: int = 0, active_binding: str | None = None):
    return nautilus_runtime_plan(
        capital=CapitalRuntimeV1(control="PAUSED", blacklist_revision=0, arm_epoch=1, updated_at_ms=1),
        bindings=rows,
        active_intents=active_intents,
        active_intent_bindings=() if active_binding is None else (active_binding,),  # type: ignore[arg-type]
    )


def test_no_demo_credentials_is_explicitly_optional() -> None:
    plan = _plan(_binding("BINANCE_USDM"), _binding("HYPERLIQUID_PERP", reason="execution_binding_disabled"))

    assert (plan.decision, plan.reason, plan.ready) == (
        "optional",
        "binance_demo_credentials_unconfigured",
        False,
    )


def test_configured_demo_is_required_and_only_ready_from_binding_local_truth() -> None:
    hyperliquid = _binding("HYPERLIQUID_PERP", reason="execution_binding_disabled")
    starting = _binding(
        "BINANCE_USDM",
        credential_state="configured",
        credential_fingerprint="2" * 64,
        runtime_state="stale",
        account_state="reconciled_flat",
        capability_state="ready",
        capability_snapshot_sha256="3" * 64,
        execution_binding_sha256="4" * 64,
    )
    ready = replace(starting, runtime_state="ready")

    starting_plan = _plan(starting, hyperliquid)
    ready_plan = _plan(ready, hyperliquid)

    assert (starting_plan.decision, starting_plan.ready, starting_plan.readiness_reason) == (
        "required",
        False,
        "runtime_stale",
    )
    assert (ready_plan.decision, ready_plan.ready, ready_plan.readiness_reason) == ("required", True, "ready")


def test_recovery_without_credentials_and_any_hyperliquid_execution_state_are_blocked() -> None:
    clean_hyperliquid = _binding("HYPERLIQUID_PERP", reason="execution_binding_disabled")
    missing_credentials = _plan(
        _binding("BINANCE_USDM"),
        clean_hyperliquid,
        active_intents=1,
        active_binding="BINANCE_USDM",
    )
    disabled_recovery = _plan(
        _binding("BINANCE_USDM"),
        clean_hyperliquid,
        active_intents=1,
        active_binding="HYPERLIQUID_PERP",
    )
    stale_hyperliquid_pointer = _plan(
        _binding("BINANCE_USDM"),
        _binding("HYPERLIQUID_PERP", capability_snapshot_sha256="5" * 64),
    )

    assert missing_credentials.reason == "recovery_blocked_credentials_missing"
    assert disabled_recovery.reason == "hyperliquid_recovery_requires_disabled_adapter"
    assert stale_hyperliquid_pointer.reason == "hyperliquid_execution_state_present"
    assert {missing_credentials.decision, disabled_recovery.decision, stale_hyperliquid_pointer.decision} == {"blocked"}


def test_exposure_is_a_recovery_obligation_even_without_an_intent() -> None:
    hyperliquid = _binding("HYPERLIQUID_PERP", reason="execution_binding_disabled")
    missing = _plan(_binding("BINANCE_USDM", account_state="exposure_present"), hyperliquid)
    invalid = _plan(
        _binding("BINANCE_USDM", credential_state="invalid", account_state="exposure_present"),
        hyperliquid,
    )
    configured = _plan(
        _binding(
            "BINANCE_USDM",
            credential_state="configured",
            credential_fingerprint="2" * 64,
            account_state="exposure_present",
        ),
        hyperliquid,
    )

    assert (missing.decision, missing.reason) == ("blocked", "recovery_blocked_credentials_missing")
    assert (invalid.decision, invalid.reason) == ("blocked", "recovery_blocked_credentials_invalid")
    assert (configured.decision, configured.reason) == ("required", "binance_demo_recovery_required")
    assert configured.readiness_reason == "capability_missing"


def test_exposure_with_changed_or_unproven_account_identity_is_blocked() -> None:
    hyperliquid = _binding("HYPERLIQUID_PERP", reason="execution_binding_disabled")
    changed = _plan(
        _binding(
            "BINANCE_USDM",
            credential_state="invalid",
            credential_fingerprint="2" * 64,
            account_state="exposure_present",
            reason="recovery_blocked_credential_changed",
        ),
        hyperliquid,
    )
    unproven = _plan(
        _binding(
            "BINANCE_USDM",
            credential_state="invalid",
            account_state="exposure_present",
            reason="recovery_blocked_account_identity_unproven",
        ),
        hyperliquid,
    )

    assert (changed.decision, changed.reason) == ("blocked", "recovery_blocked_credential_changed")
    assert (unproven.decision, unproven.reason) == (
        "blocked",
        "recovery_blocked_account_identity_unproven",
    )


def test_active_intent_with_changed_account_identity_is_blocked() -> None:
    plan = _plan(
        _binding(
            "BINANCE_USDM",
            credential_state="invalid",
            credential_fingerprint="2" * 64,
            account_state="reconciled_flat",
            reason="recovery_blocked_credential_changed",
        ),
        _binding("HYPERLIQUID_PERP", reason="execution_binding_disabled"),
        active_intents=1,
        active_binding="BINANCE_USDM",
    )

    assert (plan.decision, plan.reason) == ("blocked", "recovery_blocked_credential_changed")
