"""The Runtime composition root: routes, profile, projection, probe and the generation supervisor."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from pydantic import ValidationError

from tests.nautilus_oi_runtime_fixtures import oi_profile
from tracefold.app.nautilus import root as nautilus_root
from tracefold.app.nautilus.oi_runtime import RuntimeStateProjector
from tracefold.app.nautilus.root import RuntimeFatal, _discover_routes, _probe_payload, _risk_limits
from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials, _trader_id, route_catalogue
from tracefold.integrations.nautilus.oi_runtime.entry import deterministic_client_order_id
from tracefold.integrations.nautilus.oi_runtime.strategy import oi_strategy_config
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState


def test_recovery_horizon_starts_with_no_open_plans_and_covers_longer_existing_plans() -> None:
    now_ns = 1_000_000
    assert nautilus_root._recovery_max_holding_ns(86_400, (), now_ns) == 86_400
    plans = (
        SimpleNamespace(max_holding_ns=3_600, created_at_ns=now_ns - 172_800),
        SimpleNamespace(max_holding_ns=200_000, created_at_ns=now_ns - 100),
    )
    assert nautilus_root._recovery_max_holding_ns(86_400, plans[:1], now_ns) == 172_800
    assert nautilus_root._recovery_max_holding_ns(86_400, plans, now_ns) == 200_000


def _perpetual(base: str, *, contract_type: str = "PERPETUAL", status: str = "TRADING") -> CryptoPerpetual:
    values = CryptoPerpetual.to_dict(TestInstrumentProvider.btcusdt_perp_binance())
    values.update(
        id=f"{base}USDT-PERP.BINANCE",
        raw_symbol=f"{base}USDT",
        base_currency=base,
        info={"status": status, "contractType": contract_type},
    )
    return CryptoPerpetual.from_dict(values)


def test_the_route_catalogue_is_binances_own_usdt_perpetuals_trading_now_and_never_tradfi() -> None:
    """#680 RC8. 29 `TRADIFI_PERPETUAL` stock and commodity contracts were routed; COIN got `-4411`."""

    routes = route_catalogue(
        [
            _perpetual("BTC"),
            _perpetual("COIN", contract_type="TRADIFI_PERPETUAL"),
            _perpetual("XAU", contract_type="TRADIFI_PERPETUAL"),
            _perpetual("ETH", status="PENDING_TRADING"),
            _perpetual("SOL", contract_type="PERPETUAL_DELIVERING"),
            _perpetual("测试测试"),
            TestInstrumentProvider.ethusdt_binance(),
        ],
        stop_distance_bps=100,
    )

    assert [route.market_key for route in routes] == ["crypto:perp:BTC:USDT"]
    assert [route.stop_distance_bps for route in routes] == [100]


def test_two_instruments_claiming_one_market_are_a_catalogue_that_cannot_be_routed() -> None:
    with pytest.raises(RuntimeError, match="oi_runtime_market_route_ambiguous"):
        route_catalogue([_perpetual("BTC"), _perpetual("BTC")], stop_distance_bps=100)


def test_route_discovery_uses_nautilus_own_provider_on_the_selected_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Provider:
        def __init__(self, **kwargs: Any) -> None:
            captured["provider"] = kwargs

        async def load_all_async(self) -> None:
            captured["loaded"] = True

        def list_all(self) -> list[CryptoPerpetual]:
            return [_perpetual("BTC"), _perpetual("NVDA", contract_type="TRADIFI_PERPETUAL")]

    monkeypatch.setattr(
        nautilus_root, "get_cached_binance_http_client", lambda **kwargs: captured.setdefault("client", kwargs)
    )
    monkeypatch.setattr(nautilus_root, "BinanceFuturesInstrumentProvider", _Provider)

    routes = asyncio.run(
        _discover_routes(
            BinanceEnvironment.DEMO, BinanceRuntimeCredentials("demo-key", "demo-secret"), stop_distance_bps=100
        )
    )

    assert captured["loaded"] is True
    assert captured["client"]["environment"] is BinanceEnvironment.DEMO
    assert [route.market_key for route in routes] == ["crypto:perp:BTC:USDT"]


def test_an_empty_catalogue_fails_the_generation_not_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Provider:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def load_all_async(self) -> None:
            return None

        def list_all(self) -> list[Any]:
            return []

    monkeypatch.setattr(nautilus_root, "get_cached_binance_http_client", lambda **kwargs: kwargs)
    monkeypatch.setattr(nautilus_root, "BinanceFuturesInstrumentProvider", _Provider)
    with pytest.raises(RuntimeError, match="oi_runtime_route_catalog_empty") as raised:
        asyncio.run(
            _discover_routes(BinanceEnvironment.DEMO, BinanceRuntimeCredentials("k", "s"), stop_distance_bps=100)
        )
    assert not isinstance(raised.value, RuntimeFatal)


