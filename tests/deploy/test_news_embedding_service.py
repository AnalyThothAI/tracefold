"""Real HTTP protocol and bounded capacity without loading model weights in app tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest
import yaml

from scripts.deploy import MUTATIONS, Deployment, DeploymentError, deployment_lock
from services.news_embedding.server import (
    GPU_ALLOCATOR_BYTES,
    MAX_BATCH_SIZE,
    EmbeddingServer,
    ModelIdentity,
    Runtime,
    SentenceEncoder,
)
from tracefold.news.claim_recall import CALIBRATION

ROOT = Path(__file__).resolve().parents[2]
KEY = "test-only-private-key-value"
pytestmark = pytest.mark.deploy


class OrderedEncoder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.seen.extend(texts)
        vectors = []
        for text in texts:
            vector = [0.0] * CALIBRATION.embedder.dimensions
            vector[0 if text.startswith("first") else 1] = 1.0
            vectors.append(vector)
        return vectors, 12

    def provenance(self) -> dict[str, Any]:
        return {"device": "test", "include_prompt": True, "packages": {}}


@contextmanager
def serving(
    encoder: OrderedEncoder | None = None,
    *,
    handlers_in_use: int = 0,
) -> Iterator[tuple[Runtime, httpx.Client]]:
    identity = ModelIdentity.load(ROOT / "tracefold/news/claim_recall_calibration.json")
    runtime = Runtime(identity, encoder or OrderedEncoder(), KEY)
    with EmbeddingServer(("127.0.0.1", 0), runtime) as server:
        for _ in range(handlers_in_use):
            assert server.handlers.acquire(blocking=False)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{server.server_port}", timeout=2.0, trust_env=False
            ) as client:
                yield runtime, client
        finally:
            for _ in range(handlers_in_use):
                server.handlers.release()
            server.shutdown()
            thread.join(timeout=2)


def request(client: httpx.Client, **fields: Any) -> httpx.Response:
    return client.post(
        "/v1/embeddings",
        headers={"Authorization": "Bearer " + KEY},
        json={"model": CALIBRATION.embedder.model, "input": ["first statement", "second statement"], **fields},
    )


def test_protocol_returns_ordered_exact_dimension_vectors_and_secretless_provenance() -> None:
    with serving() as (runtime, client):
        response = request(client)
        assert response.status_code == 200
        body = response.json()
        assert body["model"] == CALIBRATION.embedder.model
        assert body["embedder_identity"] == CALIBRATION.embedder.key
        assert [row["index"] for row in body["data"]] == [0, 1]
        assert body["data"][0]["embedding"][0] == body["data"][1]["embedding"][1] == 1.0
        assert all(len(row["embedding"]) == CALIBRATION.embedder.dimensions for row in body["data"])
        assert body["usage"] == {"prompt_tokens": 12, "total_tokens": 12}
        assert runtime.encoder.seen == ["first statement", "second statement"]
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["embedder_identity"] == CALIBRATION.embedder.key
        assert ready.json()["calibration_sha"] == CALIBRATION.digest
        assert ready.json()["query_instruction"] is None
        assert ready.json()["max_batch_size"] == MAX_BATCH_SIZE == 2
        assert KEY not in ready.text
        assert "first statement" not in ready.text


@pytest.mark.parametrize("authorization", ["", "Bearer wrong-key", "Basic bad"])
def test_authentication_refuses_request_before_inference(authorization: str) -> None:
    with serving() as (runtime, client):
        response = client.post(
            "/v1/embeddings",
            headers={"Authorization": authorization},
            json={"model": CALIBRATION.embedder.model, "input": "first statement"},
        )
        assert response.status_code == 401
        assert runtime.encoder.seen == []
        assert KEY not in response.text


@pytest.mark.parametrize(
    "fields",
    [
        {"model": "wrong-model"},
        {"input": []},
        {"input": ["one", "two", "three"]},
        {"input": [123]},
        {"input": " "},
        {"input": [[1, 2]]},
        {"input": {"statement": "one"}},
        {"input": "x" * 32_769},
        {"encoding_format": "base64"},
        {"dimensions": 1},
    ],
)
def test_invalid_request_is_400_and_does_not_reach_model(fields: dict[str, Any]) -> None:
    with serving() as (runtime, client):
        assert request(client, **fields).status_code == 400
        assert runtime.encoder.seen == []


def test_http_rejects_bad_json_and_large_body_and_has_no_old_route_alias() -> None:
    with serving() as (runtime, client):
        headers = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
        assert client.post("/v1/embeddings", headers=headers, content="{").status_code == 400
        assert client.post("/v1/embeddings", headers=headers, content="x" * 131_073).status_code == 400
        assert client.post("/embeddings", headers=headers, content="{}").status_code == 404
        assert runtime.encoder.seen == []


def test_busy_request_returns_503_with_bounded_wait_and_next_request_recovers() -> None:
    class BlockingEncoder(OrderedEncoder):
        started = threading.Event()
        release = threading.Event()

        def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
            self.started.set()
            assert self.release.wait(timeout=2)
            return super().encode(texts)

    encoder = BlockingEncoder()
    with serving(encoder) as (runtime, client):
        responses: list[httpx.Response] = []
        first = threading.Thread(target=lambda: responses.append(request(client)), daemon=True)
        first.start()
        assert encoder.started.wait(timeout=1)
        started = time.monotonic()
        second = request(client)
        assert second.status_code == 503
        assert second.json()["error"]["code"] == "embedding_busy"
        assert time.monotonic() - started < 1.5
        assert client.get("/readyz").status_code == 200
        encoder.release.set()
        first.join(timeout=2)
        assert [response.status_code for response in responses] == [200]
        assert request(client).status_code == 200
        assert not runtime.gate.locked()


def test_short_background_batch_can_finish_within_bounded_admission_wait() -> None:
    class ShortBatchEncoder(OrderedEncoder):
        started = threading.Event()
        release = threading.Event()

        def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
            self.started.set()
            assert self.release.wait(timeout=1)
            return super().encode(texts)

    encoder = ShortBatchEncoder()
    with serving(encoder) as (_runtime, client):
        first_results: list[httpx.Response] = []
        first = threading.Thread(target=lambda: first_results.append(request(client)), daemon=True)
        first.start()
        assert encoder.started.wait(timeout=1)
        timer = threading.Timer(0.05, encoder.release.set)
        timer.start()
        try:
            assert request(client).status_code == 200
            first.join(timeout=2)
            assert [response.status_code for response in first_results] == [200]
        finally:
            encoder.release.set()
            timer.join(timeout=1)


@pytest.mark.parametrize("bad_vector", [[0.0], [float("nan")], [0.0] * CALIBRATION.embedder.dimensions])
def test_invalid_model_output_is_unavailable_and_releases_capacity(bad_vector: list[float]) -> None:
    class BrokenEncoder(OrderedEncoder):
        def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
            return [bad_vector] * len(texts), 1

    with serving(BrokenEncoder()) as (runtime, client):
        assert request(client).status_code == 503
        assert not runtime.gate.locked()
        runtime.encoder = OrderedEncoder()
        assert request(client).status_code == 200


def test_optional_compose_runtime_owns_private_key_cache_and_gpu_budget() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    service = compose["services"]["news-embedding"]
    assert service["profiles"] == ["news-embedding"]
    assert service["build"]["dockerfile"] == "services/news_embedding/Dockerfile"
    assert service["build"] != compose["x-tracefold-app"]["build"]
    assert service["read_only"]
    assert service["environment"]["HF_HUB_OFFLINE"] == "1"
    assert service["environment"]["EMBEDDING_MAX_BATCH_SIZE"] == "2"
    assert any("embedding-cache" in mount and mount.endswith(":/weights:ro") for mount in service["volumes"])
    assert any("news_embedding_api_key" in mount and mount.endswith(":ro") for mount in service["volumes"])
    assert "depends_on" not in service
    cuda = yaml.safe_load((ROOT / "services/news_embedding/compose.cuda.yaml").read_text())
    device = cuda["services"]["news-embedding"]["deploy"]["resources"]["reservations"]["devices"][0]
    assert device["count"] == "all"
    assert cuda["services"]["news-embedding"]["environment"]["CUDA_VISIBLE_DEVICES"] == "1"
    assert cuda["services"]["news-embedding"]["build"]["args"]["TORCH_VERSION"] == "2.11.0+cu128"


def test_identity_rejects_unpinned_revision_and_unsupported_projection(tmp_path: Path) -> None:
    data = json.loads((ROOT / "tracefold/news/claim_recall_calibration.json").read_text())
    data["embedder"]["revision"] = "main"
    target = tmp_path / "calibration.json"
    target.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="embedding_calibration_invalid"):
        ModelIdentity.load(target)


@pytest.mark.parametrize("runtime_name", ["cpu", "cuda"])
def test_real_compose_render_selects_only_the_explicit_embedding_runtime(tmp_path: Path, runtime_name: str) -> None:
    shutil.copy(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    override = tmp_path / "services/news_embedding"
    override.mkdir(parents=True)
    shutil.copy(ROOT / "services/news_embedding/compose.cuda.yaml", override)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "TRACEFOLD_"))}
    environment.update(
        HOME=str(tmp_path),
        TRACEFOLD_HOME=str(tmp_path / "operator"),
        COMPOSE_PROJECT_NAME="tracefold-embedding-render-test",
        TRACEFOLD_NEWS_EMBEDDING_RUNTIME=runtime_name,
    )
    deployment = Deployment(tmp_path, environ=environment)
    service = deployment.embedding_model["services"]["news-embedding"]
    assert service["environment"]["EMBEDDING_DEVICE"] == ("cuda:0" if runtime_name == "cuda" else "cpu")
    assert service["image"] == "tracefold-embedding-render-test-news-embedding:local"
    assert deployment.embedding_mount("/weights") == tmp_path / "operator/embedding-cache"
    assert (
        deployment.embedding_mount("/run/secrets/news_embedding_api_key")
        == tmp_path / "operator/news_embedding_api_key"
    )
    assert not (tmp_path / "operator").exists()
    if runtime_name == "cuda":
        # Compose's normalized JSON uses -1 for the YAML 'all' reservation.
        assert service["deploy"]["resources"]["reservations"]["devices"][0]["count"] == -1
        assert service["environment"]["CUDA_VISIBLE_DEVICES"] == "1"


@pytest.mark.parametrize("action", ["embedding-build", "embedding-download", "embedding-up", "embedding-down"])
def test_embedding_mutations_share_the_application_project_lock(tmp_path: Path, action: str) -> None:
    assert action in MUTATIONS
    deployment = Deployment(tmp_path, environ={"HOME": str(tmp_path)})
    deployment._model = {"name": "tracefold-lock-test"}
    with (
        deployment_lock(tmp_path / ".cache/tracefold/deploy", "tracefold-lock-test"),
        pytest.raises(DeploymentError, match="already in progress"),
    ):
        deployment.execute(action)


def test_download_and_start_have_separate_cache_access_and_do_not_touch_application_roles(tmp_path: Path) -> None:
    class PlanDeployment(Deployment):
        def __init__(self) -> None:
            super().__init__(tmp_path, environ={})
            self.calls: list[tuple[str, ...]] = []
            self._embedding_model = {
                "services": {
                    "news-embedding": {
                        "user": "1000:1000",
                        "volumes": [
                            {"target": "/weights", "source": str(tmp_path / "cache")},
                            {"target": "/run/secrets/news_embedding_api_key", "source": str(tmp_path / "key")},
                        ],
                    }
                }
            }

        def embedding_build(self) -> str:
            image = "sha256:" + "a" * 64
            self.env["TRACEFOLD_NEWS_EMBEDDING_IMAGE"] = image
            return image

        def run(self, *args: str, **_kwargs: Any) -> str:
            self.calls.append(args)
            return ""

        def embedding_compose(self, *args: str, **_kwargs: Any) -> str:
            self.calls.append(args)
            return ""

        def embedding_status(self) -> None:
            pass

    deployment = PlanDeployment()
    deployment.embedding_download()
    download = deployment.calls.pop()
    assert download[:3] == ("docker", "run", "--rm")
    assert f"type=bind,source={tmp_path / 'cache'},target=/weights" in download
    assert download[-2:] == ("/service/server.py", "download")
    assert "--gpus" not in download
    assert "news_embedding_api_key" not in " ".join(download)
    with pytest.raises(DeploymentError, match="private key"):
        deployment.embedding_up()
    assert deployment.calls == []
    (tmp_path / "key").write_text(KEY)
    deployment.embedding_up()
    assert len(deployment.calls) == 1
    start = deployment.calls[0]
    assert start[0] == "up" and start[-1] == "news-embedding"
    assert "--no-build" in start and "--no-deps" in start and "--wait" in start
    assert not any(role in start for role in ("postgres", "migrate", "workers", "executor"))


def test_model_loader_preserves_calibrated_wrapper_and_cuda_allocator_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = ModelIdentity.load(ROOT / "tracefold/news/claim_recall_calibration.json")
    observed: dict[str, Any] = {}

    class FakePooling:
        include_prompt = True

        def get_config_dict(self) -> dict[str, Any]:
            return {
                "embedding_dimension": identity.dimensions,
                "pooling_mode": identity.pooling,
                "include_prompt": self.include_prompt,
            }

    class FakeModel:
        def __init__(self, model: str, **kwargs: Any) -> None:
            observed["load"] = {"model": model, **kwargs}

        def eval(self) -> None:
            pass

        def modules(self) -> list[FakePooling]:
            return [FakePooling()]

        def get_sentence_embedding_dimension(self) -> int:
            return identity.dimensions

        def parameters(self) -> list[Any]:
            return [SimpleNamespace(dtype=identity.dtype)]

        def tokenize(self, texts: list[str]) -> dict[str, Any]:
            observed["tokenized"] = texts
            return {"attention_mask": SimpleNamespace(sum=lambda: SimpleNamespace(item=lambda: 6))}

        def encode(self, texts: list[str], **kwargs: Any) -> Any:
            observed["encoded"] = (texts, kwargs)
            vector = [0.0] * identity.dimensions
            vector[0] = 1.0
            return np.asarray([vector] * len(texts), dtype=np.float32)

    torch = ModuleType("torch")
    for dtype in ("float32", "float16", "bfloat16"):
        setattr(torch, dtype, dtype)
    torch.set_num_threads = lambda threads: observed.update(threads=threads)
    torch.set_num_interop_threads = lambda threads: observed.update(interop=threads)
    torch.inference_mode = nullcontext
    torch.cuda = SimpleNamespace(
        get_device_properties=lambda _device: SimpleNamespace(total_memory=8_000_000_000),
        set_per_process_memory_fraction=lambda fraction, device: observed.update(fraction=fraction, device=device),
        reset_peak_memory_stats=lambda _device: None,
    )
    st = ModuleType("sentence_transformers")
    st.SentenceTransformer = FakeModel
    models = ModuleType("sentence_transformers.sentence_transformer.modules")
    models.Pooling = FakePooling
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    monkeypatch.setitem(sys.modules, "sentence_transformers.sentence_transformer.modules", models)
    encoder = SentenceEncoder(identity, cache=tmp_path, device="cuda:0", max_batch=2, threads=4)
    assert observed["load"] == {
        "model": identity.model,
        "revision": identity.revision,
        "cache_folder": str(tmp_path),
        "local_files_only": True,
        "device": "cuda:0",
        "trust_remote_code": False,
        "model_kwargs": {"torch_dtype": identity.dtype},
    }
    assert encoder.model.max_seq_length == identity.max_tokens
    assert observed["fraction"] * 8_000_000_000 == GPU_ALLOCATOR_BYTES == 1_800_000_000
    assert observed["threads"] == 4 and observed["interop"] == 1
    statements = ["央行下调利率。", "A central bank cuts interest rates."]
    vectors, tokens = encoder.encode(statements)
    assert len(vectors) == 2 and tokens == 6
    assert observed["tokenized"] == statements
    assert observed["encoded"] == (
        statements,
        {
            "batch_size": 2,
            "prompt": "",
            "show_progress_bar": False,
            "normalize_embeddings": True,
            "convert_to_numpy": True,
            "precision": "float32",
        },
    )
    monkeypatch.setattr(FakePooling, "include_prompt", False)
    with pytest.raises(ValueError, match="pooling_identity_mismatch"):
        SentenceEncoder(identity, cache=tmp_path, device="cpu", max_batch=2, threads=4)


def test_handler_capacity_is_bounded_before_a_request_can_enter_inference() -> None:
    # Simulate eight slow clients occupying the real server's handler admission slots.
    with serving(handlers_in_use=8) as (runtime, client):
        assert request(client).status_code == 503
        assert runtime.encoder.seen == []
        assert not runtime.gate.locked()
