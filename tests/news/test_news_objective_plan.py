"""#199: what GEPA is allowed to optimize, and the proof it never sees anything else.

The predecessor asked one owner-blind question — "does an accepted review say anything is wrong here" —
and handed the optimizer every episode to split. In the only corpus this project has collected, 53% of
the failure cases carry a `first_bad_owner` that is not `triage_prompt`: retrieval misses, Gate
suppressions, storyline and policy defects. All of them became clusters an instruction was told to repair.

These tests are about the three-way split that replaced it, and about the one property that matters more
than any of the classification rules: whatever the optimizer actually receives contains no excluded case.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, ClassVar

import pytest

from tests.support.news_judgment import scored_judgment, trade_relevance
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.objective import (
    DevelopmentEpisode,
    build_gepa_objective_plan,
    build_readiness_report,
    production_decision,
    stable_hard_gate,
)
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.contracts import TriageContext
from tracefold.news.triage_rules import DEFAULT_POLICY

ROOT = Path(__file__).resolve().parents[2]
OBJECTIVE_MODULE = ROOT / "src" / "tracefold" / "news" / "learning" / "objective.py"

_VERDICT: dict[str, Any] = {
    "decision": "push",
    "novelty": "new_fact",
    "restates": -1,
    "event_type": "filing",
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "direction": "bullish",
    "scope": "single_name",
    "magnitude": 2,
    "actionable": True,
    "confidence": 0.8,
    "audience": "us_equity",
    "headline_zh": "特斯拉发布重大更新",
    "title_zh": "",
    "why_zh": "时间表发生变化。",
}


def _policy() -> dict[str, Any]:
    values = DEFAULT_POLICY.as_dict()
    return {
        "policy_version": TRIAGE_POLICY_VERSION,
        "policy_values": values,
        "policy_sha256": canonical_sha(values),
        "policy_source": "active_arm_manifest",
    }


def _episode(
    index: int,
    *,
    should_push: str = "must_push",
    dimensions: dict[str, str] | None = None,
    novelty: str = "new_fact",
    duplicate_of: str = "",
    expected: dict[str, Any] | None = None,
    expected_correction: str = "",
    evidence_refs: tuple[str, ...] = (),
    explicit_owner: str | None = None,
    derived_owner: str | None = None,
    production_magnitude: int = 2,
    production_novelty: str = "new_fact",
    reader_value: str = "realtime",
    told_rows: tuple[dict[str, Any], ...] = (),
    seen_rows: tuple[dict[str, Any], ...] = (),
    cluster: str | None = None,
    policy_metric: dict[str, Any] | None = None,
    production: bool = True,
) -> DevelopmentEpisode:
    opened_at_ms = 1_787_000_000_000 + index * 60_000
    card = {
        "event_id": f"event-{index}",
        "evidence_version": 1,
        "evidence_sha256": "a" * 64,
        "focus_fact_id": f"fact-{index}",
        "leader_title": f"Tesla files update {index}",
        "leader_description": "The filing changes the expected timetable.",
        "leader_url": f"https://example.invalid/{index}",
        "reporting_origin": "wire",
        "family": "general",
        "admission": "candidate",
        "queue_priority": "normal",
        "asset_class": "equity_or_commodity",
        "engine_type": "news",
        "storyline_key": "asset:TSLA",
        "comparison_title": f"tesla files update {index}",
        "raw_first_line": f"Tesla files update {index}",
        "grounded_assets": ["TSLA"],
        "member_count": 1,
        "opened_at_ms": opened_at_ms,
        "macro_lexicon": False,
        "provenance": ["1018"],
        "provider_metadata": {},
    }
    review: dict[str, Any] = {
        "review_id": f"review-{index}",
        "should_push": should_push,
        "dimensions": dict(dimensions or {"factual_fidelity": "pass"}),
        "novelty": {"judgment": novelty, "duplicate_of": duplicate_of},
        "first_bad_owner": derived_owner if derived_owner is not None else explicit_owner,
        "first_bad_owner_explicit": explicit_owner,
        "evidence_refs": list(evidence_refs),
        "expected": dict(expected or {}),
        "expected_correction": expected_correction,
        "note": "",
    }
    return DevelopmentEpisode(
        case_id=f"case-{index}",
        cluster_id=cluster or f"cluster-{index}",
        stratum="delivered",
        context=TriageContext.from_card(
            card, watchlist=(), told_rows=list(told_rows), now_ms=opened_at_ms, queue_lag_ms=0
        ),
        accepted_review=review,
        production_judgment=(
            scored_judgment(
                {**_VERDICT, "magnitude": production_magnitude, "novelty": production_novelty},
                relevance=trade_relevance(reader_value=reader_value),
            )
            if production
            else None
        ),
        policy_metric=policy_metric
        if policy_metric is not None
        else {
            "gate": {"grounded_assets": ["TSLA"], "watchlist_symbols": [], "admission": "candidate"},
            "storyline": {"title": f"Tesla files update {index}", "family": "general"},
            "seen": list(seen_rows),
            "told": [],
            **_policy(),
        },
    )


def _told_row(event_id: str, *, at_ms: int) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "at_ms": at_ms,
        "storyline_key": "asset:TSLA",
        "event_type": "filing",
        "magnitude": 2,
        "direction": "bullish",
        "headline_zh": "此前已告知读者的同一事实",
        "assets": ["TSLA"],
        "comparison_title": "tesla files update prior",
    }


def _disposition(episode: DevelopmentEpisode) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    case = build_gepa_objective_plan((episode,)).cases[0]
    return case.disposition, case.reason, case.predictors, case.dimensions


# --------------------------------------------------------------------------------------------------
# Target eligibility
# --------------------------------------------------------------------------------------------------


def test_explicit_prompt_owner_with_exact_typed_gold_is_an_event_semantics_target() -> None:
    disposition, _reason, predictors, dimensions = _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
            expected={"magnitude": 2},
            evidence_refs=("filing#magnitude",),
            explicit_owner="triage_prompt",
            production_magnitude=0,
        )
    )
    assert (disposition, predictors, dimensions) == ("target", ("event_semantics",), ("magnitude",))


def test_explicit_prompt_owner_with_evidence_and_correction_is_a_reader_card_target() -> None:
    """`factual_fidelity` is the one ReaderCard failure the metric can measure a repair of.

    A failed one arms the `factual_contradiction` hard gate, which zeroes the case until the judge can
    verify the candidate's facts against the frozen evidence.
    """

    disposition, _reason, predictors, dimensions = _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "fail"},
            evidence_refs=("card#fact",),
            expected_correction="The card states a filing date the evidence does not contain.",
            explicit_owner="triage_prompt",
        )
    )
    assert (disposition, predictors, dimensions) == ("target", ("reader_card",), ("factual_fidelity",))


@pytest.mark.parametrize("dimension", ["headline_fidelity", "why_support", "why_value"])
def test_a_copy_failure_the_metric_cannot_score_is_not_a_target(dimension: str) -> None:
    """Prompt-owned, evidenced, corrected — and still unmeasurable, so still not a target.

    `_GOLD_KEY` holds no copy dimension, so `_component` files the failure as `not_scored_no_gold` and
    drops it from the denominator; `_retains` only runs on dimensions the reviewer passed. GEPA would
    select a winner without ever scoring the repair it was pointed at.
    """

    assert _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "pass", dimension: "fail"},
            evidence_refs=("card#copy",),
            expected_correction="Do not claim priced-in without source evidence.",
            explicit_owner="triage_prompt",
        )
    )[:2] == ("excluded", "reader_card_failure_not_scorable")


def test_a_factual_failure_without_evidence_or_a_correction_is_not_a_target() -> None:
    """ "Write it better" is not a checkable objective, however confident the owner assignment is."""

    assert _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "fail"},
            evidence_refs=(),
            expected_correction="",
            explicit_owner="triage_prompt",
        )
    )[:2] == ("excluded", "reader_card_failure_without_evidence_and_correction")


def test_the_metric_really_cannot_score_a_failed_copy_dimension() -> None:
    """The premise the exclusion rests on, asserted against the metric rather than described.

    If a gold key ever appears for a copy dimension, or `_component` starts scoring a failed one, this
    fails and `_UNSCORABLE_CARD_DIMENSIONS` is wrong.
    """

    from tracefold.news.learning.metric import _component
    from tracefold.news.learning.objective import _CARD_DIMENSIONS, _NO_GOLD, _gold_value

    for dimension in ("headline_fidelity", "why_support", "why_value", "factual_fidelity"):
        assert _gold_value({dimension: "anything", "magnitude": 2}, dimension) is _NO_GOLD

    outcomes: list[tuple[str, str]] = []
    scored = _component(
        {"why_support": "fail"},
        _CARD_DIMENSIONS,
        {"why_zh": "候选写了别的"},
        {"why_zh": "生产写的"},
        {},
        None,
        outcomes,
    )
    assert scored is not None
    _score, _gold_n, effective_n, labelled_n = scored
    assert (effective_n, labelled_n) == (0, 1), "a failed copy dimension is labelled but never scored"
    assert outcomes == [("why_support", "not_scored_no_gold")]


def test_a_typed_failure_without_exact_gold_is_excluded_not_optimized_toward_change() -> None:
    assert _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
            expected={},
            evidence_refs=("filing#magnitude",),
            explicit_owner="triage_prompt",
        )
    )[:2] == ("excluded", "failed_typed_dimension_without_exact_gold")


def test_a_should_push_only_disagreement_is_not_a_structured_prompt_target() -> None:
    """Otherwise GEPA can score a win by moving any field that happens to flip the policy's action."""

    assert _disposition(
        _episode(
            1,
            should_push="must_push",
            dimensions={"factual_fidelity": "pass"},
            explicit_owner="triage_prompt",
            production_magnitude=0,
        )
    )[:2] == ("excluded", "should_push_only_failure")


