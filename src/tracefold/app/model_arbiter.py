from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from tracefold.platform.model_candidate import NativeModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout

_MODEL_IDLE_SECONDS = 5.0


async def run_model_arbiter(
    candidates: Sequence[NativeModelCandidate],
    *,
    stop_event: asyncio.Event,
) -> None:
    """Run one serial native-state model slot without a generic frontier."""

    ordered = tuple(candidates)
    while not stop_event.is_set():
        now_ms = int(time.time() * 1_000)
        available = []
        for adapter in ordered:
            try:
                candidate = await adapter.peek(now_ms=now_ms)
            except ResourceAdmissionTimeout:
                continue
            if candidate is not None:
                available.append((candidate, adapter))
        if not available:
            await _wait_or_stop(stop_event, _MODEL_IDLE_SECONDS)
            continue
        candidate, adapter = min(
            available,
            key=lambda item: (
                item[0].due_at_ms,
                item[0].stable_order,
                item[0].kind,
                item[0].target_key,
            ),
        )
        progressed = await adapter.execute(candidate)
        if progressed:
            await asyncio.sleep(0)
        else:
            await _wait_or_stop(stop_event, _MODEL_IDLE_SECONDS)


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=float(seconds))
    except TimeoutError:
        return


__all__: list[str] = []
