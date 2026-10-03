"""Statistical invariants: holdout separation, frozen selection, known selected populations and real gold."""

from __future__ import annotations

import gzip
import json
import math
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from scripts.eval_news_reader import (
    DERIVATION_FIELDS,
    PROTOCOL,
    answer_record,
    applicable,
    assemble,
    candidate_identity,
    census_claims,
    certify,
    certify_sequence,
    check_selection,
    clopper_pearson_lower,
    fit,
    fit_cut_sequences,
    guide_version,
    load,
    owner_frame,
    probability_report,
    recorded,
    register_holdout_use,
    render_report,
    sample_owner,
    split_cases,
    story_frame,
    verify_candidate,
    volume_report,
)
from scripts.news_reader_labeling import GUIDE_VERSION
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

BOUNDARY = 1_000_000


def reader_input(day: str = "2026-10-02", *, statement: str = "A new launch was announced") -> dict[str, Any]:
    return {
        "schema_version": "news_reader_input_v3",
        "as_of": day,
        "claim": {
            "statement": statement,
            "fields": {
                "subject": "project",
                "action": "launch",
                "content_kind": "state_change",
                "mode": "observation",
                "phase": "effective",
            },
        },
        "sources": [{"publisher": "fixture", "quote": statement}],
        "messages": [],
    }


def case(number: int, *, push: str = "push", key: bool = False, day: str | None = None) -> dict[str, Any]:
    return {
        "case_id": str(number),
        "claim_ref": f"cl:{number}",
        "story_id": f"story-{number}",
        "split_story_id": f"story-{number}",
        "reader_input": reader_input(day or f"2026-09-{number + 1:02}"),
        "message_intents": [],
        "reader_novelty": ReaderNovelty(novelty="unlinked"),
        "label": {"kind": "new_action", "push": push, "key": key, "anchor": "none", "note": "test only"},
        "labeler": "claude:test",
        "guide_version": GUIDE_VERSION,
        "sampling_design": "uniform",
        "inclusion_probability": 0.5,
        "reader_applicable": True,
        "pre_reader_reason": "reader_unavailable",
        "deterministic_decision": None,
    }


def judgment(m: float = 0.9, i: float = 0.1) -> ReaderJudgment:
    """With the candidate below, p_push == m and p_key == i: logistic identities on one feature each."""
    return ReaderJudgment(
        status="available",
        backend="native",
        identity="test-only",
        report_kind=ReportKindEvidence(
            value="new_action",
            confidence=0.9,
            probabilities={kind: float(kind == "new_action") for kind in PUSHABLE_KINDS},
        ),
        materiality=MaterialityEvidence(value=1 + m, probabilities=(0.0, 1 - m, m, 0.0), confidence=0.9),
        interrupt=InterruptEvidence(probabilities=(1 - i, i), confidence=0.9),
    )


def census_row(number: int, *, at_ms: int, event: str | None = None, links: list[dict[str, Any]] | None = None):
    return {
        "case_id": f"c{number:04}",
        "claim_ref": f"cl:{number}",
        "event_id": event or f"event-{number}",
        "first_available_at_ms": at_ms,
        "decided_at_ms": at_ms + 10,
        "reader_input": reader_input(statement=f"Claim {number} was announced"),
        "source_texts": [f"Source text of claim {number}"],
        "message_intents": [],
        "links": links or [],
        "receipts": [],
        "reader_applicable": True,
        "pre_reader_reason": "reader_unavailable",
        "deterministic_decision": None,
        "original_decision": "not_notified",
        "inclusion_probability": 1,
    }


