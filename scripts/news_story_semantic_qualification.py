from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import resource
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

CORPUS_SCHEMA_VERSION = "news_story_semantic_qualification_corpus_v1"
EVIDENCE_SCHEMA_VERSION = "news_story_semantic_qualification_evidence_v1"
ALLOWED_CORPUS_PARTITIONS = {"train", "development", "final_holdout", "mandatory_regression"}
REQUIRED_HARD_NEGATIVE_TYPES = {
    "incompatible_amount",
    "incompatible_reporting_period",
    "opposite_action",
    "same_company_different_announcement",
    "same_geopolitical_conflict_different_development",
    "same_location_different_occurrence_time",
    "same_person_different_statement",
    "same_template_different_asset",
}
REQUIRED_REGRESSION_FAMILIES = {"nvidia_sb_energy", "qatar_iranian_pilots"}
REQUIRED_MODEL_IDS = {
    "BAAI/bge-m3",
    "intfloat/multilingual-e5-base",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
}
_STRUCTURAL_PREFIX_RE = re.compile(
    r"^(?:\*+\s*|rpt\s*[-:：]\s*|"
    r"(?:breaking|just\s+in|update|exclusive|alert|quote|reply)\s*[:：-]\s*|"
    r"(?:coindesk|cointelegraph|the\s+block|reuters)\s*[:：]\s*)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _comparison_title(value: str) -> str:
    from tracefold.news.exact_atom_identity import comparison_title

    return comparison_title(value)


def _story_v2_module() -> Any:
    from tracefold.news import story_projection

    return story_projection


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the read-only News Story semantic qualification baseline.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("tests/fixtures/news_story_semantic_qualification_corpus_v1.json"),
    )
    parser.add_argument(
        "--model-manifest",
        type=Path,
        default=Path("tests/fixtures/news_story_semantic_qualification_models_v1.json"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--corpus-only", action="store_true")
    mode.add_argument("--evidence-only", action="store_true")
    mode.add_argument("--benchmark-items", type=int)
    mode.add_argument("--embed-model", choices=sorted(REQUIRED_MODEL_IDS))
    mode.add_argument("--evaluate-vector", type=Path)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=Path("docs/research/news-story-semantic-qualification-evidence-v1.json"),
    )
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tracefold-semantic-qualification")
    parser.add_argument("--vector-output", type=Path)
    parser.add_argument("--prepared-model-dir", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--k", type=int, choices=(4, 8, 16), default=8)
    parser.add_argument("--block-size", type=int, default=128)
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--semantic-threshold", type=float, default=0.8)
    parser.add_argument("--evaluate-linear", action="store_true")
    parser.add_argument("--linear-c", type=float, default=1.0)
    parser.add_argument("--linear-threshold", type=float, default=0.5)
    parser.add_argument("--include-final-holdout", action="store_true")
    args = parser.parse_args()
    if args.corpus_only:
        print(json.dumps(load_qualification_corpus(args.corpus), ensure_ascii=False, sort_keys=True))
        return 0
    if args.evidence_only:
        print(
            json.dumps(
                load_qualification_evidence(args.evidence),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    if args.benchmark_items is not None:
        print(
            json.dumps(
                benchmark_exact_cosine_resource(
                    item_count=int(args.benchmark_items),
                    dimension=int(args.dimension),
                    k=int(args.k),
                    block_size=int(args.block_size),
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    if args.embed_model:
        output_path = args.vector_output or (
            args.cache_dir / "vectors" / f"{str(args.embed_model).replace('/', '--')}.npy"
        )
        result = embed_corpus_model(
            args.corpus,
            args.model_manifest,
            str(args.embed_model),
            cache_dir=args.cache_dir / "models",
            output_path=output_path,
            offline=bool(args.offline),
            prepared_model_dir=args.prepared_model_dir,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    if args.evaluate_vector:
        partitions = (
            set(ALLOWED_CORPUS_PARTITIONS)
            if args.include_final_holdout
            else {"train", "development", "mandatory_regression"}
        )
        neighbors, search = neighbors_from_vector_artifact(
            args.corpus,
            args.evaluate_vector,
            k=int(args.k),
            block_size=int(args.block_size),
        )
        cosines = pair_cosines_from_vector_artifact(args.corpus, args.evaluate_vector)
        model_id = str(search["model_id"])
        result = {
            "A": evaluate_v2_corpus(args.corpus, partitions=partitions),
            "B": evaluate_fact_confidence_corpus(args.corpus, partitions=partitions),
            "C": evaluate_dense_candidate_corpus(
                args.corpus,
                neighbors,
                model_id=model_id,
                k=int(args.k),
                partitions=partitions,
            ),
            "D": evaluate_deterministic_semantic_corpus(
                args.corpus,
                neighbors,
                cosines,
                model_id=model_id,
                k=int(args.k),
                threshold=float(args.semantic_threshold),
                partitions=partitions,
            ),
            "search": search,
            "holdout_policy": {
                "final_holdout_included": bool(args.include_final_holdout),
                "partitions": sorted(partitions),
            },
        }
        if args.evaluate_linear:
            fit = fit_linear_verifier(args.corpus, cosines, c_value=float(args.linear_c))
            result["E_fit"] = fit
            result["E"] = evaluate_linear_verifier_corpus(
                args.corpus,
                neighbors,
                cosines,
                coefficients=fit["coefficients"],
                model_id=model_id,
                k=int(args.k),
                threshold=float(args.linear_threshold),
                partitions=partitions,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    from tracefold.platform.config.settings import load_settings

    report = run_read_only_qualification(
        settings=load_settings(require_ws_token=False),
        now_ms=int(time.time() * 1_000),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def run_read_only_qualification(*, settings: Any, now_ms: int) -> dict[str, Any]:
    """Load the configured facts under a read-only transaction and build baseline A."""

    from tracefold.app.repositories import repositories
    from tracefold.news.story_projection import NewsStoryFactSnapshot

    with repositories(settings, role="serve") as repos:
        repos.conn.execute("SET TRANSACTION READ ONLY")
        revision_row = repos.conn.execute("SELECT version_num FROM alembic_version").fetchone()
        payload = repos.news.load_story_projection(now_ms=int(now_ms))
    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint=str(payload["material_snapshot_fingerprint"]),
        evaluation_time_ms=int(payload["evaluation_time_ms"]),
        published_material_snapshot_fingerprint=(
            str(payload["published_material_snapshot_fingerprint"])
            if payload.get("published_material_snapshot_fingerprint")
            else None
        ),
        rows=tuple(dict(row) for row in payload["rows"]),
    )
    return build_baseline_report(
        snapshot=snapshot,
        database_revision=str(revision_row["version_num"]) if revision_row is not None else "unknown",
        rss_enabled=bool(settings.news.rss_enabled),
    )


def build_baseline_report(
    *,
    snapshot: Any,
    database_revision: str,
    rss_enabled: bool,
) -> dict[str, Any]:
    """Build the zero-write production baseline for semantic qualification."""

    from tracefold.news.story_projection import build_story_projection

    projection = build_story_projection(snapshot)
    return {
        "schema_version": "news_story_semantic_qualification_v1",
        "mode": "read_only_zero_write",
        "disposition": "qualification_incomplete",
        "database_revision": str(database_revision),
        "rss_enabled": bool(rss_enabled),
        "material_snapshot_fingerprint": snapshot.material_snapshot_fingerprint,
        "evaluation_time_ms": snapshot.evaluation_time_ms,
        "production_baseline": {
            "projection_version": projection.projection_version,
            "projection_fingerprint": projection.projection_fingerprint,
            "story_count": len(projection.stories),
            "membership_count": len(projection.memberships),
            "diagnostics": dict(projection.diagnostics),
        },
        "ablations": {
            "A": {"status": "complete", "authority": "build_story_projection"},
            "B": {"status": "pending"},
            "C": {"status": "pending"},
            "D": {"status": "pending"},
            "E": {"status": "pending"},
        },
    }


def load_qualification_corpus(path: Path) -> dict[str, Any]:
    """Validate and summarize the frozen Issue #46 labelled corpus."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise ValueError("qualification corpus schema_version is invalid")
    items = payload.get("items")
    pairs = payload.get("pairs")
    split_policy = payload.get("split_policy")
    manifest = payload.get("manifest")
    if not isinstance(items, list) or not isinstance(pairs, list) or not isinstance(split_policy, dict):
        raise ValueError("qualification corpus items, pairs, and split_policy are required")
    if not isinstance(manifest, dict):
        raise ValueError("qualification corpus manifest is required")
    if manifest.get("pair_count") != len(pairs) or manifest.get("item_count") != len(items):
        raise ValueError("qualification corpus declared counts are stale")

    items_by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("qualification corpus item must be an object")
        item_id = _bounded_text(item.get("item_id"), field="item_id", maximum=160)
        if item_id in items_by_id:
            raise ValueError(f"duplicate qualification corpus item_id: {item_id}")
        _bounded_text(item.get("event_id"), field="event_id", maximum=200)
        title = _bounded_text(item.get("original_title"), field="original_title", maximum=300)
        if len(title.encode("utf-8")) > 1_024:
            raise ValueError(f"qualification corpus original_title exceeds 1024 bytes: {item_id}")
        language = _bounded_text(item.get("language"), field="language", maximum=16)
        if not language.replace("-", "").isalpha():
            raise ValueError(f"qualification corpus language is invalid: {item_id}")
        _bounded_text(item.get("provenance_kind"), field="provenance_kind", maximum=80)
        if not item.get("source_url") and not item.get("snapshot_fingerprint"):
            raise ValueError(f"qualification corpus item lacks source provenance: {item_id}")
        items_by_id[item_id] = item

    case_ids: set[str] = set()
    event_partitions: dict[str, set[str]] = {}
    hard_negative_types: set[str] = set()
    mandatory_families: set[str] = set()
    positive_pair_count = 0
    hard_negative_pair_count = 0
    zh_en_positive_pair_count = 0
    long_short_positive_pair_count = 0
    partitions: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("qualification corpus pair must be an object")
        case_id = _bounded_text(pair.get("case_id"), field="case_id", maximum=200)
        if case_id in case_ids:
            raise ValueError(f"duplicate qualification corpus case_id: {case_id}")
        case_ids.add(case_id)
        label = pair.get("label")
        if label not in {"same_event", "different_event"}:
            raise ValueError(f"qualification corpus label is invalid: {case_id}")
        partition = pair.get("partition")
        if partition not in ALLOWED_CORPUS_PARTITIONS:
            raise ValueError(f"qualification corpus partition is invalid: {case_id}")
        partitions.add(str(partition))
        left_item_id = str(pair.get("left_item_id") or "")
        right_item_id = str(pair.get("right_item_id") or "")
        if left_item_id == right_item_id or left_item_id not in items_by_id or right_item_id not in items_by_id:
            raise ValueError(f"qualification corpus pair item reference is invalid: {case_id}")
        left = items_by_id[left_item_id]
        right = items_by_id[right_item_id]
        same_event = left["event_id"] == right["event_id"]
        if same_event != (label == "same_event"):
            raise ValueError(f"qualification corpus label disagrees with event identity: {case_id}")
        is_hard_negative = pair.get("hard_negative") is True
        if is_hard_negative != (label == "different_event"):
            raise ValueError(f"qualification corpus hard_negative disagrees with label: {case_id}")
        for item in (left, right):
            event_partitions.setdefault(str(item["event_id"]), set()).add(str(partition))
        if label == "same_event":
            positive_pair_count += 1
            languages = {str(left["language"]), str(right["language"])}
            declared_pair = "_".join(sorted(languages if len(languages) == 2 else (str(left["language"]),) * 2))
            if pair.get("language_pair") != declared_pair:
                raise ValueError(f"qualification corpus language_pair is invalid: {case_id}")
            if languages == {"en", "zh"}:
                zh_en_positive_pair_count += 1
            lengths = sorted((len(str(left["original_title"])), len(str(right["original_title"]))))
            is_long_short = lengths[1] / max(lengths[0], 1) >= 1.75
            if bool(pair.get("long_short")) != is_long_short:
                raise ValueError(f"qualification corpus long_short is invalid: {case_id}")
            if is_long_short:
                long_short_positive_pair_count += 1
        else:
            hard_negative_pair_count += 1
        if pair.get("hard_negative_type") is not None:
            hard_negative_types.add(_bounded_text(pair["hard_negative_type"], field="hard_negative_type", maximum=80))
        if pair.get("mandatory_regression_family") is not None:
            mandatory_families.add(
                _bounded_text(pair["mandatory_regression_family"], field="mandatory_regression_family", maximum=80)
            )

    leaking_events = sorted(event_id for event_id, values in event_partitions.items() if len(values) != 1)
    if leaking_events:
        raise ValueError(f"qualification corpus events cross partitions: {leaking_events[:3]}")
    if len(pairs) < 500 or len(event_partitions) < 60:
        raise ValueError("qualification corpus minimum pair/event counts are not met")
    if positive_pair_count < 150 or hard_negative_pair_count < 250:
        raise ValueError("qualification corpus minimum label counts are not met")
    if zh_en_positive_pair_count < 30 or long_short_positive_pair_count < 30:
        raise ValueError("qualification corpus language/title-length slice counts are not met")
    if hard_negative_types != REQUIRED_HARD_NEGATIVE_TYPES:
        raise ValueError("qualification corpus required hard-negative families are incomplete")
    if mandatory_families != REQUIRED_REGRESSION_FAMILIES:
        raise ValueError("qualification corpus mandatory regression families are incomplete")
    if partitions != ALLOWED_CORPUS_PARTITIONS:
        raise ValueError("qualification corpus partitions are incomplete")

    manifest_payload = {"items": items, "pairs": pairs, "split_policy": split_policy}
    manifest_sha256 = _canonical_sha256(manifest_payload)
    if manifest.get("sha256") != manifest_sha256:
        raise ValueError("qualification corpus manifest checksum is invalid")
    partition_sha256 = manifest.get("partition_sha256")
    expected_partition_sha256 = {
        partition: _canonical_sha256([pair for pair in pairs if pair["partition"] == partition])
        for partition in sorted(partitions)
    }
    if partition_sha256 != expected_partition_sha256:
        raise ValueError("qualification corpus partition checksum is invalid")

    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "manifest_sha256": manifest_sha256,
        "pair_count": len(pairs),
        "event_count": len(event_partitions),
        "positive_pair_count": positive_pair_count,
        "hard_negative_pair_count": hard_negative_pair_count,
        "zh_en_positive_pair_count": zh_en_positive_pair_count,
        "long_short_positive_pair_count": long_short_positive_pair_count,
        "hard_negative_types": sorted(hard_negative_types),
        "mandatory_regression_families": sorted(mandatory_families),
        "partitions": sorted(partitions),
    }


def qualification_evidence_fingerprint(payload: Mapping[str, Any]) -> str:
    """Fingerprint one complete evidence package, excluding only its own digest."""

    canonical = dict(payload)
    canonical.pop("evidence_fingerprint", None)
    return _canonical_sha256(canonical)


def load_qualification_evidence(path: Path) -> dict[str, Any]:
    """Load and verify the committed Issue #46 machine evidence package."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("qualification evidence schema is invalid")
    expected = qualification_evidence_fingerprint(payload)
    if payload.get("evidence_fingerprint") != expected:
        raise ValueError("qualification evidence fingerprint is stale")
    return payload


def load_model_manifest(path: Path) -> dict[str, Any]:
    """Validate the immutable offline embedding artifact contract."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "news_story_semantic_qualification_models_v1":
        raise ValueError("qualification model manifest schema_version is invalid")
    runtime = payload.get("runtime_contract")
    models = payload.get("models")
    if not isinstance(runtime, dict) or not isinstance(models, list):
        raise ValueError("qualification model manifest runtime_contract and models are required")
    if runtime.get("library") != "sentence-transformers" or runtime.get("library_version") != "5.2.2":
        raise ValueError("qualification model runtime is not pinned")
    if runtime.get("device") != "cpu" or runtime.get("thread_count") != 2:
        raise ValueError("qualification model runtime must use the two-thread CPU contract")
    if runtime.get("output_dtype") != "float32" or runtime.get("normalize_embeddings") is not True:
        raise ValueError("qualification model vector contract is invalid")
    if runtime.get("offline_cache_required_after_download") is not True:
        raise ValueError("qualification model cache must be offline after download")

    models_by_id: dict[str, dict[str, Any]] = {}
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("qualification model entry must be an object")
        model_id = _bounded_text(model.get("model_id"), field="model_id", maximum=100)
        if model_id in models_by_id:
            raise ValueError(f"duplicate qualification model_id: {model_id}")
        revision = str(model.get("revision") or "")
        weight_sha256 = str(model.get("weight_sha256") or "")
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError(f"qualification model revision is not immutable: {model_id}")
        if re.fullmatch(r"[0-9a-f]{64}", weight_sha256) is None:
            raise ValueError(f"qualification model weight checksum is invalid: {model_id}")
        if int(model.get("weight_bytes") or 0) <= 0:
            raise ValueError(f"qualification model weight size is invalid: {model_id}")
        if int(model.get("dimension") or 0) <= 0 or int(model.get("max_tokens") or 0) <= 0:
            raise ValueError(f"qualification model shape is invalid: {model_id}")
        if model.get("normalization") != "l2" or model.get("pooling") not in {"mean", "cls"}:
            raise ValueError(f"qualification model representation contract is invalid: {model_id}")
        _bounded_text(model.get("license"), field="license", maximum=32)
        _bounded_text(model.get("weight_file"), field="weight_file", maximum=80)
        _bounded_text(model.get("input_contract"), field="input_contract", maximum=120)
        if not isinstance(model.get("input_prefix"), str):
            raise ValueError(f"qualification model input_prefix is invalid: {model_id}")
        models_by_id[model_id] = model
    if set(models_by_id) != REQUIRED_MODEL_IDS:
        raise ValueError("qualification model manifest does not contain the exact Issue #46 models")

    return {
        "schema_version": str(payload["schema_version"]),
        "manifest_sha256": _canonical_sha256(payload),
        "model_ids": sorted(models_by_id),
        "dimensions": {model_id: int(models_by_id[model_id]["dimension"]) for model_id in sorted(models_by_id)},
        "licenses": {model_id: str(models_by_id[model_id]["license"]) for model_id in sorted(models_by_id)},
        "all_revisions_immutable": all(
            re.fullmatch(r"[0-9a-f]{40}", str(model["revision"])) is not None for model in models_by_id.values()
        ),
        "all_weight_checksums_pinned": all(
            re.fullmatch(r"[0-9a-f]{64}", str(model["weight_sha256"])) is not None for model in models_by_id.values()
        ),
        "all_offline_cache_required": bool(runtime["offline_cache_required_after_download"]),
    }


def clean_original_title(value: str, *, maximum_characters: int = 512) -> str:
    """Return the bounded model input while preserving event-defining text."""

    normalized = unicodedata.normalize("NFKC", str(value))
    without_controls = "".join(" " if unicodedata.category(char).startswith("C") else char for char in normalized)
    cleaned = _URL_RE.sub(" ", without_controls)
    cleaned = " ".join(cleaned.split()).strip()
    for _ in range(3):
        stripped = _STRUCTURAL_PREFIX_RE.sub("", cleaned, count=1).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned[:maximum_characters].rstrip()


def model_inputs(path: Path, model_id: str, titles: list[str]) -> list[str]:
    """Apply the frozen per-model symmetric title input contract."""

    load_model_manifest(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    model = next((entry for entry in payload["models"] if entry["model_id"] == model_id), None)
    if model is None:
        raise ValueError(f"qualification model is not declared: {model_id}")
    prefix = str(model["input_prefix"])
    cleaned = [clean_original_title(title) for title in titles]
    if any(not title for title in cleaned):
        raise ValueError("qualification model input becomes empty after cleaning")
    return [f"{prefix}{title}" for title in cleaned]


def embed_corpus_model(
    corpus_path: Path,
    model_manifest_path: Path,
    model_id: str,
    *,
    cache_dir: Path,
    output_path: Path,
    offline: bool,
    prepared_model_dir: Path | None = None,
) -> dict[str, Any]:
    """Embed the frozen corpus with an optional, pinned, CPU-only research runtime."""

    corpus_summary = load_qualification_corpus(corpus_path)
    load_model_manifest(model_manifest_path)
    manifest_payload = json.loads(model_manifest_path.read_text(encoding="utf-8"))
    model = next((entry for entry in manifest_payload["models"] if entry["model_id"] == model_id), None)
    if model is None:
        raise ValueError(f"qualification model is not declared: {model_id}")
    try:
        import numpy as np
        import torch
        from sentence_transformers import SentenceTransformer
    except ModuleNotFoundError as exc:
        raise RuntimeError("optional qualification runtime is absent; run with sentence-transformers==5.2.2") from exc

    torch.set_num_threads(2)
    with suppress(RuntimeError):
        torch.set_num_interop_threads(1)
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    items = sorted(payload["items"], key=lambda item: str(item["item_id"]))
    item_ids = [str(item["item_id"]) for item in items]
    inputs = model_inputs(model_manifest_path, model_id, [str(item["original_title"]) for item in items])
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    load_started = time.perf_counter()
    model_source = prepared_model_dir.resolve() if prepared_model_dir is not None else model_id
    encoder = SentenceTransformer(
        str(model_source),
        revision=None if prepared_model_dir is not None else str(model["revision"]),
        cache_folder=str(cache_dir),
        device="cpu",
        local_files_only=offline or prepared_model_dir is not None,
        trust_remote_code=False,
    )
    load_seconds = time.perf_counter() - load_started
    dimension = int(encoder.get_sentence_embedding_dimension() or 0)
    if dimension != int(model["dimension"]):
        raise RuntimeError(f"qualification model dimension mismatch: {model_id}")
    if int(encoder.max_seq_length) != int(model["max_tokens"]):
        raise RuntimeError(f"qualification model max token contract mismatch: {model_id}")

    embed_started = time.perf_counter()
    vectors = encoder.encode(
        inputs,
        batch_size=8 if dimension >= 1_024 else 16,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
        precision="float32",
    )
    embed_seconds = time.perf_counter() - embed_started
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.shape != (len(items), dimension) or matrix.dtype != np.float32:
        raise RuntimeError(f"qualification model vector shape/dtype mismatch: {model_id}")
    norms = np.linalg.norm(matrix, axis=1)
    if not np.all(np.isfinite(matrix)) or not np.allclose(norms, 1.0, atol=1e-5):
        raise RuntimeError(f"qualification model vectors are not finite normalized float32: {model_id}")

    if prepared_model_dir is not None:
        weight_candidates = [prepared_model_dir / str(model["weight_file"])]
    else:
        weight_candidates = [
            candidate
            for candidate in cache_dir.rglob(str(model["weight_file"]))
            if str(model["revision"]) in candidate.parts
        ]
    if not weight_candidates:
        raise RuntimeError(f"qualification model pinned weight file is absent from cache: {model_id}")
    weight_path = min(weight_candidates, key=lambda candidate: len(str(candidate)))
    weight_sha256 = _file_sha256(weight_path)
    if weight_sha256 != model["weight_sha256"] or weight_path.stat().st_size != int(model["weight_bytes"]):
        raise RuntimeError(f"qualification model pinned weight checksum/size mismatch: {model_id}")

    np.save(output_path, matrix, allow_pickle=False)
    vector_hash = hashlib.sha256()
    vector_hash.update("\n".join(item_ids).encode())
    vector_hash.update(matrix.tobytes(order="C"))
    peak_rss_raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    peak_rss_bytes = peak_rss_raw if sys.platform == "darwin" else peak_rss_raw * 1_024
    result = {
        "schema_version": "news_story_semantic_vectors_v1",
        "corpus_manifest_sha256": corpus_summary["manifest_sha256"],
        "model_id": model_id,
        "revision": str(model["revision"]),
        "weight_sha256": weight_sha256,
        "item_ids": item_ids,
        "item_count": len(item_ids),
        "dimension": dimension,
        "dtype": "float32",
        "normalized": True,
        "offline": bool(offline),
        "prepared_local_artifact": prepared_model_dir is not None,
        "input_serialization_bytes": len(json.dumps(inputs, ensure_ascii=False, separators=(",", ":")).encode()),
        "vector_bytes": int(matrix.nbytes),
        "vector_fingerprint": vector_hash.hexdigest(),
        "model_load_seconds": round(load_seconds, 6),
        "embedding_seconds": round(embed_seconds, 6),
        "titles_per_second": round(len(item_ids) / embed_seconds, 6),
        "peak_rss_bytes": peak_rss_bytes,
        "runtime": {
            "python": sys.version.split()[0],
            "sentence_transformers": str(sys.modules["sentence_transformers"].__version__),
            "torch": str(torch.__version__),
            "numpy": str(np.__version__),
            "threads": 2,
            "device": "cpu",
        },
    }
    output_path.with_suffix(".json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_v2_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one labelled candidate strictly through the production V2 seam."""

    from tracefold.news.story_projection import NewsStoryFactSnapshot, build_story_projection

    rows = tuple(_qualification_story_row(item, index=index) for index, item in enumerate((left, right)))
    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint=_canonical_sha256(rows),
        evaluation_time_ms=max(int(row["published_at_ms"]) for row in rows) + 1,
        published_material_snapshot_fingerprint=None,
        rows=rows,
    )
    projection = build_story_projection(snapshot)
    story_ids = [str(story["story_id"]) for story in projection.stories]
    accepted = int(projection.diagnostics["story_count"]) == 1
    exact = bool(_comparison_title(str(rows[0]["title"]))) and _comparison_title(
        str(rows[0]["title"])
    ) == _comparison_title(str(rows[1]["title"]))
    candidate_channels: list[str] = []
    if exact and int(projection.diagnostics["exact_membership_count"]) > 0:
        candidate_channels.append("exact_title")
    if int(projection.diagnostics["candidate_pair_count"]) > 0:
        candidate_channels.append("lexical_or_strong_fact")
    rejection_reasons: Counter[str] = Counter()
    for story in projection.stories:
        evidence = story.get("identity_evidence")
        if not isinstance(evidence, Mapping):
            continue
        raw_reasons = evidence.get("rejection_reasons")
        if isinstance(raw_reasons, Mapping):
            rejection_reasons.update({str(reason): int(count) for reason, count in raw_reasons.items()})
    return {
        "authority": "build_story_projection",
        "accepted": accepted,
        "candidate_retrieved": bool(candidate_channels),
        "candidate_channels": candidate_channels,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "duplicate_story_id_count": len(story_ids) - len(set(story_ids)),
        "projection_fingerprint": projection.projection_fingerprint,
    }


def evaluate_v2_corpus(path: Path, *, partitions: set[str] | None = None) -> dict[str, Any]:
    """Evaluate baseline A over frozen labelled pairs without copying V2 rules."""

    corpus_summary = load_qualification_corpus(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items_by_id = {str(item["item_id"]): item for item in payload["items"]}
    selected_pairs = [pair for pair in payload["pairs"] if partitions is None or str(pair["partition"]) in partitions]
    confusion = Counter[str]()
    positive_error_layers = Counter[str]()
    conflict_reasons = Counter[str]()
    positive_candidates = 0
    mandatory_failures = 0
    duplicate_story_id_pairs = 0
    case_results: list[dict[str, Any]] = []
    for pair in selected_pairs:
        observed = evaluate_v2_pair(
            items_by_id[str(pair["left_item_id"])],
            items_by_id[str(pair["right_item_id"])],
        )
        expected_positive = pair["label"] == "same_event"
        duplicate_story_id_pairs += int(observed["duplicate_story_id_count"] > 0)
        predicted_positive = bool(observed["accepted"])
        if expected_positive:
            confusion["tp" if predicted_positive else "fn"] += 1
        else:
            confusion["fp" if predicted_positive else "tn"] += 1
        if expected_positive and observed["candidate_retrieved"]:
            positive_candidates += 1
        error_layer = "none"
        if expected_positive and not predicted_positive:
            error_layer = "false_veto_or_insufficient_pair" if observed["candidate_retrieved"] else "candidate_miss"
            positive_error_layers[error_layer] += 1
        elif not expected_positive and predicted_positive:
            error_layer = "false_pair_accept"
        conflict_reasons.update(observed["rejection_reasons"])
        failed = expected_positive != predicted_positive
        if pair["partition"] == "mandatory_regression" and failed:
            mandatory_failures += 1
        case_results.append(
            {
                "case_id": str(pair["case_id"]),
                "partition": str(pair["partition"]),
                "label": str(pair["label"]),
                "candidate_retrieved": bool(observed["candidate_retrieved"]),
                "accepted": predicted_positive,
                "error_layer": error_layer,
                "rejection_reasons": dict(observed["rejection_reasons"]),
            }
        )
    tp = confusion["tp"]
    fp = confusion["fp"]
    fn = confusion["fn"]
    positive_count = tp + fn
    return {
        "algorithm": "A",
        "authority": "build_story_projection",
        "corpus_manifest_sha256": corpus_summary["manifest_sha256"],
        "partitions": sorted(partitions or ALLOWED_CORPUS_PARTITIONS),
        "pair_count": len(selected_pairs),
        "confusion": {key: confusion[key] for key in ("tp", "fp", "tn", "fn")},
        "candidate_recall": _safe_ratio(positive_candidates, positive_count),
        "pair_precision": _safe_ratio(tp, tp + fp),
        "pair_recall": _safe_ratio(tp, positive_count),
        "mandatory_regression_failure_count": mandatory_failures,
        "duplicate_story_id_pair_count": duplicate_story_id_pairs,
        "positive_error_layers": {
            key: positive_error_layers[key] for key in ("candidate_miss", "false_veto_or_insufficient_pair")
        },
        "conflict_reasons": dict(sorted(conflict_reasons.items())),
        "case_results": sorted(case_results, key=lambda row: row["case_id"]),
    }


def evaluate_fact_confidence_pair(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    force_candidate: bool = False,
) -> dict[str, Any]:
    """Evaluate ablation B with generic proper names removed from actor veto authority."""

    story_v2 = _story_v2_module()
    rows = tuple(_qualification_story_row(item, index=index) for index, item in enumerate((left, right)))
    features = [story_v2._extract_features(row, row_index=index) for index, row in enumerate(rows)]
    verified_actors = [
        _verified_actor_keys(str(row["title"]), feature.event_family)
        for row, feature in zip(rows, features, strict=True)
    ]
    incompatible_actor_scripts = (
        verified_actors[0]
        and verified_actors[1]
        and _actor_script(verified_actors[0]) != _actor_script(verified_actors[1])
    )
    if incompatible_actor_scripts:
        verified_actors = [frozenset(), frozenset()]
    corrected = [replace(feature, actors=verified_actors[index]) for index, feature in enumerate(features)]
    atoms = [
        story_v2._Atom(features=feature, row_indices=[index], order=index) for index, feature in enumerate(corrected)
    ]
    exact = bool(corrected[0].comparison_title) and corrected[0].comparison_title == corrected[1].comparison_title
    candidate_pairs = story_v2._candidate_pairs(atoms)
    candidate_channels = ["exact_title"] if exact else []
    if (0, 1) in candidate_pairs:
        candidate_channels.append("v2_lexical_or_recall_fact")
    if force_candidate:
        candidate_channels.append("dense_candidate")
    if not candidate_channels:
        return {
            "algorithm": "B",
            "accepted": False,
            "candidate_retrieved": False,
            "candidate_channels": [],
            "decision_reason": "candidate_miss",
            "verified_actor_roles": [sorted(values) for values in verified_actors],
        }
    cluster = story_v2._Cluster(anchor=atoms[0], atoms=[atoms[0]])
    decision = story_v2._same_event(cluster, atoms[1], rows)
    return {
        "algorithm": "B",
        "accepted": bool(decision.accepted),
        "candidate_retrieved": True,
        "candidate_channels": candidate_channels,
        "decision_reason": str(decision.reason),
        "verified_actor_roles": [sorted(values) for values in verified_actors],
    }


def evaluate_dense_candidate_corpus(
    path: Path,
    dense_neighbors: Mapping[str, list[str]],
    *,
    model_id: str,
    k: int,
    partitions: set[str] | None = None,
) -> dict[str, Any]:
    """Evaluate C: dense candidate union while retaining B as the only decision."""

    corpus_summary = load_qualification_corpus(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items_by_id = {str(item["item_id"]): item for item in payload["items"]}
    selected_pairs = [pair for pair in payload["pairs"] if partitions is None or str(pair["partition"]) in partitions]
    confusion = Counter[str]()
    channel_contribution = Counter[str]()
    decision_reasons = Counter[str]()
    positive_candidates = 0
    mandatory_failures = 0
    slice_counts: dict[str, list[int]] = {}
    case_results: list[dict[str, Any]] = []
    for pair in selected_pairs:
        left_item_id = str(pair["left_item_id"])
        right_item_id = str(pair["right_item_id"])
        dense_retrieved = right_item_id in dense_neighbors.get(left_item_id, []) or left_item_id in dense_neighbors.get(
            right_item_id, []
        )
        observed = evaluate_fact_confidence_pair(
            items_by_id[left_item_id],
            items_by_id[right_item_id],
            force_candidate=dense_retrieved,
        )
        lexical_retrieved = any(
            channel in observed["candidate_channels"] for channel in ("exact_title", "v2_lexical_or_recall_fact")
        )
        retrieved = bool(observed["candidate_retrieved"])
        expected_positive = pair["label"] == "same_event"
        predicted_positive = bool(observed["accepted"])
        if expected_positive:
            confusion["tp" if predicted_positive else "fn"] += 1
            positive_candidates += int(retrieved)
            contribution = (
                "both"
                if lexical_retrieved and dense_retrieved
                else "lexical_only"
                if lexical_retrieved
                else "dense_only"
                if dense_retrieved
                else "missed"
            )
            channel_contribution[contribution] += 1
            slices = [
                "overall",
                f"language_pair:{pair['language_pair']}",
                f"event_family:{pair['event_family']}",
            ]
            if pair.get("long_short") is True:
                slices.append("long_short")
            for slice_name in slices:
                counts = slice_counts.setdefault(slice_name, [0, 0])
                counts[0] += int(retrieved)
                counts[1] += 1
        else:
            confusion["fp" if predicted_positive else "tn"] += 1
        decision_reasons[str(observed["decision_reason"])] += 1
        if pair["partition"] == "mandatory_regression" and expected_positive != predicted_positive:
            mandatory_failures += 1
        case_results.append(
            {
                "case_id": str(pair["case_id"]),
                "label": str(pair["label"]),
                "partition": str(pair["partition"]),
                "lexical_retrieved": lexical_retrieved,
                "dense_retrieved": dense_retrieved,
                "accepted": predicted_positive,
                "decision_reason": str(observed["decision_reason"]),
            }
        )
    tp = confusion["tp"]
    fp = confusion["fp"]
    fn = confusion["fn"]
    positive_count = tp + fn
    return {
        "algorithm": "C",
        "model_id": model_id,
        "k": int(k),
        "candidate_authority": "exact_plus_v2_lexical_plus_dense_top_k",
        "decision_authority": "B_fact_confidence",
        "semantic_direct_accept_count": 0,
        "corpus_manifest_sha256": corpus_summary["manifest_sha256"],
        "partitions": sorted(partitions or ALLOWED_CORPUS_PARTITIONS),
        "pair_count": len(selected_pairs),
        "confusion": {key: confusion[key] for key in ("tp", "fp", "tn", "fn")},
        "candidate_recall": _safe_ratio(positive_candidates, positive_count),
        "candidate_recall_slices": {
            name: _safe_ratio(counts[0], counts[1]) for name, counts in sorted(slice_counts.items())
        },
        "candidate_channel_contribution": {
            key: channel_contribution[key] for key in ("lexical_only", "dense_only", "both", "missed")
        },
        "pair_precision": _safe_ratio(tp, tp + fp),
        "pair_recall": _safe_ratio(tp, positive_count),
        "mandatory_regression_failure_count": mandatory_failures,
        "decision_reasons": dict(sorted(decision_reasons.items())),
        "case_results": sorted(case_results, key=lambda row: row["case_id"]),
    }


def evaluate_deterministic_semantic_pair(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    cosine: float,
    threshold: float,
) -> dict[str, Any]:
    """Evaluate D's single non-exact rule after the one verified-conflict veto."""

    if not math.isfinite(cosine) or not -1.000001 <= cosine <= 1.000001 or not 0.0 <= threshold <= 1.0:
        raise ValueError("qualification semantic cosine/threshold is invalid")
    cosine = min(1.0, max(-1.0, cosine))
    b_result = evaluate_fact_confidence_pair(left, right, force_candidate=True)
    if str(b_result["decision_reason"]).endswith("_conflict"):
        return {
            "algorithm": "D",
            "accepted": False,
            "decision_reason": str(b_result["decision_reason"]),
            "verified_conflict": True,
        }
    left_title = str(left["original_title"])
    right_title = str(right["original_title"])
    left_comparison = _comparison_title(left_title)
    right_comparison = _comparison_title(right_title)
    if left_comparison and left_comparison == right_comparison:
        return {
            "algorithm": "D",
            "accepted": True,
            "decision_reason": "exact_title",
            "verified_conflict": False,
            "b_would_accept": bool(b_result["accepted"]),
            "cosine": round(cosine, 8),
            "jaccard": 1.0,
            "semantic_score": 1.0,
        }
    rows = tuple(_qualification_story_row(item, index=index) for index, item in enumerate((left, right)))
    story_v2 = _story_v2_module()
    features = [story_v2._extract_features(row, row_index=index) for index, row in enumerate(rows)]
    intersection = len(features[0].tokens & features[1].tokens)
    union = len(features[0].tokens | features[1].tokens) or 1
    jaccard = intersection / union
    semantic_score = round(0.8 * cosine + 0.2 * jaccard, 8)
    accepted = semantic_score >= threshold
    return {
        "algorithm": "D",
        "accepted": accepted,
        "decision_reason": "deterministic_semantic_rule" if accepted else "semantic_score_below_threshold",
        "verified_conflict": False,
        "b_would_accept": bool(b_result["accepted"]),
        "cosine": round(cosine, 8),
        "jaccard": round(jaccard, 8),
        "semantic_score": semantic_score,
    }


def evaluate_linear_verifier_pair(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    cosine: float,
    coefficients: Mapping[str, float],
    threshold: float,
) -> dict[str, Any]:
    """Evaluate E's one calibrated linear non-exact verifier."""

    feature_names = {"cosine", "jaccard", "containment", "length_ratio", "cross_language", "shared_strong"}
    if set(coefficients) != {"intercept", *feature_names}:
        raise ValueError("qualification linear verifier coefficient schema is invalid")
    if not math.isfinite(cosine) or not -1.000001 <= cosine <= 1.000001 or not 0.0 <= threshold <= 1.0:
        raise ValueError("qualification linear verifier cosine/threshold is invalid")
    cosine = min(1.0, max(-1.0, cosine))
    b_result = evaluate_fact_confidence_pair(left, right, force_candidate=True)
    if str(b_result["decision_reason"]).endswith("_conflict"):
        return {
            "algorithm": "E",
            "accepted": False,
            "decision_reason": str(b_result["decision_reason"]),
            "verified_conflict": True,
        }
    left_title = str(left["original_title"])
    right_title = str(right["original_title"])
    left_comparison = _comparison_title(left_title)
    right_comparison = _comparison_title(right_title)
    if left_comparison and left_comparison == right_comparison:
        return {
            "algorithm": "E",
            "accepted": True,
            "decision_reason": "exact_title",
            "verified_conflict": False,
            "probability": 1.0,
        }
    features = _linear_pair_features(left, right, cosine=cosine)
    logit = float(coefficients["intercept"]) + sum(
        float(coefficients[name]) * features[name] for name in sorted(feature_names)
    )
    probability = 1.0 / (1.0 + math.exp(-logit)) if logit >= 0 else math.exp(logit) / (1.0 + math.exp(logit))
    accepted = probability >= threshold
    return {
        "algorithm": "E",
        "accepted": accepted,
        "decision_reason": "linear_verifier" if accepted else "linear_probability_below_threshold",
        "verified_conflict": False,
        "probability": round(probability, 8),
        "features": {name: round(features[name], 8) for name in sorted(features)},
    }


def _linear_pair_features(left: Mapping[str, Any], right: Mapping[str, Any], *, cosine: float) -> dict[str, float]:
    rows = tuple(_qualification_story_row(item, index=index) for index, item in enumerate((left, right)))
    story_v2 = _story_v2_module()
    features = [story_v2._extract_features(row, row_index=index) for index, row in enumerate(rows)]
    intersection = len(features[0].tokens & features[1].tokens)
    union = len(features[0].tokens | features[1].tokens) or 1
    minimum = min(len(features[0].tokens), len(features[1].tokens)) or 1
    lengths = sorted(
        (
            len(clean_original_title(str(left["original_title"]))),
            len(clean_original_title(str(right["original_title"]))),
        )
    )
    left_language = str(left.get("language") or _detected_title_language(str(left["original_title"])))
    right_language = str(right.get("language") or _detected_title_language(str(right["original_title"])))
    return {
        "cosine": float(cosine),
        "jaccard": intersection / union,
        "containment": intersection / minimum,
        "length_ratio": lengths[0] / max(lengths[1], 1),
        "cross_language": float(left_language != right_language),
        "shared_strong": min(len(features[0].strong_keys & features[1].strong_keys), 4) / 4,
    }


def _detected_title_language(value: str) -> str:
    return "zh" if re.search(r"[\u3400-\u9fff]", value) else "en"


def fit_linear_verifier(
    path: Path,
    pair_cosines: Mapping[str, float],
    *,
    c_value: float,
) -> dict[str, Any]:
    """Fit E deterministically on train only, without a production ML dependency."""

    if not math.isfinite(c_value) or c_value <= 0:
        raise ValueError("qualification linear verifier C must be positive")
    corpus_summary = load_qualification_corpus(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items_by_id = {str(item["item_id"]): item for item in payload["items"]}
    feature_names = (
        "containment",
        "cosine",
        "cross_language",
        "jaccard",
        "length_ratio",
        "shared_strong",
    )
    rows: list[list[float]] = []
    labels: list[float] = []
    train_cases: list[dict[str, Any]] = []
    excluded_conflicts = 0
    excluded_exact = 0
    for pair in payload["pairs"]:
        if pair["partition"] != "train":
            continue
        case_id = str(pair["case_id"])
        if case_id not in pair_cosines:
            raise ValueError(f"qualification pair cosine is missing: {case_id}")
        left = items_by_id[str(pair["left_item_id"])]
        right = items_by_id[str(pair["right_item_id"])]
        conflict = evaluate_fact_confidence_pair(left, right, force_candidate=True)
        if str(conflict["decision_reason"]).endswith("_conflict"):
            excluded_conflicts += 1
            continue
        left_exact = _comparison_title(str(left["original_title"]))
        right_exact = _comparison_title(str(right["original_title"]))
        if left_exact and left_exact == right_exact:
            excluded_exact += 1
            continue
        features = _linear_pair_features(left, right, cosine=float(pair_cosines[case_id]))
        rows.append([1.0, *(features[name] for name in feature_names)])
        labels.append(float(pair["label"] == "same_event"))
        train_cases.append(
            {
                "case_id": case_id,
                "label": str(pair["label"]),
                "cosine": round(float(pair_cosines[case_id]), 8),
            }
        )
    if not rows or len(set(labels)) != 2:
        raise ValueError("qualification linear verifier train data needs both labels")

    weights = _fit_logistic_newton(rows, labels, l2_penalty=1.0 / c_value)
    coefficients = {
        "intercept": round(weights[0], 12),
        **{name: round(weights[index + 1], 12) for index, name in enumerate(feature_names)},
    }
    return {
        "algorithm": "E",
        "fit_partitions": ["train"],
        "fit_method": "deterministic_newton_logistic_l2",
        "c_value": float(c_value),
        "train_case_count": len(rows),
        "train_positive_count": int(sum(labels)),
        "excluded_verified_conflict_count": excluded_conflicts,
        "excluded_exact_count": excluded_exact,
        "coefficients": coefficients,
        "corpus_manifest_sha256": corpus_summary["manifest_sha256"],
        "train_fingerprint": _canonical_sha256(train_cases),
    }


def _fit_logistic_newton(
    rows: list[list[float]],
    labels: list[float],
    *,
    l2_penalty: float,
) -> list[float]:
    width = len(rows[0])
    weights = [0.0] * width
    for _ in range(100):
        gradient = [0.0] * width
        hessian = [[0.0] * width for _ in range(width)]
        for row, label in zip(rows, labels, strict=True):
            logit = sum(weight * value for weight, value in zip(weights, row, strict=True))
            probability = 1.0 / (1.0 + math.exp(-logit)) if logit >= 0 else math.exp(logit) / (1.0 + math.exp(logit))
            curvature = max(probability * (1.0 - probability), 1e-12)
            for left_index in range(width):
                gradient[left_index] += row[left_index] * (label - probability)
                for right_index in range(width):
                    hessian[left_index][right_index] += curvature * row[left_index] * row[right_index]
        for index in range(1, width):
            gradient[index] -= l2_penalty * weights[index]
            hessian[index][index] += l2_penalty
        delta = _solve_linear_system(hessian, gradient)
        weights = [weight + step for weight, step in zip(weights, delta, strict=True)]
        if max(abs(step) for step in delta) < 1e-10:
            break
    return weights


def _solve_linear_system(matrix: list[list[float]], values: list[float]) -> list[float]:
    augmented = [[*row, value] for row, value in zip(matrix, values, strict=True)]
    width = len(values)
    for column in range(width):
        pivot = max(range(column, width), key=lambda index: abs(augmented[index][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("qualification linear verifier fit is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        augmented[column] = [value / pivot_value for value in augmented[column]]
        for row_index in range(width):
            if row_index == column:
                continue
            factor = augmented[row_index][column]
            augmented[row_index] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row_index], augmented[column], strict=True)
            ]
    return [augmented[index][-1] for index in range(width)]


def evaluate_fact_confidence_corpus(path: Path, *, partitions: set[str] | None = None) -> dict[str, Any]:
    """Evaluate ablation B over the frozen pairs."""

    corpus_summary = load_qualification_corpus(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items_by_id = {str(item["item_id"]): item for item in payload["items"]}
    selected_pairs = [pair for pair in payload["pairs"] if partitions is None or str(pair["partition"]) in partitions]
    confusion = Counter[str]()
    positive_error_layers = Counter[str]()
    decision_reasons = Counter[str]()
    positive_candidates = 0
    mandatory_failures = 0
    case_results: list[dict[str, Any]] = []
    for pair in selected_pairs:
        observed = evaluate_fact_confidence_pair(
            items_by_id[str(pair["left_item_id"])],
            items_by_id[str(pair["right_item_id"])],
        )
        expected_positive = pair["label"] == "same_event"
        predicted_positive = bool(observed["accepted"])
        if expected_positive:
            confusion["tp" if predicted_positive else "fn"] += 1
        else:
            confusion["fp" if predicted_positive else "tn"] += 1
        if expected_positive and observed["candidate_retrieved"]:
            positive_candidates += 1
        error_layer = "none"
        if expected_positive and not predicted_positive:
            error_layer = "false_veto_or_insufficient_pair" if observed["candidate_retrieved"] else "candidate_miss"
            positive_error_layers[error_layer] += 1
        elif not expected_positive and predicted_positive:
            error_layer = "false_pair_accept"
        decision_reasons[str(observed["decision_reason"])] += 1
        if pair["partition"] == "mandatory_regression" and expected_positive != predicted_positive:
            mandatory_failures += 1
        case_results.append(
            {
                "case_id": str(pair["case_id"]),
                "partition": str(pair["partition"]),
                "label": str(pair["label"]),
                "candidate_retrieved": bool(observed["candidate_retrieved"]),
                "accepted": predicted_positive,
                "error_layer": error_layer,
                "decision_reason": str(observed["decision_reason"]),
            }
        )
    tp = confusion["tp"]
    fp = confusion["fp"]
    fn = confusion["fn"]
    positive_count = tp + fn
    return {
        "algorithm": "B",
        "authority": "shadow_fact_confidence_replacement",
        "corpus_manifest_sha256": corpus_summary["manifest_sha256"],
        "partitions": sorted(partitions or ALLOWED_CORPUS_PARTITIONS),
        "pair_count": len(selected_pairs),
        "confusion": {key: confusion[key] for key in ("tp", "fp", "tn", "fn")},
        "candidate_recall": _safe_ratio(positive_candidates, positive_count),
        "pair_precision": _safe_ratio(tp, tp + fp),
        "pair_recall": _safe_ratio(tp, positive_count),
        "mandatory_regression_failure_count": mandatory_failures,
        "positive_error_layers": {
            key: positive_error_layers[key] for key in ("candidate_miss", "false_veto_or_insufficient_pair")
        },
        "decision_reasons": dict(sorted(decision_reasons.items())),
        "case_results": sorted(case_results, key=lambda row: row["case_id"]),
    }


def _verified_actor_keys(title: str, event_family: str) -> frozenset[str]:
    if event_family == "filing":
        row = _qualification_story_row({"item_id": "actor-probe", "original_title": title}, index=0)
        return _story_v2_module()._extract_features(row, row_index=0).actors
    cleaned = clean_original_title(title)
    latin = re.match(
        r"^([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,3})\s+"
        r"(?:says?|den(?:y|ies)|rejects?|approves?|announces?|reports?|attacks?|strikes?|"
        r"buys?|sells?|acquires?|invests?|is\s+in\s+talks\s+to\s+invest)\b",
        cleaned,
        re.IGNORECASE,
    )
    if latin:
        actor = " ".join(latin.group(1).casefold().split())
        return frozenset({actor})
    cjk = re.match(r"^([\u3400-\u9fff]{2,20})(?:正|正在|宣布|表示|否认|批准|拒绝|投资|收购)", cleaned)
    if cjk:
        return frozenset({cjk.group(1)})
    return frozenset()


def _actor_script(values: frozenset[str]) -> str:
    return "cjk" if any(re.search(r"[\u3400-\u9fff]", value) for value in values) else "latin"


def exact_cosine_resource_plan(*, item_count: int, dimension: int, k: int, block_size: int) -> dict[str, Any]:
    """Describe the real exact-search work without pretending top-k changes O(n²d)."""

    if min(item_count, dimension, k, block_size) <= 0:
        raise ValueError("qualification resource dimensions must be positive")
    bounded_k = min(k, max(item_count - 1, 0))
    return {
        "item_count": item_count,
        "dimension": dimension,
        "k": bounded_k,
        "block_size": block_size,
        "vector_bytes": item_count * dimension * 4,
        "pair_comparisons": item_count * max(item_count - 1, 0),
        "dot_product_multiply_adds": item_count * max(item_count - 1, 0) * dimension,
        "full_score_matrix_bytes": item_count * item_count * 4,
        "bounded_score_block_bytes": min(block_size, item_count) * item_count * 4,
        "output_neighbor_slots": item_count * bounded_k,
        "k_does_not_reduce_pair_comparisons": True,
    }


def benchmark_exact_cosine_resource(
    *, item_count: int, dimension: int, k: int, block_size: int, seed: int = 46
) -> dict[str, Any]:
    """Execute one deterministic synthetic O(n²d) exact-search probe."""

    try:
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("optional qualification resource runtime requires numpy") from exc
    plan = exact_cosine_resource_plan(
        item_count=item_count,
        dimension=dimension,
        k=k,
        block_size=block_size,
    )
    generator = np.random.default_rng(seed)
    vectors = generator.standard_normal((item_count, dimension), dtype=np.float32)
    item_ids = [f"resource-item-{index:05d}" for index in range(item_count)]
    started = time.perf_counter()
    neighbors = exact_cosine_top_k(item_ids, vectors, k=k, block_size=block_size)
    elapsed = time.perf_counter() - started
    peak_rss_raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    peak_rss_bytes = peak_rss_raw if sys.platform == "darwin" else peak_rss_raw * 1_024
    ordering = [
        (item_id, tuple(candidate["item_id"] for candidate in neighbors[item_id])) for item_id in sorted(neighbors)
    ]
    return {
        **plan,
        "seed": seed,
        "elapsed_seconds": round(elapsed, 6),
        "peak_rss_bytes": peak_rss_bytes,
        "neighbor_ordering_fingerprint": _canonical_sha256(ordering),
        "runtime": {
            "python": sys.version.split()[0],
            "numpy": str(np.__version__),
            "platform": sys.platform,
        },
    }


def exact_cosine_top_k(
    item_ids: list[str],
    vectors: list[list[float]] | Any,
    *,
    k: int,
    block_size: int,
    score_decimal_places: int = 8,
) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic exact cosine neighbors without a full distance matrix."""

    if len(item_ids) != len(vectors) or len(set(item_ids)) != len(item_ids):
        raise ValueError("exact cosine item/vector cardinality or identity is invalid")
    if block_size <= 0 or k <= 0:
        raise ValueError("exact cosine k and block_size must be positive")
    if len(item_ids) <= 1:
        return {item_id: [] for item_id in sorted(item_ids)}
    bounded_k = min(k, len(item_ids) - 1)
    order = sorted(range(len(item_ids)), key=lambda index: item_ids[index])
    ordered_ids = [item_ids[index] for index in order]
    ordered_vectors = [vectors[index] for index in order]
    try:
        import numpy as np
    except ModuleNotFoundError:
        return _exact_cosine_top_k_python(
            ordered_ids,
            ordered_vectors,
            k=bounded_k,
            score_decimal_places=score_decimal_places,
        )

    matrix = np.asarray(ordered_vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != len(ordered_ids):
        raise ValueError("exact cosine vectors must form one two-dimensional matrix")
    norms = np.linalg.norm(matrix, axis=1)
    if not np.all(np.isfinite(matrix)) or np.any(norms == 0):
        raise ValueError("exact cosine vectors must be finite and non-zero")
    normalized = matrix / norms[:, None]
    output: dict[str, list[dict[str, Any]]] = {}
    for block_start in range(0, len(ordered_ids), block_size):
        block_end = min(block_start + block_size, len(ordered_ids))
        scores = np.round(normalized[block_start:block_end] @ normalized.T, score_decimal_places)
        for block_offset, item_index in enumerate(range(block_start, block_end)):
            row = scores[block_offset]
            row[item_index] = -np.inf
            threshold = np.partition(row, -bounded_k)[-bounded_k]
            higher = np.flatnonzero(row > threshold).tolist()
            higher.sort(key=lambda index: (-float(row[index]), ordered_ids[index]))
            tied = np.flatnonzero(row == threshold).tolist()
            tied.sort(key=lambda index: ordered_ids[index])
            selected = [*higher, *tied[: bounded_k - len(higher)]]
            output[ordered_ids[item_index]] = [
                {"item_id": ordered_ids[index], "score": float(row[index])} for index in selected
            ]
    return output


def _exact_cosine_top_k_python(
    item_ids: list[str],
    vectors: list[list[float]],
    *,
    k: int,
    score_decimal_places: int,
) -> dict[str, list[dict[str, Any]]]:
    normalized: list[list[float]] = []
    for vector in vectors:
        if not vector or not all(math.isfinite(float(value)) for value in vector):
            raise ValueError("exact cosine vectors must be finite and non-empty")
        norm = math.sqrt(sum(float(value) * float(value) for value in vector))
        if norm == 0:
            raise ValueError("exact cosine vectors must be non-zero")
        normalized.append([float(value) / norm for value in vector])
    if len({len(vector) for vector in normalized}) != 1:
        raise ValueError("exact cosine vectors must have one dimension")
    output: dict[str, list[dict[str, Any]]] = {}
    for left_index, left_id in enumerate(item_ids):
        candidates = []
        for right_index, right_id in enumerate(item_ids):
            if left_index == right_index:
                continue
            dot_product = sum(a * b for a, b in zip(normalized[left_index], normalized[right_index], strict=True))
            score = round(dot_product, score_decimal_places)
            candidates.append({"item_id": right_id, "score": score})
        output[left_id] = sorted(candidates, key=lambda row: (-float(row["score"]), str(row["item_id"])))[:k]
    return output


def neighbors_from_vector_artifact(
    corpus_path: Path,
    vector_path: Path,
    *,
    k: int,
    block_size: int,
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Build partition-isolated top-k neighbors from one frozen vector artifact."""

    try:
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("optional qualification runtime requires numpy") from exc
    corpus_summary = load_qualification_corpus(corpus_path)
    metadata_path = vector_path.with_suffix(".json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("corpus_manifest_sha256") != corpus_summary["manifest_sha256"]:
        raise ValueError("qualification vector artifact corpus fingerprint is stale")
    item_ids = [str(item_id) for item_id in metadata.get("item_ids", [])]
    matrix = np.load(vector_path, allow_pickle=False, mmap_mode="r")
    if matrix.shape != (len(item_ids), int(metadata.get("dimension") or 0)):
        raise ValueError("qualification vector artifact shape is invalid")
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    partition_by_item: dict[str, str] = {}
    for pair in payload["pairs"]:
        for field in ("left_item_id", "right_item_id"):
            item_id = str(pair[field])
            partition = str(pair["partition"])
            previous = partition_by_item.setdefault(item_id, partition)
            if previous != partition:
                raise ValueError(f"qualification vector item crosses partitions: {item_id}")
    index_by_item = {item_id: index for index, item_id in enumerate(item_ids)}
    if set(index_by_item) != set(partition_by_item):
        raise ValueError("qualification vector item identities do not match the corpus")

    started = time.perf_counter()
    neighbors: dict[str, list[str]] = {}
    partition_sizes: dict[str, int] = {}
    for partition in sorted(set(partition_by_item.values())):
        partition_ids = sorted(item_id for item_id, value in partition_by_item.items() if value == partition)
        partition_sizes[partition] = len(partition_ids)
        partition_vectors = matrix[[index_by_item[item_id] for item_id in partition_ids]]
        ranked = exact_cosine_top_k(partition_ids, partition_vectors, k=k, block_size=block_size)
        neighbors.update(
            {item_id: [str(candidate["item_id"]) for candidate in candidates] for item_id, candidates in ranked.items()}
        )
    elapsed = time.perf_counter() - started
    ordering_fingerprint = _canonical_sha256(neighbors)
    return neighbors, {
        "model_id": str(metadata["model_id"]),
        "vector_fingerprint": str(metadata["vector_fingerprint"]),
        "k": int(k),
        "block_size": int(block_size),
        "partition_sizes": partition_sizes,
        "exact_search_seconds": round(elapsed, 6),
        "neighbor_ordering_fingerprint": ordering_fingerprint,
    }


def pair_cosines_from_vector_artifact(corpus_path: Path, vector_path: Path) -> dict[str, float]:
    """Return rounded cosine for every labelled pair from normalized frozen vectors."""

    try:
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("optional qualification runtime requires numpy") from exc
    metadata = json.loads(vector_path.with_suffix(".json").read_text(encoding="utf-8"))
    corpus_summary = load_qualification_corpus(corpus_path)
    if metadata.get("corpus_manifest_sha256") != corpus_summary["manifest_sha256"]:
        raise ValueError("qualification vector artifact corpus fingerprint is stale")
    item_ids = [str(item_id) for item_id in metadata["item_ids"]]
    index_by_item = {item_id: index for index, item_id in enumerate(item_ids)}
    matrix = np.load(vector_path, allow_pickle=False, mmap_mode="r")
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    return {
        str(pair["case_id"]): round(
            float(
                np.dot(
                    matrix[index_by_item[str(pair["left_item_id"])]],
                    matrix[index_by_item[str(pair["right_item_id"])]],
                )
            ),
            8,
        )
        for pair in payload["pairs"]
    }


def evaluate_deterministic_semantic_corpus(
    path: Path,
    dense_neighbors: Mapping[str, list[str]],
    pair_cosines: Mapping[str, float],
    *,
    model_id: str,
    k: int,
    threshold: float,
    partitions: set[str] | None = None,
) -> dict[str, Any]:
    """Evaluate D's one non-exact semantic rule over candidate-union pairs."""

    corpus_summary = load_qualification_corpus(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items_by_id = {str(item["item_id"]): item for item in payload["items"]}
    selected_pairs = [pair for pair in payload["pairs"] if partitions is None or str(pair["partition"]) in partitions]
    confusion = Counter[str]()
    positive_candidates = 0
    mandatory_failures = 0
    verified_conflict_merges = 0
    decision_reasons = Counter[str]()
    score_labels: list[tuple[float, bool, str]] = []
    slice_confusions: dict[str, Counter[str]] = {}
    case_results: list[dict[str, Any]] = []
    for pair in selected_pairs:
        case_id = str(pair["case_id"])
        if case_id not in pair_cosines:
            raise ValueError(f"qualification pair cosine is missing: {case_id}")
        left_item_id = str(pair["left_item_id"])
        right_item_id = str(pair["right_item_id"])
        dense_retrieved = right_item_id in dense_neighbors.get(left_item_id, []) or left_item_id in dense_neighbors.get(
            right_item_id, []
        )
        b_result = evaluate_fact_confidence_pair(items_by_id[left_item_id], items_by_id[right_item_id])
        candidate_retrieved = bool(b_result["candidate_retrieved"] or dense_retrieved)
        expected_positive = pair["label"] == "same_event"
        positive_candidates += int(expected_positive and candidate_retrieved)
        if candidate_retrieved:
            observed = evaluate_deterministic_semantic_pair(
                items_by_id[left_item_id],
                items_by_id[right_item_id],
                cosine=float(pair_cosines[case_id]),
                threshold=threshold,
            )
            predicted_positive = bool(observed["accepted"])
            decision_reason = str(observed["decision_reason"])
            score = float(observed.get("semantic_score", 0.0 if observed.get("verified_conflict") else 1.0))
            if observed.get("verified_conflict") is True and predicted_positive:
                verified_conflict_merges += 1
        else:
            predicted_positive = False
            decision_reason = "candidate_miss"
            score = 0.0
        key = ("tp" if predicted_positive else "fn") if expected_positive else ("fp" if predicted_positive else "tn")
        confusion[key] += 1
        for slice_name in (
            "overall",
            f"language_pair:{pair['language_pair']}",
            f"event_family:{pair['event_family']}",
            "long_short" if pair.get("long_short") else "not_long_short",
        ):
            slice_confusions.setdefault(slice_name, Counter())[key] += 1
        decision_reasons[decision_reason] += 1
        failed = expected_positive != predicted_positive
        if pair["partition"] == "mandatory_regression" and failed:
            mandatory_failures += 1
        score_labels.append((score, expected_positive, case_id))
        case_results.append(
            {
                "case_id": case_id,
                "partition": str(pair["partition"]),
                "label": str(pair["label"]),
                "candidate_retrieved": candidate_retrieved,
                "cosine": round(float(pair_cosines[case_id]), 8),
                "accepted": predicted_positive,
                "decision_reason": decision_reason,
                "score": round(score, 8),
            }
        )
    tp = confusion["tp"]
    fp = confusion["fp"]
    tn = confusion["tn"]
    fn = confusion["fn"]
    positive_count = tp + fn
    return {
        "algorithm": "D",
        "model_id": model_id,
        "k": int(k),
        "threshold": round(threshold, 8),
        "rule": "verified_conflict reject; exact-title accept; otherwise 0.8*cosine + 0.2*jaccard >= threshold",
        "corpus_manifest_sha256": corpus_summary["manifest_sha256"],
        "partitions": sorted(partitions or ALLOWED_CORPUS_PARTITIONS),
        "pair_count": len(selected_pairs),
        "confusion": {key: confusion[key] for key in ("tp", "fp", "tn", "fn")},
        "candidate_recall": _safe_ratio(positive_candidates, positive_count),
        "pair_precision": _safe_ratio(tp, tp + fp),
        "pair_recall": _safe_ratio(tp, positive_count),
        "false_positive_rate": _safe_ratio(fp, fp + tn),
        "hard_negative_rejection_rate": _safe_ratio(tn, tn + fp),
        "pr_auc": _average_precision(score_labels),
        "brier_score": _brier_score(score_labels),
        "mandatory_regression_failure_count": mandatory_failures,
        "verified_conflict_merge_count": verified_conflict_merges,
        "decision_reasons": dict(sorted(decision_reasons.items())),
        "slices": {name: _confusion_metrics(values) for name, values in sorted(slice_confusions.items())},
        "case_results": sorted(case_results, key=lambda row: row["case_id"]),
    }


def evaluate_linear_verifier_corpus(
    path: Path,
    dense_neighbors: Mapping[str, list[str]],
    pair_cosines: Mapping[str, float],
    *,
    coefficients: Mapping[str, float],
    model_id: str,
    k: int,
    threshold: float,
    partitions: set[str] | None = None,
) -> dict[str, Any]:
    """Evaluate E as the sole non-exact verifier after conflicts and exact title."""

    corpus_summary = load_qualification_corpus(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items_by_id = {str(item["item_id"]): item for item in payload["items"]}
    selected_pairs = [pair for pair in payload["pairs"] if partitions is None or str(pair["partition"]) in partitions]
    confusion = Counter[str]()
    positive_candidates = 0
    mandatory_failures = 0
    verified_conflict_merges = 0
    decision_reasons = Counter[str]()
    score_labels: list[tuple[float, bool, str]] = []
    slice_confusions: dict[str, Counter[str]] = {}
    case_results: list[dict[str, Any]] = []
    for pair in selected_pairs:
        case_id = str(pair["case_id"])
        if case_id not in pair_cosines:
            raise ValueError(f"qualification pair cosine is missing: {case_id}")
        left_item_id = str(pair["left_item_id"])
        right_item_id = str(pair["right_item_id"])
        dense_retrieved = right_item_id in dense_neighbors.get(left_item_id, []) or left_item_id in dense_neighbors.get(
            right_item_id, []
        )
        b_result = evaluate_fact_confidence_pair(items_by_id[left_item_id], items_by_id[right_item_id])
        candidate_retrieved = bool(b_result["candidate_retrieved"] or dense_retrieved)
        expected_positive = pair["label"] == "same_event"
        positive_candidates += int(expected_positive and candidate_retrieved)
        if candidate_retrieved:
            observed = evaluate_linear_verifier_pair(
                items_by_id[left_item_id],
                items_by_id[right_item_id],
                cosine=float(pair_cosines[case_id]),
                coefficients=coefficients,
                threshold=threshold,
            )
            predicted_positive = bool(observed["accepted"])
            decision_reason = str(observed["decision_reason"])
            score = float(observed.get("probability", 0.0))
            verified_conflict_merges += int(observed.get("verified_conflict") is True and predicted_positive)
        else:
            predicted_positive = False
            decision_reason = "candidate_miss"
            score = 0.0
        key = ("tp" if predicted_positive else "fn") if expected_positive else ("fp" if predicted_positive else "tn")
        confusion[key] += 1
        for slice_name in (
            "overall",
            f"language_pair:{pair['language_pair']}",
            f"event_family:{pair['event_family']}",
            "long_short" if pair.get("long_short") else "not_long_short",
        ):
            slice_confusions.setdefault(slice_name, Counter())[key] += 1
        decision_reasons[decision_reason] += 1
        if pair["partition"] == "mandatory_regression" and expected_positive != predicted_positive:
            mandatory_failures += 1
        score_labels.append((score, expected_positive, case_id))
        case_results.append(
            {
                "case_id": case_id,
                "partition": str(pair["partition"]),
                "label": str(pair["label"]),
                "candidate_retrieved": candidate_retrieved,
                "cosine": round(float(pair_cosines[case_id]), 8),
                "accepted": predicted_positive,
                "decision_reason": decision_reason,
                "score": round(score, 8),
            }
        )
    tp = confusion["tp"]
    fp = confusion["fp"]
    tn = confusion["tn"]
    fn = confusion["fn"]
    positive_count = tp + fn
    return {
        "algorithm": "E",
        "model_id": model_id,
        "k": int(k),
        "threshold": round(threshold, 8),
        "rule_order": ["verified_conflict", "exact_title", "linear_verifier"],
        "coefficients": {key: round(float(value), 12) for key, value in sorted(coefficients.items())},
        "corpus_manifest_sha256": corpus_summary["manifest_sha256"],
        "partitions": sorted(partitions or ALLOWED_CORPUS_PARTITIONS),
        "pair_count": len(selected_pairs),
        "confusion": {key: confusion[key] for key in ("tp", "fp", "tn", "fn")},
        "candidate_recall": _safe_ratio(positive_candidates, positive_count),
        "pair_precision": _safe_ratio(tp, tp + fp),
        "pair_recall": _safe_ratio(tp, positive_count),
        "false_positive_rate": _safe_ratio(fp, fp + tn),
        "hard_negative_rejection_rate": _safe_ratio(tn, tn + fp),
        "pr_auc": _average_precision(score_labels),
        "brier_score": _brier_score(score_labels),
        "mandatory_regression_failure_count": mandatory_failures,
        "verified_conflict_merge_count": verified_conflict_merges,
        "decision_reasons": dict(sorted(decision_reasons.items())),
        "slices": {name: _confusion_metrics(values) for name, values in sorted(slice_confusions.items())},
        "case_results": sorted(case_results, key=lambda row: row["case_id"]),
    }


def _average_precision(values: list[tuple[float, bool, str]]) -> float:
    positives = sum(label for _, label, _ in values)
    if positives == 0:
        return 0.0
    ranked = sorted(values, key=lambda row: (-row[0], row[2]))
    true_positives = 0
    precision_sum = 0.0
    for rank, (_, label, _) in enumerate(ranked, start=1):
        if label:
            true_positives += 1
            precision_sum += true_positives / rank
    return round(precision_sum / positives, 6)


def _brier_score(values: list[tuple[float, bool, str]]) -> float:
    if not values:
        return 0.0
    squared = [(min(1.0, max(0.0, score)) - float(label)) ** 2 for score, label, _ in values]
    return round(sum(squared) / len(squared), 6)


def _confusion_metrics(values: Counter[str]) -> dict[str, float | int]:
    tp = values["tp"]
    fp = values["fp"]
    tn = values["tn"]
    fn = values["fn"]
    return {
        "count": tp + fp + tn + fn,
        "precision": _safe_ratio(tp, tp + fp),
        "recall": _safe_ratio(tp, tp + fn),
        "false_positive_rate": _safe_ratio(fp, fp + tn),
    }


def fixed_anchor_closure_metrics(corpus_path: Path, case_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure deterministic fixed-anchor closure over the evaluated labelled windows."""

    load_qualification_corpus(corpus_path)
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    items_by_id = {str(item["item_id"]): item for item in payload["items"]}
    pair_by_case = {str(pair["case_id"]): pair for pair in payload["pairs"]}
    result_by_case = {str(result["case_id"]): result for result in case_results}
    if set(result_by_case) - set(pair_by_case):
        raise ValueError("qualification closure result references unknown cases")
    edges: dict[tuple[str, str], bool] = {}
    groups: dict[str, set[str]] = {}
    verified_conflict_merges = 0
    for case_id, result in result_by_case.items():
        pair = pair_by_case[case_id]
        left_item_id = str(pair["left_item_id"])
        right_item_id = str(pair["right_item_id"])
        edge = tuple(sorted((left_item_id, right_item_id)))
        accepted = bool(result["accepted"])
        edges[edge] = accepted
        reason = str(result.get("decision_reason") or "")
        verified_conflict_merges += int(accepted and reason.endswith("_conflict"))
        group_id = "mandatory_regression" if pair["partition"] == "mandatory_regression" else case_id
        groups.setdefault(group_id, set()).update((left_item_id, right_item_id))

    predicted_cluster_by_item: dict[str, str] = {}
    predicted_clusters: list[list[str]] = []
    blocked_bridge_opportunities = 0
    permutation_stable = True
    anchor_expiry_changed_pairs = 0
    anchor_expiry_compared_pairs = 0
    late_arrival_changed_pairs = 0
    late_arrival_compared_pairs = 0
    for group_id, group_items in sorted(groups.items()):
        ordered = _closure_item_order(group_items, items_by_id)
        clusters = _fixed_anchor_assignment(ordered, edges)
        permuted = _closure_item_order(set(reversed(ordered)), items_by_id)
        reversed_clusters = _fixed_anchor_assignment(permuted, edges)
        permutation_stable &= _partition_fingerprint(clusters) == _partition_fingerprint(reversed_clusters)
        for index, cluster in enumerate(clusters):
            cluster_id = f"{group_id}:{index}"
            predicted_clusters.append(cluster)
            for item_id in cluster:
                predicted_cluster_by_item[item_id] = cluster_id
            anchor = cluster[0]
            for item_id in ordered:
                if item_id in cluster or edges.get(tuple(sorted((anchor, item_id))), False):
                    continue
                if any(edges.get(tuple(sorted((member, item_id))), False) for member in cluster[1:]):
                    blocked_bridge_opportunities += 1
        if len(ordered) > 2:
            original_map = _co_membership_map(clusters)
            without_anchor = ordered[1:]
            expiry_map = _co_membership_map(_fixed_anchor_assignment(without_anchor, edges))
            for pair_key, original_value in original_map.items():
                if ordered[0] in pair_key:
                    continue
                anchor_expiry_compared_pairs += 1
                anchor_expiry_changed_pairs += int(expiry_map.get(pair_key, False) != original_value)
            prefix = ordered[:-1]
            prefix_map = _co_membership_map(_fixed_anchor_assignment(prefix, edges))
            full_map = _co_membership_map(clusters)
            for pair_key, prefix_value in prefix_map.items():
                late_arrival_compared_pairs += 1
                late_arrival_changed_pairs += int(full_map.get(pair_key, False) != prefix_value)

    true_members_by_event: dict[str, set[str]] = {}
    predicted_members_by_cluster: dict[str, set[str]] = {}
    for item_id, predicted_cluster in predicted_cluster_by_item.items():
        true_members_by_event.setdefault(str(items_by_id[item_id]["event_id"]), set()).add(item_id)
        predicted_members_by_cluster.setdefault(predicted_cluster, set()).add(item_id)
    precision_values: list[float] = []
    recall_values: list[float] = []
    for item_id, predicted_cluster in predicted_cluster_by_item.items():
        true_event = str(items_by_id[item_id]["event_id"])
        predicted_members = predicted_members_by_cluster[predicted_cluster]
        true_members = true_members_by_event[true_event]
        overlap = len(predicted_members & true_members)
        precision_values.append(overlap / len(predicted_members))
        recall_values.append(overlap / len(true_members))
    b_precision = sum(precision_values) / len(precision_values) if precision_values else 0.0
    b_recall = sum(recall_values) / len(recall_values) if recall_values else 0.0
    b_f1 = 2 * b_precision * b_recall / (b_precision + b_recall) if b_precision + b_recall else 0.0
    cluster_conflicts = sum(
        len({str(items_by_id[item_id]["event_id"]) for item_id in cluster}) > 1 for cluster in predicted_clusters
    )
    return {
        "closure": "deterministic_fixed_anchor",
        "b_cubed_precision": round(b_precision, 6),
        "b_cubed_recall": round(b_recall, 6),
        "b_cubed_f1": round(b_f1, 6),
        "cluster_conflict_count": cluster_conflicts,
        "verified_conflict_merge_count": verified_conflict_merges,
        "transitive_bridge_merge_count": 0,
        "blocked_bridge_opportunity_count": blocked_bridge_opportunities,
        "max_cluster_size": max((len(cluster) for cluster in predicted_clusters), default=0),
        "cluster_count": len(predicted_clusters),
        "membership_fingerprint": _partition_fingerprint(predicted_clusters),
        "input_permutation_stable": permutation_stable,
        "anchor_expiry_churn": _safe_ratio(anchor_expiry_changed_pairs, anchor_expiry_compared_pairs),
        "late_arrival_churn": _safe_ratio(late_arrival_changed_pairs, late_arrival_compared_pairs),
    }


def _closure_item_order(item_ids: set[str], items_by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(
        item_ids,
        key=lambda item_id: (
            int(items_by_id[item_id].get("published_at_ms") or items_by_id[item_id].get("event_time_order") or 0),
            item_id,
        ),
    )


def _fixed_anchor_assignment(item_ids: list[str], edges: Mapping[tuple[str, str], bool]) -> list[list[str]]:
    clusters: list[list[str]] = []
    for item_id in item_ids:
        accepted = [cluster for cluster in clusters if edges.get(tuple(sorted((cluster[0], item_id))), False)]
        if len(accepted) == 1:
            accepted[0].append(item_id)
        else:
            clusters.append([item_id])
    return clusters


def _co_membership_map(clusters: list[list[str]]) -> dict[tuple[str, str], bool]:
    cluster_by_item = {item_id: index for index, cluster in enumerate(clusters) for item_id in cluster}
    ordered = sorted(cluster_by_item)
    return {
        (left, right): cluster_by_item[left] == cluster_by_item[right]
        for position, left in enumerate(ordered)
        for right in ordered[position + 1 :]
    }


def _partition_fingerprint(clusters: list[list[str]]) -> str:
    partition = sorted(sorted(cluster) for cluster in clusters)
    return _canonical_sha256(partition)


def _safe_ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _qualification_story_row(item: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    item_id = _bounded_text(item.get("item_id"), field="item_id", maximum=200)
    title = _bounded_text(item.get("original_title"), field="original_title", maximum=512)
    published_at_ms = item.get("published_at_ms")
    if published_at_ms is None:
        published_at_ms = 2_000_000_000_000 + index
    return {
        "item_id": item_id,
        "source_id": f"qualification-source-{index}",
        "canonical_url": None,
        "reporting_origin": f"qualification-origin-{index}",
        "title": title,
        "description": "",
        "published_at_ms": int(published_at_ms),
        "title_fingerprint": hashlib.sha256(title.encode()).hexdigest(),
        "tier": 4,
        "source_kind": "opennews",
        "source_position": None,
        "memberships": (),
        "provider_identity": (),
    }


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"qualification corpus {field} is missing or exceeds {maximum} characters")
    return text


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "benchmark_exact_cosine_resource",
    "build_baseline_report",
    "clean_original_title",
    "embed_corpus_model",
    "evaluate_dense_candidate_corpus",
    "evaluate_deterministic_semantic_corpus",
    "evaluate_deterministic_semantic_pair",
    "evaluate_fact_confidence_corpus",
    "evaluate_fact_confidence_pair",
    "evaluate_linear_verifier_corpus",
    "evaluate_linear_verifier_pair",
    "evaluate_v2_corpus",
    "evaluate_v2_pair",
    "exact_cosine_resource_plan",
    "exact_cosine_top_k",
    "fit_linear_verifier",
    "fixed_anchor_closure_metrics",
    "load_model_manifest",
    "load_qualification_corpus",
    "main",
    "model_inputs",
    "neighbors_from_vector_artifact",
    "pair_cosines_from_vector_artifact",
    "run_read_only_qualification",
]


if __name__ == "__main__":
    raise SystemExit(main())
