from __future__ import annotations

import asyncio
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import PositionSide

from tracefold.platform.config.models import Settings


def _secure_secret(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


@pytest.mark.parametrize(
    ("snapshot_missing", "forced_bootstrap"),
    [(False, False), (True, False), (False, True)],
)
def test_nautilus_root_composes_one_node_and_shuts_everything_down_on_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_missing: bool,
    forced_bootstrap: bool,
) -> None:
    from tracefold.app.nautilus import root

    settings = Settings()
    settings.set_config_dir(tmp_path)
    _secure_secret(tmp_path / "binance_demo_api_key", "demo-key")
    _secure_secret(tmp_path / "binance_demo_api_secret", "demo-secret")
    calls: list[str] = []
    captured: dict[str, object] = {}

    class FakeTrader:
        def add_strategy(self, strategy: object) -> None:
            calls.append("strategy")
            captured["strategy"] = strategy

    class FakeNode:
        def __init__(self, config: object, loop: asyncio.AbstractEventLoop) -> None:
            calls.append("node")
            captured["config"] = config
            captured["loop"] = loop
            self.trader = FakeTrader()
            self.is_running = False
            self._stopped = asyncio.Event()

        def add_data_client_factory(self, name: str, factory: object) -> None:
            captured["data_factory"] = (name, factory)

        def add_exec_client_factory(self, name: str, factory: object) -> None:
            captured["exec_factory"] = (name, factory)

        def build(self) -> None:
            calls.append("build")

        async def run_async(self) -> None:
            calls.append("run")
            self.is_running = True
            await self._stopped.wait()
            self.is_running = False

        async def stop_async(self) -> None:
            calls.append("node-stop")
            self._stopped.set()

        def dispose(self) -> None:
            calls.append("dispose")

    class FakeBridge:
        error = None

        def __init__(
            self,
            supplied_settings: Settings,
            queues: object,
            *,
            capability_snapshot_sha256: str | None = "not-passed",
        ) -> None:
            calls.append("database")
            captured["bridge_settings"] = supplied_settings
            captured["queues"] = queues
            captured["bridge_capability_snapshot_sha256"] = capability_snapshot_sha256

        def start(self) -> None:
            calls.append("database-start")

        def stop(self) -> None:
            calls.append("database-stop")

        def join(self, timeout: float | None = None) -> None:
            calls.append("database-join")
            captured["join_timeout"] = timeout

        def readiness(self) -> dict[str, object]:
            return {"ok": False, "reason": "starting"}

    class FakeServer:
        def __init__(self) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            calls.append("probe-start")
            while not self.should_exit:
                await asyncio.sleep(0)
            calls.append("probe-stop")

    def build_config(**kwargs: object) -> object:
        captured["node_config_args"] = kwargs
        return "node-config"

    def build_strategy(**kwargs: object) -> object:
        captured["strategy_args"] = kwargs
        return "strategy"

    def probe_server(readiness: object) -> FakeServer:
        captured["readiness"] = readiness
        return FakeServer()

    def install_signal_handlers(loop: asyncio.AbstractEventLoop, callback: object) -> tuple[()]:
        calls.append("signals")
        loop.call_soon(callback)
        return ()

    monkeypatch.setattr(root, "TradingNode", FakeNode)
    monkeypatch.setattr(root, "NautilusDatabaseBridge", FakeBridge)
    monkeypatch.setattr(root, "build_node_config", build_config)
    monkeypatch.setattr(root, "TracefoldNautilusStrategy", build_strategy)
    monkeypatch.setattr(root, "_probe_server", probe_server)
    monkeypatch.setattr(
        root,
        "installed_nautilus_wheel_identity",
        lambda: "cp313-cp313-manylinux_2_35_aarch64@sha256:e536",
    )
    monkeypatch.setattr(root, "_install_signal_handlers", install_signal_handlers)
    frozen_snapshot = SimpleNamespace(
        included={"SOLUSDT-PERP.BINANCE": object(), "BTCUSDT-PERP.BINANCE": object()},
        snapshot_sha256="c" * 64,
    )
    capability_snapshot = None if snapshot_missing else frozen_snapshot

    @contextmanager
    def fake_repositories(*_args: object, **_kwargs: object):
        yield SimpleNamespace(
            trading=SimpleNamespace(
                active_execution_capability_snapshot=lambda: capability_snapshot,
                nautilus_runtime_state=lambda: {"control": "PAUSED"},
                active_intent=lambda: None,
            )
        )

    monkeypatch.setattr(root, "repositories", fake_repositories)
    monkeypatch.setattr(
        root,
        "runtime_identity",
        lambda: SimpleNamespace(runtime_revision="rev-1", image_digest="image-1"),
    )

    root.run_nautilus(settings, bootstrap_zero_claims=forced_bootstrap)

    expected_instruments = (
        []
        if snapshot_missing or forced_bootstrap
        else [
            root.InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            root.InstrumentId.from_str("SOLUSDT-PERP.BINANCE"),
        ]
    )
    assert captured["node_config_args"] == {
        "api_key": "demo-key",
        "api_secret": "demo-secret",
        "instrument_ids": expected_instruments,
    }
    assert captured["bridge_settings"] is settings
    assert captured["bridge_capability_snapshot_sha256"] == (None if snapshot_missing else "c" * 64)
    assert captured["data_factory"] == (root.BINANCE, root.BinanceLiveDataClientFactory)
    assert captured["exec_factory"] == (root.BINANCE, root.BinanceLiveExecClientFactory)
    strategy_args = captured["strategy_args"]
    assert isinstance(strategy_args, dict)
    assert strategy_args["queues"] is captured["queues"]
    assert strategy_args["instrument_ids"] == captured["node_config_args"]["instrument_ids"]
    assert strategy_args["capabilities"] == ({} if snapshot_missing or forced_bootstrap else frozen_snapshot.included)
    assert callable(strategy_args["request_venue_flat"])
    assert callable(strategy_args["request_startup_account_reconciliation"])
    assert captured["readiness"].__self__.__class__ is FakeBridge  # type: ignore[union-attr]
    assert "demo-key" not in str(strategy_args["engine_identity"])
    assert "image-1" in str(strategy_args["engine_identity"])
    assert root.NAUTILUS_RELEASE.git_commit in str(strategy_args["engine_identity"])
    assert "wheel@cp313-cp313-manylinux_2_35_aarch64@sha256:e536" in str(strategy_args["engine_identity"])
    assert "config@" in str(strategy_args["engine_identity"])
    assert root.INTENT_POLICY_SHA256 in str(strategy_args["engine_identity"])
    assert calls.index("build") < calls.index("database-start") < calls.index("run")
    assert calls.index("probe-stop") < calls.index("dispose")
    assert calls.index("database-stop") < calls.index("database-join") < calls.index("dispose")
    assert captured["loop"].is_closed() is True  # type: ignore[union-attr]


