from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from tracefold.news.classification import SEVERITY_VALUES, classify_by_keyword
from tracefold.news.identity import cluster_texts
from tracefold.news.models import EventCategory, ThreatLevel
from tracefold.news.ranking import (
    diplomacy_entity_keys,
    importance_factors,
    promote_diplomacy_severity,
)
from tracefold.news.sources import reporting_origin_tier
from tracefold.news.story_store import _StorySnapshotLost

NEWS_STORY_LOAD_TIMEOUT_SECONDS = 3.0
NEWS_STORY_COMPUTE_TIMEOUT_SECONDS = 20.0
NEWS_STORY_PUBLISH_TIMEOUT_SECONDS = 8.0
NEWS_STORY_FAILURE_TIMEOUT_SECONDS = 3.0
NEWS_STORY_INPUT_ROW_CAP = 10_000
NEWS_STORY_INPUT_BYTES_CAP = 4 * 1024 * 1024

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


class NewsProjectionInputExceeded(RuntimeError):
    pass


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
        try:
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
        except _StorySnapshotLost as exc:
            return {
                "projection_status": "stale_snapshot",
                "items": exc.items,
                "stories": 0,
                "rows_written": 0,
            }

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
    rows = [dict(row) for row in snapshot.rows]
    clusters = cluster_texts([str(row["title"]) for row in rows])
    cluster_by_item: dict[str, int] = {}
    for cluster_index, indices in enumerate(clusters):
        for item_index in indices:
            cluster_by_item[str(rows[item_index]["item_id"])] = cluster_index

    entity_buckets: dict[str, dict[str, set[str] | set[int]]] = {}
    for row in rows:
        if snapshot.scoring_epoch_ms - int(row["published_at_ms"]) > 86_400_000:
            continue
        origin = str(row["reporting_origin"])
        tier = reporting_origin_tier(origin, fallback_tier=int(row["tier"]))
        for entity_key in diplomacy_entity_keys(str(row["title"])):
            bucket = entity_buckets.setdefault(
                entity_key,
                {"clusters": set(), "origins": set(), "tier12_origins": set()},
            )
            cast(set[int], bucket["clusters"]).add(cluster_by_item[str(row["item_id"])])
            cast(set[str], bucket["origins"]).add(origin)
            if tier <= 2:
                cast(set[str], bucket["tier12_origins"]).add(origin)
    entity_signal_by_cluster: dict[int, tuple[int, int]] = {}
    for bucket in entity_buckets.values():
        origins = cast(set[str], bucket["origins"])
        if len(origins) < 2:
            continue
        signal = (len(origins), len(cast(set[str], bucket["tier12_origins"])))
        for cluster_index in cast(set[int], bucket["clusters"]):
            previous = entity_signal_by_cluster.get(cluster_index, (0, 0))
            entity_signal_by_cluster[cluster_index] = (
                max(previous[0], signal[0]),
                max(previous[1], signal[1]),
            )

    item_updates: list[dict[str, Any]] = []
    stories: list[dict[str, Any]] = []
    memberships: list[dict[str, str]] = []
    for cluster_index, indices in enumerate(clusters):
        members = [rows[index] for index in indices]
        origins = {str(member["reporting_origin"]) for member in members}
        source_count = len(origins)
        entity_count, tier12_entity_count = entity_signal_by_cluster.get(cluster_index, (0, 0))
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

        earliest = min(
            members,
            key=lambda member: (
                int(member["published_at_ms"]),
                str(member["normalized_title"]),
                str(member["item_id"]),
            ),
        )
        canonical_key = hashlib.sha256(str(earliest["normalized_title"]).encode()).hexdigest()
        story_id = canonical_key
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
            "canonical_key": canonical_key,
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
    return {
        "input_fingerprint": snapshot.input_fingerprint,
        "temporary_clusters": len(clusters),
        "item_updates": item_updates,
        "stories": sorted(stories, key=lambda row: str(row["story_id"])),
        "memberships": sorted(
            memberships,
            key=lambda row: (str(row["story_id"]), str(row["item_id"])),
        ),
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
    if len(snapshot.rows) > NEWS_STORY_INPUT_ROW_CAP:
        raise NewsProjectionInputExceeded("news_story_input_row_cap")
    encoded = json.dumps(snapshot.rows, ensure_ascii=False, sort_keys=True, default=str).encode()
    if len(encoded) > NEWS_STORY_INPUT_BYTES_CAP:
        raise NewsProjectionInputExceeded("news_story_input_byte_cap")


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
