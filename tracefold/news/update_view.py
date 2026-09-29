"""The read projection of one Event's adopted EventUpdate and of the work that produced and sent it (#706).

Pure: storage hands in the rows it read, this module names every business word in Chinese beside the raw
enum, so no browser owns a vocabulary table. A stored document uses the exact `EventUpdate` contract.
A row the contract rejects is reported as undecodable rather than rendered from guessed fields, and a field the
contract leaves unset stays unknown.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import ValidationError

from .taxonomy import IPTC_SUBJECT_LABELS_ZH, source_authority_zh
from .updates.contracts import Claim, EventUpdate, Evidence
from .updates.notification import ClaimDecision, NotificationPlan

UPDATE_DECODE_ERROR: Final = "news_event_update_undecodable"
PLAN_DECODE_ERROR: Final = "news_notification_plan_undecodable"
# The evidence text is the whole provider body. The citations carry the exact quotes a claim rests on,
# so the source list shows the head of the text and says when it stopped.
EVIDENCE_TEXT_MAX: Final = 1200

MODE_ZH: Final[dict[str, str]] = {
    "observation": "观测事实",
    "decision": "决定",
    "commitment": "承诺",
    "conditional_threat": "条件性威胁",
    "guidance": "前瞻指引",
    "forecast": "预测",
    "commentary": "表态或观点",
    "promotion": "推广",
    "unknown": "表达方式未知",
}
PHASE_ZH: Final[dict[str, str]] = {
    "proposed": "拟议",
    "announced": "已宣布",
    "ordered": "已下令",
    "effective": "已生效",
    "executing": "执行中",
    "completed": "已完成",
    "cancelled": "已取消",
    "unknown": "阶段未知",
}
CONTENT_KIND_ZH: Final[dict[str, str]] = {
    "state_change": "状态变化",
    "official_measure": "官方措施",
    "new_quantity": "新数据",
    "level_crossed": "关口",
    "period_record": "期内纪录",
    "quantified_flow": "资金流",
    "schedule": "日程",
    "other": "其他",
}
POLARITY_ZH: Final[dict[str, str]] = {"affirmative": "肯定", "negative": "否定", "unknown": "未知"}
CHANGE_KIND_ZH: Final[dict[str, str]] = {
    "new_fact": "新事实",
    "possible_new": "可能是新内容（与旧命题的关系未能确认）",
    "parameter_change": "参数变化",
    "phase_change": "阶段推进",
    "scope_change": "范围变化",
    "correction": "更正",
    "conflict": "冲突",
    "evidence_change": "证据变化",
    "scope_retraction": "事实归属撤回",
    "restatement": "复述",
}
CLAIM_RELATION_ZH: Final[dict[str, str]] = {
    "equivalent": "等价",
    "adds_information": "补充信息",
    "real_world_change": "现实变化",
    "corrects": "更正",
    "conflicts": "冲突",
    "unrelated": "无关",
    "unresolved": "未能判定",
}
EVIDENCE_RELATION_ZH: Final[dict[str, str]] = {
    "supports": "支持",
    "refutes": "反驳",
    "reports": "转述",
    "not_addressed": "未涉及",
    "unresolved": "未能判定",
}
IMPLICATION_ORIGIN_ZH: Final[dict[str, str]] = {
    "reported_causality": "来源所述因果（推断）",
    "system_hypothesis": "系统推断",
}
PLAN_ACTION_ZH: Final[dict[str, str]] = {
    "notify": "通知",
    "no_notification": "不通知",
    "unresolved": "等待",
}
PLAN_REASON_ZH: Final[dict[str, str]] = {
    "uncovered_claims": "有命题值得通知",
    "awaiting": "等待进行中的发送或读者判断",
    "no_uncovered_actionable_claims": "没有值得通知的新命题",
    # editorial_v1 history
    "send_outcome_unresolved": "本事件仍有发送进行中",
}
CLAIM_DECISION_ZH: Final[dict[str, str]] = {"notify": "通知", "not_notified": "不通知", "deferred": "暂缓"}
CLAIM_REASON_ZH: Final[dict[str, str]] = {
    "retired": "已撤回的命题",
    "send_outcome_unresolved": "本事件仍有发送进行中，等待其结果",
    "send_outcome_ambiguous": "此前发送结果不明，按可能已送达处理，不重发",
    "stale_source": "来源已过时",
    "known_to_reader": "读者已收到同一事实",
    "linked_send_in_flight": "关联命题正在发送，等待其结果",
    "correction_of_sent": "更正此前已推送的内容",
    "protected_listing": "上币公告",
    "large_daily_move": "商品/指数当日大幅波动",
    "reader_key": "新增信息重要，标为重点",
    "reader_push": "新增信息值得推送",
    "reader_feed": "新增信息不足以打断，只进信息流",
    "reader_unavailable": "读者判断暂不可用，等待重试",
    "reader_unassessed": "读者判断长时间不可用，未评估，不推送",
}
# Reasons of `editorial_v1` decisions, shown read-only for the history the generative editor wrote.
LegacyClaimReason = Literal[
    "editor_notify", "editor_key", "editor_feed_only", "attention_unavailable_default_notify", "covered_by_sent_receipt"
]
LEGACY_CLAIM_REASON_ZH: Final[dict[str, str]] = {
    "editor_notify": "编辑判断值得通知（旧版）",
    "editor_key": "编辑判断为重点（旧版）",
    "editor_feed_only": "仅进入信息流（旧版）",
    "attention_unavailable_default_notify": "编辑判断暂不可用，按普通通知（旧版）",
    "covered_by_sent_receipt": "已送达内容已覆盖（旧版）",
    "protected_listing": "上币公告",
    "large_daily_move": "商品/指数日内大幅波动",
    "retired": "已撤回的命题",
    "stale_source": "来源已过时",
    "send_outcome_ambiguous": "此前发送结果不明，按可能已送达处理，不重发",
    "send_outcome_unresolved": "本事件仍有发送进行中，等待其结果",
}
NOVELTY_ZH: Final[dict[str, str]] = {
    "known": "已知",
    "increment": "增量",
    "development": "进展",
    "in_flight": "关联发送中",
    "unlinked": "未关联",
}
RENDER_ZH: Final[dict[str, str]] = {"full": "完整", "increment": "补充", "correction": "更正"}
SEMANTIC_STATE_ZH: Final[dict[str, str]] = {"pending": "处理中", "done": "已完成", "failed": "失败"}
NOTIFICATION_STATE_ZH: Final[dict[str, str]] = {"pending": "待决定", "done": "已决定", "failed": "通知失败"}
EXTRA_READ_STATE_ZH: Final[dict[str, str]] = {
    "reserved": "补读已预留",
    "attached": "补读材料已附加",
    "no_material": "补读无可用材料",
    "unavailable_or_budget_exhausted": "补读不可用或预算耗尽",
}
INTENT_STATE_ZH: Final[dict[str, str]] = {
    "queued": "待发送",
    "dead": "未送达（尝试耗尽）",
    "sending": "发送中",
    "sent": "已送达",
    "terminal": "未送达",
    "ambiguous": "发送结果不确定",
}


def _zh(table: Mapping[str, str], value: Any) -> str:
    text = str(value or "")
    return table.get(text, text)


def semantic_state(work: Mapping[str, Any]) -> str:
    """`failed` is the worker's own last outcome; pending is a wanted revision nothing has finished yet."""

    if str(work.get("last_outcome") or "") == "failed":
        return "failed"
    wanted = int(work.get("wanted_revision") or 0)
    done = int(work.get("done_revision") or 0)
    return "pending" if wanted > done else "done"


