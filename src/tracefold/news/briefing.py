from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from tracefold.news.identity import deterministic_id, sha256_json
from tracefold.news.models import (
    BRIEF_GROUPING_VERSION,
    BRIEF_SELECTION_VERSION,
    NEWS_LOCALE,
    BriefEvidenceBundle,
)

MAX_BRIEF_STORIES = 12
MAX_STORIES_PER_NARRATIVE = 2
MAX_STORIES_PER_ORIGIN = 3
MAX_STORIES_PER_REGION = 4
MAX_STORIES_PER_EVENT_TYPE = 4
MAX_STORIES_PER_MARKET_PATH = 5
MAX_EVIDENCE_ARTICLES_PER_STORY = 5
MIN_SELECTION_UTILITY = 54.0
CRITICAL_IMPACT = 90


def plan_brief_selection(
    stories: Sequence[Mapping[str, Any]],
    *,
    cutoff_at_ms: int,
) -> tuple[dict[str, Any], dict[str, Any], BriefEvidenceBundle]:
    """Build one deterministic, frozen editorial portfolio.

    Narrative grouping is deliberately selection-scoped. It may reduce
    redundant marginal value, but it never writes Story identity.
    """

    ordered_input = sorted(stories, key=lambda row: str(row["story_id"]))
    input_hash = sha256_json(
        [
            {
                "story_id": row["story_id"],
                "material_evidence_hash": row["material_evidence_hash"],
                "event_core": row["event_core"],
            }
            for row in ordered_input
        ]
    )
    groups = _narrative_groups(ordered_input)
    grouping_snapshot_id = deterministic_id(
        "news-narrative-grouping",
        BRIEF_GROUPING_VERSION,
        input_hash,
    )
    grouping = {
        "grouping_snapshot_id": grouping_snapshot_id,
        "input_hash": input_hash,
        "policy_version": BRIEF_GROUPING_VERSION,
        "embedding_model": "deterministic-fallback-v1",
        "fallback_used": True,
        "groups": groups,
        "receipt": {
            "method": "deterministic_event_entity_fallback",
            "story_count": len(ordered_input),
        },
        "cutoff_at_ms": cutoff_at_ms,
    }

    group_by_story = {story_id: str(group["narrative_id"]) for group in groups for story_id in group["story_ids"]}
    selected: list[Mapping[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    narrative_counts: Counter[str] = Counter()
    origin_counts: Counter[str] = Counter()
    region_counts: Counter[str] = Counter()
    event_type_counts: Counter[str] = Counter()
    market_path_counts: Counter[str] = Counter()
    ranked = sorted(
        ordered_input,
        key=lambda row: (
            -_base_utility(row),
            -int(row["impact_score"]),
            -int(row["last_material_evidence_at_ms"]),
            str(row["story_id"]),
        ),
    )
    for story in ranked:
        story_id = str(story["story_id"])
        narrative_id = group_by_story[story_id]
        origin_id = _dominant_origin(story)
        region = _portfolio_region(story)
        event_type = _portfolio_event_type(story)
        market_path = _portfolio_market_path(story)
        base = _base_utility(story)
        adjustments: list[dict[str, Any]] = []
        penalty = 0.0
        if narrative_counts[narrative_id]:
            value = 14.0 * narrative_counts[narrative_id]
            penalty += value
            adjustments.append({"kind": "narrative_redundancy", "value": -value})
        if origin_id and origin_counts[origin_id]:
            value = 8.0 * origin_counts[origin_id]
            penalty += value
            adjustments.append({"kind": "shared_origin_concentration", "value": -value})
        if region and region_counts[region]:
            value = 3.0 * region_counts[region]
            penalty += value
            adjustments.append({"kind": "region_concentration", "value": -value})
        if event_type and event_type_counts[event_type]:
            value = 4.0 * event_type_counts[event_type]
            penalty += value
            adjustments.append({"kind": "event_type_concentration", "value": -value})
        if market_path and market_path_counts[market_path]:
            value = 2.0 * market_path_counts[market_path]
            penalty += value
            adjustments.append({"kind": "market_path_concentration", "value": -value})
        marginal = round(base - penalty, 3)
        critical = int(story["impact_score"]) >= CRITICAL_IMPACT
        constraints: list[str] = []
        if narrative_counts[narrative_id] >= MAX_STORIES_PER_NARRATIVE:
            constraints.append("narrative_cap")
        if origin_id and origin_counts[origin_id] >= MAX_STORIES_PER_ORIGIN:
            constraints.append("origin_cap")
        if region and region_counts[region] >= MAX_STORIES_PER_REGION:
            constraints.append("region_cap")
        if event_type and event_type_counts[event_type] >= MAX_STORIES_PER_EVENT_TYPE:
            constraints.append("event_type_cap")
        if market_path and market_path_counts[market_path] >= MAX_STORIES_PER_MARKET_PATH:
            constraints.append("market_path_cap")
        if len(selected) >= MAX_BRIEF_STORIES:
            constraints.append("portfolio_cap")
        within_portfolio = len(selected) < MAX_BRIEF_STORIES
        override = critical and within_portfolio and (bool(constraints) or marginal < MIN_SELECTION_UTILITY)
        select = within_portfolio and (critical or (marginal >= MIN_SELECTION_UTILITY and not constraints))
        if select:
            selected.append(story)
            narrative_counts[narrative_id] += 1
            if origin_id:
                origin_counts[origin_id] += 1
            if region:
                region_counts[region] += 1
            if event_type:
                event_type_counts[event_type] += 1
            if market_path:
                market_path_counts[market_path] += 1
        decisions.append(
            {
                "story_id": story_id,
                "selected": select,
                "base_utility": base,
                "marginal_utility": marginal,
                "adjustments": adjustments,
                "constraints": constraints,
                "critical_override": override,
                "reason": (
                    "critical_override"
                    if override
                    else "selected"
                    if select
                    else "below_utility_floor"
                    if marginal < MIN_SELECTION_UTILITY
                    else constraints[0]
                    if constraints
                    else "portfolio_complete"
                ),
                "narrative_id": narrative_id,
                "portfolio_dimensions": {
                    "region": region,
                    "event_type": event_type,
                    "market_path": market_path,
                    "dominant_origin": origin_id,
                },
            }
        )

    selected_rows = tuple(_brief_story(row, group_by_story[str(row["story_id"])]) for row in selected)
    material_groups = tuple(
        {
            "narrative_id": group["narrative_id"],
            "story_ids": [
                story_id for story_id in group["story_ids"] if story_id in {str(row["story_id"]) for row in selected}
            ],
            "anchors": group["anchors"],
        }
        for group in groups
        if any(str(row["story_id"]) in group["story_ids"] for row in selected)
    )
    fingerprint_payload = {
        "policy_version": BRIEF_SELECTION_VERSION,
        "stories": [
            {
                "story_id": row["story_id"],
                "material_evidence_hash": row["material_evidence_hash"],
                "narrative_id": row["narrative_id"],
            }
            for row in selected_rows
        ],
    }
    selection_fingerprint = sha256_json(fingerprint_payload)
    evidence_cutoff_at_ms = max(
        (int(row["last_material_evidence_at_ms"]) for row in selected),
        default=cutoff_at_ms,
    )
    synthesis_input = {
        "evidence_cutoff_at_ms": evidence_cutoff_at_ms,
        "locale": NEWS_LOCALE,
        "stories": selected_rows,
        "narrative_groups": material_groups,
        "selection_policy_version": BRIEF_SELECTION_VERSION,
    }
    synthesis_input_hash = sha256_json(synthesis_input)
    selection_id = deterministic_id(
        "news-brief-selection",
        BRIEF_SELECTION_VERSION,
        selection_fingerprint,
    )
    bundle = BriefEvidenceBundle(
        selection_id=selection_id,
        selection_fingerprint=selection_fingerprint,
        synthesis_input_hash=synthesis_input_hash,
        evidence_cutoff_at_ms=evidence_cutoff_at_ms,
        locale=NEWS_LOCALE,
        stories=selected_rows,
        narrative_groups=material_groups,
        selection_policy_version=BRIEF_SELECTION_VERSION,
    )
    selection = {
        "selection_id": selection_id,
        "selection_fingerprint": selection_fingerprint,
        "grouping_snapshot_id": grouping_snapshot_id,
        "policy_version": BRIEF_SELECTION_VERSION,
        "evidence_cutoff_at_ms": evidence_cutoff_at_ms,
        "selected_story_ids": [row["story_id"] for row in selected_rows],
        "decisions": decisions,
        "critical": any(int(row["impact_score"]) >= CRITICAL_IMPACT for row in selected),
        "verified_critical": any(
            int(row["impact_score"]) >= CRITICAL_IMPACT
            and str(row["evidence_posture"]) in {"primary_source_confirmed", "independently_corroborated"}
            for row in selected
        ),
        "synthesis_input_hash": synthesis_input_hash,
        "evidence_bundle": bundle.model_dump(mode="json"),
    }
    return grouping, selection, bundle


def _narrative_groups(stories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    buckets: defaultdict[str, list[str]] = defaultdict(list)
    anchors_by_bucket: dict[str, list[str]] = {}
    for story in stories:
        event_core = _mapping(story.get("event_core"))
        entities = sorted(str(value) for value in _sequence(event_core.get("entities")) if value)
        actions = sorted(str(value) for value in _sequence(event_core.get("actions")) if value)
        locations = sorted(str(value) for value in _sequence(event_core.get("locations")) if value)
        anchors = (entities[:2] + locations[:1] + actions[:1]) or ["uncategorized"]
        bucket = sha256_json(anchors)[:16]
        buckets[bucket].append(str(story["story_id"]))
        anchors_by_bucket[bucket] = anchors
    return [
        {
            "narrative_id": deterministic_id("news-narrative", BRIEF_GROUPING_VERSION, bucket),
            "story_ids": sorted(story_ids),
            "anchors": anchors_by_bucket[bucket],
        }
        for bucket, story_ids in sorted(buckets.items())
    ]


def _base_utility(story: Mapping[str, Any]) -> float:
    posture = str(story["evidence_posture"])
    evidence_bonus = {
        "primary_source_confirmed": 8,
        "independently_corroborated": 7,
        "contested": 4,
        "corrected": 3,
        "single_origin_reported": 1,
        "withdrawn": -100,
    }.get(posture, 0)
    material_bonus = 8 if str(story["material_evolution_state"]) != "first_report" else 2
    recent_coverage_penalty = float(story.get("recent_brief_coverage_penalty") or 0)
    return round(
        int(story["impact_score"]) * 0.58
        + int(story["priority_score"]) * 0.32
        + evidence_bonus
        + material_bonus
        - recent_coverage_penalty,
        3,
    )


def _brief_story(story: Mapping[str, Any], narrative_id: str) -> dict[str, Any]:
    evidence = _bounded_evidence(story)
    evidence_factors = _bounded_evidence_factors(
        _mapping(story["evidence_factors"]),
        evidence,
    )
    return {
        "story_id": str(story["story_id"]),
        "material_evidence_hash": str(story["material_evidence_hash"]),
        "title": str(story["title"]),
        "snippet": str(story.get("snippet") or ""),
        "event_core": dict(_mapping(story["event_core"])),
        "evidence_posture": str(story["evidence_posture"]),
        "evidence_factors": evidence_factors,
        "impact_score": int(story["impact_score"]),
        "impact_profile": dict(_mapping(story["impact_profile"])),
        "priority_score": int(story["priority_score"]),
        "priority_profile": dict(_mapping(story["priority_profile"])),
        "material_evolution_state": str(story["material_evolution_state"]),
        "narrative_id": narrative_id,
        "evidence_articles": evidence,
    }


def _bounded_evidence(story: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence = [dict(article) for article in _sequence(story.get("evidence_articles")) if isinstance(article, Mapping)]
    conflicts = _sequence(_mapping(story.get("evidence_factors")).get("material_conflicts"))
    required_refs = {
        str(conflict.get(field) or "")
        for conflict in conflicts
        if isinstance(conflict, Mapping)
        for field in ("left_revision_id", "right_revision_id")
        if str(conflict.get(field) or "")
    }
    evidence.sort(
        key=lambda article: (
            0
            if str(article.get("evidence_ref")) in required_refs
            or str(article.get("development_relation")) == "correction"
            else 1,
            {
                "originating": 0,
                "independent": 1,
                "unresolved": 2,
                "derived": 3,
                "syndicated": 4,
            }.get(str(article.get("origin_relation")), 5),
            -int(article.get("observed_at_ms") or 0),
            str(article.get("evidence_ref") or ""),
        )
    )
    selected: list[dict[str, Any]] = []
    seen_content: set[str] = set()
    seen_origins: set[str] = set()
    for article in evidence:
        snapshot = _mapping(article.get("content_snapshot"))
        content_key = str(snapshot.get("content_hash") or "") or sha256_json(
            [article.get("title"), article.get("snippet")]
        )
        origin = str(article.get("reporting_origin_id") or article.get("publisher_organization_id") or "")
        required = (
            str(article.get("evidence_ref")) in required_refs
            or str(article.get("development_relation")) == "correction"
        )
        if not required and (content_key in seen_content or (origin and origin in seen_origins and len(selected) >= 2)):
            continue
        selected.append(article)
        seen_content.add(content_key)
        if origin:
            seen_origins.add(origin)
        if len(selected) >= MAX_EVIDENCE_ARTICLES_PER_STORY:
            break
    return selected


def _bounded_evidence_factors(
    factors: Mapping[str, object],
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    bounded = dict(factors)
    selected_refs = {
        str(article.get("evidence_ref") or "") for article in evidence if str(article.get("evidence_ref") or "")
    }
    conflicts = [
        dict(conflict)
        for conflict in _sequence(factors.get("material_conflicts"))
        if isinstance(conflict, Mapping)
        and {
            str(conflict.get("left_revision_id") or ""),
            str(conflict.get("right_revision_id") or ""),
        }
        <= selected_refs
    ]
    bounded["material_conflicts"] = conflicts
    bounded["has_material_conflict"] = bool(conflicts)
    bounded["evidence_bounding"] = {
        "maximum_articles": MAX_EVIDENCE_ARTICLES_PER_STORY,
        "selected_article_count": len(evidence),
        "conflicts_retained": len(conflicts),
    }
    return bounded


def _dominant_origin(story: Mapping[str, Any]) -> str | None:
    evidence = _sequence(story.get("evidence_articles"))
    for article in evidence:
        if not isinstance(article, Mapping):
            continue
        origin = str(article.get("reporting_origin_id") or article.get("publisher_organization_id") or "")
        if origin:
            return origin
    return None


def _portfolio_region(story: Mapping[str, Any]) -> str | None:
    core = _mapping(story.get("event_core"))
    values = sorted(str(value) for value in _sequence(core.get("locations") or core.get("entities")) if str(value))
    return values[0] if values else None


def _portfolio_event_type(story: Mapping[str, Any]) -> str | None:
    core = _mapping(story.get("event_core"))
    values = sorted(str(value) for value in _sequence(core.get("actions")) if str(value))
    return values[0] if values else None


def _portfolio_market_path(story: Mapping[str, Any]) -> str | None:
    dimensions = _mapping(_mapping(story.get("impact_profile")).get("dimensions"))
    candidates = [
        (_int(dimensions.get(name)), name) for name in ("market", "macro", "policy", "systemic", "geopolitical")
    ]
    score, name = max(candidates, default=(0, ""), key=lambda item: (item[0], item[1]))
    return name if score > 0 else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


__all__ = ["plan_brief_selection"]
