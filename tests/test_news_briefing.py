from __future__ import annotations

from tracefold.news.briefing import plan_brief_selection


def test_brief_portfolio_is_utility_bounded_and_can_return_fewer_than_eight() -> None:
    stories = [story(f"high-{index}", entity=f"entity-{index}", impact=80, priority=80) for index in range(3)] + [
        story(f"noise-{index}", entity=f"noise-{index}", impact=20, priority=20) for index in range(10)
    ]

    grouping, selection, bundle = plan_brief_selection(
        stories,
        cutoff_at_ms=1_000,
    )

    assert grouping["fallback_used"] is True
    assert grouping["embedding_model"] == "deterministic-fallback-v1"
    assert selection["selected_story_ids"] == ["high-0", "high-1", "high-2"]
    assert len(bundle.stories) == 3
    assert all(
        decision["reason"] == "below_utility_floor"
        for decision in selection["decisions"]
        if decision["story_id"].startswith("noise-")
    )


def test_critical_story_overrides_coverage_penalty_and_concentration_caps() -> None:
    repeated = [
        story(
            f"repeated-{index}",
            entity="fed",
            impact=90 if index == 2 else 80,
            priority=90,
            recent_brief_coverage_penalty=120 if index == 2 else 0,
        )
        for index in range(3)
    ]

    _grouping, selection, _bundle = plan_brief_selection(
        repeated,
        cutoff_at_ms=1_000,
    )

    assert selection["selected_story_ids"] == [
        "repeated-0",
        "repeated-1",
        "repeated-2",
    ]
    critical = next(decision for decision in selection["decisions"] if decision["story_id"] == "repeated-2")
    assert critical["critical_override"] is True
    assert critical["marginal_utility"] < 54
    assert "narrative_cap" in critical["constraints"]


def test_noncritical_redundancy_is_rejected_after_two_same_narrative_stories() -> None:
    repeated = [
        story(
            f"repeated-{index}",
            entity="fed",
            impact=80,
            priority=80 - index,
        )
        for index in range(3)
    ]

    _grouping, selection, _bundle = plan_brief_selection(
        repeated,
        cutoff_at_ms=1_000,
    )

    assert selection["selected_story_ids"] == ["repeated-0", "repeated-1"]
    rejected = next(decision for decision in selection["decisions"] if decision["story_id"] == "repeated-2")
    assert rejected["selected"] is False
    assert "narrative_cap" in rejected["constraints"]
    assert any(adjustment["kind"] == "narrative_redundancy" for adjustment in rejected["adjustments"])


def test_selection_fingerprint_is_stable_when_only_cutoff_changes() -> None:
    stories = [story("story-1", entity="fed", impact=80, priority=80)]

    first_grouping, first_selection, first_bundle = plan_brief_selection(
        stories,
        cutoff_at_ms=1_000,
    )
    second_grouping, second_selection, second_bundle = plan_brief_selection(
        stories,
        cutoff_at_ms=2_000,
    )

    assert first_grouping["grouping_snapshot_id"] == second_grouping["grouping_snapshot_id"]
    assert first_selection["selection_fingerprint"] == second_selection["selection_fingerprint"]
    assert first_bundle.synthesis_input_hash == second_bundle.synthesis_input_hash
    assert first_selection["selection_id"] == second_selection["selection_id"]
    assert first_bundle.evidence_cutoff_at_ms == second_bundle.evidence_cutoff_at_ms


def test_brief_evidence_is_strictly_bounded_and_conflict_receipt_matches_retained_refs() -> None:
    evidence_articles = [
        {
            "evidence_ref": f"revision-{index}",
            "title": f"Report {index}",
            "snippet": "",
            "observed_at_ms": index,
            "origin_relation": "independent",
            "reporting_origin_id": f"origin-{index}",
            "development_relation": "correction" if index < 6 else "follow_up",
        }
        for index in range(7)
    ]
    conflicts = [
        {
            "left_revision_id": f"revision-{index}",
            "right_revision_id": f"revision-{index + 1}",
            "kind": "action_conflict",
            "values": ["increase", "decrease"],
        }
        for index in range(6)
    ]
    candidate = story(
        "contested-story",
        entity="fed",
        impact=90,
        priority=90,
        evidence_articles=evidence_articles,
        evidence_posture="contested",
        evidence_factors={
            "has_material_conflict": True,
            "material_conflicts": conflicts,
        },
    )

    _grouping, _selection, bundle = plan_brief_selection(
        [candidate],
        cutoff_at_ms=1_000,
    )

    selected = bundle.stories[0]
    assert len(selected["evidence_articles"]) == 5
    selected_refs = {article["evidence_ref"] for article in selected["evidence_articles"]}
    retained = selected["evidence_factors"]["material_conflicts"]
    assert retained
    assert all(
        {
            conflict["left_revision_id"],
            conflict["right_revision_id"],
        }
        <= selected_refs
        for conflict in retained
    )
    assert selected["evidence_factors"]["evidence_bounding"] == {
        "maximum_articles": 5,
        "selected_article_count": 5,
        "conflicts_retained": len(retained),
    }


def story(
    story_id: str,
    *,
    entity: str,
    impact: int,
    priority: int,
    recent_brief_coverage_penalty: int = 0,
    evidence_articles: list[dict[str, object]] | None = None,
    evidence_posture: str = "independently_corroborated",
    evidence_factors: dict[str, object] | None = None,
) -> dict[str, object]:
    articles = evidence_articles or [
        {
            "evidence_ref": f"{story_id}-revision",
            "title": f"{story_id} title",
            "snippet": "",
            "observed_at_ms": 1,
            "origin_relation": "originating",
            "reporting_origin_id": f"{story_id}-origin",
            "development_relation": "initial",
        }
    ]
    return {
        "story_id": story_id,
        "material_evidence_hash": f"{story_id}-material-hash",
        "title": f"{story_id} title",
        "snippet": "",
        "event_core": {
            "entities": [entity],
            "locations": [entity],
            "actions": ["decrease"],
        },
        "evidence_posture": evidence_posture,
        "evidence_factors": evidence_factors or {},
        "impact_score": impact,
        "impact_profile": {
            "dimensions": {
                "market": 80,
                "macro": 60,
                "policy": 40,
                "systemic": 0,
                "geopolitical": 0,
            }
        },
        "priority_score": priority,
        "priority_profile": {},
        "material_evolution_state": "first_report",
        "last_material_evidence_at_ms": 1_000,
        "recent_brief_coverage_penalty": recent_brief_coverage_penalty,
        "evidence_articles": articles,
    }