def _settings_with_risk(**overrides: Any) -> Settings:
    return Settings(trading={"execution": {"enabled": True, "binance": {"environment": "DEMO"}, "risk": overrides}})


def test_risk_limits_come_from_the_operator_config() -> None:
    default = _risk_limits(Settings())

    assert default.risk_fraction_per_trade == Decimal("0.01")
    assert default.max_leverage == 1
    assert default.max_spread_fraction_of_stop == Decimal("0.3")
    assert default.post_stop_cooldown_ns == 14_400_000_000_000
    assert default.market_stale_after_ns == 5_000_000_000
    assert Settings().trading.execution.risk.stop_distance_bps == 100

    edited = _risk_limits(_settings_with_risk(max_spread_fraction_of_stop="0.5", post_stop_cooldown_seconds=0))
    assert edited.max_spread_fraction_of_stop == Decimal("0.5")
    assert edited.post_stop_cooldown_ns == 0


@pytest.mark.parametrize(
    "retired",
    [
        "max_total_risk_usd",
        "reconciliation_interval_seconds",
        "max_risk_per_trade_usd",
        "max_positions",
        "max_daily_loss_usd",
    ],
)
def test_the_retired_risk_keys_are_refused_by_name(retired: str) -> None:
    """Removed execution limits cannot silently reappear as no-op operator settings."""

    with pytest.raises(ValidationError, match=retired):
        _settings_with_risk(**{retired: 5})


@pytest.mark.parametrize(
    "override",
    [
        {"risk_fraction_per_trade": "0.02"},
        {"max_leverage": 2},
        {"stop_distance_bps": 120},
        {"max_spread_fraction_of_stop": "0.2"},
        {"post_stop_cooldown_seconds": 60},
        {"market_stale_after_seconds": 7.0},
    ],
)
def test_every_risk_value_reaches_the_runtime_policy_without_renaming_the_account(override: dict[str, Any]) -> None:
    routes = oi_profile().routes
    baseline = nautilus_root._active_profile(Settings(), routes)
    edited = nautilus_root._active_profile(_settings_with_risk(**override), routes)

    assert edited.account_slot == baseline.account_slot
    assert edited.namespace == baseline.namespace
    if "stop_distance_bps" not in override:
        assert edited.risk != baseline.risk


# The exact venue-visible identity strings preserved for the historical namespaces. The deterministic
# entry, stop and take-profit ids are how a restarted Runtime recognizes its own orders on the venue.
_PINNED_ENTRY_ID = "e" * 64
_PINNED_IDENTITY = {
    "paper": {
        "trader_id": "OI-F46FB62A731F",
        "order_id_tag": "F46",
        "entry": "tf80c234dfddf49bb7dae54e6e54940c",
        "stop": "tf5f89700146d29ff4c7dd53541e8efc",
        "take_profit": "tf4cacf4cde4f8c30ec87f2c0fec6535",
    },
    "live": {
        "trader_id": "OI-436C67334FF6",
        "order_id_tag": "436",
        "entry": "tf62ab44161ae7839b91694b6f7ee798",
        "stop": "tfcb1414c4798006b5b2ecd8a0e57b70",
        "take_profit": "tf46b6e068ca7ccbd3c87fe01299cf5f",
    },
}


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_the_runtime_namespace_produces_exactly_these_venue_visible_identities(mode: str) -> None:
    profile = nautilus_root._active_profile(
        Settings(
            trading={
                "execution": {"enabled": True, "exit_policy": {"take_profit_bps": 200, "max_holding_seconds": 14400}}
            }
        ),
        oi_profile().routes,
        namespace=f"tracefold:binance_usdm_primary:{mode}",
    )
    expected = _PINNED_IDENTITY[mode]

    assert profile.namespace == f"tracefold:binance_usdm_primary:{mode}"
    assert _trader_id(profile).value == expected["trader_id"]
    assert oi_strategy_config(profile).order_id_tag == expected["order_id_tag"]
    for leg in ("entry", "stop", "take_profit"):
        derived = deterministic_client_order_id(namespace=profile.namespace, entry_id=_PINNED_ENTRY_ID, leg=leg)
        assert derived.value == expected[leg]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"risk_fraction_per_trade": "0"}, "trading_execution_risk_fraction_invalid"),
        ({"risk_fraction_per_trade": "0.2"}, "trading_execution_risk_fraction_invalid"),
        ({"max_leverage": 0}, "trading_execution_max_leverage_invalid"),
        ({"max_leverage": 125}, "trading_execution_max_leverage_invalid"),
        ({"stop_distance_bps": 0}, "trading_execution_stop_distance_invalid"),
        ({"stop_distance_bps": 6_000}, "trading_execution_stop_distance_invalid"),
        ({"max_spread_fraction_of_stop": "0"}, "trading_execution_max_spread_invalid"),
        ({"max_spread_fraction_of_stop": "1.5"}, "trading_execution_max_spread_invalid"),
        ({"post_stop_cooldown_seconds": -1}, "trading_execution_post_stop_cooldown_invalid"),
        ({"market_stale_after_seconds": 0.5}, "trading_execution_market_stale_invalid"),
    ],
)
def test_risk_bounds_refuse_the_values_that_would_make_a_limit_stop_being_one(
    override: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValidationError, match=reason):
        _settings_with_risk(**override)


