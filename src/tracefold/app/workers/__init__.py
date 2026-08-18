from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any
from uuid import uuid4

import uvicorn
from loguru import logger
from psycopg import OperationalError
from psycopg.errors import IdleInTransactionSessionTimeout, LockNotAvailable, QueryCanceled, TransactionTimeout
from psycopg_pool import PoolTimeout

from tracefold.app.database import WorkerDatabase
from tracefold.app.llm import configured_chat_model, llm_is_configured
from tracefold.app.workers.capabilities import CpuProcess, FiniteOperations, ModelAdapter
from tracefold.app.workers.cpu_prewarm import prewarm_projection_cpu_modules
from tracefold.app.workers.model_arbiter import run_model_arbiter
from tracefold.app.workers.probe import _create_workers_probe_app
from tracefold.app.workers.projection_edf import run_projection_edf
from tracefold.app.workers.runtime import WORKERS_RUNTIME_VERSION, WorkersRuntimeRepository
from tracefold.integrations.feishu import FeishuNewsPushSender
from tracefold.integrations.macro_sources import MacroSourceClient
from tracefold.integrations.opennews import OpenNewsStrategyHistoryClient, OpenNewsWebSocketClient
from tracefold.macro import (
    FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
    FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
    FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
    FedDocumentAnalysisAgent,
    MacroAcquisition,
    MacroAcquisitionService,
    MacroDocumentAnalysisService,
    MacroProjectionCandidate,
    acquisition_loop_policy,
)
from tracefold.news import DecidePolicy
from tracefold.news.agents.triage_model import TriageModel
from tracefold.news.consumers import (
    DeduperConsumer,
    DelivererConsumer,
    JanitorLoop,
    NewsPipeline,
    OpenNewsReceiver,
    RecoveryRunner,
    TriageConsumer,
)
from tracefold.platform.config.settings import (
    Settings,
    news_model_availability,
    news_push_availability,
)
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import postgres_health_check
from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceCapability,
    ResourceOperationOverrun,
)

GRACEFUL_DRAIN_TIMEOUT_SECONDS = 30.0
FATAL_EXIT_TIMEOUT_SECONDS = 5.0
_WORKER_INTERNAL_PORT = 8766
_RUNTIME_REVISION_ENV = "TRACEFOLD_RUNTIME_REVISION"
_HEARTBEAT_SECONDS = 5.0
_CONTROL_TIMEOUT_SECONDS = 1.0
_CONTROL_RETRY_SECONDS = 0.250
_CONTROL_HEARTBEAT_STALE_SECONDS = 15.0
_DOCUMENT_MODEL_TIMEOUT_SECONDS = 180.0
_MODEL_MAX_TOKENS = 6_000
# One TriageVerdict is ~250-300 output tokens (JSON + three Chinese reader strings); successful calls were hitting a
# 300 cap and truncated tool calls surfaced as `news_triage_output_invalid`. Keep ~2x headroom.
_TRIAGE_MAX_TOKENS = 700
_NEWS_OLLAMA_BASE_URL = "http://host.docker.internal:11434/v1"
_PRODUCTIVE_REPOLL_SECONDS = 0.250
_MACRO_CLOCKS = (
    ("macro_intraday_market", "intraday_market"),
    ("macro_settlements", "daily_settlement"),
    ("macro_economic_releases", "scheduled_release"),
    ("macro_official_state", "official_state"),
    ("macro_official_documents", "official_document"),
)


class _Disposition(Enum):
    PROGRESSED = auto()
    RETRY_SOON = auto()
    IDLE = auto()


class _FreshRuntimeRowExists(RuntimeError):
    pass


class _ControlFailure(RuntimeError):
    pass


