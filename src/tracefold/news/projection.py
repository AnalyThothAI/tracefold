from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pyuca import Collator  # type: ignore[import-untyped]

from tracefold.news.classification import SEVERITY_VALUES, classify_by_keyword
from tracefold.news.identity import (
    cluster_texts,
    normalize_story_canonical_title,
    normalize_story_text,
    public_story_title_hash,
    utf16_length,
    utf16_sort_key,
)
from tracefold.news.models import STORY_IDENTITY_VERSION, EventCategory, ThreatLevel
from tracefold.news.ranking import (
    PUBLIC_SELECTOR_VERSION,
    diplomacy_entity_keys,
    importance_factors,
    promote_diplomacy_severity,
    select_top_stories,
)
from tracefold.news.sources import reporting_origin_tier
from tracefold.news.story_store import (
    NewsProjectionInputExceeded,
    _require_bounded_story_rows,
)

NEWS_STORY_LOAD_TIMEOUT_SECONDS = 3.0
NEWS_STORY_COMPUTE_TIMEOUT_SECONDS = 25.0
NEWS_STORY_PUBLISH_TIMEOUT_SECONDS = 8.0
NEWS_STORY_FAILURE_TIMEOUT_SECONDS = 3.0
_CATEGORY_ORDER: tuple[EventCategory, ...] = (
    "conflict",
    "protest",
    "disaster",
    "diplomatic",
    "economic",
    "terrorism",
    "cyber",
    "health",
    "environmental",
    "military",
    "crime",
    "infrastructure",
    "tech",
    "general",
)
_PUBLIC_TOP_STORY_FIELDS: tuple[str, ...] = (
    "story_id",
    "primary_title",
    "primary_source",
    "primary_link",
    "primary_published_at_ms",
    "source_count",
    "unique_source_count",
    "sources",
    "last_updated_ms",
    "member_titles",
    "source_tier",
    "upstream_importance_score",
    "entity_corroboration",
    "corroboration_source_count",
    "importance_score",
    "effective_importance_score",
    "is_alert",
    "threat_level",
    "category",
)
_PUBLIC_SOURCE_COLLATOR = Collator()
_PUBLIC_STORY_CATEGORIES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("war", "attack", "missile", "troops", "airstrike", "combat", "military"), "conflict", "critical"),
    (("killed", "dead", "casualties", "massacre", "shooting"), "violence", "high"),
    (("protest", "uprising", "riot", "unrest", "coup"), "unrest", "high"),
    (("sanctions", "tensions", "escalation", "threat"), "geopolitical", "elevated"),
    (("crisis", "emergency", "disaster", "collapse"), "crisis", "high"),
    (("earthquake", "flood", "hurricane", "wildfire", "tsunami"), "natural_disaster", "elevated"),
    (("election", "vote", "parliament", "legislation"), "political", "moderate"),
    (("market", "economy", "trade", "tariff", "inflation"), "economic", "moderate"),
)


@dataclass(frozen=True, slots=True)
class NewsProjectionSnapshot:
    input_fingerprint: str
    cutoff_ms: int
    scoring_epoch_ms: int
    current_input_fingerprint: str | None
    rows: tuple[dict[str, Any], ...]

    @property
    def unchanged(self) -> bool:
        return self.current_input_fingerprint == self.input_fingerprint


