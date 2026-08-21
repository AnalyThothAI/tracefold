from __future__ import annotations

import asyncio
import functools
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

import tracefold.news.agents.programs.candidates as candidate_programs
from tracefold.app.database import WorkerDatabase
from tracefold.app.learning_runtime import (
    UNVERSIONED,
    active_arm_manifest,
    candidate_program_artifact,
    runtime_identity,
    runtime_manifest_sha,
)
from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.probe import _create_workers_probe_app
from tracefold.app.workers.runtime import WORKERS_RUNTIME_VERSION, WorkersRuntimeRepository
from tracefold.integrations.feishu import FeishuNewsPushSender
from tracefold.integrations.opennews import OpenNewsStrategyHistoryClient, OpenNewsWebSocketClient
from tracefold.integrations.venues import (
    fetch_binance_candles,
    fetch_binance_futures_day_quotes,
    fetch_binance_futures_quotes,
    fetch_binance_instruments,
    fetch_binance_spot_day_quotes,
    fetch_binance_spot_quotes,
    fetch_hyperliquid_candles,
    fetch_hyperliquid_instruments,
    fetch_hyperliquid_quotes,
    fetch_us_reference_instruments,
)
from tracefold.news import CandidateManifest, DecidePolicy
from tracefold.news.agents.semantic_program import (
    DspyNewsSemanticProgram,
    DspyPredictorAdapter,
    ProgramArtifact,
    load_stable_program_artifact,
)
from tracefold.news.canary import CanaryRuntimeArm
from tracefold.news.consumers import (
    DeduperConsumer,
    DelivererConsumer,
    EventReactionLoop,
    InstrumentSnapshotLoop,
    JanitorLoop,
    NewsPipeline,
    OpenNewsReceiver,
    QuoteSnapshotLoop,
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
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

GRACEFUL_DRAIN_TIMEOUT_SECONDS = 30.0
FATAL_EXIT_TIMEOUT_SECONDS = 5.0
_WORKER_INTERNAL_PORT = 8766
_HEARTBEAT_SECONDS = 5.0
_CONTROL_TIMEOUT_SECONDS = 1.0
_CONTROL_RETRY_SECONDS = 0.250
_CONTROL_HEARTBEAT_STALE_SECONDS = 15.0
_NEWS_OLLAMA_BASE_URL = "http://host.docker.internal:11434/v1"


def _configured_semantic_program(
    settings: Settings, artifact: ProgramArtifact, models: Any
) -> DspyNewsSemanticProgram | None:
    """Compose one arm-local Program from an image-carried artifact and operator-owned endpoints."""

    if not models.triage_configured or not models.triage_model:
        return None
    route_timeout = float(artifact.execution.route_deadline_seconds)
    max_tokens = max(artifact.event_semantics.max_tokens, artifact.reader_card.max_tokens)
    primary = configured_lm_endpoint(
        settings,
        model_name=models.triage_model,
    )
    fallback_adapter: DspyPredictorAdapter | None = None
    if models.triage_fallback_model:
        endpoint = settings.llm.news_triage_fallback
        fallback = configured_lm_endpoint(
            settings,
            model_name=models.triage_fallback_model,
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
        )
        fallback_adapter = DspyPredictorAdapter.from_runtime(
            model_name=fallback.model_name,
            api_key=fallback.api_key,
            api_base=fallback.api_base,
            timeout=route_timeout,
            max_tokens=max_tokens,
            model_kwargs=fallback.model_kwargs,
        )
    return DspyNewsSemanticProgram(
        artifact,
        primary_adapter=DspyPredictorAdapter.from_runtime(
            model_name=primary.model_name,
            api_key=primary.api_key,
            api_base=primary.api_base,
            timeout=route_timeout,
            max_tokens=max_tokens,
            model_kwargs=primary.model_kwargs,
        ),
        fallback_adapter=fallback_adapter,
    )


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


@dataclass(slots=True)
class _Components:
    news_pipeline: NewsPipeline | None
    news_bus: Any | None


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
    finite: FiniteOperations,
) -> _Components:
    news_pipeline: NewsPipeline | None = None
    news_bus: Any | None = None
    if settings.news.enabled:
        news_bus, news_pipeline = await _wire_news_pipeline(settings=settings, db=db, finite=finite)
    return _Components(news_pipeline=news_pipeline, news_bus=news_bus)


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

    watchlist_symbols = settings.news.watchlist_symbols
    ws_client = OpenNewsWebSocketClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
    history_client = (
        OpenNewsStrategyHistoryClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
    )

    recovery = RecoveryRunner(bus=bus, db=db, history_client=history_client) if ws_client else None
    receiver = (
        OpenNewsReceiver(
            bus=bus,
            db=db,
            ws_client=ws_client,
            history_client=history_client,
            recovery=recovery,
        )
        if ws_client
        else None
    )

    models = news_model_availability(settings)
    identity = runtime_identity()
    stable_arm = active_arm_manifest(settings)
    compiled_candidates: dict[str, CandidateManifest] = {}
    candidate_failures: dict[str, str] = {}
    for index, document in enumerate(candidate_programs.COMPILED_CANDIDATE_DOCUMENTS):
        try:
            candidate = CandidateManifest.model_validate(document)
        except (TypeError, ValueError) as exc:
            logger.error(
                "candidate manifest rejected index={} error={}",
                index,
                type(exc).__name__,
            )
            continue
        compiled_candidates[candidate.candidate_sha] = candidate
    canary_arms: dict[str, CanaryRuntimeArm] = {}
    stable_artifact = load_stable_program_artifact()
    if (
        stable_artifact.program_version != stable_arm.program_version
        or stable_artifact.program_sha256 != stable_arm.program_sha256
    ):
        raise RuntimeError("news_stable_program_manifest_mismatch")
    semantic_judge = _configured_semantic_program(settings, stable_artifact, models)
    if semantic_judge is not None:
        for candidate in compiled_candidates.values():
            if candidate.parent_stable_sha != stable_arm.bundle_sha:
                candidate_failures[candidate.candidate_sha] = "candidate_parent_stale"
                logger.warning(
                    "ignoring canary candidate with stale parent candidate={} parent={} active={}",
                    candidate.candidate_sha,
                    candidate.parent_stable_sha,
                    stable_arm.bundle_sha,
                )
                continue
            arm = candidate.candidate_arm
            try:
                candidate_artifact = candidate_program_artifact(candidate, stable_artifact)
            except (OSError, ValueError) as exc:
                candidate_failures[candidate.candidate_sha] = "candidate_artifact_invalid"
                logger.error("candidate Program artifact rejected program={} error={}", arm.program_sha256, exc)
                continue
            try:
                candidate_program = _configured_semantic_program(settings, candidate_artifact, models)
            except (TypeError, ValueError) as exc:
                candidate_failures[candidate.candidate_sha] = "candidate_runtime_invalid"
                logger.error("candidate Program composition rejected program={} error={}", arm.program_sha256, exc)
                continue
            if candidate_program is None:  # guarded by semantic_judge, kept explicit for type narrowing
                candidate_failures[candidate.candidate_sha] = "candidate_runtime_unavailable"
                continue
            canary_arms[arm.bundle_sha] = CanaryRuntimeArm(
                bundle_sha=arm.bundle_sha,
                program=candidate_program,
                policy=DecidePolicy(**arm.policy),
                program_version=arm.program_version,
                program_sha256=arm.program_sha256,
            )

    await db.run_news(
        "news_canary_startup_validation",
        _trip_unavailable_active_canary,
        db,
        {candidate_sha: candidate.candidate_arm.bundle_sha for candidate_sha, candidate in compiled_candidates.items()},
        frozenset(canary_arms),
        dict(candidate_failures),
        operation_timeout_seconds=3.0,
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
            watchlist_symbols=watchlist_symbols,
            suppress_low_signal=settings.news.gate.suppress_low_signal,
        ),
        triage=TriageConsumer(
            bus=bus,
            db=db,
            judge=semantic_judge,
            program_version=stable_artifact.program_version,
            program_sha256=stable_artifact.program_sha256,
            watchlist_symbols=watchlist_symbols,
            watchlist=sorted(watchlist_symbols),
            concurrency=settings.news.triage.concurrency,
            circuit_failures=settings.news.triage.circuit_failures,
            circuit_open_seconds=settings.news.triage.circuit_open_seconds,
            policy=DecidePolicy(**settings.news.policy.model_dump()),
            stable_bundle_sha=stable_arm.bundle_sha,
            canary_arms=canary_arms,
            runtime_manifest={
                "manifest_sha": runtime_manifest_sha(
                    stable_bundle_sha=stable_arm.bundle_sha,
                    candidate_shas=sorted(compiled_candidates),
                    image_digest=identity.image_digest,
                    runtime_revision=identity.runtime_revision,
                ),
                "stable_bundle_sha": stable_arm.bundle_sha,
                "candidate_shas": sorted(compiled_candidates),
                "image_digest": identity.image_digest,
                "runtime_revision": identity.runtime_revision,
                "now_ms": int(time.time() * 1000),
            },
        ),
        deliverer=DelivererConsumer(
            bus=bus,
            db=db,
            sender=sender,
            finite_operations=finite,
            min_interval_seconds=settings.news.push.min_interval_seconds,
        ),
        janitor=JanitorLoop(
            db=db,
            bus=bus,
            retention_raw_days=settings.news.retention.raw_days,
            retention_judged_days=settings.news.retention.judged_days,
        ),
        instruments=_instrument_snapshot_loop(settings, db=db),
        quotes=_quote_snapshot_loop(settings, db=db, watchlist=sorted(watchlist_symbols)),
        reactions=_event_reaction_loop(settings, db=db),
    )
    return bus, pipeline


