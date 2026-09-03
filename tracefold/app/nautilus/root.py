"""Composition root for the one Binance USD-M OI execution Runtime."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext, suppress
from dataclasses import asdict, dataclass, replace
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
    RUNTIME_HEARTBEAT_INTERVAL_NS,
    OiRuntimeDatabaseBridge,
    RuntimeStateProjector,
    load_recovery_inputs,
    load_runtime_control_state,
)
from tracefold.app.nautilus.oi_runtime import run_nautilus as run_disabled_runtime
from tracefold.app.nautilus.probe import create_nautilus_probe_app
from tracefold.app.nautilus.reconciliation import (
    build_runtime_reconciliation_snapshot,
    reconcile_reports_into_cache,
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
from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import (
    CompleteBinanceAccountReports,
    load_complete_binance_account_reports,
    single_binance_execution_client,
)
from tracefold.integrations.nautilus.oi_runtime.risk import account_equity_usd
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.state import (
    PRIVATE_RECONCILIATION_REASONS,
    PrivateReconciliationReason,
    RuntimeReadiness,
)
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy
from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading import EXECUTION_STRATEGY_ID, canonical_sha256
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState

_RUNTIME_RELEASE = "nautilus-1.231.0+oi-v1"
_EXECUTION_STRATEGY = EXECUTION_STRATEGY_ID
_INTERNAL_PORT = 8767
_STOP_TIMEOUT_SECONDS = 20.0
_START_TIMEOUT_SECONDS = 90.0
_HEARTBEAT_INTERVAL_SECONDS = RUNTIME_HEARTBEAT_INTERVAL_NS / 1_000_000_000
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
                "alive": True,
                "execution_safe": False,
                "entries_armed": False,
                "entry_block_reason": "runtime_starting",
                "mode": profile.mode,
                "account_slot": profile.account_slot,
                "runtime_release": profile.runtime_release,
                "config_sha256": profile.config_sha256,
                "credential_fingerprint": credential_fingerprint,
                "singleton_ready": False,
                "startup_reconciled": False,
                "portfolio_ready": False,
                "control_plane_ready": False,
                "audit_ready": False,
                "day_start_ready": False,
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


@dataclass(frozen=True, slots=True)
class _PrivateReconciliationResult:
    reports: CompleteBinanceAccountReports
    triggers: tuple[str, ...]
    observed_at_ns: int
    duration_ns: int


class _PrivateReconciliationRequests:
    """Coalesce Strategy repair hints into the App-owned private-account scan."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop, wake: asyncio.Event) -> None:
        self._loop = loop
        self._wake = wake
        self._pending: set[PrivateReconciliationReason] = set()
        self._lock = Lock()

    def request(self, reason: PrivateReconciliationReason) -> None:
        if reason not in PRIVATE_RECONCILIATION_REASONS:
            raise ValueError("oi_runtime_private_reconciliation_reason_invalid")
        with self._lock:
            self._pending.add(reason)
        self._loop.call_soon_threadsafe(self._wake.set)

    def drain(self) -> tuple[str, ...]:
        with self._lock:
            reasons = tuple(sorted(self._pending))
            self._pending.clear()
        return reasons


def run_nautilus(settings: Settings) -> None:
    """Run disabled without a node, or supervise the configured paper/live node."""

    execution = settings.trading.execution
    if execution.mode == "disabled":
        run_disabled_runtime(_disabled_profile(settings))
        return
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
                    repos=singleton_repos,
                )
            )
        finally:
            singleton.release()


