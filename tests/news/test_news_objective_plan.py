"""Issue #501: the Objective Plan is every Gold-bearing case; owner columns grant nothing.

Issue #651 §9 adds the question that comes before all of them: which target is this plan for. A review
now answers only the questions its own Event posed, so a case is evidence for the targets it was labelled
for and for no others, and the population — and therefore the split — is a property of the target.
"""

from __future__ import annotations

from tracefold.news.learning.objective import build_gepa_objective_plan, build_readiness_report

from .test_news_program_gepa_real import _episode


def _case(plan: object, case_id: str) -> object:
    return next(case for case in plan.cases if case.case_id == case_id)


def test_every_valid_gold_case_is_included_whatever_its_owner_column_says() -> None:
    mismatch = _episode(1, target=True)
    exact = _episode(2, target=False)
    unowned = _episode(3, target=True, first_bad_owner_explicit=None, first_bad_owner=None)
    wrong_owner = _episode(5, target=True, first_bad_owner_explicit="triage_prompt", first_bad_owner="triage_prompt")
    owned_exact = _episode(6, target=False, first_bad_owner_explicit="taxonomy", first_bad_owner="taxonomy")

    plan = build_gepa_objective_plan((mismatch, exact, unowned, wrong_owner, owned_exact), "classification")

    for episode in (mismatch, exact, unowned, wrong_owner, owned_exact):
        case = _case(plan, episode.case_id)
        assert case.disposition == "included", episode.case_id
        assert case.predictors == ("taxonomy",)
        assert case.reason == "accepted_taxonomy_gold"
    assert _case(plan, mismatch.case_id).stable_exact is False
    assert _case(plan, exact.case_id).stable_exact is True
    assert _case(plan, wrong_owner.case_id).owner == "triage_prompt"
    assert plan.stable_exact_n == 2 and plan.stable_mismatch_n == 3
    assert plan.target == "classification"
    assert plan.target_predictors == ("taxonomy",)
    assert len(plan.optimizer_cluster_ids) == 5
    assert plan.exclusion_reasons == {}


def test_invalid_gold_or_an_unanswered_target_is_excluded_but_a_missing_stable_answer_is_not() -> None:
    """#651 §9: the surviving exclusions are about the reviewer's label, not the previous arm's output.

    GEPA scores the *candidate* against Gold, so a case the Stable arm never answered still poses a
    complete question with a correct answer. What goes missing without a recorded Stable answer is
    `stable_exact`, a readiness diagnostic nothing gates on — and the old `stable_output_absent` threw the
    reviewer's label away to protect that number. What still excludes a case is Gold this code cannot
    parse, or a review that never answered this target's question at all.
    """

    no_stable = _episode(1, target=True).model_copy(update={"production_judgment": None})
    bad_gold = _episode(2, target=False, taxonomy={"event_family": "whale"})
    # A review whose Event posed only the semantics question: no taxonomy was written, so the corpus seals
    # no `classification` in its applicable targets and this plan may not invent one for it.
    unanswered = _episode(3, target=True, taxonomy=None).model_copy(update={"applicable_targets": ("understanding",)})

    plan = build_gepa_objective_plan((no_stable, bad_gold, unanswered), "classification")

    assert _case(plan, no_stable.case_id).disposition == "included"
    assert _case(plan, no_stable.case_id).reason == "accepted_taxonomy_gold"
    assert _case(plan, no_stable.case_id).stable_exact is None
    assert _case(plan, bad_gold.case_id).reason == "accepted_taxonomy_gold_invalid"
    assert _case(plan, unanswered.case_id).reason == "target_not_labelled_by_review"
    assert plan.optimizer_case_ids == (no_stable.case_id,)
    assert plan.exclusion_reasons == {"accepted_taxonomy_gold_invalid": 1, "target_not_labelled_by_review": 1}
    # One surviving cluster cannot fill both halves of the split, so the corpus blocks on the two empty
    # halves themselves rather than on anything these exclusions are named for.
    assert plan.blocking_reasons == ("train_empty", "selection_empty")