class NewsProjectionService:
    """Short load/publish sessions around one complete deterministic calculation."""

    def __init__(self, *, db: Any, worker_name: str = "news_story_projection") -> None:
        self.db = db
        self.worker_name = worker_name

    def load(self, *, now_ms: int) -> NewsProjectionSnapshot:
        with self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=NEWS_STORY_LOAD_TIMEOUT_SECONDS,
        ) as repos:
            payload = repos.news.load_story_projection(now_ms=now_ms)
        snapshot = NewsProjectionSnapshot(
            input_fingerprint=str(payload["input_fingerprint"]),
            cutoff_ms=int(payload["cutoff_ms"]),
            scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
            current_input_fingerprint=(
                str(payload["current_input_fingerprint"]) if payload.get("current_input_fingerprint") else None
            ),
            rows=tuple(dict(row) for row in payload["rows"]),
        )
        _require_bounded_snapshot(snapshot)
        return snapshot

    def publish(
        self,
        snapshot: NewsProjectionSnapshot,
        projection: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        with (
            self.db.worker_session(
                self.worker_name,
                statement_timeout_seconds=5.0,
                transaction_timeout_seconds=NEWS_STORY_PUBLISH_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            return cast(
                dict[str, Any],
                repos.news.publish_story_projection(
                    snapshot=snapshot,
                    projection=projection,
                    now_ms=now_ms,
                ),
            )

    def mark_failed(self, *, now_ms: int, error_code: str) -> None:
        with (
            self.db.worker_session(
                self.worker_name,
                statement_timeout_seconds=NEWS_STORY_FAILURE_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            repos.news.record_story_projection_failure(
                now_ms=now_ms,
                error_code=error_code,
            )


def compute_news_story_projection(snapshot: NewsProjectionSnapshot) -> dict[str, Any]:
    _require_bounded_snapshot(snapshot)
    rows = [dict(row) for row in snapshot.rows]
    for row in rows:
        row["normalized_title"] = normalize_story_text(str(row["title"]))
    clusters = cluster_texts([str(row["title"]) for row in rows])

    scoring_groups: list[tuple[list[dict[str, Any]], int, str]] = []
    story_members: dict[str, list[dict[str, Any]]] = {}
    story_anchors: dict[str, dict[str, Any]] = {}
    story_canonical_keys: dict[str, str] = {}
    for indices in clusters:
        members = [rows[index] for index in indices]
        canonical_members = [(normalize_story_canonical_title(str(member["title"])), member) for member in members]
        trackable_members = [(title, member) for title, member in canonical_members if title]
        if trackable_members:
            canonical_title, anchor = min(
                trackable_members,
                key=lambda pair: (
                    int(pair[1]["published_at_ms"]),
                    utf16_sort_key(pair[0]),
                    str(pair[1]["item_id"]),
                ),
            )
            story_identity = str(anchor["normalized_title"])
            canonical_key = public_story_title_hash(canonical_title)
        else:
            anchor = min(
                members,
                key=lambda member: (
                    int(member["published_at_ms"]),
                    str(member["reporting_origin"]),
                    str(member["title"]),
                    str(member["item_id"]),
                ),
            )
            story_identity = f"untrackable:{anchor['reporting_origin']}:{anchor['title']}:{anchor['item_id']}"
            canonical_key = public_story_title_hash(f"untrackable:{anchor['reporting_origin']}:{anchor['title']}")
        story_id = public_story_title_hash(story_identity)
        if story_id in story_members:
            raise RuntimeError("news_story_component_identity_collision")
        scoring_groups.append(
            (
                members,
                len({str(member["reporting_origin"]) for member in members}),
                canonical_key,
            )
        )
        story_members[story_id] = members
        story_anchors[story_id] = anchor
        story_canonical_keys[story_id] = canonical_key
        for member in members:
            member["story_id"] = story_id
            member["canonical_key"] = canonical_key

    entity_buckets: dict[str, dict[str, set[str] | set[int]]] = {}
    for row in rows:
        if snapshot.scoring_epoch_ms - int(row["published_at_ms"]) > 86_400_000:
            continue
        origin = str(row["reporting_origin"])
        tier = reporting_origin_tier(origin, fallback_tier=int(row["tier"]))
        for entity_key in diplomacy_entity_keys(str(row["title"])):
            bucket = entity_buckets.setdefault(
                entity_key,
                {"canonical_keys": set(), "origins": set(), "tier12_origins": set()},
            )
            cast(set[str], bucket["canonical_keys"]).add(str(row["canonical_key"]))
            cast(set[str], bucket["origins"]).add(origin)
            if tier <= 2:
                cast(set[str], bucket["tier12_origins"]).add(origin)
    entity_signal_by_canonical_key: dict[str, tuple[int, int]] = {}
    for bucket in entity_buckets.values():
        origins = cast(set[str], bucket["origins"])
        if len(origins) < 2:
            continue
        signal = (len(origins), len(cast(set[str], bucket["tier12_origins"])))
        for canonical_key in cast(set[str], bucket["canonical_keys"]):
            previous = entity_signal_by_canonical_key.get(canonical_key, (0, 0))
            entity_signal_by_canonical_key[canonical_key] = (
                max(previous[0], signal[0]),
                max(previous[1], signal[1]),
            )

    item_updates: list[dict[str, Any]] = []
    for members, source_count, canonical_key in scoring_groups:
        entity_count, tier12_entity_count = entity_signal_by_canonical_key.get(canonical_key, (0, 0))
        for member in members:
            classification = classify_by_keyword(
                str(member["title"]),
                now_ms=snapshot.scoring_epoch_ms,
            )
            tier = reporting_origin_tier(
                str(member["reporting_origin"]),
                fallback_tier=int(member["tier"]),
            )
            level = promote_diplomacy_severity(
                classification.level,
                title=str(member["title"]),
                tier12_origin_count=tier12_entity_count,
            )
            factors = importance_factors(
                level=level,
                tier=tier,
                corroboration_count=source_count,
                published_at_ms=int(member["published_at_ms"]),
                now_ms=snapshot.scoring_epoch_ms,
                title=str(member["title"]),
                entity_corroboration_count=entity_count,
            )
            member.update(
                {
                    "effective_tier": tier,
                    "level": level,
                    "category": classification.category,
                    "classification_source": classification.source,
                    "classification_confidence": classification.confidence,
                    "importance_score": int(factors["total"]),
                    "importance_factors": factors,
                }
            )
            item_updates.append(
                {
                    key: member[key]
                    for key in (
                        "item_id",
                        "level",
                        "category",
                        "classification_source",
                        "classification_confidence",
                        "importance_score",
                        "importance_factors",
                    )
                }
            )

    stories: list[dict[str, Any]] = []
    memberships: list[dict[str, str]] = []
    for story_id, members in story_members.items():
        earliest = story_anchors[story_id]
        source_count = len({str(member["reporting_origin"]) for member in members})
        representative = min(
            members,
            key=lambda member: (
                int(member["effective_tier"]),
                -int(member["published_at_ms"]),
                str(member["normalized_title"]),
                str(member["item_id"]),
            ),
        )
        scoring = min(
            members,
            key=lambda member: (
                -int(member["importance_score"]),
                int(member["effective_tier"]),
                -int(member["published_at_ms"]),
                str(member["reporting_origin"]),
                str(member["item_id"]),
            ),
        )
        level = cast(
            ThreatLevel,
            max(
                (str(member["level"]) for member in members),
                key=lambda value: (SEVERITY_VALUES[cast(ThreatLevel, value)], value),
            ),
        )
        category = cast(
            EventCategory,
            _mode([str(member["category"]) for member in members], _CATEGORY_ORDER),
        )
        first_published_at_ms = min(int(member["published_at_ms"]) for member in members)
        last_published_at_ms = max(int(member["published_at_ms"]) for member in members)
        story = {
            "story_id": story_id,
            "canonical_key": story_canonical_keys[story_id],
            "canonical_title": str(earliest["title"]),
            "representative_item_id": str(representative["item_id"]),
            "representative_source_id": str(representative["source_id"]),
            "representative_title": str(representative["title"]),
            "representative_url": representative.get("canonical_url"),
            "representative_description": str(representative["description"]),
            "scoring_item_id": str(scoring["item_id"]),
            "level": level,
            "category": category,
            "importance_score": int(scoring["importance_score"]),
            "importance_factors": dict(scoring["importance_factors"]),
            "item_count": len(members),
            "source_count": source_count,
            "first_published_at_ms": first_published_at_ms,
            "last_published_at_ms": last_published_at_ms,
        }
        story["state_fingerprint"] = _stable_hash(story)
        stories.append(story)
        memberships.extend({"story_id": story_id, "item_id": str(member["item_id"])} for member in members)

    # Keep complete materialized Story ownership above. WorldMonitor's public
    # seed independently drops short titles before running the same clustering
    # kernel, so the public evidence components must be derived in that order.
    public_rows = [row for row in rows if utf16_length(str(row["title"])) > 10]
    public_component_indices = cluster_texts([str(row["title"]) for row in public_rows])
    public_clusters: list[dict[str, Any]] = []
    for indices in public_component_indices:
        public_members = [public_rows[index] for index in indices]
        parent_story_ids = {str(member["story_id"]) for member in public_members}
        if len(parent_story_ids) != 1:
            raise RuntimeError("news_public_component_crossed_story_boundary")
        story_id = parent_story_ids.pop()
        public_representative = min(
            public_members,
            key=lambda member: (
                int(member["effective_tier"]),
                -int(member["published_at_ms"]),
                str(member["item_id"]),
            ),
        )
        tier_by_origin: dict[str, int] = {}
        for member in public_members:
            origin = str(member["reporting_origin"]).strip()
            if not origin:
                continue
            tier_by_origin[origin] = min(
                tier_by_origin.get(origin, int(member["effective_tier"])),
                int(member["effective_tier"]),
            )
        ordered_origins = sorted(
            tier_by_origin,
            key=lambda origin: (tier_by_origin[origin], _PUBLIC_SOURCE_COLLATOR.sort_key(origin)),
        )
        public_category, public_threat_level = _categorize_public_story(str(public_representative["title"]))
        public_clusters.append(
            {
                "story_id": story_id,
                "primary_title": str(public_representative["title"]),
                "primary_source": str(public_representative["reporting_origin"]).strip(),
                "primary_link": public_representative.get("canonical_url"),
                "primary_published_at_ms": int(public_representative["published_at_ms"]),
                "source_count": len(public_members),
                "unique_source_count": len(ordered_origins),
                "sources": ordered_origins,
                "last_updated_ms": max(int(member["published_at_ms"]) for member in public_members),
                "member_titles": [str(member["title"]) for member in public_members if str(member["title"])],
                "source_tier": min(tier_by_origin.values(), default=4),
                "upstream_importance_score": max(int(member["importance_score"]) for member in public_members),
                "entity_corroboration": False,
                "corroboration_source_count": 0,
                "is_alert": any(str(member["level"]) in {"critical", "high"} for member in public_members),
                "threat_level": public_threat_level,
                "category": public_category,
                "threat": {
                    "level": str(public_representative["level"]),
                    "category": str(public_representative["category"]),
                    "source": str(public_representative["classification_source"]),
                },
            }
        )

    selection_stats: dict[str, int | bool] = {}
    selected = select_top_stories(public_clusters, now_ms=snapshot.scoring_epoch_ms, stats=selection_stats)
    selection_payload = {
        "projection_revision": snapshot.input_fingerprint,
        "selector_evaluated_at_ms": snapshot.scoring_epoch_ms,
        "top_stories": [{field: cluster[field] for field in _PUBLIC_TOP_STORY_FIELDS} for cluster in selected],
        "selection_stats": selection_stats,
        "selector_version": PUBLIC_SELECTOR_VERSION,
        "identity_version": STORY_IDENTITY_VERSION,
    }
    selection_snapshot = {
        **selection_payload,
        "selection_fingerprint": _stable_hash(selection_payload),
    }
    return {
        "input_fingerprint": snapshot.input_fingerprint,
        "temporary_clusters": len(clusters),
        "temporary_public_clusters": len(public_clusters),
        "item_updates": item_updates,
        "public_clusters": public_clusters,
        "stories": sorted(stories, key=lambda row: str(row["story_id"])),
        "memberships": sorted(
            memberships,
            key=lambda row: (str(row["story_id"]), str(row["item_id"])),
        ),
        "selection_snapshot": selection_snapshot,
    }


def rebuild_all_news_for_maintenance(*, db: Any, now_ms: int) -> dict[str, Any]:
    service = NewsProjectionService(db=db, worker_name="news_maintenance_rebuild")
    snapshot = service.load(now_ms=now_ms)
    if snapshot.unchanged:
        return {
            "projection_status": "unchanged_input",
            "items": len(snapshot.rows),
            "stories": 0,
            "rows_written": 0,
        }
    projection = compute_news_story_projection(snapshot)
    return service.publish(snapshot, projection, now_ms=now_ms)


def _require_bounded_snapshot(snapshot: NewsProjectionSnapshot) -> None:
    _require_bounded_story_rows(snapshot.rows)


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _mode(values: Sequence[str], order: Sequence[str]) -> str:
    counts = Counter(values)
    highest = max(counts.values())
    index = {value: position for position, value in enumerate(order)}
    return min(
        (value for value, count in counts.items() if count == highest),
        key=lambda value: (index.get(value, len(index)), value),
    )


def _categorize_public_story(title: str) -> tuple[str, str]:
    lowered = title.lower()
    for keywords, category, threat_level in _PUBLIC_STORY_CATEGORIES:
        if any(keyword in lowered for keyword in keywords):
            return category, threat_level
    return "general", "moderate"


__all__ = [
    "NEWS_STORY_COMPUTE_TIMEOUT_SECONDS",
    "NEWS_STORY_FAILURE_TIMEOUT_SECONDS",
    "NEWS_STORY_LOAD_TIMEOUT_SECONDS",
    "NEWS_STORY_PUBLISH_TIMEOUT_SECONDS",
    "NewsProjectionInputExceeded",
    "NewsProjectionService",
    "NewsProjectionSnapshot",
    "compute_news_story_projection",
    "rebuild_all_news_for_maintenance",
]