def test_asset_grounding_is_a_gate_defect_and_never_an_event_semantics_target() -> None:
    assert (
        _disposition(
            _episode(
                1,
                dimensions={"factual_fidelity": "pass", "asset_grounding": "fail"},
                expected={"assets": [{"symbol": "TSLA", "role": "primary"}]},
                evidence_refs=("gate#grounding",),
                explicit_owner="triage_prompt",
            )
        )[0]
        == "excluded"
    )


# --------------------------------------------------------------------------------------------------
# The owner gate
# --------------------------------------------------------------------------------------------------


def test_a_derived_prompt_owner_the_operator_never_confirmed_is_excluded() -> None:
    """`news_reviews.first_bad_owner` is `submission.first_bad_owner or _derive_owner(submission)`.

    The column cannot tell a human's judgment from ReviewDesk's queue hint, so the plan reads the
    persisted submission instead. This case is the exact shape the derived map produces on its own: a
    failed `magnitude` maps to `triage_prompt` whether or not anybody said so.
    """

    assert _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
            expected={"magnitude": 2},
            evidence_refs=("filing#magnitude",),
            explicit_owner=None,
            derived_owner="triage_prompt",
            production_magnitude=0,
        )
    )[:2] == ("excluded", "owner_derived_only:triage_prompt")


