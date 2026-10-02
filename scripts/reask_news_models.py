"""Authorized offline News reasks on operator-selected routes; no stores, caches or sends.

Inputs are explicit JSONL: speech has claim/evidence, extraction has source (FrozenInput),
reader has reader_input (current version). Output is an append-only research journal.
Use separate native and generated reader runs. Failed native calls are recorded as failures,
never silently counted as native evidence after generation fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from tracefold.app.learning_runtime import compose_news_models
from tracefold.app.news_updates import NewsJudgmentEndpoint, compose_reader_judge
from tracefold.news.adapters.extraction import DspyExtractor
from tracefold.news.adapters.reader_judge import DspyReaderJudge
from tracefold.news.adapters.semantic_judgments import GeneratedJudgments
from tracefold.news.notifications.reader import ReaderInput
from tracefold.news.updates.contracts import FrozenInput
from tracefold.news.updates.extraction import ground_extraction, validate_extraction
from tracefold.news.updates.identity import canonical_json, digest
from tracefold.news.updates.judgment import OPTIONS, Budget, Question
from tracefold.news.updates.topics import CODEBOOK
from tracefold.platform.config.loader import load_settings
from tracefold.platform.config.secret_file import read_secure_secret_text


def input_digest(row: dict[str, Any], kind: str) -> str:
    """Labels and sampling metadata cannot change an exact model input."""
    return digest(row["reader_input"]) if kind == "reader" else digest(row)


async def reask(args: argparse.Namespace) -> None:
    settings = load_settings(require_ws_token=False)
    models = compose_news_models(settings)
    if models is None:
        raise ValueError("news_offline_configured_models_required")
    generated = GeneratedJudgments(models.judgment.lms, model_identity=models.judgment.identity)
    extractor = DspyExtractor(models.extraction.lms, model_identity=models.extraction.identity, topics=dict(CODEBOOK))
    reader = DspyReaderJudge(models.judgment.lms, generated_model_identity=models.judgment.identity)
    connection = None
    if args.kind == "reader" and args.backend == "native":
        route = settings.llm.news_reader_judgment
        key_file = settings.news_reader_judgment_api_key_file()
        if not route.configured or key_file is None:
            raise ValueError("news_offline_native_reader_route_required")
        endpoint = NewsJudgmentEndpoint(
            base_url=str(route.base_url), model=str(route.model), api_key=read_secure_secret_text(key_file)
        )
        reader, connection = compose_reader_judge(
            generated_lm_factory=models.judgment.lms,
            generated_model_identity=models.judgment.identity,
            reader_judgment=endpoint,
        )
    rows = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    expected_identity = {"speech": generated.identity, "extraction": extractor.identity, "reader": reader.identity}[
        args.kind
    ]
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("news_offline_duplicate_case")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    completed = set()
    if args.output.exists():
        journal = {row["case_id"]: row for row in map(json.loads, args.output.read_text().splitlines())}
        by_id = {row["case_id"]: row for row in rows}
        if set(journal) - set(by_id):
            raise ValueError("news_offline_resume_cases_changed")
        if any(record.get("program_identity") != expected_identity for record in journal.values()):
            raise ValueError("news_offline_resume_program_changed")
        if any(
            case in by_id and record["input_sha256"] != input_digest(by_id[case], args.kind)
            for case, record in journal.items()
        ):
            raise ValueError("news_offline_resume_input_changed")
        completed = {
            case
            for case, record in journal.items()
            if not args.retry_failed or not ("error_class" in record or "error_code" in record)
        }
    semaphore = asyncio.Semaphore(args.concurrency)

    async def speech_batch(batch: list[dict[str, Any]]) -> None:
        async with semaphore:
            results = {
                row["case_id"]: {
                    "case_id": row["case_id"],
                    "input_sha256": digest(row),
                    "kind": "speech",
                    "program_identity": generated.identity,
                }
                for row in batch
            }
            try:
                async with asyncio.timeout(args.timeout):
                    questions = []
                    for row in batch:
                        payload = json.loads(json.dumps({"claim": row["claim"], "evidence": row.get("evidence", [])}))
                        for field in ("mode", "actor_role"):
                            payload["claim"].get("fields", {}).pop(field, None)
                        questions.append(Question(item_id=row["case_id"], payload_json=canonical_json(payload)))
                    for task in ("mode", "actor_role"):
                        answers = await generated.judge(task, tuple(questions), context_json=None)
                        if {answer.item_id for answer in answers.answers} != set(results):
                            raise ValueError("news_offline_speech_batch_incomplete")
                        for answer in answers.answers:
                            if answer.status != "available" or answer.value not in {
                                value for value, _ in OPTIONS[task]
                            }:
                                raise ValueError("news_offline_speech_answer_invalid")
                            results[answer.item_id][task] = answer.value
            except Exception as exc:
                for result in results.values():
                    result["error_class"] = type(exc).__name__
            with args.output.open("a", encoding="utf-8") as stream:
                for result in results.values():
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            completed.update(results)
            print(f"speech: {len(completed)}/{len(rows)} recorded", flush=True)

    async def ask(row: dict[str, Any]) -> None:
        if row["case_id"] in completed:
            return
        async with semaphore:
            result: dict[str, Any] = {
                "case_id": row["case_id"],
                "input_sha256": input_digest(row, args.kind),
                "kind": args.kind,
                "program_identity": expected_identity,
            }
            try:
                async with asyncio.timeout(args.timeout):
                    if args.kind == "extraction":
                        source = FrozenInput.model_validate(row["source"])
                        extracted = ground_extraction(source, await extractor.extract(source))
                        validate_extraction(source, extracted)
                        result.update(program_identity=extractor.identity, extraction=extracted.model_dump(mode="json"))
                    else:
                        reader_input = ReaderInput.model_validate(row["reader_input"])
                        judgment = await reader.judge(reader_input, Budget.start(args.timeout))
                        result.update(program_identity=reader.identity, judgment=judgment.model_dump(mode="json"))
                        if judgment.status != "available" or judgment.backend != args.backend:
                            result["error_code"] = "news_offline_requested_backend_unavailable"
            except Exception as exc:
                # Provider exception strings can contain URLs or credentials. Keep a bounded class only.
                result["error_class"] = type(exc).__name__
            with args.output.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            completed.add(row["case_id"])
            if len(completed) % 25 == 0 or len(completed) == len(rows):
                print(f"{args.kind}/{args.backend}: {len(completed)}/{len(rows)} recorded", flush=True)

    try:
        if args.kind == "speech":
            pending = [row for row in rows if row["case_id"] not in completed]
            await asyncio.gather(
                *(
                    speech_batch(pending[offset : offset + args.batch_size])
                    for offset in range(0, len(pending), args.batch_size)
                )
            )
        else:
            await asyncio.gather(*(ask(row) for row in rows))
    finally:
        if connection is not None:
            await connection.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("speech", "extraction", "reader"))
    parser.add_argument("--backend", choices=("native", "generated"), default="generated")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--retry-failed", action="store_true", help="Retry only explicitly failed journal cases.")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 16 or not 1 <= args.batch_size <= 32 or args.timeout <= 0:
        parser.error("concurrency must be 1..16 and timeout must be positive")
    asyncio.run(reask(args))


if __name__ == "__main__":
    main()