def _trip_unavailable_active_canary(
    db: WorkerDatabase,
    compiled_candidate_bundles: dict[str, str],
    runnable_candidate_bundles: frozenset[str],
    candidate_failures: dict[str, str],
) -> bool:
    """Fail closed a nonterminal candidate that this image cannot execute."""

    with db.worker_session("news_canary_startup_validation", 3.0) as repos, repos.transaction():
        status = repos.news.canary_status()
        activation = status.get("activation")
        if activation is None or str(activation["state"]) not in {"armed", "active"}:
            return False
        candidate_manifest_sha = str(activation["candidate_manifest_sha"])
        candidate_bundle_sha = str(activation["candidate_bundle_sha"])
        expected_bundle_sha = compiled_candidate_bundles.get(candidate_manifest_sha)
        if candidate_bundle_sha == expected_bundle_sha and candidate_bundle_sha in runnable_candidate_bundles:
            return False
        if expected_bundle_sha is None:
            reason = "candidate_manifest_missing_or_invalid"
        elif candidate_bundle_sha != expected_bundle_sha:
            reason = "candidate_bundle_mismatch"
        else:
            reason = candidate_failures.get(candidate_manifest_sha, "candidate_runtime_unavailable")
        return bool(
            repos.news.transition_canary(
                activation_id=str(activation["activation_id"]),
                target_state="tripped",
                reason=reason,
                now_ms=_now_ms(),
            )
        )


