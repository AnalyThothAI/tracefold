from __future__ import annotations

import pytest

from tracefold.news.ranking import public_recency_weight, score_public_cluster, select_top_stories

NOW_MS = 1_785_600_000_000


def test_public_cluster_score_matches_pinned_keyword_fallback_formula() -> None:
    cluster = {
        "primary_title": "Iran missile attack killed troops in Tehran crisis",
        "primary_source": "Reuters",
        "sources": ["Reuters"],
        "source_count": 1,
        "upstream_importance_score": 80,
        "threat": {"level": "high", "source": "keyword"},
        "is_alert": True,
    }

    assert score_public_cluster(cluster) == pytest.approx(586.875)


@pytest.mark.parametrize(
    ("threat_source", "expected"),
    (("llm", 314.0), ("keyword", 75.775)),
)
def test_public_finance_demotion_preserves_only_strong_non_keyword_signal(
    threat_source: str,
    expected: float,
) -> None:
    cluster = {
        "primary_title": "Startup CEO reports quarterly profit",
        "primary_source": "BBC World",
        "sources": ["BBC World"],
        "upstream_importance_score": 60,
        "threat": {"level": "high", "source": threat_source},
    }

    assert score_public_cluster(cluster) == pytest.approx(expected)


def test_public_selector_reserves_the_highest_ranked_brief_eligible_story() -> None:
    alerts = [
        {
            "story_id": f"alert-{index}",
            "primary_title": f"Iran war missile attack killed troops in airstrike on base {index}",
            "primary_source": f"Alert Wire {index}",
            "primary_published_at_ms": NOW_MS - 6 * 60_000,
            "last_updated_ms": NOW_MS - 6 * 60_000,
            "sources": [f"Alert Wire {index}"],
            "source_count": 1,
            "is_alert": True,
        }
        for index in range(12)
    ]
    eligible = {
        "story_id": "eligible",
        "primary_title": "Mountaineer killed in Pakistan avalanche, his company confirms",
        "primary_source": "BBC World",
        "primary_published_at_ms": NOW_MS - 6 * 60 * 60_000,
        "last_updated_ms": NOW_MS - 6 * 60 * 60_000,
        "sources": ["BBC World", "CNN", "Sky News", "CBS News"],
        "source_count": 4,
        "is_alert": False,
    }
    stats: dict[str, int | bool] = {}

    selected = select_top_stories([*alerts, eligible], now_ms=NOW_MS, stats=stats)

    assert [story["story_id"] for story in selected] == [
        "alert-0",
        "alert-1",
        "alert-2",
        "alert-3",
        "alert-4",
        "alert-5",
        "alert-6",
        "eligible",
    ]
    assert stats == {
        "considered": 13,
        "admissibility_dropped": 0,
        "source_cap_dropped": 0,
        "overflow_dropped": 5,
        "brief_eligible_considered": 1,
        "brief_eligible_promoted": True,
    }


def test_public_selector_computes_cross_publisher_entity_corroboration_within_24_hours() -> None:
    recent = [
        {
            "story_id": "reuters",
            "primary_title": "Delegation arrives for scheduled meeting",
            "primary_source": "Reuters",
            "primary_published_at_ms": NOW_MS - 60_000,
            "last_updated_ms": NOW_MS - 60_000,
            "member_titles": ["Iran peace talks resume in Geneva"],
            "sources": ["Reuters"],
        },
        {
            "story_id": "ap",
            "primary_title": "Officials prepare for another round",
            "primary_source": "AP News",
            "primary_published_at_ms": NOW_MS - 120_000,
            "last_updated_ms": NOW_MS - 120_000,
            "member_titles": ["Iran talks delegation arrives in Geneva"],
            "sources": ["AP News"],
        },
    ]
    stale = {
        "story_id": "stale",
        "primary_title": "Officials prepare for talks",
        "primary_source": "BBC World",
        "primary_published_at_ms": NOW_MS - 25 * 60 * 60_000,
        "last_updated_ms": NOW_MS - 25 * 60 * 60_000,
        "member_titles": ["Iran talks delegation arrived yesterday"],
        "sources": ["BBC World"],
        "entity_corroboration": True,
        "corroboration_source_count": 99,
    }

    selected = select_top_stories([*recent, stale], now_ms=NOW_MS)

    assert [story["story_id"] for story in selected] == ["reuters", "ap"]
    assert [story["entity_corroboration"] for story in selected] == [True, True]
    assert [story["corroboration_source_count"] for story in selected] == [2, 2]


def test_public_selector_attributes_every_candidate_to_source_cap_before_overflow() -> None:
    sources = ["Shared Wire", "Shared Wire", "Shared Wire", "Wire A", "Wire B", "Shared Wire", "Wire C", "Wire D"]
    candidates = [
        {
            "story_id": f"story-{index}",
            "primary_title": f"Breaking military attack update {index}",
            "primary_source": source,
            "primary_published_at_ms": NOW_MS,
            "last_updated_ms": NOW_MS,
            "sources": [source],
            "upstream_importance_score": 100,
            "is_alert": True,
        }
        for index, source in enumerate(sources)
    ]
    stats: dict[str, int | bool] = {}

    selected = select_top_stories(candidates, now_ms=NOW_MS, limit=5, stats=stats)

    assert [story["story_id"] for story in selected] == ["story-0", "story-1", "story-2", "story-3", "story-4"]
    assert stats == {
        "considered": 8,
        "admissibility_dropped": 0,
        "source_cap_dropped": 1,
        "overflow_dropped": 2,
        "brief_eligible_considered": 0,
        "brief_eligible_promoted": False,
    }


def test_public_selector_uses_only_the_four_pinned_admission_paths() -> None:
    candidates = [
        {
            "story_id": "publisher-diversity",
            "primary_title": "Routine scheduled cabinet meeting",
            "primary_source": "Unknown A",
            "last_updated_ms": NOW_MS,
            "sources": ["Unknown A", "Unknown B"],
        },
        {
            "story_id": "alert",
            "primary_title": "Routine scheduled cabinet meeting",
            "primary_source": "Unknown C",
            "last_updated_ms": NOW_MS,
            "sources": ["Unknown C"],
            "is_alert": True,
        },
        {
            "story_id": "score",
            "primary_title": "Routine scheduled cabinet meeting",
            "primary_source": "Unknown D",
            "last_updated_ms": NOW_MS,
            "sources": ["Unknown D"],
            "upstream_importance_score": 100,
        },
        {
            "story_id": "dropped",
            "primary_title": "Routine scheduled cabinet meeting",
            "primary_source": "Unknown E",
            "last_updated_ms": NOW_MS,
            "sources": ["Unknown E"],
        },
    ]
    stats: dict[str, int | bool] = {}

    selected = select_top_stories(candidates, now_ms=NOW_MS, stats=stats)

    assert {story["story_id"] for story in selected} == {"publisher-diversity", "alert", "score"}
    assert stats["admissibility_dropped"] == 1


def test_public_recency_weight_reaches_its_floor_after_eight_hours() -> None:
    assert public_recency_weight({"last_updated_ms": NOW_MS - 4 * 60 * 60_000}, now_ms=NOW_MS) == 0.75
    assert public_recency_weight({"last_updated_ms": NOW_MS - 8 * 60 * 60_000}, now_ms=NOW_MS) == 0.5
    assert public_recency_weight({"last_updated_ms": NOW_MS - 20 * 60 * 60_000}, now_ms=NOW_MS) == 0.5
    assert public_recency_weight({}, now_ms=NOW_MS) == 1.0