@pytest.mark.parametrize(
    "owner",
    [
        "receiver",
        "deduper",
        "event_evidence",
        "gate",
        "retrieval",
        "storyline",
        "policy",
        "delivery",
        "taxonomy",
        "model",
    ],
)
def test_no_engineering_owned_failure_reaches_the_optimizer(owner: str) -> None:
    disposition, reason, _predictors, _dimensions = _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
            expected={"magnitude": 2},
            evidence_refs=("filing#magnitude",),
            explicit_owner=owner,
            production_magnitude=0,
        )
    )
    assert (disposition, reason) == ("excluded", f"non_prompt_owner:{owner}")


def test_an_explicit_unknown_owner_is_excluded_under_its_own_name() -> None:
    assert _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
            expected={"magnitude": 2},
            evidence_refs=("filing#magnitude",),
            explicit_owner="unknown",
            production_magnitude=0,
        )
    )[:2] == ("excluded", "unknown_owner")


# --------------------------------------------------------------------------------------------------
# Novelty: which stage actually owns a duplicate the model missed
# --------------------------------------------------------------------------------------------------


def test_a_restatement_whose_prior_reached_the_told_context_is_a_prompt_target() -> None:
    prior_at_ms = 1_787_000_000_000 - 600_000
    disposition, _reason, predictors, dimensions = _disposition(
        _episode(
            1,
            novelty="restatement",
            duplicate_of="event-prior",
            production_novelty="new_fact",
            explicit_owner="triage_prompt",
            told_rows=(_told_row("event-prior", at_ms=prior_at_ms),),
            seen_rows=({"event_id": "event-prior"},),
        )
    )
    assert (disposition, predictors, dimensions) == ("target", ("event_semantics",), ("novelty",))


def test_a_restatement_whose_prior_was_never_selected_is_a_retrieval_defect() -> None:
    """The model cannot recognise a card it was not shown, and no instruction repairs that."""

    assert _disposition(
        _episode(
            1,
            novelty="restatement",
            duplicate_of="event-prior",
            production_novelty="new_fact",
            explicit_owner="triage_prompt",
            told_rows=(),
            seen_rows=({"event_id": "event-prior"},),
        )
    )[:2] == ("excluded", "novelty_prior_not_selected")


def test_a_restatement_whose_prior_is_outside_the_source_history_is_not_retrieval_either() -> None:
    assert _disposition(
        _episode(
            1,
            novelty="restatement",
            duplicate_of="event-prior",
            production_novelty="new_fact",
            explicit_owner="triage_prompt",
            told_rows=(),
            seen_rows=(),
        )
    )[:2] == ("excluded", "novelty_prior_outside_source_history")