def claim_reasons_zh(decisions: Any) -> str:
    """The named reasons a plan did not notify, each once with its count when it repeats."""

    counts: dict[str, int] = {}
    for row in decisions if isinstance(decisions, Sequence) and not isinstance(decisions, str) else ():
        if isinstance(row, Mapping) and row.get("decision") != "notify":
            code = str(row.get("reason") or "")
            reason = CLAIM_REASON_ZH.get(code) or LEGACY_CLAIM_REASON_ZH.get(code) or code
            if reason:
                counts[reason] = counts.get(reason, 0) + 1
    return " · ".join(f"{reason} ×{n}" if n > 1 else reason for reason, n in counts.items())


def decode_update(document: Any) -> EventUpdate | None:
    try:
        return EventUpdate.model_validate(document)
    except ValidationError:
        return None


def decode_plan(plan: Any) -> NotificationPlan | None:
    if plan is None:
        return None
    try:
        return NotificationPlan.model_validate(plan)
    except ValidationError:
        return None


# Change kinds that name what a later revision is about. A restatement or a new source relationship
# leaves the Event's headline where it was.
_HEADLINE_CHANGE_KINDS = frozenset(
    {"new_fact", "parameter_change", "phase_change", "scope_change", "correction", "conflict", "possible_new"}
)


