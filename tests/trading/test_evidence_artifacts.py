"""Evidence artifacts are canonical, immutable and independently verifiable."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracefold.app.cli.evidence_artifacts import load_evidence_artifact, publish_evidence_artifact
from tracefold.trading.evidence_clock import DiscoveryCorpusReceiptV1


def _receipt() -> DiscoveryCorpusReceiptV1:
    return DiscoveryCorpusReceiptV1(
        corpus_sha256="a" * 64,
        artifact_sha256="a" * 64,
        artifact_path="corpus.json",
        capture_sha256="b" * 64,
        drain_sha256="c" * 64,
        execution_contract_receipt_sha256="d" * 64,
        source_count=1,
        created_at_ms=1,
    )


def test_publish_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    first_path, first_sha = publish_evidence_artifact(tmp_path, kind="corpus", artifact=_receipt())
    second_path, second_sha = publish_evidence_artifact(tmp_path, kind="corpus", artifact=_receipt())

    assert first_path == second_path
    assert first_sha == second_sha == _receipt().receipt_sha256
    assert load_evidence_artifact(first_path, DiscoveryCorpusReceiptV1, expected_sha256=first_sha) == _receipt()


def test_publish_refuses_a_corrupt_existing_content_address(tmp_path: Path) -> None:
    path, digest = publish_evidence_artifact(tmp_path, kind="corpus", artifact=_receipt())
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="trading_evidence_artifact_corrupt"):
        publish_evidence_artifact(tmp_path, kind="corpus", artifact=_receipt())
    with pytest.raises(RuntimeError, match="trading_evidence_artifact_corrupt"):
        load_evidence_artifact(path, DiscoveryCorpusReceiptV1, expected_sha256=digest)
