"""Offline journals bind questions/backend/input and record measured call duration."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import reask_news_models as reasks
from scripts.news_reader_io import write_jsonl
from tests.support.news_extraction_809 import extraction_case
from tests.support.news_update_semantic import generated
from tracefold.news.notifications.policy import PUSHABLE_KINDS
from tracefold.news.notifications.reader import (
    READER_QUESTIONS_IDENTITY,
    InterruptEvidence,
    MaterialityEvidence,
    ReaderJudgment,
    ReportKindEvidence,
)
from tracefold.news.updates.identity import digest


def test_offline_cli_loads_provider_sdk_in_a_fresh_process() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import scripts.reask_news_models; import litellm; assert callable(litellm.acompletion)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_extraction_journal_keeps_visible_input_grounding_changes_and_frozen_instruction(monkeypatch, tmp_path) -> None:
    source, manual = extraction_case("R2")
    reply = manual.model_dump(mode="json")
    for quote in reply["claims"][0]["citations"]:
        quote["evidence_ref"] = "e1"
    bad = {**reply["claims"][0], "slot": "invented", "citations": [{"evidence_ref": "e1", "quote": "Invented."}]}
    reply["claims"].append(bad)
    generated(monkeypatch, reply)
    route = SimpleNamespace(lms=lambda: SimpleNamespace(callbacks=[], history=[]), identity="fixture-route")
    monkeypatch.setattr(reasks, "load_settings", lambda **_: SimpleNamespace())
    monkeypatch.setattr(reasks, "compose_news_models", lambda _: SimpleNamespace(judgment=route, extraction=route))
    source_file, journal = tmp_path / "sources.jsonl", tmp_path / "journal.jsonl"
    write_jsonl(source_file, [{"case_id": "R2", "source": source.model_dump(mode="json")}])
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("Frozen program A", encoding="utf-8")
    args = argparse.Namespace(
        kind="extraction",
        backend="generated",
        input=source_file,
        output=journal,
        extraction_instruction=instruction,
        concurrency=1,
        timeout=5,
        retry_failed=False,
        batch_size=1,
    )
    asyncio.run(reasks.reask(args))
    result = json.loads(journal.read_text())
    assert len(result["decoded_extraction"]["claims"]) == 2
    assert len(result["extraction"]["claims"]) == 1
    assert result["extraction"]["discarded_claims"] == [
        {"slot": "invented", "code": "news_citation_not_in_frozen_source"}
    ]
    assert result["extraction_input_sha256"] == digest(result["extraction_input"])
    assert result["model_identity"] == "fixture-route"
    assert result["calls"]["lm_requests"] == 0  # Fake generation makes no real requests.
    assert "error_code" not in result
    asyncio.run(reasks.reask(args))
    assert len(journal.read_text().splitlines()) == 1
    instruction.write_text("Frozen program B", encoding="utf-8")
    with pytest.raises(ValueError, match="news_offline_resume_program_changed"):
        asyncio.run(reasks.reask(args))


def test_extraction_call_audit_counts_failed_primary_and_fallback_without_request_secrets() -> None:
    primary = SimpleNamespace(model="primary", callbacks=[], history=[])
    fallback = SimpleNamespace(
        model="fallback",
        callbacks=[],
        history=[
            {
                "response": {"model": "served-fallback"},
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "kwargs": {"api_key": "never-export-this"},
            }
        ],
    )
    audit = reasks.ExtractionCalls(lambda: (primary, fallback))
    assert audit.route() == (primary, fallback)
    audit.on_lm_start("a", primary, {"api_key": "never-export-this", "messages": ["private prompt"]})
    audit.on_lm_end("a", None, RuntimeError("secret provider URL"))
    audit.on_lm_start("b", fallback, {})
    audit.on_lm_end("b", [])
    result = audit.report()
    assert result["lm_requests"] == 2
    assert [row["status"] for row in result["requests"]] == ["failed", "response"]
    assert result["responses"][0]["usage"]["completion_tokens"] == 20
    assert result["responses"][0]["served_model"] == "served-fallback"
    assert all(
        secret not in json.dumps(result) for secret in ("never-export-this", "private prompt", "secret provider URL")
    )


def test_extraction_provider_output_is_retained_only_when_requested() -> None:
    lm = SimpleNamespace(
        model="fixture",
        callbacks=[],
        history=[
            {
                "response": {"model": "fixture", "choices": [{"message": {"content": '{"claims":[]}'}}]},
                "usage": {},
            }
        ],
    )
    audit = reasks.ExtractionCalls(lambda: lm)
    audit.route()
    assert "output_text" not in audit.report()["responses"][0]
    assert audit.report(include_outputs=True)["responses"][0]["output_text"] == ['{"claims":[]}']


@pytest.mark.parametrize("suffix", [".jsonl", ".jsonl.gz"])
def test_reader_journal_measures_duration_binds_resume_and_keeps_wrong_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
) -> None:
    frozen = {
        "schema_version": "news_reader_input_v3",
        "as_of": "2026-10-02",
        "messages": [],
        "claim": {"statement": "A launch", "fields": {"subject": "project", "action": "launch"}},
        "sources": [{"publisher": "fixture", "quote": "A launch"}],
    }
    source, output = tmp_path / f"cases{suffix}", tmp_path / "journal.jsonl"
    write_jsonl(source, [{"case_id": "a", "reader_input": frozen}])
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

    calls = []

    class Reader:
        identity = "stub-program"

        async def judge(self, *_: Any) -> ReaderJudgment:
            calls.append("asked")
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
    assert len(calls) == 2
    args.input = tmp_path / "fixed.jsonl.gz"
    args.output = tmp_path / "skipped.jsonl"
    write_jsonl(
        args.input,
        [
            {
                "case_id": "fixed",
                "reader_input": frozen,
                "reader_applicable": False,
                "original_reason": "protected_listing",
            }
        ],
    )
    asyncio.run(reasks.reask(args))
    skipped = json.loads(args.output.read_text())
    assert skipped["skipped"] is True and skipped["original_reason"] == "protected_listing"
    assert "judgment" not in skipped and "error_code" not in skipped and len(calls) == 2
