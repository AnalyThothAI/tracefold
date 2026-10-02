"""Map native notification states to the retained public News vocabulary."""

from collections.abc import Mapping
from typing import Any


def pending_state(state: str) -> str:
    return {"sending": "pending", "terminal": "dead"}.get(state, state)


def receipt_notification(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "intent_id",
        "kind",
        "state",
        "card",
        "receipt",
        "error_code",
        "attempted_at_ms",
        "settled_at_ms",
        "created_at_ms",
        "edit_state",
        "pending_card",
        "edit_error_code",
        "edit_attempted_at_ms",
        "edit_settled_at_ms",
        "content_revision",
        "claim_refs",
        "plan_key",
    )
    value = {key: row[key] for key in keys}
    value["body"] = (row["card"] or {}).get("body")
    value["payload_sha256"] = (row["card"] or {}).get("payload_sha256")
    value["timings"] = row.get("timings")
    value["plan_timings"] = row.get("plan_timings")
    if row["state"] == "sending":
        value["error_code"] = None
    return value


def pending_notification(row: Mapping[str, Any]) -> dict[str, Any]:
    state = pending_state(str(row["state"]))
    return {
        **{
            key: row[key]
            for key in (
                "intent_id",
                "kind",
                "attempts",
                "error_code",
                "next_attempt_at_ms",
                "content_revision",
                "claim_refs",
                "plan_key",
            )
        },
        "state": state,
        "enqueued_at_ms": row["reserved_at_ms"],
        "frozen_card": row["card"] is not None,
        "settled_at_ms": None if state in ("pending", "dead") else row["settled_at_ms"],
    }
