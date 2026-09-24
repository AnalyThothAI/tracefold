"""Composition root for the one Binance USD-M OI execution Runtime.

Nautilus owns execution state (#680): its startup reconciliation rebuilds the Cache from the venue
before the Strategy starts, and its continuous checks keep it converged. This root only supervises.
It holds the account-slot lock, builds a node generation, reads the venue's positions every 30 s for
the Strategy's venue-truth invariant (#680 PR-3), publishes what the Strategy reports, and rebuilds
the generation after a failure it can outlive. The process exits for exactly three reasons:
a configuration or credential file it cannot use, a database schema it cannot read, and the loss of
the account-slot lock. A network, venue or database blip is never one of them (#680 RC1).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

import uvicorn
from alembic.script import ScriptDirectory
from loguru import logger
from nautilus_trader.adapters.binance import (
    BINANCE,
    BinanceAccountType,
    BinanceFuturesInstrumentProvider,
    BinanceInstrumentProviderConfig,
    BinanceLiveDataClientFactory,
)
from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
from nautilus_trader.common.component import LiveClock
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import AccountId

from tracefold.app.nautilus.oi_runtime import (
    RUNTIME_HEARTBEAT_INTERVAL_NS,
    OiRuntimeDatabaseBridge,
    RuntimeStateProjector,
    load_runtime_inputs,
)
from tracefold.app.process import create_probe_app, install_signal_handlers, remove_signal_handlers
from tracefold.app.repository_session import RepositorySession, postgres_connection, repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.binance import BinanceVenuePositions, OiBinanceExecClientFactory
from tracefold.integrations.nautilus.oi_runtime.config import (
    ActiveRuntimeMode,
    BinanceRuntimeCredentials,
    OiExitPolicy,
    OiInstrumentRoute,
    OiRiskLimits,
    OiRuntimeProfile,
    binance_environment,
    build_oi_node_config,
    route_catalogue,
)
from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.risk import account_equity_usd
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy, RuntimeView
from tracefold.integrations.nautilus.oi_runtime.venue import watch_venue
from tracefold.platform.config.models import Settings, TradingExitPolicySettings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.postgres.client import postgres_health_check
from tracefold.platform.postgres.migrations import alembic_config, latest_migration_version
from tracefold.trading.execution_contracts import EXECUTION_STRATEGY_ID
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState

_EXECUTION_STRATEGY = EXECUTION_STRATEGY_ID
_INTERNAL_PORT = 8767
_STOP_TIMEOUT_SECONDS = 20.0
# Connect (30 s), startup reconciliation (60 s) and portfolio (10 s), each bounded by the node config.
_START_TIMEOUT_SECONDS = 120.0
_HEARTBEAT_INTERVAL_SECONDS = RUNTIME_HEARTBEAT_INTERVAL_NS / 1_000_000_000
# How long a failed generation waits before the next one is built: the venue or the network that
# failed it gets time to come back, and a persistent failure costs one attempt a minute.
_REBUILD_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)
_BINANCE_USDM_ACCOUNT_ID = AccountId("BINANCE-USDT_FUTURES-master")


def _recovery_max_holding_ns(configured_ns: int, open_plan_holding_ns: Iterable[int]) -> int:
    """Keep the configured lookback when there are no plans to recover."""

    return max((configured_ns, *open_plan_holding_ns))


class RuntimeFatal(RuntimeError):
    """A reason this process must stop rather than rebuild: configuration, credentials or the lock."""


class _GenerationFailed(RuntimeError):
    """This node generation cannot go on; the next one may."""


@dataclass(slots=True)
class _ProbeState:
    payload: dict[str, Any]
    lock: Lock

    @classmethod
    def starting(cls, *, mode: str, account_slot: str) -> _ProbeState:
        return cls(
            payload={
                "ok": False,
                "alive": False,
                "entries_armed": False,
                "entry_block_reason": "runtime_starting",
                "mode": mode,
                "account_slot": account_slot,
                "unexpected_exposure": False,
                "positions_count": 0,
                "open_orders_count": 0,
                "protection_status": "not_applicable",
                "heartbeat_at_ns": 0,
            },
            lock=Lock(),
        )

    def publish(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.payload = dict(payload)

    def readiness(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.payload)


def run_nautilus(settings: Settings) -> None:
    """Supervise the configured paper/live node, or say why there is nothing to supervise."""

    execution = settings.trading.execution
    if execution.mode == "disabled":
        logger.info("Execution runtime disabled by configuration; no Binance node is built")
        return
    mode = execution.mode
    credentials = _read_credentials(settings)
    with postgres_connection(settings, application_name="tracefold_nautilus_singleton", long_lived=True) as conn:
        _require_current_schema(conn)
        singleton_repos = repositories_for_connection(conn)
        singleton = AccountSlotSingleton(
            account_slot=execution.account_slot,
            try_acquire=singleton_repos.trading.try_acquire_execution_account_slot,
            release=singleton_repos.trading.release_execution_account_slot,
            heartbeat=lambda: bool(conn.execute("SELECT 1 AS alive").fetchone()["alive"]),
        )
        if not singleton.acquire():
            raise RuntimeFatal("oi_runtime_account_slot_already_owned")
        try:
            asyncio.run(
                _run_active_runtime(
                    settings=settings,
                    mode=mode,
                    credentials=credentials,
                    singleton=singleton,
                    repos=singleton_repos,
                )
            )
        finally:
            singleton.release()


def _require_current_schema(conn: Any) -> None:
    """Refuse to become the account-slot owner against a schema this build cannot read.

    Direction is the whole question (#598 D5-c): a database ahead of this image was migrated by a newer
    deploy and is still readable; one behind it is missing migrations this code compiles against.
    """

    image_head = latest_migration_version()
    health = postgres_health_check(conn, expected_migration_version=image_head)
    if "error" in health or "detail" in health:
        raise RuntimeFatal(f"oi_runtime_schema_probe_failed: {health.get('error')}: {health.get('detail')}")
    if health.get("ok"):
        return
    database_head = health.get("migration_version")
    if _database_precedes_image(database_head, image_head=image_head):
        raise RuntimeFatal(f"oi_runtime_schema_head_mismatch: database={database_head} expected={image_head}")
    logger.warning(
        "Execution runtime starting against a forward-migrated database database={} image={}",
        database_head,
        image_head,
    )


def _database_precedes_image(database_head: Any, *, image_head: str) -> bool:
    """Is the live revision one this image's own history has already passed?"""

    if not database_head:
        return True
    if database_head == image_head:
        return False
    scripts = ScriptDirectory.from_config(alembic_config())
    return database_head in {script.revision for script in scripts.walk_revisions("base", image_head)}


async def _run_active_runtime(
    *,
    settings: Settings,
    mode: ActiveRuntimeMode,
    credentials: BinanceRuntimeCredentials,
    singleton: AccountSlotSingleton,
    repos: RepositorySession,
) -> None:
    """Hold the probe and the lock for the process's life; build node generations until told to stop."""

    execution = settings.trading.execution
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    probe = _ProbeState.starting(mode=mode, account_slot=execution.account_slot)
    server = _probe_server(probe.readiness)
    installed_signals = install_signal_handlers(loop, stop.set)
    probe_task = asyncio.create_task(server.serve(), name="oi-nautilus-probe")
    failures = 0
    try:
        while not stop.is_set():
            try:
                await _run_generation(
                    settings=settings,
                    mode=mode,
                    credentials=credentials,
                    singleton=singleton,
                    repos=repos,
                    stop=stop,
                    probe=probe,
                )
                failures = 0
            except RuntimeFatal:
                raise
            except Exception as exc:
                delay = _REBUILD_BACKOFF_SECONDS[min(failures, len(_REBUILD_BACKOFF_SECONDS) - 1)]
                failures += 1
                logger.opt(exception=exc).warning(
                    "Execution runtime generation ended ({}); rebuilding in {} s", _failure_name(exc), delay
                )
                probe.publish(
                    {
                        **probe.readiness(),
                        "ok": False,
                        "alive": False,
                        "entries_armed": False,
                        "entry_block_reason": "runtime_rebuilding",
                    }
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
    finally:
        server.should_exit = True
        with suppress(TimeoutError, Exception):
            await asyncio.wait_for(probe_task, timeout=_STOP_TIMEOUT_SECONDS)
        remove_signal_handlers(loop, installed_signals)


def _failure_name(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, _GenerationFailed) else type(exc).__name__


async def _run_generation(
    *,
    settings: Settings,
    mode: ActiveRuntimeMode,
    credentials: BinanceRuntimeCredentials,
    singleton: AccountSlotSingleton,
    repos: RepositorySession,
    stop: asyncio.Event,
    probe: _ProbeState,
) -> None:
    """One TradingNode, from route discovery to shutdown. Returns only when `stop` was requested."""

    execution = settings.trading.execution
    routes = await _discover_routes(mode, credentials, stop_distance_bps=execution.risk.stop_distance_bps)
    profile = _active_profile(settings, mode, routes)
    inputs = load_runtime_inputs(repos, profile, now_ns=time.time_ns())
    profile = replace(
        profile,
        recovery_max_holding_ns=_recovery_max_holding_ns(
            profile.recovery_max_holding_ns,
            (value.plan.max_holding_ns for value in inputs.open_plans),
        ),
    )
    signals = ExecutionSignalClient(account_slot=profile.account_slot, execution_strategy=_EXECUTION_STRATEGY)
    journal = ExecutionJournal(
        factory=ObservationFactory(account_slot=profile.account_slot, execution_strategy=_EXECUTION_STRATEGY)
    )
    loop = asyncio.get_running_loop()

    def dispatch_pump_on_loop(pump: Callable[[], None]) -> None:
        """The timer's only job: hand its pump to the thread that runs every Nautilus callback (#510 F)."""

        loop.call_soon_threadsafe(pump)

    strategy = OiNautilusStrategy(
        profile=profile,
        signals=signals,
        journal=journal,
        inputs=inputs,
        dispatch_pump=dispatch_pump_on_loop,
        singleton_ready=lambda: singleton.acquired,
        venue_reads=True,
    )
    node = _build_active_node(
        profile=profile,
        credentials=credentials,
        strategy=strategy,
        loop=loop,
        log_directory=settings.log_file.parent,
    )
    node_task = asyncio.create_task(node.run_async(), name="oi-nautilus-node")
    bridge: OiRuntimeDatabaseBridge | None = None
    projector: RuntimeStateProjector | None = None
    venue_task: asyncio.Task[None] | None = None
    try:
        if not await _await_node_started(node=node, node_task=node_task, stop=stop):
            return
        # The venue-truth read runs beside the node on the same loop, so every reading reaches the
        # Strategy on the thread that owns the Cache. It reads, and nothing else (#680 PR-3).
        venue = BinanceVenuePositions(mode=mode, credentials=credentials)
        venue_task = asyncio.create_task(
            watch_venue(venue.read, strategy.observe_venue, stop), name="oi-venue-positions"
        )
        started_at_ns = time.time_ns()
        state = _runtime_state(
            profile=profile,
            view=strategy.runtime_view(started_at_ns),
            now_ns=started_at_ns,
            base=None,
        )
        projector = RuntimeStateProjector(initial=state)
        projector.start(repos)
        bridge = OiRuntimeDatabaseBridge(
            settings=settings,
            profile=profile,
            signals=signals,
            journal=journal,
            update_day_start=strategy.update_day_start,
            singleton=singleton,
            projector=projector,
        )
        bridge.start()
        logger.info(
            "Execution runtime generation running account_slot={} mode={} routes={} open_plans={}",
            profile.account_slot,
            profile.mode,
            len(profile.routes),
            len(inputs.open_plans),
        )
        view_failure: str | None = None
        while not stop.is_set():
            if node_task.done():
                raise _GenerationFailed("oi_runtime_node_stopped")
            # The heartbeat that proves the lock's session is alive runs on the bridge thread; this is
            # the same fail-closed read, taken from memory.
            if not singleton.acquired:
                raise RuntimeFatal("oi_runtime_account_slot_lost")
            now_ns = time.time_ns()
            try:
                view = strategy.runtime_view(now_ns)
                bridge.set_equity(account_equity_usd(cache=node.cache, account_id=profile.account_id), now_ns)
                state = _runtime_state(profile=profile, view=view, now_ns=now_ns, base=state)
                projector.offer(state)
                probe.publish(_probe_payload(state))
                view_failure = None
            except Exception as exc:
                # Reading the Cache for the projection is not a reason to stop trading; the heartbeat
                # goes stale instead, which is how every reader already decides a Runtime is gone.
                if view_failure != type(exc).__name__:
                    logger.opt(exception=exc).error("Execution runtime projection failed")
                view_failure = type(exc).__name__
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
    finally:
        if venue_task is not None:
            venue_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await venue_task
        await _shutdown_generation(
            node=node,
            node_task=node_task,
            bridge=bridge,
            projector=projector,
            singleton=singleton,
            repos=repos,
        )


async def _shutdown_generation(
    *,
    node: TradingNode,
    node_task: asyncio.Task[None],
    bridge: OiRuntimeDatabaseBridge | None,
    projector: RuntimeStateProjector | None,
    singleton: AccountSlotSingleton,
    repos: RepositorySession,
) -> None:
    if projector is not None and singleton.acquired:
        written = projector.current
        stopped_at_ns = max(time.time_ns(), written.heartbeat_at_ns)
        projector.offer(
            replace(
                written,
                alive=False,
                entries_armed=False,
                heartbeat_at_ns=stopped_at_ns,
                entry_block_reason="runtime_stopped",
                updated_at_ns=stopped_at_ns,
            )
        )
        if bridge is None:
            with suppress(Exception):
                projector.write_once(repos)
    # The bridge drains the journal and one last projection write before it closes its session.
    if bridge is not None:
        bridge.stop()
    if node.is_running():
        with suppress(Exception):
            await asyncio.wait_for(node.stop_async(), timeout=_STOP_TIMEOUT_SECONDS)
    with suppress(TimeoutError, Exception):
        await asyncio.wait_for(asyncio.gather(node_task, return_exceptions=True), timeout=_STOP_TIMEOUT_SECONDS)
    if bridge is not None:
        bridge.join(_STOP_TIMEOUT_SECONDS)
    with suppress(Exception):
        node.dispose()


def _runtime_state(
    *,
    profile: OiRuntimeProfile,
    view: RuntimeView,
    now_ns: int,
    base: ExecutionRuntimeState | None,
) -> ExecutionRuntimeState:
    if base is None:
        return ExecutionRuntimeState(
            account_slot=profile.account_slot,
            mode=profile.mode,
            runtime_id=uuid4(),
            alive=True,
            entries_armed=view.entries_armed,
            unexpected_exposure=view.unexpected_exposure,
            positions_count=view.positions_count,
            open_orders_count=view.open_orders_count,
            protection_status=view.protection_status,
            heartbeat_at_ns=now_ns,
            entry_block_reason=view.entry_block_reason,
            started_at_ns=now_ns,
            updated_at_ns=now_ns,
            account_snapshot=view.account_snapshot,
            routes_count=len(profile.routes),
        )
    return replace(
        base,
        alive=True,
        entries_armed=view.entries_armed,
        unexpected_exposure=view.unexpected_exposure,
        positions_count=view.positions_count,
        open_orders_count=view.open_orders_count,
        protection_status=view.protection_status,
        heartbeat_at_ns=now_ns,
        entry_block_reason=view.entry_block_reason,
        updated_at_ns=now_ns,
        account_snapshot=view.account_snapshot,
    )


def _active_profile(
    settings: Settings,
    mode: ActiveRuntimeMode,
    routes: tuple[OiInstrumentRoute, ...],
) -> OiRuntimeProfile:
    execution = settings.trading.execution
    # Every deterministic client order id this Runtime can claim lives under this namespace, so the
    # account slot and the mode are what a restart matches intent under (#520 PR-A).
    namespace = f"tracefold:{execution.account_slot}:{mode}"
    exit_policy = execution.exit_policy
    if exit_policy is None:
        if mode == "live":
            raise RuntimeFatal("trading_execution_live_exit_policy_required")
        exit_policy = TradingExitPolicySettings(take_profit_bps=200, max_holding_seconds=14_400)
    try:
        return OiRuntimeProfile(
            mode=mode,
            account_slot=execution.account_slot,
            account_id=_BINANCE_USDM_ACCOUNT_ID,
            namespace=namespace,
            routes=routes,
            risk=_risk_limits(settings),
            exit_policy=OiExitPolicy(
                policy_id=exit_policy.policy_id,
                take_profit_bps=exit_policy.take_profit_bps,
                max_holding_ns=exit_policy.max_holding_seconds * 1_000_000_000,
            ),
            excluded_asset_ids=frozenset(settings.trading.analysis.excluded_asset_ids),
            verified_routes=tuple(
                (route.native_symbol, route.asset_id, route.units_per_contract)
                for route in settings.trading.analysis.verified_routes
            ),
        )
    except ValueError as exc:
        if str(exc) == "oi_runtime_routes_missing":
            raise
        raise RuntimeFatal(str(exc)) from exc


def _build_active_node(
    *,
    profile: OiRuntimeProfile,
    credentials: BinanceRuntimeCredentials,
    strategy: OiNautilusStrategy,
    loop: asyncio.AbstractEventLoop,
    log_directory: Path | None = None,
) -> TradingNode:
    node = TradingNode(config=build_oi_node_config(profile, credentials, log_directory=log_directory), loop=loop)
    node.trader.add_strategy(strategy)
    node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
    # Nautilus' Binance client, with fill reports that name each venue trade once (#680 PR-3).
    node.add_exec_client_factory(BINANCE, OiBinanceExecClientFactory)
    node.build()
    if len(node.kernel.exec_engine.registered_clients) != 1:
        raise RuntimeFatal("oi_runtime_execution_client_ambiguous")
    return node


def _risk_limits(settings: Settings) -> OiRiskLimits:
    """The operator's risk section, as the Runtime's entry and sizing policy (#510 E, #680)."""

    risk = settings.trading.execution.risk
    return OiRiskLimits(
        risk_fraction_per_trade=risk.risk_fraction_per_trade,
        max_risk_per_trade_usd=risk.max_risk_per_trade_usd,
        max_positions=risk.max_positions,
        max_leverage=risk.max_leverage,
        max_daily_loss_usd=risk.max_daily_loss_usd,
        max_spread_fraction_of_stop=risk.max_spread_fraction_of_stop,
        post_stop_cooldown_ns=risk.post_stop_cooldown_seconds * 1_000_000_000,
        market_stale_after_ns=int(risk.market_stale_after_seconds * 1_000_000_000),
    )


async def _discover_routes(
    mode: ActiveRuntimeMode,
    credentials: BinanceRuntimeCredentials,
    *,
    stop_distance_bps: int,
) -> tuple[OiInstrumentRoute, ...]:
    """The route catalogue, from Nautilus' own Binance instrument provider (#680 RC8).

    The node's providers then load exactly these instruments; the catalogue is filtered here, once,
    and an empty one is a failure of this generation, not of the process.
    """

    clock = LiveClock()
    client = get_cached_binance_http_client(
        clock=clock,
        account_type=BinanceAccountType.USDT_FUTURES,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        environment=binance_environment(mode),
    )
    provider = BinanceFuturesInstrumentProvider(
        client=client,
        clock=clock,
        account_type=BinanceAccountType.USDT_FUTURES,
        config=BinanceInstrumentProviderConfig(load_all=True, query_commission_rates=False),
    )
    await provider.load_all_async()
    routes = route_catalogue(provider.list_all(), stop_distance_bps=stop_distance_bps)
    if not routes:
        raise _GenerationFailed("oi_runtime_route_catalog_empty")
    return routes


async def _await_node_started(*, node: TradingNode, node_task: asyncio.Task[None], stop: asyncio.Event) -> bool:
    """True once the Strategy runs -- after Nautilus reconciled the venue -- and False on a stop request."""

    deadline = asyncio.get_running_loop().time() + _START_TIMEOUT_SECONDS
    while not node.trader.is_running:
        if stop.is_set():
            return False
        if node_task.done():
            raise _GenerationFailed("oi_runtime_node_returned_during_start")
        if asyncio.get_running_loop().time() >= deadline:
            raise _GenerationFailed("oi_runtime_start_timeout")
        await asyncio.sleep(0.05)
    return True


def _probe_payload(state: ExecutionRuntimeState) -> dict[str, Any]:
    return {
        "ok": state.alive,
        "alive": state.alive,
        "entries_armed": state.entries_armed,
        "entry_block_reason": state.entry_block_reason,
        "mode": state.mode,
        "account_slot": state.account_slot,
        "unexpected_exposure": state.unexpected_exposure,
        "positions_count": state.positions_count,
        "open_orders_count": state.open_orders_count,
        "protection_status": state.protection_status,
        "heartbeat_at_ns": state.heartbeat_at_ns,
    }


def _read_credentials(settings: Settings) -> BinanceRuntimeCredentials:
    return BinanceRuntimeCredentials(
        api_key=_read_secret(settings.trading_binance_usdm_api_key_file(), "api_key"),
        api_secret=_read_secret(settings.trading_binance_usdm_api_secret_file(), "api_secret"),
    )


def _read_secret(path: Any, name: str) -> str:
    if path is None:
        raise RuntimeFatal(f"oi_runtime_{name}_file_missing")
    try:
        return read_secure_secret_text(path)
    except SecretFileError as exc:
        raise RuntimeFatal(f"oi_runtime_{name}_file_{exc.code}") from None


def _probe_server(readiness: Callable[[], dict[str, Any]]) -> uvicorn.Server:
    config = uvicorn.Config(
        # Always 200, payload and all: this endpoint is the operator's diagnosis of the process that
        # owns live exposure, not a gate anything waits on. The Compose healthcheck asks `/healthz`.
        create_probe_app(
            title="Tracefold Nautilus Probe",
            readiness=readiness,
            readiness_status_gate=False,
        ),
        host="0.0.0.0",  # noqa: S104 -- Compose publishes only on operator-selected host loopback
        port=_INTERNAL_PORT,
        log_config=None,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    server.capture_signals = nullcontext  # type: ignore[method-assign, assignment]
    return server


__all__ = ["RuntimeFatal", "run_nautilus"]
