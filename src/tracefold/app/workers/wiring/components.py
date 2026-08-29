from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tracefold.app.trading_bindings import project_binding_credentials
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.news import _wire_news_pipeline
from tracefold.app.workers.wiring.trading import _wire_capital_lane, _wire_venue_catalog
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading.capital_lane import CapitalLane
from tracefold.trading.catalog import VenueCatalog

if TYPE_CHECKING:
    from tracefold.integrations.rabbitmq import RabbitMQBus


@dataclass(slots=True)
class _Components:
    news_pipeline: NewsPipeline | None
    news_bus: RabbitMQBus | None
    capital_lane: CapitalLane | None = None
    venue_catalog: VenueCatalog | None = None
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
    trading_db = WorkerTradingDatabase(db)
    await project_binding_credentials(settings, trading_db)
    capital_lane = _wire_capital_lane(settings=settings, db=db, telemetry=telemetry)
    if capital_lane is not None:
        await capital_lane.start()
    else:
        now_ms = int(time.time() * 1_000)
        updated = await trading_db.tx(
            "trading_decision_disabled",
            lambda repos: repos.trading.set_decision_runtime(
                state="DISABLED",
                heartbeat_at_ms=None,
                reason="trading_disabled",
                now_ms=now_ms,
            ),
            timeout_seconds=10.0,
        )
        if not updated:
            raise RuntimeError("trading_decision_runtime_missing")
    venue_catalog = _wire_venue_catalog(db=db)
    return _Components(
        news_pipeline=news_pipeline,
        news_bus=news_bus,
        capital_lane=capital_lane,
        venue_catalog=venue_catalog,
        telemetry=telemetry,
    )
