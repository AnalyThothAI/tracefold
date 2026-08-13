from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any

from tracefold.platform.resource import CpuTaskTimeout, ResourceAdmissionTimeout

from .projection import (
    NEWS_STORY_COMPUTE_TIMEOUT_SECONDS,
    NEWS_STORY_FAILURE_TIMEOUT_SECONDS,
    NEWS_STORY_LOAD_TIMEOUT_SECONDS,
    NEWS_STORY_PUBLISH_TIMEOUT_SECONDS,
    NewsProjectionInputExceeded,
    NewsProjectionService,
    compute_news_story_projection,
)


class NewsStoryProjection:
    """The sole dirty-triggered writer for the complete current Story closure."""

    def __init__(
        self,
        *,
        db: Any,
        heavy_db: Any,
        cpu: Any,
        dirty: asyncio.Event | None = None,
        debounce_seconds: float = 1.0,
        safety_seconds: float = 300.0,
        push_enabled: bool = False,
    ) -> None:
        self.db = db
        self.heavy_db = heavy_db
        self.cpu = cpu
        self.dirty = dirty or asyncio.Event()
        self.debounce_seconds = float(debounce_seconds)
        self.safety_seconds = float(safety_seconds)
        self.push_enabled = bool(push_enabled)
        self.service = NewsProjectionService(db=db)

    async def run(self, *, stop_event: asyncio.Event) -> None:
        await self.sample()
        while not stop_event.is_set():
            trigger = await _wait_for_story_trigger(
                dirty=self.dirty,
                stop_event=stop_event,
                timeout_seconds=self.safety_seconds,
            )
            if trigger == "stop":
                return
            if trigger == "dirty":
                self.dirty.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=self.debounce_seconds)
                if stop_event.is_set():
                    return
                self.dirty.clear()
            await self.sample()

    async def sample(self) -> None:
        try:
            snapshot = await self.heavy_db.run_business(
                "news_story_load",
                self.service.load,
                operation_timeout_seconds=NEWS_STORY_LOAD_TIMEOUT_SECONDS,
                now_ms=_now_ms(),
            )
            if snapshot.unchanged:
                await self.db.run_business(
                    "news_story_publish",
                    self.service.publish,
                    snapshot,
                    {},
                    operation_timeout_seconds=NEWS_STORY_PUBLISH_TIMEOUT_SECONDS,
                    now_ms=_now_ms(),
                    push_enabled=self.push_enabled,
                )
                return
            projection = await self.cpu.run(
                "news_story_compute",
                compute_news_story_projection,
                snapshot,
                service_timeout_seconds=NEWS_STORY_COMPUTE_TIMEOUT_SECONDS,
            )
            await self.heavy_db.run_business(
                "news_story_publish",
                self.service.publish,
                snapshot,
                projection,
                operation_timeout_seconds=NEWS_STORY_PUBLISH_TIMEOUT_SECONDS,
                now_ms=_now_ms(),
                push_enabled=self.push_enabled,
            )
        except ResourceAdmissionTimeout:
            return
        except (CpuTaskTimeout, NewsProjectionInputExceeded) as exc:
            error_code = "operation_timeout" if isinstance(exc, CpuTaskTimeout) else str(exc)
            try:
                await self.db.run_business(
                    "news_story_degraded",
                    self.service.mark_failed,
                    operation_timeout_seconds=NEWS_STORY_FAILURE_TIMEOUT_SECONDS,
                    now_ms=_now_ms(),
                    error_code=error_code,
                )
            except ResourceAdmissionTimeout:
                return


def _now_ms() -> int:
    return int(time.time() * 1_000)


async def _wait_for_story_trigger(
    *,
    dirty: asyncio.Event,
    stop_event: asyncio.Event,
    timeout_seconds: float,
) -> str:
    dirty_task = asyncio.create_task(dirty.wait())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {dirty_task, stop_task},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            return "stop"
        return "dirty" if dirty_task in done else "safety"
    finally:
        dirty_task.cancel()
        stop_task.cancel()
        await asyncio.gather(dirty_task, stop_task, return_exceptions=True)


__all__ = ["NewsStoryProjection"]
