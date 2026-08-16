from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.news_story_semantic_qualification import (
    build_baseline_report,
    clean_original_title,
    embed_corpus_model,
    evaluate_dense_candidate_corpus,
    evaluate_deterministic_semantic_pair,
    evaluate_fact_confidence_corpus,
    evaluate_fact_confidence_pair,
    evaluate_linear_verifier_corpus,
    evaluate_linear_verifier_pair,
    evaluate_v2_corpus,
    evaluate_v2_pair,
    exact_cosine_resource_plan,
    exact_cosine_top_k,
    fit_linear_verifier,
    fixed_anchor_closure_metrics,
    load_model_manifest,
    load_qualification_corpus,
    load_qualification_evidence,
    model_inputs,
    qualification_evidence_fingerprint,
)
from tracefold.news.story_projection import NewsStoryFactSnapshot

CORPUS_PATH = Path("tests/fixtures/news_story_semantic_qualification_corpus_v1.json")
MODEL_MANIFEST_PATH = Path("tests/fixtures/news_story_semantic_qualification_models_v1.json")
EVIDENCE_PATH = Path("docs/research/news-story-semantic-qualification-evidence-v1.json")


def test_qualification_baseline_uses_the_authoritative_story_projection() -> None:
    rows = tuple(
        {
            "item_id": f"item-{index}",
            "source_id": "news-opennews",
            "canonical_url": None,
            "reporting_origin": f"wire-{index}",
            "title": "Central bank holds rates steady",
            "description": "",
            "published_at_ms": 2_000_000_000_000 + index,
            "title_fingerprint": str(index).rjust(64, "0"),
            "tier": 4,
            "source_kind": "opennews",
            "source_position": None,
            "memberships": (),
            "provider_identity": (),
        }
        for index in range(2)
    )
    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint="a" * 64,
        evaluation_time_ms=2_000_000_000_100,
        published_material_snapshot_fingerprint=None,
        rows=rows,
    )

    report = build_baseline_report(
        snapshot=snapshot,
        database_revision="test",
        rss_enabled=False,
    )

    assert report["schema_version"] == "news_story_semantic_qualification_v1"
    assert report["mode"] == "read_only_zero_write"
    assert report["disposition"] == "qualification_incomplete"
    assert report["database_revision"] == "test"
    assert report["rss_enabled"] is False
    assert report["material_snapshot_fingerprint"] == "a" * 64
    assert report["evaluation_time_ms"] == 2_000_000_000_100
    assert report["production_baseline"]["story_count"] == 1
    assert report["production_baseline"]["membership_count"] == 2
    assert report["production_baseline"]["diagnostics"]["exact_membership_count"] == 1
    assert report["ablations"] == {
        "A": {"status": "complete", "authority": "build_story_projection"},
        "B": {"status": "pending"},
        "C": {"status": "pending"},
        "D": {"status": "pending"},
        "E": {"status": "pending"},
    }


def test_committed_qualification_corpus_meets_issue_46_gates() -> None:
    corpus = load_qualification_corpus(CORPUS_PATH)

    assert corpus["schema_version"] == "news_story_semantic_qualification_corpus_v1"
    assert corpus["pair_count"] == 500
    assert corpus["event_count"] >= 60
    assert corpus["positive_pair_count"] >= 150
    assert corpus["hard_negative_pair_count"] >= 250
    assert corpus["zh_en_positive_pair_count"] >= 30
    assert corpus["long_short_positive_pair_count"] >= 30
    assert corpus["mandatory_regression_families"] == ["nvidia_sb_energy", "qatar_iranian_pilots"]
    assert corpus["hard_negative_types"] == [
        "incompatible_amount",
        "incompatible_reporting_period",
        "opposite_action",
        "same_company_different_announcement",
        "same_geopolitical_conflict_different_development",
        "same_location_different_occurrence_time",
        "same_person_different_statement",
        "same_template_different_asset",
    ]
    assert corpus["partitions"] == ["development", "final_holdout", "mandatory_regression", "train"]
    assert len(corpus["manifest_sha256"]) == 64


def test_qualification_corpus_rejects_stale_declared_counts(tmp_path: Path) -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    payload["manifest"]["pair_count"] += 1
    corrupted = tmp_path / "corrupted-corpus.json"
    corrupted.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="declared counts"):
        load_qualification_corpus(corrupted)


