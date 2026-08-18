"""`tracefold news why <event_id>`: one Event's whole chain, from raw first line to delivery (read-only, no model)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_HELD = frozenset({"drop", "throttled", "degraded"})


def explain_event(repos: Any, event_id: str) -> dict[str, Any] | None:
    """Return the ordered chain for one Event, ending with a one-line ``outcome`` a person can read at a glance."""

    detail = repos.news.event_detail(event_id)
    card = repos.news.event_card(event_id)
    if detail is None or card is None:
        return None
    event = dict(detail["event"])
    metadata = dict(card.get("provider_metadata") or {})
    triage = [v for v in detail["verdicts"] if v["stage"] == "triage"]
    deep = [v for v in detail["verdicts"] if v["stage"] == "deep"]
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
                "rationale": verdict.get("rationale"),
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
            "stage": "analyst",
            "final_decision": v.get("final_decision"),
            "degraded": bool(v.get("degraded")),
            "error_code": v.get("error_code"),
            "throttled_by": v.get("throttled_by"),
            "verdict": v.get("verdict"),
        }
        for v in deep
    )
    chain.extend(
        {
            "stage": "delivery",
            **{k: d.get(k) for k in ("kind", "state", "error_code", "attempted_at_ms", "settled_at_ms")},
        }
        for d in detail["deliveries"]
    )
    return {
        "event_id": event_id,
        "outcome": _outcome_line(event, latest, detail["deliveries"]),
        "chain": chain,
        "labels": detail.get("labels") or [],
    }


def _outcome_line(event: Mapping[str, Any], latest: Mapping[str, Any] | None, deliveries: list[Any]) -> str:
    admission = str(event.get("admission") or "")
    if admission not in {"candidate", "listing_deterministic"}:
        return f"held at gate: {admission}"
    if latest is None:
        return "admitted, waiting for triage" if event.get("published_at_ms") else "admitted, not yet published"
    final = str(latest.get("final_decision") or "")
    reason = latest.get("throttled_by") or latest.get("override_rule") or ""
    if final in _HELD:
        return f"held at decide: {final} ({reason})"
    sent = [d for d in deliveries if d.get("state") == "sent"]
    terminal = [d for d in deliveries if d.get("state") == "terminal"]
    if sent:
        return f"{final} ({reason}) -> delivered {', '.join(str(d.get('kind')) for d in sent)}"
    if terminal:
        return f"{final} ({reason}) -> delivery terminal: {terminal[-1].get('error_code')}"
    return f"{final} ({reason}) -> awaiting delivery"


__all__ = ["explain_event"]
