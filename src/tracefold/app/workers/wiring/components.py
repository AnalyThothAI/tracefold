from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.news import _wire_news_pipeline
from tracefold.app.workers.wiring.trading import _wire_capital_lane
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading.capital_lane import CapitalLane

if TYPE_CHECKING:
    from tracefold.integrations.rabbitmq import RabbitMQBus


@dataclass(slots=True)
class _Components:
    news_pipeline: NewsPipeline | None
    news_bus: RabbitMQBus | None
    capital_lane: CapitalLane | None = None
    telemetry: TelemetryRegistry | None = None


async def _wire_components(
    *,
    settings: Settings,
    db: WorkerDatabase,
    finite: FiniteOperations,
    telemetry: TelemetryRegistry,
) -> _Components:
    if settings.news.push.enabled and not settings.news.enabled:
        raise RuntimeError("news_push_unavailable:news_item_push_news_disabled")
    news_pipeline: NewsPipeline | None = None
    news_bus: RabbitMQBus | None = None
    if settings.news.enabled:
        news_bus, news_pipeline = await _wire_news_pipeline(
            settings=settings,
            db=db,
            finite=finite,
            telemetry=telemetry,
        )
        await news_pipeline.register_runtime_manifest()
    capital_lane = _wire_capital_lane(settings=settings, db=db, telemetry=telemetry)
    return _Components(
        news_pipeline=news_pipeline,
        news_bus=news_bus,
        capital_lane=capital_lane,
        telemetry=telemetry,
    )
