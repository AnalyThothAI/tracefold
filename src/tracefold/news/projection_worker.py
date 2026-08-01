from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.platform.resource import CpuTaskTimeout, ResourceAdmissionTimeout

from .projection import (
    NEWS_STORY_OPERATION_TIMEOUT_SECONDS,
    NewsProjectionInputExceeded,
    NewsProjectionService,
    compute_news_story_projection,
)


class NewsStoryProjection:
    """The sole fixed-period writer for the complete current Story closure."""

    def __init__(self, *, db: Any, cpu: Any) -> None:
        self.db = db
        self.cpu = cpu
        self.service = NewsProjectionService(db=db)

    async def sample(self) -> None:
        try:
            async with asyncio.timeout(NEWS_STORY_OPERATION_TIMEOUT_SECONDS):
                snapshot = await self.db.run_business(
                    "news_story_load",
                    self.service.load,
                    operation_timeout_seconds=3.0,
                    now_ms=_now_ms(),
                )
                if snapshot.unchanged:
                    return
                projection = await self.cpu.run(
                    "news_story_compute",
                    compute_news_story_projection,
                    snapshot,
                    service_timeout_seconds=NEWS_STORY_OPERATION_TIMEOUT_SECONDS - 5.0,
                    operation_timeout_seconds=NEWS_STORY_OPERATION_TIMEOUT_SECONDS - 5.0,
                )
                await self.db.run_business(
                    "news_story_publish",
                    self.service.publish,
                    snapshot,
                    projection,
                    operation_timeout_seconds=5.0,
                    now_ms=_now_ms(),
                )
        except ResourceAdmissionTimeout:
            return
        except (TimeoutError, CpuTaskTimeout, NewsProjectionInputExceeded) as exc:
            error_code = "operation_timeout" if isinstance(exc, (TimeoutError, CpuTaskTimeout)) else str(exc)
            try:
                await self.db.run_business(
                    "news_story_degraded",
                    self.service.mark_failed,
                    operation_timeout_seconds=3.0,
                    now_ms=_now_ms(),
                    error_code=error_code,
                )
            except ResourceAdmissionTimeout:
                return


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["NewsStoryProjection"]
