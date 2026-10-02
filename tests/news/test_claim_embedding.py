"""Local model integrity, exact-vector compatibility and physical execution lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tracefold.app import claim_embedding as embedding
from tracefold.news.claim_recall import CALIBRATION, Probe, vector_bytes


def vector(x: float, y: float = 0) -> bytes:
    return vector_bytes([x, y, *([0.0] * (CALIBRATION.embedder.dimensions - 2))], CALIBRATION.embedder)


class Encoder:
    def __init__(self, _cache_dir: Path) -> None:
        texts, expected = embedding.golden_vectors(CALIBRATION.embedder)
        self.golden = dict(zip(texts, expected, strict=True))
        self.batches: list[tuple[str, ...]] = []
        self.fail = False

    def encode(self, texts, _operation):
        self.batches.append(tuple(texts))
        if self.fail and "fact-2" in texts:
            raise RuntimeError("injected inference failure")
        return tuple(
            vector_bytes(self.golden[text], CALIBRATION.embedder)
            if text in self.golden
            else vector(1, int(text[-1]) if text.startswith("fact-") else 0)
            for text in texts
        )


def route(monkeypatch, tmp_path: Path, encoder=Encoder, **kwargs) -> embedding.ClaimEmbedder:
    monkeypatch.setattr(embedding, "_OnnxEncoder", encoder)
    return embedding.ClaimEmbedder(model=CALIBRATION.embedder.model, cache_dir=tmp_path, **kwargs)


def test_local_route_is_lazy_batched_ordered_and_recovers_without_partial_vectors(monkeypatch, tmp_path):
    statuses = []
    model = route(monkeypatch, tmp_path, on_status=statuses.append, max_batch_size=2)
    assert model._encoder is None and not model._pending
    texts = [f"fact-{index}" for index in range(5)]

    async def run():
        probes = await model.probes(texts)
        assert [probe.text for probe in probes] == texts
        assert [probe.vector for probe in probes] == [vector(1, index) for index in range(5)]
        assert all(probe.embedder == CALIBRATION.embedder.key for probe in probes)
        encoder = model._encoder
        assert encoder.batches[-3:] == [tuple(texts[:2]), tuple(texts[2:4]), tuple(texts[4:])]
        encoder.fail = True
        assert await model.probes(texts) == tuple(Probe(text) for text in texts)
        encoder.fail = False
        assert all(probe.vector for probe in await model.probes(texts))
        await model.aclose()

    asyncio.run(run())
    assert statuses == [True, True, False, True]
    with pytest.raises(ValueError, match="calibration_identity_mismatch"):
        embedding.ClaimEmbedder(model="wrong", cache_dir=tmp_path)


def test_model_loading_and_golden_check_run_off_the_event_loop(monkeypatch, tmp_path):
    owner = threading.get_ident()
    threads = []

    class CheckedEncoder(Encoder):
        def __init__(self, cache):
            threads.append(threading.get_ident())
            super().__init__(cache)

        def encode(self, texts, operation):
            threads.append(threading.get_ident())
            return super().encode(texts, operation)

    model = route(monkeypatch, tmp_path, CheckedEncoder)

    async def run():
        assert await model.self_test()
        assert (await model.probes(["policy"]))[0].vector
        await model.aclose()

    asyncio.run(run())
    assert len(set(threads)) == 1 and threads[0] != owner


def test_golden_check_rejects_a_different_basis_and_never_retries_it(monkeypatch, tmp_path):
    loads = []

    class RotatedEncoder(Encoder):
        def __init__(self, cache):
            loads.append(cache)
            super().__init__(cache)

        def encode(self, texts, _operation):
            return tuple(vector_bytes(np.roll(self.golden[text], 1), CALIBRATION.embedder) for text in texts)

    statuses = []
    model = route(monkeypatch, tmp_path, RotatedEncoder, on_status=statuses.append)

    async def run():
        assert await model.probes(["policy"]) == (Probe("policy"),)
        assert await model.probes(["second"]) == (Probe("second"),)
        assert model.unavailable_reason == "news_embedding_self_test_failed"
        await model.aclose()

    asyncio.run(run())
    assert loads == [tmp_path] and statuses == [False]


@pytest.mark.parametrize("failure", [FileNotFoundError, ValueError, RuntimeError])
def test_missing_or_invalid_model_disables_only_dense_and_does_not_download(monkeypatch, tmp_path, failure):
    def broken(_cache):
        raise failure("injected unavailable model")

    statuses = []
    model = route(monkeypatch, tmp_path, broken, on_status=statuses.append)

    async def run():
        assert await model.probes(["policy"]) == (Probe("policy"),)
        assert await model.probes(["second"]) == (Probe("second"),)
        assert model.unavailable_reason == (
            "news_embedding_model_missing" if failure is FileNotFoundError else "news_embedding_self_test_failed"
        )
        await model.aclose()

    asyncio.run(run())
    assert statuses == [False]


def test_missing_golden_resource_disables_dense(monkeypatch, tmp_path):
    model = route(monkeypatch, tmp_path)

    def missing(_identity):
        raise FileNotFoundError("missing golden resource")

    monkeypatch.setattr(embedding, "golden_vectors", missing)

    async def run():
        assert await model.probes(["policy"]) == (Probe("policy"),)
        await model.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_or_cancellation_keeps_admission_until_native_completion(monkeypatch, tmp_path, cancel):
    entered, release = threading.Event(), threading.Event()
    operations = []

    class BlockingEncoder(Encoder):
        def encode(self, texts, operation):
            if list(texts) == ["blocked"]:
                operation.run_options = SimpleNamespace(terminate=False)
                operations.append(operation)
                entered.set()
                assert release.wait(2), "test did not release native operation"
            return super().encode(texts, operation)

    monkeypatch.setattr(embedding, "EMBEDDING_SECONDS", 0.03)
    monkeypatch.setattr(embedding, "ADMISSION_SECONDS", 0.01)
    model = route(monkeypatch, tmp_path, BlockingEncoder)

    async def run():
        assert await model.self_test()
        first = asyncio.create_task(model.probes(["blocked"]))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        if cancel:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            assert await first == (Probe("blocked"),)
        assert operations[0].cancelled.is_set()
        assert operations[0].run_options.terminate
        assert len(model._pending) == 1
        assert await model.probes(["second"]) == (Probe("second"),)
        assert len(model._pending) == 1, "busy requests must not accumulate in the executor queue"
        release.set()
        while model._pending:
            await asyncio.sleep(0.001)
        assert (await model.probes(["recovered"]))[0].vector
        await model.aclose()

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_close_waits_for_physical_completion_and_closes_admission(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()

    class BlockingEncoder(Encoder):
        def encode(self, texts, operation):
            if list(texts) == ["blocked"]:
                entered.set()
                assert release.wait(2)
                assert operation.cancelled.is_set()
            return super().encode(texts, operation)

    model = route(monkeypatch, tmp_path, BlockingEncoder)

    async def run():
        assert await model.self_test()
        first = asyncio.create_task(model.probes(["blocked"]))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        closing = asyncio.create_task(model.aclose())
        await asyncio.sleep(0.01)
        assert not closing.done()
        assert await model.probes(["closed"]) == (Probe("closed"),)
        release.set()
        await closing
        await first
        assert not model._pending

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_close_releases_the_loaded_session_only_after_successful_drain(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()

    class BlockingEncoder(Encoder):
        def encode(self, texts, operation):
            if list(texts) == ["blocked"]:
                entered.set()
                assert release.wait(2)
            return super().encode(texts, operation)

    monkeypatch.setattr(embedding, "CLOSE_SECONDS", 0.01)
    model = route(monkeypatch, tmp_path, BlockingEncoder)

    async def run():
        assert await model.self_test()
        session = weakref.ref(model._encoder)
        first = asyncio.create_task(model.probes(["blocked"]))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        with pytest.raises(RuntimeError, match="drain_timeout"):
            await model.aclose()
        assert session() is not None and model._encoder is session()
        release.set()
        await first
        while model._pending:
            await asyncio.sleep(0.001)
        await model.aclose()
        assert model._encoder is None
        # Future completion may wake this coroutine just before the executor drops its finished work item.
        async with asyncio.timeout(1):
            while session() is not None:
                await asyncio.sleep(0.001)
        assert not model.ready

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_prepare_downloads_only_fixed_artifacts_atomically_and_repairs_damage(monkeypatch, tmp_path):
    calls = []

    def download(model, *, revision, allow_patterns, local_dir):
        calls.append((model, revision, allow_patterns))
        (local_dir / "onnx").mkdir()
        (local_dir / "onnx/model.onnx").write_bytes(b"fixed model")
        (local_dir / "tokenizer.json").write_text("{}")

    monkeypatch.setattr("huggingface_hub.snapshot_download", download)
    monkeypatch.setattr(embedding, "_OnnxEncoder", lambda _cache: pytest.fail("prepare loaded the model"))
    first = embedding.prepare_model(tmp_path)
    assert first["revision"] == CALIBRATION.embedder.revision
    assert len(calls) == 1 and calls[0][2] == list(embedding.MODEL_FILES)
    assert embedding.prepare_model(tmp_path) == first and len(calls) == 1
    snapshot = embedding.model_snapshot_dir(tmp_path)
    (snapshot / "onnx/model.onnx").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="snapshot_mismatch"):
        embedding.validate_model_snapshot(tmp_path)
    assert embedding.prepare_model(tmp_path) == first and len(calls) == 2
    assert not list(tmp_path.glob(".news-embedding-*"))


def test_failed_download_never_publishes_an_incomplete_snapshot(monkeypatch, tmp_path):
    def download(_model, **kwargs):
        (kwargs["local_dir"] / "tokenizer.json").write_text("{}")
        raise OSError("interrupted download")

    monkeypatch.setattr("huggingface_hub.snapshot_download", download)
    with pytest.raises(OSError, match="interrupted"):
        embedding.prepare_model(tmp_path)
    assert not embedding.model_snapshot_dir(tmp_path).exists()


def test_onnx_encoder_applies_fixed_token_cap_masked_mean_and_cpu_thread_budget(monkeypatch, tmp_path):
    snapshot = embedding.model_snapshot_dir(tmp_path)
    (snapshot / "onnx").mkdir(parents=True)
    (snapshot / "tokenizer.json").write_text("{}")
    (snapshot / "onnx/model.onnx").write_bytes(b"fake graph")
    (snapshot / embedding.MODEL_MANIFEST).write_text(json.dumps(embedding._snapshot_manifest(snapshot)))
    settings = {}

    class Tokenizer:
        def __init__(self):
            self.padding = {"pad_id": 1, "pad_type_id": 0, "pad_token": "<pad>"}

        @classmethod
        def from_file(cls, path):
            assert path == str(snapshot / "tokenizer.json")
            return cls()

        def enable_truncation(self, **kwargs):
            settings["truncation"] = kwargs

        def token_to_id(self, token):
            assert token == "<pad>"
            return 1

        def enable_padding(self, **kwargs):
            settings["padding"] = kwargs

        def encode_batch(self, _texts):
            assert os.environ["TOKENIZERS_PARALLELISM"] == "false"
            return [
                SimpleNamespace(ids=[1, 2, 0], attention_mask=[1, 1, 0], type_ids=[0, 0, 0]),
                SimpleNamespace(ids=[1, 2, 3], attention_mask=[1, 1, 1], type_ids=[0, 0, 0]),
            ]

    class Options:
        def add_session_config_entry(self, key, value):
            settings[key] = value

    class Session:
        def __init__(self, _path, *, sess_options, providers):
            settings["options"] = sess_options
            assert providers == ["CPUExecutionProvider"]

        def get_inputs(self):
            return [SimpleNamespace(name=name) for name in ("input_ids", "attention_mask", "token_type_ids")]

        def get_outputs(self):
            return [SimpleNamespace(name="last_hidden_state")]

        def run(self, outputs, values, _run_options):
            assert outputs == ["last_hidden_state"] and values["input_ids"].shape == (2, 3)
            assert all(value.dtype == np.int64 for value in values.values())
            hidden = np.zeros((2, 3, CALIBRATION.embedder.dimensions), dtype=np.float32)
            hidden[0, :, 0] = [1, 1, 10000]
            hidden[1, :, 1] = 1
            return [hidden]

    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=Tokenizer))
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            SessionOptions=Options,
            InferenceSession=Session,
            RunOptions=SimpleNamespace,
            ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        ),
    )
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "true")
    encoder = embedding._OnnxEncoder(tmp_path)
    assert encoder.encode(["first", "second"], embedding._Operation()) == (vector(1), vector(0, 1))
    assert settings["truncation"] == {"max_length": 256}
    assert settings["padding"] == {"direction": "right", "pad_id": 1, "pad_type_id": 0, "pad_token": "<pad>"}
    assert settings["options"].intra_op_num_threads == 2
    assert settings["options"].inter_op_num_threads == 1
    assert settings["session.intra_op.allow_spinning"] == "0"


def test_real_tokenizer_caps_a_full_batch_of_long_statements_and_preserves_order(monkeypatch, tmp_path):
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    snapshot = embedding.model_snapshot_dir(tmp_path)
    (snapshot / "onnx").mkdir(parents=True)
    # The fixed multilingual model uses XLM-R special tokens, not BERT's [PAD]/[CLS]/[SEP].
    vocabulary = {"<s>": 0, "<pad>": 1, "</s>": 2, "<unk>": 3, "word": 4}
    vocabulary.update({f"fact{index}": index + 5 for index in range(32)})
    tokenizer = Tokenizer(models.WordLevel(vocabulary, unk_token="<unk>"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="<s> $A </s>", special_tokens=[("<s>", 0), ("</s>", 2)]
    )
    tokenizer.enable_padding(direction="right", pad_id=1, pad_type_id=0, pad_token="<pad>")
    tokenizer.save(str(snapshot / "tokenizer.json"))
    (snapshot / "onnx/model.onnx").write_bytes(b"fake graph")
    (snapshot / embedding.MODEL_MANIFEST).write_text(json.dumps(embedding._snapshot_manifest(snapshot)))

    class Options:
        def add_session_config_entry(self, _key, _value):
            pass

    class Session:
        def __init__(self, _path, **_kwargs):
            pass

        def get_inputs(self):
            return [SimpleNamespace(name=name) for name in ("input_ids", "attention_mask", "token_type_ids")]

        def get_outputs(self):
            return [SimpleNamespace(name="last_hidden_state")]

        def run(self, _outputs, values, _operation):
            assert values["input_ids"].shape == (32, 256)
            assert (values["input_ids"][:, 0] == 0).all() and (values["input_ids"][:, -1] == 2).all()
            assert values["attention_mask"].sum() == 32 * 256
            hidden = np.zeros((32, 256, CALIBRATION.embedder.dimensions), dtype=np.float32)
            hidden[:, :, 0] = values["input_ids"][:, 1, None]
            hidden[:, :, 1] = 1
            return [hidden]

    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            SessionOptions=Options,
            InferenceSession=Session,
            RunOptions=SimpleNamespace,
            ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        ),
    )
    encoder = embedding._OnnxEncoder(tmp_path)
    texts = [f"fact{index} " + "word " * (512 + index) for index in range(32)]
    assert encoder.encode(texts, embedding._Operation()) == tuple(vector(index + 5, 1) for index in range(32))
    with pytest.raises(ValueError, match="text_invalid"):
        encoder.encode(["x" * (embedding.MAX_TEXT_CHARS + 1)], embedding._Operation())
