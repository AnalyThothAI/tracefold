from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.identifiers import InstrumentId
from pydantic import ValidationError

from tests.nautilus_oi_runtime_fixtures import oi_profile
from tracefold.app.nautilus import root as nautilus_root
from tracefold.app.nautilus.oi_runtime import RuntimeStateProjector
from tracefold.app.nautilus.root import (
    _discover_routes,
    _observe_reconciliation,
    _PrivateReconciliationRequests,
    _PrivateReconciliationResult,
    _probe_payload,
    _reconcile_account,
    _risk_limits,
)
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials
from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import CompleteBinanceAccountReports
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState


def _repos(trading: Any) -> Any:
    return SimpleNamespace(trading=trading, transaction=nullcontext)


def _complete_reports(
    *,
    positions: tuple[Any, ...] = (),
    regular_orders: tuple[Any, ...] = (),
    algo_orders: tuple[Any, ...] = (),
) -> CompleteBinanceAccountReports:
    return CompleteBinanceAccountReports(
        positions=positions,
        regular_orders=regular_orders,
        algo_orders=algo_orders,
    )


def _runtime_state(*, heartbeat_at_ns: int = 1_000_000_000) -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        mode="paper",
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        alive=True,
        execution_safe=False,
        entries_armed=False,
        startup_reconciled=False,
        unexpected_exposure=False,
        account_flat=True,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        reconciliation_observed_at_ns=heartbeat_at_ns,
        heartbeat_at_ns=heartbeat_at_ns,
        entry_block_reason="runtime_starting",
        started_at_ns=heartbeat_at_ns,
        updated_at_ns=heartbeat_at_ns,
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
            stop_distance_bps=Settings().trading.execution.risk.stop_distance_bps,
        )
    )

    assert captured["loaded"] is True
    assert captured["client"]["environment"] is BinanceEnvironment.DEMO
    assert type(captured["client"]["clock"]).__name__ == "LiveClock"
    assert [route.market_key for route in routes] == ["crypto:perp:BTC:USDT"]
    assert [route.stop_distance_bps for route in routes] == [100]


def test_one_reconciliation_period_owns_both_account_freshness_budgets() -> None:
    """#510 B. Production ran `account_stale_after_ns` at exactly one reconciliation period.

    The account clock is only ever as fresh as the last private scan, so a budget equal to the period
    is expired for the tail of every cycle by construction: `evaluate_entry` returned
    `halt("account_stale")` for five of 2026-09-02's six Signals. There is now one input, and both
    budgets are multiples of it, so the relation cannot be edited apart.
    """

    risk = oi_profile().risk
    production = _risk_limits(Settings())

    assert risk.reconciliation_interval_seconds == 5.0
    assert risk.account_stale_after_ns == 2 * risk.reconciliation_interval_ns
    assert risk.reconciliation_stale_after_ns == 3 * risk.reconciliation_interval_ns
    assert production.account_stale_after_ns > production.reconciliation_interval_ns
    assert production.reconciliation_stale_after_ns > production.account_stale_after_ns
    # Market freshness is a quote-stream fact and stays its own operator number.
    assert production.market_stale_after_ns == 5_000_000_000


def test_private_reconciliation_requests_wake_immediately_and_coalesce_duplicate_reasons() -> None:
    async def exercise() -> tuple[tuple[str, ...], tuple[str, ...]]:
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        requests = _PrivateReconciliationRequests(loop=loop, wake=wake)

        requests.request("unknown_outcome")
        requests.request("unknown_outcome")
        requests.request("flatten_pending")
        await asyncio.wait_for(wake.wait(), timeout=0.1)
        return requests.drain(), requests.drain()

    first, second = asyncio.run(exercise())

    assert first == ("flatten_pending", "unknown_outcome")
    assert second == ()


def test_startup_private_reconciliation_projects_complete_reports_and_records_the_fresh_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = SimpleNamespace(value="BINANCE-001")
    position = SimpleNamespace(account_id=account_id)
    regular = SimpleNamespace(account_id=account_id)
    algo = SimpleNamespace(account_id=account_id)
    reports = _complete_reports(
        positions=(position,),
        regular_orders=(regular,),
        algo_orders=(algo,),
    )

    async def load(_client: Any) -> CompleteBinanceAccountReports:
        return reports

    projected: list[Any] = []
    monkeypatch.setattr(nautilus_root, "load_complete_binance_account_reports", load)
    node = SimpleNamespace(
        kernel=SimpleNamespace(
            exec_engine=SimpleNamespace(reconcile_execution_report=lambda report: projected.append(report) or True),
            clock=SimpleNamespace(timestamp_ns=lambda: 9_000),
        )
    )
    client = SimpleNamespace(account_id=account_id)

    result = asyncio.run(_reconcile_account(node=node, client=client, triggers=("startup",)))

    assert result.reports is reports
    assert result.triggers == ("startup",)
    assert result.observed_at_ns == 9_000
    assert result.duration_ns > 0
    assert projected == [position, regular, algo]


