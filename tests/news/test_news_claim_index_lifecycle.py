"""Fault and cancellation boundaries of the composed durable embedding drain."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tracefold.app.claim_embedding import ClaimEmbedder
from tracefold.news.bus import DeferError
from tracefold.news.claim_recall import CALIBRATION
from tracefold.news.pipeline import maintenance
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.storage.claim_recall import PgClaimRecall


class PendingDatabase:
    """A committed pending version survives external failures and cancellation."""

    def __init__(self) -> None:
        self.in_transaction = False
        self.backfill_calls = 0
        self.vector_writes: list[Any] = []
        self.pending_version = {"claim_ref": "event:claim", "text_sha256": "version", "embed_text": "A policy changed."}
        self.repos = SimpleNamespace(news=SimpleNamespace(claim_index=self))

    def backfill(self, **_kwargs: Any) -> int:
        self.backfill_calls += 1
        return 0

    def pending(self, limit: int, **_kwargs: Any) -> list[dict[str, str]]:
        assert limit == 2
        return [dict(self.pending_version)]

    def save_vectors(self, rows: list[Any], **_kwargs: Any) -> None:
        assert self.in_transaction
        self.vector_writes.extend(rows)

    async def tx(self, _name: str, fn: Callable[[Any], Any], **_kwargs: Any) -> Any:
        self.in_transaction = True
        try:
            return fn(self.repos)
        finally:
            self.in_transaction = False

    async def read(self, _name: str, fn: Callable[[Any], Any], **_kwargs: Any) -> Any:
        return await self.tx(_name, fn)


def route(transport: httpx.MockTransport) -> ClaimEmbedder:
    return ClaimEmbedder(
        model=CALIBRATION.embedder.model,
        base_url="https://embedding.test/v1",
        api_key="test-only-key",
        transport=transport,
        max_batch_size=2,
    )


def test_temporary_embedding_503_leaves_pending_fact_and_does_not_stop_retention() -> None:
    async def run() -> None:
        db = PendingDatabase()
        requests = 0
        turns = 0
        stop = asyncio.Event()

        def respond(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            assert not db.in_transaction
            requests += 1
            return httpx.Response(503)

        embedder = route(httpx.MockTransport(respond))
        loop = JanitorLoop(db=db, cold_db=db, claim_recall=PgClaimRecall(db, embedder=embedder), period_seconds=0.01)

        async def retention() -> None:
            nonlocal turns
            turns += 1
            if turns == 3:
                stop.set()

        loop.turn = retention  # type: ignore[method-assign]
        before = dict(db.pending_version)
        try:
            await asyncio.wait_for(loop.run(stop_event=stop), timeout=1)
        finally:
            await embedder.aclose()
        assert turns == 3 and requests == 1 and db.backfill_calls == 1
        assert db.pending_version == before and db.vector_writes == []

    asyncio.run(run())


def test_cancellation_during_external_encoding_drains_both_tasks_without_writing_a_vector() -> None:
    async def run() -> None:
        db = PendingDatabase()
        encoding = asyncio.Event()
        cancelled = asyncio.Event()
        retention_started = asyncio.Event()
        retention_cancelled = asyncio.Event()

        async def respond(_request: httpx.Request) -> httpx.Response:
            assert not db.in_transaction
            encoding.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("cancelled inference returned")

        embedder = route(httpx.MockTransport(respond))
        loop = JanitorLoop(db=db, cold_db=db, claim_recall=PgClaimRecall(db, embedder=embedder))

        async def retention() -> None:
            retention_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                retention_cancelled.set()

        loop.turn = retention  # type: ignore[method-assign]
        before = dict(db.pending_version)
        task = asyncio.create_task(loop.run(stop_event=asyncio.Event()))
        try:
            await asyncio.wait_for(encoding.wait(), timeout=1)
            await asyncio.wait_for(retention_started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await embedder.aclose()
        assert cancelled.is_set() and retention_cancelled.is_set()
        assert db.pending_version == before and db.vector_writes == []
        assert not db.in_transaction

    asyncio.run(run())


def test_cold_lane_admission_deferral_retries_without_stopping_the_maintenance_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        stop = asyncio.Event()
        attempted = asyncio.Event()
        maintenance_running = asyncio.Event()
        calls = 0
        waits: list[float] = []
        original_sleep = maintenance._sleep_or_stop

        class ColdRecall:
            async def advance(self) -> bool:
                nonlocal calls
                calls += 1
                await maintenance_running.wait()
                if calls == 1:
                    attempted.set()
                    raise DeferError("db_admission_timeout:news_claim_index_backfill")
                stop.set()
                return False

        async def bounded_test_sleep(event: asyncio.Event, seconds: float) -> None:
            waits.append(seconds)
            await original_sleep(event, min(seconds, 0.01))

        monkeypatch.setattr(maintenance, "_sleep_or_stop", bounded_test_sleep)
        loop = JanitorLoop(db=object(), cold_db=object(), claim_recall=ColdRecall())  # type: ignore[arg-type]

        async def retention() -> None:
            maintenance_running.set()
            await attempted.wait()

        loop.turn = retention  # type: ignore[method-assign]
        await asyncio.wait_for(loop.run(stop_event=stop), timeout=1)
        assert calls == 2 and 30.0 in waits

    asyncio.run(run())
