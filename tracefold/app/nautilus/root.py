"""Composition root for the one Binance USD-M OI execution Runtime."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext, suppress
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

import uvicorn
from nautilus_trader.adapters.binance import (
    BINANCE,
    BinanceAccountType,
    BinanceFuturesInstrumentProvider,
    BinanceInstrumentProviderConfig,
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
)
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
from nautilus_trader.common.component import LiveClock
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.instruments import CryptoPerpetual

from tracefold.app.nautilus.oi_runtime import (
    OiRuntimeDatabaseBridge,
    OiRuntimeReadiness,
    load_runtime_control_state,
)
from tracefold.app.nautilus.oi_runtime import run_nautilus as run_disabled_runtime
from tracefold.app.nautilus.probe import create_nautilus_probe_app
from tracefold.app.nautilus.reconciliation import (
    account_reports_are_flat,
    build_runtime_reconciliation_snapshot,
    load_complete_binance_account_reports,
    reconcile_reports_into_cache,
    single_binance_execution_client,
)
from tracefold.app.repository_session import RepositorySession, postgres_connection, repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.config import (
    BinanceRuntimeCredentials,
    OiInstrumentRoute,
    OiRiskLimits,
    OiRuntimeProfile,
    build_oi_node_config,
)
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy, RuntimeReadiness
from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading import OperatorIntentV1, TradeSignalV1, canonical_sha256
from tracefold.trading.storage.execution_stream import (
    MAX_EXECUTION_READ_BATCH,
    ExecutionProfileActivation,
    ExecutionRuntimeState,
    materialize_operator_intents,
    materialize_trade_signals,
)

_RUNTIME_RELEASE = "nautilus-1.231.0+oi-v1"
_EXECUTION_STRATEGY = "oi_nautilus_v1"
_INTERNAL_PORT = 8767
_STOP_TIMEOUT_SECONDS = 20.0
_START_TIMEOUT_SECONDS = 90.0
_RECONCILIATION_INTERVAL_SECONDS = 2.0
_HEARTBEAT_INTERVAL_SECONDS = 0.5
_DEFAULT_STOP_DISTANCE_BPS = 100
_BINANCE_USDM_ACCOUNT_ID = AccountId("BINANCE-USDT_FUTURES-master")


@dataclass(slots=True)
class _ProbeState:
    payload: dict[str, Any]
    lock: Lock

    @classmethod
    def starting(cls, profile: OiRuntimeProfile, credential_fingerprint: str) -> _ProbeState:
        return cls(
            payload={
                "ok": False,
                "reason": "runtime_starting",
                "mode": profile.mode,
                "runtime_profile_id": profile.profile_id,
                "runtime_release": profile.runtime_release,
                "config_sha256": profile.config_sha256,
                "credential_fingerprint": credential_fingerprint,
                "singleton_ready": False,
                "credential_ready": True,
                "activation_ready": False,
                "startup_reconciled": False,
                "portfolio_ready": False,
                "audit_ready": False,
                "unexpected_exposure": False,
                "account_flat": False,
                "reconciliation_observed_at_ns": 0,
            },
            lock=Lock(),
        )

    def publish(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.payload = dict(payload)

    def readiness(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.payload)


def run_nautilus(settings: Settings) -> OiRuntimeReadiness | None:
    """Run disabled without a node, or supervise the configured paper/live node."""

    execution = settings.trading.execution
    if execution.mode == "disabled":
        return run_disabled_runtime(_disabled_profile(settings))
    credentials = _read_credentials(settings)
    credential_fingerprint = canonical_sha256(
        {
            "identity_version": "binance_usdm_api_key_v1",
            "account_slot": execution.account_slot,
            "api_key": credentials.api_key,
        }
    )
    with postgres_connection(settings, application_name="tracefold_nautilus_singleton") as conn:
        singleton_repos = repositories_for_connection(conn)
        singleton = AccountSlotSingleton(
            account_slot=execution.account_slot,
            try_acquire=singleton_repos.trading.try_acquire_execution_account_slot,
            release=singleton_repos.trading.release_execution_account_slot,
            heartbeat=lambda: bool(conn.execute("SELECT 1 AS alive").fetchone()["alive"]),
        )
        if not singleton.acquire():
            raise RuntimeError("oi_runtime_account_slot_already_owned")
        try:
            asyncio.run(
                _run_active_runtime(
                    settings=settings,
                    credentials=credentials,
                    credential_fingerprint=credential_fingerprint,
                    singleton=singleton,
                )
            )
        finally:
            singleton.release()
    return None


async def _run_active_runtime(
    *,
    settings: Settings,
    credentials: BinanceRuntimeCredentials,
    credential_fingerprint: str,
    singleton: AccountSlotSingleton,
) -> None:
    routes = await _discover_routes(settings.trading.execution.mode, credentials)
    profile = _active_profile(settings, routes)
    with postgres_connection(settings, application_name="tracefold_nautilus_state") as state_conn:
        state_repos = repositories_for_connection(state_conn)
        existing_activation = _preflight_profile(state_repos, profile)
        control = (
            load_runtime_control_state(state_repos, profile.profile_id) if existing_activation is not None else None
        )
    signals = ExecutionSignalClient(
        runtime_profile_id=profile.profile_id,
        execution_strategy=_EXECUTION_STRATEGY,
    )
    audit = AuditSink(
        factory=ObservationFactory(
            runtime_profile_id=profile.profile_id,
            runtime_release=profile.runtime_release,
            execution_strategy=_EXECUTION_STRATEGY,
        )
    )
    readiness = RuntimeReadiness()
    strategy = OiNautilusStrategy(
        profile=profile,
        signals=signals,
        audit=audit,
        readiness=readiness,
        singleton_ready=lambda: singleton.acquired,
        day_start=None,
        initial_control_state=control,
    )
    loop = asyncio.get_running_loop()
    node = _build_active_node(
        profile=profile,
        credentials=credentials,
        strategy=strategy,
        loop=loop,
    )
    probe = _ProbeState.starting(profile, credential_fingerprint)
    server = _probe_server(probe.readiness)
    stop = asyncio.Event()
    installed_signals = _install_signal_handlers(loop, stop.set)
    node_task = asyncio.create_task(node.run_async(), name="oi-nautilus-node")
    probe_task = asyncio.create_task(server.serve(), name="oi-nautilus-probe")
    bridge: OiRuntimeDatabaseBridge | None = None
    state: ExecutionRuntimeState | None = None
    try:
        await _await_node_started(node=node, node_task=node_task)
        client = single_binance_execution_client(node.kernel.exec_engine)
        if client.account_id != profile.account_id:
            raise RuntimeError("oi_runtime_account_identity_mismatch")
        reports, observed_at_ns = await _reconcile_account(node=node, client=client)
        with postgres_connection(settings, application_name="tracefold_nautilus_state") as state_conn:
            state_repos = repositories_for_connection(state_conn)
            activation = _activate_profile(
                repos=state_repos,
                profile=profile,
                existing=existing_activation,
                account_flat=account_reports_are_flat(reports),
                created_at_ns=observed_at_ns,
            )
            recovery_signals, recovery_manual_entries = _load_recovery_inputs(
                state_repos,
                profile.profile_id,
            )
        readiness.activate()
        snapshot = build_runtime_reconciliation_snapshot(
            profile=profile,
            signals=recovery_signals,
            manual_entries=recovery_manual_entries,
            cache=node.cache,
            account_observed_at_ns=observed_at_ns,
            reconciliation_observed_at_ns=observed_at_ns,
        )
        strategy.reconcile_runtime(snapshot)
        _observe_reconciliation(audit=audit, reports=reports, observed_at_ns=observed_at_ns)
        bridge = OiRuntimeDatabaseBridge(
            settings=settings,
            profile=profile,
            signals=signals,
            audit=audit,
            update_day_start=strategy.update_day_start,
        )
        bridge.start()
        started_at_ns = time.time_ns()
        identity = runtime_identity()
        state = ExecutionRuntimeState(
            account_slot=profile.account_slot,
            runtime_profile_id=activation.runtime_profile_id,
            mode=_active_mode(profile),
            runtime_release=profile.runtime_release,
            config_sha256=profile.config_sha256,
            runtime_id=uuid4(),
            runtime_revision=identity.runtime_revision,
            image_digest=identity.image_digest,
            credential_fingerprint=credential_fingerprint,
            lifecycle_state="starting",
            ready=False,
            singleton_ready=True,
            credential_ready=True,
            activation_ready=True,
            startup_reconciled=False,
            portfolio_ready=bool(node.portfolio.initialized),
            audit_ready=False,
            unexpected_exposure=False,
            account_flat=account_reports_are_flat(reports),
            reconciliation_observed_at_ns=observed_at_ns,
            heartbeat_at_ns=started_at_ns,
            unavailable_reason="runtime_starting",
            started_at_ns=started_at_ns,
            updated_at_ns=started_at_ns,
        )
        _observe_runtime_start(audit=audit, state=state)
        with postgres_connection(settings, application_name="tracefold_nautilus_state") as state_conn:
            state_repos = repositories_for_connection(state_conn)
            with state_repos.transaction():
                state_repos.trading.put_execution_runtime_state(state)
        next_reconciliation = loop.time() + _RECONCILIATION_INTERVAL_SECONDS
        while not stop.is_set():
            if node_task.done():
                await node_task
                raise RuntimeError("oi_runtime_node_returned")
            if probe_task.done():
                await probe_task
                raise RuntimeError("oi_runtime_probe_returned")
            if bridge.fatal_error is not None:
                raise RuntimeError("oi_runtime_database_bridge_failed") from bridge.fatal_error
            if not singleton.check():
                raise RuntimeError("oi_runtime_account_slot_lost")
            now_ns = time.time_ns()
            equity = _account_equity(node, profile)
            if equity is not None:
                bridge.set_equity(equity, now_ns)
            if loop.time() >= next_reconciliation:
                reports, observed_at_ns = await _reconcile_account(node=node, client=client)
                _observe_reconciliation(audit=audit, reports=reports, observed_at_ns=observed_at_ns)
                with postgres_connection(settings, application_name="tracefold_nautilus_state") as state_conn:
                    recovery_signals, recovery_manual_entries = _load_recovery_inputs(
                        repositories_for_connection(state_conn),
                        profile.profile_id,
                    )
                strategy.reconcile_runtime(
                    build_runtime_reconciliation_snapshot(
                        profile=profile,
                        signals=recovery_signals,
                        manual_entries=recovery_manual_entries,
                        cache=node.cache,
                        account_observed_at_ns=observed_at_ns,
                        reconciliation_observed_at_ns=observed_at_ns,
                    )
                )
                next_reconciliation = loop.time() + _RECONCILIATION_INTERVAL_SECONDS
            strategy_readiness = strategy.readiness()
            portfolio_ready = bool(node.portfolio.initialized)
            audit_ready = audit.can_accept_exposure() and bridge.connected
            with postgres_connection(settings, application_name="tracefold_nautilus_state") as state_conn:
                state_repos = repositories_for_connection(state_conn)
                latest = state_repos.trading.latest_execution_profile_activation(profile.account_slot)
                activation_current = latest == activation
                ready = bool(
                    strategy_readiness.ready
                    and activation_current
                    and portfolio_ready
                    and audit_ready
                    and singleton.acquired
                )
                reason = _unavailable_reason(
                    ready=ready,
                    singleton_ready=singleton.acquired,
                    activation_ready=activation_current,
                    portfolio_ready=portfolio_ready,
                    bridge_connected=bridge.connected,
                    strategy_reason=strategy_readiness.reason,
                )
                state = replace(
                    state,
                    lifecycle_state="running",
                    ready=ready,
                    singleton_ready=singleton.acquired,
                    activation_ready=activation_current,
                    startup_reconciled=strategy_readiness.startup_reconciled,
                    portfolio_ready=portfolio_ready,
                    audit_ready=audit_ready,
                    unexpected_exposure=strategy_readiness.unexpected_exposure,
                    account_flat=account_reports_are_flat(reports),
                    reconciliation_observed_at_ns=strategy_readiness.reconciliation_observed_at_ns,
                    heartbeat_at_ns=now_ns,
                    unavailable_reason=reason,
                    updated_at_ns=now_ns,
                )
                with state_repos.transaction():
                    if not state_repos.trading.update_execution_runtime_state(state):
                        raise RuntimeError("oi_runtime_generation_lost")
            probe.publish(_probe_payload(state))
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
    finally:
        server.should_exit = True
        if bridge is not None:
            bridge.stop()
        if state is not None and singleton.acquired:
            stopped_at_ns = max(time.time_ns(), state.heartbeat_at_ns)
            stopped = replace(
                state,
                lifecycle_state="stopped",
                ready=False,
                heartbeat_at_ns=stopped_at_ns,
                unavailable_reason="runtime_stopped",
                updated_at_ns=stopped_at_ns,
            )
            with (
                suppress(Exception),
                postgres_connection(
                    settings,
                    application_name="tracefold_nautilus_state",
                ) as state_conn,
            ):
                state_repos = repositories_for_connection(state_conn)
                with state_repos.transaction():
                    state_repos.trading.update_execution_runtime_state(stopped)
        if node.is_running():
            with suppress(Exception):
                await asyncio.wait_for(node.stop_async(), timeout=_STOP_TIMEOUT_SECONDS)
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(node_task, probe_task, return_exceptions=True),
                timeout=_STOP_TIMEOUT_SECONDS,
            )
        if bridge is not None:
            bridge.join(_STOP_TIMEOUT_SECONDS)
        _remove_signal_handlers(loop, installed_signals)
        node.dispose()


def _disabled_profile(settings: Settings) -> OiRuntimeProfile:
    execution = settings.trading.execution
    identity = {
        "mode": execution.mode,
        "profile_id": execution.profile_id,
        "account_slot": execution.account_slot,
        "runtime_release": _RUNTIME_RELEASE,
    }
    return OiRuntimeProfile(
        mode="disabled",
        profile_id=execution.profile_id,
        account_slot=execution.account_slot,
        account_id=_BINANCE_USDM_ACCOUNT_ID,
        runtime_release=_RUNTIME_RELEASE,
        config_sha256=canonical_sha256(identity),
        credential_namespace=f"tracefold:{execution.profile_id}:disabled",
        cache_namespace=f"tracefold:{execution.profile_id}:disabled",
        client_order_namespace=f"tracefold:{execution.profile_id}:disabled",
        routes=(),
        risk=_risk_limits(),
    )


def _active_profile(settings: Settings, routes: tuple[OiInstrumentRoute, ...]) -> OiRuntimeProfile:
    execution = settings.trading.execution
    risk = _risk_limits()
    config_sha256 = canonical_sha256(
        {
            "config_version": "oi_binance_usdm_v1",
            "mode": execution.mode,
            "profile_id": execution.profile_id,
            "account_slot": execution.account_slot,
            "account_id": _BINANCE_USDM_ACCOUNT_ID.value,
            "runtime_release": _RUNTIME_RELEASE,
            "route_rule": "binance_usdm_trading_usdt_perpetual_v1",
            "stop_distance_bps": _DEFAULT_STOP_DISTANCE_BPS,
            "risk": {key: str(value) for key, value in asdict(risk).items()},
        }
    )
    namespace = f"tracefold:{execution.profile_id}:{execution.mode}"
    return OiRuntimeProfile(
        mode=execution.mode,
        profile_id=execution.profile_id,
        account_slot=execution.account_slot,
        account_id=_BINANCE_USDM_ACCOUNT_ID,
        runtime_release=_RUNTIME_RELEASE,
        config_sha256=config_sha256,
        credential_namespace=namespace,
        cache_namespace=namespace,
        client_order_namespace=namespace,
        routes=routes,
        risk=risk,
    )


def _active_mode(profile: OiRuntimeProfile) -> Literal["paper", "live"]:
    if profile.mode == "paper":
        return "paper"
    if profile.mode == "live":
        return "live"
    raise RuntimeError("oi_runtime_active_mode_invalid")


def _build_active_node(
    *,
    profile: OiRuntimeProfile,
    credentials: BinanceRuntimeCredentials,
    strategy: OiNautilusStrategy,
    loop: asyncio.AbstractEventLoop,
) -> TradingNode:
    node = TradingNode(config=build_oi_node_config(profile, credentials), loop=loop)
    node.trader.add_strategy(strategy)
    node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
    node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)
    node.build()
    single_binance_execution_client(node.kernel.exec_engine)
    return node


def _risk_limits() -> OiRiskLimits:
    return OiRiskLimits(
        risk_fraction_per_trade=Decimal("0.01"),
        max_risk_per_trade_usd=Decimal("10"),
        max_total_risk_usd=Decimal("25"),
        max_positions=1,
        max_leverage=1,
        max_daily_loss_usd=Decimal("25"),
        market_stale_after_ns=5_000_000_000,
        account_stale_after_ns=5_000_000_000,
        reconciliation_stale_after_ns=10_000_000_000,
    )


async def _discover_routes(
    mode: str,
    credentials: BinanceRuntimeCredentials,
) -> tuple[OiInstrumentRoute, ...]:
    environment = BinanceEnvironment.DEMO if mode == "paper" else BinanceEnvironment.LIVE
    clock = LiveClock(asyncio.get_running_loop())
    client = get_cached_binance_http_client(
        clock=clock,
        account_type=BinanceAccountType.USDT_FUTURES,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        environment=environment,
    )
    provider = BinanceFuturesInstrumentProvider(
        client=client,
        clock=clock,
        account_type=BinanceAccountType.USDT_FUTURES,
        config=BinanceInstrumentProviderConfig(load_all=True, query_commission_rates=False),
    )
    await provider.load_all_async()
    routes: dict[str, OiInstrumentRoute] = {}
    for instrument in provider.list_all():
        if not isinstance(instrument, CryptoPerpetual):
            continue
        if instrument.quote_currency != USDT or instrument.settlement_currency != USDT:
            continue
        if str((instrument.info or {}).get("status")) != "TRADING":
            continue
        market_key = f"crypto:perp:{instrument.base_currency.code}:USDT"
        if market_key in routes:
            raise RuntimeError("oi_runtime_market_route_ambiguous")
        routes[market_key] = OiInstrumentRoute(
            market_key=market_key,
            instrument_id=instrument.id,
            stop_distance_bps=_DEFAULT_STOP_DISTANCE_BPS,
        )
    if not routes:
        raise RuntimeError("oi_runtime_route_catalog_empty")
    return tuple(routes[key] for key in sorted(routes))


def _preflight_profile(repos: RepositorySession, profile: OiRuntimeProfile) -> ExecutionProfileActivation | None:
    current = repos.trading.latest_execution_profile_activation(profile.account_slot)
    existing = repos.trading.execution_profile_activation(profile.profile_id)
    if existing is not None and current != existing:
        raise RuntimeError("oi_runtime_profile_cannot_be_reactivated")
    if existing is not None and (
        existing.account_slot != profile.account_slot
        or existing.mode != profile.mode
        or existing.runtime_release != profile.runtime_release
        or existing.config_sha256 != profile.config_sha256
    ):
        raise RuntimeError("oi_runtime_profile_identity_changed")
    return existing


def _activate_profile(
    *,
    repos: RepositorySession,
    profile: OiRuntimeProfile,
    existing: ExecutionProfileActivation | None,
    account_flat: bool,
    created_at_ns: int,
) -> ExecutionProfileActivation:
    if existing is not None:
        return existing
    if not account_flat:
        raise RuntimeError("oi_runtime_cold_transition_requires_binance_flat")
    signal_seq, command_seq = repos.trading.execution_stream_fence()
    activation = ExecutionProfileActivation(
        runtime_profile_id=profile.profile_id,
        account_slot=profile.account_slot,
        activated_after_signal_seq=signal_seq,
        activated_after_command_seq=command_seq,
        mode=profile.mode,
        runtime_release=profile.runtime_release,
        config_sha256=profile.config_sha256,
        created_at_ns=created_at_ns,
    )
    with repos.transaction():
        repos.trading.append_execution_profile_activation(activation)
    if repos.trading.latest_execution_profile_activation(profile.account_slot) != activation:
        raise RuntimeError("oi_runtime_activation_not_current")
    return activation


def _load_recovery_inputs(
    repos: RepositorySession,
    profile_id: str,
) -> tuple[tuple[TradeSignalV1, ...], tuple[OperatorIntentV1, ...]]:
    signal_rows = repos.trading.execution_recovery_signals(
        runtime_profile_id=profile_id,
        limit=MAX_EXECUTION_READ_BATCH,
    )
    command_rows = repos.trading.execution_recovery_manual_entries(
        runtime_profile_id=profile_id,
        limit=MAX_EXECUTION_READ_BATCH,
    )
    if (
        len(signal_rows) == MAX_EXECUTION_READ_BATCH
        or len(command_rows) == MAX_EXECUTION_READ_BATCH
        or len(signal_rows) + len(command_rows) > MAX_EXECUTION_READ_BATCH
    ):
        raise RuntimeError("oi_runtime_recovery_history_overflow")
    return materialize_trade_signals(signal_rows), materialize_operator_intents(command_rows)


async def _reconcile_account(*, node: TradingNode, client: Any) -> tuple[tuple[list[Any], list[Any]], int]:
    reports = await load_complete_binance_account_reports(client)
    for report in (*reports[0], *reports[1]):
        if report.account_id != client.account_id:
            raise RuntimeError("oi_runtime_account_report_scope_invalid")
    reconcile_reports_into_cache(engine=node.kernel.exec_engine, reports=reports)
    return reports, int(node.kernel.clock.timestamp_ns())


def _observe_reconciliation(
    *,
    audit: AuditSink,
    reports: tuple[list[Any], list[Any]],
    observed_at_ns: int,
) -> None:
    positions, orders = reports
    references: set[str] = set()
    for report in (*positions, *orders):
        for name in ("client_order_id", "venue_order_id", "position_id", "instrument_id"):
            value = getattr(report, name, None)
            if value is not None:
                references.add(str(getattr(value, "value", value)))
    bounded_references = tuple(sorted(references)[:16])
    account_flat = account_reports_are_flat(reports)
    audit.offer(
        audit.factory.create(
            normalized_kind="reconciliation",
            occurred_at_ns=observed_at_ns,
            observed_at_ns=observed_at_ns,
            native_identity_references=bounded_references,
            summary={
                "source": "binance_private_api",
                "positions": len(positions),
                "orders": len(orders),
                "account_flat": account_flat,
                "native_refs_truncated": len(references) > len(bounded_references),
            },
            payload={
                "source": "binance_private_api",
                "position_reports": len(positions),
                "order_reports": len(orders),
                "account_flat": account_flat,
                "native_identity_references": bounded_references,
            },
            event_identity=f"binance-private:{observed_at_ns}",
        )
    )


def _observe_runtime_start(*, audit: AuditSink, state: ExecutionRuntimeState) -> None:
    accepted = audit.offer(
        audit.factory.create(
            normalized_kind="readiness",
            occurred_at_ns=state.started_at_ns,
            observed_at_ns=state.started_at_ns,
            summary={
                "lifecycle": "started",
                "runtime_id": str(state.runtime_id),
                "mode": state.mode,
                "runtime_revision": state.runtime_revision,
                "image_digest": state.image_digest,
                "config_sha256": state.config_sha256,
                "credential_fingerprint": state.credential_fingerprint,
                "account_slot": state.account_slot,
            },
            payload={
                "lifecycle": "started",
                "runtime_id": str(state.runtime_id),
                "mode": state.mode,
                "runtime_revision": state.runtime_revision,
                "image_digest": state.image_digest,
                "config_sha256": state.config_sha256,
                "credential_fingerprint": state.credential_fingerprint,
                "account_slot": state.account_slot,
            },
            event_identity=f"runtime-start:{state.runtime_id}",
        )
    )
    if not accepted:
        raise RuntimeError("oi_runtime_start_receipt_unavailable")


async def _await_node_started(*, node: TradingNode, node_task: asyncio.Task[None]) -> None:
    deadline = asyncio.get_running_loop().time() + _START_TIMEOUT_SECONDS
    while not node.trader.is_running:
        if node_task.done():
            await node_task
            raise RuntimeError("oi_runtime_node_returned_during_start")
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("oi_runtime_start_timeout")
        await asyncio.sleep(0.05)


def _account_equity(node: TradingNode, profile: OiRuntimeProfile) -> Decimal | None:
    account = node.cache.account(profile.account_id)
    if account is None:
        return None
    total = account.balance_total(USDT)
    if total is None:
        return None
    method = getattr(total, "as_decimal", None)
    return Decimal(str(total)) if method is None else Decimal(method())


def _unavailable_reason(
    *,
    ready: bool,
    singleton_ready: bool,
    activation_ready: bool,
    portfolio_ready: bool,
    bridge_connected: bool,
    strategy_reason: str,
) -> str | None:
    if ready:
        return None
    if not singleton_ready:
        return "singleton_unavailable"
    if not activation_ready:
        return "activation_not_current"
    if not bridge_connected:
        return "database_bridge_unavailable"
    if not portfolio_ready:
        return "portfolio_unavailable"
    return strategy_reason


def _probe_payload(state: ExecutionRuntimeState) -> dict[str, Any]:
    return {
        "ok": state.ready,
        "reason": state.unavailable_reason or "ready",
        "mode": state.mode,
        "runtime_profile_id": state.runtime_profile_id,
        "runtime_release": state.runtime_release,
        "config_sha256": state.config_sha256,
        "runtime_revision": state.runtime_revision,
        "image_digest": state.image_digest,
        "credential_fingerprint": state.credential_fingerprint,
        "singleton_ready": state.singleton_ready,
        "credential_ready": state.credential_ready,
        "activation_ready": state.activation_ready,
        "startup_reconciled": state.startup_reconciled,
        "portfolio_ready": state.portfolio_ready,
        "audit_ready": state.audit_ready,
        "unexpected_exposure": state.unexpected_exposure,
        "account_flat": state.account_flat,
        "reconciliation_observed_at_ns": state.reconciliation_observed_at_ns,
        "heartbeat_at_ns": state.heartbeat_at_ns,
    }


def _read_credentials(settings: Settings) -> BinanceRuntimeCredentials:
    return BinanceRuntimeCredentials(
        api_key=_read_secret(settings.trading_binance_usdm_api_key_file(), "api_key"),
        api_secret=_read_secret(settings.trading_binance_usdm_api_secret_file(), "api_secret"),
    )


def _read_secret(path: Any, name: str) -> str:
    if path is None:
        raise RuntimeError(f"oi_runtime_{name}_file_missing")
    try:
        return read_secure_secret_text(path)
    except SecretFileError as exc:
        raise RuntimeError(f"oi_runtime_{name}_file_{exc.code}") from None


def _probe_server(readiness: Callable[[], dict[str, Any]]) -> uvicorn.Server:
    config = uvicorn.Config(
        create_nautilus_probe_app(readiness),
        host="0.0.0.0",  # noqa: S104 -- Compose publishes only on operator-selected host loopback
        port=_INTERNAL_PORT,
        log_config=None,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    server.capture_signals = nullcontext  # type: ignore[method-assign, assignment]
    return server


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[], None],
) -> tuple[signal.Signals, ...]:
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, callback)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signum)
    return tuple(installed)


def _remove_signal_handlers(loop: asyncio.AbstractEventLoop, installed: Sequence[signal.Signals]) -> None:
    for signum in installed:
        loop.remove_signal_handler(signum)


__all__ = ["run_nautilus"]