def test_private_report_failure_does_not_project_cache_or_mint_a_fresh_reconciliation_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_client: Any) -> CompleteBinanceAccountReports:
        raise RuntimeError("private-report-failed")

    projected: list[Any] = []
    clock_reads: list[bool] = []
    monkeypatch.setattr(nautilus_root, "load_complete_binance_account_reports", fail)
    node = SimpleNamespace(
        kernel=SimpleNamespace(
            exec_engine=SimpleNamespace(reconcile_execution_report=lambda report: projected.append(report) or True),
            clock=SimpleNamespace(timestamp_ns=lambda: clock_reads.append(True) or 9_000),
        )
    )

    with pytest.raises(RuntimeError, match="private-report-failed"):
        asyncio.run(
            _reconcile_account(
                node=node,
                client=SimpleNamespace(account_id=SimpleNamespace(value="BINANCE-001")),
                triggers=("steady",),
            )
        )

    assert projected == []
    assert clock_reads == []


class _ProjectionTrading:
    def __init__(self) -> None:
        self.puts: list[ExecutionRuntimeState] = []
        self.updates: list[ExecutionRuntimeState] = []

    def put_execution_runtime_state(self, state: ExecutionRuntimeState) -> None:
        self.puts.append(state)

    def update_execution_runtime_state(self, state: ExecutionRuntimeState) -> bool:
        self.updates.append(state)
        return True


def _projector(starting: ExecutionRuntimeState) -> RuntimeStateProjector:
    return RuntimeStateProjector(initial=starting, recovery_inputs=((), ()))


def test_runtime_state_projector_writes_changes_immediately_and_unchanged_state_only_on_heartbeat() -> None:
    """#510 PR-5b. The loop offers; only the bridge thread's connection writes."""

    trading = _ProjectionTrading()
    repos = _repos(trading)
    starting = _runtime_state()
    projector = _projector(starting)
    projector.start(repos)

    changed = replace(
        starting,
        heartbeat_at_ns=starting.heartbeat_at_ns + 1,
        entry_block_reason="reconciliation_stale",
        updated_at_ns=starting.updated_at_ns + 1,
    )
    projector.offer(changed)
    projector.write_once(repos)
    assert projector.current == changed

    before_heartbeat = replace(
        changed,
        heartbeat_at_ns=changed.heartbeat_at_ns + 100_000_000,
        updated_at_ns=changed.updated_at_ns + 100_000_000,
    )
    projector.offer(before_heartbeat)
    projector.write_once(repos)
    assert projector.current == changed

    heartbeat = replace(
        changed,
        heartbeat_at_ns=changed.heartbeat_at_ns + 500_000_000,
        updated_at_ns=changed.updated_at_ns + 500_000_000,
    )
    projector.offer(heartbeat)
    projector.write_once(repos)
    assert projector.current == heartbeat

    # Nothing offered since the last write is nothing to write.
    projector.write_once(repos)

    assert trading.puts == [starting]
    assert trading.updates == [changed, heartbeat]


def test_probe_readiness_requires_execution_safety_but_not_entry_arming() -> None:
    safe_but_paused = replace(
        _runtime_state(),
        execution_safe=True,
        entries_armed=False,
        startup_reconciled=True,
        entry_block_reason="entries_paused",
    )

    payload = _probe_payload(safe_but_paused)

    assert payload["ok"] is True
    assert payload["alive"] is True
    assert payload["execution_safe"] is True
    assert payload["entries_armed"] is False
    assert _probe_payload(replace(safe_but_paused, execution_safe=False))["ok"] is False
    # #537 PR-4. `/readyz` states only what an operator acts on. The build's release string, the
    # configuration digest, the image digest, the deployment revision and the credential fingerprint
    # were five of its fourteen keys and no reader -- healthcheck, page or command -- named one.
    assert set(payload) == {
        "ok",
        "alive",
        "execution_safe",
        "entries_armed",
        "entry_block_reason",
        "mode",
        "account_slot",
        "startup_reconciled",
        "unexpected_exposure",
        "account_flat",
        "positions_count",
        "open_orders_count",
        "protection_status",
        "heartbeat_at_ns",
    }
    assert set(nautilus_root._ProbeState.starting(oi_profile("paper")).readiness()) <= set(payload)


