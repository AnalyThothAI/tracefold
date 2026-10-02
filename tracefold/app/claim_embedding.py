"""Bounded external embedding route; no model weights in the application image."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import numpy as np

from tracefold.news.claim_recall import CALIBRATION, EmbedderIdentity, Probe, vector_bytes

EMBEDDING_SECONDS = 5.0
SELF_TEST_RETRY_SECONDS = 30.0
SELF_TEST = ("A central bank cuts interest rates.", "央行下调利率。", "A software vendor releases a game.")
log = logging.getLogger("tracefold.news")


class ClaimEmbedder:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        on_status: Callable[[bool], None] | None = None,
        max_batch_size: int = 2,
    ) -> None:
        self.identity: EmbedderIdentity = CALIBRATION.embedder
        if model != self.identity.model:
            raise ValueError("news_embedding_calibration_identity_mismatch")
        if max_batch_size < 1:
            raise ValueError("news_embedding_batch_size_invalid")
        self.model = model
        self.max_batch_size = max_batch_size
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=EMBEDDING_SECONDS,
            transport=transport,
        )
        self.ready = False
        self._tested = False
        self._retry_self_test_at = 0.0
        self._test_lock = asyncio.Lock()
        self.on_status = on_status

    def _report(self, available: bool) -> None:
        if self.on_status is not None:
            self.on_status(available)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _encode(self, texts: Sequence[str]) -> tuple[bytes, ...]:
        async with asyncio.timeout(EMBEDDING_SECONDS):
            response = await self.client.post("embeddings", json={"model": self.model, "input": list(texts)})
            response.raise_for_status()
            data: Any = response.json()
        rows = sorted(data["data"], key=lambda row: int(row["index"]))
        if [r["index"] for r in rows] != list(range(len(texts))):
            raise ValueError("news_embedding_response_cardinality")
        return tuple(vector_bytes(row["embedding"], self.identity) for row in rows)

    async def _vectors(self, texts: Sequence[str]) -> tuple[bytes, ...]:
        batches = [
            await self._encode(texts[start : start + self.max_batch_size])
            for start in range(0, len(texts), self.max_batch_size)
        ]
        return tuple(vector for batch in batches for vector in batch)

    async def self_test(self) -> bool:
        async with self._test_lock:
            if self._tested:
                return self.ready
            if time.monotonic() < self._retry_self_test_at:
                return False
            retryable = False
            try:
                vectors = await self._vectors(SELF_TEST)
                matrix = np.stack([np.frombuffer(v, dtype="<f2").astype(np.float32) for v in vectors])
                # A multilingual paraphrase must place the translation above an unrelated fact.
                self.ready = float(matrix[0] @ matrix[1]) > float(matrix[0] @ matrix[2])
            except httpx.HTTPStatusError as exc:
                self.ready = False
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            except (httpx.RequestError, TimeoutError):
                self.ready = False
                retryable = True
            except (ValueError, KeyError, TypeError):
                self.ready = False
            self._tested = not retryable
            if retryable:
                self._retry_self_test_at = time.monotonic() + SELF_TEST_RETRY_SECONDS
            self._report(self.ready)
            if not self.ready:
                log.warning("news_embedding_self_test_failed recall_degraded=true")
            return self.ready

    async def probes(self, texts: Sequence[str]) -> tuple[Probe, ...]:
        if not texts:
            return ()
        if await self.self_test():
            try:
                vectors = await self._vectors(texts)
                self._report(True)
                return tuple(Probe(t, v, self.identity.key) for t, v in zip(texts, vectors, strict=True))
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                log.warning("news_embedding_unavailable recall_degraded=true")
                self._report(False)
        return tuple(Probe(t) for t in texts)