async def _run_active_runtime(
    *,
    settings: Settings,
    credentials: BinanceRuntimeCredentials,
    credential_fingerprint: str,
    singleton: AccountSlotSingleton,
    repos: RepositorySession,
) -> None:
    """Own Binance, Nautilus and the in-memory picture; do no PostgreSQL work once the loop starts.

    Startup is sequential and reads what it needs on the session that already exists to hold the
    account-slot advisory lock. From `bridge.start()` the bridge thread is this process's only
    PostgreSQL caller: two connections instead of three, and no synchronous statement on the thread
    that also runs every Nautilus order and position callback (#510 E).
    """

    execution = settings.trading.execution
    routes = await _discover_routes(
        execution.mode,
        credentials,
        stop_distance_bps=execution.risk.stop_distance_bps,
    )
    profile = _active_profile(settings, routes)
    # Control state belongs to the account slot and outlives this process: a slot the operator
    # resumed is still resumed after a restart, a new image or a risk-config change (#520 PR-A).
    control = load_runtime_control_state(repos, profile.account_slot, now_ns=time.time_ns())
    signals = ExecutionSignalClient(
        account_slot=profile.account_slot,
        execution_strategy=_EXECUTION_STRATEGY,
    )
    audit = AuditSink(
        factory=ObservationFactory(
            account_slot=profile.account_slot,
            runtime_release=profile.runtime_release,
            execution_strategy=_EXECUTION_STRATEGY,
        )
    )
    readiness = RuntimeReadiness()
    loop = asyncio.get_running_loop()
    runtime_wake = asyncio.Event()
    reconciliation_requests = _PrivateReconciliationRequests(loop=loop, wake=runtime_wake)
    bridge: OiRuntimeDatabaseBridge | None = None
    projector: RuntimeStateProjector | None = None

    def dispatch_pump_on_loop(pump: Callable[[], None]) -> None:
        """The timer's only job: hand its pump to the thread that owns Runtime state (#510 F)."""

        loop.call_soon_threadsafe(pump)

    strategy = OiNautilusStrategy(
        profile=profile,
        signals=signals,
        audit=audit,
        readiness=readiness,
        dispatch_pump=dispatch_pump_on_loop,
        singleton_ready=lambda: singleton.acquired,
        control_plane_ready=lambda: bridge is not None and bridge.connected and bridge.inputs_ready,
        day_start=None,
        request_reconciliation=reconciliation_requests.request,
        initial_control_state=control,
    )
    node = _build_active_node(
        profile=profile,
        credentials=credentials,
        strategy=strategy,
        loop=loop,
    )
    route_instrument_ids = frozenset(route.instrument_id for route in profile.routes)
    probe = _ProbeState.starting(profile, credential_fingerprint)
    server = _probe_server(probe.readiness)
    stop = asyncio.Event()

    def request_stop() -> None:
        stop.set()
        runtime_wake.set()

    installed_signals = _install_signal_handlers(loop, request_stop)
    node_task = asyncio.create_task(node.run_async(), name="oi-nautilus-node")
    probe_task = asyncio.create_task(server.serve(), name="oi-nautilus-probe")
    try:
        await _await_node_started(node=node, node_task=node_task)
        client = single_binance_execution_client(node.kernel.exec_engine)
        if client.account_id != profile.account_id:
            raise RuntimeError("oi_runtime_account_identity_mismatch")
        result = await _reconcile_account(node=node, client=client, triggers=("startup",))
        reports = result.reports
        observed_at_ns = result.observed_at_ns
        recovery_inputs = load_recovery_inputs(repos, profile.account_slot, observed_at_ns)
        strategy.reconcile_runtime(
            build_runtime_reconciliation_snapshot(
                profile=profile,
                signals=recovery_inputs[0],
                manual_entries=recovery_inputs[1],
                cache=node.cache,
                account_observed_at_ns=observed_at_ns,
                reconciliation_observed_at_ns=observed_at_ns,
            )
        )
        reconciliation_identity = _observe_reconciliation(audit=audit, result=result, previous_identity=None)
        started_at_ns = time.time_ns()
        identity = runtime_identity()
        state = ExecutionRuntimeState(
            account_slot=profile.account_slot,
            mode=_active_mode(profile),
            runtime_release=profile.runtime_release,
            config_sha256=profile.config_sha256,
            runtime_id=uuid4(),
            runtime_revision=identity.runtime_revision,
            image_digest=identity.image_digest,
            credential_fingerprint=credential_fingerprint,
            lifecycle_state="starting",
            alive=True,
            execution_safe=False,
            entries_armed=False,
            control_plane_ready=False,
            singleton_ready=True,
            startup_reconciled=False,
            portfolio_ready=bool(node.portfolio.initialized),
            audit_ready=False,
            day_start_ready=False,
            unexpected_exposure=False,
            account_flat=reports.account_flat,
            positions_count=len(reports.positions),
            open_orders_count=len(reports.orders),
            protection_status="unknown" if reports.positions else "not_applicable",
            reconciliation_observed_at_ns=observed_at_ns,
            heartbeat_at_ns=started_at_ns,
            entry_block_reason="runtime_starting",
            started_at_ns=started_at_ns,
            updated_at_ns=started_at_ns,
            account_snapshot=strategy.account_snapshot(projected_at_ns=started_at_ns),
            # The catalogue this generation discovered, published where the Signal lane can read it.
            # `_discover_routes` runs once per start, so a catalogue change is a new Runtime start and
            # a new insert; the steady heartbeat never rewrites it.
            routes=tuple(sorted(route.market_key for route in profile.routes)),
        )
        _observe_runtime_start(audit=audit, state=state)
        projector = RuntimeStateProjector(initial=state, recovery_inputs=recovery_inputs)
        projector.start(repos)
        bridge = OiRuntimeDatabaseBridge(
            settings=settings,
            profile=profile,
            signals=signals,
            audit=audit,
            update_day_start=strategy.update_day_start,
            singleton=singleton,
            projector=projector,
        )
        bridge.start()
        reconciliation_interval = profile.risk.reconciliation_interval_seconds
        next_reconciliation = loop.time() + reconciliation_interval
        while not stop.is_set():
            runtime_wake.clear()
            if node_task.done():
                await node_task
                raise RuntimeError("oi_runtime_node_returned")
            if probe_task.done():
                await probe_task
                raise RuntimeError("oi_runtime_probe_returned")
            if bridge.fatal_error is not None:
                raise RuntimeError("oi_runtime_database_bridge_failed") from bridge.fatal_error
            # The heartbeat that proves the lock's session is alive runs on the bridge thread now;
            # this is the same fail-closed read, taken from memory.
            if not singleton.acquired:
                raise RuntimeError("oi_runtime_account_slot_lost")
            now_ns = time.time_ns()
            # The same function the entry path's `NautilusRiskFacts` uses, so the day-start baseline
            # and every intraday comparison against it are one definition of equity (#510 B). A
            # missing account or an unpriced owned position simply defers the baseline; entries stay
            # blocked on `day_start_baseline_missing` until it can be written, which is the safe way
            # to be unsure.
            with suppress(RuntimeError):
                bridge.set_equity(
                    account_equity_usd(
                        cache=node.cache,
                        portfolio=node.portfolio,
                        account_id=profile.account_id,
                        routes=route_instrument_ids,
                    ),
                    now_ns,
                )
            reconciliation_triggers = set(reconciliation_requests.drain())
            if loop.time() >= next_reconciliation:
                reconciliation_triggers.add("steady")
            if reconciliation_triggers:
                result = await _reconcile_account(
                    node=node,
                    client=client,
                    triggers=tuple(sorted(reconciliation_triggers)),
                )
                reports = result.reports
                observed_at_ns = result.observed_at_ns
                reconciliation_identity = _observe_reconciliation(
                    audit=audit,
                    result=result,
                    previous_identity=reconciliation_identity,
                )
                recovery_signals, recovery_manual_entries = bridge.recovery_inputs()
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
                next_reconciliation = loop.time() + reconciliation_interval
            strategy_readiness = strategy.readiness()
            portfolio_ready = bool(node.portfolio.initialized)
            audit_ready = audit.can_accept_exposure() and bridge.connected
            execution_safe = bool(strategy_readiness.execution_safe)
            entries_armed = bool(strategy_readiness.entries_armed and execution_safe)
            entry_block_reason = None if entries_armed else strategy_readiness.entry_block_reason or "entry_blocked"
            positions_count = len(reports.positions)
            state = replace(
                state,
                lifecycle_state="running",
                alive=True,
                execution_safe=execution_safe,
                entries_armed=entries_armed,
                control_plane_ready=strategy_readiness.control_plane_ready,
                singleton_ready=singleton.acquired,
                startup_reconciled=strategy_readiness.startup_reconciled,
                portfolio_ready=portfolio_ready,
                audit_ready=audit_ready,
                day_start_ready=strategy_readiness.day_start_ready,
                unexpected_exposure=strategy_readiness.unexpected_exposure,
                account_flat=reports.account_flat,
                positions_count=positions_count,
                open_orders_count=len(reports.orders),
                protection_status=strategy.protection_status(
                    positions_count=positions_count,
                    unexpected_exposure=strategy_readiness.unexpected_exposure,
                ),
                reconciliation_observed_at_ns=strategy_readiness.reconciliation_observed_at_ns,
                heartbeat_at_ns=now_ns,
                entry_block_reason=entry_block_reason,
                updated_at_ns=now_ns,
                account_snapshot=strategy.account_snapshot(projected_at_ns=now_ns),
            )
            projector.offer(state)
            probe.publish(_probe_payload(state))
            with suppress(TimeoutError):
                await asyncio.wait_for(runtime_wake.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
    finally:
        server.should_exit = True
        if projector is not None and singleton.acquired:
            written = projector.current
            stopped_at_ns = max(time.time_ns(), written.heartbeat_at_ns)
            projector.offer(
                replace(
                    written,
                    lifecycle_state="stopped",
                    alive=False,
                    execution_safe=False,
                    entries_armed=False,
                    control_plane_ready=False,
                    heartbeat_at_ns=stopped_at_ns,
                    entry_block_reason="runtime_stopped",
                    updated_at_ns=stopped_at_ns,
                )
            )
            if bridge is None:
                # Nothing ever started the thread that owns the writes, so the startup session is
                # still the only one open and this is still the shutdown path, not the loop.
                with suppress(Exception):
                    projector.write_once(repos)
        # The bridge drains one last projection write before it closes its session.
        if bridge is not None:
            bridge.stop()
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
        "account_slot": execution.account_slot,
        "runtime_release": _RUNTIME_RELEASE,
    }
    return OiRuntimeProfile(
        mode="disabled",
        account_slot=execution.account_slot,
        account_id=_BINANCE_USDM_ACCOUNT_ID,
        runtime_release=_RUNTIME_RELEASE,
        config_sha256=canonical_sha256(identity),
        cache_namespace=f"tracefold:{execution.account_slot}:disabled",
        client_order_namespace=f"tracefold:{execution.account_slot}:disabled",
        routes=(),
        risk=_risk_limits(settings),
    )


