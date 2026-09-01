from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from tests.nautilus_oi_runtime_fixtures import oi_profile
from tracefold.app.nautilus import root as nautilus_root
from tracefold.app.nautilus.root import (
    _activate_profile,
    _discover_routes,
    _observe_reconciliation,
    _observe_runtime_start,
    _preflight_profile,
)
from tracefold.app.workers.trading_notifications import trading_notification_text
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials
from tracefold.trading.notification_policy import is_notifiable
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


def test_route_discovery_uses_real_clock_and_skips_unaddressable_provider_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Instrument:
        def __init__(self, base_code: str) -> None:
            self.quote_currency = nautilus_root.USDT
            self.settlement_currency = nautilus_root.USDT
            self.info = {"status": "TRADING"}
            self.base_currency = SimpleNamespace(code=base_code)
            self.id = InstrumentId.from_str(f"{base_code}USDT-PERP.BINANCE")

    class _Provider:
        def __init__(self, **kwargs: Any) -> None:
            captured["provider"] = kwargs

        async def load_all_async(self) -> None:
            captured["loaded"] = True

        def list_all(self) -> list[_Instrument]:
            return [_Instrument("测试测试"), _Instrument("BTC")]

    monkeypatch.setattr(nautilus_root, "CryptoPerpetual", _Instrument)
    monkeypatch.setattr(
        nautilus_root,
        "get_cached_binance_http_client",
        lambda **kwargs: captured.setdefault("client", kwargs),
    )
    monkeypatch.setattr(nautilus_root, "BinanceFuturesInstrumentProvider", _Provider)

    routes = asyncio.run(
        _discover_routes(
            "paper",
            BinanceRuntimeCredentials(api_key="demo-key", api_secret="demo-secret"),
        )
    )

    assert captured["loaded"] is True
    assert captured["client"]["environment"] == nautilus_root.BinanceEnvironment.DEMO
    assert type(captured["client"]["clock"]).__name__ == "LiveClock"
    assert [route.market_key for route in routes] == ["crypto:perp:BTC:USDT"]


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


def test_the_notification_predicate_reads_the_summaries_these_writers_actually_produce() -> None:
    """#472: the shipped predicate asked for keys no writer has ever emitted.

    It wanted `summary ->> 'state' = 'flat'` from reconciliation and a `control_stage` from every
    readiness, while these two writers produce `account_flat` and `lifecycle`. Both branches were
    therefore unreachable for the whole life of the feature, and nothing failed — the delivery queue
    was simply always empty. Reading the predicate against what the writers return, rather than
    against a restatement of it, is what makes the next rename fail here instead of in production.
    """

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
    position = SimpleNamespace(instrument_id=SimpleNamespace(value="BTCUSDT-PERP.BINANCE"))

    _observe_reconciliation(audit=audit, reports=([], []), observed_at_ns=1_000)
    _observe_reconciliation(audit=audit, reports=([position], []), observed_at_ns=2_000)
    _observe_runtime_start(audit=audit, state=state)
    flat, unflat, started = audit.flush_once(lambda _values: None)

    # The timer's steady state is not a card; exposure and a Runtime restart both are.
    assert is_notifiable(flat.normalized_kind, flat.summary) is False
    assert is_notifiable(unflat.normalized_kind, unflat.summary) is True
    assert is_notifiable(started.normalized_kind, started.summary) is True
    for observation in (unflat, started):
        row = {"normalized_kind": observation.normalized_kind, "summary": observation.summary}
        assert trading_notification_text(row) is not None, f"{observation.normalized_kind} renders no stage"
