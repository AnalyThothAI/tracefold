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
    triage = [v for v in detail["verdicts"] if v["stage"] == "triage"]
    latest = triage[-1] if triage else None
    chain: list[dict[str, Any]] = [
        {
            "stage": "item",
            "raw_first_line": card.get("raw_first_line") or "",
            "title": event.get("leader_title"),
            "reporting_origin": event.get("reporting_origin"),
            "engine_type": event.get("engine_type"),
            "provider_score": event.get("provider_score_max"),
            "provider_coins": [
                f"{c.get('symbol')}:{c.get('grade') or '-'}" for c in (metadata.get("coins") or []) if c.get("symbol")
            ],
            "members": int(event.get("member_count") or 1),
        },
        {
            "stage": "gate",
            "admission": event.get("admission"),
            "priority": event.get("priority"),
            "asset_class": event.get("asset_class"),
            "grounded_assets": list(event.get("grounded_assets") or []),
            "watchlist_hits": list(event.get("watchlist_hits") or []),
            "macro_lexicon": bool(event.get("macro_lexicon")),
            "storyline_key": event.get("storyline_key"),
            "published_at_ms": event.get("published_at_ms"),
        },
    ]
    if latest is not None:
        verdict = dict(latest.get("verdict") or {})
        trace = dict(latest.get("trace") or {})
        chain.append(
            {
                "stage": "triage",
                "policy_version": latest.get("policy_version"),
                "prompt_version": latest.get("prompt_version"),
                "model": latest.get("model"),
                "degraded": bool(latest.get("degraded")),
                "error_code": latest.get("error_code"),
                "model_decision": latest.get("model_decision"),
                "event_type": verdict.get("event_type"),
                "audience": verdict.get("audience"),
                "assets": verdict.get("assets"),
                "direction": verdict.get("direction"),
                "scope": verdict.get("scope"),
                "magnitude": verdict.get("magnitude"),
                "actionable": verdict.get("actionable"),
                "headline_zh": verdict.get("headline_zh"),
                "title_zh": verdict.get("title_zh"),
                "why_zh": verdict.get("why_zh"),
            }
        )
        chain.append(
            {
                "stage": "decide",
                "rule_baseline_decision": latest.get("rule_baseline_decision"),
                "final_decision": latest.get("final_decision"),
                "override_rule": latest.get("override_rule"),
                "throttled_by": latest.get("throttled_by"),
                "storyline_key": trace.get("storyline_key") or event.get("storyline_key"),
                "status_preliminary": trace.get("status"),
                "status_final": trace.get("status_final"),
                "queue_lag_ms": trace.get("queue_lag_ms"),
                "latency_ms": trace.get("latency_ms"),
                "published_at_ms": latest.get("published_at_ms"),
            }
        )
    chain.extend(
        {
            "stage": "delivery",
            **{k: d.get(k) for k in ("kind", "state", "error_code", "attempted_at_ms", "settled_at_ms")},
        }
        for d in detail["deliveries"]
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
        "labels": detail.get("labels") or [],
    }


def outcome_line(outcome: Mapping[str, Any]) -> str:
    """The same sentence the console shows: text_zh, a full-width colon, reason_zh (reason omitted when empty)."""

    text = str(outcome.get("text_zh") or "")
    reason = str(outcome.get("reason_zh") or "")
    return f"{text}：{reason}" if reason else text


__all__ = ["explain_event", "outcome_line"]