def test_committed_evidence_is_reproducible_and_does_not_authorize_v3() -> None:
    first = load_qualification_evidence(EVIDENCE_PATH)
    second = load_qualification_evidence(EVIDENCE_PATH)

    assert first == second
    assert qualification_evidence_fingerprint(first) == qualification_evidence_fingerprint(second)
    assert first["disposition"] == "not_qualified"
    assert first["final_holdout"]["status"] == "sealed_not_executed"
    assert first["future_v3_contract"] is None
    assert first["production_changes"] == []


def test_model_manifest_pins_exact_issue_46_artifacts_and_contracts() -> None:
    manifest = load_model_manifest(MODEL_MANIFEST_PATH)

    assert manifest["model_ids"] == [
        "BAAI/bge-m3",
        "intfloat/multilingual-e5-base",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ]
    assert manifest["dimensions"] == {
        "BAAI/bge-m3": 1024,
        "intfloat/multilingual-e5-base": 768,
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    }
    assert manifest["licenses"] == {
        "BAAI/bge-m3": "MIT",
        "intfloat/multilingual-e5-base": "MIT",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": "Apache-2.0",
    }
    assert manifest["all_revisions_immutable"] is True
    assert manifest["all_weight_checksums_pinned"] is True
    assert manifest["all_offline_cache_required"] is True


def test_clean_original_title_removes_only_structural_noise() -> None:
    assert (
        clean_original_title(
            "\x00ＣＯＩＮＤＥＳＫ：  Nvidia does NOT cut its $3B SB Energy investment  https://example.test/a\n"
        )
        == "Nvidia does NOT cut its $3B SB Energy investment"
    )
    assert (
        clean_original_title(
            "据TheInformation：英伟达正在洽谈向SB Energy投资30亿美元，作为OpenAI数据中心交易的一部分。"
        )
        == "据TheInformation:英伟达正在洽谈向SB Energy投资30亿美元,作为OpenAI数据中心交易的一部分。"
    )


def test_v2_pair_baseline_uses_projection_and_keeps_split_story_ids_unique() -> None:
    exact = evaluate_v2_pair(
        {"item_id": "exact-left", "original_title": "Central bank holds rates steady"},
        {"item_id": "exact-right", "original_title": "Central bank holds rates steady"},
    )
    qatar = evaluate_v2_pair(
        {"item_id": "qatar-short", "original_title": "Qatar Denies Detaining Iranian Pilots - Foreign Ministry"},
        {
            "item_id": "qatar-long",
            "original_title": "Qatar denies detaining Iranian pilots, says it found remains of one",
        },
    )
    far_apart = evaluate_v2_pair(
        {
            "item_id": "far-left",
            "original_title": "Central bank holds rates steady",
            "published_at_ms": 1_000,
        },
        {
            "item_id": "far-right",
            "original_title": "Central bank holds rates steady",
            "published_at_ms": 1_000 + 3 * 24 * 60 * 60 * 1_000,
        },
    )

    assert exact["authority"] == "build_story_projection"
    assert exact["candidate_channels"] == ["exact_title"]
    assert exact["accepted"] is True
    assert qatar["candidate_retrieved"] is True
    assert qatar["accepted"] is False
    assert qatar["rejection_reasons"]["actor_conflict"] >= 1
    assert far_apart["candidate_retrieved"] is True
    assert far_apart["accepted"] is False
    assert far_apart["rejection_reasons"]["event_time_conflict"] == 1
    assert far_apart["duplicate_story_id_count"] == 0


def test_v2_corpus_baseline_classifies_known_failures_by_pipeline_layer() -> None:
    result = evaluate_v2_corpus(CORPUS_PATH, partitions={"mandatory_regression"})

    assert result["algorithm"] == "A"
    assert result["authority"] == "build_story_projection"
    assert result["pair_count"] == 27
    assert result["mandatory_regression_failure_count"] > 0
    assert result["positive_error_layers"]["false_veto_or_insufficient_pair"] > 0
    assert result["conflict_reasons"]["actor_conflict"] > 0


def test_b_fact_confidence_exposes_residual_pair_evidence_after_false_veto() -> None:
    qatar = evaluate_fact_confidence_pair(
        {"item_id": "qatar-short", "original_title": "Qatar Denies Detaining Iranian Pilots - Foreign Ministry"},
        {
            "item_id": "qatar-long",
            "original_title": "Qatar denies detaining Iranian pilots, says it found remains of one",
        },
    )
    opposite = evaluate_fact_confidence_pair(
        {"item_id": "capture", "original_title": "Iran Says Qatar Captured Three of Its Pilots Early in War With U.S."},
        {"item_id": "denial", "original_title": "Qatar denies detaining Iranian pilots, says it found remains of one"},
    )

    assert qatar["algorithm"] == "B"
    assert qatar["verified_actor_roles"] == [["qatar"], ["qatar"]]
    assert qatar["accepted"] is False
    assert qatar["decision_reason"] == "jaccard_below_threshold"
    assert opposite["accepted"] is False
    assert opposite["decision_reason"] == "actor_conflict"


