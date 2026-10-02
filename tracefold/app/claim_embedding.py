"""Offline, fixed MiniLM inference with one bounded Workers-owned execution lane."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from importlib.resources import files
from pathlib import Path
from typing import Any

import numpy as np

from tracefold.news.claim_recall import CALIBRATION, EmbedderIdentity, Probe, vector_bytes

EMBEDDING_SECONDS = 5.0
ADMISSION_SECONDS = 0.5
STARTUP_SECONDS = 30.0
CLOSE_SECONDS = 5.0
MAX_TEXT_CHARS = 32_768
GOLDEN_RESOURCE = "claim_embedding_golden.json"
GOLDEN_MIN_COSINE = 0.998
GOLDEN_MAX_ABS_ERROR = 0.01
MODEL_FILES = ("tokenizer.json", "onnx/model.onnx")
MODEL_MANIFEST = "tracefold-model.json"
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


def model_snapshot_dir(cache_dir: Path) -> Path:
    return cache_dir / CALIBRATION.embedder.revision


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _snapshot_manifest(snapshot: Path) -> dict[str, Any]:
    return {
        "model": CALIBRATION.embedder.model,
        "revision": CALIBRATION.embedder.revision,
        "files": {name: _file_sha256(snapshot / name) for name in MODEL_FILES},
    }


def validate_model_snapshot(cache_dir: Path) -> dict[str, Any]:
    snapshot = model_snapshot_dir(cache_dir)
    expected = json.loads((snapshot / MODEL_MANIFEST).read_text(encoding="utf-8"))
    actual = _snapshot_manifest(snapshot)
    if actual != expected:
        raise ValueError("news_embedding_model_snapshot_mismatch")
    return actual


def prepare_model(cache_dir: Path) -> dict[str, Any]:
    """Explicit download only; never imports ONNX or constructs an inference session."""
    from huggingface_hub import snapshot_download

    cache_dir.mkdir(parents=True, exist_ok=True)
    snapshot = model_snapshot_dir(cache_dir)
    try:
        return {"prepared": True, "snapshot": str(snapshot), **validate_model_snapshot(cache_dir)}
    except (OSError, ValueError, TypeError):
        pass
    # A complete revision is published together. Interrupted downloads cannot become runtime input.
    with tempfile.TemporaryDirectory(prefix=".news-embedding-", dir=cache_dir) as staging_name:
        staging = Path(staging_name)
        snapshot_download(
            CALIBRATION.embedder.model,
            revision=CALIBRATION.embedder.revision,
            allow_patterns=list(MODEL_FILES),
            local_dir=staging,
        )
        manifest = _snapshot_manifest(staging)
        (staging / MODEL_MANIFEST).write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        # Preserve a damaged previous directory until the replacement has been published successfully.
        previous = staging.with_name(staging.name + "-previous")
        if snapshot.exists():
            os.replace(snapshot, previous)
        try:
            os.replace(staging, snapshot)
        except BaseException:
            if previous.exists():
                os.replace(previous, snapshot)
            raise
        finally:
            if previous.exists():
                shutil.rmtree(previous)
    return {"prepared": True, "snapshot": str(snapshot), **manifest}


@dataclass
class _Operation:
    cancelled: threading.Event = field(default_factory=threading.Event)
    run_options: Any = None

    def cancel(self) -> None:
        self.cancelled.set()
        if self.run_options is not None:
            self.run_options.terminate = True


class _OnnxEncoder:
    def __init__(self, cache_dir: Path) -> None:
        # Only the enabled Workers capability imports native inference or reads model files.
        # This encoder is the application's only tokenizers consumer. Its parallelism switch is
        # process-wide, including single-text encode(), so disable Rayon for the remaining lifetime
        # rather than changing it temporarily while other Workers capabilities are running.
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        import onnxruntime as ort  # type: ignore[import-untyped]
        from tokenizers import Tokenizer

        validate_model_snapshot(cache_dir)
        snapshot = model_snapshot_dir(cache_dir)
        self.tokenizer = Tokenizer.from_file(str(snapshot / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=CALIBRATION.embedder.max_tokens)
        padding = self.tokenizer.padding
        if padding is None or self.tokenizer.token_to_id(str(padding["pad_token"])) != padding["pad_id"]:
            raise ValueError("news_embedding_tokenizer_invalid")
        self.tokenizer.enable_padding(
            direction="right",
            pad_id=padding["pad_id"],
            pad_type_id=padding["pad_type_id"],
            pad_token=padding["pad_token"],
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.session = ort.InferenceSession(
            str(snapshot / "onnx/model.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.ort = ort
        self.inputs = {item.name for item in self.session.get_inputs()}
        if not {"input_ids", "attention_mask"} <= self.inputs or self.inputs - {
            "input_ids",
            "attention_mask",
            "token_type_ids",
        }:
            raise ValueError("news_embedding_model_inputs_invalid")
        outputs = self.session.get_outputs()
        if len(outputs) != 1 or outputs[0].name != "last_hidden_state":
            raise ValueError("news_embedding_model_output_invalid")

    def encode(self, texts: Sequence[str], operation: _Operation) -> tuple[bytes, ...]:
        if operation.cancelled.is_set():
            raise TimeoutError("news_embedding_cancelled")
        if any(not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS for text in texts):
            raise ValueError("news_embedding_text_invalid")
        encoded = self.tokenizer.encode_batch(list(texts))
        values = {
            "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
            "attention_mask": np.asarray([item.attention_mask for item in encoded], dtype=np.int64),
            "token_type_ids": np.asarray([item.type_ids for item in encoded], dtype=np.int64),
        }
        operation.run_options = self.ort.RunOptions()
        if operation.cancelled.is_set():
            operation.run_options.terminate = True
        hidden = self.session.run(
            ["last_hidden_state"], {name: values[name] for name in self.inputs}, operation.run_options
        )[0]
        if hidden.shape != (*values["input_ids"].shape, CALIBRATION.embedder.dimensions) or hidden.dtype != np.float32:
            raise ValueError("news_embedding_model_shape_invalid")
        mask = values["attention_mask"].astype(np.float32)[..., None]
        vectors = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1.0)
        return tuple(vector_bytes(row, CALIBRATION.embedder) for row in vectors)


class ClaimEmbedder:
    def __init__(
        self,
        *,
        model: str,
        cache_dir: Path,
        on_status: Callable[[bool], None] | None = None,
        max_batch_size: int = 32,
    ) -> None:
        self.identity = CALIBRATION.embedder
        if model != self.identity.model:
            raise ValueError("news_embedding_calibration_identity_mismatch")
        if not 1 <= max_batch_size <= 32:
            raise ValueError("news_embedding_batch_size_invalid")
        self.model = model
        self.cache_dir = cache_dir
        self.max_batch_size = max_batch_size
        self.on_status = on_status
        self.ready = False
        self.unavailable_reason: str | None = "news_embedding_self_test_pending"
        self._tested = False
        self._closed = False
        self._encoder: _OnnxEncoder | None = None
        self._test_lock = asyncio.Lock()
        self._gate = asyncio.BoundedSemaphore(1)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tracefold-claim-embedding")
        self._pending: dict[Future[Any], _Operation] = {}

    def _report(self, available: bool, reason: str | None = None) -> None:
        self.unavailable_reason = None if available else reason or "news_embedding_unavailable"
        if self.on_status is not None:
            self.on_status(available)

    async def _run[T](self, function: Callable[[_Operation], T], *, budget_seconds: float) -> T:
        if self._closed:
            raise RuntimeError("news_embedding_closed")
        async with asyncio.timeout(ADMISSION_SECONDS):
            await self._gate.acquire()
        operation = _Operation()
        loop = asyncio.get_running_loop()
        try:
            if self._closed:
                raise RuntimeError("news_embedding_closed")
            underlying = self._executor.submit(function, operation)
        except BaseException:
            self._gate.release()
            raise
        self._pending[underlying] = operation

        def completed(_future: Future[Any]) -> None:
            def release() -> None:
                self._pending.pop(underlying, None)
                self._gate.release()

            with suppress(RuntimeError):
                loop.call_soon_threadsafe(release)

        underlying.add_done_callback(completed)
        wrapped = asyncio.wrap_future(underlying)
        # Retrieve failures even after the waiting coroutine has left; retain admission until physical completion.
        wrapped.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        try:
            async with asyncio.timeout(budget_seconds):
                return await asyncio.shield(wrapped)
        except BaseException:
            operation.cancel()
            underlying.cancel()
            raise

    def _load_and_test(self, operation: _Operation) -> bool:
        encoder = _OnnxEncoder(self.cache_dir)
        texts, expected = golden_vectors(self.identity)
        vectors = tuple(
            vector
            for start in range(0, len(texts), self.max_batch_size)
            for vector in encoder.encode(texts[start : start + self.max_batch_size], operation)
        )
        matrix = np.stack([np.frombuffer(vector, dtype="<f2").astype(np.float32) for vector in vectors])
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        if not (
            (np.einsum("ij,ij->i", matrix, expected) >= GOLDEN_MIN_COSINE).all()
            and (np.abs(matrix - expected).max(axis=1) <= GOLDEN_MAX_ABS_ERROR).all()
        ):
            raise ValueError("news_embedding_golden_mismatch")
        self._encoder = encoder
        return True

    async def self_test(self) -> bool:
        async with self._test_lock:
            if self._tested:
                return self.ready
            try:
                self.ready = await self._run(self._load_and_test, budget_seconds=STARTUP_SECONDS)
            except Exception as exc:
                reason = (
                    "news_embedding_model_missing"
                    if isinstance(exc, FileNotFoundError)
                    else "news_embedding_self_test_failed"
                )
                self._report(False, reason)
                log.warning("%s recall_degraded=true", reason)
            else:
                self._report(True)
            self._tested = True
            return self.ready

    async def probes(self, texts: Sequence[str]) -> tuple[Probe, ...]:
        if not texts:
            return ()
        if await self.self_test():
            try:
                vectors: list[bytes] = []
                for start in range(0, len(texts), self.max_batch_size):
                    batch = texts[start : start + self.max_batch_size]
                    encoder = self._encoder
                    if encoder is None:
                        raise RuntimeError("news_embedding_not_loaded")
                    vectors.extend(
                        await self._run(
                            partial(encoder.encode, batch),
                            budget_seconds=EMBEDDING_SECONDS,
                        )
                    )
                self._report(True)
                return tuple(
                    Probe(text, vector, self.identity.key) for text, vector in zip(texts, vectors, strict=True)
                )
            except Exception:
                log.warning("news_embedding_unavailable recall_degraded=true")
                self._report(False)
        return tuple(Probe(text) for text in texts)

    async def aclose(self) -> None:
        self._closed = True
        pending = tuple(self._pending)
        for future in pending:
            self._pending[future].cancel()
            future.cancel()
        try:
            if pending:
                wrapped = [asyncio.wrap_future(future) for future in pending]
                for waiting in wrapped:
                    waiting.add_done_callback(lambda future: None if future.cancelled() else future.exception())
                done, unfinished = await asyncio.wait(wrapped, timeout=CLOSE_SECONDS)
                for completed in done:
                    if not completed.cancelled():
                        completed.exception()
                if unfinished:
                    raise RuntimeError("news_embedding_drain_timeout")
            self._encoder = None
            self.ready = False
            self.unavailable_reason = "news_embedding_closed"
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
