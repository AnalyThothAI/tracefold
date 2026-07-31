from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class ResourceAdmissionTimeout(TimeoutError):
    """A bounded capability could not accept work before submission."""


class ResourceOperationOverrun(RuntimeError):
    """A synchronous operation outlived its code-owned outer deadline."""


class CpuTaskTimeout(TimeoutError):
    """A deterministic CPU child reached its declared execution timeout."""


class CpuTaskProcessExpired(RuntimeError):
    """The spawn-only CPU child exited unexpectedly."""


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
    "ResourceOperationOverrun",
    "ResourceSubmissionTracker",
]
