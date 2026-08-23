"""Shared runtime mechanics for News pipeline stages."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

from ..bus import DeferError, TransientError


class _Db:
    """Thin adapter over WorkerDatabase's News lane: run a sync repository function inside one session."""

    def __init__(self, db: Any, *, cold: bool = False) -> None:
        self._db = db
        self._lane = db.heavy_business() if cold else None

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos, repos.transaction():
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos:
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def _run(self, name: str, fn: Callable[[], Any], timeout_seconds: float) -> Any:
        try:
            if self._lane is not None:
                return await self._lane.run_business(name, fn, operation_timeout_seconds=timeout_seconds)
            return await self._db.run_news(name, fn, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"db_overrun:{name}") from exc


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
