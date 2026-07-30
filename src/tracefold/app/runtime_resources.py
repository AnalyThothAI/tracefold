from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

from pebble import ProcessExpired, ProcessPool

from tracefold.platform.workers.resource_errors import (
    CpuTaskProcessExpired,
    CpuTaskTimeout,
)

_CAPACITIES = {
    "realtime_db": 1,
    "background_db": 1,
    "provider_io": 3,
    "model": 1,
    "cpu": 1,
}
_PROVIDER_CAPACITIES = {
    "global": 3,
    "per_host": 2,
    "profile_gmgn": 1,
    "profile_binance": 1,
    "image": 1,
}


class ProviderGovernor:
    """One process-wide provider concurrency budget."""

    def __init__(self) -> None:
        self._global = asyncio.Semaphore(_PROVIDER_CAPACITIES["global"])
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self._lanes = {
            name: asyncio.Semaphore(capacity)
            for name, capacity in _PROVIDER_CAPACITIES.items()
            if name not in {"global", "per_host"}
        }

    @property
    def capacities(self) -> dict[str, int]:
        return dict(_PROVIDER_CAPACITIES)

    @asynccontextmanager
    async def acquire(self, *, host: str, lane: str | None = None):
        normalized_host = str(host).strip().lower()
        if not normalized_host:
            raise ValueError("provider_host_required")
        if lane is not None and lane not in self._lanes:
            raise ValueError(f"provider_lane_invalid:{lane}")
        host_gate = self._hosts.setdefault(
            normalized_host,
            asyncio.Semaphore(_PROVIDER_CAPACITIES["per_host"]),
        )
        lane_gate = self._lanes.get(lane) if lane is not None else None
        async with self._global, host_gate:
            if lane_gate is None:
                yield
            else:
                async with lane_gate:
                    yield


class RuntimeResources:
    """Code-owned process resource budget for the steady workers runtime."""

    def __init__(self) -> None:
        self._executors = {
            name: ThreadPoolExecutor(
                max_workers=capacity,
                thread_name_prefix=f"tracefold-{name}",
            )
            for name, capacity in _CAPACITIES.items()
            if name != "cpu"
        }
        self._cpu_pool = ProcessPool(
            max_workers=_CAPACITIES["cpu"],
            context=multiprocessing.get_context("spawn"),
        )
        self._closed = False
        self._accepting = True
        self._pending: dict[str, set[asyncio.Future[Any]]] = {name: set() for name in _CAPACITIES}

    @property
    def capacities(self) -> dict[str, int]:
        return dict(_CAPACITIES)

    async def run_realtime_db[T](self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        return await self._run_thread("realtime_db", function, *args, **kwargs)

    async def run_background_db[T](self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        return await self._run_thread("background_db", function, *args, **kwargs)

    async def run_provider_io[T](self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        return await self._run_thread("provider_io", function, *args, **kwargs)

    async def run_provider_cleanup[T](
        self,
        function: Callable[..., T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        return await self._run_thread(
            "provider_io",
            function,
            *args,
            allow_shutdown=True,
            **kwargs,
        )

    async def run_model[T](self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        return await self._run_thread("model", function, *args, **kwargs)

    async def run_model_cleanup[T](
        self,
        function: Callable[..., T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        return await self._run_thread(
            "model",
            function,
            *args,
            allow_shutdown=True,
            **kwargs,
        )

    async def run_cpu[T](
        self,
        function: Callable[..., T],
        /,
        *args: Any,
        timeout_seconds: float,
        **kwargs: Any,
    ) -> T:
        self._require_open()
        _require_spawn_safe_function(function)
        future = self._cpu_pool.schedule(
            function,
            args=args,
            kwargs=kwargs,
            timeout=float(timeout_seconds),
        )
        wrapped = asyncio.wrap_future(future)
        self._pending["cpu"].add(wrapped)
        try:
            return await wrapped
        except FutureTimeoutError as exc:
            raise CpuTaskTimeout(f"cpu_task_timeout:{float(timeout_seconds):g}s") from exc
        except ProcessExpired as exc:
            raise CpuTaskProcessExpired(f"cpu_task_process_expired:pid={exc.pid}:exitcode={exc.exitcode}") from exc
        finally:
            self._pending["cpu"].discard(wrapped)

    async def _run_thread[T](
        self,
        lane: str,
        function: Callable[..., T],
        /,
        *args: Any,
        allow_shutdown: bool = False,
        **kwargs: Any,
    ) -> T:
        self._require_open(allow_shutdown=allow_shutdown)
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._executors[lane],
            partial(function, *args, **kwargs),
        )
        self._pending[lane].add(future)
        try:
            return await future
        finally:
            self._pending[lane].discard(future)

    def begin_shutdown(self) -> None:
        self._accepting = False

    async def drain(
        self,
        lanes: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> bool:
        pending = {future for lane in lanes for future in self._pending[lane] if not future.done()}
        if not pending:
            return True
        done, still_pending = await asyncio.wait(
            pending,
            timeout=max(0.0, float(timeout_seconds)),
        )
        del done
        return not still_pending

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
        self._cpu_pool.stop()
        try:
            self._cpu_pool.join(timeout=1)
        except FutureTimeoutError:
            self._cpu_pool.stop()

    def _require_open(self, *, allow_shutdown: bool = False) -> None:
        if self._closed:
            raise RuntimeError("runtime_resources_closed")
        if not self._accepting and not allow_shutdown:
            raise RuntimeError("runtime_resources_shutting_down")


def _require_spawn_safe_function(function: Callable[..., Any]) -> None:
    qualname = str(getattr(function, "__qualname__", ""))
    module = str(getattr(function, "__module__", ""))
    if not module or not qualname or "<locals>" in qualname:
        raise TypeError("cpu_task_requires_top_level_function")