def test_a_restatement_the_reviewer_could_not_name_is_not_verifiable() -> None:
    assert _disposition(
        _episode(
            1,
            novelty="restatement",
            duplicate_of="",
            production_novelty="new_fact",
            explicit_owner="triage_prompt",
        )
    )[:2] == ("excluded", "accepted_novelty_target_not_verifiable")


def test_an_accepted_progression_the_model_called_new_names_no_prior_to_check() -> None:
    """`NoveltyJudgment` accepts `duplicate_of` only for `restatement`.

    So when the reviewer asserts a link the model did not make and cannot name the prior, there is no way
    to prove the card reached the ToldContext — which is the retrieval defect, not a Prompt one.
    """

    assert _disposition(
        _episode(
            1,
            novelty="progression",
            production_novelty="new_fact",
            explicit_owner="triage_prompt",
            told_rows=(_told_row("event-prior", at_ms=1_786_999_400_000),),
        )
    )[:2] == ("excluded", "accepted_novelty_target_not_verifiable")


def test_a_model_claimed_progression_the_reviewer_rejected_is_checkable_against_the_told_context() -> None:
    disposition, _reason, predictors, dimensions = _disposition(
        _episode(
            1,
            novelty="new_fact",
            production_novelty="progression",
            explicit_owner="triage_prompt",
            told_rows=(_told_row("event-prior", at_ms=1_786_999_400_000),),
        )
    )
    assert (disposition, predictors, dimensions) == ("target", ("event_semantics",), ("novelty",))


def test_an_unreplayable_novelty_target_blocks_the_run_like_any_other_target() -> None:
    """`would_be_target` has to know about all three target kinds, not just the two typed ones."""

    plan = build_gepa_objective_plan(
        (
            *_mixed_corpus(),
            _episode(
                99,
                novelty="restatement",
                duplicate_of="event-prior",
                production_novelty="new_fact",
                explicit_owner="triage_prompt",
                told_rows=(_told_row("event-prior", at_ms=1_786_999_400_000),),
                seen_rows=({"event_id": "event-prior"},),
                policy_metric={
                    "gate": {"grounded_assets": ["TSLA"], "watchlist_symbols": [], "admission": "candidate"},
                    "storyline": {"title": "broken", "family": "general"},
                    "seen": [{"event_id": "event-prior"}],
                    "told": [],
                },
            ),
        )
    )
    assert "non_replayable_target" in plan.blocking_reasons


def test_a_false_duplicate_claim_against_an_empty_context_is_not_the_instructions_fault() -> None:
    assert _disposition(
        _episode(
            1,
            novelty="new_fact",
            production_novelty="restatement",
            explicit_owner="triage_prompt",
            told_rows=(),
        )
    )[:2] == ("excluded", "accepted_novelty_target_not_verifiable")


# --------------------------------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------------------------------


def test_a_case_the_stable_program_answers_correctly_is_a_control() -> None:
    disposition, reason, _predictors, _dimensions = _disposition(
        _episode(1, should_push="must_push", dimensions={"factual_fidelity": "pass", "magnitude": "pass"})
    )
    assert (disposition, reason) == ("control", "stable_correct_under_accepted_review")


def test_a_correctly_held_case_is_a_negative_control() -> None:
    disposition, _reason, _predictors, _dimensions = _disposition(
        _episode(
            1,
            should_push="must_hold",
            dimensions={"factual_fidelity": "pass"},
            reader_value="background",
        )
    )
    assert disposition == "control"


def test_a_wrong_answer_nobody_blamed_the_prompt_for_never_becomes_a_control() -> None:
    """The dangerous half of the owner gate.

    Excluding a non-Prompt failure from `target` and then keeping it as a `control` would put it back in
    the trainset under a friendlier name — and a control is exactly the shape whose low score the
    reflection model is asked to raise.
    """

    disposition, reason, _predictors, _dimensions = _disposition(
        _episode(
            1,
            dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
            expected={"magnitude": 2},
            evidence_refs=("retrieval#miss",),
            explicit_owner="retrieval",
            production_magnitude=0,
        )
    )
    assert disposition == "excluded" and reason == "non_prompt_owner:retrieval"


def test_a_hard_gated_case_is_not_a_control_even_with_a_clean_rubric() -> None:
    """Every rubric dimension passes and the reviewer stated no action, so nothing here reads as wrong.

    The stable arm still scores zero: `magnitude 0` under `reader_value=realtime` is not realtime-eligible,
    so the policy resolves it to `trade_relevance_inconsistent`. Kept as a control it would sit in the
    trainset as a case the reflection model is asked to raise, and no instruction can raise it.
    """

    disposition, reason, _predictors, _dimensions = _disposition(
        _episode(1, should_push="uncertain", dimensions={"factual_fidelity": "pass"}, production_magnitude=0)
    )
    assert (disposition, reason) == ("excluded", "stable_hard_gate:relevance_inconsistent")


