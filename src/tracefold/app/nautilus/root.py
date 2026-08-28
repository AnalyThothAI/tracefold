"""Composition root for the independent Nautilus execution process."""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
from collections.abc import Callable, Sequence
from contextlib import nullcontext, suppress
from decimal import Decimal
from queue import Full
from typing import Any

import uvicorn
from loguru import logger
from nautilus_trader.adapters.binance import (
    BINANCE,
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from tracefold.integrations.nautilus import (
    NAUTILUS_RELEASE,
    build_node_config,
    installed_nautilus_wheel_identity,
)
from tracefold.integrations.nautilus.messages import (
    StrategyCommand,
    StrategyQueues,
    VenueFlatConfirmed,
    VenueFlatProofRequested,
    VenueFlatUnproven,
    strategy_queues,
)
from tracefold.integrations.nautilus.strategy import SOLUSDT_PERP, TracefoldNautilusStrategy
from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading import INTENT_POLICY_SHA256

from .database import NAUTILUS_POLL_SECONDS, NautilusDatabaseBridge
from .probe import create_nautilus_probe_app

_INTERNAL_PORT = 8767
_SHUTDOWN_TIMEOUT_SECONDS = 15.0
_COMMAND_ENQUEUE_TIMEOUT_SECONDS = 5.0


def run_nautilus(settings: Settings) -> None:
    """Build and run the one Binance Demo TradingNode until SIGINT/SIGTERM."""

    api_key, api_secret = _read_credentials(settings)
    loop = asyncio.new_event_loop()
    node: TradingNode | None = None
    try:
        node = TradingNode(
            config=build_node_config(
                api_key=api_key,
                api_secret=api_secret,
                instrument_id=SOLUSDT_PERP,
            ),
            loop=loop,
        )
        queues = strategy_queues()
        bridge = NautilusDatabaseBridge(settings, queues)
        strategy = TracefoldNautilusStrategy(
            engine_identity=_engine_identity(settings),
            queues=queues,
            request_venue_flat=lambda request: _schedule_venue_flat_proof(
                node=node,
                queues=queues,
                loop=loop,
                request=request,
            ),
        )
        node.trader.add_strategy(strategy)
        node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
        node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)
        node.build()
        server = _probe_server(bridge.readiness)
        loop.run_until_complete(_run_runtime(node=node, bridge=bridge, server=server))
    finally:
        if node is not None:
            node.dispose()
        if not loop.is_closed():
            loop.close()


async def _run_runtime(
    *,
    node: TradingNode,
    bridge: NautilusDatabaseBridge,
    server: uvicorn.Server,
) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = _install_signal_handlers(loop, stop_event.set)
    bridge_started = False
    node_task: asyncio.Task[None] | None = None
    probe_task: asyncio.Task[None] | None = None
    try:
        bridge.start()
        bridge_started = True
        probe_task = asyncio.create_task(server.serve(), name="nautilus-probe")
        node_task = asyncio.create_task(node.run_async(), name="nautilus-node")
        await _supervise(
            stop_event=stop_event,
            node_task=node_task,
            probe_task=probe_task,
            bridge=bridge,
        )
    finally:
        server.should_exit = True
        try:
            if node_task is not None and not node_task.done():
                await asyncio.wait_for(node.stop_async(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            pending = [task for task in (node_task, probe_task) if task is not None]
            if pending:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_SHUTDOWN_TIMEOUT_SECONDS,
                )
        finally:
            if bridge_started:
                bridge.stop()
                bridge.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            _remove_signal_handlers(loop, installed_signals)


async def _supervise(
    *,
    stop_event: asyncio.Event,
    node_task: asyncio.Task[None],
    probe_task: asyncio.Task[None],
    bridge: NautilusDatabaseBridge,
) -> None:
    while not stop_event.is_set():
        if bridge.error is not None:
            raise RuntimeError("nautilus_database_failed") from bridge.error
        if node_task.done():
            await node_task
            raise RuntimeError("nautilus_node_returned")
        if probe_task.done():
            await probe_task
            raise RuntimeError("nautilus_probe_returned")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=0.1)