def test_nautilus_probe_is_bound_to_its_one_internal_port() -> None:
    from tracefold.app.nautilus import root

    server = root._probe_server(lambda: {"ok": False})

    assert server.config.port == 8767


def test_nautilus_root_rejects_missing_credentials_before_constructing_the_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tracefold.app.nautilus import root

    settings = Settings()
    settings.set_config_dir(tmp_path)
    monkeypatch.setattr(root, "TradingNode", lambda *args, **kwargs: pytest.fail("node constructed"))

    with pytest.raises(ValueError, match=r"^nautilus_api_key_file_missing$"):
        root.run_nautilus(settings)


@pytest.mark.parametrize(
    ("runtime", "active_intent", "error"),
    [
        ({"control": "RUNNING"}, None, "nautilus_bootstrap_requires_paused"),
        ({"control": "PAUSED"}, object(), "nautilus_bootstrap_requires_no_active_intent"),
    ],
)
def test_zero_claim_recovery_refuses_live_control_or_an_active_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: dict[str, str],
    active_intent: object | None,
    error: str,
) -> None:
    from tracefold.app.nautilus import root

    settings = Settings()
    settings.set_config_dir(tmp_path)
    _secure_secret(tmp_path / "binance_demo_api_key", "demo-key")
    _secure_secret(tmp_path / "binance_demo_api_secret", "demo-secret")

    @contextmanager
    def fake_repositories(*_args: object, **_kwargs: object):
        yield SimpleNamespace(
            trading=SimpleNamespace(
                active_execution_capability_snapshot=lambda: SimpleNamespace(snapshot_sha256="c" * 64),
                nautilus_runtime_state=lambda: runtime,
                active_intent=lambda: active_intent,
            )
        )

    monkeypatch.setattr(root, "repositories", fake_repositories)
    monkeypatch.setattr(root, "TradingNode", lambda *args, **kwargs: pytest.fail("node constructed"))

    with pytest.raises(RuntimeError, match=rf"^{error}$"):
        root.run_nautilus(settings, bootstrap_zero_claims=True)


