from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .classification import SEVERITY_VALUES, has_historical_marker
from .models import ThreatLevel
from .sources import reporting_origin_tier

SOURCE_TIER_SCORE: Final = {1: 100, 2: 75, 3: 50, 4: 25}
DIPLOMACY_FLASHPOINT_BOOST: Final = 18
ENTITY_CORROBORATION_PER_SOURCE: Final = 4

PUBLIC_SELECTOR_VERSION: Final = "worldmonitor_public_insights_0e8785c4"

MILITARY_KEYWORDS: Final = (
    "war",
    "armada",
    "invasion",
    "airstrike",
    "strike",
    "missile",
    "troops",
    "deployed",
    "offensive",
    "artillery",
    "bomb",
    "combat",
    "fleet",
    "warship",
    "carrier",
    "navy",
    "airforce",
    "deployment",
    "mobilization",
    "attack",
)
VIOLENCE_KEYWORDS: Final = (
    "killed",
    "dead",
    "death",
    "shot",
    "blood",
    "massacre",
    "slaughter",
    "fatalities",
    "casualties",
    "wounded",
    "injured",
    "murdered",
    "execution",
    "crackdown",
    "violent",
    "clashes",
    "gunfire",
    "shooting",
)
UNREST_KEYWORDS: Final = (
    "protest",
    "protests",
    "uprising",
    "revolt",
    "revolution",
    "riot",
    "riots",
    "demonstration",
    "unrest",
    "dissent",
    "rebellion",
    "insurgent",
    "overthrow",
    "coup",
    "martial law",
    "curfew",
    "shutdown",
    "blackout",
)
CRISIS_KEYWORDS: Final = (
    "crisis",
    "emergency",
    "catastrophe",
    "disaster",
    "collapse",
    "humanitarian",
    "sanctions",
    "ultimatum",
    "threat",
    "retaliation",
    "escalation",
    "tensions",
    "breaking",
    "urgent",
    "developing",
    "exclusive",
)
FINANCE_DEMOTE_KEYWORDS: Final = (
    "ceo",
    "earnings",
    "stock",
    "startup",
    "data center",
    "datacenter",
    "revenue",
    "quarterly",
    "profit",
    "investor",
    "ipo",
    "funding",
    "valuation",
)

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
    "wagner",
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

ENTITY_CORROBORATION_WINDOW_MS: Final = 24 * 60 * 60 * 1_000


def _normalized(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", value.lower()).split())


def _finite_number(value: object, fallback: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def _count_matches(text: str, keywords: Sequence[str]) -> int:
    return sum(keyword in text for keyword in keywords)


def _normalized_threat_level(value: object) -> str:
    level = str(value or "")
    upper = level.upper()
    if upper.startswith("THREAT_LEVEL_"):
        suffix = upper.removeprefix("THREAT_LEVEL_").lower()
        return "info" if suffix == "unspecified" else suffix
    return level.lower()


def _publisher_count(cluster: Mapping[str, Any]) -> int:
    sources = cluster.get("sources")
    unique_sources = len(sources) if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)) else 0
    return max(
        unique_sources,
        int(_finite_number(cluster.get("corroboration_source_count"), 0)),
        int(_finite_number(cluster.get("corroboration_count"), 0)),
        1,
    )


def _cluster_updated_ms(cluster: Mapping[str, Any]) -> float:
    value = cluster.get("last_updated_ms")
    if value is None:
        value = cluster.get("primary_published_at_ms")
    return _finite_number(value, 0)


