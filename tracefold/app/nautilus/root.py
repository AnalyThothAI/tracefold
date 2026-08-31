"""Composition root for the independent Nautilus execution process."""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from decimal import Decimal
from queue import Full
from threading import Lock
from typing import Any, cast

import uvicorn
from loguru import logger
from nautilus_trader.adapters.binance import (
    BINANCE,
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from tracefold.app.repository_session import repositories
from tracefold.app.trading_bindings import (
    BindingCredentialFact,
    load_binance_demo_credential_snapshot,
)
from tracefold.integrations.nautilus import (
    NAUTILUS_RELEASE,
    BinanceCredentials,
    account_execution_adapter,
    build_node_config,
    execution_clients,
    installed_nautilus_wheel_identity,
    load_complete_account_reports,
    load_funding_cashflows,
)
from tracefold.integrations.nautilus.messages import (
    QuoteStreamChanged,
    StartupAccountReconciliationConfirmed,
    StartupAccountReconciliationUnproven,
    StrategyCommand,
    StrategyQueues,
    VenueFlatConfirmed,
    VenueFlatProofRequested,
    VenueFlatUnproven,
    strategy_queues,
)
from tracefold.integrations.nautilus.strategy import TracefoldNautilusStrategy
from tracefold.platform.config.models import Settings
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading import (
    BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
    PROTECTION_CONTRACT_SHA256,
    QUOTE_CONTRACT_SHA256,
    ExecutionBindingV1,
    VenueBinding,
    canonical_sha256,
    require_execution_binding_enabled,
)
from tracefold.trading.intent import INTENT_POLICY_SHA256

from .database import NAUTILUS_POLL_SECONDS, NautilusDatabaseBridge
from .probe import create_nautilus_probe_app

_INTERNAL_PORT = 8767
_SHUTDOWN_TIMEOUT_SECONDS = 15.0
_COMMAND_ENQUEUE_TIMEOUT_SECONDS = 5.0
_BOOTSTRAP_CAPABILITY_IDENTITY = "bootstrap-zero-claims-v1"


def run_nautilus(settings: Settings, *, bootstrap_zero_claims: bool = False) -> None:
    """Build and run the single zero-claim or Binance USD-M Demo node."""

    credential_snapshot = load_binance_demo_credential_snapshot(settings)
    binance_credentials = (
        BinanceCredentials(
            api_key=credential_snapshot.api_key,
            api_secret=credential_snapshot.api_secret,
        )
        if credential_snapshot.fact.state == "configured"
        and credential_snapshot.api_key is not None
        and credential_snapshot.api_secret is not None
        else None
    )
    configured_bindings: set[VenueBinding] = {"BINANCE_USDM"} if binance_credentials is not None else set()
    with repositories(settings, role="nautilus") as repos:
        now_ms = int(time.time() * 1_000)
        capability_snapshots = {
            binding: repos.trading.active_execution_capability_snapshot(binding=binding)
            for binding in configured_bindings
        }
        binding_runtimes = {
            binding: repos.trading.binding_runtime(binding=binding, now_ms=now_ms) for binding in configured_bindings
        }
        active_execution_bindings = {
            binding: repos.trading.active_execution_binding(binding=binding) for binding in configured_bindings
        }
        if bootstrap_zero_claims:
            if repos.trading.capital_control() != "PAUSED":
                raise RuntimeError("nautilus_bootstrap_requires_paused")
            if repos.trading.active_intent_values() is not None:
                raise RuntimeError("nautilus_bootstrap_requires_no_active_intent")
        for snapshot in capability_snapshots.values():
            if snapshot is not None:
                _require_current_capability_contract(snapshot)
    pending_execution_bindings = {
        binding: value
        for binding in configured_bindings
        if (
            value := _pending_execution_binding(
                binding=binding,
                credential=credential_snapshot.fact,
                runtime=binding_runtimes[binding],
                capability_snapshot=capability_snapshots[binding],
                active=active_execution_bindings[binding],
            )
        )
        is not None
    }
    capabilities_by_binding = {
        binding: (
            {}
            if snapshot is None or bootstrap_zero_claims
            else {row.instrument_id: row for row in snapshot.included.values()}
        )
        for binding, snapshot in capability_snapshots.items()
    }
    capabilities = {
        instrument_id: row
        for binding_capabilities in capabilities_by_binding.values()
        for instrument_id, row in binding_capabilities.items()
    }
    instrument_ids_by_binding = {
        binding: [InstrumentId.from_str(value) for value in sorted(capabilities_by_binding[binding])]
        for binding in configured_bindings
    }
    instrument_ids = [InstrumentId.from_str(value) for value in sorted(capabilities)]
    bootstrap_mode = not capabilities
    loop = asyncio.new_event_loop()
    node: TradingNode | None = None
    try:
        node = TradingNode(
            config=build_node_config(
                instrument_ids_by_binding=instrument_ids_by_binding,
                binance_credentials=binance_credentials,
            ),
            loop=loop,
        )
        queues = strategy_queues()
        quote_stream = _QuoteStreamGeneration()
        bridge = NautilusDatabaseBridge(
            settings,
            queues,
            capability_snapshot_sha256s={
                binding: None if snapshot is None else snapshot.snapshot_sha256
                for binding, snapshot in capability_snapshots.items()
            },
            pending_execution_bindings=pending_execution_bindings,
        )
        strategy = TracefoldNautilusStrategy(
            engine_identity=_engine_identity(
                settings,
                {
                    binding: (_BOOTSTRAP_CAPABILITY_IDENTITY if snapshot is None else snapshot.snapshot_sha256)
                    for binding, snapshot in capability_snapshots.items()
                },
            ),
            instrument_ids=instrument_ids,
            capabilities=capabilities,
            queues=queues,
            quote_stream_generation=quote_stream.current,
            request_venue_flat=lambda request: _schedule_venue_flat_proof(
                node=node,
                queues=queues,
                loop=loop,
                request=request,
            ),
            request_startup_account_reconciliation=(
                lambda: _schedule_startup_account_reconciliation(
                    node=node,
                    queues=queues,
                    loop=loop,
                    bootstrap_account_zero=bootstrap_mode,
                )
            ),
        )
        node.trader.add_strategy(strategy)
        if binance_credentials is not None:
            node.add_data_client_factory(
                BINANCE,
                _quote_stream_data_client_factory(queues, quote_stream),
            )
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


@dataclass(slots=True)
class _QuoteStreamGeneration:
    generations: dict[VenueBinding, int] = field(default_factory=lambda: {"BINANCE_USDM": 0})
    lock: Lock = field(default_factory=Lock)

    def current(self, binding: VenueBinding) -> int:
        with self.lock:
            return self.generations[binding]

    def reconnected(self, binding: VenueBinding) -> QuoteStreamChanged:
        with self.lock:
            self.generations[binding] += 1
            generation = self.generations[binding]
        return QuoteStreamChanged(binding=binding, connected=True, generation=generation)

    def disconnected(self, binding: VenueBinding) -> QuoteStreamChanged:
        with self.lock:
            self.generations[binding] += 1
            generation = self.generations[binding]
        return QuoteStreamChanged(binding=binding, connected=False, generation=generation)


def _quote_stream_data_client_factory(
    queues: StrategyQueues,
    generation: _QuoteStreamGeneration,
) -> type[BinanceLiveDataClientFactory]:
    async def on_reconnect() -> None:
        await _enqueue_strategy_command(queues, generation.reconnected("BINANCE_USDM"))

    class QuoteStreamBinanceLiveDataClientFactory(BinanceLiveDataClientFactory):
        @staticmethod
        def create(
            loop: asyncio.AbstractEventLoop,
            name: str,
            config: Any,
            msgbus: Any,
            cache: Any,
            clock: Any,
        ) -> Any:
            client = BinanceLiveDataClientFactory.create(
                loop=loop,
                name=name,
                config=config,
                msgbus=msgbus,
                cache=cache,
                clock=clock,
            )
            _chain_binance_reconnect_callbacks(client, on_reconnect)
            return client

    return QuoteStreamBinanceLiveDataClientFactory


def _chain_binance_reconnect_callbacks(
    client: Any,
    on_reconnect: Callable[[], Awaitable[None]],
) -> None:
    """Chain Tracefold invalidation onto both reconnect callbacks in the pinned adapter."""

    for name in ("_ws_client", "_ws_public_client"):
        websocket = getattr(client, name, None)
        if websocket is None:
            raise RuntimeError("nautilus_binance_reconnect_callback_unavailable")
        original = getattr(websocket, "_handler_reconnect", None)
        if original is None or not callable(original):
            raise RuntimeError("nautilus_binance_reconnect_callback_unavailable")
        typed_original = cast(Callable[[], Awaitable[None]], original)

        async def chained(original: Callable[[], Awaitable[None]] = typed_original) -> None:
            await on_reconnect()
            await original()

        websocket._handler_reconnect = chained


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


def _engine_identity(settings: Settings, capability_snapshot_sha256s: dict[VenueBinding, str]) -> str:
    del settings
    identity = runtime_identity()
    config_payload = {
        "version": "nautilus_binance_usdm_demo_config_v1",
        "release": NAUTILUS_RELEASE.version,
        "capability_snapshot_sha256s": dict(sorted(capability_snapshot_sha256s.items())),
        "poll_seconds": str(NAUTILUS_POLL_SECONDS),
        "environment": "DEMO",
        "cache": "memory",
        "reconciliation": True,
        "reconciliation_scope": "dedicated_account",
        "inflight_check_interval_ms": 0,
        "external_order_claims": "capability_snapshot",
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


def _pending_execution_binding(
    *,
    binding: VenueBinding,
    credential: BindingCredentialFact,
    runtime: Any,
    capability_snapshot: Any,
    active: ExecutionBindingV1 | None,
) -> ExecutionBindingV1 | None:
    require_execution_binding_enabled(binding)
    if runtime is None or capability_snapshot is None:
        return None
    _require_current_capability_contract(capability_snapshot)
    if (
        credential.state != "configured"
        or credential.fingerprint is None
        or runtime.credential_fingerprint != credential.fingerprint
        or runtime.account_generation < 1
        or runtime.catalog_snapshot_sha256 != capability_snapshot.catalog_snapshot_sha256
    ):
        return None
    value = ExecutionBindingV1(
        binding=binding,
        venue="binance.usdm",
        account_identity_sha256=canonical_sha256(
            {
                "identity_version": "credential_scoped_account_v1",
                "binding": binding,
                "credential_fingerprint": credential.fingerprint,
            }
        ),
        account_generation=runtime.account_generation,
        credential_fingerprint=credential.fingerprint,
        catalog_snapshot_sha256=capability_snapshot.catalog_snapshot_sha256,
        capability_snapshot_sha256=capability_snapshot.snapshot_sha256,
        adapter_contract_sha256=BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
        quote_contract_sha256=QUOTE_CONTRACT_SHA256,
        protection_contract_sha256=PROTECTION_CONTRACT_SHA256,
        client_runtime_identity=(
            f"nautilus-trader=={NAUTILUS_RELEASE.version};wheel={installed_nautilus_wheel_identity()}"
        ),
        created_at_ms=int(time.time() * 1_000),
    )
    if active is not None and active.model_dump(exclude={"created_at_ms"}) == value.model_dump(
        exclude={"created_at_ms"}
    ):
        return None
    return value


def _require_current_capability_contract(capability_snapshot: Any) -> None:
    if (
        capability_snapshot.binding != "BINANCE_USDM"
        or capability_snapshot.venue != "binance.usdm"
        or capability_snapshot.adapter_contract_sha256 != BINANCE_USDM_ADAPTER_CONTRACT_SHA256
        or capability_snapshot.quote_contract_sha256 != QUOTE_CONTRACT_SHA256
        or capability_snapshot.protection_contract_sha256 != PROTECTION_CONTRACT_SHA256
        or capability_snapshot.client_runtime_identity
        != f"nautilus-trader=={NAUTILUS_RELEASE.version};wheel={installed_nautilus_wheel_identity()}"
    ):
        raise RuntimeError("nautilus_capability_contract_mismatch")


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
    task.add_done_callback(_log_account_proof_task_failure)


def _schedule_startup_account_reconciliation(
    *,
    node: TradingNode,
    queues: StrategyQueues,
    loop: asyncio.AbstractEventLoop,
    bootstrap_account_zero: bool,
) -> None:
    task = loop.create_task(
        _run_startup_account_reconciliation(
            node=node,
            queues=queues,
            bootstrap_account_zero=bootstrap_account_zero,
        ),
        name="startup-account-reconciliation",
    )
    task.add_done_callback(_log_account_proof_task_failure)


def _log_account_proof_task_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("Nautilus account proof task failed ({})", type(error).__name__)


async def _run_startup_account_reconciliation(
    *,
    node: TradingNode,
    queues: StrategyQueues,
    bootstrap_account_zero: bool,
) -> None:
    observed_at_ms = int(node.kernel.clock.timestamp_ms())
    try:
        clients = execution_clients(node.kernel.exec_engine)
        reconciliations = {
            binding: await account_execution_adapter(
                binding,
                client,
                account_report_loader=load_complete_account_reports,
            ).reconcile_account(binding)
            for binding, client in clients.items()
        }
        if bootstrap_account_zero and any(
            reconciliation.position_reports or reconciliation.order_reports
            for reconciliation in reconciliations.values()
        ):
            raise RuntimeError("nautilus_bootstrap_account_not_empty")
        if not bootstrap_account_zero:
            reconciliation_succeeded = True
            for binding, reconciliation in reconciliations.items():
                client = clients[binding]
                for report in (*reconciliation.position_reports, *reconciliation.order_reports):
                    if report.account_id != client.account_id:
                        raise RuntimeError("nautilus_startup_report_account_mismatch")
                    reconciliation_succeeded = (
                        node.kernel.exec_engine.reconcile_execution_report(report) and reconciliation_succeeded
                    )
            if not reconciliation_succeeded:
                raise RuntimeError("nautilus_startup_report_reconciliation_failed")
        verified_at_ms = int(node.kernel.clock.timestamp_ms())
        command: StrategyCommand = StartupAccountReconciliationConfirmed(
            verified_at_ms=verified_at_ms,
            bootstrap_account_zero=bootstrap_account_zero,
        )
    except Exception as exc:
        unexpected_exposure = isinstance(exc, RuntimeError) and str(exc) == "nautilus_bootstrap_account_not_empty"
        logger.warning("Nautilus startup account reconciliation was not established ({})", type(exc).__name__)
        command = StartupAccountReconciliationUnproven(
            observed_at_ms=observed_at_ms,
            unexpected_exposure=unexpected_exposure,
        )
    await _enqueue_strategy_command(queues, command)


async def _run_venue_flat_proof(
    *,
    node: TradingNode,
    queues: StrategyQueues,
    request: VenueFlatProofRequested,
) -> None:
    try:
        verified_at_ms, account_wide_zero, funding_by_currency = await _account_wide_venue_flat_report(
            node=node,
            request=request,
        )
        command: StrategyCommand = VenueFlatConfirmed(
            intent_id=request.intent_id,
            instrument_id=request.instrument_id,
            position_id=request.position_id,
            authoritative_quantity=Decimal(0),
            verified_at_ms=verified_at_ms,
            funding_by_currency=funding_by_currency,
            account_wide_zero=account_wide_zero,
        )
    except Exception as exc:
        logger.warning("Nautilus venue-flat proof was not established ({})", type(exc).__name__)
        command = VenueFlatUnproven(
            intent_id=request.intent_id,
            position_id=request.position_id,
            observed_at_ms=request.observed_at_ms,
        )
    await _enqueue_strategy_command(queues, command)


async def _account_wide_venue_flat_report(
    *,
    node: TradingNode,
    request: VenueFlatProofRequested,
) -> tuple[int, bool, dict[str, str]]:
    """Query one public client for every position and open order in the bound account."""

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

    position_reports, order_reports = await load_complete_account_reports(client)
    for report in position_reports:
        if report.account_id.value != request.account_id:
            raise RuntimeError("account_position_report_scope_invalid")
        if not node.kernel.exec_engine.reconcile_execution_report(report):
            raise RuntimeError("account_position_report_reconciliation_failed")
        raise RuntimeError("account_position_report_not_flat")

    allowed_order_ids = set(request.owned_open_order_ids)
    for report in order_reports:
        client_order_id = None if report.client_order_id is None else report.client_order_id.value
        if report.account_id.value != request.account_id or client_order_id not in allowed_order_ids:
            raise RuntimeError("account_open_order_report_unexpected")
        if not node.kernel.exec_engine.reconcile_execution_report(report):
            raise RuntimeError("account_open_order_report_reconciliation_failed")

    verified_at_ms = node.kernel.clock.timestamp_ns() // 1_000_000
    if verified_at_ms < request.observed_at_ms:
        raise RuntimeError("account_venue_report_stale")
    if request.opened_at_ms is None:
        raise RuntimeError("account_funding_window_unproven")
    funding_by_currency = await load_funding_cashflows(
        client,
        provider_instrument_id=request.provider_instrument_id,
        opened_at_ms=request.opened_at_ms,
        verified_at_ms=verified_at_ms,
    )
    return verified_at_ms, not order_reports, funding_by_currency


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
