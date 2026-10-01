"""Event timeline: the ordered, human-readable steps one Event went through (pure).

Built from the same rows ``event_detail`` returns (event, members, deliveries and, since #706,
the EventUpdate plane: evidence revisions, semantic observations and adoptions, the notification plan and the
update intents); each step carries a Chinese title/summary and the raw facts it was built from, so the console
shows the sentence and keeps the fields one click away. ``tracefold news why`` prints the same steps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .outcome import (
    Outcome,
    admission_zh,
    delivery_error_zh,
    error_code_zh,
    event_outcome,
    storyline_key_zh,
)
from .update_view import CHANGE_KIND_ZH, INTENT_STATE_ZH, semantic_state


def reader_delivery(
    deliveries: Sequence[Mapping[str, Any]], current_revision: str | None = None
) -> Mapping[str, Any] | None:
    """The Event's representative reader card: the current revision's latest attempt, else its latest sent
    one, else its latest attempt. An earlier revision's receipt never hides the current one's outcome.

    The same order `storage.feed_sql` picks the feed row's `d` by, so the feed row and the detail agree.
    Market follow-up notifications use a separate ledger.
    """

    readers = list(enumerate(deliveries))
    if not readers:
        return None
    return max(
        readers,
        key=lambda pair: (
            current_revision is not None and pair[1].get("content_revision") == current_revision,
            pair[1].get("state") == "sent",
            int(pair[1].get("created_at_ms") or 0),
            str(pair[1].get("intent_id") or ""),
            pair[0],
        ),
    )[1]


def event_timeline(
    *,
    event: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
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
) -> tuple[Outcome, list[dict[str, Any]]]:
    """Return ``(outcome, steps)``; steps are in pipeline order and only include stages that happened.

    Current semantic work contributes evidence, adoption, notification and intent steps in clock order.
    Each intent carries its own receipt step.
    """

    delivery = reader_delivery(deliveries, (notification or {}).get("content_revision"))
    outcome = event_outcome(
        admission=event.get("admission"),
        delivery=delivery,
        delivery_queue=delivery_queue,
        semantic=semantic,
        adopted=adopted,
        notification=notification,
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

    update_steps = _update_steps(
        semantic=semantic,
        evidence_snapshots=evidence_snapshots,
        revisions=revisions,
        observations=observations,
        notification_view=notification_view,
        intents=intents,
    )
    steps.extend(sorted(update_steps, key=lambda step: step["at_ms"]))

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
    adopted_by_result = {
        str(row["observation_result_id"]): row for row in revisions if row.get("observation_result_id") is not None
    }
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
                "title_zh": "事实归属清理" if revision.get("scope_repair_id") else "采用更新",
                "at_ms": int(revision["adopted_at_ms"]),
                "summary_zh": (described or "无新增变化") + f" · {int(revision.get('claim_n') or 0)} 条命题",
                "facts": {
                    "content_revision": revision.get("content_revision"),
                    "previous_content_revision": revision.get("previous_content_revision"),
                    "input_revision": revision.get("input_revision"),
                    "change_kinds": list(kinds) if isinstance(kinds, list) else [],
                    "scope_repair_id": revision.get("scope_repair_id"),
                },
            }
        )
    if semantic is not None and semantic_state(semantic) in {"failed", "cancelled"}:
        cancelled = semantic_state(semantic) == "cancelled"
        steps.append(
            {
                "stage": "semantic",
                "title_zh": "历史语义任务已取消" if cancelled else "语义处理失败",
                "at_ms": int(semantic.get("updated_at_ms") or 0),
                "summary_zh": "旧材料不再解析，保留来源与既有结果"
                if cancelled
                else (error_code_zh(semantic.get("last_error_code")) or "多次尝试后失败，等待新的材料版本"),
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
]