def test_exit_defaults_are_shared_by_all_connections() -> None:
    paper = nautilus_root._active_profile(Settings(), oi_profile().routes)
    assert paper.exit_policy.take_profit_bps == 200
    assert paper.exit_policy.max_holding_ns == 14_400_000_000_000
    # Nautilus reconciles at least a day of history, and always more than the longest holding time.
    assert paper.reconciliation_lookback_mins == 1_500
    long_hold = replace(paper, exit_policy=replace(paper.exit_policy, max_holding_ns=72 * 3_600_000_000_000))
    assert long_hold.reconciliation_lookback_mins == 72 * 60 + 60
    assert nautilus_root._active_profile(Settings(), oi_profile().routes).exit_policy == paper.exit_policy


def _runtime_state(*, heartbeat_at_ns: int = 1_000_000_000) -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        connection="DEMO",
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        alive=True,
        entries_armed=False,
        unexpected_exposure=False,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        heartbeat_at_ns=heartbeat_at_ns,
        entry_block_reason="runtime_starting",
        started_at_ns=heartbeat_at_ns,
        updated_at_ns=heartbeat_at_ns,
    )


class _ProjectionTrading:
    def __init__(self) -> None:
        self.puts: list[ExecutionRuntimeState] = []
        self.updates: list[ExecutionRuntimeState] = []

    def put_execution_runtime_state(self, state: ExecutionRuntimeState) -> None:
        self.puts.append(state)

    def update_execution_runtime_state(self, state: ExecutionRuntimeState) -> bool:
        self.updates.append(state)
        return True


def test_the_projector_writes_a_change_immediately_and_an_unchanged_row_only_on_the_heartbeat() -> None:
    trading = _ProjectionTrading()
    repos = SimpleNamespace(trading=trading, transaction=nullcontext)
    starting = _runtime_state()
    projector = RuntimeStateProjector(initial=starting)
    projector.start(repos)  # type: ignore[arg-type]

    def beat(state: ExecutionRuntimeState, after_ns: int, **changes: Any) -> ExecutionRuntimeState:
        at_ns = state.heartbeat_at_ns + after_ns
        return replace(state, heartbeat_at_ns=at_ns, updated_at_ns=at_ns, **changes)

    changed = beat(starting, 1, entry_block_reason="entries_paused")
    projector.offer(changed)
    projector.write_once(repos)  # type: ignore[arg-type]
    projector.offer(beat(changed, 100_000_000))
    projector.write_once(repos)  # type: ignore[arg-type]
    heartbeat = beat(changed, 500_000_000)
    projector.offer(heartbeat)
    projector.write_once(repos)  # type: ignore[arg-type]
    projector.write_once(repos)  # type: ignore[arg-type]

    assert trading.puts == [starting]
    assert trading.updates == [changed, heartbeat]


def test_projector_keeps_a_refused_or_failed_candidate_until_it_is_durable() -> None:
    trading = _ProjectionTrading()
    repos = SimpleNamespace(trading=trading, transaction=nullcontext)
    starting = _runtime_state()
    projector = RuntimeStateProjector(initial=starting)
    projector.start(repos)  # type: ignore[arg-type]
    candidate = replace(
        starting,
        heartbeat_at_ns=starting.heartbeat_at_ns + 1,
        updated_at_ns=starting.updated_at_ns + 1,
        entry_block_reason="entries_paused",
    )
    projector.offer(candidate)
    original = trading.update_execution_runtime_state
    trading.update_execution_runtime_state = lambda _state: False  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="generation_fenced"):
        projector.write_once(repos)  # type: ignore[arg-type]
    assert projector.current == starting
    trading.update_execution_runtime_state = original  # type: ignore[method-assign]
    projector.write_once(repos)  # type: ignore[arg-type]
    assert projector.current == candidate


