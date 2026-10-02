"""Verify offline ONNX vectors and production retrieval against frozen pre-ONNX evidence.

Run with an explicitly prepared cache; no download, database, LLM or notification operation runs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
import platform
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import numpy as np

from tracefold.app.claim_embedding import ClaimEmbedder, validate_model_snapshot
from tracefold.news.claim_recall import CALIBRATION, Candidate, Probe, prepare_rank, rank
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, reader_novelty
from tracefold.news.notifications.recall import select_for_claim
from tracefold.news.updates.contracts import Claim

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/news"
MIN_COSINE = 0.9999


def frozen_vectors(fixtures: Path = FIXTURES) -> dict[str, bytes]:
    data = json.loads((fixtures / "issue_791_recall_golden_vectors.json").read_text(encoding="utf-8"))
    for field in ("model", "dimensions", "revision", "max_tokens", "pooling", "dtype", "normalization", "template"):
        if data[field] != getattr(CALIBRATION.embedder, field):
            raise ValueError("news_embedding_verification_fixture_identity_mismatch")
    return {text: base64.b64decode(raw, validate=True) for text, raw in data["vectors"].items()}


def _matrix(vectors: Mapping[str, bytes], texts: list[str]) -> np.ndarray:
    matrix = np.stack([np.frombuffer(vectors[text], dtype="<f2").astype(np.float64) for text in texts])
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _native_threads() -> int | None:
    tasks = Path("/proc/self/task")
    return sum(1 for _ in tasks.iterdir()) if tasks.exists() else None


def comparison_report(
    previous: Mapping[str, bytes], current: Mapping[str, bytes], fixtures: Path = FIXTURES
) -> dict[str, Any]:
    """Use the production ranker and reader selector, preserving existing stored vectors."""
    texts = list(previous)
    old, new = _matrix(previous, texts), _matrix(current, texts)
    cosines = np.clip(np.einsum("ij,ij->i", old, new), -1, 1)
    errors = np.abs(new - old).max(axis=1)
    _require(float(cosines.min()) >= MIN_COSINE, "news_embedding_verification_cosine_failed")
    comparisons: dict[str, dict[str, int]] = {}
    for consumer in ("prior", "receipt"):
        policy: Literal["prior", "receipt"] = consumer
        old_rows = tuple(
            Candidate(f"frozen:{index:02d}", previous[text], CALIBRATION.embedder.key)
            for index, text in enumerate(texts)
        )
        mixed_rows = tuple(
            Candidate(row.key, current[text] if index % 2 else previous[text], row.embedder)
            for index, (text, row) in enumerate(zip(texts, old_rows, strict=True))
        )
        old_candidate_mismatches = mixed_candidate_mismatches = 0
        for text in texts:
            before = rank(
                prepare_rank(Probe(text, previous[text], CALIBRATION.embedder.key), old_rows, policy), old_rows
            )
            fresh = Probe(text, current[text], CALIBRATION.embedder.key)
            old_index = rank(prepare_rank(fresh, old_rows, policy), old_rows)
            mixed_index = rank(prepare_rank(fresh, mixed_rows, policy), mixed_rows)
            signature = tuple((hit.key, hit.routes) for hit in before.hits)
            old_candidate_mismatches += signature != tuple((hit.key, hit.routes) for hit in old_index.hits)
            mixed_candidate_mismatches += signature != tuple((hit.key, hit.routes) for hit in mixed_index.hits)
        _require(
            old_candidate_mismatches == mixed_candidate_mismatches == 0, "news_embedding_verification_rank_changed"
        )
        comparisons[consumer] = {
            "queries": len(texts),
            "old_candidate_mismatches": old_candidate_mismatches,
            "mixed_candidate_mismatches": mixed_candidate_mismatches,
        }

    gold_fixture = json.loads((fixtures / "issue_750_gold_recall.json").read_text(encoding="utf-8"))
    links = tuple(ClaimLink.model_validate(row) for row in gold_fixture["links"])
    receipts = tuple(LinkedReceipt.model_validate(row) for row in gold_fixture["link_receipts"])
    candidates = tuple(
        Candidate(
            f"{row['intent_id']}:{claim['ref']}",
            previous[claim["statement"]],
            CALIBRATION.embedder.key,
            group=row["intent_id"],
        )
        for row in gold_fixture["candidates"]
        for claim in row["claims"]
    )
    available = frozenset(row["intent_id"] for row in gold_fixture["candidates"])

    def select(claim: Claim, vectors: Mapping[str, bytes]) -> tuple[str, ...]:
        probe = Probe(claim.statement, vectors[claim.statement], CALIBRATION.embedder.key)
        ranking = rank(prepare_rank(probe, candidates, "receipt"), candidates)
        return select_for_claim(reader_novelty(claim.ref, links, receipts), ranking, available=available).intent_ids

    gold, data = (Claim.model_validate(row) for row in gold_fixture["claims"])
    selected = select(gold, current)
    prefixes = {intent[7:13] for intent in selected}
    positives = {key for key, label in gold_fixture["labels"].items() if label == 2}
    negatives = {key for key, label in gold_fixture["labels"].items() if label == 0}
    _require(len(positives) == 4 and positives <= prefixes, "news_embedding_verification_gold_receipt_missing")
    _require(not prefixes & negatives and not select(data, current), "news_embedding_verification_unrelated_receipt")
    _require(selected == select(gold, previous), "news_embedding_verification_reader_selection_changed")

    query = (
        "The House Oversight Committee expanded its prediction market insider trading probe "
        "to Hyperliquid, Crypto.com, and PredictIt"
    )
    related = "The House Oversight Committee widens its insider trading probe to Hyperliquid"
    noise = (
        "Russian drone hits a Kyiv market as trading halts",
        "Trump administration ends the fuel-economy credit trading market",
        "Iran stock market index falls in thin trading",
        "Arm and Intel rise in premarket trading as the chip market rallies",
    )
    rows = tuple(Candidate(text, previous[text], CALIBRATION.embedder.key, lexical=0.9) for text in (related, *noise))
    new_ranking = rank(prepare_rank(Probe(query, current[query], CALIBRATION.embedder.key), rows, "receipt"), rows)
    old_ranking = rank(prepare_rank(Probe(query, previous[query], CALIBRATION.embedder.key), rows, "receipt"), rows)
    keys = tuple(hit.key for hit in new_ranking.hits)
    unrelated = len(set(keys) & set(noise))
    _require(related in keys and unrelated <= 1, "news_embedding_verification_shared_words_failed")
    _require(keys == tuple(hit.key for hit in old_ranking.hits), "news_embedding_verification_shared_words_changed")
    return {
        "vectors": {
            "count": len(texts),
            "cosine_min": float(cosines.min()),
            "cosine_p50": float(np.median(cosines)),
            "cosine_mean": float(cosines.mean()),
            "max_abs_error": float(errors.max()),
            "max_abs_error_p50": float(np.median(errors)),
            "identical_fp16_vectors": sum(previous[text] == current[text] for text in texts),
            "required_cosine": MIN_COSINE,
        },
        "rank_compatibility": comparisons,
        "issue_750": {
            "gold_receipts": sorted(positives),
            "selected_receipts": sorted(prefixes),
            "negative_receipts": len(prefixes & negatives),
            "empty_data_selection": True,
            "selection_matches_previous": True,
        },
        "issue_755": {
            "selected": keys,
            "related_selected": True,
            "unrelated_selected": unrelated,
            "selection_matches_previous": True,
        },
    }


async def verify(cache_dir: Path, fixtures: Path = FIXTURES) -> dict[str, Any]:
    previous = frozen_vectors(fixtures)
    embedder = ClaimEmbedder(model=CALIBRATION.embedder.model, cache_dir=cache_dir)
    try:
        before_threads = _native_threads()
        started = time.perf_counter()
        _require(await embedder.self_test(), "news_embedding_verification_self_test_failed")
        startup_ms = (time.perf_counter() - started) * 1000
        startup_threads = _native_threads()
        started = time.perf_counter()
        probes = await embedder.probes(list(previous))
        vectors_ms = (time.perf_counter() - started) * 1000
        encoded_threads = _native_threads()
        _require(all(probe.vector is not None for probe in probes), "news_embedding_verification_degraded")
        current = {probe.text: probe.vector for probe in probes if probe.vector is not None}
        report = comparison_report(previous, current, fixtures)
        business = json.loads((fixtures / "reader_791_business_regressions.json").read_text(encoding="utf-8"))
        texts = [case["statement"] for case in business["cases"]]
        _require(len(texts) == 11, "news_embedding_verification_eleven_case_fixture_changed")
        batch_times = []
        batch: tuple[Probe, ...] = ()
        for _ in range(3):
            started = time.perf_counter()
            batch = await embedder.probes(texts)
            batch_times.append((time.perf_counter() - started) * 1000)
        singles = [(await embedder.probes([text]))[0] for text in texts]
        _require(
            all(probe.vector is not None for probe in (*batch, *singles)),
            "news_embedding_verification_eleven_case_degraded",
        )
        pair_cosines = []
        for expected_text, batched, single in zip(texts, batch, singles, strict=True):
            _require(expected_text == batched.text == single.text, "news_embedding_verification_batch_order_failed")
            if batched.vector is None or single.vector is None:
                raise ValueError("news_embedding_verification_eleven_case_degraded")
            left = np.frombuffer(batched.vector, dtype="<f2").astype(np.float64)
            right = np.frombuffer(single.vector, dtype="<f2").astype(np.float64)
            pair_cosines.append(float(left @ right / np.linalg.norm(left) / np.linalg.norm(right)))
        _require(min(pair_cosines) >= MIN_COSINE, "news_embedding_verification_batch_padding_failed")
        return {
            "protocol": "799_offline_onnx_compatibility_v1",
            "ok": True,
            "model_snapshot": validate_model_snapshot(cache_dir),
            "fixture_sha256": {
                filename: hashlib.sha256((fixtures / filename).read_bytes()).hexdigest()
                for filename in (
                    "issue_791_recall_golden_vectors.json",
                    "issue_750_gold_recall.json",
                    "reader_791_business_regressions.json",
                )
            },
            "runtime": {
                "python": platform.python_version(),
                "packages": {name: importlib.metadata.version(name) for name in ("onnxruntime", "tokenizers", "numpy")},
                "provider": "CPUExecutionProvider",
                "intra_op_threads": 2,
                "tokenizers_parallelism": os.environ.get("TOKENIZERS_PARALLELISM"),
                "native_threads_before_encoder": before_threads,
                "native_threads_after_startup": startup_threads,
                "native_threads_after_55_encodings": encoded_threads,
                "startup_golden_ms": startup_ms,
                "frozen_55_encoding_ms": vectors_ms,
            },
            **report,
            "eleven_statement_batch": {
                "fixture": "reader_791_business_regressions.json",
                "statements": len(texts),
                "unique_statements": len(set(texts)),
                "batch_ms": batch_times,
                "batch_ms_p50": float(np.median(batch_times)),
                "batch_vs_single_cosine_min": min(pair_cosines),
                "order_preserved": True,
            },
            "limitations": [
                "Frozen historical FP16 vectors are the baseline; PyTorch is not invoked again.",
                "Rank and reader-selector verification uses frozen eligible candidates; PostgreSQL retrieval, "
                "transaction budgets, concurrent Workers and production-scale backfill are separate evidence.",
                "The eleven-statement batch contains eleven frozen reader business cases, including a repeated "
                "statement; it is not one reconstructed eleven-claim extraction.",
                "No new LLM judgment, chronological adoption or delivery is performed.",
            ],
        }
    finally:
        await embedder.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(verify(args.cache_dir))
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
