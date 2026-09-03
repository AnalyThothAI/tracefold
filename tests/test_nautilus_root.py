from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from nautilus_trader.model.identifiers import InstrumentId
from pydantic import ValidationError

from tests.nautilus_oi_runtime_fixtures import oi_profile
from tracefold.app.nautilus import root as nautilus_root
from tracefold.app.nautilus.oi_runtime import RuntimeStateProjector
from tracefold.app.nautilus.root import (
    _activate_profile,
    _discover_routes,
    _observe_reconciliation,
    _observe_runtime_start,
    _preflight_profile,
    _PrivateReconciliationRequests,
    _PrivateReconciliationResult,
    _probe_payload,
    _reconcile_account,
    _risk_limits,
)
from tracefold.app.workers.trading_notifications import trading_notification_text
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials
from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import CompleteBinanceAccountReports
from tracefold.platform.config.models import Settings
from tracefold.trading.notification_policy import is_notifiable
from tracefold.trading.storage.execution_stream import ExecutionProfileActivation, ExecutionRuntimeState


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


def _repos(trading: Any) -> Any:
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
        runtime_profile_id="oi-paper-profile",
        mode="paper",
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="a" * 64,
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        runtime_revision="b" * 40,
        image_digest="sha256:" + "c" * 64,
        credential_fingerprint="d" * 64,
        lifecycle_state="starting",
        alive=True,
        execution_safe=False,
        entries_armed=False,
        control_plane_ready=False,
        singleton_ready=True,
        credential_ready=True,
        activation_ready=True,
        startup_reconciled=False,
        portfolio_ready=False,
        audit_ready=False,
        day_start_ready=False,
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
    assert captured["client"]["environment"] == nautilus_root.BinanceEnvironment.DEMO
    assert type(captured["client"]["clock"]).__name__ == "LiveClock"
    assert [route.market_key for route in routes] == ["crypto:perp:BTC:USDT"]
    assert [route.stop_distance_bps for route in routes] == [100]


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


def test_rolling_restart_of_an_existing_profile_does_not_require_binance_flat() -> None:
    """#510 PR-3. Only a cold transition to a new profile needs a proven flat account."""

    existing = _activation()
    trading = _Trading(existing)

    activation = _activate_profile(
        repos=_repos(trading),
        profile=oi_profile(),
        existing=existing,
        account_flat=False,
        created_at_ns=200,
    )

    assert activation == existing
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
    def __init__(self, *, latest: ExecutionProfileActivation | None = None) -> None:
        self.puts: list[ExecutionRuntimeState] = []
        self.updates: list[ExecutionRuntimeState] = []
        self.latest = latest

    def put_execution_runtime_state(self, state: ExecutionRuntimeState) -> None:
        self.puts.append(state)

    def update_execution_runtime_state(self, state: ExecutionRuntimeState) -> bool:
        self.updates.append(state)
        return True

    def latest_execution_profile_activation(self, _account_slot: str) -> ExecutionProfileActivation | None:
        return self.latest


def _projector(
    starting: ExecutionRuntimeState,
    *,
    activation: ExecutionProfileActivation | None = None,
) -> RuntimeStateProjector:
    return RuntimeStateProjector(
        initial=starting,
        activation=activation or _activation(),
        recovery_inputs=((), ()),
    )


def test_runtime_state_projector_writes_changes_immediately_and_unchanged_state_only_on_heartbeat() -> None:
    """#510 PR-5b. The loop offers; only the bridge thread's connection writes."""

    trading = _ProjectionTrading()
    repos = _repos(trading)
    starting = _runtime_state()
    projector = _projector(starting)
    projector.start(repos)

    changed = replace(
        starting,
        lifecycle_state="running",
        heartbeat_at_ns=starting.heartbeat_at_ns + 1,
        entry_block_reason="portfolio_unavailable",
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


def test_projector_reads_activation_currency_for_a_loop_that_no_longer_holds_a_connection() -> None:
    activation = _activation()
    trading = _ProjectionTrading(latest=activation)
    repos = _repos(trading)
    projector = _projector(_runtime_state(), activation=activation)

    assert projector.activation_current is True

    projector.refresh_activation(repos)
    assert projector.activation_current is True

    trading.latest = _activation("next-profile")
    projector.refresh_activation(repos)
    assert projector.activation_current is False


def test_probe_readiness_requires_execution_safety_but_not_entry_arming() -> None:
    safe_but_paused = replace(
        _runtime_state(),
        lifecycle_state="running",
        execution_safe=True,
        entries_armed=False,
        control_plane_ready=True,
        startup_reconciled=True,
        portfolio_ready=True,
        entry_block_reason="entries_paused",
    )

    payload = _probe_payload(safe_but_paused)

    assert payload["ok"] is True
    assert payload["alive"] is True
    assert payload["execution_safe"] is True
    assert payload["entries_armed"] is False
    assert _probe_payload(replace(safe_but_paused, execution_safe=False))["ok"] is False


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
            runtime_profile_id="oi-paper-profile",
            runtime_release="nautilus-1.231.0+oi-v1",
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

    flat_identity = _observe_reconciliation(
        audit=audit,
        result=_PrivateReconciliationResult(_complete_reports(), ("steady",), 1_000, 1_000_000),
        previous_identity=None,
    )
    _observe_reconciliation(
        audit=audit,
        result=_PrivateReconciliationResult(
            _complete_reports(positions=(position,)),
            ("unexpected_exposure",),
            2_000,
            1_000_000,
        ),
        previous_identity=flat_identity,
    )
    _observe_runtime_start(audit=audit, state=state)
    flat, unflat, started = audit.flush_once(lambda _values: None)

    # The timer's steady state is not a card; exposure and a Runtime restart both are.
    assert is_notifiable(flat.normalized_kind, flat.summary) is False
    assert is_notifiable(unflat.normalized_kind, unflat.summary) is True
    assert is_notifiable(started.normalized_kind, started.summary) is True
    for observation in (unflat, started):
        row = {"normalized_kind": observation.normalized_kind, "summary": observation.summary}
        assert trading_notification_text(row) is not None, f"{observation.normalized_kind} renders no stage"


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
def test_every_risk_value_is_inside_the_config_digest_the_activation_fence_reads(override: dict[str, Any]) -> None:
    """#510 E. Risk lived outside `config_sha256`, so the fence could not see an operator edit.

    The fence is `_preflight_profile`: a profile id whose recorded `config_sha256` no longer matches
    the one this process computed cannot be reused, so a risk change now needs a new profile and a
    fresh activation, exactly like a mode or account-slot change.
    """

    routes = oi_profile().routes
    baseline = nautilus_root._active_profile(Settings(trading={"execution": {"mode": "paper"}}), routes)
    edited = nautilus_root._active_profile(_settings_with_risk(**override), routes)

    assert edited.config_sha256 != baseline.config_sha256
    assert edited.profile_id == baseline.profile_id

    recorded = replace(
        _activation(baseline.profile_id),
        account_slot=baseline.account_slot,
        runtime_release=baseline.runtime_release,
        config_sha256=baseline.config_sha256,
    )
    trading = _Trading(recorded)

    with pytest.raises(RuntimeError, match="oi_runtime_profile_identity_changed"):
        _preflight_profile(_repos(trading), edited)


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