@dataclass(slots=True)
class _ProbeState:
    runtime_id: str
    runtime_version: str
    started_at_ms: int
    clock_ms: Callable[[], int]
    runtime_revision: str = "unversioned"
    lifecycle_state: str = "starting"
    heartbeat_at_ms: int | None = None
    ready: bool = False
    unavailable_reason: str = "runtime_starting"

    def payload(self) -> dict[str, Any]:
        heartbeat_stale_after_ms = int(_CONTROL_HEARTBEAT_STALE_SECONDS * 1_000)
        heartbeat_current = (
            self.heartbeat_at_ms is not None
            and max(0, int(self.clock_ms()) - int(self.heartbeat_at_ms)) <= heartbeat_stale_after_ms
        )
        ready = self.ready and self.lifecycle_state == "running" and heartbeat_current
        unavailable_reason = self.unavailable_reason
        if self.ready and not heartbeat_current:
            unavailable_reason = "runtime_heartbeat_stale"
        return {
            "ok": ready,
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "runtime_revision": self.runtime_revision,
            "process_id": os.getpid(),
            "lifecycle_state": self.lifecycle_state,
            "started_at_ms": self.started_at_ms,
            "heartbeat_at_ms": self.heartbeat_at_ms,
            "heartbeat_stale_after_ms": heartbeat_stale_after_ms,
            "unavailable_reason": None if ready else unavailable_reason,
        }


@dataclass(slots=True)
class _Components:
    news_pipeline: NewsPipeline | None
    news_bus: Any | None
    macro_source: MacroSourceClient | None
    macro_turns: tuple[MacroAcquisition, ...]
    due_turns: tuple[tuple[Callable[[], Awaitable[bool | str | None]], float], ...]
    projections: tuple[Any, ...]
    models: tuple[Any, ...]
    document_model: MacroDocumentAnalysisService | None


