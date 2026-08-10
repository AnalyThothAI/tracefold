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

from tracefold.app.database import WorkerDatabase
from tracefold.app.llm import configured_chat_model, llm_is_configured
from tracefold.app.market_providers import (
    AssetMarketProviders,
    wire_asset_market,
)
from tracefold.app.model_arbiter import run_model_arbiter
from tracefold.app.projection_edf import run_projection_edf
from tracefold.app.provider_ownership import configured_profile_provider_ids, gmgn_stream_enabled
from tracefold.app.worker_capabilities import CpuProcess, FiniteOperations, ModelAdapter
from tracefold.app.worker_cpu_prewarm import prewarm_worker_cpu_modules
from tracefold.app.worker_http import _create_workers_probe_app
from tracefold.app.workers_runtime import WORKERS_RUNTIME_VERSION, WorkersRuntimeRepository
from tracefold.integrations.deepagents.fed_document_analysis import FedDocumentAnalysisAgent
from tracefold.integrations.gmgn.providers import gmgn_upstream_client
from tracefold.integrations.macro_sources import MacroSourceClient
from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.integrations.news_feeds import RssFeedReader, parse_rss_feed_wire
from tracefold.integrations.news_push import FeishuNewsPushDelivery
from tracefold.integrations.opennews import OpenNewsRestClient, OpenNewsWebSocketClient
from tracefold.macro import (
    FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
    FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
    FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
    MacroAcquisition,
    MacroAcquisitionService,
    MacroDocumentAnalysisService,
    MacroProjectionCandidate,
    acquisition_loop_policy,
)
from tracefold.market import (
    TOKEN_RADAR_REFRESH_SECONDS,
    AssetProfileRefresh,
    CollectorService,
    EventAnchorBackfill,
    EventMarketCaptureService,
    IngestService,
    MarketTickPoll,
    ProfileProjectionCandidate,
    RadarCurrentProjectionCycle,
    ResolutionRefresh,
    StocksRadarCurrentProjection,
    TickLookup,
    TokenImageMirror,
    TokenRadarCurrentProjection,
)
from tracefold.news import (
    NewsAcquisition,
    NewsBriefCandidate,
    NewsStoryProjection,
)
from tracefold.news.push import NewsStoryPush
from tracefold.news.sources import opennews_source, public_rss_sources
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import postgres_health_check
from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

GRACEFUL_DRAIN_TIMEOUT_SECONDS = 30.0
FATAL_EXIT_TIMEOUT_SECONDS = 5.0
_WORKER_INTERNAL_PORT = 8766
_RUNTIME_REVISION_ENV = "TRACEFOLD_RUNTIME_REVISION"
_HEARTBEAT_SECONDS = 5.0
_CONTROL_TIMEOUT_SECONDS = 1.0
_EVENT_ANCHOR_ACTIVE_WINDOW_MS = 300_000
_DOCUMENT_MODEL_TIMEOUT_SECONDS = 180.0
_MODEL_MAX_TOKENS = 6_000
_NEWS_BRIEF_TOTAL_TIMEOUT_SECONDS = 60.0
_NEWS_OLLAMA_BASE_URL = "http://host.docker.internal:11434/v1"
_PRODUCTIVE_REPOLL_SECONDS = 0.250
_MARKET_TICK_POLL_SECONDS = 35.0
_NEWS_PUSH_IDLE_SECONDS = 5.0
_NEWS_RSS_IDLE_SECONDS = 30.0
_NEWS_PUSH_RECONCILE_SECONDS = 10.0
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
        heartbeat_current = (
            self.heartbeat_at_ms is not None and max(0, int(self.clock_ms()) - int(self.heartbeat_at_ms)) <= 15_000
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
            "heartbeat_stale_after_ms": 15_000,
            "unavailable_reason": None if ready else unavailable_reason,
        }


