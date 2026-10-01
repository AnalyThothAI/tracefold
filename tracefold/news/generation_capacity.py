"""News-owned generation admission and stage call observations; no provider scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

GENERATION_CAPACITY_WAIT = "news_generation_capacity_wait"


@dataclass(slots=True)
class GenerationStage:
    """Mutable call facts shared by the children of one semantic, planning or card stage."""

    started_calls: int = 0
    cancelled_calls: int = 0


_STAGE: ContextVar[GenerationStage | None] = ContextVar("news_generation_stage", default=None)


@contextmanager
def generation_stage() -> Iterator[GenerationStage]:
    """Give one workflow stage its own observations without changing its deadline."""

    stage = GenerationStage()
    token = _STAGE.set(stage)
    try:
        yield stage
    finally:
        _STAGE.reset(token)


@contextmanager
def generation_call() -> Iterator[None]:
    """Record a real generated or native Predict call and cancellation while it was running."""

    stage = _STAGE.get()
    if stage is not None:
        stage.started_calls += 1
    try:
        yield
    except asyncio.CancelledError:
        if stage is not None:
            stage.cancelled_calls += 1
        raise


class _CapacityWaitCancelled(asyncio.CancelledError):
    """Keep the stage observations when its surrounding timeout cancels admission."""

    def __init__(self, stage: GenerationStage | None) -> None:
        super().__init__(GENERATION_CAPACITY_WAIT)
        self.stage = stage


def generation_capacity_wait_timed_out(exc: BaseException) -> bool:
    """A waiting operation timed out, and no sibling provider call was cancelled with it."""

    if not isinstance(exc, TimeoutError) or not isinstance(exc.__cause__, _CapacityWaitCancelled):
        return False
    stage = exc.__cause__.stage
    return stage is None or stage.cancelled_calls == 0


def generation_capacity_wait_before_call(exc: BaseException) -> bool:
    """The whole observed stage timed out waiting without starting any provider call."""

    if not generation_capacity_wait_timed_out(exc):
        return False
    cause = exc.__cause__
    return isinstance(cause, _CapacityWaitCancelled) and cause.stage is not None and cause.stage.started_calls == 0


class NewsGenerationCapacity:
    """One runtime's actual generative calls share slots, including declared fallbacks.

    Waiting has no separate deadline: the News stage owns it. Direct task cancellation remains
    CancelledError. The timeout cause retains that stage's observations so mixed fanout cannot
    mistake a cancelled provider call for a stage that only waited for admission.
    """

    def __init__(self, concurrent: int) -> None:
        if not 1 <= concurrent <= 32:
            raise ValueError("news_model_concurrency_invalid")
        self._slots = asyncio.Semaphore(concurrent)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        try:
            await self._slots.acquire()
        except asyncio.CancelledError as exc:
            raise _CapacityWaitCancelled(_STAGE.get()) from exc
        try:
            yield
        finally:
            self._slots.release()
