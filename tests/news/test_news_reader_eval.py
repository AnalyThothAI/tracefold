"""Statistical invariants: holdout separation, fixed sequence, real gold and sample units."""

from __future__ import annotations

import gzip
import json
import math
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.eval_news_reader import (
    answer_provenance,
    answer_record,
    assemble,
    certify,
    certify_sequence,
    clopper_pearson_lower,
    dataset_digest,
    fit,
    fit_cut_sequences,
    load,
    probability_report,
    recorded,
    render_report,
    split_cases,
    volume_report,
)
from scripts.label_news_reader import GUIDE_VERSION
from tracefold.news.notifications.novelty import ReaderNovelty
from tracefold.news.notifications.policy import KIND_FLOOR, PUSHABLE_KINDS, ReaderCalibration
from tracefold.news.notifications.reader import (
    READER_QUESTIONS_IDENTITY,
    InterruptEvidence,
    MaterialityEvidence,
    ReaderJudgment,
    ReportKindEvidence,
)
from tracefold.news.updates.identity import digest


def case(number: int, *, push: str = "push", key: bool = False, day: str | None = None) -> dict[str, Any]:
    return {
        "case_id": str(number),
        "claim_ref": f"cl:{number}",
        "story_id": f"story-{number}",
        "reader_input": {
            "schema_version": "news_reader_input_v3",
            "as_of": day or f"2026-09-{number + 1:02}",
            "claim": {
                "statement": "A new launch was announced",
                "fields": {
                    "subject": "project",
                    "action": "launch",
                    "content_kind": "state_change",
                    "mode": "observation",
                    "phase": "effective",
                },
            },
            "sources": [{"publisher": "fixture", "quote": "A new launch was announced"}],
            "messages": [],
        },
        "message_intents": [],
        "reader_novelty": ReaderNovelty(novelty="unlinked"),
        "label": {"kind": "new_action", "push": push, "key": key, "anchor": "none", "note": "test only"},
        "labeler": "owner",
        "guide_version": GUIDE_VERSION,
        "sampling_design": "uniform",
        "inclusion_probability": 0.5,
    }


def judgment() -> ReaderJudgment:
    return ReaderJudgment(
        status="available",
        backend="native",
        identity="test-only",
        report_kind=ReportKindEvidence(
            value="new_action",
            confidence=0.9,
            probabilities={kind: float(kind == "new_action") for kind in PUSHABLE_KINDS},
        ),
        materiality=MaterialityEvidence(value=2.9, probabilities=(0.0, 0.0, 0.1, 0.9), confidence=0.9),
        interrupt=InterruptEvidence(probabilities=(0.1, 0.9), confidence=0.9),
    )


def test_clopper_pearson_matches_exact_one_sided_reference_values() -> None:
    assert clopper_pearson_lower(0, 0) == 0
    assert clopper_pearson_lower(0, 150) == 0
    assert clopper_pearson_lower(150, 150) == pytest.approx(0.1 ** (1 / 150))
    # Exact binomial inversion, independent R/SciPy reference at alpha .1.
    assert clopper_pearson_lower(90, 100) == pytest.approx(0.8501174076983797)
    assert clopper_pearson_lower(9, 10) == pytest.approx(0.663152276693275)
    with pytest.raises(ValueError, match="binomial_arguments"):
        clopper_pearson_lower(11, 10)


def test_fixed_sequence_stops_on_first_failed_cut_even_if_later_cut_would_pass() -> None:
    rows = [case(i, push="push" if i != 10 else "feed") for i in range(31)]
    scores = {row["case_id"]: (0.995 if i < 10 else 0.96 if i == 10 else 0.8) for i, row in enumerate(rows)}
    result = certify_sequence(rows, scores, target=0.79, minimum=1, cuts=(0.99, 0.95, 0.7), delta=0.1)
    assert result["selected"]["cut"] == 0.99
    assert [value["cut"] for value in result["tested"]] == [0.99, 0.95]
    assert clopper_pearson_lower(30, 31, 0.1) > 0.79


def test_certification_does_not_turn_weights_or_proxy_labels_into_gold_trials() -> None:
    rows = [case(i) for i in range(3)]
    scores = {row["case_id"]: 1.0 for row in rows}
    for row in rows:
        row["inclusion_probability"] = 0.001
    insufficient = certify_sequence(rows, scores, target=0.1, minimum=150, cuts=(0.99,))
    assert insufficient["status"] == "uncalibrated"
    assert insufficient["tested"][0]["owner_labels"] == 3
    rows[0]["labeler"] = "claude:test"
    with pytest.raises(ValueError, match="proxy_labels_cannot_certify"):
        certify_sequence(rows, scores, target=0.1, minimum=1)


def test_repeated_stories_cannot_be_claim_precision_binomial_trials() -> None:
    rows = [case(0), case(1)]
    rows[1]["story_id"] = rows[0]["story_id"]
    with pytest.raises(ValueError, match="independent_story_representatives"):
        certify_sequence(rows, {"0": 1, "1": 1}, target=0.1, minimum=1)