def test_a_must_push_the_reader_never_got_is_a_failure_with_no_owner_not_a_control() -> None:
    disposition, reason, _predictors, _dimensions = _disposition(
        _episode(
            1,
            should_push="must_push",
            dimensions={"factual_fidelity": "pass"},
            reader_value="background",
        )
    )
    assert (disposition, reason) == ("excluded", "owner_absent")


def test_the_mirrored_hard_gate_ladder_agrees_with_the_metric_itself() -> None:
    """The one duplicated rule in this module, held to the original by execution rather than by comment.

    `stable_hard_gate` mirrors the gate ladder inside `accepted_review_metric`, because that ladder lives
    in the scoring function whose bytes *are* the metric's published identity and this module may not
    import it. So the mirror is checked against the original on every shape that matters.
    """

    from tracefold.news.learning.metric import accepted_review_metric, build_compile_example

    episodes = (
        _episode(1, should_push="must_push", reader_value="background"),  # must_push_miss
        _episode(2, should_push="must_hold", reader_value="realtime"),  # must_hold_send
        _episode(3, should_push="uncertain", dimensions={"factual_fidelity": "fail"}),  # factual_contradiction
        _episode(4, should_push="uncertain", production_magnitude=0),  # relevance_inconsistent
        _episode(5, should_push="uncertain"),  # no gate
        _episode(6, should_push="uncertain", novelty="restatement", duplicate_of="event-prior"),
        _episode(
            7,
            should_push="uncertain",
            policy_metric={
                "gate": {"grounded_assets": ["AAPL"], "watchlist_symbols": [], "admission": "candidate"},
                "storyline": {"title": "Tesla files update 7", "family": "general"},
                "seen": [],
                "told": [],
                **_policy(),
            },
        ),  # ungrounded_primary_asset
    )
    for episode in episodes:
        example = build_compile_example(episode)
        assert episode.production_judgment is not None
        prediction = type(
            "P",
            (),
            {
                "get": lambda self, key, default=None, _e=episode: {
                    "verdict": _e.production_judgment.verdict.model_dump(mode="json"),
                    "editorial": _e.production_judgment.editorial.model_dump(mode="json"),
                }.get(key, default)
            },
        )()
        scored = accepted_review_metric(example, prediction, None, None, None)
        decision = production_decision(episode.production_judgment, episode.policy_metric)
        assert stable_hard_gate(episode, decision) == str(scored.hard_gate or ""), episode.case_id


# --------------------------------------------------------------------------------------------------
# What the optimizer actually receives
# --------------------------------------------------------------------------------------------------


def _mixed_corpus() -> tuple[DevelopmentEpisode, ...]:
    """Four verified targets, eight controls, and one of every excluded shape, interleaved in time."""

    episodes: list[DevelopmentEpisode] = []
    for cycle in range(4):
        base = cycle * 10
        episodes.append(
            _episode(
                base + 1,
                should_push="must_push",
                dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
                expected={"magnitude": 2},
                evidence_refs=("filing#magnitude",),
                explicit_owner="triage_prompt",
                production_magnitude=0,
            )
        )
        episodes.append(_episode(base + 2, should_push="must_push", dimensions={"factual_fidelity": "pass"}))
        episodes.append(
            _episode(
                base + 3, should_push="must_hold", dimensions={"factual_fidelity": "pass"}, reader_value="background"
            )
        )
        episodes.append(
            _episode(
                base + 4,
                dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
                expected={"magnitude": 2},
                evidence_refs=("retrieval#miss",),
                explicit_owner="retrieval",
                production_magnitude=0,
            )
        )
        episodes.append(
            _episode(
                base + 5,
                dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
                expected={"magnitude": 2},
                evidence_refs=("filing#magnitude",),
                derived_owner="triage_prompt",
                production_magnitude=0,
            )
        )
    return tuple(episodes)


def test_the_optimizer_corpus_is_targets_plus_controls_and_nothing_else() -> None:
    plan = build_gepa_objective_plan(_mixed_corpus())

    assert set(plan.optimizer_case_ids) == set(plan.target_case_ids) | set(plan.control_case_ids)
    assert set(plan.optimizer_case_ids).isdisjoint(plan.excluded_case_ids)
    assert len(plan.target_case_ids) == 4
    assert len(plan.control_case_ids) == 8
    assert len(plan.excluded_case_ids) == 8
    assert plan.exclusion_reasons == {"non_prompt_owner:retrieval": 4, "owner_derived_only:triage_prompt": 4}
    assert plan.blocking_reasons == ()


