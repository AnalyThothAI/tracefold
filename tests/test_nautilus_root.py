from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from tests.nautilus_oi_runtime_fixtures import oi_profile
from tracefold.app.nautilus.root import (
    _activate_profile,
    _observe_reconciliation,
    _observe_runtime_start,
    _preflight_profile,
)
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.trading.storage.execution_stream import ExecutionProfileActivation


class _Trading:
    def __init__(self, current: ExecutionProfileActivation | None = None) -> None:
        self.current = current
        self.by_profile = {} if current is None else {current.runtime_profile_id: current}
        self.appended: list[ExecutionProfileActivation] = []

    def latest_execution_profile_activation(self, _account_slot: str) -> ExecutionProfileActivation | None:
        return self.current

    def execution_profile_activation(self, profile_id: str) -> ExecutionProfileActivation | None:
        return self.by_profile.get(profile_id)

    def execution_stream_fence(self) -> tuple[int, int]:
        return 12, 34

    def append_execution_profile_activation(self, value: ExecutionProfileActivation) -> None:
        self.appended.append(value)
        self.by_profile[value.runtime_profile_id] = value
        self.current = value


def _repos(trading: _Trading) -> Any:
    return SimpleNamespace(trading=trading, transaction=nullcontext)


def _activation(profile_id: str = "oi-paper-profile") -> ExecutionProfileActivation:
    profile = oi_profile()
    return ExecutionProfileActivation(
        runtime_profile_id=profile_id,
        account_slot=profile.account_slot,
        activated_after_signal_seq=1,
        activated_after_command_seq=2,
        mode="paper",
        runtime_release=profile.runtime_release,
        config_sha256=profile.config_sha256,
        created_at_ns=100,
    )


def test_new_profile_activation_requires_authoritative_binance_flat() -> None:
    trading = _Trading()

    with pytest.raises(RuntimeError, match="oi_runtime_cold_transition_requires_binance_flat"):
        _activate_profile(
            repos=_repos(trading),
            profile=oi_profile(),
            existing=None,
            account_flat=False,
            created_at_ns=200,
        )

    assert trading.appended == []


def test_new_profile_activation_records_the_current_stream_fence() -> None:
    trading = _Trading()

    activation = _activate_profile(
        repos=_repos(trading),
        profile=oi_profile(),
        existing=None,
        account_flat=True,
        created_at_ns=200,
    )

    assert activation.activated_after_signal_seq == 12
    assert activation.activated_after_command_seq == 34
    assert trading.current == activation


def test_superseded_profile_cannot_be_reactivated() -> None:
    old = _activation()
    trading = _Trading(_activation("next-profile"))
    trading.by_profile[old.runtime_profile_id] = old

    with pytest.raises(RuntimeError, match="oi_runtime_profile_cannot_be_reactivated"):
        _preflight_profile(_repos(trading), oi_profile())


def test_reconciliation_observation_preserves_native_ids_and_flat_proof() -> None:
    audit = AuditSink(
        factory=ObservationFactory(
            runtime_profile_id="oi-paper-profile",
            runtime_release="nautilus-1.231.0+oi-v1",
            execution_strategy="oi_nautilus_v1",
        )
    )
    position = SimpleNamespace(instrument_id=SimpleNamespace(value="BTCUSDT-PERP.BINANCE"))
    order = SimpleNamespace(
        client_order_id=SimpleNamespace(value="tf-client"),
        venue_order_id=SimpleNamespace(value="12345"),
        instrument_id=SimpleNamespace(value="BTCUSDT-PERP.BINANCE"),
    )

    _observe_reconciliation(audit=audit, reports=([position], [order]), observed_at_ns=1_000)

    observation = audit.flush_once(lambda _values: None)[0]
    assert observation.normalized_kind == "reconciliation"
    assert observation.summary == {
        "source": "binance_private_api",
        "positions": 1,
        "orders": 1,
        "account_flat": False,
        "native_refs_truncated": False,
    }
    assert observation.native_identity_references == ("12345", "BTCUSDT-PERP.BINANCE", "tf-client")


def test_runtime_start_receipt_binds_exact_runtime_image_config_and_credentials() -> None:
    audit = AuditSink(
        factory=ObservationFactory(
            runtime_profile_id="oi-paper-profile",
            runtime_release="nautilus-1.231.0+oi-v1",
            execution_strategy="oi_nautilus_v1",
        )
    )
    state: Any = SimpleNamespace(
        started_at_ns=1_000,
        runtime_id="11111111-1111-4111-8111-111111111111",
        mode="paper",
        runtime_revision="a" * 40,
        image_digest="sha256:" + "b" * 64,
        config_sha256="c" * 64,
        credential_fingerprint="d" * 64,
        account_slot="binance_usdm_primary",
    )

    _observe_runtime_start(audit=audit, state=state)

    observation = audit.flush_once(lambda _values: None)[0]
    assert observation.normalized_kind == "readiness"
    assert observation.summary["runtime_id"] == state.runtime_id
    assert observation.summary["image_digest"] == state.image_digest
    assert observation.summary["config_sha256"] == state.config_sha256
    assert observation.summary["credential_fingerprint"] == state.credential_fingerprint
