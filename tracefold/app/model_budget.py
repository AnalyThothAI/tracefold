"""One endpoint budget across News and Trading, using PostgreSQL session locks."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from tracefold.app.repository_session import async_postgres_connection, postgres_connection


class ModelBudget:
    """A provider call holds a session lock, never a database transaction."""

    def __init__(self, settings: Any, *, resource: str, timeout_s: float) -> None:
        self.settings = settings
        self.timeout_s = timeout_s
        self.capacity = settings.llm.max_shared_concurrent_calls
        self.keys = tuple(
            int.from_bytes(
                hashlib.sha256(f"tracefold-model|{resource}|{slot}".encode()).digest()[:8], "big", signed=True
            )
            for slot in range(self.capacity)
        )

    def _reserve(self, stop: threading.Event) -> tuple[Any, Any]:
        session = postgres_connection(self.settings, application_name="tracefold_model_budget", long_lived=True)
        conn = session.__enter__()
        deadline = time.monotonic() + self.timeout_s
        try:
            while not stop.is_set():
                for key in self.keys:
                    acquired = conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()
                    if acquired["acquired"]:
                        return session, conn
                if time.monotonic() >= deadline:
                    raise TimeoutError("model_shared_budget_timeout")
                stop.wait(0.1)
            raise TimeoutError("model_shared_budget_cancelled")
        except BaseException:
            session.__exit__(None, None, None)
            raise

    @contextmanager
    def acquire(self) -> Iterator[None]:
        session, _ = self._reserve(threading.Event())
        try:
            yield
        finally:
            session.__exit__(None, None, None)

    @asynccontextmanager
    async def acquire_async(self) -> AsyncIterator[None]:
        async with async_postgres_connection(
            self.settings,
            application_name="tracefold_model_budget",
            long_lived=True,
        ) as conn:
            deadline = time.monotonic() + self.timeout_s
            while True:
                for key in self.keys:
                    cursor = await conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,))
                    acquired = await cursor.fetchone()
                    if acquired["acquired"]:
                        yield
                        return
                if time.monotonic() >= deadline:
                    raise TimeoutError("model_shared_budget_timeout")
                await asyncio.sleep(0.1)
