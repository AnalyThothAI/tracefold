"""Bounded external embedding route; no model weights in the application image."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable, Sequence
from importlib.resources import files
from typing import Any

import httpx
import numpy as np

from tracefold.news.claim_recall import CALIBRATION, EmbedderIdentity, Probe, vector_bytes

EMBEDDING_SECONDS = 5.0
SELF_TEST_RETRY_SECONDS = 30.0
GOLDEN_RESOURCE = "claim_embedding_golden.json"
GOLDEN_MIN_COSINE = 0.998
GOLDEN_MAX_ABS_ERROR = 0.01
log = logging.getLogger("tracefold.news")


def golden_vectors(identity: EmbedderIdentity) -> tuple[tuple[str, ...], np.ndarray]:
    data = json.loads(files("tracefold.news").joinpath(GOLDEN_RESOURCE).read_text())
    if EmbedderIdentity(**data["embedder"]) != identity or len(data["vectors"]) < 6:
        raise ValueError("news_embedding_golden_identity_mismatch")
    texts = tuple(data["vectors"])
    raw = [base64.b64decode(data["vectors"][text], validate=True) for text in texts]
    if any(not isinstance(text, str) or not text.strip() for text in texts) or any(
        len(vector) != identity.dimensions * 2 for vector in raw
    ):
        raise ValueError("news_embedding_golden_shape_invalid")
    matrix = np.stack([np.frombuffer(vector, dtype="<f2").astype(np.float32) for vector in raw])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if not np.isfinite(matrix).all() or (norms <= 0).any():
        raise ValueError("news_embedding_golden_invalid")
    return texts, matrix / norms


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
        if data["model"] != self.identity.model or data["embedder_identity"] != self.identity.key:
            raise ValueError("news_embedding_response_identity_mismatch")
        rows = data["data"]
        if not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get("index")) is not int for r in rows):
            raise ValueError("news_embedding_response_index_invalid")
        rows = sorted(rows, key=lambda row: row["index"])
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
                texts, expected = golden_vectors(self.identity)
                vectors = await self._vectors(texts)
                matrix = np.stack([np.frombuffer(v, dtype="<f2").astype(np.float32) for v in vectors])
                matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
                self.ready = bool(
                    (np.einsum("ij,ij->i", matrix, expected) >= GOLDEN_MIN_COSINE).all()
                    and (np.abs(matrix - expected).max(axis=1) <= GOLDEN_MAX_ABS_ERROR).all()
                )
            except httpx.HTTPStatusError as exc:
                self.ready = False
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            except (httpx.RequestError, TimeoutError):
                self.ready = False
                retryable = True
            except (ValueError, KeyError, TypeError, OSError):
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