def test_connected_fact_cluster_retains_cases_in_the_same_split() -> None:
    included = _episode(1, target=True)
    shadow = included.model_copy(update={"case_id": "shadow"})
    other = _episode(2, target=False)

    plan = build_gepa_objective_plan((included, shadow, other), "classification")

    assert len(plan.optimizer_case_ids) == 3
    assert len({episode.cluster_id for episode in plan.optimizer_episodes}) == 2
    assert not plan.excluded_case_ids
    assert plan.optimizer_ready
    assert not (
        {ep.cluster_id for ep in plan.train_episodes} & {ep.cluster_id for ep in plan.development_selection_episodes}
    )


def test_one_cluster_cannot_be_split_and_blocks_on_two_empty_halves() -> None:
    """One fact cannot be both what GEPA learns from and what it selects the winner on.

    The blockers name the state a compile cannot proceed from — nothing to train on, nothing to select on
    — rather than the arithmetic that produced it (#651 §9). `split_requires_two_clusters` survives as the
    split's own error text, which the plan publishes beside the blockers so the cause is still readable.
    """

    plan = build_gepa_objective_plan((_episode(1, target=True),), "classification")

    assert plan.optimizer_case_ids
    assert plan.split is None
    assert plan.split_error == "news_program_compile_split_requires_two_clusters"
    assert plan.blocking_reasons == ("train_empty", "selection_empty")


def test_readiness_answers_for_one_target_and_publishes_the_others_sealed_counts() -> None:
    """#651 §9: `targets` replaces `development_profile`, which could not answer "ready for what".

    There was never a corpus-wide readiness. The same cases can be a complete classification corpus and no
    explanation corpus at all, and one `ready: false` covering both said nothing an operator could act on.
    The report now states `ready` only for the target it actually planned — that is the only split it
    holds — and republishes the other targets' sealed counts so a wrong question can be corrected.
    """

    episodes = tuple(_episode(index, target=index % 2 == 1) for index in range(1, 13))
    plan = build_gepa_objective_plan(episodes, "classification")
    coverage = {
        "targets": {
            "classification": {"case_n": 12, "cluster_n": 12},
            "understanding": {"case_n": 12, "cluster_n": 12},
            "explanation": {"case_n": 0, "cluster_n": 0},
        },
        "rubric_ineligible_n": 4,
        "explanation_supervision_pending_n": 2,
    }

    report = build_readiness_report(
        plan, episodes=episodes, identity={"dataset": "test"}, coverage=coverage, target="classification"
    )

    assert report["schema"] == "tracefold.news.gepa_readiness_report.v6"
    assert report["target"] == "classification"
    assert report["objective"]["compilable"] is True
    assert report["objective"]["blockers"] == []
    targets = report["targets"]
    assert targets["schema"] == "tracefold.news.readiness_targets.v1"
    assert targets["target"] == "classification"
    assert targets["by_target"]["classification"] == {
        "predictor": "taxonomy",
        "planned": True,
        "case_n": 12,
        "cluster_n": 12,
        "ready": True,
        "blockers": [],
        "train_case_n": 8,
        "development_selection_case_n": 4,
        "cluster_leak": [],
        # These fixtures state no action and no novelty judgment, so the halves carry none of the four
        # published strata. Reported, and gating nothing.
        "train_stratum_n": 0,
        "development_selection_stratum_n": 0,
    }
    # An unplanned target states its sealed evidence and no verdict: `ready` is a statement about a split,
    # and building two more splits to print two numbers would cost more than it tells anyone.
    assert targets["by_target"]["explanation"] == {
        "predictor": "reader_card",
        "planned": False,
        "case_n": 0,
        "cluster_n": 0,
    }
    assert targets["rubric_ineligible_n"] == 4
    assert targets["explanation_supervision_pending_n"] == 2
    assert "development_profile" not in report
    assert "outcome" not in report
    assert "blocking" not in report
    assert "owner_distribution" not in report
    assert "exact_gold_coverage" not in report


