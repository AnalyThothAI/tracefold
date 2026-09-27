"""Event timeline: the ordered, human-readable steps one Event went through (pure).

Built from the same rows ``event_detail`` returns (event, members, legacy verdicts, deliveries and, since #706,
the EventUpdate plane: evidence revisions, semantic observations and adoptions, the notification plan and the
update intents); each step carries a Chinese title/summary and the raw facts it was built from, so the console
shows the sentence and keeps the fields one click away. ``tracefold news why`` prints the same steps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from .outcome import (
    Outcome,
    admission_zh,
    decision_zh,
    delivery_error_zh,
    direction_zh,
    error_code_zh,
    event_outcome,
    fact_kind_zh,
    override_rule_zh,
    scope_zh,
    storyline_key_zh,
    throttled_by_zh,
)
from .update_view import CHANGE_KIND_ZH, INTENT_STATE_ZH, semantic_state

_READER_DELIVERY_KINDS: Final = frozenset({"first", "update"})


def _latest_triage(verdicts: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    triage = [v for v in verdicts if v.get("stage") == "triage"]
    return triage[-1] if triage else None


def reader_delivery(deliveries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The Event's representative reader card: its latest sent one, else its latest attempt.

    The same order `storage.feed_sql` picks the feed row's `d` by, so the feed row and the detail agree.
    Legacy `followup` cards were never an Event's outcome and stay out.
    """

    readers = [(index, row) for index, row in enumerate(deliveries) if row.get("kind") in _READER_DELIVERY_KINDS]
    if not readers:
        return None
    return max(
        readers,
        key=lambda pair: (
            pair[1].get("state") == "sent",
            int(pair[1].get("created_at_ms") or 0),
            str(pair[1].get("intent_id") or ""),
            pair[0],
        ),
    )[1]


def restated_card(trace: Mapping[str, Any], verdict: Mapping[str, Any], *, at_ms: int) -> dict[str, Any] | None:
    """The told-ledger entry a restatement verdict points at (``verdict.restates`` indexes ``trace.told``), with its
    age at decision time; None when the verdict is not a grounded restatement."""

    if str(verdict.get("novelty") or "") != "restatement":
        return None
    told = trace.get("told")
    index = verdict.get("restates")
    if not isinstance(told, list) or not isinstance(index, int) or not 0 <= index < len(told):
        return None
    entry = told[index] if isinstance(told[index], Mapping) else {}
    entry_at = int(entry.get("at_ms") or 0)
    return {
        "event_id": entry.get("event_id"),
        "headline_zh": str(entry.get("headline_zh") or ""),
        "at_ms": entry_at or None,
        "ago_min": max(0, at_ms - entry_at) // 60_000 if entry_at else int(entry.get("ago_min") or 0),
        "history_scope": str(entry.get("history_scope") or "recent"),
        "retrieval_reason": str(entry.get("retrieval_reason") or "recent"),
    }


def _restatement_reason(restated: Mapping[str, Any]) -> str:
    reason = f"重复：{restated['ago_min']} 分钟前已推「{restated['headline_zh']}」"
    retrieval_zh = {
        "recent": "4 小时近期记录",
        "exact_fingerprint": "精确事实定向召回",
        "canonical_asset_overlap": "同一标的定向召回",
        "title_similarity": "标题相似定向召回",
    }.get(str(restated["retrieval_reason"]))
    return reason + (f" · {retrieval_zh}" if retrieval_zh else "")


def _seen_summary(latest: Mapping[str, Any], seen_against: Mapping[str, Any]) -> str:
    ago = max(0, int(latest["created_at_ms"]) - int(seen_against.get("at_ms") or 0)) // 60_000
    return f"重复拦截 · {ago} 分钟前已推「{seen_against.get('headline_zh')}」"


def _decision_inputs(
    latest: Mapping[str, Any], verdict: Mapping[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any] | None, Mapping[str, Any] | None]:
    trace = dict(latest.get("trace") or {})
    restated = restated_card(trace, verdict, at_ms=int(latest["created_at_ms"]))
    seen = trace.get("seen_against") if isinstance(trace.get("seen_against"), Mapping) else None
    return str(latest.get("final_decision") or ""), trace, restated, seen


