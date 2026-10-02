"""Policy proofs use explicitly synthetic calibration, never archived model scores."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tracefold.news.notifications.novelty import ClaimLink, ReaderNovelty
from tracefold.news.notifications.policy import (
    KIND_FLOOR,
    PUSHABLE_KINDS,
    ReaderCalibration,
    reader_decision,
    reader_scores,
)
from tracefold.news.notifications.reader import (
    REPORT_KIND_OPTIONS,
    AnchorEvidence,
    InterruptEvidence,
    MaterialityEvidence,
    ReaderJudgment,
    ReportKind,
    ReportKindEvidence,
)
from tracefold.news.updates.contracts import ClaimFields

# Synthetic arithmetic fixture: not a fitted production calibration.
CALIBRATION = ReaderCalibration(
    push_coefficients=(0, 0, 1, -1),
    key_coefficients=(0, 1, 0),
    push_cut=0.6,
    key_cut=0.75,
    certification_status="certified",
)


def judgment(
    m: float = 0.7,
    *,
    e: float = 1,
    i: float = 0.2,
    kind: ReportKind = "new_action",
    anchor: dict[str, float] | None = None,
    backend: str = "native",
) -> ReaderJudgment:
    probabilities: dict[str, float] = dict.fromkeys((key for key, _ in REPORT_KIND_OPTIONS), 0)
    probabilities[kind] = e
    probabilities["background"] += 1 - e
    return ReaderJudgment.model_validate(
        {
            "status": "available",
            "backend": backend,
            "identity": "synthetic-policy-proof",
            "report_kind": ReportKindEvidence(
                value=max(probabilities, key=probabilities.__getitem__), probabilities=probabilities, confidence=0.8
            ),
            "materiality": MaterialityEvidence(value=1 + m, probabilities=(0, 1 - m, m, 0), confidence=0.8),
            "interrupt": InterruptEvidence(probabilities=(1 - i, i), confidence=0.8),
            "anchor": None if anchor is None else AnchorEvidence(probabilities=anchor, confidence=0.8),
        }
    )


def decide(novelty: ReaderNovelty, answer: ReaderJudgment, **options: Any):
    return reader_decision(
        novelty,
        answer,
        first_available_at_ms=20,
        message_intents=("ra", "rb"),
        calibration=options.pop("calibration", CALIBRATION),
        **options,
    )


def test_reader_rows_keep_known_inflight_and_later_correction_precedence() -> None:
    weak = judgment(0.01, e=0.01)
    assert decide(ReaderNovelty(novelty="known", intent_id="ra"), weak).outcome == "known"
    assert decide(ReaderNovelty(novelty="in_flight", intent_id="ra"), weak).outcome == "in_flight"
    correction = ReaderNovelty(
        novelty="development",
        intent_id="ra",
        settled_at_ms=10,
        path=(ClaimLink(current_ref="c", previous_ref="a", relation="corrects", asserted_at_ms=1),),
    )
    result = decide(correction, weak)
    assert (result.outcome, result.render, result.anchor_intent_id, result.scores) == (
        "correction",
        "correction",
        "ra",
        None,
    )
    older = correction.model_copy(update={"settled_at_ms": 30})
    result = decide(older, weak)
    assert (result.outcome, result.render, result.anchor_intent_id) == ("feed", "increment", "ra")


@pytest.mark.parametrize("backend", ["native", "generated"])
def test_uncertified_backend_cannot_authorize_a_model_push(backend: str) -> None:
    result = reader_decision(
        ReaderNovelty(novelty="unlinked"),
        judgment(1, i=1, backend=backend),
        first_available_at_ms=20,
        message_intents=(),
    )
    assert result.outcome == "feed"
    assert result.scores is not None and result.scores.certification_status == "uncalibrated"


@pytest.mark.parametrize("kind,pushable", list(PUSHABLE_KINDS.items()))
def test_qualification_table_gates_every_category(kind: ReportKind, pushable: bool) -> None:
    result = decide(ReaderNovelty(novelty="unlinked"), judgment(0.9, kind=kind))
    assert result.outcome == ("push" if pushable else "feed")


def test_eligible_mass_floor_is_inclusive_and_key_requires_push() -> None:
    novelty = ReaderNovelty(novelty="unlinked")
    assert decide(novelty, judgment(0.8, e=KIND_FLOOR - 0.001, i=1)).outcome == "feed"
    assert decide(novelty, judgment(0.8, e=KIND_FLOOR, i=1)).outcome == "key"
    assert decide(novelty, judgment(0.2, i=1)).outcome == "feed"
    assert decide(novelty, judgment(0.8, i=0.74)).outcome == "push"
    at_cut = judgment(0.8, i=0.75)
    exact_cut = replace(CALIBRATION, key_cut=reader_scores(at_cut, calibration=CALIBRATION).p_key)
    assert decide(novelty, at_cut, calibration=exact_cut).outcome == "key"


def test_held_is_one_calibration_input_and_anchoring_controls_render_only() -> None:
    anchored = {"m1": 0.1, "m2": 0.8, "none": 0.1}
    unlinked = ReaderNovelty(novelty="unlinked")
    unheld = decide(unlinked, judgment(0.7))
    held = decide(unlinked, judgment(0.7, anchor=anchored))
    strong = decide(unlinked, judgment(0.95, anchor=anchored))
    assert unheld.outcome == "push" and held.outcome == "feed"
    assert (strong.outcome, strong.render, strong.anchor_intent_id) == ("push", "increment", "rb")
    assert held.scores is not None and held.scores.held and held.scores.p_push < unheld.scores.p_push
    # Anchored details can still push on their new information, without a separate held cut.
    assert strong.scores is not None and strong.scores.p_push >= CALIBRATION.push_cut


@pytest.mark.parametrize(
    "kind,mode,phase,effective",
    [
        ("state_change", "observation", "executing", True),
        ("state_change", "observation", "completed", True),
        ("official_measure", "decision", "ordered", True),
        ("official_measure", "decision", "effective", True),
        ("state_change", "observation", "cancelled", True),
        ("state_change", "observation", None, False),
        ("state_change", "observation", "unknown", False),
        ("state_change", "commitment", "announced", False),
        ("state_change", "forecast", "completed", False),
        ("new_quantity", "observation", "completed", False),
    ],
)
def test_actual_unanchored_state_change_preserves_the_existing_held_exception(
    kind: str,
    mode: str,
    phase: str | None,
    effective: bool,
) -> None:
    fields = ClaimFields.model_validate(
        {"subject": "Venue", "action": "resumed withdrawals", "content_kind": kind, "mode": mode, "phase": phase}
    )
    result = decide(
        ReaderNovelty(novelty="increment", intent_id="ra"),
        judgment(0.7, anchor={"m1": 0.1, "m2": 0.1, "none": 0.8}),
        claim_fields=fields,
    )
    assert (result.outcome, result.render) == ("push" if effective else "feed", "full")
    assert result.scores is not None and result.scores.held is not effective


def test_expectation_and_anchor_probability_do_not_change_push_score() -> None:
    answer = judgment(0.8, e=0.7)
    other = answer.model_copy(
        update={
            "materiality": MaterialityEvidence(
                value=2.6,
                probabilities=(0.2, 0, 0, 0.8),
                confidence=0.1,
            ),
            "anchor": AnchorEvidence(probabilities={"m1": 0.9, "none": 0.1}, confidence=0.1),
        }
    )
    assert reader_scores(answer, calibration=CALIBRATION) == reader_scores(other, calibration=CALIBRATION)


def test_rule_changes_replay_saved_distributions_without_changing_question_identity(monkeypatch) -> None:
    from tracefold.news.notifications.reader import READER_QUESTIONS_IDENTITY

    identity = READER_QUESTIONS_IDENTITY
    answer = judgment(0.8, kind="self_reported_metric")
    assert reader_scores(answer, calibration=CALIBRATION).e == 1
    monkeypatch.setitem(PUSHABLE_KINDS, "self_reported_metric", False)
    assert reader_scores(answer, calibration=CALIBRATION).e == 0
    assert identity == READER_QUESTIONS_IDENTITY


def test_probability_extremes_and_calibration_validation() -> None:
    for p in (0, 1):
        scores = reader_scores(judgment(p, e=p, i=p), calibration=CALIBRATION)
        assert 0 <= scores.p_push <= 1 and 0 <= scores.p_key <= 1
    with pytest.raises(ValueError, match="materiality_floor_invalid"):
        replace(CALIBRATION, materiality_floor=4)
    with pytest.raises(ValueError, match="coefficients_invalid"):
        replace(CALIBRATION, push_coefficients=(float("nan"), 0, 0, 0))
    with pytest.raises(ValueError, match="certification_invalid"):
        replace(CALIBRATION, push_cut=None)


def test_provider_rounding_does_not_make_derived_mass_exceed_one() -> None:
    answer = judgment(1)
    probabilities = dict(answer.report_kind.probabilities)
    probabilities["market_move"] = 0.0000005
    rounded = answer.model_copy(
        update={
            "report_kind": ReportKindEvidence(value="new_action", probabilities=probabilities, confidence=0.8),
            "materiality": MaterialityEvidence(value=2.5, probabilities=(0, 0, 0.5, 0.5000005), confidence=0.8),
        }
    )
    scores = reader_scores(rounded, calibration=CALIBRATION)
    assert scores.e == scores.m == 1
    assert rounded.materiality.probabilities == (0, 0, 0.5, 0.5000005)