def test_the_probe_states_what_an_operator_acts_on_and_always_answers_200() -> None:
    paused = replace(_runtime_state(), entry_block_reason="entries_paused")
    payload = _probe_payload(paused)

    assert payload["ok"] is True and payload["entries_armed"] is False
    assert set(payload) == {
        "ok",
        "alive",
        "entries_armed",
        "entry_block_reason",
        "connection",
        "account_slot",
        "unexpected_exposure",
        "positions_count",
        "open_orders_count",
        "protection_status",
        "heartbeat_at_ns",
        "runtime_id",
        "started_at_ns",
        "account_snapshot",
        "account_projection_failure",
        "convergence_checked_at_ns",
        "convergence_failure",
        "venue_read_started_at_ns",
        "venue_read_completed_at_ns",
        "venue_read_failure",
        "recovery_attempted_at_ns",
        "recovery_result",
    }
    starting = nautilus_root._ProbeState.starting(connection="DEMO", account_slot="binance_usdm_primary").readiness()
    assert set(starting) == set(payload) | {"process_started_at_ns"}
    assert starting["process_started_at_ns"] > 0
    client = TestClient(nautilus_root._probe_server(lambda: starting).config.app)
    response = client.get("/readyz")
    assert response.status_code == 200 and response.json() == starting
    assert client.get("/healthz").text == "ok\n"


def test_generation_cannot_rebuild_while_the_old_writer_is_still_alive() -> None:
    class _Node:
        def is_running(self) -> bool:
            return False

        def dispose(self) -> None:
            pass

    class _Writer:
        def stop(self) -> None:
            pass

        def join(self, _timeout: float) -> None:
            raise RuntimeError("oi_runtime_state_writer_shutdown_timeout")

    async def run() -> None:
        node_task = asyncio.create_task(asyncio.sleep(0))
        with pytest.raises(RuntimeFatal, match="state_writer_shutdown_timeout"):
            await nautilus_root._shutdown_generation(
                node=_Node(),  # type: ignore[arg-type]
                node_task=node_task,
                bridge=None,
                projector=None,
                writer=_Writer(),  # type: ignore[arg-type]
                singleton=SimpleNamespace(acquired=True),  # type: ignore[arg-type]
            )

    asyncio.run(run())


def _supervise(monkeypatch: pytest.MonkeyPatch, outcomes: list[BaseException | None]) -> list[int]:
    """Run the supervisor over scripted generations; `None` is a generation that ends on a stop request."""

    attempts: list[int] = []

    async def generation(*, stop: asyncio.Event, **_kwargs: Any) -> None:
        attempts.append(len(attempts))
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        stop.set()

    class _Server:
        should_exit = False

        async def serve(self) -> None:
            return None

    monkeypatch.setattr(nautilus_root, "_run_generation", generation)
    monkeypatch.setattr(nautilus_root, "_probe_server", lambda _readiness: _Server())
    monkeypatch.setattr(nautilus_root, "_REBUILD_BACKOFF_SECONDS", (0.0,))
    asyncio.run(
        nautilus_root._run_active_runtime(
            settings=Settings(trading={"execution": {"enabled": True, "binance": {"environment": "DEMO"}}}),
            environment=BinanceEnvironment.DEMO,
            namespace="tracefold:binance_usdm_primary:paper",
            credentials=BinanceRuntimeCredentials("k", "s"),
            singleton=SimpleNamespace(acquired=True),  # type: ignore[arg-type]
            repos=SimpleNamespace(),  # type: ignore[arg-type]
        )
    )
    return attempts


def test_a_transient_failure_rebuilds_the_generation_inside_the_same_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """#680 RC1: 322 of 384 restarts were a TLS EOF on a REST call that a retry would have survived."""

    attempts = _supervise(
        monkeypatch,
        [
            OSError("TLS close_notify EOF"),
            nautilus_root._GenerationFailed("oi_runtime_start_timeout"),
            None,
        ],
    )
    assert attempts == [0, 1, 2]


def test_losing_the_account_slot_stops_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeFatal, match="oi_runtime_account_slot_lost"):
        _supervise(monkeypatch, [RuntimeFatal("oi_runtime_account_slot_lost")])