def score_public_cluster(cluster: Mapping[str, Any]) -> float:
    """Port of WorldMonitor's public ``scoreImportance`` at commit 0e8785c4."""

    score = 0.0
    title = _normalized(str(cluster.get("primary_title") or ""))
    upstream = _finite_number(cluster.get("upstream_importance_score"), 0)
    if upstream > 0:
        score += upstream * 2.2

    threat = cluster.get("threat")
    threat_map = threat if isinstance(threat, Mapping) else {}
    level = _normalized_threat_level(threat_map.get("level"))
    threat_source = str(threat_map.get("source") or "")
    threat_scores = {"critical": 220, "high": 150, "medium": 80, "low": 20, "info": 0}
    if level and threat_source == "llm":
        score += threat_scores.get(level, 0)
    elif level and upstream > 0 and threat_source != "keyword-historical-downgrade":
        score += threat_scores.get(level, 0) * 0.35

    source_tier = int(
        _finite_number(
            cluster.get("source_tier"),
            reporting_origin_tier(str(cluster.get("primary_source") or ""), fallback_tier=4),
        )
    )
    score += 35 if source_tier == 1 else 20 if source_tier == 2 else 8 if source_tier == 3 else 0
    score += min(_publisher_count(cluster), 6) * 12
    if cluster.get("entity_corroboration") is True:
        score += 45

    violence_count = _count_matches(title, VIOLENCE_KEYWORDS)
    if violence_count > 0:
        score += 50 + violence_count * 12
    military_count = _count_matches(title, MILITARY_KEYWORDS)
    if military_count > 0:
        score += 40 + military_count * 10
    unrest_count = _count_matches(title, UNREST_KEYWORDS)
    if unrest_count > 0:
        score += 35 + unrest_count * 9
    flashpoint_count = _count_matches(title, FLASHPOINT_KEYWORDS)
    if flashpoint_count > 0:
        score += 30 + flashpoint_count * 8
    diplomacy_count = _count_matches(title, DIPLOMACY_KEYWORDS)
    if diplomacy_count > 0:
        score += 35 + diplomacy_count * 9
    if (violence_count > 0 or unrest_count > 0 or diplomacy_count > 0) and flashpoint_count > 0:
        score *= 1.25
    crisis_count = _count_matches(title, CRISIS_KEYWORDS)
    if crisis_count > 0:
        score += 15 + crisis_count * 5

    finance_count = _count_matches(title, FINANCE_DEMOTE_KEYWORDS)
    strong_non_keyword = threat_source == "llm" and level in {"high", "critical"}
    if finance_count > 0 and cluster.get("entity_corroboration") is not True and not strong_non_keyword:
        score *= 0.35
    return score


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
    age_ms = now_ms - published_at_ms
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
        "reporting_origin_count": corroboration_count,
        "scoring_corroboration_count": scoring_corroboration_count,
        "corroboration_points": round(corroboration * 0.15, 2),
        "recency_points": round(recency * 0.10, 2),
        "diplomacy_flashpoint_boost": boost,
        "entity_corroboration_boost": entity_boost,
        "total": _js_round(base + boost + entity_boost),
    }


def _entity_keys_for_cluster(cluster: Mapping[str, Any]) -> set[str]:
    member_titles = cluster.get("member_titles")
    titles = (
        member_titles
        if isinstance(member_titles, Sequence) and not isinstance(member_titles, (str, bytes)) and member_titles
        else (cluster.get("primary_title"),)
    )
    keys: set[str] = set()
    for title in titles:
        text = _normalized(str(title or ""))
        for entity, action in DIPLOMACY_FLASHPOINT_PAIRS:
            if _contains_token(text, entity) and _contains_token(text, action):
                keys.add(f"{entity}:{action}")
    return keys


def compute_entity_corroboration(clusters: Sequence[dict[str, Any]], *, now_ms: int) -> None:
    """Port WorldMonitor's 24-hour cross-publisher entity corroboration pass."""

    buckets: dict[str, tuple[list[dict[str, Any]], set[str]]] = {}
    for cluster in clusters:
        cluster["entity_corroboration"] = False
        cluster["corroboration_source_count"] = 0
        updated_ms = _cluster_updated_ms(cluster)
        if updated_ms <= 0 or now_ms - updated_ms > ENTITY_CORROBORATION_WINDOW_MS:
            continue
        for key in _entity_keys_for_cluster(cluster):
            grouped_clusters, sources = buckets.setdefault(key, ([], set()))
            grouped_clusters.append(cluster)
            raw_sources = cluster.get("sources")
            if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes)):
                continue
            sources.update(source.strip() for source in raw_sources if isinstance(source, str) and source.strip())

    for grouped_clusters, sources in buckets.values():
        if len(sources) < 2:
            continue
        for cluster in grouped_clusters:
            cluster["entity_corroboration"] = True
            cluster["corroboration_source_count"] = max(
                int(_finite_number(cluster.get("corroboration_source_count"), 0)),
                len(sources),
            )