def test_b_corpus_ablation_stays_distinct_and_reports_residual_reasons() -> None:
    result = evaluate_fact_confidence_corpus(CORPUS_PATH, partitions={"mandatory_regression"})

    assert result["algorithm"] == "B"
    assert result["pair_count"] == 27
    assert result["mandatory_regression_failure_count"] > 0
    assert result["decision_reasons"]["jaccard_below_threshold"] > 0
    assert result["candidate_recall"] < 1.0


def test_exact_cosine_top_k_is_block_bounded_and_tie_stable() -> None:
    item_ids = ["item-c", "item-a", "item-d", "item-b"]
    vectors = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]

    first = exact_cosine_top_k(item_ids, vectors, k=2, block_size=1)
    permutation = [1, 3, 0, 2]
    second = exact_cosine_top_k(
        [item_ids[index] for index in permutation],
        [vectors[index] for index in permutation],
        k=2,
        block_size=2,
    )

    assert first == second
    assert [neighbor["item_id"] for neighbor in first["item-c"]] == ["item-a", "item-b"]
    assert all(neighbor["score"] == 1.0 for neighbor in first["item-c"])


def test_exact_cosine_resource_plan_keeps_vectors_separate_from_quadratic_work() -> None:
    plan = exact_cosine_resource_plan(item_count=10_000, dimension=1_024, k=16, block_size=16)

    assert plan["vector_bytes"] == 40_960_000
    assert plan["pair_comparisons"] == 99_990_000
    assert plan["dot_product_multiply_adds"] == 102_389_760_000
    assert plan["full_score_matrix_bytes"] == 400_000_000
    assert plan["bounded_score_block_bytes"] == 640_000
    assert plan["k_does_not_reduce_pair_comparisons"] is True


def test_model_inputs_apply_only_the_pinned_model_specific_prefix() -> None:
    title = "COINDESK: Qatar does not detain 3 Iranian pilots https://example.test/story"

    mini = model_inputs(MODEL_MANIFEST_PATH, "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", [title])
    e5 = model_inputs(MODEL_MANIFEST_PATH, "intfloat/multilingual-e5-base", [title])

    assert mini == ["Qatar does not detain 3 Iranian pilots"]
    assert e5 == ["query: Qatar does not detain 3 Iranian pilots"]


def test_embedding_runtime_is_not_a_production_dependency(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="optional qualification runtime"):
        embed_corpus_model(
            CORPUS_PATH,
            MODEL_MANIFEST_PATH,
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            cache_dir=tmp_path / "model-cache",
            output_path=tmp_path / "vectors.npy",
            offline=True,
        )


def test_c_dense_candidates_only_expand_retrieval_not_acceptance() -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    neighbors: dict[str, set[str]] = {}
    for pair in payload["pairs"]:
        if pair["partition"] != "mandatory_regression":
            continue
        neighbors.setdefault(pair["left_item_id"], set()).add(pair["right_item_id"])
        neighbors.setdefault(pair["right_item_id"], set()).add(pair["left_item_id"])

    result = evaluate_dense_candidate_corpus(
        CORPUS_PATH,
        {item_id: sorted(values) for item_id, values in neighbors.items()},
        model_id="test/deterministic-neighbors",
        k=16,
        partitions={"mandatory_regression"},
    )

    assert result["algorithm"] == "C"
    assert result["decision_authority"] == "B_fact_confidence"
    assert result["candidate_recall"] == 1.0
    assert result["semantic_direct_accept_count"] == 0
    assert result["mandatory_regression_failure_count"] > 0
    closure = fixed_anchor_closure_metrics(CORPUS_PATH, result["case_results"])
    assert closure["transitive_bridge_merge_count"] == 0
    assert closure["verified_conflict_merge_count"] == 0
    assert 0.0 <= closure["b_cubed_precision"] <= 1.0
    assert 0.0 <= closure["b_cubed_recall"] <= 1.0