def headline_claim_statement(update: EventUpdate) -> str | None:
    """The claim an unsent Event is titled by: what its latest revision changed, else its lead claim.

    A later revision keeps its earlier claims (a parameter change supersedes, it does not retire), so the
    first listed claim would title a 25% -> 50% update with the 25% statement.
    """

    live = {claim.ref: claim.statement for claim in update.current_claims}
    if update.previous_content_revision is not None:
        for change in update.changes:
            if change.kind in _HEADLINE_CHANGE_KINDS and change.current_ref in live:
                return live[change.current_ref]
    return next(iter(live.values()), None)


def _source(evidence: Evidence) -> dict[str, Any]:
    source = evidence.source
    return {
        "publisher_id": source.publisher_id,
        "artifact_id": source.artifact_id,
        "origin_id": source.origin_id,
        "attribution": source.attribution,
        "published_at_ms": source.published_at_ms,
        "first_available_at_ms": source.first_available_at_ms,
        "url": source.url,
        "source_authority": source.source_authority,
        "source_authority_zh": source_authority_zh(source.source_authority),
    }


def _claim(
    claim: Claim,
    *,
    retired: bool,
    superseded: bool,
    evidence: Mapping[str, Evidence],
    relations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    fields = claim.fields
    counts: dict[str, int] = {}
    for row in relations:
        counts[row["relation"]] = counts.get(row["relation"], 0) + 1
    backed = counts.get("supports", 0) + counts.get("reports", 0)
    return {
        "ref": claim.ref,
        "statement": claim.statement,
        "retired": retired,
        "superseded": superseded,
        "subject": fields.subject,
        "action": fields.action,
        "object": fields.object,
        "speaker": fields.speaker,
        "conditions": list(fields.conditions),
        "quantities": [quantity.model_dump(mode="json") for quantity in fields.quantities],
        "effective_at": fields.effective_at,
        "occurred_at": fields.occurred_at,
        "statistical_period": fields.statistical_period,
        "polarity": fields.polarity,
        "polarity_zh": _zh(POLARITY_ZH, fields.polarity),
        "mode": fields.mode,
        "mode_zh": _zh(MODE_ZH, fields.mode),
        # `None` is a claim that is not an action; the contract never infers a phase from a date.
        "phase": fields.phase,
        "phase_zh": _zh(PHASE_ZH, fields.phase) if fields.phase is not None else "",
        "content_kind": fields.content_kind,
        "content_kind_zh": _zh(CONTENT_KIND_ZH, fields.content_kind),
        "assets": [asset.model_dump(mode="json") for asset in fields.assets],
        "citations": [
            {
                "evidence_ref": citation.evidence_ref,
                "quote": citation.quote,
                "source": _source(evidence[citation.evidence_ref]) if citation.evidence_ref in evidence else None,
            }
            for citation in claim.citations
        ],
        "first_available_at_ms": claim.first_available_at_ms,
        "antecedent_refs": list(claim.antecedent_refs),
        "relation_counts": {
            key: counts.get(key, 0) for key in ("supports", "refutes", "reports", "not_addressed", "unresolved")
        },
        # Sources disagree when one source refutes what another supports or reports. Copies of one origin
        # are still listed per source: the relation is per material, never a vote.
        "disputed": bool(counts.get("refutes")) and backed > 0,
    }


def event_update_view(
    head: Mapping[str, Any],
    *,
    previous_claims: Mapping[tuple[str, str], Mapping[str, Any]],
    sent_headline: str | None,
) -> dict[str, Any] | None:
    """The adopted head as the console reads it, or ``None`` when its document does not decode.

    ``previous_claims`` maps ``(update_ref, claim_ref)`` to ``{"statement", "event_id"}`` for the prior
    claims storage could still find; a change whose previous claim is not there keeps its refs and an
    unknown statement rather than a guessed one.
    """

    update = decode_update(head.get("document"))
    if update is None:
        return None
    evidence = {item.ref: item for item in update.evidence}
    retired = set(update.retired_claim_refs)
    by_claim: dict[str, list[dict[str, Any]]] = {}
    by_evidence: dict[str, list[dict[str, Any]]] = {}
    statements = {claim.ref: claim.statement for claim in update.claims}
    for relation in update.evidence_relations:
        row = {
            "claim_ref": relation.claim_ref,
            "evidence_ref": relation.evidence_ref,
            "relation": relation.relation,
            "relation_zh": _zh(EVIDENCE_RELATION_ZH, relation.relation),
        }
        by_claim.setdefault(relation.claim_ref, []).append(row)
        by_evidence.setdefault(relation.evidence_ref, []).append(row)
    claims = [
        _claim(
            claim,
            retired=claim.ref in retired,
            superseded=claim.ref in update.superseded_claim_refs,
            evidence=evidence,
            relations=by_claim.get(claim.ref, []),
        )
        for claim in update.claims
    ]
    changes = []
    for change in update.changes:
        previous = (
            previous_claims.get((change.previous_content_ref, change.previous_ref))
            if change.previous_ref is not None and change.previous_content_ref is not None
            else None
        )
        changes.append(
            {
                "kind": change.kind,
                "kind_zh": _zh(CHANGE_KIND_ZH, change.kind),
                "current_ref": change.current_ref,
                "current_statement": statements.get(change.current_ref, ""),
                "previous_ref": change.previous_ref,
                "previous_content_ref": change.previous_content_ref,
                "previous_statement": str(previous["statement"]) if previous else None,
                "previous_event_id": str(previous["event_id"]) if previous else None,
                "relation": change.relation,
                "relation_zh": _zh(CLAIM_RELATION_ZH, change.relation) if change.relation is not None else "",
            }
        )
    sources = [
        {
            "evidence_ref": item.ref,
            "text": item.text[:EVIDENCE_TEXT_MAX],
            "text_truncated": len(item.text) > EVIDENCE_TEXT_MAX,
            "source": _source(item),
            "relations": [
                row | {"claim_statement": statements.get(row["claim_ref"], "")} for row in by_evidence.get(item.ref, [])
            ],
        }
        for item in update.evidence
    ]
    claim_headline = headline_claim_statement(update)
    return {
        "update_ref": update.ref,
        "content_revision": update.content_revision,
        "input_revision": update.input_revision,
        "previous_content_revision": update.previous_content_revision,
        "adopted_at_ms": update.adopted_at_ms,
        "scope_repair_id": head.get("scope_repair_id"),
        "headline": sent_headline or claim_headline,
        "headline_source": "sent_card" if sent_headline else ("claim" if claim_headline else None),
        "topics": [{"code": code, "label_zh": IPTC_SUBJECT_LABELS_ZH.get(code, code)} for code in update.topics],
        "claims": claims,
        "retired_claim_refs": list(update.retired_claim_refs),
        "disputed_claim_refs": [claim["ref"] for claim in claims if claim["disputed"]],
        "changes": changes,
        "sources": sources,
        "implications": [
            {
                "claim_refs": list(implication.claim_refs),
                "channel": implication.channel,
                "explanation": implication.explanation,
                "conditions": list(implication.conditions),
                "origin": implication.origin,
                "origin_zh": _zh(IMPLICATION_ORIGIN_ZH, implication.origin),
            }
            for implication in update.implications
        ],
        "open_questions": [
            {"question": gap.question, "claim_refs": list(gap.claim_refs), "target_ref": gap.target_ref}
            for gap in update.open_questions
        ],
    }


def previous_content_refs(document: Any) -> list[str]:
    """The prior update refs a head's changes point at, for the one bounded lookup storage runs."""

    update = decode_update(document)
    if update is None:
        return []
    return sorted({change.previous_content_ref for change in update.changes if change.previous_content_ref})


def semantic_view(work: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if work is None:
        return None
    state = semantic_state(work)
    return {
        "state": state,
        "state_zh": SEMANTIC_STATE_ZH[state],
        "wanted_revision": int(work["wanted_revision"]),
        "done_revision": work.get("done_revision"),
        "attempts": int(work.get("attempts") or 0),
        "last_outcome": work.get("last_outcome"),
        "last_error_code": work.get("last_error_code"),
        "next_attempt_at_ms": work.get("next_attempt_at_ms"),
        "extra_read_state": work.get("extra_read_state"),
        "extra_read_state_zh": _zh(EXTRA_READ_STATE_ZH, work.get("extra_read_state")),
        "updated_at_ms": int(work["updated_at_ms"]),
    }


def _claim_reason_zh(row: ClaimDecision) -> str:
    """A templated reason: the rule, and for a reader row the score and the cut it was read against."""

    text = _zh(CLAIM_REASON_ZH, row.reason)
    judgment = None if row.reader is None else row.reader.judgment
    if judgment is not None and judgment.importance is not None and judgment.backend is not None:
        cuts = judgment.cuts
        text += f"（增量重要性 {judgment.importance.value:.2f}；推送 ≥ {cuts.push}，重点 ≥ {cuts.key}）"
    if row.reader is not None and row.reader.earlier is not None:
        text += f"；{_zh(RENDER_ZH, row.reader.render)}此前已推送的一条"
    return text


def plan_view(plan: NotificationPlan, *, statements: Mapping[str, str]) -> dict[str, Any]:
    return {
        "origin": "reader_v2",
        "action": plan.action,
        "action_zh": _zh(PLAN_ACTION_ZH, plan.action),
        "reason": plan.reason,
        "reason_zh": _zh(PLAN_REASON_ZH, plan.reason),
        "key": plan.key,
        "update_ref": plan.update_ref,
        "reader_revision": plan.reader_revision,
        "decision_ref": plan.record_ref,
        "reader_identity": plan.reader_identity,
        "claim_decisions": [
            {
                "claim_ref": row.claim_ref,
                # A decision about a claim the current head no longer carries keeps its ref and no text.
                "statement": statements.get(row.claim_ref),
                "decision": row.decision,
                "decision_zh": _zh(CLAIM_DECISION_ZH, row.decision),
                "reason": row.reason,
                "reason_zh": _claim_reason_zh(row),
                **_reader_fields(row),
            }
            for row in plan.claim_decisions
        ],
    }


def _reader_fields(row: ClaimDecision) -> dict[str, Any]:
    reader = row.reader
    if reader is None:
        return {}
    judgment = reader.judgment
    importance = None if judgment is None else judgment.importance
    return {
        "novelty": reader.novelty,
        "novelty_zh": _zh(NOVELTY_ZH, reader.novelty),
        "render": reader.render,
        "earlier_intent_id": None if reader.earlier is None else reader.earlier.intent_id,
        "importance": None if importance is None else round(importance.value, 2),
        "importance_probabilities": None if importance is None else [round(p, 3) for p in importance.probabilities],
        "reader_backend": None if judgment is None else judgment.backend,
    }


def legacy_plan_view(plan: Mapping[str, Any], *, statements: Mapping[str, str]) -> dict[str, Any] | None:
    """An `editorial_v1` decision, read-only: its action and each claim's named reason, never its free text."""

    rows = plan.get("claim_decisions")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        return None
    action = str(plan.get("action") or "")
    reason = str(plan.get("reason") or "")
    return {
        "origin": "editorial_v1",
        "action": action,
        "action_zh": _zh(PLAN_ACTION_ZH, action),
        "reason": reason,
        "reason_zh": _zh(PLAN_REASON_ZH, reason),
        "key": bool(plan.get("key")),
        "update_ref": str(plan.get("update_ref") or ""),
        "reader_revision": str(plan.get("reader_revision") or ""),
        "decision_ref": plan.get("decision_ref"),
        "reader_identity": None,
        "claim_decisions": [
            {
                "claim_ref": str(row.get("claim_ref") or ""),
                "statement": statements.get(str(row.get("claim_ref") or "")),
                "decision": str(row.get("decision") or ""),
                "decision_zh": _zh(CLAIM_DECISION_ZH, row.get("decision")),
                "reason": str(row.get("reason") or ""),
                "reason_zh": _zh(LEGACY_CLAIM_REASON_ZH, row.get("reason")),
            }
            for row in rows
        ],
    }


def notification_view(work: Mapping[str, Any] | None, *, statements: Mapping[str, str]) -> dict[str, Any] | None:
    if work is None:
        return None
    raw = work.get("plan")
    plan = decode_plan(raw) if work.get("origin") != "editorial_v1" else None
    view = (
        plan_view(plan, statements=statements)
        if plan is not None
        else legacy_plan_view(raw, statements=statements)
        if work.get("origin") == "editorial_v1" and isinstance(raw, Mapping)
        else None
    )
    state = str(work["state"])
    return {
        "state": state,
        "state_zh": _zh(NOTIFICATION_STATE_ZH, state),
        "content_revision": str(work["content_revision"]),
        "attempts": int(work.get("attempts") or 0),
        "last_error_code": work.get("last_error_code"),
        "next_attempt_at_ms": work.get("next_attempt_at_ms"),
        "updated_at_ms": int(work["updated_at_ms"]),
        "plan": view,
        "plan_error_code": PLAN_DECODE_ERROR if raw is not None and view is None else None,
    }


def intent_state(queue: Mapping[str, Any] | None, delivery: Mapping[str, Any] | None) -> str:
    """The ledger outranks the to-do list: a row in `news_deliveries` says what was actually attempted."""

    if delivery is not None:
        return str(delivery["state"])
    if queue is not None and queue.get("state") == "dead":
        return "dead"
    return "queued"


def intent_views(
    queue_rows: Sequence[Mapping[str, Any]],
    delivery_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Every `update` intent of one Event, from its queue row, its ledger row, or both."""

    queue = {str(row["intent_id"]): row for row in queue_rows}
    ledger = {str(row["intent_id"]): row for row in delivery_rows}
    views = []
    for intent_id in sorted(
        queue.keys() | ledger.keys(),
        key=lambda key: (
            int((queue.get(key) or {}).get("enqueued_at_ms") or (ledger.get(key) or {}).get("created_at_ms") or 0),
            key,
        ),
    ):
        q = queue.get(intent_id)
        d = ledger.get(intent_id)
        state = intent_state(q, d)
        source = d if d is not None else q or {}
        card = dict((d or {}).get("card") or {})
        views.append(
            {
                "intent_id": intent_id,
                "content_revision": source.get("content_revision"),
                "claim_refs": list(source.get("claim_refs") or []),
                "key": bool(source.get("plan_key")),
                "state": state,
                "state_zh": _zh(INTENT_STATE_ZH, state),
                "error_code": (d or {}).get("error_code") or (q or {}).get("error_code"),
                "attempts": int((q or {}).get("attempts") or 0) if q is not None else None,
                "enqueued_at_ms": (q or {}).get("enqueued_at_ms"),
                "attempted_at_ms": (d or {}).get("attempted_at_ms"),
                "settled_at_ms": (d or {}).get("settled_at_ms"),
                "headline_zh": str(card.get("headline_zh") or "").strip() or None,
                # The exact frozen text the channel received, and the channel's own receipt for it.
                "body": (d or {}).get("body"),
                "payload_sha256": (d or {}).get("payload_sha256"),
                "receipt": (d or {}).get("receipt"),
            }
        )
    return views


def sent_headline(intents: Sequence[Mapping[str, Any]]) -> str | None:
    """The card headline of the latest update intent a reader actually received."""

    sent = [row for row in intents if row["state"] == "sent" and row.get("headline_zh")]
    if not sent:
        return None
    return str(max(sent, key=lambda row: (int(row.get("settled_at_ms") or 0), row["intent_id"]))["headline_zh"])


__all__ = [
    "CHANGE_KIND_ZH",
    "CLAIM_DECISION_ZH",
    "CLAIM_REASON_ZH",
    "CLAIM_RELATION_ZH",
    "CONTENT_KIND_ZH",
    "EVIDENCE_RELATION_ZH",
    "IMPLICATION_ORIGIN_ZH",
    "INTENT_STATE_ZH",
    "LEGACY_CLAIM_REASON_ZH",
    "MODE_ZH",
    "NOVELTY_ZH",
    "PHASE_ZH",
    "PLAN_ACTION_ZH",
    "PLAN_DECODE_ERROR",
    "PLAN_REASON_ZH",
    "UPDATE_DECODE_ERROR",
    "LegacyClaimReason",
    "claim_reasons_zh",
    "decode_plan",
    "decode_update",
    "event_update_view",
    "headline_claim_statement",
    "intent_views",
    "notification_view",
    "previous_content_refs",
    "semantic_state",
    "semantic_view",
    "sent_headline",
]