async def run_workers(settings: Settings) -> None:
    """Run the sole Workers process root until an ordered graceful stop."""

    runtime_id = str(uuid4())
    runtime_version = WORKERS_RUNTIME_VERSION
    started_at_ms = _now_ms()
    telemetry = TelemetryRegistry()
    probe_state = _ProbeState(
        runtime_id=runtime_id,
        runtime_version=runtime_version,
        started_at_ms=started_at_ms,
        clock_ms=_now_ms,
        runtime_revision=os.environ.get(_RUNTIME_REVISION_ENV, "").strip() or "unversioned",
    )
    work_stop_event = asyncio.Event()
    control_stop_event = asyncio.Event()
    probe_stop_event = asyncio.Event()
    shutdown_requested = asyncio.Event()
    db: WorkerDatabase | None = None
    lock_conn: Any | None = None
    finite = FiniteOperations(telemetry=telemetry)
    model_adapter = ModelAdapter(telemetry=telemetry)
    projection_cpu = CpuProcess(telemetry=telemetry)
    components: _Components | None = None
    server: uvicorn.Server | None = None
    graceful = False
    phase = "startup"
    signal_loop = asyncio.get_running_loop()
    fatal_watchdog: asyncio.TimerHandle | None = None

    def request_shutdown() -> None:
        probe_state.ready = False
        probe_state.lifecycle_state = "stopping"
        probe_state.unavailable_reason = "runtime_stopping"
        work_stop_event.set()
        shutdown_requested.set()

    def enter_fatal(_exc: BaseException) -> None:
        nonlocal fatal_watchdog
        if fatal_watchdog is not None:
            return
        probe_state.ready = False
        probe_state.lifecycle_state = "failed"
        probe_state.unavailable_reason = "runtime_failed"
        work_stop_event.set()
        control_stop_event.set()
        probe_stop_event.set()
        if server is not None:
            server.should_exit = True
        finite.close_admission()
        model_adapter.close_admission()
        projection_cpu.close_admission()
        if db is not None:
            db.close_business_admission()
        fatal_watchdog = signal_loop.call_later(
            FATAL_EXIT_TIMEOUT_SECONDS,
            os._exit,
            1,
        )

    installed_signals = _install_signal_handlers(signal_loop, request_shutdown)
    try:
        db = WorkerDatabase.create(settings, telemetry=telemetry)
        startup_status = _startup_database_status(db)
        if not startup_status.get("ok"):
            raise RuntimeError(f"workers_postgres_unavailable:{startup_status}")
        lock_conn = db.acquire_steady_runtime_lock()
        db.check_pinned_liveness(lock_conn)
        db.prewarm_control_connection()
        await projection_cpu.prewarm()
        await projection_cpu.run(
            "projection_cpu_modules_prewarm",
            prewarm_projection_cpu_modules,
            service_timeout_seconds=20.0,
        )
        began: bool = await db.run_control(
            "workers_runtime_begin",
            _runtime_begin,
            db,
            runtime_id,
            runtime_version,
            started_at_ms,
            operation_timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
        )
        if not began:
            db.release_steady_runtime_lock(lock_conn)
            lock_conn = None
            finite.close()
            model_adapter.close()
            projection_cpu.close()
            await db.aclose()
            db.close_executors()
            raise _FreshRuntimeRowExists("workers_runtime_fresh_row_exists")

        components = await _wire_components(
            settings=settings,
            db=db,
            telemetry=telemetry,
            finite=finite,
            model_adapter=model_adapter,
            projection_cpu=projection_cpu,
            runtime_id=runtime_id,
        )
        await _reconcile_once(components)
        server = _probe_server(probe_state=probe_state, telemetry=telemetry)

        async with asyncio.TaskGroup() as group:
            business_tasks: list[asyncio.Task[Any]] = []
            control_task: asyncio.Task[Any] | None = None
            probe_task = group.create_task(
                _guard_child(
                    _run_probe(server, stop_event=probe_stop_event),
                    on_fatal=enter_fatal,
                ),
                name="workers-probe",
            )
            if components.news_pipeline is not None:
                for task_name, runner in components.news_pipeline.runners():
                    business_tasks.append(
                        group.create_task(
                            _guard_child(runner(work_stop_event), on_fatal=enter_fatal),
                            name=task_name,
                        )
                    )
            for index, (turn, idle_seconds) in enumerate(components.due_turns):
                business_tasks.append(
                    group.create_task(
                        _guard_child(
                            _run_due(
                                turn,
                                idle_seconds=idle_seconds,
                                stop_event=work_stop_event,
                            ),
                            on_fatal=enter_fatal,
                        ),
                        name=f"durable-due-{index}",
                    )
                )
            business_tasks.append(
                group.create_task(
                    _guard_child(
                        _run_recurring_database_overrun_boundary(
                            lambda: run_projection_edf(
                                components.projections,
                                stop_event=work_stop_event,
                                telemetry=telemetry,
                            ),
                            stop_event=work_stop_event,
                            worker="projection-edf",
                        ),
                        on_fatal=enter_fatal,
                    ),
                    name="projection-edf",
                )
            )
            business_tasks.append(
                group.create_task(
                    _guard_child(
                        _run_recurring_database_overrun_boundary(
                            lambda: run_model_arbiter(
                                components.models,
                                stop_event=work_stop_event,
                            ),
                            stop_event=work_stop_event,
                            worker="model-arbiter",
                        ),
                        on_fatal=enter_fatal,
                    ),
                    name="model-arbiter",
                )
            )

            await _guard_child(
                _wait_for_probe_start(server),
                on_fatal=enter_fatal,
            )
            initial_heartbeat_at_ms = await _guard_child(
                _control_heartbeat_with_retry(
                    db=db,
                    lock_conn=lock_conn,
                    runtime_id=runtime_id,
                    stop_event=shutdown_requested,
                ),
                on_fatal=enter_fatal,
            )
            if initial_heartbeat_at_ms is not None and not shutdown_requested.is_set():
                probe_state.heartbeat_at_ms = initial_heartbeat_at_ms
                await _guard_child(
                    db.run_control(
                        "workers_runtime_running",
                        _runtime_transition,
                        db,
                        runtime_id,
                        "running",
                        None,
                        operation_timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
                    ),
                    on_fatal=enter_fatal,
                )
                probe_state.ready = True
                probe_state.lifecycle_state = "running"
                probe_state.unavailable_reason = ""
                control_task = group.create_task(
                    _guard_child(
                        _run_control(
                            db=db,
                            lock_conn=lock_conn,
                            runtime_id=runtime_id,
                            probe_state=probe_state,
                            stop_event=control_stop_event,
                        ),
                        on_fatal=enter_fatal,
                    ),
                    name="workers-control",
                )
                phase = "runtime"

            await shutdown_requested.wait()
            shutdown_started = signal_loop.time()
            probe_state.ready = False
            probe_state.lifecycle_state = "stopping"
            probe_state.unavailable_reason = "runtime_stopping"
            await _guard_child(
                _within_graceful_deadline(
                    db.run_control(
                        "workers_runtime_stopping",
                        _runtime_transition,
                        db,
                        runtime_id,
                        "stopping",
                        None,
                        operation_timeout_seconds=min(
                            _CONTROL_TIMEOUT_SECONDS,
                            _remaining(shutdown_started),
                        ),
                    ),
                    shutdown_started,
                ),
                on_fatal=enter_fatal,
            )
            work_stop_event.set()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*business_tasks),
                    timeout=_remaining(shutdown_started),
                )
            except TimeoutError as exc:
                enter_fatal(exc)
                raise RuntimeError("graceful_deadline_exceeded") from exc
            phase = "cleanup"
            await _guard_child(
                _graceful_cleanup(
                    started_at=shutdown_started,
                    db=db,
                    finite=finite,
                    model_adapter=model_adapter,
                    projection_cpu=projection_cpu,
                    components=components,
                ),
                on_fatal=enter_fatal,
            )
            control_stop_event.set()
            if control_task is not None:
                await _within(control_task, shutdown_started)
            await _within_graceful_deadline(
                db.run_control(
                    "workers_runtime_stopped",
                    _runtime_transition,
                    db,
                    runtime_id,
                    "stopped",
                    None,
                    operation_timeout_seconds=min(
                        _CONTROL_TIMEOUT_SECONDS,
                        _remaining(shutdown_started),
                    ),
                    allow_shutdown=True,
                ),
                shutdown_started,
            )
            db.close_control_admission()
            if not await db.drain_control(timeout_seconds=_remaining(shutdown_started)):
                raise RuntimeError("worker_database_control_drain_timeout")
            db.release_steady_runtime_lock(lock_conn)
            lock_conn = None
            await _within(db.aclose(), shutdown_started)
            db.close_executors()
            probe_stop_event.set()
            server.should_exit = True
            await _within(probe_task, shutdown_started)
        graceful = True
    except _FreshRuntimeRowExists as exc:
        probe_state.ready = False
        probe_state.lifecycle_state = "failed"
        probe_state.unavailable_reason = "runtime_fresh_row_exists"
        raise RuntimeError("workers_runtime_fresh_row_exists") from exc
    except asyncio.CancelledError:
        probe_state.ready = False
        probe_state.lifecycle_state = "failed"
        probe_state.unavailable_reason = "runtime_failed"
        work_stop_event.set()
        control_stop_event.set()
        probe_stop_event.set()
        if server is not None:
            server.should_exit = True
        raise
    except BaseException as exc:
        enter_fatal(exc)
        probe_state.ready = False
        probe_state.lifecycle_state = "failed"
        probe_state.unavailable_reason = "runtime_failed"
        work_stop_event.set()
        control_stop_event.set()
        probe_stop_event.set()
        if server is not None:
            server.should_exit = True
        await _fatal_exit(
            exc=exc,
            db=db,
            runtime_id=runtime_id,
            finite=finite,
            model_adapter=model_adapter,
            projection_cpu=projection_cpu,
            phase=phase,
        )
    finally:
        _remove_signal_handlers(signal_loop, installed_signals)
        if not graceful:
            # The fatal path deliberately leaves the singleton session open;
            # os._exit is the release authority.
            pass