def test_d_semantic_rule_replaces_jaccard_accept_and_never_overrides_conflict() -> None:
    qatar = evaluate_deterministic_semantic_pair(
        {"item_id": "qatar-short", "original_title": "Qatar Denies Detaining Iranian Pilots - Foreign Ministry"},
        {
            "item_id": "qatar-long",
            "original_title": "Qatar denies detaining Iranian pilots, says it found remains of one",
        },
        cosine=0.95,
        threshold=0.8,
    )
    conflict = evaluate_deterministic_semantic_pair(
        {"item_id": "capture", "original_title": "Iran Says Qatar Captured Three of Its Pilots Early in War With U.S."},
        {"item_id": "denial", "original_title": "Qatar denies detaining Iranian pilots, says it found remains of one"},
        cosine=0.99,
        threshold=0.8,
    )
    replaced = evaluate_deterministic_semantic_pair(
        {"item_id": "fed-long", "original_title": "Fed holds interest rates steady amid inflation concerns"},
        {"item_id": "fed-short", "original_title": "Fed holds rates steady as inflation concerns persist"},
        cosine=0.1,
        threshold=0.8,
    )
    rounded_cosine = evaluate_deterministic_semantic_pair(
        {"item_id": "round-left", "original_title": "One exact event"},
        {"item_id": "round-right", "original_title": "One exact event"},
        cosine=1.0000001,
        threshold=0.8,
    )

    assert qatar["accepted"] is True
    assert qatar["decision_reason"] == "deterministic_semantic_rule"
    assert conflict == {
        "algorithm": "D",
        "accepted": False,
        "decision_reason": "actor_conflict",
        "verified_conflict": True,
    }
    assert replaced["b_would_accept"] is True
    assert replaced["accepted"] is False
    assert rounded_cosine["accepted"] is True
    assert rounded_cosine["cosine"] == 1.0


def test_e_linear_verifier_is_the_only_non_exact_decision_and_keeps_conflict_veto() -> None:
    coefficients = {
        "intercept": -8.0,
        "cosine": 10.0,
        "jaccard": 0.0,
        "containment": 0.0,
        "length_ratio": 0.0,
        "cross_language": 0.0,
        "shared_strong": 0.0,
    }
    qatar = evaluate_linear_verifier_pair(
        {"item_id": "qatar-short", "original_title": "Qatar Denies Detaining Iranian Pilots - Foreign Ministry"},
        {
            "item_id": "qatar-long",
            "original_title": "Qatar denies detaining Iranian pilots, says it found remains of one",
        },
        cosine=0.95,
        coefficients=coefficients,
        threshold=0.5,
    )
    conflict = evaluate_linear_verifier_pair(
        {"item_id": "capture", "original_title": "Iran Says Qatar Captured Three of Its Pilots Early in War With U.S."},
        {"item_id": "denial", "original_title": "Qatar denies detaining Iranian pilots, says it found remains of one"},
        cosine=0.99,
        coefficients=coefficients,
        threshold=0.5,
    )

    assert qatar["accepted"] is True
    assert qatar["decision_reason"] == "linear_verifier"
    assert qatar["probability"] > 0.5
    assert conflict["accepted"] is False
    assert conflict["decision_reason"] == "actor_conflict"
    assert conflict["verified_conflict"] is True


def test_e_fits_only_train_and_evaluates_without_layering_d() -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cosines = {str(pair["case_id"]): (0.9 if pair["label"] == "same_event" else 0.1) for pair in payload["pairs"]}
    neighbors: dict[str, set[str]] = {}
    for pair in payload["pairs"]:
        neighbors.setdefault(str(pair["left_item_id"]), set()).add(str(pair["right_item_id"]))
        neighbors.setdefault(str(pair["right_item_id"]), set()).add(str(pair["left_item_id"]))

    fit = fit_linear_verifier(CORPUS_PATH, cosines, c_value=1.0)
    result = evaluate_linear_verifier_corpus(
        CORPUS_PATH,
        {item_id: sorted(values) for item_id, values in neighbors.items()},
        cosines,
        coefficients=fit["coefficients"],
        model_id="test/separable-cosines",
        k=16,
        threshold=0.5,
        partitions={"development"},
    )

    assert fit["algorithm"] == "E"
    assert fit["fit_partitions"] == ["train"]
    assert fit["train_case_count"] > 0
    assert set(fit["coefficients"]) == {
        "intercept",
        "containment",
        "cosine",
        "cross_language",
        "jaccard",
        "length_ratio",
        "shared_strong",
    }
    assert result["algorithm"] == "E"
    assert result["rule_order"] == ["verified_conflict", "exact_title", "linear_verifier"]
    assert result["pair_count"] > 0
    assert result["verified_conflict_merge_count"] == 0
