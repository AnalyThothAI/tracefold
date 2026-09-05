from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.runtime import (
    NEWS_DELIVERY,
    NEWS_EDITORIAL,
    NEWS_INGESTION,
    NEWS_MARKET_REVIEW,
    TRADING_SIGNAL_LANE,
    CapabilityStates,
)
from tracefold.app.workers.wiring.news import _wire_news_pipeline
from tracefold.app.workers.wiring.trading import _wire_signal_lane
from tracefold.news.bus import BrokerBackpressure, BrokerUnavailable
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.platform.config.models import Settings, news_push_availability
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading.signal_lane import SignalLane

if TYPE_CHECKING:
    from tracefold.integrations.rabbitmq import RabbitMQBus


@dataclass(slots=True)
class _Components:
    news_pipeline: NewsPipeline | None
    news_bus: RabbitMQBus | None
    runtime_manifest_sha: str | None = None
    signal_lane: SignalLane | None = None
    telemetry: TelemetryRegistry | None = None
    capabilities: CapabilityStates = field(default_factory=CapabilityStates)


def _capability_fault_reason(task_name: str, exc: BaseException) -> str:
    """Name what stopped one capability task, in the vocabulary a status reader publishes."""

    if any(isinstance(item, (BrokerBackpressure, BrokerUnavailable)) for item in _leaf_exceptions(exc)):
        return f"{task_name}:news_broker_unavailable"
    return f"{task_name}:{type(exc).__name__}"


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for item in exc.exceptions:
            leaves.extend(_leaf_exceptions(item))
        return leaves
    return [exc]


async def _wire_components(
    *,
    settings: Settings,
    db: WorkerDatabase,
    finite: FiniteOperations,
    telemetry: TelemetryRegistry,
) -> _Components:
    capabilities = CapabilityStates()
    news_pipeline: NewsPipeline | None = None
    news_bus: RabbitMQBus | None = None
    runtime_manifest_sha: str | None = None
    if settings.news.enabled:
        news_bus, news_pipeline = await _wire_news_pipeline(
            settings=settings,
            db=db,
            finite=finite,
            telemetry=telemetry,
            capabilities=capabilities,
        )
        runtime_manifest_sha = await _register_runtime_manifest(news_pipeline, capabilities=capabilities)
    else:
        capabilities.disabled(NEWS_INGESTION, "news_disabled")
        capabilities.disabled(NEWS_EDITORIAL, "news_disabled")
        capabilities.disabled(NEWS_MARKET_REVIEW, "news_disabled")
        # A push target declared against a disabled News is a configuration error, not a delivery.
        # It used to refuse the whole process; now it refuses only the capability it describes.
        push = news_push_availability(settings, inspect_secret_file=False)
        capabilities.declare(
            NEWS_DELIVERY,
            "unavailable" if push.requested else "disabled",
            reason="news_item_push_news_disabled" if push.requested else "news_disabled",
        )
    signal_lane = _wire_trading_lane(settings=settings, db=db, telemetry=telemetry, capabilities=capabilities)
    return _Components(
        news_pipeline=news_pipeline,
        news_bus=news_bus,
        runtime_manifest_sha=runtime_manifest_sha,
        signal_lane=signal_lane,
        telemetry=telemetry,
        capabilities=capabilities,
    )


async def _register_runtime_manifest(
    news_pipeline: NewsPipeline,
    *,
    capabilities: CapabilityStates,
) -> str | None:
    """Register the editorial Program manifest, or fault only the editorial capability.

    Reception, market facts and market notifications read no Program manifest, so a registration
    failure has nothing to say about them (#553 §7). The version check itself is unchanged: a
    manifest that cannot be registered leaves no Triage consumer to run an unproven Program.
    """

    try:
        await news_pipeline.register_runtime_manifest()
    except Exception as exc:
        logger.opt(exception=exc).error("News Program manifest registration failed; editorial capability faulted")
        news_pipeline.disable_editorial()
        capabilities.faulted(NEWS_EDITORIAL, f"news_program_manifest_registration_failed:{type(exc).__name__}")
        return None
    return news_pipeline.runtime_manifest_sha


def _wire_trading_lane(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry,
    capabilities: CapabilityStates,
) -> SignalLane | None:
    if not settings.trading.enabled:
        capabilities.disabled(TRADING_SIGNAL_LANE, "trading_disabled")
        return None
    try:
        lane = _wire_signal_lane(settings=settings, db=db, telemetry=telemetry)
    except Exception as exc:
        logger.opt(exception=exc).error("Trading Signal lane wiring failed; Trading capability faulted")
        capabilities.faulted(TRADING_SIGNAL_LANE, f"trading_signal_lane_wiring_failed:{type(exc).__name__}")
        return None
    if lane is None:
        capabilities.disabled(TRADING_SIGNAL_LANE, "trading_disabled")
    return lane