def test_account_wide_positions_and_orders_are_reconciled_before_flat_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tracefold.app.nautilus import root
    from tracefold.integrations.nautilus.messages import (
        VenueFlatConfirmed,
        VenueFlatProofRequested,
        strategy_queues,
    )

    instrument_id = root.InstrumentId.from_str("SOLUSDT-PERP.BINANCE")
    venue_account_id = SimpleNamespace(value="BINANCE-001")
    closing_order = SimpleNamespace(instrument_id=instrument_id)

    class FakeClient:
        account_id = venue_account_id

    async def complete_reports(_client: object) -> tuple[list[object], list[object]]:
        return [], []

    monkeypatch.setattr(root, "load_complete_account_reports", complete_reports)

    class FakeExecutionEngine:
        def __init__(self) -> None:
            self.reconciled: list[object] = []

        def get_clients_for_orders(self, orders: list[object]) -> set[object]:
            assert orders == [closing_order]
            return {FakeClient()}

        def reconcile_execution_report(self, supplied_report: object) -> bool:
            self.reconciled.append(supplied_report)
            return True

    engine = FakeExecutionEngine()
    node = SimpleNamespace(
        cache=SimpleNamespace(order=lambda _client_order_id: closing_order),
        kernel=SimpleNamespace(
            exec_engine=engine,
            clock=SimpleNamespace(timestamp_ns=lambda: 1_900_000_000_600_000_000),
        ),
    )
    queues = strategy_queues()
    request = VenueFlatProofRequested(
        intent_id="a" * 64,
        instrument_id=instrument_id.value,
        account_id=venue_account_id.value,
        position_id="position-1",
        closing_client_order_id="tf-c-owned",
        observed_at_ms=1_900_000_000_000,
    )

    asyncio.run(root._run_venue_flat_proof(node=node, queues=queues, request=request))

    assert engine.reconciled == []
    assert queues.commands.get_nowait() == VenueFlatConfirmed(
        intent_id=request.intent_id,
        instrument_id=request.instrument_id,
        position_id=request.position_id,
        authoritative_quantity=Decimal(0),
        verified_at_ms=1_900_000_000_600,
        account_wide_zero=True,
    )


def test_nonzero_account_position_report_never_confirms_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.app.nautilus import root
    from tracefold.integrations.nautilus.messages import (
        VenueFlatProofRequested,
        VenueFlatUnproven,
        strategy_queues,
    )

    instrument_id = root.InstrumentId.from_str("SOLUSDT-PERP.BINANCE")
    venue_account_id = SimpleNamespace(value="BINANCE-001")
    closing_order = SimpleNamespace(instrument_id=instrument_id)
    report = SimpleNamespace(
        instrument_id=instrument_id,
        account_id=venue_account_id,
        position_side=PositionSide.LONG,
        quantity=SimpleNamespace(as_decimal=lambda: Decimal("0.001")),
        ts_last=1_900_000_000_500_000_000,
    )

    class FakeClient:
        account_id = venue_account_id

    async def complete_reports(_client: object) -> tuple[list[object], list[object]]:
        return [report], []

    monkeypatch.setattr(root, "load_complete_account_reports", complete_reports)

    engine = SimpleNamespace(
        get_clients_for_orders=lambda _orders: {FakeClient()},
        reconcile_execution_report=lambda _report: True,
    )
    node = SimpleNamespace(
        cache=SimpleNamespace(order=lambda _client_order_id: closing_order),
        kernel=SimpleNamespace(
            exec_engine=engine,
            clock=SimpleNamespace(timestamp_ns=lambda: 1_900_000_000_600_000_000),
        ),
    )
    queues = strategy_queues()
    request = VenueFlatProofRequested(
        intent_id="a" * 64,
        instrument_id=instrument_id.value,
        account_id=venue_account_id.value,
        position_id="position-1",
        closing_client_order_id="tf-c-owned",
        observed_at_ms=1_900_000_000_000,
    )

    asyncio.run(root._run_venue_flat_proof(node=node, queues=queues, request=request))

    assert queues.commands.get_nowait() == VenueFlatUnproven(
        intent_id=request.intent_id,
        position_id=request.position_id,
        observed_at_ms=request.observed_at_ms,
    )