def _active_profile(settings: Settings, routes: tuple[OiInstrumentRoute, ...]) -> OiRuntimeProfile:
    execution = settings.trading.execution
    risk = _risk_limits(settings)
    config_sha256 = canonical_sha256(
        {
            "config_version": "oi_binance_usdm_v1",
            "mode": execution.mode,
            "account_slot": execution.account_slot,
            "account_id": _BINANCE_USDM_ACCOUNT_ID.value,
            "runtime_release": _RUNTIME_RELEASE,
            "route_rule": "binance_usdm_trading_usdt_perpetual_v1",
            "stop_distance_bps": execution.risk.stop_distance_bps,
            "risk": {key: str(value) for key, value in asdict(risk).items()},
        }
    )
    # Every deterministic client order id this Runtime can claim lives under this namespace, so the
    # account slot and the mode are what a restart rebuilds ownership from (#520 PR-A).
    namespace = f"tracefold:{execution.account_slot}:{execution.mode}"
    return OiRuntimeProfile(
        mode=execution.mode,
        account_slot=execution.account_slot,
        account_id=_BINANCE_USDM_ACCOUNT_ID,
        runtime_release=_RUNTIME_RELEASE,
        config_sha256=config_sha256,
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


def _risk_limits(settings: Settings) -> OiRiskLimits:
    """The operator's risk section, as the Runtime's gap policy (#510 E).

    These were literals here, so an operator could not see them with `tracefold config` and could not
    change one without a new image. `reconciliation_interval_seconds` stays the single input both
    account clocks derive from.
    """

    risk = settings.trading.execution.risk
    return OiRiskLimits(
        risk_fraction_per_trade=risk.risk_fraction_per_trade,
        max_risk_per_trade_usd=risk.max_risk_per_trade_usd,
        max_total_risk_usd=risk.max_total_risk_usd,
        max_positions=risk.max_positions,
        max_leverage=risk.max_leverage,
        max_daily_loss_usd=risk.max_daily_loss_usd,
        market_stale_after_ns=int(risk.market_stale_after_seconds * 1_000_000_000),
        reconciliation_interval_ns=int(risk.reconciliation_interval_seconds * 1_000_000_000),
    )


async def _discover_routes(
    mode: str,
    credentials: BinanceRuntimeCredentials,
    *,
    stop_distance_bps: int,
) -> tuple[OiInstrumentRoute, ...]:
    environment = BinanceEnvironment.DEMO if mode == "paper" else BinanceEnvironment.LIVE
    clock = LiveClock()
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
        try:
            route = OiInstrumentRoute(
                market_key=market_key,
                instrument_id=instrument.id,
                stop_distance_bps=stop_distance_bps,
            )
        except ValueError as exc:
            if str(exc) == "oi_runtime_market_key_invalid":
                continue
            raise
        if market_key in routes:
            raise RuntimeError("oi_runtime_market_route_ambiguous")
        routes[market_key] = route
    if not routes:
        raise RuntimeError("oi_runtime_route_catalog_empty")
    return tuple(routes[key] for key in sorted(routes))


async def _reconcile_account(
    *,
    node: TradingNode,
    client: Any,
    triggers: tuple[str, ...],
) -> _PrivateReconciliationResult:
    allowed = {*PRIVATE_RECONCILIATION_REASONS, "startup", "steady"}
    if not triggers or any(trigger not in allowed for trigger in triggers):
        raise ValueError("oi_runtime_private_reconciliation_trigger_invalid")
    started_at_ns = time.perf_counter_ns()
    reports = await load_complete_binance_account_reports(client)
    for report in (*reports.positions, *reports.orders):
        if report.account_id != client.account_id:
            raise RuntimeError("oi_runtime_account_report_scope_invalid")
    reconcile_reports_into_cache(engine=node.kernel.exec_engine, reports=reports)
    return _PrivateReconciliationResult(
        reports=reports,
        triggers=tuple(sorted(set(triggers))),
        observed_at_ns=int(node.kernel.clock.timestamp_ns()),
        duration_ns=time.perf_counter_ns() - started_at_ns,
    )


_IDENTITY_FIELDS = (
    "instrument_id",
    "position_id",
    "venue_position_id",
    "client_order_id",
    "venue_order_id",
    "position_side",
    "order_side",
    "order_status",
    "quantity",
)


def _report_identity(report: Any) -> str:
    parts = []
    for name in _IDENTITY_FIELDS:
        value = getattr(report, name, None)
        if value is not None:
            parts.append(f"{name}={getattr(value, 'value', value)}")
    return "|".join(parts)


def _reconciliation_identity(reports: CompleteBinanceAccountReports) -> tuple[str, ...]:
    """What the account currently is, as the reconciliation observation would state it."""

    return tuple(
        sorted(
            f"{group}:{_report_identity(report)}"
            for group, values in (
                ("position", reports.positions),
                ("regular_order", reports.regular_orders),
                ("algo_order", reports.algo_orders),
            )
            for report in values
        )
    )


def _observe_reconciliation(
    *,
    audit: AuditSink,
    result: _PrivateReconciliationResult,
    previous_identity: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Append an observation only when the account changed, and return the identity just seen.

    A steady scan that finds the same positions and orders states nothing the current projection row
    does not already carry; it was 6996 of the ledger's 7019 rows (#510 E). Current state belongs in
    the projection and the ledger keeps the changes, but any non-steady trigger still appends, because
    a reconciliation someone asked for is itself the fact.
    """

    identity = _reconciliation_identity(result.reports)
    if result.triggers == ("steady",) and identity == previous_identity:
        return identity
    reports = result.reports
    positions = reports.positions
    orders = reports.orders
    observed_at_ns = result.observed_at_ns
    references: set[str] = set()
    for report in (*positions, *orders):
        for name in ("client_order_id", "venue_order_id", "position_id", "instrument_id"):
            value = getattr(report, name, None)
            if value is not None:
                references.add(str(getattr(value, "value", value)))
    bounded_references = tuple(sorted(references)[:16])
    account_flat = reports.account_flat
    audit.offer(
        audit.factory.create(
            normalized_kind="reconciliation",
            occurred_at_ns=observed_at_ns,
            observed_at_ns=observed_at_ns,
            native_identity_references=bounded_references,
            summary={
                "source": "binance_private_api",
                "trigger": "+".join(result.triggers),
                "duration_us": result.duration_ns // 1_000,
                "positions": len(positions),
                "regular_orders": len(reports.regular_orders),
                "algo_orders": len(reports.algo_orders),
                "orders": len(orders),
                "account_flat": account_flat,
                "native_refs_truncated": len(references) > len(bounded_references),
            },
            payload={
                "source": "binance_private_api",
                "triggers": list(result.triggers),
                "duration_ns": result.duration_ns,
                "position_reports": len(positions),
                "regular_order_reports": len(reports.regular_orders),
                "algo_order_reports": len(reports.algo_orders),
                "order_reports": len(orders),
                "account_flat": account_flat,
                "rate_limit_headers_observed": False,
                "native_identity_references": bounded_references,
            },
            event_identity=f"binance-private:{observed_at_ns}",
        )
    )
    return identity


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


def _probe_payload(state: ExecutionRuntimeState) -> dict[str, Any]:
    return {
        "ok": state.alive and state.execution_safe,
        "alive": state.alive,
        "execution_safe": state.execution_safe,
        "entries_armed": state.entries_armed,
        "entry_block_reason": state.entry_block_reason,
        "mode": state.mode,
        "account_slot": state.account_slot,
        "runtime_release": state.runtime_release,
        "config_sha256": state.config_sha256,
        "runtime_revision": state.runtime_revision,
        "image_digest": state.image_digest,
        "credential_fingerprint": state.credential_fingerprint,
        "singleton_ready": state.singleton_ready,
        "startup_reconciled": state.startup_reconciled,
        "portfolio_ready": state.portfolio_ready,
        "control_plane_ready": state.control_plane_ready,
        "audit_ready": state.audit_ready,
        "day_start_ready": state.day_start_ready,
        "unexpected_exposure": state.unexpected_exposure,
        "account_flat": state.account_flat,
        "positions_count": state.positions_count,
        "open_orders_count": state.open_orders_count,
        "protection_status": state.protection_status,
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
