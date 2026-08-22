"""One-slot cold database admission shared by display/research polling loops."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

from .bus import DeferError, TransientError


class ColdDatabase:
    """Short database sessions on the heavy-business lane, never the four-slot News hot lane."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._lane = db.heavy_business()

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos, repos.transaction():
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos:
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def _run(self, name: str, fn: Callable[[], Any], timeout_seconds: float) -> Any:
        try:
            return await self._lane.run_business(name, fn, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"cold_db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"cold_db_overrun:{name}") from exc


__all__ = ["ColdDatabase"]
