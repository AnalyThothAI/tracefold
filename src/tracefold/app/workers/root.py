"""Workers process lifecycle root."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import uvicorn
from loguru import logger
from psycopg import OperationalError
from psycopg.errors import IdleInTransactionSessionTimeout, LockNotAvailable, QueryCanceled, TransactionTimeout
from psycopg_pool import PoolTimeout

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.probe import _create_workers_probe_app
from tracefold.app.workers.runtime import WORKERS_RUNTIME_VERSION, WorkersRuntimeRepository
from tracefold.app.workers.task_contract import (
    WORKERS_CONTROL_TASK_NAME,
    WORKERS_PROBE_TASK_NAME,
    worker_business_runners,
)
from tracefold.app.workers.wiring.components import _Components, _wire_components
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.client import postgres_health_check
from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun
from tracefold.platform.runtime_identity import UNVERSIONED, runtime_identity

GRACEFUL_DRAIN_TIMEOUT_SECONDS = 30.0
FATAL_EXIT_TIMEOUT_SECONDS = 5.0
_WORKER_INTERNAL_PORT = 8766
_HEARTBEAT_SECONDS = 5.0
_CONTROL_TIMEOUT_SECONDS = 1.0
_CONTROL_RETRY_SECONDS = 0.250
_CONTROL_HEARTBEAT_STALE_SECONDS = 15.0


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
    runtime_revision: str = UNVERSIONED
    image_digest: str = UNVERSIONED
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
            "image_digest": self.image_digest,
            "process_id": os.getpid(),
            "lifecycle_state": self.lifecycle_state,
            "started_at_ms": self.started_at_ms,
            "heartbeat_at_ms": self.heartbeat_at_ms,
            "heartbeat_stale_after_ms": heartbeat_stale_after_ms,
            "unavailable_reason": None if ready else unavailable_reason,
        }


async def run_workers(settings: Settings) -> None:
    """Run the sole Workers process root until an ordered graceful stop."""

    runtime_id = str(uuid4())
    runtime_version = WORKERS_RUNTIME_VERSION
    started_at_ms = _now_ms()
    telemetry = TelemetryRegistry()
    identity = runtime_identity()
    probe_state = _ProbeState(
        runtime_id=runtime_id,
        runtime_version=runtime_version,
        started_at_ms=started_at_ms,
        clock_ms=_now_ms,
        runtime_revision=identity.runtime_revision,
        image_digest=identity.image_digest,
    )
    work_stop_event = asyncio.Event()
    control_stop_event = asyncio.Event()
    probe_stop_event = asyncio.Event()
    shutdown_requested = asyncio.Event()
    db: WorkerDatabase | None = None
    lock_conn: Any | None = None
    finite = FiniteOperations(telemetry=telemetry)
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
            await db.aclose()
            db.close_executors()
            raise _FreshRuntimeRowExists("workers_runtime_fresh_row_exists")

        components = await _wire_components(settings=settings, db=db, finite=finite)
        server = _probe_server(probe_state=probe_state, telemetry=telemetry)

        async with asyncio.TaskGroup() as group:
            business_tasks: list[asyncio.Task[Any]] = []
            control_task: asyncio.Task[Any] | None = None
            probe_task = group.create_task(
                _guard_child(
                    _run_probe(server, stop_event=probe_stop_event),
                    on_fatal=enter_fatal,
                ),
                name=WORKERS_PROBE_TASK_NAME,
            )
            for task_name, runner in worker_business_runners(
                news_pipeline=components.news_pipeline,
                trading_pipeline=components.trading_pipeline,
            ):
                business_tasks.append(
                    group.create_task(
                        _guard_child(runner(work_stop_event), on_fatal=enter_fatal),
                        name=task_name,
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
                    name=WORKERS_CONTROL_TASK_NAME,
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
    # The Workers root installs its own handlers; uvicorn must not also claim SIGINT/SIGTERM.
    server.capture_signals = nullcontext  # type: ignore[method-assign, assignment]
    return server


async def _wait_for_probe_start(server: uvicorn.Server) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    while not server.started:
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("workers_probe_start_timeout")
        await asyncio.sleep(0.010)


async def _graceful_cleanup(
    *,
    started_at: float,
    db: WorkerDatabase,
    finite: FiniteOperations,
    components: _Components,
) -> None:
    try:
        db.close_business_admission()
        finite.close_admission()
        if components.news_pipeline is not None:
            await _within(components.news_pipeline.close(), started_at)
        if components.trading_pipeline is not None:
            await _within(components.trading_pipeline.close(), started_at)
        if components.news_bus is not None:
            await _within(components.news_bus.close(), started_at)
        if not await db.drain_business(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("worker_database_business_drain_timeout")
        if not await finite.drain(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("finite_operation_drain_timeout")
        finite.close()
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
    phase: str,
) -> None:
    logger.opt(exception=exc).critical("Workers runtime fatal exit")
    finite.close_admission()
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
