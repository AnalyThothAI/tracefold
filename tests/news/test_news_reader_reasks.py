"""Offline journals bind questions/backend/input and record measured call duration."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import reask_news_models as reasks
from tracefold.news.notifications.policy import PUSHABLE_KINDS
from tracefold.news.notifications.reader import (
    READER_QUESTIONS_IDENTITY,
    InterruptEvidence,
    MaterialityEvidence,
    ReaderJudgment,
    ReportKindEvidence,
)
from tracefold.news.updates.identity import digest


def test_reader_journal_measures_duration_binds_resume_and_keeps_wrong_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frozen = {
        "schema_version": "news_reader_input_v3",
        "as_of": "2026-10-02",
        "messages": [],
        "claim": {"statement": "A launch", "fields": {"subject": "project", "action": "launch"}},
        "sources": [{"publisher": "fixture", "quote": "A launch"}],
    }
    source, output = tmp_path / "cases.jsonl", tmp_path / "journal.jsonl"
    source.write_text(json.dumps({"case_id": "a", "reader_input": frozen}) + "\n")
    supplied = ReaderJudgment(
        status="available",
        backend="generated",
        identity="stub-adapter",
        report_kind=ReportKindEvidence(
            value="new_action",
            confidence=1,
            probabilities={kind: float(kind == "new_action") for kind in PUSHABLE_KINDS},
        ),
        materiality=MaterialityEvidence(value=2, probabilities=(0, 0, 1, 0), confidence=1),
        interrupt=InterruptEvidence(probabilities=(1, 0), confidence=1),
    )

    class Reader:
        identity = "stub-program"

        async def judge(self, *_: Any) -> ReaderJudgment:
            return supplied

    route = SimpleNamespace(lms=None, identity="stub-model")
    monkeypatch.setattr(reasks, "load_settings", lambda **_: SimpleNamespace())
    monkeypatch.setattr(reasks, "compose_news_models", lambda _: SimpleNamespace(judgment=route, extraction=route))
    monkeypatch.setattr(reasks, "GeneratedJudgments", lambda *_, **__: SimpleNamespace(identity="stub-generated"))
    monkeypatch.setattr(reasks, "DspyExtractor", lambda *_, **__: SimpleNamespace(identity="stub-extraction"))
    monkeypatch.setattr(reasks, "DspyReaderJudge", lambda *_, **__: Reader())
    clock = iter((10.0, 10.25, 20.0, 20.1))
    monkeypatch.setattr(reasks, "monotonic", lambda: next(clock))
    args = argparse.Namespace(
        kind="reader",
        backend="generated",
        input=source,
        output=output,
        concurrency=1,
        timeout=5,
        retry_failed=False,
        batch_size=1,
    )
    asyncio.run(reasks.reask(args))
    record = json.loads(output.read_text())
    assert record["input_sha256"] == digest(frozen)
    assert record["questions_identity"] == READER_QUESTIONS_IDENTITY
    assert record["requested_backend"] == "generated"
    assert record["duration_ms"] == 250
    assert record["judgment"]["identity"] == "stub-adapter"
    assert "error_code" not in record
    for field, changed in (("questions_identity", "old-questions"), ("requested_backend", "native")):
        output.write_text(json.dumps({**record, field: changed}) + "\n")
        with pytest.raises(ValueError, match="resume_questions_or_backend_changed"):
            asyncio.run(reasks.reask(args))
    supplied = supplied.model_copy(update={"backend": "native"})
    args.output = tmp_path / "failed.jsonl"
    asyncio.run(reasks.reask(args))
    failed = json.loads(args.output.read_text())
    assert failed["judgment"]["backend"] == "native"
    assert failed["error_code"] == "news_offline_requested_backend_unavailable"
    assert failed["duration_ms"] == pytest.approx(100)
