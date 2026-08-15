from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tracefold.app.repositories import repositories
from tracefold.news.story_projection import (
    NewsStoryFactSnapshot,
    NewsStoryProjection,
    build_story_projection,
)
from tracefold.platform.config.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a zero-write News Story V2 shadow report from the configured PostgreSQL facts.",
    )
    parser.parse_args()
    settings = load_settings(require_ws_token=False)
    now_ms = int(time.time() * 1_000)
    with repositories(settings, role="serve") as repos:
        repos.conn.execute("SET TRANSACTION READ ONLY")
        revision_row = repos.conn.execute("SELECT version_num FROM alembic_version").fetchone()
        payload = repos.news.load_story_projection(now_ms=now_ms)
        current_memberships = [
            dict(row)
            for row in repos.conn.execute(
                """
                SELECT member.story_id, member.item_id
                  FROM news_story_members member
                  JOIN news_stories story ON story.story_id = member.story_id
                 ORDER BY member.story_id, member.item_id
                """
            ).fetchall()
        ]

    snapshot = NewsStoryFactSnapshot(
        material_snapshot_fingerprint=str(payload["material_snapshot_fingerprint"]),
        evaluation_time_ms=int(payload["evaluation_time_ms"]),
        published_material_snapshot_fingerprint=(
            str(payload["published_material_snapshot_fingerprint"])
            if payload.get("published_material_snapshot_fingerprint")
            else None
        ),
        rows=tuple(dict(row) for row in payload["rows"]),
    )
    started = time.perf_counter()
    projection = build_story_projection(snapshot)
    compute_seconds = time.perf_counter() - started
    report = build_shadow_report(
        snapshot=snapshot,
        projection=projection,
        current_memberships=current_memberships,
        compute_seconds=compute_seconds,
        database_revision=str(revision_row["version_num"]) if revision_row is not None else "unknown",
        rss_enabled=bool(settings.news.rss_enabled),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["bounds"]["all_passed"] else 1


def build_shadow_report(
    *,
    snapshot: NewsStoryFactSnapshot,
    projection: NewsStoryProjection,
    current_memberships: Sequence[Mapping[str, Any]],
    compute_seconds: float,
    database_revision: str,
    rss_enabled: bool,
) -> dict[str, Any]:
    new_sizes = Counter(str(row["story_id"]) for row in projection.memberships)
    old_sizes = Counter(str(row["story_id"]) for row in current_memberships)
    old_by_item = {str(row["item_id"]): str(row["story_id"]) for row in current_memberships}
    new_by_item = {str(row["item_id"]): str(row["story_id"]) for row in projection.memberships}
    new_to_old: dict[str, set[str]] = {}
    old_to_new: dict[str, set[str]] = {}
    for item_id in sorted(old_by_item.keys() & new_by_item.keys()):
        old_story_id = old_by_item[item_id]
        new_story_id = new_by_item[item_id]
        new_to_old.setdefault(new_story_id, set()).add(old_story_id)
        old_to_new.setdefault(old_story_id, set()).add(new_story_id)

    evidence_bytes = [
        len(json.dumps(story["identity_evidence"], ensure_ascii=False, sort_keys=True).encode())
        for story in projection.stories
    ]
    diagnostics = dict(projection.diagnostics)
    bounds = {
        "input_rows_at_most_10000": diagnostics["input_physical_item_count"] <= 10_000,
        "input_bytes_at_most_8mib": diagnostics["input_encoded_bytes"] <= 8 * 1024 * 1024,
        "candidate_pairs_at_most_250000": diagnostics["candidate_pair_peak"] <= 250_000,
        "compute_seconds_at_most_25": compute_seconds <= 25.0,
        "identity_evidence_at_most_8kib": max(evidence_bytes, default=0) <= 8 * 1024,
    }
    bounds["all_passed"] = all(bounds.values())
    stories_by_id = {str(story["story_id"]): story for story in projection.stories}
    large_stories = [
        {
            "story_id": story_id,
            "item_count": item_count,
            "source_count": int(stories_by_id[story_id]["source_count"]),
            "anchor_item_id": str(stories_by_id[story_id]["identity_evidence"]["anchor_item_id"]),
            "representative_title": str(stories_by_id[story_id]["representative_title"]),
            "manual_coherence_disposition": "required",
        }
        for story_id, item_count in sorted(new_sizes.items())
        if item_count > 20
    ]
    return {
        "schema_version": "news_story_v2_shadow_v1",
        "mode": "read_only_zero_write",
        "database_revision": database_revision,
        "rss_enabled": rss_enabled,
        "projection_version": projection.projection_version,
        "projection_fingerprint": projection.projection_fingerprint,
        "material_snapshot_fingerprint": snapshot.material_snapshot_fingerprint,
        "evaluation_time_ms": snapshot.evaluation_time_ms,
        "versions": dict(projection.versions),
        "compute_seconds": round(float(compute_seconds), 6),
        "diagnostics": diagnostics,
        "current_story_distribution": _distribution(old_sizes.values()),
        "v2_story_distribution": _distribution(new_sizes.values()),
        "current_to_v2": {
            "shared_item_count": len(old_by_item.keys() & new_by_item.keys()),
            "current_only_item_count": len(old_by_item.keys() - new_by_item.keys()),
            "v2_only_item_count": len(new_by_item.keys() - old_by_item.keys()),
            "merge_count": sum(len(story_ids) > 1 for story_ids in new_to_old.values()),
            "split_count": sum(len(story_ids) > 1 for story_ids in old_to_new.values()),
        },
        "identity_evidence_bytes_max": max(evidence_bytes, default=0),
        "bounds": bounds,
        "stories_over_20_items": large_stories,
    }


def _distribution(values: Iterable[int]) -> dict[str, int]:
    ordered = sorted(int(value) for value in values)
    return {
        "story_count": len(ordered),
        "size_p50": _nearest_rank(ordered, 0.50),
        "size_p90": _nearest_rank(ordered, 0.90),
        "size_p99": _nearest_rank(ordered, 0.99),
        "size_max": max(ordered, default=0),
    }


def _nearest_rank(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    return int(values[max(0, math.ceil(float(percentile) * len(values)) - 1)])


if __name__ == "__main__":
    raise SystemExit(main())
