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
    build_story_projection,
)


class NewsStoryProjectionWorker:
    """The sole dirty-triggered writer for the complete current Story closure."""

    def __init__(
        self,
        *,
        db: Any,
        heavy_db: Any,
        cpu: Any,
        telemetry: Any | None = None,
        dirty: asyncio.Event | None = None,
        debounce_seconds: float = 1.0,
        safety_seconds: float = 300.0,
    ) -> None:
        self.db = db
        self.heavy_db = heavy_db
        self.cpu = cpu
        self.telemetry = telemetry
        self.dirty = dirty or asyncio.Event()
        self.debounce_seconds = float(debounce_seconds)
        self.safety_seconds = float(safety_seconds)
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
                )
                return
            projection = await self.cpu.run(
                "news_story_compute",
                build_story_projection,
                snapshot,
                service_timeout_seconds=NEWS_STORY_COMPUTE_TIMEOUT_SECONDS,
            )
            publish_result = await self.heavy_db.run_business(
                "news_story_publish",
                self.service.publish,
                snapshot,
                projection,
                operation_timeout_seconds=NEWS_STORY_PUBLISH_TIMEOUT_SECONDS,
                now_ms=_now_ms(),
            )
            if publish_result.get("projection_status") == "rebuilt":
                self._record_projection_diagnostics(projection.diagnostics)
        except ResourceAdmissionTimeout:
            self.dirty.set()
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

    def _record_projection_diagnostics(self, diagnostics: dict[str, Any]) -> None:
        if self.telemetry is None:
            return
        for measure in (
            "input_physical_item_count",
            "input_encoded_bytes",
            "population_physical_item_count",
            "exact_atom_count",
            "exact_membership_count",
            "candidate_pair_count",
            "preliminary_rss_candidate_pair_count",
            "candidate_pair_peak",
            "accepted_decision_count",
            "rejected_decision_count",
            "conflict_veto_count",
            "ambiguity_split_count",
            "grounded_provider_count",
            "story_count",
        ):
            self.telemetry.set_news_story_projection_value(measure, int(diagnostics.get(measure, 0)))
        family_counts = diagnostics.get("event_family_counts")
        if isinstance(family_counts, dict):
            for family in ("market_telemetry", "filing", "disaster", "general"):
                self.telemetry.set_news_story_projection_value(
                    f"event_family_{family}",
                    int(family_counts.get(family, 0)),
                )


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


__all__ = ["NewsStoryProjectionWorker"]