def bind_census(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frame = {"frame_id": "census", "unit": "claim_decision", "units": len(rows), "days": ["2026-10-01", "2026-10-02"]}
    for row in rows:
        row["sampling_frame"] = deepcopy(frame)
    return rows


def candidate() -> dict[str, Any]:
    result = {
        "protocol": PROTOCOL,
        "phase": "fit",
        "backend": "native",
        "questions_identity": READER_QUESTIONS_IDENTITY,
        "eligibility_table_sha256": digest(PUSHABLE_KINDS),
        "kind_floor": KIND_FLOOR,
        "guide_version": GUIDE_VERSION,
        "dataset_sha256": "frozen-fitting-dataset",
        "split": {"boundary_ms": BOUNDARY, "fit": [], "certification": []},
        "calibration": asdict(ReaderCalibration(push_coefficients=(0, 0, 1, 0), key_coefficients=(0, 1, 0))),
        "cut_sequence": {"push": [0.9, 0.8, 0.5], "key": [0.9, 0.5]},
        "answer_provenance": {"adapter_identity": "test-only", "served_model": None, "program_identity": "test-only"},
    }
    result["candidate_identity"] = digest(result)
    return result


def scenario(key_stories: int = 20) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """200 strong, 30 medium, 10 key-only and 110 weak single-claim holdout stories, plus one older story."""
    plan = [(0.95, 0.95 if n < key_stories else 0.1) for n in range(200)]
    plan += [(0.85, 0.1)] * 30 + [(0.1, 0.6)] * 10 + [(0.1, 0.1)] * 110
    rows = [census_row(number, at_ms=BOUNDARY + number) for number in range(len(plan))]
    rows.append(census_row(9999, at_ms=BOUNDARY - 5))
    journal = [
        {"case_id": row["case_id"], **answer_record(judgment(m, i), row["reader_input"], program_identity="test-only")}
        for row, (m, i) in zip(rows, [*plan, (0.95, 0.95)], strict=True)
    ]
    return bind_census(rows), journal


def owner_labels(selection: list[dict[str, Any]], *, push: Any, key: Any = lambda _: False) -> list[dict[str, Any]]:
    return [
        {
            "case_id": row["case_id"],
            "story_id": row["story_id"],
            "labeler": "owner",
            "guide_version": row["guide_version"],
            "reader_input_sha256": digest(row["reader_input"]),
            "label": {
                "kind": "new_action",
                "anchor": "none",
                "push": "push" if push(row) else "feed",
                "key": bool(key(row)) and bool(push(row)),
                "note": "",
            },
        }
        for row in selection
    ]


@pytest.fixture
def no_refit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Candidate derivation has its own rebuild test; these tests freeze a deterministic candidate.
    monkeypatch.setattr("scripts.eval_news_reader.verify_candidate", lambda rows, artifact: None)


def test_clopper_pearson_matches_exact_one_sided_reference_values() -> None:
    assert clopper_pearson_lower(0, 0, 0.1) == 0
    assert clopper_pearson_lower(0, 150, 0.1) == 0
    assert clopper_pearson_lower(150, 150, 0.1) == pytest.approx(0.1 ** (1 / 150))
    # SciPy beta.ppf(delta, k, n - k + 1) references.
    assert clopper_pearson_lower(90, 100, 0.1) == pytest.approx(0.8501174076983797)
    assert clopper_pearson_lower(9, 10, 0.1) == pytest.approx(0.663152276693275)
    assert clopper_pearson_lower(170, 200, 0.05) == pytest.approx(0.8020893083170084)
    assert clopper_pearson_lower(202, 244, 0.05) == pytest.approx(0.7831326773928748)
    with pytest.raises(ValueError, match="binomial_arguments"):
        clopper_pearson_lower(11, 10, 0.1)


def test_known_population_census_is_the_plain_one_sided_bound_and_stops_at_first_failure() -> None:
    scores = {f"s{i}": 0.95 if i < 200 else 0.85 if i < 230 else 0.6 for i in range(260)}
    strata = dict.fromkeys(scores, "push_region")
    labels = {case: (i < 170) or (200 <= i < 210) or i >= 230 for i, case in enumerate(scores)}
    result = certify_sequence(
        scores=scores, strata=strata, labels=labels, cuts=(0.9, 0.8, 0.5), target=0.8, minimum=150, delta=0.05
    )
    first, second = result["tested"]
    assert (first["cut"], first["population"], first["positives"]) == (0.9, 200, 170)
    assert first["lower_bound"] == pytest.approx(clopper_pearson_lower(170, 200, 0.05))
    assert first["passed"] and not second["passed"]
    # The fixed sequence never inspects a cut after the first failure.
    assert result["selected"]["cut"] == 0.9 and len(result["tested"]) == 2


def test_sequence_starts_where_the_scored_population_reaches_the_minimum() -> None:
    scores = {f"s{i}": 0.99 if i < 100 else 0.9 for i in range(300)}
    strata = dict.fromkeys(scores, "push_region")
    labels = dict.fromkeys(scores, True)
    result = certify_sequence(
        scores=scores, strata=strata, labels=labels, cuts=(0.95, 0.85), target=0.65, minimum=150, delta=0.05
    )
    assert result["populations"] == {"0.95": 100, "0.85": 300}
    assert result["sequence"] == [0.85] and result["selected"]["cut"] == 0.85
    empty = certify_sequence(
        scores=scores, strata=strata, labels=labels, cuts=(0.95,), target=0.65, minimum=150, delta=0.05
    )
    assert empty["status"] == "uncalibrated" and empty["failure"] == "population_below_minimum"
    assert empty["tested"] == []


def test_stratified_bound_weights_known_sizes_and_unlabelled_strata_count_as_zero() -> None:
    scores = {f"r{i}": 0.95 for i in range(150)} | {f"b{i}": 0.95 for i in range(50)} | {"far": 0.1}
    strata = {case: "rest" if case.startswith("b") or case == "far" else "push_region" for case in scores}
    labels = {f"r{i}": True for i in range(150)} | {f"b{i}": True for i in range(5)}
    result = certify_sequence(
        scores=scores, strata=strata, labels=labels, cuts=(0.9,), target=0.1, minimum=150, delta=0.05
    )
    selected = result["selected"]
    # Two populated strata share the error; the empty key stratum does not consume any.
    assert [row["delta"] for row in selected["strata"]] == [0.025, None, 0.025]
    expected = (150 * clopper_pearson_lower(150, 150, 0.025) + 50 * clopper_pearson_lower(5, 5, 0.025)) / 200
    assert selected["lower_bound"] == pytest.approx(expected)
    assert selected["independent_stories"] == 155
    unlabelled = {case: value for case, value in labels.items() if not case.startswith("b")}
    result = certify_sequence(
        scores=scores, strata=strata, labels=unlabelled, cuts=(0.9,), target=0.1, minimum=150, delta=0.05
    )
    assert result["selected"]["lower_bound"] == pytest.approx(150 * clopper_pearson_lower(150, 150, 0.025) / 200)
    too_few = certify_sequence(
        scores=scores, strata=strata, labels=unlabelled, cuts=(0.9,), target=0.1, minimum=151, delta=0.05
    )
    assert too_few["status"] == "uncalibrated"
    with pytest.raises(ValueError, match="strict_to_loose"):
        certify_sequence(scores=scores, strata=strata, labels=labels, cuts=(0.5, 0.9), target=0.1, minimum=1, delta=0.1)
    with pytest.raises(ValueError, match="outside_frame"):
        certify_sequence(
            scores=scores, strata=strata, labels={"unknown": True}, cuts=(0.9,), target=0.1, minimum=1, delta=0.1
        )


def test_story_frame_joins_events_and_links_and_excludes_stories_reaching_before_the_boundary() -> None:
    rows = [
        census_row(1, at_ms=BOUNDARY + 20, event="shared"),
        census_row(2, at_ms=BOUNDARY + 10, event="shared"),
        census_row(
            3,
            at_ms=BOUNDARY + 30,
            links=[
                {"current_ref": "cl:3", "previous_ref": "cl:4", "relation": "adds_information", "asserted_at_ms": 1}
            ],
        ),
        census_row(4, at_ms=BOUNDARY - 1),
        census_row(5, at_ms=BOUNDARY + 5),
    ]
    frame = story_frame(census_claims(bind_census(rows)), BOUNDARY)
    assert frame["stories"] == 2 and frame["boundary_crossing_stories"] == 1 and frame["holdout_claims"] == 3
    assert sorted(row["case_id"] for row in frame["representatives"].values()) == ["c0002", "c0005"]


def test_census_requires_every_decision_and_keeps_each_claims_first_decision() -> None:
    rows = [census_row(1, at_ms=BOUNDARY), census_row(2, at_ms=BOUNDARY)]
    again = census_row(1, at_ms=BOUNDARY)
    again.update(case_id="later", decided_at_ms=BOUNDARY + 999)
    rows.append(again)
    assert [row["case_id"] for row in census_claims(bind_census(rows))] == ["c0001", "c0002"]
    rows[0]["inclusion_probability"] = 0.5
    with pytest.raises(ValueError, match="complete_claim_census"):
        census_claims(rows)
    missing = bind_census([census_row(1, at_ms=BOUNDARY)])
    del missing[0]["event_id"]
    with pytest.raises(ValueError, match="census_case_fields"):
        census_claims(missing)


def test_owner_sample_freezes_score_strata_before_labels_and_validates_external_selection(no_refit) -> None:
    census, journal = scenario()
    sizes = {"push_region": "all", "key_region": 4, "rest": 11}
    rows, manifest = sample_owner(census, journal, [], candidate(), push_cut=0.8, key_cut=0.5, sizes=sizes, seed=7)
    assert manifest["frame"]["stories"] == 350 and manifest["frame"]["boundary_crossing_stories"] == 0
    assert {name: value["population"] for name, value in manifest["strata"].items()} == {
        "push_region": 230,
        "key_region": 10,
        "rest": 110,
    }
    assert manifest["strata"]["rest"]["inclusion_probability"] == pytest.approx(0.1)
    assert len(rows) == 245 and {row["guide_version"] for row in rows} == {GUIDE_VERSION}
    assert set(rows[0]) == {
        "case_id",
        "claim_ref",
        "reader_input",
        "source_texts",
        "story_id",
        "stratum",
        "inclusion_probability",
        "sampling_design",
        "sampling_unit",
        "sampling_frame",
        "guide_version",
    }
    assert rows == sample_owner(census, journal, [], candidate(), push_cut=0.8, key_cut=0.5, sizes=sizes, seed=7)[0]
    frozen = deepcopy(manifest["selection"])
    _, validated = sample_owner(census, journal, [], candidate(), frozen=frozen)
    assert validated["external_selection"]["seed_reproduces_selection"] is True
    assert validated["selection"]["selected"] == manifest["selection"]["selected"]
    for change, error in (
        (lambda value: value["representatives"]["c0000"].update(p_push=0.5), "selection_scores_changed"),
        (lambda value: value["selected"].update(c0300="push_region"), "selection_stratum_changed"),
        (lambda value: value["sample_sizes"].update(rest=12), "selection_sample_size_changed"),
        (lambda value: value.update(frozen_before_labels=False), "must_precede_owner_labels"),
        (lambda value: value.update(push_cut=0.81), "cut_not_in_candidate_sequence"),
    ):
        changed = deepcopy(frozen)
        change(changed)
        with pytest.raises(ValueError, match=error):
            sample_owner(census, journal, [], candidate(), frozen=changed)


def test_certify_known_population_push_only_when_key_population_is_too_small(no_refit, tmp_path: Path) -> None:
    census, journal = scenario(key_stories=20)
    sizes = {"push_region": "all", "key_region": "all", "rest": 20}
    selection, manifest = sample_owner(census, journal, [], candidate(), push_cut=0.8, key_cut=0.5, sizes=sizes, seed=1)
    labels = owner_labels(selection, push=lambda row: not 180 <= int(row["case_id"][1:]) < 200)
    result = certify(census, journal, [], candidate(), manifest, labels)
    tested = result["push_certificate"]["tested"]
    assert [(row["cut"], row["population"], row["positives"]) for row in tested] == [(0.9, 200, 180), (0.8, 230, 210)]
    assert result["calibration"]["push_cut"] == 0.8 and result["calibration"]["key_cut"] is None
    assert result["certification_status"] == "certified"
    assert result["certification_scope"] == "push only; no claim is key"
    assert result["key_certificate"]["failure"] == "population_below_minimum"
    # The key error is split over the two push cuts the sequence could have certified.
    assert result["key_certificate"]["delta"] == pytest.approx(0.025)
    assert result["volume"]["totals"] == {"push": 230, "key": 0}
    assert result["release_gates"]["statistical_precision"] is True
    assert result["release_gates"]["event_card_daily_replay"] is None and result["release_ready"] is False
    assert result["diagnostics"]["recall"]["recorded_production"] == 0
    ledger = tmp_path / "ledger.jsonl"
    register_holdout_use(ledger, result, certification_dataset_sha256=result["owner_labels_sha256"])
    with pytest.raises(ValueError, match="already_used_by_another_candidate"):
        register_holdout_use(
            ledger, {**result, "candidate_identity": "retuned"}, certification_dataset_sha256="same-gold"
        )


def test_certify_also_certifies_key_with_enough_selected_key_stories(no_refit) -> None:
    census, journal = scenario(key_stories=120)
    sizes = {"push_region": "all", "key_region": "all", "rest": 5}
    selection, manifest = sample_owner(census, journal, [], candidate(), push_cut=0.8, key_cut=0.5, sizes=sizes, seed=1)
    labels = owner_labels(
        selection,
        push=lambda row: int(row["case_id"][1:]) < 230,
        key=lambda row: int(row["case_id"][1:]) < 120,
    )
    result = certify(census, journal, [], candidate(), manifest, labels)
    assert result["calibration"]["push_cut"] == 0.8 and result["calibration"]["key_cut"] == 0.5
    assert [row["cut"] for row in result["key_certificate"]["tested"]] == [0.9, 0.5]
    assert result["certification_scope"] == "push and key"
    assert result["volume"]["totals"] == {"push": 230, "key": 120}


def test_certify_binds_selection_owner_labels_and_guide(no_refit) -> None:
    census, journal = scenario()
    sizes = {"push_region": "all", "key_region": "all", "rest": 5}
    selection, manifest = sample_owner(census, journal, [], candidate(), push_cut=0.8, key_cut=0.5, sizes=sizes, seed=1)
    labels = owner_labels(selection, push=lambda row: True)
    for change, error in (
        (lambda rows, _: rows.pop(), "selected_owner_labels_incomplete"),
        (lambda rows, _: rows[0].update(labeler="claude:proxy"), "proxy_labels_cannot_certify"),
        (lambda rows, _: rows[0].update(guide_version="news_reader_owner_guide_v9:" + "0" * 64), "guide_changed"),
        (lambda rows, _: rows[0].update(story_id="other-story"), "independent_story_representatives"),
        (lambda _, value: value["selection"].update(seed=8), "owner_selection_changed"),
        (lambda _, value: value.update(candidate_identity="another"), "owner_selection_changed"),
    ):
        changed_labels, changed_manifest = deepcopy(labels), deepcopy(manifest)
        change(changed_labels, changed_manifest)
        with pytest.raises(ValueError, match=error):
            certify(census, journal, [], candidate(), changed_manifest, changed_labels)
    changed = deepcopy(journal)
    changed[0]["judgment"]["identity"] = "other-adapter"
    with pytest.raises(ValueError, match="single_model_adapter_identity"):
        owner_frame(census, changed, candidate())


def test_unanswered_or_fixed_rule_representatives_can_never_be_selected(no_refit) -> None:
    census, journal = scenario()
    census[0].update(reader_applicable=False, pre_reader_reason="protected_listing", deterministic_decision="notify")
    journal = [record for record in journal if record["case_id"] != "c0001"]
    frame = owner_frame(census, journal, candidate())
    assert frame["scores"]["c0000"]["reason"] == "fixed_rule" and frame["scores"]["c0001"]["reason"] == "no_answer"
    assert not frame["scores"]["c0000"]["eligible"]
    selection = {
        "push_cut": 0.8,
        "key_cut": 0.5,
        "seed": 1,
        "sample_sizes": {"push_region": "all", "key_region": 0, "rest": 0},
        "representatives": frame["scores"],
        "selected": {"c0000": "push_region"},
        "frozen_before_labels": True,
    }
    with pytest.raises(ValueError, match="selection_stratum_changed"):
        check_selection(selection, frame, candidate())


def test_time_story_split_excludes_boundary_story_and_recent_hard_examples() -> None:
    rows = [case(i) for i in range(10)]
    rows[1]["story_id"] = rows[8]["story_id"] = "boundary-story"
    rows[1]["split_story_id"] = rows[8]["split_story_id"] = "boundary-story"
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


def test_journal_assembly_binds_frozen_input_backend_model_and_one_recorded_guide() -> None:
    frozen = case(0)
    frozen.pop("reader_novelty")
    proxy = {
        "case_id": "0",
        "reader_input_sha256": digest(frozen["reader_input"]),
        "labeler": "claude:test",
        "guide_version": GUIDE_VERSION,
        "story_id": "proxy-story",
        "label": frozen["label"],
        "inclusion_probability": 0.1,
        "sampling_design": "uniform",
    }
    raw = {"case_id": "0", **answer_record(judgment(), frozen["reader_input"], program_identity="test-only")}
    assembled = assemble([frozen], [proxy], {"native": [raw]})
    assert assembled[0]["story_id"] == "proxy-story"
    assert assembled[0]["inclusion_probability"] == 0.1
    assert recorded(assembled, "native")["0"].identity == "test-only"
    # A candidate may be fitted under an earlier guide; its labels must still share one guide.
    earlier = "news_reader_owner_guide_v5:" + "d" * 64
    assert assemble([frozen], [{**proxy, "guide_version": earlier}], {"native": [raw]})[0]["guide_version"] == earlier
    second = {**case(1), "case_id": "1"}
    second.pop("reader_novelty")
    mixed = [proxy, {**proxy, "case_id": "1", "reader_input_sha256": digest(second["reader_input"])}]
    mixed[1]["guide_version"] = earlier
    with pytest.raises(ValueError, match="single_guide_version_required"):
        assemble([frozen, second], mixed, {"native": [raw]})
    for field, value, error in (
        ("input_sha256", "changed", "journal_input_changed"),
        ("requested_backend", "generated", "questions_or_backend_changed"),
        ("questions_identity", "obsolete", "questions_or_backend_changed"),
    ):
        changed = {**raw, field: value}
        with pytest.raises(ValueError, match=error):
            assemble([frozen], [proxy], {"native": [changed]})
    with pytest.raises(ValueError, match="label_input_changed"):
        assemble([frozen], [{**proxy, "reader_input_sha256": "another-input"}], {"native": [raw]})
    other = case(1)
    other["answers"] = {
        "native": answer_record(
            judgment().model_copy(update={"identity": "other-model"}),
            other["reader_input"],
            program_identity="test-only",
        )
    }
    with pytest.raises(ValueError, match="single_model_adapter_identity"):
        recorded([*assembled, other], "native")


def test_guide_version_is_recorded_not_pinned_to_the_current_code_guide() -> None:
    earlier = "news_reader_owner_guide_v5:" + "a" * 64
    assert guide_version([{"guide_version": earlier}, {"guide_version": earlier}]) == earlier
    for rows in ([{"guide_version": earlier}, {"guide_version": GUIDE_VERSION}], [{"guide_version": "prior-guide"}]):
        with pytest.raises(ValueError, match="single_guide_version_required"):
            guide_version(rows)


def test_cut_candidates_use_fitting_oof_scores_and_holdout_count_without_labels() -> None:
    predictions = [{"p_push": i / 100, "p_key": (i + 1) / 101} for i in range(100)]
    sequences = fit_cut_sequences(predictions, 300)
    assert max(sequences["push"]) <= 0.5
    assert sequences["push"] == sorted(sequences["push"], reverse=True)
    assert len(sequences["push"]) <= 11
    tied = [{"p_push": 0.99, "p_key": 0.99 if i >= 700 else 0.0004} for i in range(840)]
    assert fit_cut_sequences(tied, 360)["key"][0] == 0.99


def test_probability_report_and_exact_volume_are_descriptive_not_cut_selection() -> None:
    report = probability_report([1, 1], [0.8, 0.9], [1, 1])
    assert report["auc"] is None and report["calibration_slope"] is None
    assert report["brier"] == pytest.approx(0.025)
    assert report["log_loss"] == pytest.approx(-math.log(0.72) / 2)
    rows = [{"case_id": str(i), "reader_input": reader_input("2026-09-01")} for i in range(3)]
    volume = volume_report(["2026-09-01", "2026-09-02"], rows, {"0", "1"}, {"1"})
    assert volume["days"] == {"2026-09-01": {"push": 2, "key": 1}, "2026-09-02": {"push": 0, "key": 0}}
    assert volume["totals"] == {"push": 2, "key": 1} and volume["guardrail_only"] is True


def test_load_validates_new_labels_guide_format_and_frozen_novelty(tmp_path: Path) -> None:
    row = case(0)
    row.pop("reader_novelty")
    path = tmp_path / "cases.jsonl.gz"
    path.write_bytes(gzip.compress((json.dumps(row) + "\n").encode()))
    loaded = load(path)
    assert loaded[0]["reader_novelty"].novelty == "unlinked"
    for change, error in (
        ({"label": {"verdict": "keep"}}, "current_labels_required"),
        ({"guide_version": "prior-guide"}, "guide_version_invalid"),
    ):
        path.write_bytes(gzip.compress((json.dumps({**row, **change}) + "\n").encode()))
        with pytest.raises(ValueError, match=error):
            load(path)


def test_report_keeps_status_and_tested_cuts_explicit() -> None:
    artifact = {
        "backend": "native",
        "dataset_sha256": "test-only",
        "questions_identity": "test-only",
        "guide_version": "test-only",
        "calibration": {"push_cut": None, "key_cut": None},
        "push_certificate": {"tested": [], "failure": "population_below_minimum"},
    }
    before = deepcopy(artifact)
    text = render_report(artifact, "certificate.json")
    assert "**uncalibrated**" in text and "push: not tested (population_below_minimum)" in text
    assert "`certificate.json`" in text and "/" not in text.split("Machine-readable evidence:")[1]
    assert artifact == before
    assert ReaderCalibration().certification_status == "uncalibrated"


def test_missing_research_dependency_has_explicit_install_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sklearn.model_selection", None)
    with pytest.raises(ValueError, match="install_research_dependency_group"):
        fit([], "native")


def test_candidate_rebuild_rejects_parameter_edits_even_after_rehash(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = {field: None for field in DERIVATION_FIELDS}
    frozen.update(backend="native", calibration={"materiality_floor": 1, "push_coefficients": [0, 1, 2, 3]})
    frozen["candidate_identity"] = candidate_identity(frozen)
    monkeypatch.setattr("scripts.eval_news_reader.fit", lambda rows, backend: deepcopy(frozen))
    verify_candidate([], frozen)
    edited = deepcopy(frozen)
    edited["calibration"]["push_coefficients"][0] = 0.01
    edited["calibration"]["materiality_floor"] = 3
    edited["candidate_identity"] = candidate_identity(edited)
    with pytest.raises(ValueError, match="candidate_derivation_changed"):
        verify_candidate([], edited)


def test_holdout_ledger_allows_replay_but_rejects_another_candidate_or_gold(tmp_path: Path) -> None:
    ledger = tmp_path / "holdout.jsonl"
    artifact = {"holdout_identity": "same-holdout", "candidate_identity": "first-candidate"}
    register_holdout_use(ledger, artifact, certification_dataset_sha256="same-gold")
    register_holdout_use(ledger, artifact, certification_dataset_sha256="same-gold")
    assert len(ledger.read_text().splitlines()) == 1
    with pytest.raises(ValueError, match="already_used_by_another_candidate"):
        register_holdout_use(
            ledger, {**artifact, "candidate_identity": "retuned"}, certification_dataset_sha256="same-gold"
        )
    with pytest.raises(ValueError, match="gold_sample_changed_after_use"):
        register_holdout_use(ledger, artifact, certification_dataset_sha256="new-gold-trial")


def test_production_applicability_is_not_inferred_from_answer_scores() -> None:
    row = case(0)
    assert applicable(row)
    row.update(reader_applicable=False, pre_reader_reason="protected_listing", deterministic_decision="notify")
    assert not applicable(row)
    row["reader_applicable"] = True
    with pytest.raises(ValueError, match="production_applicability_changed"):
        applicable(row)
