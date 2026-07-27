from __future__ import annotations

from typing import Any

from tracefold.market.pricing.message_price_payload import message_price_payload
from tracefold.market.radar.post_text_quality import post_quality_score


def token_target_post_payload(
    row: dict[str, Any],
    *,
    stage: dict[str, Any] | None = None,
    bucket_ms: int | None = None,
    since_ms: int | None = None,
) -> dict[str, Any]:
    text = row.get("text_clean") or row.get("text")
    confidence = float(row.get("confidence") or 0.0)
    quality = post_quality_score(
        {
            "text": text,
            "mention_source": "token_intent",
            "attribution_status": "direct",
            "attribution_confidence": confidence,
            "attribution_weight": confidence,
        }
    )
    payload = {
        "event_id": row.get("event_id"),
        "tweet_id": row.get("tweet_id"),
        "target_type": row.get("target_type"),
        "target_id": row.get("target_id"),
        "symbol": row.get("symbol"),
        "author_handle": row.get("author_handle"),
        "text": text,
        "url": row.get("canonical_url"),
        "received_at_ms": row.get("received_at_ms"),
        "mention_source": "token_intent",
        "attribution_status": row.get("attribution_status"),
        "attribution_confidence": confidence,
        "attribution_weight": confidence,
        "event_type": "token_intent",
        "reference": _reference(row.get("reference_json")),
        "price": message_price_payload(row),
        "post_quality": quality,
        "stage_id": stage.get("stage_id") if stage else None,
        "stage_phase": stage.get("stage_phase") if stage else None,
        "author_role": stage.get("author_role") if stage else None,
        "is_stage_representative": bool(stage.get("is_stage_representative")) if stage else False,
        "price_delta_from_previous_post_pct": stage.get("price_delta_from_previous_post_pct") if stage else None,
    }
    if bucket_ms is not None and since_ms is not None:
        received_at_ms = int(row.get("received_at_ms") or 0)
        payload["bucket_start_ms"] = since_ms + ((received_at_ms - since_ms) // bucket_ms) * bucket_ms
    return payload


def _reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "tweet_id": value.get("tweet_id"),
        "author_handle": value.get("author_handle"),
        "type": value.get("type"),
    }
