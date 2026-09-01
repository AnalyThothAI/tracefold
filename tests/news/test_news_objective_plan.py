"""Issue #456: the Objective Plan contains taxonomy targets and exact controls only."""

from __future__ import annotations

from tracefold.news.learning.objective import build_gepa_objective_plan, build_readiness_report

from .test_news_program_gepa_real import _episode


def _case(plan: object, case_id: str) -> object:
    return next(case for case in plan.cases if case.case_id == case_id)


def _without_owner(episode: object) -> object:
    review = dict(episode.accepted_review)
    review["first_bad_owner_explicit"] = None
    review["first_bad_owner"] = None
    return episode.model_copy(update={"accepted_review": review})


def test_only_explicit_taxonomy_mismatches_are_targets() -> None:
    target = _episode(1, target=True)
    control = _episode(2, target=False)
    unowned = _without_owner(_episode(3, target=True))
    wrong_owner = _episode(5, target=True)
    wrong_owner_review = dict(wrong_owner.accepted_review)
    wrong_owner_review["first_bad_owner_explicit"] = "triage_prompt"
    wrong_owner_review["first_bad_owner"] = "triage_prompt"
    wrong_owner = wrong_owner.model_copy(update={"accepted_review": wrong_owner_review})

    plan = build_gepa_objective_plan((target, control, unowned, wrong_owner))

    assert _case(plan, target.case_id).disposition == "target"
    assert _case(plan, target.case_id).predictors == ("event_semantics",)
    assert _case(plan, control.case_id).disposition == "control"
    assert _case(plan, unowned.case_id).reason == "taxonomy_mismatch_without_explicit_owner"
    assert _case(plan, wrong_owner.case_id).reason == "non_taxonomy_owner:triage_prompt"
    assert set(plan.observed_failure_cluster_ids) == {
        target.cluster_id,
        unowned.cluster_id,
        wrong_owner.cluster_id,
    }


def test_an_explicit_owner_cannot_turn_an_exact_case_into_a_control() -> None:
    exact = _episode(2, target=False)
    review = dict(exact.accepted_review)
    review["first_bad_owner_explicit"] = "taxonomy"
    review["first_bad_owner"] = "taxonomy"
    exact = exact.model_copy(update={"accepted_review": review})

    plan = build_gepa_objective_plan((exact,))

    assert plan.cases[0].disposition == "excluded"
    assert plan.cases[0].reason == "explicit_owner_on_taxonomy_control"


def test_connected_fact_cluster_casts_one_optimizer_vote() -> None:
    target = _episode(1, target=True)
    shadow = target.model_copy(update={"case_id": "shadow"})
    control = _episode(2, target=False)

    plan = build_gepa_objective_plan((target, shadow, control))

    assert len(plan.optimizer_case_ids) == 2
    assert len({episode.cluster_id for episode in plan.optimizer_episodes}) == 2
    assert sum(case.reason.startswith("cluster_representative_shadowed") for case in plan.cases) == 1


def test_objective_compilability_and_development_profile_readiness_are_separate() -> None:
    episodes = tuple(_episode(index, target=index % 2 == 1) for index in range(1, 13))
    plan = build_gepa_objective_plan(episodes)

    report = build_readiness_report(plan, episodes=episodes, identity={"dataset": "test"}, coverage={})

    assert report["objective"]["compilable"] is True
    assert report["objective"]["blockers"] == []
    assert report["development_profile"]["ready"] is False
    assert report["development_profile"]["blockers"]
    assert "outcome" not in report
    assert "blocking" not in report


def test_complete_profile_reports_train_selection_counts_and_taxonomy_support() -> None:
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
        "calibration": {
            "cluster_n": 50,
            "disagreement_unadjudicated_n": 0,
            "kappa": {"event_family": 0.8, "change_state": 0.8, "assertion_status": 0.8},
            "subject_mean_set_f1": 0.8,
        },
    }

    report = build_readiness_report(plan, episodes=episodes, identity={"dataset": "test"}, coverage=coverage)

    assert report["development_profile"]["ready"] is True
    assert report["train"]["taxonomy_target_cluster_n"] == 70
    assert report["train"]["taxonomy_control_cluster_n"] == 70
    assert report["development_selection"]["taxonomy_target_cluster_n"] == 30
    assert report["development_selection"]["taxonomy_control_cluster_n"] == 30
    assert report["call_envelope"]["task_model_calls_per_metric_call"] == 2
    assert report["call_envelope"]["task_model_calls_per_full_selection_evaluation"] == 120
    assert report["taxonomy_gold"]["cluster_n"] == 200
    assert report["taxonomy_gold"]["support"]["event_family"] == {
        "product_service_change": 100,
        "other": 100,
    }
