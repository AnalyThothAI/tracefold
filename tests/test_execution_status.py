from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

from tracefold.app.execution_status import execution_readiness_projection
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState


def _execution(mode: str = "paper") -> SimpleNamespace:
    return SimpleNamespace(mode=mode, profile_id="demo-v1", account_slot="binance_usdm_primary")


def _state(*, heartbeat_at_ns: int = 10_000_000_000) -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        runtime_profile_id="demo-v1",
        mode="paper",
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="a" * 64,
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        runtime_revision="b" * 40,
        image_digest="sha256:" + "c" * 64,
        credential_fingerprint="d" * 64,
        lifecycle_state="running",
        ready=True,
        singleton_ready=True,
        credential_ready=True,
        activation_ready=True,
        startup_reconciled=True,
        portfolio_ready=True,
        audit_ready=True,
        unexpected_exposure=False,
        account_flat=True,
        reconciliation_observed_at_ns=9_000_000_000,
        heartbeat_at_ns=heartbeat_at_ns,
        unavailable_reason=None,
        started_at_ns=8_000_000_000,
        updated_at_ns=heartbeat_at_ns,
    )


def test_disabled_execution_never_projects_a_stale_runtime_as_ready() -> None:
    projection = execution_readiness_projection(_execution("disabled"), _state(), now_ns=10_000_000_000)

    assert projection["ready"] is False
    assert projection["reason"] == "disabled"
    assert projection["runtime_release"] is None


def test_active_execution_projects_exact_runtime_gates_and_identity() -> None:
    projection = execution_readiness_projection(_execution(), _state(), now_ns=10_000_000_000)

    assert projection["ready"] is True
    assert projection["reason"] == "ready"
    assert projection["runtime_release"] == "nautilus-1.231.0+oi-v1"
    assert projection["credential_fingerprint"] == "d" * 64
    assert projection["reconciliation_age_ms"] == 1_000


def test_active_execution_fails_closed_on_identity_or_heartbeat_drift() -> None:
    mismatch = execution_readiness_projection(
        SimpleNamespace(mode="paper", profile_id="demo-v2", account_slot="binance_usdm_primary"),
        _state(),
        now_ns=10_000_000_000,
    )
    stale = execution_readiness_projection(_execution(), _state(), now_ns=15_000_000_001)

    assert mismatch["ready"] is False
    assert mismatch["reason"] == "runtime_identity_mismatch"
    assert stale["ready"] is False
    assert stale["reason"] == "runtime_heartbeat_stale"


def test_transient_flat_and_unexpected_exposure_facts_remain_fail_closed() -> None:
    state = replace(
        _state(),
        ready=False,
        unexpected_exposure=True,
        unavailable_reason="unexpected_exposure",
    )

    projection = execution_readiness_projection(_execution(), state, now_ns=10_000_000_000)

    assert projection["ready"] is False
    assert projection["reason"] == "unexpected_exposure"
    assert projection["account_flat"] is True
    assert projection["unexpected_exposure"] is True