def public_recency_weight(cluster: Mapping[str, Any], *, now_ms: int) -> float:
    updated_ms = _cluster_updated_ms(cluster)
    if updated_ms <= 0:
        return 1.0
    age_hours = max(0.0, (now_ms - updated_ms) / 3_600_000)
    return max(0.5, 1 - age_hours / 16)


def is_brief_lead_eligible(cluster: Mapping[str, Any]) -> bool:
    sources = cluster.get("sources")
    source_count = (
        sum(isinstance(source, str) and bool(source.strip()) for source in sources)
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes))
        else 0
    )
    return source_count >= 2 or cluster.get("entity_corroboration") is True


def is_top_stories_admissible(cluster: Mapping[str, Any], score: float) -> bool:
    return is_brief_lead_eligible(cluster) or cluster.get("is_alert") is True or score > 100


def select_top_stories(
    stories: Sequence[dict[str, Any]],
    *,
    now_ms: int,
    limit: int = 8,
    stats: dict[str, int | bool] | None = None,
) -> list[dict[str, Any]]:
    """Select WorldMonitor's public top-eight corpus with no personalization."""

    clusters = list(stories)
    compute_entity_corroboration(clusters, now_ms=now_ms)

    admissible: list[tuple[dict[str, Any], float, float]] = []
    admissibility_dropped = 0
    for cluster in clusters:
        score = score_public_cluster(cluster)
        if is_top_stories_admissible(cluster, score):
            admissible.append((cluster, score, score * public_recency_weight(cluster, now_ms=now_ms)))
        else:
            admissibility_dropped += 1
    admissible.sort(key=lambda entry: (-entry[2], -entry[1]))

    def fill(
        seed: tuple[dict[str, Any], float, float] | None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        selected: list[dict[str, Any]] = []
        source_counts: dict[str, int] = {}
        source_cap_dropped = 0
        overflow_dropped = 0

        def take(entry: tuple[dict[str, Any], float, float]) -> None:
            cluster, score, effective_score = entry
            selected.append(
                {
                    **cluster,
                    "importance_score": score,
                    "effective_importance_score": effective_score,
                }
            )
            source = str(cluster.get("primary_source") or "")
            source_counts[source] = source_counts.get(source, 0) + 1

        if seed is not None and limit > 0:
            take(seed)
        for entry in admissible:
            if entry is seed:
                continue
            source = str(entry[0].get("primary_source") or "")
            if source_counts.get(source, 0) >= 3:
                source_cap_dropped += 1
                continue
            if len(selected) >= limit:
                overflow_dropped += 1
                continue
            take(entry)
        selected.sort(
            key=lambda cluster: (
                -float(cluster["effective_importance_score"]),
                -float(cluster["importance_score"]),
            )
        )
        return selected, source_cap_dropped, overflow_dropped

    selected, source_cap_dropped, overflow_dropped = fill(None)
    brief_eligible = [entry for entry in admissible if is_brief_lead_eligible(entry[0])]
    promoted = (
        brief_eligible[0]
        if limit > 0 and not any(is_brief_lead_eligible(cluster) for cluster in selected) and brief_eligible
        else None
    )
    if promoted is not None:
        selected, source_cap_dropped, overflow_dropped = fill(promoted)

    if stats is not None:
        stats.update(
            {
                "considered": len(clusters),
                "admissibility_dropped": admissibility_dropped,
                "source_cap_dropped": source_cap_dropped,
                "overflow_dropped": overflow_dropped,
                "brief_eligible_considered": len(brief_eligible),
                "brief_eligible_promoted": promoted is not None,
            }
        )
    return selected


__all__ = [
    "PUBLIC_SELECTOR_VERSION",
    "compute_entity_corroboration",
    "diplomacy_entity_keys",
    "importance_factors",
    "importance_score",
    "is_brief_lead_eligible",
    "is_top_stories_admissible",
    "promote_diplomacy_severity",
    "public_recency_weight",
    "score_public_cluster",
    "select_top_stories",
]