def test_the_optimizer_elects_one_representative_per_connected_fact_cluster() -> None:
    corpus = list(_mixed_corpus())
    original = build_gepa_objective_plan(corpus)
    seed = next(episode for episode in corpus if episode.case_id in original.target_case_ids)
    inflated = tuple(
        [*corpus, *(seed.model_copy(update={"case_id": f"duplicate-case-{index}"}) for index in range(1, 10))]
    )

    plan = build_gepa_objective_plan(inflated)
    optimizer_clusters = [episode.cluster_id for episode in plan.optimizer_episodes]
    readiness = build_readiness_report(
        plan, episodes=inflated, identity={"development_dataset_sha": "0" * 64}, coverage={}
    )

    assert len(plan.optimizer_case_ids) == len(set(optimizer_clusters))
    assert optimizer_clusters.count(seed.cluster_id) == 1
    assert len([case for case in plan.cases if case.reason == "cluster_representative_shadowed:target"]) == 9
    assert build_gepa_objective_plan(tuple(reversed(inflated))).optimizer_case_ids == plan.optimizer_case_ids
    assert readiness["corpus"]["case_n"] == len(inflated)
    assert len(readiness["case_dispositions"]) == len(inflated)
    assert readiness["objective"]["optimizer_case_n"] == readiness["objective"]["optimizer_cluster_n"]
    assert readiness["call_envelope"]["metric_calls_per_full_selection_evaluation"] == len(
        plan.development_selection_episodes
    )


def test_a_target_beats_a_control_from_the_same_fact_cluster() -> None:
    corpus = list(_mixed_corpus())
    original = build_gepa_objective_plan(corpus)
    target = next(episode for episode in corpus if episode.case_id in original.target_case_ids)
    control = next(episode for episode in corpus if episode.case_id in original.control_case_ids)
    same_cluster = tuple(
        episode.model_copy(update={"cluster_id": target.cluster_id}) if episode.case_id == control.case_id else episode
        for episode in corpus
    )

    plan = build_gepa_objective_plan(same_cluster)
    dispositions = {case.case_id: case for case in plan.cases}

    assert target.case_id in plan.target_case_ids
    assert control.case_id not in plan.optimizer_case_ids
    assert dispositions[control.case_id].disposition == "excluded"
    assert dispositions[control.case_id].reason == "cluster_representative_shadowed:control"
    assert plan.schema_version == "tracefold.news.gepa_objective_plan.v2"
    assert plan.split is not None
    assert plan.split["schema"] == "tracefold.news.compile_split_receipt.v2"


def test_equal_safety_controls_prefer_the_newer_case_before_the_case_id() -> None:
    older_with_extra_novelty_stratum = _episode(1, should_push="should_push", cluster="one-fact")
    newer = _episode(
        2,
        should_push="uncertain",
        cluster="one-fact",
    )

    plan = build_gepa_objective_plan((older_with_extra_novelty_stratum, newer))

    assert plan.control_case_ids == (newer.case_id,)
    assert next(case for case in plan.cases if case.case_id == older_with_extra_novelty_stratum.case_id).reason == (
        "cluster_representative_shadowed:control"
    )


def test_the_split_roots_cover_the_optimizer_corpus_only() -> None:
    plan = build_gepa_objective_plan(_mixed_corpus())
    assert plan.split is not None

    train_ids = {episode.case_id for episode in plan.train_episodes}
    selection_ids = {episode.case_id for episode in plan.development_selection_episodes}
    assert train_ids | selection_ids == set(plan.optimizer_case_ids)
    assert not train_ids & selection_ids
    assert plan.split["train"]["case_root_sha256"] == canonical_sha(sorted(train_ids))
    assert plan.split["development_selection"]["case_root_sha256"] == canonical_sha(sorted(selection_ids))
    train_clusters = {episode.cluster_id for episode in plan.train_episodes}
    selection_clusters = {episode.cluster_id for episode in plan.development_selection_episodes}
    assert not train_clusters & selection_clusters
    # Ordered by Event time, so the earlier clusters train and the later ones select — no shuffle, no seed.
    assert max(e.context.now_ms for e in plan.train_episodes) <= min(
        e.context.now_ms for e in plan.development_selection_episodes
    )


def test_both_halves_carry_a_verified_target_or_the_plan_blocks() -> None:
    plan = build_gepa_objective_plan(_mixed_corpus())
    targets = set(plan.target_case_ids)
    assert any(episode.case_id in targets for episode in plan.train_episodes)
    assert any(episode.case_id in targets for episode in plan.development_selection_episodes)

    # Move every target into the earliest clusters and the later half has none left to select on.
    front_loaded = tuple(
        episode for episode in _mixed_corpus() if episode.case_id in targets or int(episode.case_id.split("-")[1]) < 20
    )
    blocked = build_gepa_objective_plan(front_loaded)
    assert "development_selection_target_missing" in blocked.blocking_reasons or blocked.split is None