def test_readiness_reports_halves_and_taxonomy_support_under_retired_quota_counts() -> None:
    """The corpus-size quotas are gone (#651 §9), and the counts they read now only describe.

    `development_boundary_cluster_n_insufficient` and its four siblings refused a corpus for being thin,
    which cost every honest small corpus its chance to end in an equally honest `NO_OP`. The cluster-role
    counts below sit under every retired floor and change nothing: what decides `ready` is whether this
    target's split has two halves that do not share a fact. The halves, the call envelope and the Gold
    summary are reported exactly as before, because none of them was ever the gate.
    """

    episode_rows = []
    for index in range(1, 201):
        episode = _episode(index, target=index % 2 == 1)
        review = dict(episode.accepted_review)
        review["should_push"] = "must_push" if index % 2 else "should_hold"
        review["novelty"] = {"judgment": "new_fact", "duplicate_of": ""}
        episode_rows.append(episode.model_copy(update={"accepted_review": review}))
    episodes = tuple(episode_rows)
    plan = build_gepa_objective_plan(episodes, "classification")
    coverage = {
        "boundary_cluster_n": 1,
        "retention_cluster_n": 1,
        "negative_cluster_n": 1,
        "safety_cluster_n": 1,
        "stratum_n": 1,
    }

    report = build_readiness_report(
        plan, episodes=episodes, identity={"dataset": "test"}, coverage=coverage, target="classification"
    )

    classification = report["targets"]["by_target"]["classification"]
    assert classification["ready"] is True
    assert classification["blockers"] == []
    assert classification["cluster_leak"] == []
    assert classification["train_stratum_n"] == 4
    assert classification["development_selection_stratum_n"] == 4
    assert report["train"]["cluster_n"] == 140
    assert report["train"]["stable_exact_n"] == 70
    assert report["train"]["stable_mismatch_n"] == 70
    assert report["development_selection"]["cluster_n"] == 60
    assert report["development_selection"]["stable_exact_n"] == 30
    assert report["development_selection"]["stable_mismatch_n"] == 30
    assert report["objective"]["optimizer_cluster_n"] == 200
    assert report["objective"]["target_predictors"] == ["taxonomy"]
    assert report["call_envelope"]["task_model_calls_per_metric_call"] == 2
    assert report["call_envelope"]["metric_calls_per_reflection_minibatch"] == 6
    assert report["call_envelope"]["task_model_calls_per_full_selection_evaluation"] == 120
    assert report["taxonomy_gold"]["cluster_n"] == 200
    assert report["taxonomy_gold"]["stable_exact_n"] == 100
    assert report["taxonomy_gold"]["stable_mismatch_n"] == 100
    assert report["taxonomy_gold"]["support"]["event_family"] == {
        "product_service_change": 100,
        "other": 100,
    }


def test_readiness_keeps_members_when_gold_differs() -> None:
    """Different accepted labels remain scoreable within one split group."""

    shadowed = _episode(1, target=True)
    elected = _episode(2, target=False).model_copy(update={"cluster_id": shadowed.cluster_id})
    other = _episode(3, target=True)
    episodes = (shadowed, elected, other)
    plan = build_gepa_objective_plan(episodes, "classification")

    report = build_readiness_report(
        plan, episodes=episodes, identity={"dataset": "test"}, coverage={}, target="classification"
    )

    assert report["corpus"]["case_n"] == 3
    assert report["taxonomy_gold"]["cluster_n"] == 2
    assert report["taxonomy_gold"]["cluster_n"] == len(plan.optimizer_cluster_ids)
    # Both members contribute label support; independent cluster support stays separate.
    assert report["taxonomy_gold"]["support"]["event_family"] == {"other": 2, "product_service_change": 1}
    shadowed_case = _case(plan, shadowed.case_id)
    assert shadowed_case.disposition == "included"
    assert shadowed.case_id in plan.optimizer_case_ids
    assert {case["case_id"] for case in report["case_dispositions"]} == {episode.case_id for episode in episodes}


def test_readiness_counts_all_cases_and_independent_groups_separately() -> None:
    """All nine cases survive in three independently split groups."""

    episodes = tuple(
        _episode(index, target=index % 2 == 1).model_copy(update={"cluster_id": f"cluster-{index % 3}"})
        for index in range(1, 10)
    )
    plan = build_gepa_objective_plan(episodes, "classification")

    report = build_readiness_report(
        plan, episodes=episodes, identity={"dataset": "test"}, coverage={}, target="classification"
    )

    assert len(plan.optimizer_case_ids) == 9
    assert len(plan.optimizer_cluster_ids) == 3
    assert report["taxonomy_gold"]["cluster_n"] == 3
    assert report["objective"]["excluded_case_n"] == 0
    assert dict(plan.exclusion_reasons) == {}
