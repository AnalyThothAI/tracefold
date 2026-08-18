"""Event timeline: the ordered, human-readable steps one Event went through (pure).

Built from the same rows ``event_detail`` returns (event, members, verdicts, deliveries); each step carries a Chinese
title/summary and the raw facts it was built from, so the console shows the sentence and keeps the fields one click
away. ``tracefold news why`` prints the same steps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .outcome import (
    Outcome,
    admission_zh,
    decision_zh,
    delivery_error_zh,
    direction_zh,
    error_code_zh,
    event_outcome,
    event_type_zh,
    magnitude_zh,
    override_rule_zh,
    scope_zh,
    storyline_key_zh,
    throttled_by_zh,
)


def _latest_triage(verdicts: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    triage = [v for v in verdicts if v.get("stage") == "triage"]
    return triage[-1] if triage else None


def _first_delivery(deliveries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    first = [d for d in deliveries if d.get("kind") == "first"]
    return first[-1] if first else None


def event_timeline(
    *,
    event: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
    verdicts: Sequence[Mapping[str, Any]],
    deliveries: Sequence[Mapping[str, Any]],
) -> tuple[Outcome, list[dict[str, Any]]]:
    """Return ``(outcome, steps)``; steps are in pipeline order and only include stages that happened."""

    latest = _latest_triage(verdicts)
    delivery = _first_delivery(deliveries)
    outcome = event_outcome(
        admission=event.get("admission"),
        published_at_ms=event.get("published_at_ms"),
        triage=latest,
        delivery=delivery,
    )
    steps: list[dict[str, Any]] = []

    member_count = int(event.get("member_count") or max(len(members), 1))
    origins = sorted({str(m.get("reporting_origin") or "") for m in members if m.get("reporting_origin")})
    received_bits = [f"来源 {event.get('reporting_origin') or '-'}"]
    if member_count > 1:
        sources = f"（{len(origins)} 个来源）" if len(origins) > 1 else ""
        received_bits.append(f"归并 {member_count} 条同类报道{sources}")
    if event.get("ingest_mode") == "recovery":
        received_bits.append("断线补抄")
    steps.append(
        {
            "stage": "received",
            "title_zh": "收到",
            "at_ms": int(event["opened_at_ms"]),
            "summary_zh": " · ".join(received_bits),
            "facts": {
                "reporting_origin": event.get("reporting_origin"),
                "member_count": member_count,
                "origins": origins,
                "ingest_mode": event.get("ingest_mode"),
                "provider_score_max": event.get("provider_score_max"),
                "provenance": list(event.get("provenance") or []),
            },
        }
    )

    admission = str(event.get("admission") or "")
    gate_bits = [admission_zh(admission)]
    if event.get("priority") == "high":
        gate_bits.append("高优先级")
    grounded = list(event.get("grounded_assets") or [])
    shown = list(dict.fromkeys(str(s).replace("XYZ-", "") for s in grounded))
    if shown:
        gate_bits.append("关联 " + " ".join(shown[:4]))
    if event.get("watchlist_hits"):
        gate_bits.append("命中关注列表")
    steps.append(
        {
            "stage": "gate",
            "title_zh": "门禁",
            "at_ms": int(event["opened_at_ms"]),
            "summary_zh": " · ".join(gate_bits),
            "facts": {
                "admission": admission,
                "priority": event.get("priority"),
                "asset_class": event.get("asset_class"),
                "grounded_assets": grounded,
                "watchlist_hits": list(event.get("watchlist_hits") or []),
                "macro_lexicon": bool(event.get("macro_lexicon")),
                "storyline_key": event.get("storyline_key"),
                "storyline_zh": storyline_key_zh(event.get("storyline_key")),
                "published_at_ms": event.get("published_at_ms"),
            },
        }
    )

    if latest is not None:
        verdict = dict(latest.get("verdict") or {})
        degraded = bool(latest.get("degraded"))
        if degraded:
            triage_summary = "模型不可用：" + (error_code_zh(latest.get("error_code")) or "未知原因") + "，按规则兜底"
        else:
            bits = [str(verdict.get("headline_zh") or "").strip() or "（无标题）"]
            facts_bits = [
                direction_zh(verdict.get("direction")),
                magnitude_zh(verdict.get("magnitude")),
                event_type_zh(verdict.get("event_type")),
            ]
            bits.append(" / ".join(b for b in facts_bits if b))
            model_decision = latest.get("model_decision")
            if model_decision:
                bits.append("模型建议：" + decision_zh(model_decision))
            triage_summary = " · ".join(b for b in bits if b)
        steps.append(
            {
                "stage": "triage",
                "title_zh": "审稿",
                "at_ms": int(latest["created_at_ms"]),
                "summary_zh": triage_summary,
                "facts": {
                    "degraded": degraded,
                    "error_code": latest.get("error_code"),
                    "model": latest.get("model"),
                    "model_decision": latest.get("model_decision"),
                    "event_type": verdict.get("event_type"),
                    "direction": verdict.get("direction"),
                    "magnitude": verdict.get("magnitude"),
                    "scope": verdict.get("scope"),
                    "scope_zh": scope_zh(verdict.get("scope")),
                    "actionable": verdict.get("actionable"),
                    "confidence": verdict.get("confidence"),
                    "assets": verdict.get("assets"),
                    "headline_zh": verdict.get("headline_zh"),
                    "title_zh": verdict.get("title_zh"),
                    "why_zh": verdict.get("why_zh"),
                    "audience": verdict.get("audience"),
                    "prompt_version": latest.get("prompt_version"),
                    "policy_version": latest.get("policy_version"),
                    "latency_ms": (latest.get("trace") or {}).get("latency_ms"),
                },
            }
        )
        final = str(latest.get("final_decision") or "")
        trace = dict(latest.get("trace") or {})
        if final == "throttled":
            decide_summary = "限流 · " + throttled_by_zh(latest.get("throttled_by"))
        else:
            reason = override_rule_zh(latest.get("override_rule"))
            decide_summary = decision_zh(final) + (f" · {reason}" if reason else "")
        steps.append(
            {
                "stage": "decide",
                "title_zh": "决策",
                "at_ms": int(latest["created_at_ms"]),
                "summary_zh": decide_summary,
                "facts": {
                    "final_decision": final,
                    "override_rule": latest.get("override_rule"),
                    "throttled_by": latest.get("throttled_by"),
                    "rule_baseline_decision": latest.get("rule_baseline_decision"),
                    "storyline_key": trace.get("storyline_key") or event.get("storyline_key"),
                    "storyline_zh": storyline_key_zh(trace.get("storyline_key") or event.get("storyline_key")),
                    "status_final": trace.get("status_final") or trace.get("status"),
                    "queue_lag_ms": trace.get("queue_lag_ms"),
                    "published_at_ms": latest.get("published_at_ms"),
                },
            }
        )

    for row in deliveries:
        state = str(row.get("state") or "")
        if state == "sent":
            summary = "已推送到飞书"
        elif state == "terminal":
            summary = "未送达：" + (delivery_error_zh(row.get("error_code")) or "未知原因")
        else:
            summary = "推送中"
        steps.append(
            {
                "stage": "delivery",
                "title_zh": "推送" if row.get("kind") == "first" else "跟进推送",
                "at_ms": int(row.get("settled_at_ms") or row.get("attempted_at_ms") or 0),
                "summary_zh": summary,
                "facts": {
                    "kind": row.get("kind"),
                    "state": state,
                    "error_code": row.get("error_code"),
                    "attempted_at_ms": row.get("attempted_at_ms"),
                    "settled_at_ms": row.get("settled_at_ms"),
                },
            }
        )
    return outcome, steps


__all__ = ["event_timeline"]
