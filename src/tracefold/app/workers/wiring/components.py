from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.news import _wire_news_pipeline
from tracefold.app.workers.wiring.trading import _wire_trading_pipeline
from tracefold.news.consumers import NewsPipeline
from tracefold.platform.config.settings import Settings


@dataclass(slots=True)
class _Components:
    news_pipeline: NewsPipeline | None
    news_bus: Any | None
    trading_pipeline: Any | None = None


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
        await news_pipeline.register_runtime_manifest()
    trading_pipeline = _wire_trading_pipeline(settings=settings, db=db)
    return _Components(news_pipeline=news_pipeline, news_bus=news_bus, trading_pipeline=trading_pipeline)