@dataclass(slots=True)
class _Components:
    providers: AssetMarketProviders
    asset_profile_refresh: AssetProfileRefresh
    collector: CollectorService | None
    news: NewsAcquisition | None
    news_story: NewsStoryProjection | None
    news_brief: NewsBriefCandidate | None
    news_push: NewsStoryPush | None
    macro_source: MacroSourceClient | None
    macro_turns: tuple[MacroAcquisition, ...]
    due_turns: tuple[tuple[Callable[[], Awaitable[bool | str | None]], float], ...]
    market_poll: MarketTickPoll | None
    radar_current: RadarCurrentProjectionCycle
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
    cpu = CpuProcess(telemetry=telemetry)
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
        cpu.close_admission()
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
        await cpu.prewarm()
        await cpu.run(
            "workers_cpu_modules_prewarm",
            prewarm_worker_cpu_modules,
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
            cpu.close()
            await db.aclose()
            db.close_executors()
            raise _FreshRuntimeRowExists("workers_runtime_fresh_row_exists")

        components = await _wire_components(
            settings=settings,
            db=db,
            telemetry=telemetry,
            finite=finite,
            model_adapter=model_adapter,
            cpu=cpu,
            runtime_id=runtime_id,
        )
        await _reconcile_once(components)
        server = _probe_server(probe_state=probe_state, telemetry=telemetry)

        async with asyncio.TaskGroup() as group:
            business_tasks: list[asyncio.Task[Any]] = []
            probe_task = group.create_task(
                _guard_child(
                    _run_probe(server, stop_event=probe_stop_event),
                    on_fatal=enter_fatal,
                ),
                name="workers-probe",
            )
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
            control_watchdog_task = group.create_task(
                _guard_child(
                    _run_control_watchdog(
                        probe_state=probe_state,
                        stop_event=control_stop_event,
                    ),
                    on_fatal=enter_fatal,
                ),
                name="workers-control-watchdog",
            )
            if components.collector is not None:
                business_tasks.append(
                    group.create_task(
                        _guard_child(
                            components.collector.run(stop_event=work_stop_event),
                            on_fatal=enter_fatal,
                        ),
                        name="gmgn-stream",
                    )
                )
            if components.news is not None and components.news.opennews_enabled:
                business_tasks.append(
                    group.create_task(
                        _guard_child(
                            components.news.run_opennews(stop_event=work_stop_event),
                            on_fatal=enter_fatal,
                        ),
                        name="opennews-stream",
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
            if components.market_poll is not None:
                business_tasks.append(
                    group.create_task(
                        _guard_child(
                            _run_periodic(
                                components.market_poll.sample,
                                period_seconds=_MARKET_TICK_POLL_SECONDS,
                                stop_event=work_stop_event,
                            ),
                            on_fatal=enter_fatal,
                        ),
                        name="market-tick-poll",
                    )
                )
            business_tasks.append(
                group.create_task(
                    _guard_child(
                        _run_periodic(
                            components.radar_current.sample,
                            period_seconds=TOKEN_RADAR_REFRESH_SECONDS,
                            initial_delay_seconds=TOKEN_RADAR_REFRESH_SECONDS,
                            stop_event=work_stop_event,
                        ),
                        on_fatal=enter_fatal,
                    ),
                    name="radar-current-cycle",
                )
            )
            if components.news_story is not None:
                business_tasks.append(
                    group.create_task(
                        _guard_child(
                            _run_periodic(
                                lambda: _sample_news_story(news_story=components.news_story),
                                period_seconds=60.0,
                                stop_event=work_stop_event,
                            ),
                            on_fatal=enter_fatal,
                        ),
                        name="news-story-projection",
                    )
                )
            if components.news_push is not None:
                business_tasks.append(
                    group.create_task(
                        _guard_child(
                            _run_periodic(
                                lambda: _sample_news_push(news_push=components.news_push),
                                period_seconds=_NEWS_PUSH_RECONCILE_SECONDS,
                                stop_event=work_stop_event,
                            ),
                            on_fatal=enter_fatal,
                        ),
                        name="news-push-reconcile",
                    )
                )
            business_tasks.append(
                group.create_task(
                    _guard_child(
                        run_projection_edf(
                            components.projections,
                            stop_event=work_stop_event,
                            telemetry=telemetry,
                        ),
                        on_fatal=enter_fatal,
                    ),
                    name="projection-edf",
                )
            )
            business_tasks.append(
                group.create_task(
                    _guard_child(
                        run_model_arbiter(components.models, stop_event=work_stop_event),
                        on_fatal=enter_fatal,
                    ),
                    name="model-arbiter",
                )
            )

            await _guard_child(
                _wait_for_probe_start(server),
                on_fatal=enter_fatal,
            )
            probe_state.heartbeat_at_ms = await _guard_child(
                _control_liveness_and_heartbeat(
                    db=db,
                    lock_conn=lock_conn,
                    runtime_id=runtime_id,
                ),
                on_fatal=enter_fatal,
            )
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
            phase = "runtime"

            await shutdown_requested.wait()
            shutdown_started = signal_loop.time()
            probe_state.ready = False
            probe_state.lifecycle_state = "stopping"
            probe_state.unavailable_reason = "runtime_stopping"
            await _guard_child(
                db.run_control(
                    "workers_runtime_stopping",
                    _runtime_transition,
                    db,
                    runtime_id,
                    "stopping",
                    None,
                    operation_timeout_seconds=_remaining(shutdown_started),
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
                    cpu=cpu,
                    components=components,
                ),
                on_fatal=enter_fatal,
            )
            control_stop_event.set()
            await _within(
                asyncio.gather(control_task, control_watchdog_task),
                shutdown_started,
            )
            await db.run_control(
                "workers_runtime_stopped",
                _runtime_transition,
                db,
                runtime_id,
                "stopped",
                None,
                operation_timeout_seconds=_remaining(shutdown_started),
                allow_shutdown=True,
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
            cpu=cpu,
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
        value = await turn()
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


async def _run_periodic(
    sample: Callable[[], Awaitable[None]],
    *,
    period_seconds: float,
    initial_delay_seconds: float = 0.0,
    stop_event: asyncio.Event,
) -> None:
    if initial_delay_seconds > 0.0:
        await _wait_or_stop(stop_event, initial_delay_seconds)
        if stop_event.is_set():
            return
    loop = asyncio.get_running_loop()
    deadline = loop.time()
    while not stop_event.is_set():
        await sample()
        deadline += float(period_seconds)
        if deadline <= loop.time():
            deadline = loop.time() + float(period_seconds)
        await _wait_or_stop(stop_event, deadline - loop.time())


async def _sample_news_story(
    *,
    news_story: NewsStoryProjection,
) -> None:
    """Project the complete Story closure on its fixed 60-second cadence."""

    await news_story.sample()


async def _sample_news_push(*, news_push: NewsStoryPush) -> None:
    """Discover score-qualified Stories without rebuilding the Story closure."""

    try:
        await news_push.reconcile(now_ms=_now_ms())
    except ResourceAdmissionTimeout:
        return


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
            probe_state.heartbeat_at_ms = await _control_liveness_and_heartbeat(
                db=db,
                lock_conn=lock_conn,
                runtime_id=runtime_id,
            )
            await _wait_or_stop(stop_event, _HEARTBEAT_SECONDS)
    except asyncio.CancelledError:
        raise
    except RuntimeError as exc:
        if "singleton_lost" in str(exc):
            raise
        raise _ControlFailure("workers_control_failed") from exc
    except Exception as exc:
        raise _ControlFailure("workers_control_failed") from exc


async def _run_control_watchdog(
    *,
    probe_state: _ProbeState,
    stop_event: asyncio.Event,
) -> None:
    loop = asyncio.get_running_loop()
    last_heartbeat_at_ms = probe_state.heartbeat_at_ms
    last_success_at = loop.time()
    while not stop_event.is_set():
        if probe_state.heartbeat_at_ms != last_heartbeat_at_ms:
            last_heartbeat_at_ms = probe_state.heartbeat_at_ms
            last_success_at = loop.time()
        if loop.time() - last_success_at > 15.0:
            raise _ControlFailure("workers_control_heartbeat_stale")
        await _wait_or_stop(stop_event, 0.250)


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
    cpu: CpuProcess,
    runtime_id: str,
) -> _Components:
    providers = wire_asset_market(settings)
    ingest = _PooledIngestStore(
        db,
        providers=providers,
        event_anchor_active_window_ms=_EVENT_ANCHOR_ACTIVE_WINDOW_MS,
    )
    collector: CollectorService | None = None
    if gmgn_stream_enabled(settings):
        collector = CollectorService(store=ingest, upstream_client=None, db=db)
        collector.upstream_client = gmgn_upstream_client(
            settings,
            on_frame=collector.handle_frame,
        )

    due_turns: list[tuple[Callable[[], Awaitable[bool | str | None]], float]] = []
    news: NewsAcquisition | None = None
    news_story: NewsStoryProjection | None = None
    news_brief: NewsBriefCandidate | None = None
    news_push: NewsStoryPush | None = None
    model_candidates: list[Any] = []
    if settings.news.enabled:
        source = opennews_source()
        rss_sources = public_rss_sources() if settings.news.rss_enabled else ()
        opennews_rest = OpenNewsRestClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
        opennews_ws = (
            OpenNewsWebSocketClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
        )
        news = NewsAcquisition(
            db=db,
            finite_operations=finite,
            rss_sources=rss_sources,
            rss_feed_reader=RssFeedReader(),
            rss_feed_parser=parse_rss_feed_wire,
            opennews_source=source,
            opennews_rest_client=opennews_rest,
            opennews_ws_client=opennews_ws,
        )
        news_story = NewsStoryProjection(db=db, cpu=cpu)
        news_brief = NewsBriefCandidate(
            db=db,
            model_adapter=model_adapter,
            publisher=ProviderChainNewsBriefPublisher(
                ollama_base_url=_NEWS_OLLAMA_BASE_URL,
                configured_base_url=settings.llm.base_url,
                configured_api_key=settings.llm.api_key,
                configured_model=settings.llm.news_brief_model,
                groq_api_key=settings.llm.groq_api_key,
                total_timeout_seconds=_NEWS_BRIEF_TOTAL_TIMEOUT_SECONDS,
            ),
            runtime_id=runtime_id,
            stable_order=20,
        )
        # The acquisition turn always advances bounded Item expiry. With RSS
        # disabled it has no claimable source and performs no network request.
        due_turns.append((news.turn, _NEWS_RSS_IDLE_SECONDS))
        model_candidates.append(news_brief)
        if settings.news.push.enabled:
            webhook_url = settings.news.push.feishu_webhook_url
            signing_secret = settings.news.push.feishu_signing_secret
            translation_api_key = settings.llm.api_key
            translation_enabled = bool(translation_api_key)
            if not webhook_url:
                raise RuntimeError("news_push_webhook_missing_after_validation")
            news_push = NewsStoryPush(
                db=db,
                finite_operations=finite,
                delivery=FeishuNewsPushDelivery(
                    webhook_url=webhook_url,
                    signing_secret=signing_secret,
                    finite_operations=finite,
                    translation_enabled=translation_enabled,
                    translation_base_url=(settings.llm.base_url if translation_enabled else None),
                    translation_api_key=translation_api_key,
                    translation_engine=(settings.llm.news_brief_model if translation_enabled else None),
                ),
                runtime_id=runtime_id,
            )
            due_turns.append((news_push.turn, _NEWS_PUSH_IDLE_SECONDS))

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
            timeout_seconds=min(30.0, float(source_config.request_timeout_seconds)),
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
            idle_seconds, _old_batch = acquisition_loop_policy(clock_kind)
            due_turns.append((turn.turn, idle_seconds))

    if providers.dex_discovery_market is not None:
        resolution = ResolutionRefresh(
            db=db,
            dex_discovery_market=providers.dex_discovery_market,
            finite_operations=finite,
            runtime_id=runtime_id,
            claim_limit=1,
        )
        due_turns.append((resolution.turn, 30.0))
    asset_profile_refresh = AssetProfileRefresh(
        db=db,
        finite_operations=finite,
        runtime_id=runtime_id,
        dex_profile_sources=providers.dex_profile_sources,
    )
    due_turns.append((asset_profile_refresh.turn, 60.0))
    image = TokenImageMirror(
        db=db,
        app_home=settings.app_home,
        finite_operations=finite,
        runtime_id=runtime_id,
    )
    due_turns.append((image.turn, 60.0))
    event_anchor = EventAnchorBackfill(
        db=db,
        providers=providers,
        finite_operations=finite,
        runtime_id=runtime_id,
    )
    due_turns.append((event_anchor.turn, 1.0))

    market_poll = (
        MarketTickPoll(db=db, providers=providers, finite_operations=finite)
        if providers.cex_market is not None or providers.dex_quote_market is not None
        else None
    )
    active_profile_provider_ids = configured_profile_provider_ids(settings)
    wired_profile_provider_ids = tuple(source.provider for source in providers.dex_profile_sources)
    if wired_profile_provider_ids != active_profile_provider_ids:
        raise RuntimeError("profile_provider_wiring_mismatch")
    token_radar_current = TokenRadarCurrentProjection(db=db, cpu=cpu, telemetry=telemetry)
    stocks_radar_current = StocksRadarCurrentProjection(db=db, cpu=cpu)
    radar_current = RadarCurrentProjectionCycle(
        token=token_radar_current,
        stocks=stocks_radar_current,
    )
    projections = (
        ProfileProjectionCandidate(
            db=db,
            cpu=cpu,
            runtime_id=runtime_id,
            active_profile_provider_ids=active_profile_provider_ids,
            stable_order=10,
        ),
        MacroProjectionCandidate(db=db, cpu=cpu, runtime_id=runtime_id, stable_order=20),
    )
    return _Components(
        providers=providers,
        asset_profile_refresh=asset_profile_refresh,
        collector=collector,
        news=news,
        news_story=news_story,
        news_brief=news_brief,
        news_push=news_push,
        macro_source=macro_source,
        macro_turns=tuple(macro_turns),
        due_turns=tuple(due_turns),
        market_poll=market_poll,
        radar_current=radar_current,
        projections=projections,
        models=tuple(model_candidates),
        document_model=document_model,
    )


async def _reconcile_once(components: _Components) -> None:
    await components.radar_current.initialize()
    await components.asset_profile_refresh.reconcile()
    if components.news is not None:
        await components.news.reconcile()
    if components.news_push is not None:
        await components.news_push.reconcile(now_ms=_now_ms())
    for turn in components.macro_turns:
        await turn.reconcile()
    if components.document_model is not None:
        await components.document_model.reconcile()


async def _graceful_cleanup(
    *,
    started_at: float,
    db: WorkerDatabase,
    finite: FiniteOperations,
    model_adapter: ModelAdapter,
    cpu: CpuProcess,
    components: _Components,
) -> None:
    try:
        db.close_business_admission()
        finite.close_admission()
        model_adapter.close_admission()
        cpu.close_admission()
        if components.collector is not None:
            await _within(components.collector.close(), started_at)
        if components.news is not None:
            await _within(components.news.close(), started_at)
        if components.news_brief is not None:
            await _within(components.news_brief.close(), started_at)
        if components.news_push is not None:
            await _within(components.news_push.close(), started_at)
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
        await _within(_close_market_providers(components.providers, finite), started_at)
        if not await db.drain_business(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("worker_database_business_drain_timeout")
        if not await finite.drain(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("finite_operation_drain_timeout")
        if not await model_adapter.drain(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("model_adapter_drain_timeout")
        if not await cpu.drain(timeout_seconds=_remaining(started_at)):
            raise RuntimeError("cpu_process_drain_timeout")
        finite.close()
        model_adapter.close()
        cpu.close()
    except TimeoutError as exc:
        raise RuntimeError("graceful_deadline_exceeded") from exc
    except Exception as exc:
        if _remaining(started_at) <= 0.001:
            raise RuntimeError("graceful_deadline_exceeded") from exc
        raise


async def _close_market_providers(
    providers: AssetMarketProviders,
    finite: FiniteOperations,
) -> None:
    seen: set[int] = set()
    synchronous = [
        providers.cex_market,
        providers.dex_discovery_market,
        providers.dex_quote_market,
        *(source.market for source in providers.dex_profile_sources),
    ]
    for provider in synchronous:
        if provider is None or id(provider) in seen:
            continue
        seen.add(id(provider))
        await finite.run(
            "market_provider_close",
            provider.close,
            timeout_seconds=5.0,
            allow_shutdown=True,
        )


async def _fatal_exit(
    *,
    exc: BaseException,
    db: WorkerDatabase | None,
    runtime_id: str,
    finite: FiniteOperations,
    model_adapter: ModelAdapter,
    cpu: CpuProcess,
    phase: str,
) -> None:
    logger.opt(exception=exc).critical("Workers runtime fatal exit")
    finite.close_admission()
    model_adapter.close_admission()
    cpu.close_admission()
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
    if isinstance(exc, ResourceOperationOverrun) or "resource_operation_overrun" in messages:
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


class _PooledIngestStore:
    def __init__(
        self,
        db: WorkerDatabase,
        *,
        providers: AssetMarketProviders,
        event_anchor_active_window_ms: int,
    ) -> None:
        self.db = db
        self.event_anchor_active_window_ms = int(event_anchor_active_window_ms)
        self._capture_service = EventMarketCaptureService(
            providers=providers,
            now_ms=_now_ms,
        )

    def insert_raw_frame(self, **kwargs: Any) -> bool:
        with self.db.worker_session("gmgn_capture", 3.0) as repos, repos.transaction():
            return repos.evidence.insert_raw_frame(**kwargs)

    def ingest_event(self, event: Any) -> Any:
        prepared = IngestService.prepare_event(event)
        market_resolutions: list[dict[str, Any]] = []
        prefetched_ticks: dict[tuple[str, str], Any] = {}
        with self.db.worker_session("gmgn_capture", 3.0) as repos, repos.transaction():
            ingest = _ingest_service_for_repos(
                repos,
                event_anchor_active_window_ms=self.event_anchor_active_window_ms,
            )
            if ingest.event_already_exists(prepared):
                return ingest.duplicate_result(prepared)
            ingest.prepare_registry_for_resolution(prepared)
            resolutions = ingest.resolve_prepared(prepared, persist=False)
            for decision in resolutions:
                resolution = ingest.market_resolution_for_decision(decision)
                if resolution is None:
                    continue
                market_resolutions.append(resolution)
                prefetched_ticks[(resolution["target_type"], resolution["target_id"])] = (
                    repos.market_ticks.latest_at_or_before(
                        target_type=resolution["target_type"],
                        target_id=resolution["target_id"],
                        at_ms=prepared.event_ms,
                        max_lag_ms=60_000,
                    )
                )
            lookup = TickLookup(
                latest_at_or_before=lambda target_type, target_id, _at_ms, _max_lag_ms: prefetched_ticks.get(
                    (target_type, target_id)
                )
            )
            captures = [
                self._capture_service.capture_for_event(
                    event_id=resolution["event_id"],
                    intent_id=resolution["intent_id"],
                    resolution_id=resolution["resolution_id"],
                    resolution=resolution,
                    event_ms=prepared.event_ms,
                    tick_lookup=lookup,
                )
                for resolution in market_resolutions
            ]
            return ingest.commit_prepared_event(
                prepared,
                resolutions=resolutions,
                captures=captures,
            )


def _ingest_service_for_repos(repos: Any, *, event_anchor_active_window_ms: int) -> IngestService:
    return IngestService(
        evidence=repos.evidence,
        entities=repos.entities,
        registry=repos.registry,
        identity_evidence=repos.identity_evidence,
        token_intent_lookup=repos.token_intent_lookup,
        token_evidence=repos.token_evidence,
        token_intents=repos.token_intents,
        intent_resolutions=repos.intent_resolutions,
        discovery=repos.discovery,
        market_ticks=repos.market_ticks,
        market_tick_current=repos.market_tick_current,
        enriched_events=repos.enriched_events,
        event_anchor_jobs=repos.event_anchor_jobs,
        persisted_live=repos.persisted_live,
        transaction=repos.transaction,
        event_anchor_active_window_ms=event_anchor_active_window_ms,
    )


async def _within(awaitable: Awaitable[Any], started_at: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout=_remaining(started_at))


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