def test_stratified_bounds_allocate_alpha_and_do_not_pool_weighted_counts() -> None:
    rows = [case(i) for i in range(12)]
    for i, row in enumerate(rows):
        row.update(
            sampling_design="stratified", stratum="a" if i < 6 else "b", inclusion_probability=0.1 if i < 6 else 0.9
        )
    result = certify_sequence(rows, {row["case_id"]: 1 for row in rows}, target=0.1, minimum=1, cuts=(0.99,))
    strata = result["selected"]["strata"]
    assert [value["delta"] for value in strata] == [0.025, 0.025]
    assert [value["owner_labels"] for value in strata] == [6, 6]
    assert result["selected"]["lower_bound"] == pytest.approx(clopper_pearson_lower(6, 6, 0.025))
    rows[0]["inclusion_probability"] = 0.2
    with pytest.raises(ValueError, match="constant_within_stratum"):
        certify_sequence(rows, {row["case_id"]: 1 for row in rows}, target=0.1, minimum=1)


@pytest.mark.parametrize("design,probability", [("hard_case", 0.5), ("uniform", None), (None, 0.5)])
def test_unknown_or_difficult_sampling_cannot_certify(design: Any, probability: Any) -> None:
    row = case(0)
    row.update(sampling_design=design, inclusion_probability=probability)
    with pytest.raises(ValueError, match="probability_sample_required"):
        certify_sequence([row], {"0": 1}, target=0.1, minimum=1)


def test_time_story_split_excludes_boundary_story_and_recent_hard_examples() -> None:
    rows = [case(i) for i in range(10)]
    rows[1]["story_id"] = rows[8]["story_id"] = "boundary-story"
    rows[9]["sampling_design"] = "hard_case"
    split = split_cases(rows)
    assert split["fit"] == ["0", "2", "3", "4", "5", "6"]
    assert split["certification"] == ["7"]
    assert split["excluded_story_ids"] == ["boundary-story", "story-9"]


def test_recorded_current_answers_round_trip_and_reject_old_scores_and_identity() -> None:
    row = case(0)
    row["answers"] = {"native": answer_record(judgment(), row["reader_input"], program_identity="test-only")}
    assert (
        answer_record(recorded([row], "native")["0"], row["reader_input"], program_identity="test-only")
        == row["answers"]["native"]
    )
    row["answers"]["native"]["questions_identity"] = "obsolete"
    with pytest.raises(ValueError, match="questions_changed"):
        recorded([row], "native")
    row["answers"]["native"] = {"importance": {"value": 3}}
    with pytest.raises(ValueError, match="historical_scores_require_real_reask"):
        recorded([row], "native")


def test_journal_assembly_binds_frozen_input_backend_model_and_gold_metadata() -> None:
    frozen = case(0)
    frozen.pop("reader_novelty")
    gold = {
        "case_id": "0",
        "reader_input_sha256": digest(frozen["reader_input"]),
        "labeler": "owner",
        "guide_version": GUIDE_VERSION,
        "story_id": "owner-story",
        "label": frozen["label"],
        "inclusion_probability": 0.1,
        "sampling_design": "uniform",
    }
    raw = {"case_id": "0", **answer_record(judgment(), frozen["reader_input"], program_identity="test-only")}
    assembled = assemble([frozen], [gold], {"native": [raw]})
    assert assembled[0]["story_id"] == "owner-story"
    assert assembled[0]["inclusion_probability"] == 0.1
    assert recorded(assembled, "native")["0"].identity == "test-only"
    without_joint_sampling = {
        key: value for key, value in gold.items() if key not in {"inclusion_probability", "sampling_design"}
    }
    unknown = assemble([frozen], [without_joint_sampling], {"native": [raw]})
    assert unknown[0]["inclusion_probability"] is None
    assert unknown[0]["sampling_design"] == "unknown"
    old_guide = {**gold, "guide_version": "prior-guide"}
    with pytest.raises(ValueError, match="owner_guide_changed"):
        assemble([frozen], [old_guide], {"native": [raw]})
    proxy = {**gold, "labeler": "claude:test", "label": {**gold["label"], "push": "feed"}}
    override = assemble([frozen], [proxy, without_joint_sampling], {"native": [raw]})
    assert override[0]["labeler"] == "owner"
    assert override[0]["label"]["push"] == "push"
    assert override[0]["inclusion_probability"] is None
    for field, value, error in (
        ("input_sha256", "changed", "journal_input_changed"),
        ("requested_backend", "generated", "questions_or_backend_changed"),
        ("questions_identity", "obsolete", "questions_or_backend_changed"),
    ):
        changed = {**raw, field: value}
        with pytest.raises(ValueError, match=error):
            assemble([frozen], [gold], {"native": [changed]})
    broken_gold = {**gold, "reader_input_sha256": "another-input"}
    with pytest.raises(ValueError, match="label_input_changed"):
        assemble([frozen], [broken_gold], {"native": [raw]})
    second = case(1)
    second["answers"] = {
        "native": answer_record(
            judgment().model_copy(update={"identity": "other-model"}),
            second["reader_input"],
            program_identity="test-only",
        )
    }
    with pytest.raises(ValueError, match="single_model_adapter_identity"):
        recorded([*assembled, second], "native")


