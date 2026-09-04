"""Issue #501: the Objective Plan is every Gold-bearing, replayable case; owner columns grant nothing."""

from __future__ import annotations

from tracefold.news.learning.objective import build_gepa_objective_plan, build_readiness_report

from .test_news_program_gepa_real import _episode


def _case(plan: object, case_id: str) -> object:
    return next(case for case in plan.cases if case.case_id == case_id)


def test_every_valid_gold_case_with_a_replayable_stable_answer_is_included() -> None:
    mismatch = _episode(1, target=True)
    exact = _episode(2, target=False)
    unowned = _episode(3, target=True, first_bad_owner_explicit=None, first_bad_owner=None)
    wrong_owner = _episode(5, target=True, first_bad_owner_explicit="triage_prompt", first_bad_owner="triage_prompt")
    owned_exact = _episode(6, target=False, first_bad_owner_explicit="taxonomy", first_bad_owner="taxonomy")

    plan = build_gepa_objective_plan((mismatch, exact, unowned, wrong_owner, owned_exact))

    for episode in (mismatch, exact, unowned, wrong_owner, owned_exact):
        case = _case(plan, episode.case_id)
        assert case.disposition == "included", episode.case_id
        assert case.predictors == ("taxonomy",)
        assert case.reason == "accepted_taxonomy_gold_with_replayable_stable"
    assert _case(plan, mismatch.case_id).stable_exact is False
    assert _case(plan, exact.case_id).stable_exact is True
    assert _case(plan, wrong_owner.case_id).owner == "triage_prompt"
    assert plan.stable_exact_n == 2 and plan.stable_mismatch_n == 3
    assert plan.target_predictors == ("taxonomy",)
    assert len(plan.optimizer_cluster_ids) == 5
    assert plan.exclusion_reasons == {}


def test_a_case_without_a_replayable_stable_answer_or_valid_gold_is_excluded_with_a_reason() -> None:
    no_stable = _episode(1, target=True).model_copy(update={"production_judgment": None})
    bad_gold = _episode(2, target=False, taxonomy={"event_family": "whale"})

    plan = build_gepa_objective_plan((no_stable, bad_gold))

    assert _case(plan, no_stable.case_id).disposition == "excluded"
    assert _case(plan, no_stable.case_id).reason == "stable_output_absent"
    assert _case(plan, bad_gold.case_id).reason == "accepted_taxonomy_gold_invalid"
    assert plan.optimizer_case_ids == ()
    assert plan.blocking_reasons == ("no_taxonomy_gold_clusters",)
    assert plan.exclusion_reasons == {"accepted_taxonomy_gold_invalid": 1, "stable_output_absent": 1}


def test_connected_fact_cluster_casts_one_optimizer_vote() -> None:
    included = _episode(1, target=True)
    shadow = included.model_copy(update={"case_id": "shadow"})
    other = _episode(2, target=False)

    plan = build_gepa_objective_plan((included, shadow, other))

    assert len(plan.optimizer_case_ids) == 2
    assert len({episode.cluster_id for episode in plan.optimizer_episodes}) == 2
    assert sum(case.reason == "cluster_representative_shadowed" for case in plan.cases) == 1


def test_one_cluster_cannot_be_split_and_blocks_with_the_split_reason() -> None:
    plan = build_gepa_objective_plan((_episode(1, target=True),))

    assert plan.optimizer_case_ids
    assert plan.split is None
    assert plan.blocking_reasons == ("split_requires_two_clusters",)


def test_objective_compilability_and_development_profile_readiness_are_separate() -> None:
    episodes = tuple(_episode(index, target=index % 2 == 1) for index in range(1, 13))
    plan = build_gepa_objective_plan(episodes)

    report = build_readiness_report(plan, episodes=episodes, identity={"dataset": "test"}, coverage={})

    assert report["schema"] == "tracefold.news.gepa_readiness_report.v4"
    assert report["objective"]["compilable"] is True
    assert report["objective"]["blockers"] == []
    assert report["development_profile"]["ready"] is False
    assert report["development_profile"]["blockers"]
    assert "outcome" not in report
    assert "blocking" not in report
    assert "owner_distribution" not in report
    assert "exact_gold_coverage" not in report


def test_complete_profile_reports_halves_and_taxonomy_support_without_a_calibration_gate() -> None:
    episode_rows = []
    for index in range(1, 201):
        episode = _episode(index, target=index % 2 == 1)
        review = dict(episode.accepted_review)
        review["should_push"] = "must_push" if index % 2 else "should_hold"
        review["novelty"] = {"judgment": "new_fact", "duplicate_of": ""}
        episode_rows.append(episode.model_copy(update={"accepted_review": review}))
    episodes = tuple(episode_rows)
    plan = build_gepa_objective_plan(episodes)
    coverage = {
        "boundary_cluster_n": 30,
        "retention_cluster_n": 100,
        "negative_cluster_n": 50,
        "safety_cluster_n": 1,
        "stratum_n": 3,
    }

    report = build_readiness_report(plan, episodes=episodes, identity={"dataset": "test"}, coverage=coverage)

    assert report["development_profile"]["ready"] is True
    assert report["development_profile"]["blockers"] == []
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


def test_readiness_summarizes_cluster_representatives_when_member_gold_differs() -> None:
    """#534: media members of one fact carry different accepted Gold; readiness summarizes the elected one.

    The freeze already summarizes one representative per connected fact cluster, so a corpus it sealed must
    not make `news learning readiness` fail closed on `news_taxonomy_summary_cluster_conflict`.
    """

    shadowed = _episode(1, target=True)
    elected = _episode(2, target=False).model_copy(update={"cluster_id": shadowed.cluster_id})
    other = _episode(3, target=True)
    episodes = (shadowed, elected, other)
    plan = build_gepa_objective_plan(episodes)

    report = build_readiness_report(plan, episodes=episodes, identity={"dataset": "test"}, coverage={})

    assert report["corpus"]["case_n"] == 3
    assert report["taxonomy_gold"]["cluster_n"] == 2
    assert report["taxonomy_gold"]["cluster_n"] == len(plan.optimizer_cluster_ids)
    # The elected member's Gold, not the shadowed member's, is the one the summary supports.
    assert report["taxonomy_gold"]["support"]["event_family"] == {"other": 1, "product_service_change": 1}
    shadowed_case = _case(plan, shadowed.case_id)
    assert shadowed_case.disposition == "excluded"
    assert shadowed_case.reason == "cluster_representative_shadowed"
    assert shadowed.case_id in plan.excluded_case_ids
    assert {case["case_id"] for case in report["case_dispositions"]} == {episode.case_id for episode in episodes}


def test_readiness_never_summarizes_two_representatives_of_one_cluster() -> None:
    """Two conflicting representatives cannot exist: election is by cluster, so the summary sees one each."""

    episodes = tuple(
        _episode(index, target=index % 2 == 1).model_copy(update={"cluster_id": f"cluster-{index % 3}"})
        for index in range(1, 10)
    )
    plan = build_gepa_objective_plan(episodes)

    report = build_readiness_report(plan, episodes=episodes, identity={"dataset": "test"}, coverage={})

    assert len(plan.optimizer_case_ids) == len(plan.optimizer_cluster_ids) == 3
    assert report["taxonomy_gold"]["cluster_n"] == 3
    assert report["objective"]["excluded_case_n"] == 6
    assert dict(plan.exclusion_reasons) == {"cluster_representative_shadowed": 6}
