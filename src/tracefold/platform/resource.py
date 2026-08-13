from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from enum import StrEnum
from typing import Any


class ResourceAdmissionTimeout(TimeoutError):
    """A bounded capability could not accept work before submission."""


class ResourceCapability(StrEnum):
    """The fixed physical capability that owns a submitted operation."""

    DATABASE_BUSINESS = "database_business"
    DATABASE_CONTROL = "database_control"
    FINITE_OPERATION = "finite_operation"
    MODEL_ADAPTER = "model_adapter"
    CPU_PROCESS = "cpu_process"


class ResourceOperationOverrun(RuntimeError):
    """A submitted operation still owns its typed physical capability."""

    def __init__(
        self,
        *,
        capability: ResourceCapability,
        operation_name: str,
    ) -> None:
        self.capability = capability
        self.operation_name = str(operation_name).strip() or "unknown"
        super().__init__(f"resource_operation_overrun:{self.capability.value}:{self.operation_name}")


class CpuTaskTimeout(TimeoutError):
    """A deterministic CPU child reached its declared execution timeout."""


class CpuTaskProcessExpired(RuntimeError):
    """The spawn-only CPU child exited unexpectedly."""


async def await_concurrent_future[T](
    underlying: Future[T],
    wrapped: asyncio.Future[T],
    *,
    timeout_seconds: float,
    capability: ResourceCapability,
    operation_name: str,
) -> T:
    """Let an already-finished native future win over a delayed asyncio callback."""

    wrapped.add_done_callback(_retrieve_future_exception)
    done, _ = await asyncio.wait(
        {wrapped},
        timeout=max(0.001, float(timeout_seconds)),
    )
    if done:
        return await wrapped
    if underlying.done():
        wrapped.cancel()
        return underlying.result()
    raise ResourceOperationOverrun(
        capability=capability,
        operation_name=operation_name,
    )


def _retrieve_future_exception(future: asyncio.Future[Any]) -> None:
    """Retrieve a late native failure after its caller has left the envelope."""

    if not future.cancelled():
        future.exception()


class ResourceSubmissionTracker:
    """Track only the resource operation currently awaited by a claimed shard."""

    def __init__(self) -> None:
        self._submitted = False

    @property
    def submitted(self) -> bool:
        return self._submitted

    async def run[T](
        self,
        submit: Callable[[Callable[[], None]], Awaitable[T]],
    ) -> T:
        self._submitted = False
        try:
            result = await submit(self._mark_submitted)
        except (asyncio.CancelledError, ResourceOperationOverrun):
            raise
        except BaseException:
            self._submitted = False
            raise
        self._submitted = False
        return result

    def _mark_submitted(self) -> None:
        self._submitted = True


__all__ = [
    "CpuTaskProcessExpired",
    "CpuTaskTimeout",
    "ResourceAdmissionTimeout",
    "ResourceCapability",
    "ResourceOperationOverrun",
    "ResourceSubmissionTracker",
    "await_concurrent_future",
]
