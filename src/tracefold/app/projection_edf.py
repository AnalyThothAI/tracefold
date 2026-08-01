from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from tracefold.platform.projection import ProjectionCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout

_IDLE_POLL_SECONDS = 0.250


async def run_projection_edf(
    candidates: Sequence[ProjectionCandidate],
    *,
    stop_event: asyncio.Event,
    telemetry: Any,
) -> None:
    """Run the one stateless, non-preemptive projection EDF."""

    ordered = tuple(candidates)
    while not stop_event.is_set():
        now_ms = _now_ms()
        available = []
        for candidate in ordered:
            try:
                shard = await candidate.peek(now_ms=now_ms)
            except ResourceAdmissionTimeout:
                continue
            if shard is not None:
                available.append((shard, candidate))
        if not available:
            await _wait_or_stop(stop_event, _IDLE_POLL_SECONDS)
            continue
        shard, candidate = min(
            available,
            key=lambda item: (
                item[0].deadline_at_ms,
                item[0].stable_order,
                item[0].domain,
                item[0].shard_key,
            ),
        )
        progressed = await candidate.execute(shard)
        if progressed:
            completed_at_ms = _now_ms()
            if completed_at_ms > shard.deadline_at_ms and telemetry is not None:
                telemetry.record_projection_deadline_miss(
                    "projection_edf",
                    shard.domain,
                )
            await _wait_or_stop(stop_event, _IDLE_POLL_SECONDS)
        else:
            await _wait_or_stop(stop_event, _IDLE_POLL_SECONDS)


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["run_projection_edf"]
