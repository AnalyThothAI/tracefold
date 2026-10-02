"""Bounded external embedding route; no model weights in the application image."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import httpx
import numpy as np

from tracefold.news.claim_recall import CALIBRATION, EmbedderIdentity, Probe, vector_bytes

EMBEDDING_SECONDS = 5.0
SELF_TEST = ("A central bank cuts interest rates.", "央行下调利率。", "A software vendor releases a game.")
log = logging.getLogger("tracefold.news")


class ClaimEmbedder:
    def __init__(
        self, *, model: str, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.identity: EmbedderIdentity = CALIBRATION.embedder
        if model != self.identity.model:
            raise ValueError("news_embedding_calibration_identity_mismatch")
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=EMBEDDING_SECONDS,
            transport=transport,
        )
        self.ready = False
        self._tested = False
        self._test_lock = asyncio.Lock()

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

    async def self_test(self) -> bool:
        async with self._test_lock:
            if self._tested:
                return self.ready
            try:
                vectors = await self._encode(SELF_TEST)
                matrix = np.stack([np.frombuffer(v, dtype="<f2").astype(np.float32) for v in vectors])
                # A multilingual paraphrase must place the translation above an unrelated fact.
                self.ready = float(matrix[0] @ matrix[1]) > float(matrix[0] @ matrix[2])
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                self.ready = False
            self._tested = True
            if not self.ready:
                log.warning("news_embedding_self_test_failed recall_degraded=true")
            return self.ready

    async def probes(self, texts: Sequence[str]) -> tuple[Probe, ...]:
        if not texts:
            return ()
        if await self.self_test():
            try:
                vectors = await self._encode(texts)
                return tuple(Probe(t, v, self.identity.key) for t, v in zip(texts, vectors, strict=True))
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                log.warning("news_embedding_unavailable recall_degraded=true")
        return tuple(Probe(t) for t in texts)
