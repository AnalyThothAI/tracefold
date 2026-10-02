"""The operator command separates explicit downloads from offline, version-bound work."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from tracefold.app.claim_embedding import ClaimEmbedder
from tracefold.cli import main
from tracefold.news.claim_recall import CALIBRATION


@pytest.fixture
def operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TRACEFOLD_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"llm": {"news_embedding": {"model": CALIBRATION.embedder.model}}})
    )
    return tmp_path


def test_prepare_is_explicit_and_does_not_load_the_model_or_open_a_database(operator, monkeypatch) -> None:
    calls = []

    def prepare(path):
        calls.append(path)
        return {"prepared": True}

    def forbidden(*args, **kwargs):
        raise AssertionError("model/database work during download")

    monkeypatch.setattr("tracefold.app.claim_embedding.prepare_model", prepare)
    monkeypatch.setattr(ClaimEmbedder, "__init__", forbidden)
    monkeypatch.setattr("tracefold.app.repository_session.repositories", forbidden)
    output = io.StringIO()
    assert main(["news", "embedding", "prepare"], stdout=output) == 0
    assert calls == [operator / "cache/news-embedding"]
    assert json.loads(output.getvalue())["data"]["prepared"] is True


def test_offline_check_of_missing_model_reports_failure_without_download(operator, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("offline command downloaded a model")

    monkeypatch.setattr("tracefold.app.claim_embedding.prepare_model", forbidden)
    output = io.StringIO()
    assert main(["news", "embedding", "check"], stdout=output) == 1
    assert json.loads(output.getvalue())["error"] == "news_embedding_self_test_failed"


def test_prepare_rejects_an_uncalibrated_model_before_any_download(operator, monkeypatch) -> None:
    (operator / "config.yaml").write_text(yaml.safe_dump({"llm": {"news_embedding": {"model": "another-model"}}}))

    def forbidden(*args, **kwargs):
        raise AssertionError("uncalibrated model downloaded")

    monkeypatch.setattr("tracefold.app.claim_embedding.prepare_model", forbidden)
    output = io.StringIO()
    assert main(["news", "embedding", "prepare"], stdout=output) == 1
    assert json.loads(output.getvalue())["error"] == "news_embedding_calibration_identity_mismatch"


@pytest.mark.parametrize("batch_size", [1, 31, 513])
def test_backfill_rejects_an_unbounded_page_before_loading_model(operator, monkeypatch, batch_size):
    def forbidden(*args, **kwargs):
        raise AssertionError("model work before validating batch size")

    monkeypatch.setattr(ClaimEmbedder, "__init__", forbidden)
    assert (
        main(
            ["news", "embedding", "backfill", "--batch-size", str(batch_size), "--checkpoint", "unused.json"],
            stdout=io.StringIO(),
        )
        == 2
    )


def test_backfill_resumes_only_the_matching_database_and_model(operator, monkeypatch) -> None:
    from tracefold.news.storage.claim_recall import PgClaimRecall

    async def ready(self):
        return True

    seen = []

    async def bulk(self, *, batch_size, resume, checkpoint):
        seen.append(resume)
        await checkpoint({"as_of_ms": 123, "phase": "adopted", "after": [1, "a", 2]})
        return {"completed": True}

    monkeypatch.setattr(ClaimEmbedder, "self_test", ready)
    monkeypatch.setattr(PgClaimRecall, "bulk_backfill", bulk)
    checkpoint = operator / "backfill.json"
    command = ["news", "embedding", "backfill", "--checkpoint", str(checkpoint)]
    assert main(command, stdout=io.StringIO()) == 0
    assert main(command, stdout=io.StringIO()) == 0
    assert seen == [None, {"as_of_ms": 123, "phase": "adopted", "after": [1, "a", 2]}]
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    saved = json.loads(checkpoint.read_text())
    saved["database"] = "another-database"
    checkpoint.write_text(json.dumps(saved))
    assert main(command, stdout=io.StringIO()) == 1
    assert len(seen) == 2


def test_backfill_database_failure_preserves_checkpoint_and_reports_no_connection_secrets(operator, monkeypatch):
    from psycopg import OperationalError

    from tracefold.news.storage.claim_recall import PgClaimRecall

    async def ready(self):
        return True

    async def failed(self, **kwargs):
        raise OperationalError("connection failed: password=private-value")

    monkeypatch.setattr(ClaimEmbedder, "self_test", ready)
    monkeypatch.setattr(PgClaimRecall, "bulk_backfill", failed)
    checkpoint = operator / "backfill.json"
    output = io.StringIO()
    assert main(["news", "embedding", "backfill", "--checkpoint", str(checkpoint)], stdout=output) == 1
    assert json.loads(output.getvalue())["error"] == "OperationalError"
    assert "private-value" not in output.getvalue()
    assert not checkpoint.exists()