def test_both_halves_carry_a_control_even_when_target_strata_cover_every_gate() -> None:
    source = _mixed_corpus()
    source_plan = build_gepa_objective_plan(source)
    controls = [episode for episode in source if episode.case_id in source_plan.control_case_ids][:4]
    targets = [
        _episode(
            100 + index,
            should_push="must_hold" if index in {1, 4} else "must_push",
            dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
            expected={"magnitude": 2},
            evidence_refs=("filing#magnitude",),
            explicit_owner="triage_prompt",
            production_magnitude=0,
        )
        for index in range(6)
    ]

    plan = build_gepa_objective_plan((*controls, *targets))

    assert plan.split is not None
    assert plan.split_error == ""
    assert plan.blocking_reasons == ("development_selection_control_missing",)


def test_a_corpus_of_failures_alone_no_longer_splits() -> None:
    """Before #199 this was the normal shape of a compile, and it optimized happily."""

    failures = tuple(
        _episode(
            index,
            dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
            expected={"magnitude": 2},
            evidence_refs=("filing#magnitude",),
            explicit_owner="triage_prompt",
            production_magnitude=0,
        )
        for index in range(1, 5)
    )
    plan = build_gepa_objective_plan(failures)
    assert plan.blocking_reasons == ("no_correct_control_clusters",)
    assert plan.split is None


def test_the_required_strata_still_fail_closed_on_the_optimizer_corpus() -> None:
    """The release profile's coverage floor did not move; it now applies to what GEPA actually sees."""

    no_negative = tuple(
        episode for episode in _mixed_corpus() if str(dict(episode.accepted_review).get("should_push")) != "must_hold"
    )
    plan = build_gepa_objective_plan(no_negative)
    assert any(reason.endswith("_negative_action_missing") for reason in plan.blocking_reasons)


def test_an_accepted_external_miss_is_out_of_scope_rather_than_a_free_control() -> None:
    """No stable output means every passed retention anchor is credited for free (`field_absent`).

    Kept as a control it would pay full marks to any candidate at all — including one that regressed
    everywhere else — which is the one thing a control must never do.
    """

    disposition, reason, _predictors, _dimensions = _disposition(
        _episode(1, should_push="must_push", dimensions={"factual_fidelity": "pass"}, production=False)
    )
    assert (disposition, reason) == ("excluded", "stable_output_absent")


def test_a_corpus_of_external_misses_alone_does_not_block_on_replayability() -> None:
    """Out of scope is not the same as broken: it shrinks the corpus, it does not fail the run."""

    plan = build_gepa_objective_plan(
        tuple(
            _episode(index, dimensions={"factual_fidelity": "pass"}, explicit_owner="triage_prompt", production=False)
            for index in range(1, 4)
        )
    )
    assert "non_replayable_target" not in plan.blocking_reasons
    assert plan.exclusion_reasons == {"stable_output_absent": 3}


def test_an_unverifiable_policy_projection_blocks_before_the_budget_is_spent() -> None:
    corpus = list(_mixed_corpus())
    broken = _episode(
        99,
        dimensions={"factual_fidelity": "pass", "magnitude": "fail"},
        expected={"magnitude": 2},
        evidence_refs=("filing#magnitude",),
        explicit_owner="triage_prompt",
        production_magnitude=0,
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"], "watchlist_symbols": [], "admission": "candidate"},
            "storyline": {"title": "broken", "family": "general"},
            "seen": [],
            "told": [],
        },
    )
    plan = build_gepa_objective_plan((*corpus, broken))
    assert "non_replayable_target" in plan.blocking_reasons
    assert broken.case_id in plan.excluded_case_ids


# --------------------------------------------------------------------------------------------------
# One implementation
# --------------------------------------------------------------------------------------------------