def _probe_server(readiness: Callable[[], dict[str, Any]]) -> uvicorn.Server:
    config = uvicorn.Config(
        create_nautilus_probe_app(readiness),
        host="0.0.0.0",  # noqa: S104 -- compose publishes this internal probe only on host loopback
        port=_INTERNAL_PORT,
        log_config=None,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    server.capture_signals = nullcontext  # type: ignore[method-assign, assignment]
    return server


def _read_credentials(settings: Settings) -> tuple[str, str]:
    key_file = settings.trading_nautilus_api_key_file()
    if key_file is None:
        raise ValueError("nautilus_api_key_file_missing")
    try:
        api_key = read_secure_secret_text(key_file)
    except SecretFileError as exc:
        raise ValueError(f"nautilus_api_key_file_{exc.code}") from None

    secret_file = settings.trading_nautilus_api_secret_file()
    if secret_file is None:
        raise ValueError("nautilus_api_secret_file_missing")
    try:
        api_secret = read_secure_secret_text(secret_file)
    except SecretFileError as exc:
        raise ValueError(f"nautilus_api_secret_file_{exc.code}") from None
    return api_key, api_secret


def _engine_identity(settings: Settings) -> str:
    identity = runtime_identity()
    config_payload = {
        "version": "nautilus_binance_demo_config_v1",
        "release": NAUTILUS_RELEASE.version,
        "instrument_id": SOLUSDT_PERP.value,
        "poll_seconds": str(NAUTILUS_POLL_SECONDS),
        "environment": "BINANCE_DEMO_USDT_FUTURES",
        "cache": "memory",
        "reconciliation": True,
        "reconciliation_scope": "dedicated_account",
        "inflight_check_interval_ms": 0,
        "external_order_claims": [SOLUSDT_PERP.value],
    }
    config_sha256 = hashlib.sha256(
        json.dumps(config_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        f"tracefold@{identity.runtime_revision};"
        f"image@{identity.image_digest};"
        f"nautilus@{NAUTILUS_RELEASE.version}+{NAUTILUS_RELEASE.git_commit};"
        f"wheel@{installed_nautilus_wheel_identity()};"
        f"config@{config_sha256};"
        f"intent-policy@{INTENT_POLICY_SHA256}"
    )


def _schedule_venue_flat_proof(
    *,
    node: TradingNode,
    queues: StrategyQueues,
    loop: asyncio.AbstractEventLoop,
    request: VenueFlatProofRequested,
) -> None:
    task = loop.create_task(
        _run_venue_flat_proof(node=node, queues=queues, request=request),
        name=f"venue-flat-proof:{request.intent_id[:12]}",
    )
    task.add_done_callback(_log_venue_flat_task_failure)


def _log_venue_flat_task_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("Nautilus venue-flat proof task failed ({})", type(error).__name__)


async def _run_venue_flat_proof(
    *,
    node: TradingNode,
    queues: StrategyQueues,
    request: VenueFlatProofRequested,
) -> None:
    try:
        verified_at_ms = await _targeted_venue_flat_report(node=node, request=request)
        command: StrategyCommand = VenueFlatConfirmed(
            intent_id=request.intent_id,
            instrument_id=request.instrument_id,
            position_id=request.position_id,
            authoritative_quantity=Decimal(0),
            verified_at_ms=verified_at_ms,
        )
    except Exception as exc:
        logger.warning("Nautilus venue-flat proof was not established ({})", type(exc).__name__)
        command = VenueFlatUnproven(
            intent_id=request.intent_id,
            position_id=request.position_id,
            observed_at_ms=request.observed_at_ms,
        )
    await _enqueue_strategy_command(queues, command)


async def _targeted_venue_flat_report(
    *,
    node: TradingNode,
    request: VenueFlatProofRequested,
) -> int:
    """Query one public execution client and reconcile its fresh exact-instrument report."""

    instrument_id = InstrumentId.from_str(request.instrument_id)
    closing_order = node.cache.order(ClientOrderId(request.closing_client_order_id))
    if closing_order is None or closing_order.instrument_id != instrument_id:
        raise RuntimeError("closing_order_not_reconciled")

    clients = node.kernel.exec_engine.get_clients_for_orders([closing_order])
    if len(clients) != 1:
        raise RuntimeError("closing_order_execution_client_ambiguous")
    client = next(iter(clients))
    if client.account_id.value != request.account_id:
        raise RuntimeError("closing_order_account_mismatch")

    command = GeneratePositionStatusReports(
        instrument_id=instrument_id,
        start=None,
        end=None,
        command_id=UUID4(),
        ts_init=node.kernel.clock.timestamp_ns(),
    )
    reports = await client.generate_position_status_reports(command)
    if len(reports) != 1:
        raise RuntimeError("targeted_position_report_count_invalid")
    report = reports[0]
    if report.instrument_id != instrument_id or report.account_id.value != request.account_id:
        raise RuntimeError("targeted_position_report_scope_invalid")

    verified_at_ms = int(report.ts_last) // 1_000_000
    if verified_at_ms < request.observed_at_ms:
        raise RuntimeError("targeted_position_report_stale")
    reconciled = node.kernel.exec_engine.reconcile_execution_report(report)
    if not reconciled:
        raise RuntimeError("targeted_position_report_reconciliation_failed")
    if report.position_side != PositionSide.FLAT or report.quantity.as_decimal() != 0:
        raise RuntimeError("targeted_position_report_not_flat")
    return verified_at_ms


async def _enqueue_strategy_command(queues: StrategyQueues, command: StrategyCommand) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _COMMAND_ENQUEUE_TIMEOUT_SECONDS
    while True:
        try:
            queues.commands.put_nowait(command)
            return
        except Full:
            if loop.time() >= deadline:
                raise RuntimeError("nautilus_command_queue_stalled") from None
            await asyncio.sleep(0.05)


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


def _remove_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    installed: Sequence[signal.Signals],
) -> None:
    for signum in installed:
        loop.remove_signal_handler(signum)


__all__ = ["run_nautilus"]
