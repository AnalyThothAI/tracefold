"""Shared runtime mechanics for News pipeline stages."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, Protocol

from tracefold.news.market_review.instrument_storage import InstrumentsRepository
from tracefold.news.market_review.storage import PriceRepository
from tracefold.news.storage.root import NewsRepository


class NewsRepositories(Protocol):
    """The News callback capability; deliberately no raw connection or Trading repository."""

    @property
    def news(self) -> NewsRepository: ...

    @property
    def instruments(self) -> InstrumentsRepository: ...

    @property
    def price(self) -> PriceRepository: ...


class NewsDatabasePort(Protocol):
    """Everything a News pipeline stage may ask of the process database, and nothing else.

    Two bounded operations: run a synchronous repository function inside one session (`read`), or inside
    one session and one transaction (`tx`). Admission and overrun are the caller's own `DeferError` and
    `TransientError`, so a stage never learns which lane, pool or executor answered it.

    The port exists because the pipeline used to accept an untyped object and then call
    `worker_session`/`run_news`/`heavy_business` on it: no import edge, but a hard dependency on one App
    class's exact shape. `tracefold.app` implements this; `tracefold.news` never names the implementation.
    """

    async def read[T](self, name: str, fn: Callable[[NewsRepositories], T], *, timeout_seconds: float = 3.0) -> T: ...

    async def tx[T](self, name: str, fn: Callable[[NewsRepositories], T], *, timeout_seconds: float = 3.0) -> T: ...


async def _receive_or_stop(client: Any, *, stop_event: asyncio.Event) -> Any | None:
    receive_task = asyncio.create_task(client.receive())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait({receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and stop_task.result():
            return None
        return await receive_task
    finally:
        for task in (receive_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receive_task, stop_task, return_exceptions=True)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.001, float(seconds)))