def event_timeline(
    *,
    event: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
    verdicts: Sequence[Mapping[str, Any]],
    deliveries: Sequence[Mapping[str, Any]],
    delivery_queue: Mapping[str, Any] | None = None,
    semantic: Mapping[str, Any] | None = None,
    adopted: bool = False,
    notification: Mapping[str, Any] | None = None,
    evidence_snapshots: Sequence[Mapping[str, Any]] = (),
    revisions: Sequence[Mapping[str, Any]] = (),
    observations: Sequence[Mapping[str, Any]] = (),
    notification_view: Mapping[str, Any] | None = None,
    intents: Sequence[Mapping[str, Any]] = (),
    now_ms: int | None = None,
) -> tuple[Outcome, list[dict[str, Any]]]:
    """Return ``(outcome, steps)``; steps are in pipeline order and only include stages that happened.

    A legacy Event (a Triage verdict and no semantic work) keeps exactly its triage/decide steps. An Event
    on the EventUpdate path gets evidence, semantic, notification and intent steps instead, in clock order
    after the Gate; a legacy verdict it also carries stays as the history it is.
    """

    latest = _latest_triage(verdicts)
    delivery = reader_delivery(deliveries)
    outcome = event_outcome(
        admission=event.get("admission"),
        opened_at_ms=event.get("opened_at_ms"),
        published_at_ms=event.get("published_at_ms"),
        triage=latest,
        delivery=delivery,
        delivery_queue=delivery_queue,
        semantic=semantic,
        adopted=adopted,
        notification=notification,
        now_ms=now_ms,
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
            facts_bits = [direction_zh(verdict.get("direction")), fact_kind_zh(verdict.get("fact_kind"))]
            bits.append(" / ".join(b for b in facts_bits if b))
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
                    "judgment_origin": latest.get("judgment_origin"),
                    "judgment_contract_version": latest.get("judgment_contract_version"),
                    "event_kind": event.get("event_kind"),
                    "direction": verdict.get("direction"),
                    "fact_kind": verdict.get("fact_kind"),
                    "scope": verdict.get("scope"),
                    "scope_zh": scope_zh(verdict.get("scope")),
                    "confidence": verdict.get("confidence"),
                    "assets": verdict.get("assets"),
                    "headline_zh": verdict.get("headline_zh"),
                    "why_zh": verdict.get("why_zh"),
                    "evidence_ref": verdict.get("evidence_ref"),
                    "program_version": latest.get("program_version"),
                    "program_sha256": latest.get("program_sha256"),
                    "policy_version": latest.get("policy_version"),
                    # #675 PR-3: how this Event carries each instrument the judgment names, beside the
                    # assets themselves. `null` for a verdict written before the reading existed and for
                    # a judgment that named none.
                    "grounding": (latest.get("trace") or {}).get("grounding"),
                    "latency_ms": (latest.get("trace") or {}).get("latency_ms"),
                },
            }
        )
        final, trace, restated, seen_against = _decision_inputs(latest, verdict)
        if final == "throttled":
            duplicate = str(latest.get("throttled_by") or "").endswith(":seen")
            decide_summary = ("重复拦截 · " if duplicate else "历史限流 · ") + throttled_by_zh(
                latest.get("throttled_by")
            )
            if duplicate and seen_against:
                decide_summary = _seen_summary(latest, seen_against)
        else:
            reason = override_rule_zh(latest.get("override_rule"))
            if latest.get("override_rule") == "restatement" and restated is not None:
                reason = _restatement_reason(restated)
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
                    "storyline_key_preliminary": trace.get("storyline_key_preliminary"),
                    "status_preliminary": trace.get("status"),
                    "status_final": trace.get("status_final"),
                    "novelty": verdict.get("novelty"),
                    "restates_event_id": trace.get("restates_event_id"),
                    "restated_headline_zh": restated["headline_zh"] if restated else None,
                    "restated_at_ms": restated["at_ms"] if restated else None,
                    "restated_history_scope": restated["history_scope"] if restated else None,
                    "restated_retrieval_reason": restated["retrieval_reason"] if restated else None,
                    "told_count": trace.get("told_count"),
                    "seen_count": trace.get("seen_count"),
                    "seen_similarity": trace.get("seen_similarity"),
                    "seen_scope": trace.get("seen_scope"),
                    "seen_against_event_id": (seen_against or {}).get("event_id"),
                    "seen_against_headline_zh": (seen_against or {}).get("headline_zh"),
                    "reasked_after_told_change": bool(trace.get("reasked_after_told_change")),
                    "queue_lag_ms": trace.get("queue_lag_ms"),
                    "latency_ms": trace.get("latency_ms"),
                    "published_at_ms": latest.get("published_at_ms"),
                },
            }
        )

    update_steps = _update_steps(
        semantic=semantic,
        evidence_snapshots=evidence_snapshots,
        revisions=revisions,
        observations=observations,
        notification_view=notification_view,
        intents=intents,
    )
    steps.extend(sorted(update_steps, key=lambda step: step["at_ms"]))

    for row in deliveries:
        if row.get("kind") == "update":
            # An update intent is one step with its queue and its ledger row, above.
            continue
        state = str(row.get("state") or "")
        if state == "sent":
            summary = "已送达"
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


def _change_kinds_zh(kinds: Any) -> str:
    counts: dict[str, int] = {}
    for kind in kinds if isinstance(kinds, list) else ():
        label = CHANGE_KIND_ZH.get(str(kind), str(kind))
        counts[label] = counts.get(label, 0) + 1
    return "、".join(f"{label} ×{n}" if n > 1 else label for label, n in counts.items())


