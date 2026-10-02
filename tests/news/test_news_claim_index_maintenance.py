"""Durable index backlog drains without waiting for the retention sweep."""

from __future__ import annotations

import asyncio

import pytest

from tracefold.news.bus import TransientError
from tracefold.news.pipeline.maintenance import JanitorLoop


def test_index_backlog_drains_between_retention_turns_and_stops_with_workers() -> None:
    async def run() -> None:
        stop = asyncio.Event()
        batches = 0
        retention_turns = 0

        class Recall:
            async def advance(self) -> bool:
                nonlocal batches
                batches += 1
                if batches == 4:
                    stop.set()
                return True

        loop = JanitorLoop(db=object(), cold_db=object(), claim_recall=Recall())  # type: ignore[arg-type]

        async def retention() -> None:
            nonlocal retention_turns
            retention_turns += 1

        loop.turn = retention  # type: ignore[method-assign]
        await asyncio.wait_for(loop.run(stop_event=stop), timeout=2.0)
        assert batches == 4 and retention_turns == 1

    asyncio.run(run())


@pytest.mark.parametrize("unavailable", [False, True])
def test_idle_or_unavailable_index_does_not_spin_and_shutdown_interrupts_backoff(unavailable: bool) -> None:
    async def run() -> None:
        stop = asyncio.Event()
        attempted = asyncio.Event()
        batches = 0

        class Recall:
            async def advance(self) -> bool:
                nonlocal batches
                batches += 1
                attempted.set()
                if unavailable:
                    raise TransientError("embedding_unavailable")
                return False

        loop = JanitorLoop(db=object(), cold_db=object(), claim_recall=Recall())  # type: ignore[arg-type]
        task = asyncio.create_task(loop._claim_index_loop(stop_event=stop))
        await attempted.wait()
        await asyncio.sleep(0.2)
        assert batches == 1
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)

    asyncio.run(run())