def test_cut_candidates_use_fitting_oof_scores_and_holdout_count_without_labels() -> None:
    predictions = [{"p_push": i / 100, "p_key": (i + 1) / 101} for i in range(100)]
    sequences = fit_cut_sequences(predictions, 300)
    assert max(sequences["push"]) <= 0.5
    assert sequences["push"] == sorted(sequences["push"], reverse=True)
    assert len(sequences["push"]) <= 11
    # Exact support boundary with a large tied low-score block must include
    # the stricter high-score candidate, rather than beginning below that block.
    tied = [{"p_push": 0.99, "p_key": 0.99 if i >= 700 else 0.0004} for i in range(840)]
    assert fit_cut_sequences(tied, 360)["key"][0] == 0.99


def test_frozen_certification_candidate_cannot_change_dataset_or_split() -> None:
    rows = [case(i) for i in range(10)]
    for row in rows:
        row["answers"] = {"native": answer_record(judgment(), row["reader_input"], program_identity="test-only")}
    predictions = [{"case_id": str(i), "p_push": 0.8, "p_key": 0.7} for i in range(7)]
    artifact = {
        "phase": "fit",
        "backend": "native",
        "dataset_sha256": dataset_digest(rows),
        "split": split_cases(rows),
        "questions_identity": READER_QUESTIONS_IDENTITY,
        "eligibility_table_sha256": digest(PUSHABLE_KINDS),
        "kind_floor": KIND_FLOOR,
        "cut_sequence": fit_cut_sequences(predictions, 3),
        "oof_predictions": predictions,
        "answer_provenance": answer_provenance(rows, recorded(rows, "native"), "native"),
        "guide_versions": [GUIDE_VERSION],
        "calibration": {"materiality_floor": 2, "push_coefficients": [0, 0, 0, 0], "key_coefficients": [0, 0, 0]},
    }
    original = deepcopy(artifact)
    artifact["split"]["certification"].append("0")
    with pytest.raises(ValueError, match="certification_split_changed"):
        certify(rows, artifact)
    changed = deepcopy(rows)
    changed[0]["label"]["key"] = True
    with pytest.raises(ValueError, match="frozen_dataset_changed"):
        certify(changed, original)
    # No paired baseline and one label class => cannot manufacture AUC evidence.
    uncalibrated = certify(rows, original)
    assert uncalibrated["certification_status"] == "uncalibrated"
    assert uncalibrated["certification_failure"] == "discrimination_or_paired_baseline_unverified"


def test_probability_report_and_volume_are_descriptive_not_cut_selection() -> None:
    # Single class needs no optional fitting dependency and slope remains unknown.
    report = probability_report([1, 1], [0.8, 0.9], [1, 1])
    assert report["auc"] is None and report["calibration_slope"] is None
    assert report["brier"] == pytest.approx(0.025)
    assert report["log_loss"] == pytest.approx(-math.log(0.72) / 2)
    rows = [case(i, day="2026-09-01") for i in range(3)]
    rows[0]["inclusion_probability"] = 0.1
    volume = volume_report(rows, {"0", "1"}, {"1"})
    assert volume["days"] == {"2026-09-01": {"push": 12, "key": 2}}
    assert volume["guardrail_only"] is True
    assert volume["quantiles"]["push"]["p50"] == 12
    rows[2]["reader_input"]["as_of"] = "2026-09-02"
    volume = volume_report(rows, {"0", "1"}, {"1"})
    assert volume["days"]["2026-09-02"] == {"push": 0, "key": 0}


def test_load_validates_new_labels_and_frozen_novelty(tmp_path: Path) -> None:
    row = case(0)
    row.pop("reader_novelty")
    path = tmp_path / "cases.jsonl.gz"
    path.write_bytes(gzip.compress((json.dumps(row) + "\n").encode()))
    loaded = load(path)
    assert loaded[0]["reader_novelty"].novelty == "unlinked"
    row["label"] = {"verdict": "keep"}
    path.write_bytes(gzip.compress((json.dumps(row) + "\n").encode()))
    with pytest.raises(ValueError, match="current_labels_required"):
        load(path)


def test_report_keeps_uncalibrated_status_explicit() -> None:
    artifact = {
        "backend": "native",
        "dataset_sha256": "test-only",
        "questions_identity": "test-only",
        "guide_versions": ["test-only"],
        "calibration": {"push_cut": None, "key_cut": None},
    }
    before = deepcopy(artifact)
    text = render_report(artifact)
    assert "**uncalibrated**" in text
    assert artifact == before
    assert ReaderCalibration().certification_status == "uncalibrated"


def test_missing_research_dependency_has_explicit_install_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sklearn.model_selection", None)
    with pytest.raises(ValueError, match="install_research_dependency_group"):
        fit([], "native")