def test_owned_account_open_order_requires_retirement_and_a_second_zero_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tracefold.app.nautilus import root
    from tracefold.integrations.nautilus.messages import (
        VenueFlatConfirmed,
        VenueFlatProofRequested,
        strategy_queues,
    )

    instrument_id = root.InstrumentId.from_str("SOLUSDT-PERP.BINANCE")
    venue_account_id = SimpleNamespace(value="BINANCE-001")
    closing_order = SimpleNamespace(instrument_id=instrument_id)
    order_report = SimpleNamespace(
        account_id=venue_account_id,
        client_order_id=SimpleNamespace(value="tf-stop-owned"),
    )

    class FakeClient:
        account_id = venue_account_id

    async def complete_reports(_client: object) -> tuple[list[object], list[object]]:
        return [], [order_report]

    monkeypatch.setattr(root, "load_complete_account_reports", complete_reports)

    engine = SimpleNamespace(
        get_clients_for_orders=lambda _orders: {FakeClient()},
        reconcile_execution_report=lambda supplied: supplied is order_report,
    )
    node = SimpleNamespace(
        cache=SimpleNamespace(order=lambda _client_order_id: closing_order),
        kernel=SimpleNamespace(
            exec_engine=engine,
            clock=SimpleNamespace(timestamp_ns=lambda: 1_900_000_000_600_000_000),
        ),
    )
    queues = strategy_queues()
    request = VenueFlatProofRequested(
        intent_id="a" * 64,
        instrument_id=instrument_id.value,
        account_id=venue_account_id.value,
        position_id="position-1",
        closing_client_order_id="tf-c-owned",
        observed_at_ms=1_900_000_000_000,
        owned_open_order_ids=("tf-stop-owned",),
    )

    asyncio.run(root._run_venue_flat_proof(node=node, queues=queues, request=request))

    assert queues.commands.get_nowait() == VenueFlatConfirmed(
        intent_id=request.intent_id,
        instrument_id=request.instrument_id,
        position_id=request.position_id,
        authoritative_quantity=Decimal(0),
        verified_at_ms=1_900_000_000_600,
        account_wide_zero=False,
    )


def test_unowned_account_open_order_never_confirms_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.app.nautilus import root
    from tracefold.integrations.nautilus.messages import (
        VenueFlatProofRequested,
        VenueFlatUnproven,
        strategy_queues,
    )

    instrument_id = root.InstrumentId.from_str("SOLUSDT-PERP.BINANCE")
    venue_account_id = SimpleNamespace(value="BINANCE-001")
    closing_order = SimpleNamespace(instrument_id=instrument_id)
    order_report = SimpleNamespace(
        account_id=venue_account_id,
        client_order_id=SimpleNamespace(value="external-order"),
    )

    class FakeClient:
        account_id = venue_account_id

    async def complete_reports(_client: object) -> tuple[list[object], list[object]]:
        return [], [order_report]

    monkeypatch.setattr(root, "load_complete_account_reports", complete_reports)

    engine = SimpleNamespace(
        get_clients_for_orders=lambda _orders: {FakeClient()},
        reconcile_execution_report=lambda _report: True,
    )
    node = SimpleNamespace(
        cache=SimpleNamespace(order=lambda _client_order_id: closing_order),
        kernel=SimpleNamespace(
            exec_engine=engine,
            clock=SimpleNamespace(timestamp_ns=lambda: 1_900_000_000_600_000_000),
        ),
    )
    queues = strategy_queues()
    request = VenueFlatProofRequested(
        intent_id="a" * 64,
        instrument_id=instrument_id.value,
        account_id=venue_account_id.value,
        position_id="position-1",
        closing_client_order_id="tf-c-owned",
        observed_at_ms=1_900_000_000_000,
        owned_open_order_ids=("tf-stop-owned",),
    )

    asyncio.run(root._run_venue_flat_proof(node=node, queues=queues, request=request))

    assert queues.commands.get_nowait() == VenueFlatUnproven(
        intent_id=request.intent_id,
        position_id=request.position_id,
        observed_at_ms=request.observed_at_ms,
    )