def _update_steps(
    *,
    semantic: Mapping[str, Any] | None,
    evidence_snapshots: Sequence[Mapping[str, Any]],
    revisions: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    notification_view: Mapping[str, Any] | None,
    intents: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The EventUpdate path's steps (#706), each from one durable row and never from a guess."""

    steps: list[dict[str, Any]] = []
    for snapshot in evidence_snapshots:
        version = int(snapshot["evidence_version"])
        steps.append(
            {
                "stage": "evidence",
                "title_zh": "材料版本" if version == 1 else "材料更新",
                "at_ms": int(snapshot["created_at_ms"]),
                "summary_zh": f"第 {version} 版证据" + ("" if version == 1 else "：新成员或正文修订"),
                "facts": {
                    "evidence_version": version,
                    "evidence_sha256": snapshot.get("evidence_sha256"),
                    "focus_fact_id": snapshot.get("focus_fact_id"),
                },
            }
        )
    adopted_by_result = {str(row["observation_result_id"]): row for row in revisions}
    for observation in observations:
        adoption = adopted_by_result.get(str(observation["result_id"]))
        summary = f"输入第 {int(observation['input_revision'])} 版"
        summary += " · 形成新的事件更新" if adoption is not None else " · 内容无实质变化，未改写已采用版本"
        steps.append(
            {
                "stage": "semantic",
                "title_zh": "语义理解",
                "at_ms": int(observation["completed_at_ms"]),
                "summary_zh": summary,
                "facts": {
                    "result_id": observation.get("result_id"),
                    "input_revision": observation.get("input_revision"),
                    "program_identity": observation.get("program_identity"),
                    "adopted_content_revision": observation.get("adopted_content_revision"),
                },
            }
        )
    for revision in revisions:
        kinds = revision.get("change_kinds")
        described = _change_kinds_zh(kinds)
        steps.append(
            {
                "stage": "semantic",
                "title_zh": "采用更新",
                "at_ms": int(revision["adopted_at_ms"]),
                "summary_zh": (described or "无新增变化") + f" · {int(revision.get('claim_n') or 0)} 条命题",
                "facts": {
                    "content_revision": revision.get("content_revision"),
                    "previous_content_revision": revision.get("previous_content_revision"),
                    "input_revision": revision.get("input_revision"),
                    "change_kinds": list(kinds) if isinstance(kinds, list) else [],
                },
            }
        )
    if semantic is not None and semantic_state(semantic) == "failed":
        steps.append(
            {
                "stage": "semantic",
                "title_zh": "语义处理失败",
                "at_ms": int(semantic.get("updated_at_ms") or 0),
                "summary_zh": error_code_zh(semantic.get("last_error_code")) or "多次尝试后失败，等待新的材料版本",
                "facts": {
                    "wanted_revision": semantic.get("wanted_revision"),
                    "attempts": semantic.get("attempts"),
                    "last_outcome": semantic.get("last_outcome"),
                    "last_error_code": semantic.get("last_error_code"),
                },
            }
        )
    plan = (notification_view or {}).get("plan")
    if notification_view is not None and isinstance(plan, Mapping):
        selected = [row for row in plan.get("claim_decisions") or () if row.get("decision") == "notify"]
        summary = str(plan.get("action_zh") or "")
        if plan.get("action") == "notify":
            summary += f" {len(selected)} 条命题" + (" · 重点" if plan.get("key") else "")
        summary += f" · {plan.get('reason_zh')}" if plan.get("reason_zh") else ""
        steps.append(
            {
                "stage": "notify",
                "title_zh": "通知决策",
                "at_ms": int(notification_view["updated_at_ms"]),
                "summary_zh": summary,
                "facts": {
                    "action": plan.get("action"),
                    "reason": plan.get("reason"),
                    "key": plan.get("key"),
                    "content_revision": notification_view.get("content_revision"),
                    "selected_claim_refs": [row.get("claim_ref") for row in selected],
                    "reader_revision": plan.get("reader_revision"),
                },
            }
        )
    for intent in intents:
        state = str(intent["state"])
        summary = INTENT_STATE_ZH.get(state, state)
        if state in {"terminal", "dead"} and intent.get("error_code"):
            summary += "：" + (delivery_error_zh(intent.get("error_code")) or str(intent.get("error_code")))
        summary += f" · {len(intent.get('claim_refs') or [])} 条命题"
        steps.append(
            {
                "stage": "delivery",
                "title_zh": "推送" + (" · 重点" if intent.get("key") else ""),
                "at_ms": int(
                    intent.get("settled_at_ms") or intent.get("attempted_at_ms") or intent.get("enqueued_at_ms") or 0
                ),
                "summary_zh": summary,
                "facts": {
                    "intent_id": intent.get("intent_id"),
                    "state": state,
                    "error_code": intent.get("error_code"),
                    "claim_refs": list(intent.get("claim_refs") or []),
                    "attempted_at_ms": intent.get("attempted_at_ms"),
                    "settled_at_ms": intent.get("settled_at_ms"),
                },
            }
        )
    return steps


__all__ = [
    "event_timeline",
    "reader_delivery",
    "restated_card",
]
