from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def token_radar_publication_status(conn: Any, *, projection_version: str) -> dict[str, Any]:
    version = str(projection_version).strip()
    if not version:
        raise ValueError("token_radar_projection_version_required")
    states = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM token_radar_publication_state
            WHERE projection_version = %s
            ORDER BY "window" ASC, venue ASC
            """,
            (version,),
        ).fetchall()
    ]
    statuses = [str(state["latest_attempt_status"]) for state in states]
    ready_count = statuses.count("ready")
    failed_count = statuses.count("failed")
    if len(statuses) != ready_count + failed_count:
        raise ValueError("token_radar_publication_status_invalid")
    if not states:
        status = "missing"
    elif failed_count == 0:
        status = "ready"
    elif ready_count > 0 or any(state.get("current_generation_id") is not None for state in states):
        status = "degraded"
    else:
        status = "failed"
    return {
        "projection_version": version,
        "status": status,
        "state_count": len(states),
        "ready_count": ready_count,
        "failed_count": failed_count,
        "publication_states": states,
    }


def token_profile_image_repair_targets(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT target_type, target_id, observed_at_ms AS source_watermark_ms
        FROM token_profile_current
        WHERE status = 'ready'
          AND (
            quality_flags_json ? 'logo_mirror_pending'
            OR quality_flags_json ? 'source_not_admitted'
            OR quality_flags_json ? 'logo_mirror_unsupported'
            OR quality_flags_json ? 'logo_mirror_failed'
          )
        ORDER BY updated_at_ms DESC, target_type ASC, target_id ASC
        LIMIT %s
        """,
        (int(limit),),
    ).fetchall()
    return [
        {
            "target_type": str(row["target_type"]),
            "target_id": str(row["target_id"]),
            "source_watermark_ms": _required_source_watermark_ms(row),
            "priority": 25,
        }
        for row in rows
    ]


def _required_source_watermark_ms(row: Mapping[str, Any]) -> int:
    try:
        value = row["source_watermark_ms"]
    except KeyError as exc:
        raise ValueError("token_profile_image_repair_source_watermark_required") from exc
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("token_profile_image_repair_source_watermark_required")
    if value <= 0:
        raise ValueError("token_profile_image_repair_source_watermark_required")
    return int(value)


__all__ = [
    "token_profile_image_repair_targets",
    "token_radar_publication_status",
]