def test_reconciliation_observation_preserves_native_ids_and_flat_proof() -> None:
    audit = AuditSink(
        factory=ObservationFactory(
            account_slot="oi-paper-profile",
            execution_strategy="oi_nautilus_v1",
        )
    )
    position = SimpleNamespace(instrument_id=SimpleNamespace(value="BTCUSDT-PERP.BINANCE"))
    order = SimpleNamespace(
        client_order_id=SimpleNamespace(value="tf-client"),
        venue_order_id=SimpleNamespace(value="12345"),
        instrument_id=SimpleNamespace(value="BTCUSDT-PERP.BINANCE"),
    )

    _observe_reconciliation(
        audit=audit,
        result=_PrivateReconciliationResult(
            reports=_complete_reports(positions=(position,), regular_orders=(order,)),
            triggers=("steady",),
            observed_at_ns=1_000,
            duration_ns=2_000_000,
        ),
        previous_identity=None,
    )

    observation = audit.flush_once(lambda _values: None)[0]
    assert observation.normalized_kind == "reconciliation"
    assert observation.summary == {
        "source": "binance_private_api",
        "trigger": "steady",
        "duration_us": 2_000,
        "positions": 1,
        "regular_orders": 1,
        "algo_orders": 0,
        "orders": 1,
        "account_flat": False,
        "native_refs_truncated": False,
    }
    assert observation.native_identity_references == ("12345", "BTCUSDT-PERP.BINANCE", "tf-client")


def test_a_steady_reconciliation_that_changed_nothing_stays_out_of_the_ledger() -> None:
    """Current account state belongs in the projection; the ledger only carries the changes.

    The steady heartbeat wrote one observation every twelve seconds and was 6996 of the 7019 rows in
    the observation table (#510 E). The projection already carries `account_flat`, the counts, and
    `reconciliation_observed_at_ns`, refreshed every loop.
    """

    audit = AuditSink(
        factory=ObservationFactory(
            account_slot="oi-paper-profile",
            execution_strategy="oi_nautilus_v1",
        )
    )
    position = SimpleNamespace(
        instrument_id=SimpleNamespace(value="UNIUSDT-PERP.BINANCE"),
        quantity=SimpleNamespace(value="3"),
    )
    stop = SimpleNamespace(
        client_order_id=SimpleNamespace(value="tf0065f6482c5577533ba696da631582"),
        venue_order_id=SimpleNamespace(value="61742419"),
        instrument_id=SimpleNamespace(value="UNIUSDT-PERP.BINANCE"),
        order_status=SimpleNamespace(value="ACCEPTED"),
    )

    def steady(reports: CompleteBinanceAccountReports, observed_at_ns: int) -> _PrivateReconciliationResult:
        return _PrivateReconciliationResult(
            reports=reports,
            triggers=("steady",),
            observed_at_ns=observed_at_ns,
            duration_ns=2_000_000,
        )

    held = _complete_reports(positions=(position,), algo_orders=(stop,))
    first = _observe_reconciliation(audit=audit, result=steady(held, 1_000), previous_identity=None)
    second = _observe_reconciliation(audit=audit, result=steady(held, 2_000), previous_identity=first)
    third = _observe_reconciliation(audit=audit, result=steady(held, 3_000), previous_identity=second)

    assert first == second == third
    assert audit.queued_count == 1

    flat = _complete_reports()
    fourth = _observe_reconciliation(audit=audit, result=steady(flat, 4_000), previous_identity=third)

    assert fourth != third
    assert audit.queued_count == 2

    _observe_reconciliation(audit=audit, result=steady(flat, 5_000), previous_identity=fourth)

    assert audit.queued_count == 2

    _observe_reconciliation(
        audit=audit,
        result=_PrivateReconciliationResult(
            reports=flat,
            triggers=("unexpected_exposure",),
            observed_at_ns=6_000,
            duration_ns=2_000_000,
        ),
        previous_identity=fourth,
    )

    assert audit.queued_count == 3
    observed = audit.flush_once(lambda _values: None)
    assert [value.summary["trigger"] for value in observed] == ["steady", "steady", "unexpected_exposure"]
    assert [value.summary["positions"] for value in observed] == [1, 0, 0]


