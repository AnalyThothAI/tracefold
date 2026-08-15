from __future__ import annotations

import pytest

from tracefold.news.classification import classify_by_keyword, has_historical_marker
from tracefold.news.ranking import importance_factors


@pytest.mark.parametrize(
    ("title", "expected"),
    (
        ("On this day in 1969: The moon landing", True),
        ("Missile launch reported on April 26, 2026", False),
        ("Stocks suffer flashback to March 2020 crash", False),
    ),
)
def test_pinned_historical_marker_semantics_remain_worldmonitor_compatible(title: str, expected: bool) -> None:
    assert has_historical_marker(title, now_ms=1_776_211_200_000) is expected


def test_pinned_classifier_remains_worldmonitor_compatible() -> None:
    assert classify_by_keyword("Iran launches a missile").model_dump() == {
        "level": "high",
        "category": "military",
        "confidence": 0.8,
        "source": "keyword",
    }


def test_pinned_importance_formula_remains_worldmonitor_compatible() -> None:
    factors = importance_factors(
        level="high",
        tier=1,
        corroboration_count=3,
        published_at_ms=1_779_000_000_000,
        now_ms=1_779_000_000_000,
        title="Central bank statement",
    )
    assert factors == {
        "severity_level": "high",
        "severity_points": 41.25,
        "source_tier": 1,
        "source_points": 20.0,
        "reporting_origin_count": 3,
        "scoring_corroboration_count": 3,
        "corroboration_points": 9.0,
        "recency_points": 10.0,
        "diplomacy_flashpoint_boost": 0,
        "entity_corroboration_boost": 0,
        "total": 80,
    }
