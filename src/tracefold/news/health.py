from __future__ import annotations

from typing import Any


def attach_pipeline_runtime_health(
    news_health: dict[str, Any],
    *,
    worker_status: dict[str, Any] | None,
    now_ms: int,
) -> None:
    """Attach the current process's rebuild receipt to database-derived health."""

    if not worker_status or not worker_status.get("enabled"):
        return
    story = news_health["layers"]["story"]
    last_finished = worker_status.get("last_finished_at_ms")
    last_result = worker_status.get("last_result")
    notes = dict(last_result.get("notes") or {}) if isinstance(last_result, dict) else {}
    successful_build = (
        last_finished is not None
        and isinstance(last_result, dict)
        and int(last_result.get("failed") or 0) == 0
        and int(last_result.get("dead") or 0) == 0
        and not worker_status.get("last_error")
    )
    last_complete = int(last_finished) if successful_build and last_finished is not None else None
    age_ms = max(0, now_ms - last_complete) if last_complete is not None else None
    story.update(
        {
            "last_complete_rebuild_at_ms": last_complete,
            "last_complete_rebuild_age_ms": age_ms,
            "last_rebuild_item_count": int(notes.get("items") or 0),
            "last_rebuild_story_count": int(notes.get("stories") or 0),
            "last_rebuild_story_writes": int(notes.get("story_writes") or 0),
            "last_rebuild_membership_writes": int(notes.get("membership_writes") or 0),
        }
    )
    reasons = list(story.get("reasons") or [])
    effective_status = str(worker_status.get("effective_status") or "unavailable")
    if effective_status in {"failed", "unavailable"}:
        reasons.append(f"pipeline_worker_{effective_status}")
        story["status"] = "degraded"
    elif last_complete is None:
        reasons.append("pipeline_has_no_complete_rebuild")
        if story["status"] != "degraded":
            story["status"] = "warming"
    elif age_ms is not None and age_ms > 300_000:
        reasons.append("last_complete_rebuild_older_than_5m")
        if story["status"] == "ready":
            story["status"] = "degraded"
    story["reasons"] = list(dict.fromkeys(reasons))
    _recompute_overall(news_health)


def _recompute_overall(news_health: dict[str, Any]) -> None:
    layers = news_health["layers"]
    statuses = [str(layer["status"]) for layer in layers.values()]
    news_health["status"] = "degraded" if "degraded" in statuses else "warming" if "warming" in statuses else "ready"
    news_health["reasons"] = [
        f"{name}:{reason}" for name, layer in layers.items() for reason in layer.get("reasons", ())
    ]


__all__ = ["attach_pipeline_runtime_health"]
