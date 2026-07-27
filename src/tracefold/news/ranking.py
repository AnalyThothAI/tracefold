from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .classification import SEVERITY_VALUES, has_historical_marker
from .models import ThreatLevel

SOURCE_TIER_SCORE: Final = {1: 100, 2: 75, 3: 50, 4: 25}
DIPLOMACY_FLASHPOINT_BOOST: Final = 18
ENTITY_CORROBORATION_PER_SOURCE: Final = 4

# WorldMonitor's narrow, explicit geopolitical boost. Both a diplomacy/action
# word and a flashpoint entity must be present.
DIPLOMACY_KEYWORDS: Final = (
    "ceasefire",
    "truce",
    "armistice",
    "treaty",
    "accord",
    "pact",
    "diplomatic",
    "diplomacy",
    "mediate",
    "mediator",
    "negotiation",
    "negotiations",
    "negotiate",
    "normalization",
    "normalisation",
)
FLASHPOINT_KEYWORDS: Final = (
    "iran",
    "tehran",
    "russia",
    "moscow",
    "china",
    "beijing",
    "taiwan",
    "ukraine",
    "kyiv",
    "north korea",
    "pyongyang",
    "israel",
    "gaza",
    "west bank",
    "syria",
    "damascus",
    "yemen",
    "hezbollah",
    "hamas",
    "kremlin",
    "pentagon",
    "nato",
)
DIPLOMACY_FLASHPOINT_PAIRS: Final = (
    ("iran", "deal"),
    ("iran", "talks"),
    ("iran", "ceasefire"),
    ("iran", "treaty"),
    ("iran", "accord"),
    ("iran", "peace"),
    ("israel", "ceasefire"),
    ("israel", "truce"),
    ("israel", "accord"),
    ("gaza", "ceasefire"),
    ("gaza", "truce"),
    ("ukraine", "ceasefire"),
    ("ukraine", "talks"),
    ("russia", "talks"),
    ("russia", "treaty"),
    ("hamas", "truce"),
    ("hezbollah", "truce"),
    ("syria", "ceasefire"),
    ("china", "talks"),
    ("china", "accord"),
    ("taiwan", "talks"),
    ("yemen", "ceasefire"),
    ("north korea", "talks"),
    ("pyongyang", "talks"),
)

OPINION_MARKERS: Final = (
    "/opinion/",
    "/commentisfree/",
    "opinion:",
    "analysis:",
    "column:",
    "editorial:",
    "explainer:",
)
FEEL_GOOD_MARKERS: Final = (
    "heartwarming",
    "feel-good",
    "feel good",
    "inspiring story",
    "reunion",
    "lifestyle",
)
EPHEMERAL_LIVE_MARKERS: Final = (
    "watch live:",
    "live briefing:",
    "live hearing:",
    "stream live:",
)


