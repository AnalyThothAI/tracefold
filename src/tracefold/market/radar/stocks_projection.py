from __future__ import annotations

import hashlib
import json
from typing import Any

_RANK_LIMIT = 100
_SOURCE_EVENT_LIMIT = 25


def compute_stocks_radar_target_feature(
    payload: dict[str, Any],
) -> dict[str, Any]:
    target_id = str(payload["target_id"])
    window = str(payload["window"])
    now_ms = int(payload["now_ms"])
    rows_by_event = {str(row["event_id"]): dict(row) for row in payload.get("rows", [])}
    ordered_events = sorted(
        rows_by_event.values(),
        key=lambda row: (
            -int(row["received_at_ms"]),
            str(row["event_id"]),
        ),
    )
    feature: dict[str, Any] | None = None
    if ordered_events:
        latest = ordered_events[0]
        author_handles = {
            str(row.get("author_handle") or "").strip().lower()
            for row in ordered_events
            if str(row.get("author_handle") or "").strip()
        }
        feature_state = {
            "window_key": window,
            "target_id": target_id,
            "symbol": str(latest["symbol"]),
            "security_name": str(latest["security_name"]),
            "exchange": str(latest["exchange"]),
            "instrument_type": str(latest["instrument_type"]),
            "mentions": len(ordered_events),
            "unique_authors": len(author_handles),
            "latest_seen_ms": int(latest["received_at_ms"]),
            "latest_event_id": str(latest["event_id"]),
            "latest_author_handle": (str(latest["author_handle"]) if latest.get("author_handle") else None),
            "latest_text": str(latest["text"]),
            "source_event_ids": [str(row["event_id"]) for row in ordered_events[:_SOURCE_EVENT_LIMIT]],
        }
        feature = {
            **feature_state,
            "state_fingerprint": _fingerprint(feature_state),
            "computed_at_ms": now_ms,
        }
    return {
        "feature": feature,
        "source_rows": len(ordered_events),
        "target_id": target_id,
        "window": window,
    }


def rank_stocks_radar(
    payload: dict[str, Any],
) -> dict[str, Any]:
    window = str(payload["window"])
    now_ms = int(payload["now_ms"])
    features = {str(row["target_id"]): dict(row) for row in payload.get("current_features", [])}
    ranked_features = sorted(
        features.values(),
        key=lambda row: (
            -int(row["mentions"]),
            -int(row["latest_seen_ms"]),
            str(row["symbol"]),
            str(row["target_id"]),
        ),
    )[:_RANK_LIMIT]
    ranked_rows = [
        {
            **{key: value for key, value in row.items() if key != "rank"},
            "rank": rank,
            "window_key": window,
            "computed_at_ms": now_ms,
        }
        for rank, row in enumerate(ranked_features, start=1)
    ]
    return {
        "rows": ranked_rows,
        "state_fingerprint": _fingerprint(
            [
                [
                    row["rank"],
                    row["target_id"],
                    row["state_fingerprint"],
                ]
                for row in ranked_rows
            ]
        ),
        "source_frontier_ms": max(
            (int(row["latest_seen_ms"]) for row in ranked_rows),
            default=0,
        ),
    }


def _fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


__all__ = [
    "compute_stocks_radar_target_feature",
    "rank_stocks_radar",
]