def _settings_with_risk(**overrides: Any) -> Settings:
    return Settings(trading={"execution": {"mode": "paper", "risk": overrides}})


def test_risk_limits_come_from_the_operator_config_and_carry_the_route_stop_distance() -> None:
    """#510 E. Every one of these was a literal in `root.py`, invisible to `tracefold config`."""

    default = _risk_limits(Settings())

    assert default.risk_fraction_per_trade == Decimal("0.01")
    assert default.max_risk_per_trade_usd == Decimal("10")
    assert default.max_total_risk_usd == Decimal("25")
    assert (default.max_positions, default.max_leverage) == (1, 1)
    assert default.max_daily_loss_usd == Decimal("25")
    assert default.market_stale_after_ns == 5_000_000_000
    assert default.reconciliation_interval_ns == 5_000_000_000
    assert Settings().trading.execution.risk.stop_distance_bps == 100

    edited = _risk_limits(_settings_with_risk(max_positions=3, reconciliation_interval_seconds=8.0))

    assert edited.max_positions == 3
    assert edited.reconciliation_interval_ns == 8_000_000_000
    # The two derived account clocks follow the one operator input, as #510 PR-2 established.
    assert edited.account_stale_after_ns == 16_000_000_000
    assert edited.reconciliation_stale_after_ns == 24_000_000_000


@pytest.mark.parametrize(
    "override",
    [
        {"risk_fraction_per_trade": "0.02"},
        {"max_risk_per_trade_usd": "9"},
        {"max_total_risk_usd": "30"},
        {"max_positions": 2},
        {"max_leverage": 2},
        {"max_daily_loss_usd": "40"},
        {"stop_distance_bps": 120},
        {"reconciliation_interval_seconds": 6.0},
        {"market_stale_after_seconds": 7.0},
    ],
)
def test_every_risk_value_reaches_the_runtime_policy_without_renaming_the_account(
    override: dict[str, Any],
) -> None:
    """#537 PR-4. An operator risk edit changes what the Runtime enforces and nothing else.

    It used to also change `config_sha256`, a digest of the whole configuration that rode on the
    durable projection and on a start receipt no reader ever opened; the one mechanism that consumed
    it was the Nautilus instance id, which then made every risk edit a new Cache namespace. The
    account slot, the mode and the client order namespace are what identity means here, and none of
    them may move when a risk number does.
    """

    routes = oi_profile().routes
    baseline = nautilus_root._active_profile(Settings(trading={"execution": {"mode": "paper"}}), "paper", routes)
    edited = nautilus_root._active_profile(_settings_with_risk(**override), "paper", routes)

    assert edited.account_slot == baseline.account_slot
    assert edited.client_order_namespace == baseline.client_order_namespace
    assert edited.cache_namespace == baseline.cache_namespace
    if "stop_distance_bps" not in override:
        assert edited.risk != baseline.risk


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"risk_fraction_per_trade": "0"}, "trading_execution_risk_fraction_invalid"),
        ({"risk_fraction_per_trade": "0.2"}, "trading_execution_risk_fraction_invalid"),
        ({"max_risk_per_trade_usd": "0.5"}, "trading_execution_risk_limit_invalid"),
        ({"max_risk_per_trade_usd": "50"}, "trading_execution_risk_limit_invalid"),
        ({"max_total_risk_usd": "20000"}, "trading_execution_risk_limit_invalid"),
        ({"max_positions": 0}, "trading_execution_max_positions_invalid"),
        ({"max_positions": 11}, "trading_execution_max_positions_invalid"),
        ({"max_leverage": 0}, "trading_execution_max_leverage_invalid"),
        ({"max_leverage": 125}, "trading_execution_max_leverage_invalid"),
        ({"max_daily_loss_usd": "5"}, "trading_execution_daily_loss_invalid"),
        ({"stop_distance_bps": 0}, "trading_execution_stop_distance_invalid"),
        ({"stop_distance_bps": 6_000}, "trading_execution_stop_distance_invalid"),
        ({"reconciliation_interval_seconds": 0.5}, "trading_execution_reconciliation_interval_invalid"),
        ({"reconciliation_interval_seconds": 120.0}, "trading_execution_reconciliation_interval_invalid"),
        ({"market_stale_after_seconds": 0.5}, "trading_execution_market_stale_invalid"),
    ],
)
def test_risk_bounds_refuse_the_values_that_would_make_a_limit_stop_being_one(
    override: dict[str, Any],
    reason: str,
) -> None:
    with pytest.raises(ValidationError, match=reason):
        _settings_with_risk(**override)