def test_run_gepa_hands_the_optimizer_exactly_the_plan_it_published() -> None:
    """The acceptance test #199 calls the most important one: capture what the optimizer really got."""

    from dataclasses import asdict

    import dspy

    from tracefold.news.learning.baseline import BaselineCase, run_baseline
    from tracefold.news.learning.optimizer import run_gepa
    from tracefold.news.program.artifact import load_stable_program_artifact

    base = list(_mixed_corpus())
    seed = build_gepa_objective_plan(base).optimizer_episodes[0]
    corpus = tuple([*base, seed.model_copy(update={"case_id": "shadowed-optimizer-member"})])
    plan = build_gepa_objective_plan(corpus)
    artifact = load_stable_program_artifact()

    def _recorded_case(episode: DevelopmentEpisode) -> BaselineCase:
        assert episode.production_judgment is not None
        return BaselineCase(
            episode=episode,
            recorded_decision_result=asdict(production_decision(episode.production_judgment, episode.policy_metric)),
        )

    baseline = run_baseline(
        tuple(_recorded_case(episode) for episode in plan.optimizer_episodes),
        mode="recorded",
        artifact=artifact,
        objective=plan,
        dataset_identity={"development_dataset_sha": "0" * 64},
        retrieval_population=corpus,
    )
    captured: dict[str, set[str]] = {}

    class _CapturingOptimizer:
        def __init__(self, metric: Any, **kwargs: Any) -> None:
            del metric, kwargs

        def compile(self, student: Any, *, trainset: list[Any], teacher: None, valset: list[Any]) -> Any:
            captured["train"] = {example.case_id for example in trainset}
            captured["val"] = {example.case_id for example in valset}
            student.event_semantics.signature = student.event_semantics.signature.with_instructions("Learned.")
            student.detailed_results = type(
                "R",
                (),
                {
                    "parents": [[None]],
                    "val_aggregate_scores": [0.5],
                    "discovery_eval_counts": [1],
                    "total_metric_calls": 1,
                    "num_full_val_evals": 1,
                    "seed": 129,
                    "best_idx": 0,
                },
            )()
            return student

    class _Judge:
        identity: ClassVar[dict[str, Any]] = {"judge_id": "test/noop", "failure_cache": False}

        def retains(self, *_args: Any, **_kwargs: Any) -> bool:
            return False

    from tracefold.news.learning.optimizer import build_optimizer_lm

    lm = build_optimizer_lm(
        model_name="test/task", api_key="k", api_base="http://127.0.0.1:1", timeout=1.0, max_tokens=128
    )
    reflection = build_optimizer_lm(
        model_name="test/reflection",
        api_key="k",
        api_base="http://127.0.0.1:1",
        timeout=1.0,
        max_tokens=128,
        role="reflection",
    )
    with dspy.context(lm=lm):
        result = run_gepa(
            base_program=artifact,
            episodes=corpus,
            task_lm=lm,
            reflection_lm=reflection,
            judge=_Judge(),
            max_metric_calls=4,
            seed=129,
            review_rubric_version="news_review_v4",
            optimizer_factory=_CapturingOptimizer,
        )

    seen = captured["train"] | captured["val"]
    assert seen == set(plan.optimizer_case_ids)
    assert baseline.objective["optimizer_case_root_sha256"] == canonical_sha(sorted(seen))
    assert baseline.objective["split"] == plan.split
    assert seen.isdisjoint(plan.excluded_case_ids)
    assert result.failure_cluster_ids == plan.target_failure_cluster_ids
    assert result.target_dimensions == plan.target_dimensions
    assert result.split == plan.split
    assert result.train_count == len(plan.train_episodes)
    assert result.val_count == len(plan.development_selection_episodes)


def test_the_objective_module_imports_no_framework_database_or_provider() -> None:
    tree = ast.parse(OBJECTIVE_MODULE.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
    assert not any(module.split(".")[0] in {"dspy", "gepa", "psycopg", "litellm", "httpx"} for module in modules)
    # Nor the modules that would reach them one hop away: the metric holds DSPy, the evaluator holds the
    # database, and the compiler package holds the container and the promotion seam.
    assert not any(
        module.endswith((".metric", ".evaluator", ".review", ".canary", ".baseline")) or ".compiler" in module
        for module in modules
    )


def test_readiness_explains_the_same_plan_without_asking_a_model_anything() -> None:
    corpus = _mixed_corpus()
    plan = build_gepa_objective_plan(corpus)
    report = build_readiness_report(plan, episodes=corpus, identity={"development_dataset_sha": "0" * 64}, coverage={})

    assert report["outcome"] == "ready"
    assert report["objective"]["schema"] == "tracefold.news.gepa_objective_plan.v2"
    assert report["objective"]["target_cluster_n"] == len(plan.target_failure_cluster_ids)
    assert report["objective"]["exclusion_reasons"] == plan.exclusion_reasons
    assert report["owner_distribution"]["explicit"] == {"retrieval": 4, "triage_prompt": 4}
    assert report["owner_distribution"]["derived"] == {"triage_prompt": 4}
    assert report["train"]["target_case_n"] + report["development_selection"]["target_case_n"] == 4
    assert report["call_envelope"]["task_model_calls_per_metric_call"] == 2
    assert len(report["case_dispositions"]) == len(corpus)
