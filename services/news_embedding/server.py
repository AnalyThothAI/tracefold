"""Bounded OpenAI embedding protocol over an explicitly cached, immutable model."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.metadata
import json
import math
import os
import re
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

MAX_BATCH_SIZE = 2
MAX_BODY_BYTES = 131_072
MAX_TEXT_CHARS = 32_768
GPU_ALLOCATOR_BYTES = 1_800_000_000
ADMISSION_WAIT_SECONDS = 0.5
DEPENDENCIES = ("torch", "sentence-transformers", "transformers", "huggingface-hub", "safetensors", "numpy")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelIdentity:
    model: str
    dimensions: int
    revision: str
    max_tokens: int
    pooling: str
    dtype: str
    normalization: str
    template: str
    calibration_sha: str

    @classmethod
    def load(cls, path: Path) -> ModelIdentity:
        calibration = json.loads(path.read_text(encoding="utf-8"))
        result = cls(**calibration["embedder"], calibration_sha=_digest(calibration))
        if (
            not re.fullmatch(r"[0-9a-f]{40}", result.revision)
            or result.dimensions < 1
            or not 1 <= result.max_tokens <= 8192
            or result.pooling not in {"mean", "cls", "lasttoken"}
            or result.dtype not in {"float32", "float16", "bfloat16"}
            or result.normalization != "l2"
            or result.template != "claim_embed_text_v1"
        ):
            raise ValueError("embedding_calibration_invalid")
        return result

    @property
    def key(self) -> str:
        return _digest(
            [
                self.model,
                self.dimensions,
                self.revision,
                self.max_tokens,
                self.pooling,
                self.dtype,
                self.normalization,
                self.template,
            ]
        )

    def provenance(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "revision": self.revision,
            "dimensions": self.dimensions,
            "max_tokens": self.max_tokens,
            "pooling": self.pooling,
            "dtype": self.dtype,
            "normalization": self.normalization,
            "template": self.template,
            "embedder_identity": self.key,
            "calibration_sha": self.calibration_sha,
            "dense_text": "statement",
            "query_instruction": None,
            "trust_remote_code": False,
        }


class Encoder(Protocol):
    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]: ...

    def provenance(self) -> dict[str, Any]: ...


class SentenceEncoder:
    def __init__(self, identity: ModelIdentity, *, cache: Path, device: str, max_batch: int, threads: int) -> None:
        # These dependencies belong only to this service image, never the Tracefold image.
        import torch
        from sentence_transformers import SentenceTransformer
        from sentence_transformers.models import Pooling

        self.torch = torch
        self.device = device
        self.max_batch = max_batch
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        if device.startswith("cuda"):
            total = torch.cuda.get_device_properties(device).total_memory
            torch.cuda.set_per_process_memory_fraction(min(1.0, GPU_ALLOCATOR_BYTES / total), device)
            torch.cuda.reset_peak_memory_stats(device)
        self.model = SentenceTransformer(
            identity.model,
            revision=identity.revision,
            cache_folder=str(cache),
            local_files_only=True,
            device=device,
            trust_remote_code=False,
            model_kwargs={"torch_dtype": getattr(torch, identity.dtype)},
        )
        self.model.max_seq_length = identity.max_tokens
        self.model.eval()
        poolings = [module for module in self.model.modules() if isinstance(module, Pooling)]
        flags = {
            "cls": "pooling_mode_cls_token",
            "mean": "pooling_mode_mean_tokens",
            "lasttoken": "pooling_mode_lasttoken",
            "max": "pooling_mode_max_tokens",
            "weightedmean": "pooling_mode_weightedmean_tokens",
            "mean_sqrt_len": "pooling_mode_mean_sqrt_len_tokens",
        }
        active = [name for name, flag in flags.items() if any(getattr(pool, flag, False) for pool in poolings)]
        if len(poolings) != 1 or active != [identity.pooling] or not poolings[0].include_prompt:
            raise ValueError("embedding_pooling_identity_mismatch")
        if self.model.get_sentence_embedding_dimension() != identity.dimensions:
            raise ValueError("embedding_dimension_identity_mismatch")
        if any(parameter.dtype != getattr(torch, identity.dtype) for parameter in self.model.parameters()):
            raise ValueError("embedding_dtype_identity_mismatch")

    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
        import numpy as np

        with self.torch.inference_mode():
            tokens = int(self.model.tokenize(texts)["attention_mask"].sum().item())
            vectors = self.model.encode(
                texts,
                batch_size=self.max_batch,
                prompt="",
                show_progress_bar=False,
                normalize_embeddings=True,
                convert_to_numpy=True,
                precision="float32",
            )
        # BF16 inference can round the normalization itself; the protocol emits final FP32 L2 vectors.
        vectors = vectors.astype(np.float32, copy=False)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or (norms <= 0).any():
            raise ValueError("embedding_output_invalid")
        vectors = vectors / norms
        return vectors.tolist(), tokens

    def provenance(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "device": self.device,
            "include_prompt": True,
            "output_dtype": "float32",
            "packages": {name: importlib.metadata.version(name) for name in DEPENDENCIES},
        }
        if self.device.startswith("cuda"):
            result.update(
                allocator_limit_bytes=GPU_ALLOCATOR_BYTES,
                allocator_peak_reserved_bytes=self.torch.cuda.max_memory_reserved(self.device),
            )
        return result


@dataclass
class Runtime:
    identity: ModelIdentity
    encoder: Encoder
    api_key: str = field(repr=False)
    max_batch: int = MAX_BATCH_SIZE
    gate: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not 1 <= self.max_batch <= MAX_BATCH_SIZE or not _valid_key(self.api_key):
            raise ValueError("embedding_service_configuration_invalid")

    def ready(self) -> dict[str, Any]:
        return {
            "ok": True,
            "ready": True,
            **self.identity.provenance(),
            **self.encoder.provenance(),
            "max_batch_size": self.max_batch,
            "max_inflight": 1,
            "admission_wait_seconds": ADMISSION_WAIT_SECONDS,
            "runtime_revision": os.environ.get("TRACEFOLD_BUILD_REVISION", "unversioned"),
        }


class EmbeddingServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], runtime: Runtime) -> None:
        self.runtime = runtime
        self.handlers = threading.BoundedSemaphore(8)
        super().__init__(address, EmbeddingHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = super().get_request()
        connection.settimeout(2.0)
        return connection, address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self.handlers.acquire(blocking=False):
            with suppress(OSError):
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.handlers.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.handlers.release()


class EmbeddingHandler(BaseHTTPRequestHandler):
    server: EmbeddingServer

    def log_message(self, _format: str, *args: Any) -> None:
        # Request paths, headers and statements may contain private data.
        pass

    def _send(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        if status == 503:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def _error(self, status: int, code: str) -> None:
        self._send(status, {"error": {"message": code, "type": "embedding_error", "code": code}})

    def do_GET(self) -> None:
        if self.path == "/readyz":
            self._send(200, self.server.runtime.ready())
        else:
            self._error(404, "embedding_route_not_found")

    def do_POST(self) -> None:
        runtime = self.server.runtime
        if not hmac.compare_digest(
            self.headers.get("Authorization", "").encode(),
            ("Bearer " + runtime.api_key).encode(),
        ):
            self._error(401, "embedding_unauthorized")
            return
        if self.path != "/v1/embeddings":
            self._error(404, "embedding_route_not_found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_BODY_BYTES or self.headers.get("Transfer-Encoding"):
                raise ValueError
            if self.headers.get_content_type() != "application/json":
                raise ValueError
            payload = self.rfile.read(length)
            if len(payload) != length:
                raise ValueError
            data = json.loads(payload)
            if not isinstance(data, dict) or set(data) - {"model", "input", "encoding_format"}:
                raise ValueError
            if data.get("model") != runtime.identity.model or data.get("encoding_format", "float") != "float":
                raise ValueError
            texts = data.get("input")
            texts = [texts] if isinstance(texts, str) else texts
            if not isinstance(texts, list) or not 1 <= len(texts) <= runtime.max_batch:
                raise ValueError
            if any(not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS for text in texts):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._error(400, "embedding_request_invalid")
            return
        except TimeoutError:
            self._error(408, "embedding_request_timeout")
            return
        if not runtime.gate.acquire(timeout=ADMISSION_WAIT_SECONDS):
            self._error(503, "embedding_busy")
            return
        try:
            vectors, token_count = runtime.encoder.encode(texts)
            if len(vectors) != len(texts) or any(
                len(vector) != runtime.identity.dimensions
                or any(not math.isfinite(value) for value in vector)
                or not math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, abs_tol=0.001)
                for vector in vectors
            ):
                raise ValueError("embedding_output_invalid")
            self._send(
                200,
                {
                    "object": "list",
                    "model": runtime.identity.model,
                    "data": [
                        {"object": "embedding", "index": index, "embedding": vector}
                        for index, vector in enumerate(vectors)
                    ],
                    "usage": {"prompt_tokens": token_count, "total_tokens": token_count},
                },
            )
        except (ValueError, RuntimeError):
            self._error(503, "embedding_inference_unavailable")
        finally:
            runtime.gate.release()


def _valid_key(key: str) -> bool:
    return 16 <= len(key) <= 4096 and not any(character.isspace() for character in key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "serve"))
    parser.add_argument("--calibration", type=Path, default=Path("/service/calibration.json"))
    parser.add_argument("--cache", type=Path, default=Path("/weights"))
    args = parser.parse_args()
    identity = ModelIdentity.load(args.calibration)
    if args.action == "download":
        from huggingface_hub import snapshot_download

        snapshot_download(
            identity.model,
            revision=identity.revision,
            cache_dir=str(args.cache),
            allow_patterns=["*.json", "*.txt", "*.model", "*.safetensors", "pytorch_model.bin"],
        )
        print(json.dumps({"downloaded": True, **identity.provenance()}))
        return
    api_key = Path(os.environ.get("EMBEDDING_API_KEY_FILE", "/run/secrets/news_embedding_api_key")).read_text().strip()
    if not _valid_key(api_key):
        raise ValueError("embedding_private_key_invalid")
    max_batch = int(os.environ.get("EMBEDDING_MAX_BATCH_SIZE", str(MAX_BATCH_SIZE)))
    threads = int(os.environ.get("EMBEDDING_CPU_THREADS", "4"))
    if not 1 <= max_batch <= MAX_BATCH_SIZE or not 1 <= threads <= 8:
        raise ValueError("embedding_service_resource_configuration_invalid")
    encoder = SentenceEncoder(
        identity,
        cache=args.cache,
        device=os.environ.get("EMBEDDING_DEVICE", "cpu"),
        max_batch=max_batch,
        threads=threads,
    )
    runtime = Runtime(identity, encoder, api_key, max_batch)
    with EmbeddingServer(("0.0.0.0", 8080), runtime) as server:  # noqa: S104 -- isolated Compose service bind
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
