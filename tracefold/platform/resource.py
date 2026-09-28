from __future__ import annotations

import asyncio
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


async def drain_futures(pending: set[asyncio.Future[Any]], *, timeout_seconds: float) -> bool:
    """Wait for physical completions before a resource permit may be released."""
    active = {future for future in pending if not future.done()}
    if not active:
        return True
    _, unfinished = await asyncio.wait(active, timeout=max(0.0, float(timeout_seconds)))
    return not unfinished


__all__ = [
    "ResourceAdmissionTimeout",
    "ResourceCapability",
    "ResourceOperationOverrun",
    "await_concurrent_future",
    "drain_futures",
]