def _price_venue_enabled(settings: Any, source_key: str) -> bool:
    """#88 reuses the existing venue switches; the price plane never gets an operator knob of its own."""

    venues = settings.news.venues
    if not venues.enabled:
        return False
    if source_key.startswith("binance."):
        return bool(venues.binance)
    if source_key.startswith("hl."):
        return bool(venues.hyperliquid)
    return False


def _quote_snapshot_loop(settings: Any, *, db: Any, watchlist: Sequence[str]) -> QuoteSnapshotLoop | None:
    """One batch quote adapter per source group, resolved by source key so a new HIP-3 dex needs no wiring."""

    def fetcher_for(source_key: str) -> Any | None:
        if not _price_venue_enabled(settings, source_key):
            return None
        if source_key == "binance.spot":
            return fetch_binance_spot_quotes
        if source_key == "binance.perp":
            return fetch_binance_futures_quotes
        if source_key.startswith("hl."):
            return functools.partial(fetch_hyperliquid_quotes, venue=source_key)
        return None

    def day_fetcher_for(source_key: str) -> Any | None:
        """The wider endpoint one turn in fifteen, for the day-change reference (#109).

        Only Binance has one: a Hyperliquid request already carries `prevDayPx` beside the mid, so its
        ordinary fetcher is its day fetcher and there is nothing to alternate.
        """

        if not _price_venue_enabled(settings, source_key):
            return None
        if source_key == "binance.spot":
            return fetch_binance_spot_day_quotes
        if source_key == "binance.perp":
            return fetch_binance_futures_day_quotes
        return None

    venues = settings.news.venues
    if not venues.enabled or not (venues.binance or venues.hyperliquid):
        return None
    return QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for, day_fetcher_for=day_fetcher_for, watchlist=watchlist)


def _event_reaction_loop(settings: Any, *, db: Any) -> EventReactionLoop | None:
    def fetcher_for(venue: str) -> Any | None:
        if not _price_venue_enabled(settings, venue):
            return None
        if venue.startswith("binance."):

            async def binance(venue_symbol: str, start_ms: int, end_ms: int) -> Any:
                return await fetch_binance_candles(venue_symbol, venue=venue, start_ms=start_ms, end_ms=end_ms)

            return binance
        if venue.startswith("hl."):

            async def hyperliquid(venue_symbol: str, start_ms: int, end_ms: int) -> Any:
                return await fetch_hyperliquid_candles(venue_symbol, venue=venue, start_ms=start_ms, end_ms=end_ms)

            return hyperliquid
        return None

    venues = settings.news.venues
    if not venues.enabled or not (venues.binance or venues.hyperliquid):
        return None
    return EventReactionLoop(db=db, fetcher_for=fetcher_for)


def _instrument_snapshot_loop(settings: Any, *, db: Any) -> InstrumentSnapshotLoop | None:
    """#75: one fetcher per venue family, each independently skippable. No credentials are involved."""

    venues = settings.news.venues
    if not venues.enabled:
        return None
    fetchers: list[tuple[str, Callable[[], Any]]] = []
    if venues.binance:
        fetchers.append(("binance", fetch_binance_instruments))
    if venues.hyperliquid:
        fetchers.append(("hyperliquid", fetch_hyperliquid_instruments))
    if venues.us_reference:
        fetchers.append(("us_reference", fetch_us_reference_instruments))
    if not fetchers:
        return None
    return InstrumentSnapshotLoop(
        db=db,
        fetchers=fetchers,
        period_seconds=float(venues.snapshot_period_hours) * 3600.0,
    )


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
