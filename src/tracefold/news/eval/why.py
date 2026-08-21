"""`tracefold news why <event_id>`: one Event's whole chain, from raw first line to delivery (read-only, no model)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def explain_event(repos: Any, event_id: str) -> dict[str, Any] | None:
    """Return the ordered chain for one Event, ending with a one-line ``outcome`` a person can read at a glance."""

    detail = repos.news.event_detail(event_id)
    card = repos.news.event_card(event_id)
    if detail is None or card is None:
        return None
    event = dict(detail["event"])
    metadata = dict(card.get("provider_metadata") or {})
    steps = list(detail.get("timeline") or [])
    received = next((step for step in steps if step["stage"] == "received"), None)
    chain: list[dict[str, Any]] = [
        {
            "stage": "item",
            "raw_first_line": card.get("raw_first_line") or "",
            "title": event.get("leader_title"),
            "engine_type": event.get("engine_type"),
            "provider_coins": [
                f"{c.get('symbol')}:{c.get('grade') or '-'}" for c in (metadata.get("coins") or []) if c.get("symbol")
            ],
            **dict((received or {}).get("facts") or {}),
        }
    ]
    # Every later stage is the timeline step's facts verbatim: the CLI and the console read one projection.
    chain.extend(
        {"stage": step["stage"], **dict(step.get("facts") or {})} for step in steps if step["stage"] != "received"
    )
    outcome = dict(detail.get("outcome") or {})
    return {
        "event_id": event_id,
        "outcome": outcome_line(outcome),
        "outcome_kind": outcome.get("kind"),
        "timeline": [
            {"stage": s["stage"], "title_zh": s["title_zh"], "at_ms": s["at_ms"], "summary_zh": s["summary_zh"]}
            for s in (detail.get("timeline") or [])
        ],
        "chain": chain,
    }


def outcome_line(outcome: Mapping[str, Any]) -> str:
    """The same sentence the console shows: text_zh, a full-width colon, reason_zh (reason omitted when empty)."""

    text = str(outcome.get("text_zh") or "")
    reason = str(outcome.get("reason_zh") or "")
    return f"{text}：{reason}" if reason else text


__all__ = ["explain_event", "outcome_line"]