async def _guard_child(
    awaitable: Awaitable[Any],
    *,
    on_fatal: Callable[[BaseException], None],
) -> Any:
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        on_fatal(exc)
        raise


async def _run_due(
    turn: Callable[[], Awaitable[bool | str | None]],
    *,
    idle_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            value = await turn()
        except ResourceOperationOverrun as exc:
            if exc.capability is not ResourceCapability.DATABASE_BUSINESS:
                raise
            _log_recurring_database_overrun("durable-due", exc)
            await _wait_or_stop(stop_event, float(idle_seconds))
            continue
        disposition = (
            _Disposition.PROGRESSED
            if value is True or value in {"processed", "failed", "terminal"}
            else _Disposition.IDLE
            if value is False
            else _Disposition.RETRY_SOON
        )
        if disposition is _Disposition.PROGRESSED:
            await _wait_or_stop(stop_event, _PRODUCTIVE_REPOLL_SECONDS)
        elif disposition is _Disposition.RETRY_SOON:
            await _wait_or_stop(stop_event, min(float(idle_seconds), 0.250))
        else:
            await _wait_or_stop(stop_event, float(idle_seconds))


async def _run_recurring_database_overrun_boundary(
    start: Callable[[], Awaitable[None]],
    *,
    stop_event: asyncio.Event,
    worker: str,
) -> None:
    while not stop_event.is_set():
        try:
            await start()
            return
        except ResourceOperationOverrun as exc:
            if exc.capability is not ResourceCapability.DATABASE_BUSINESS:
                raise
            _log_recurring_database_overrun(worker, exc)
            await _wait_or_stop(stop_event, _PRODUCTIVE_REPOLL_SECONDS)


def _log_recurring_database_overrun(worker: str, exc: ResourceOperationOverrun) -> None:
    logger.bind(
        worker=worker,
        capability=exc.capability.value,
        operation_name=exc.operation_name,
    ).warning("Recurring database operation outlived its wrapper; native capability remains authoritative")


async def _run_control(
    *,
    db: WorkerDatabase,
    lock_conn: Any,
    runtime_id: str,
    probe_state: _ProbeState,
    stop_event: asyncio.Event,
) -> None:
    try:
        while not stop_event.is_set():
            heartbeat_at_ms = await _control_heartbeat_with_retry(
                db=db,
                lock_conn=lock_conn,
                runtime_id=runtime_id,
                stop_event=stop_event,
            )
            if heartbeat_at_ms is None:
                return
            probe_state.heartbeat_at_ms = heartbeat_at_ms
            await _wait_or_stop(stop_event, _HEARTBEAT_SECONDS)
    except asyncio.CancelledError:
        raise
    except ResourceOperationOverrun:
        raise
    except RuntimeError as exc:
        if "singleton_lost" in str(exc):
            raise
        raise _ControlFailure("workers_control_failed") from exc
    except Exception as exc:
        raise _ControlFailure("workers_control_failed") from exc


async def _control_heartbeat_with_retry(
    *,
    db: WorkerDatabase,
    lock_conn: Any,
    runtime_id: str,
    stop_event: asyncio.Event,
) -> int | None:
    """Retry only the idempotent heartbeat's precise transient DB failures."""

    while True:
        try:
            return await _control_liveness_and_heartbeat(
                db=db,
                lock_conn=lock_conn,
                runtime_id=runtime_id,
            )
        except (
            ResourceAdmissionTimeout,
            LockNotAvailable,
            QueryCanceled,
            TransactionTimeout,
            IdleInTransactionSessionTimeout,
            PoolTimeout,
            OperationalError,
        ) as exc:
            logger.bind(error=type(exc).__name__).warning("Workers control heartbeat transient database failure")
            await _wait_or_stop(stop_event, _CONTROL_RETRY_SECONDS)
            if stop_event.is_set():
                return None


async def _control_liveness_and_heartbeat(
    *,
    db: WorkerDatabase,
    lock_conn: Any,
    runtime_id: str,
) -> int:
    try:
        await db.run_control(
            "singleton_liveness",
            db.check_pinned_liveness,
            lock_conn,
            operation_timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
        )
    except ResourceAdmissionTimeout:
        raise
    except Exception as exc:
        raise RuntimeError("singleton_lost") from exc
    heartbeat_at_ms = _now_ms()
    await db.run_control(
        "workers_runtime_heartbeat",
        _runtime_heartbeat,
        db,
        runtime_id,
        heartbeat_at_ms,
        operation_timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
    )
    return heartbeat_at_ms


async def _run_probe(server: uvicorn.Server, *, stop_event: asyncio.Event) -> None:
    await server.serve()
    if not stop_event.is_set():
        raise RuntimeError("workers_probe_returned")


def _probe_server(
    *,
    probe_state: _ProbeState,
    telemetry: TelemetryRegistry,
) -> uvicorn.Server:
    config = uvicorn.Config(
        _create_workers_probe_app(
            readiness=probe_state.payload,
            render_metrics=telemetry.render_prometheus_text,
        ),
        host="0.0.0.0",  # noqa: S104 -- published only on the host loopback by compose
        port=_WORKER_INTERNAL_PORT,
        log_config=None,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    server.capture_signals = nullcontext
    return server


async def _wait_for_probe_start(server: uvicorn.Server) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    while not server.started:
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("workers_probe_start_timeout")
        await asyncio.sleep(0.010)


async def _wire_components(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry,
    finite: FiniteOperations,
    model_adapter: ModelAdapter,
    projection_cpu: CpuProcess,
    runtime_id: str,
) -> _Components:
    due_turns: list[tuple[Callable[[], Awaitable[bool | str | None]], float]] = []
    model_candidates: list[Any] = []
    news_pipeline: NewsPipeline | None = None
    news_bus: Any | None = None
    if settings.news.enabled:
        news_bus, news_pipeline = await _wire_news_pipeline(settings=settings, db=db, finite=finite)

    document_model: MacroDocumentAnalysisService | None = None
    document_analysis_model_name: str | None = None
    if settings.llm.macro_document_analysis_enabled and llm_is_configured(settings):
        model, effective_model = configured_chat_model(
            settings,
            model_name=settings.llm.macro_document_analysis_model,
            request_timeout_seconds=_DOCUMENT_MODEL_TIMEOUT_SECONDS,
            max_tokens=_MODEL_MAX_TOKENS,
        )
        document_model = MacroDocumentAnalysisService(
            db=db,
            database=db,
            agent=FedDocumentAnalysisAgent(
                model=model,
                model_name=effective_model,
                completion_timeout_seconds=_DOCUMENT_MODEL_TIMEOUT_SECONDS,
            ),
            worker_name="macro_document_analysis",
            lease_owner=f"macro_document_analysis:{runtime_id}",
            stable_order=30,
        )
        document_analysis_model_name = effective_model
        model_candidates.append(document_model)

    macro_source: MacroSourceClient | None = None
    macro_turns: list[MacroAcquisition] = []
    source_config = settings.providers.macro_sources
    if source_config.enabled:
        macro_source = MacroSourceClient(
            user_agent=str(source_config.user_agent),
            fred_enabled=source_config.fred_enabled,
            cboe_enabled=source_config.cboe_enabled,
            cftc_enabled=source_config.cftc_enabled,
            nasdaq_daily_enabled=source_config.nasdaq_daily_enabled,
            yfinance_enabled=source_config.yfinance_enabled,
        )
        for worker_name, clock_kind in _MACRO_CLOCKS:
            turn = MacroAcquisition(
                db=db,
                finite_operations=finite,
                service=MacroAcquisitionService(
                    db=db,
                    worker_name=worker_name,
                    clock_kind=clock_kind,
                    source_client=macro_source,
                    enabled_adapter_ids=macro_source.enabled_adapter_ids,
                    lease_owner=f"{worker_name}:{runtime_id}",
                    document_analysis_model_name=document_analysis_model_name,
                    document_analysis_prompt_version=(
                        FED_DOCUMENT_ANALYSIS_PROMPT_VERSION if document_analysis_model_name is not None else None
                    ),
                    document_analysis_max_attempts=3,
                    document_analysis_fomc_lookback_days=FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
                    document_analysis_speech_lookback_days=FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
                ),
            )
            macro_turns.append(turn)
            idle_seconds = acquisition_loop_policy(clock_kind)[0]
            due_turns.append((turn.turn, idle_seconds))

    projections = (MacroProjectionCandidate(db=db, cpu=projection_cpu, runtime_id=runtime_id, stable_order=20),)
    return _Components(
        news_pipeline=news_pipeline,
        news_bus=news_bus,
        macro_source=macro_source,
        macro_turns=tuple(macro_turns),
        due_turns=tuple(due_turns),
        projections=projections,
        models=tuple(model_candidates),
        document_model=document_model,
    )


async def _wire_news_pipeline(
    *, settings: Settings, db: WorkerDatabase, finite: FiniteOperations
) -> tuple[Any, NewsPipeline]:
    """Broker-driven News V3: one RabbitMQ bus + consumers; models/providers are optional capabilities."""

    from tracefold.integrations.rabbitmq import RabbitMQBus

    broker_url = settings.news.broker.url
    if not broker_url:
        raise RuntimeError("news_broker_url_missing")
    bus = RabbitMQBus(
        url=broker_url,
        name_prefix=settings.news.broker.name_prefix,
        connect_timeout_seconds=settings.news.broker.connect_timeout_seconds,
    )
    await bus.connect()

    strategy_ids = tuple(settings.news.opennews_strategy_ids)
    watchlist_symbols = settings.news.watchlist_symbols
    ws_client = OpenNewsWebSocketClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
    history_client = (
        OpenNewsStrategyHistoryClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
    )

    recovery = (
        RecoveryRunner(bus=bus, db=db, history_client=history_client, strategy_ids=strategy_ids) if ws_client else None
    )
    receiver = (
        OpenNewsReceiver(
            bus=bus,
            db=db,
            ws_client=ws_client,
            history_client=history_client,
            strategy_ids=strategy_ids,
            recovery=recovery,
        )
        if ws_client
        else None
    )

    models = news_model_availability(settings)
    triage_model: TriageModel | None = None
    if models.triage_configured and models.triage_model:
        chat_model, effective = configured_chat_model(
            settings,
            model_name=models.triage_model,
            request_timeout_seconds=settings.news.triage.deadline_seconds + 2.0,
            max_tokens=_TRIAGE_MAX_TOKENS,
        )
        triage_model = TriageModel(
            model=chat_model, model_name=effective, deadline_seconds=settings.news.triage.deadline_seconds
        )

    push = news_push_availability(settings)
    sender = (
        FeishuNewsPushSender(
            webhook_url=str(settings.news.push.feishu_webhook_url),
            signing_secret=settings.news.push.feishu_signing_secret,
        )
        if push.delivery_available
        else None
    )
    pipeline = NewsPipeline(
        receiver=receiver,
        recovery=recovery,
        deduper=DeduperConsumer(
            bus=bus,
            db=db,
            strategy_ids=strategy_ids,
            watchlist_symbols=watchlist_symbols,
            suppress_low_signal=settings.news.gate.suppress_low_signal,
        ),
        triage=TriageConsumer(
            bus=bus,
            db=db,
            model=triage_model,
            watchlist_symbols=watchlist_symbols,
            watchlist=sorted(watchlist_symbols),
            hourly_cap=settings.news.push.hourly_cap,
            concurrency=settings.news.triage.concurrency,
            circuit_failures=settings.news.triage.circuit_failures,
            circuit_open_seconds=settings.news.triage.circuit_open_seconds,
            policy=DecidePolicy(**settings.news.policy.model_dump()),
        ),
        deliverer=DelivererConsumer(
            bus=bus,
            db=db,
            sender=sender,
            finite_operations=finite,
            min_interval_seconds=settings.news.push.min_interval_seconds,
            hourly_cap=settings.news.push.hourly_cap,
        ),
        janitor=JanitorLoop(db=db, bus=bus),
    )
    return bus, pipeline


async def _reconcile_once(components: _Components) -> None:
    for turn in components.macro_turns:
        await turn.reconcile()
    if components.document_model is not None:
        await components.document_model.reconcile()
    macro_projections = tuple(
        projection for projection in components.projections if isinstance(projection, MacroProjectionCandidate)
    )
    if len(macro_projections) > 1:
        raise RuntimeError("macro_projection_candidate_wiring_duplicate")
    if macro_projections:
        await macro_projections[0].reconcile()


async def _graceful_cleanup(
    *,
    started_at: float,
    db: WorkerDatabase,
    finite: FiniteOperations,
    model_adapter: ModelAdapter,
    projection_cpu: CpuProcess,
    components: _Components,
) -> None:
    try:
        db.close_business_admission()
        finite.close_admission()
        model_adapter.close_admission()
        projection_cpu.close_admission()
        if components.news_pipeline is not None:
            await _within(components.news_pipeline.close(), started_at)
        if components.news_bus is not None:
            await _within(components.news_bus.close(), started_at)
        if components.macro_source is not None:
            await _within(
                finite.run(
                    "macro_source_close",
                    components.macro_source.close,
                    timeout_seconds=min(5.0, _remaining(started_at)),
                    allow_shutdown=True,
                ),
                started_at,
            )
        if not await db.drain_business(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("worker_database_business_drain_timeout")
        if not await finite.drain(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("finite_operation_drain_timeout")
        if not await model_adapter.drain(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("model_adapter_drain_timeout")
        if not await projection_cpu.drain(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("cpu_process_drain_timeout")
        finite.close()
        model_adapter.close()
        projection_cpu.close()
    except TimeoutError as exc:
        raise RuntimeError("graceful_deadline_exceeded") from exc
    except Exception as exc:
        if _remaining(started_at) <= 0.001:
            raise RuntimeError("graceful_deadline_exceeded") from exc
        raise


async def _fatal_exit(
    *,
    exc: BaseException,
    db: WorkerDatabase | None,
    runtime_id: str,
    finite: FiniteOperations,
    model_adapter: ModelAdapter,
    projection_cpu: CpuProcess,
    phase: str,
) -> None:
    logger.opt(exception=exc).critical("Workers runtime fatal exit")
    finite.close_admission()
    model_adapter.close_admission()
    projection_cpu.close_admission()
    fatal_code = _fatal_code(exc, phase=phase)
    if db is not None:
        db.close_business_admission()
        try:
            async with asyncio.timeout(max(0.001, FATAL_EXIT_TIMEOUT_SECONDS - 0.5)):
                await db.run_control(
                    "workers_runtime_failed",
                    _runtime_transition,
                    db,
                    runtime_id,
                    "failed",
                    fatal_code,
                    operation_timeout_seconds=1.0,
                    allow_shutdown=True,
                )
        except BaseException:
            os._exit(1)
    os._exit(1)


def _fatal_code(exc: BaseException, *, phase: str) -> str:
    leaves = _leaf_exceptions(exc)
    messages = ":".join(str(item) for item in _leaf_exceptions(exc)).lower()
    if any(isinstance(item, ResourceOperationOverrun) for item in leaves):
        return "resource_operation_overrun"
    if "singleton_lost" in messages:
        return "singleton_lost"
    if "graceful_deadline_exceeded" in messages:
        return "graceful_deadline_exceeded"
    if phase == "startup":
        return "startup_failed"
    if phase == "cleanup":
        return "cleanup_failed"
    if any(isinstance(item, _ControlFailure) for item in leaves):
        return "control_failed"
    if any(
        marker in messages
        for marker in (
            "_invariant_",
            "_cas_failed",
            "_mismatch",
            "parallel_submission",
        )
    ):
        return "runtime_invariant_failed"
    return "child_failed"


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for nested in exc.exceptions:
            leaves.extend(_leaf_exceptions(nested))
        return leaves
    return [exc]


def _startup_database_status(db: WorkerDatabase) -> dict[str, object]:
    with db.worker_pool.connection(timeout=0.250) as conn:
        return postgres_health_check(
            conn,
            expected_migration_version=latest_migration_version(),
        )


def _runtime_begin(
    db: WorkerDatabase,
    runtime_id: str,
    runtime_version: str,
    started_at_ms: int,
) -> bool:
    with db.worker_session("workers_runtime_begin", 1.0) as repos, repos.transaction():
        return WorkersRuntimeRepository(repos.conn).begin(
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            started_at_ms=started_at_ms,
            now_ms=_now_ms(),
        )


def _runtime_transition(
    db: WorkerDatabase,
    runtime_id: str,
    lifecycle_state: Any,
    fatal_code: Any,
) -> None:
    with db.worker_session("workers_runtime_transition", 1.0) as repos, repos.transaction():
        WorkersRuntimeRepository(repos.conn).transition(
            runtime_id=runtime_id,
            lifecycle_state=lifecycle_state,
            fatal_code=fatal_code,
            now_ms=_now_ms(),
        )


def _runtime_heartbeat(db: WorkerDatabase, runtime_id: str, heartbeat_at_ms: int) -> None:
    with db.worker_session("workers_runtime_heartbeat", 1.0) as repos, repos.transaction():
        WorkersRuntimeRepository(repos.conn).heartbeat(
            runtime_id=runtime_id,
            now_ms=heartbeat_at_ms,
        )


async def _within(awaitable: Awaitable[Any], started_at: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout=_remaining(started_at))


async def _within_graceful_deadline(awaitable: Awaitable[Any], started_at: float) -> Any:
    try:
        return await _within(awaitable, started_at)
    except TimeoutError as exc:
        raise RuntimeError("graceful_deadline_exceeded") from exc


def _remaining(started_at: float) -> float:
    return max(
        0.001,
        GRACEFUL_DRAIN_TIMEOUT_SECONDS - (asyncio.get_running_loop().time() - float(started_at)),
    )


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, float(seconds)))
    except TimeoutError:
        return


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


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["run_workers"]