def _normalized(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", value.lower()).split())


def _js_round(value: float) -> int:
    """Match JavaScript Math.round for the non-negative scoring domain."""

    return math.floor(value + 0.5)


def _contains_token(text: str, keyword: str) -> bool:
    return re.search(rf"(?:^|\s){re.escape(keyword)}", text) is not None


def has_diplomacy_flashpoint_signal(title: str) -> bool:
    text = _normalized(title)
    if any(
        _contains_token(text, entity) and _contains_token(text, action) for entity, action in DIPLOMACY_FLASHPOINT_PAIRS
    ):
        return True
    return any(_contains_token(text, keyword) for keyword in DIPLOMACY_KEYWORDS) and any(
        _contains_token(text, keyword) for keyword in FLASHPOINT_KEYWORDS
    )


def diplomacy_entity_keys(title: str) -> tuple[str, ...]:
    text = _normalized(title)
    keys = tuple(
        f"{entity}:{action}"
        for entity, action in DIPLOMACY_FLASHPOINT_PAIRS
        if _contains_token(text, entity) and _contains_token(text, action)
    )
    if keys:
        return keys
    if has_diplomacy_flashpoint_signal(title):
        return ("generic:diplomacy-flashpoint",)
    return ()


def promote_diplomacy_severity(
    level: ThreatLevel,
    *,
    title: str,
    tier12_origin_count: int,
) -> ThreatLevel:
    if level in {"critical", "high"} or has_historical_marker(title):
        return level
    if tier12_origin_count >= 3 and has_diplomacy_flashpoint_signal(title):
        return "high"
    return level


def importance_score(
    *,
    level: ThreatLevel,
    tier: int,
    corroboration_count: int,
    published_at_ms: int,
    now_ms: int,
    title: str,
    entity_corroboration_count: int = 0,
) -> int:
    """WorldMonitor 55/20/15/10 scoring with narrow boosts."""

    return int(
        importance_factors(
            level=level,
            tier=tier,
            corroboration_count=corroboration_count,
            published_at_ms=published_at_ms,
            now_ms=now_ms,
            title=title,
            entity_corroboration_count=entity_corroboration_count,
        )["total"]
    )


def importance_factors(
    *,
    level: ThreatLevel,
    tier: int,
    corroboration_count: int,
    published_at_ms: int,
    now_ms: int,
    title: str,
    entity_corroboration_count: int = 0,
) -> dict[str, float | int | str]:
    scoring_corroboration_count = max(int(corroboration_count), int(entity_corroboration_count))
    corroboration = min(max(scoring_corroboration_count, 0), 5) * 20
    age_ms = max(0, now_ms - published_at_ms)
    recency = max(0.0, 1.0 - age_ms / 86_400_000) * 100
    base = _js_round(
        SEVERITY_VALUES[level] * 0.55
        + SOURCE_TIER_SCORE.get(int(tier), 25) * 0.20
        + corroboration * 0.15
        + recency * 0.10
    )
    boost = DIPLOMACY_FLASHPOINT_BOOST if has_diplomacy_flashpoint_signal(title) else 0
    entity_boost = min(max(entity_corroboration_count, 0), 5) * ENTITY_CORROBORATION_PER_SOURCE
    return {
        "severity_level": level,
        "severity_points": round(SEVERITY_VALUES[level] * 0.55, 2),
        "source_tier": tier,
        "source_points": round(SOURCE_TIER_SCORE.get(int(tier), 25) * 0.20, 2),
        "physical_source_count": corroboration_count,
        "scoring_corroboration_count": scoring_corroboration_count,
        "corroboration_points": round(corroboration * 0.15, 2),
        "recency_points": round(recency * 0.10, 2),
        "diplomacy_flashpoint_boost": boost,
        "entity_corroboration_boost": entity_boost,
        "total": _js_round(base + boost + entity_boost),
    }


def is_delayed_brief_excluded(*, title: str, url: str, description: str) -> bool:
    text = f"{title} {url} {description}".lower()
    return any(marker in text for marker in OPINION_MARKERS + FEEL_GOOD_MARKERS + EPHEMERAL_LIVE_MARKERS)


def select_top_stories(
    stories: Sequence[Mapping[str, Any]],
    *,
    limit: int = 8,
    max_per_source: int = 3,
) -> list[dict[str, Any]]:
    ordered = sorted(
        (dict(story) for story in stories),
        key=lambda story: (
            -int(story["importance_score"]),
            -int(story["last_published_at_ms"]),
            str(story["story_id"]),
        ),
    )
    selected: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    for story in ordered:
        source = str(story["representative_source_id"])
        if source_counts.get(source, 0) >= max_per_source:
            continue
        if len(selected) >= limit:
            break
        selected.append(story)
        source_counts[source] = source_counts.get(source, 0) + 1
    return selected


__all__ = [
    "diplomacy_entity_keys",
    "importance_factors",
    "importance_score",
    "is_delayed_brief_excluded",
    "promote_diplomacy_severity",
    "select_top_stories",
]
