"""One human-readable outcome per Event, and the Chinese vocabulary for every rule/reason key (pure).

The console, ``/api/news/*`` and ``tracefold news why`` all read the same ``event_outcome()`` so an operator sees one
conclusion everywhere; the raw keys stay on the API for engineers, this module only *names* them. The vocabulary lives
next to the rules on purpose: a new ``decide()`` rule or error code lands here in the same change, so the console never
renders a bare key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from .events.storyline import NO_STORYLINE_KEY, storyline_entry
from .models import ADMITTED_ADMISSIONS
from .update_view import claim_reasons_zh, notification_state, semantic_state

OUTCOME_VERSION: Final = "news_outcome_v1"

OutcomeKind = Literal[
    "held_recovery",
    "held_gate",
    "pending_delivery",
    "delivered",
    "delivery_failed",
    "queued_semantic",
    "semantic_failed",
    "no_update",
    "queued_notification",
    "notification_deferred",
    "notification_exhausted",
    "not_notified",
    "delivery_ambiguous",
]

# Grouping the console uses for the task tabs: 已推送 / 被拦截 / 处理中. Kept here so CLI and HTTP agree.
OUTCOME_GROUP: Final[dict[str, str]] = {
    "held_recovery": "held",
    "held_gate": "held",
    "pending_delivery": "pending",
    "delivered": "pushed",
    "delivery_failed": "held",
    "queued_semantic": "pending",
    "semantic_failed": "held",
    "no_update": "held",
    "queued_notification": "pending",
    "notification_deferred": "pending",
    "notification_exhausted": "held",
    "not_notified": "held",
    "delivery_ambiguous": "held",
}


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: OutcomeKind
    text_zh: str
    reason_zh: str
    group: str  # pushed | held | pending

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text_zh": self.text_zh, "reason_zh": self.reason_zh, "group": self.group}


# ------------------------------------------------------------------------------------------------ vocabulary
ADMISSION_ZH: Final[dict[str, str]] = {
    "candidate": "已送审",
    "listing_deterministic": "上币/下币公告（自动送审）",
    "recovery": "断线期间补抄的旧闻，仅用于去重与历史",
}

ERROR_CODE_ZH: Final[dict[str, str]] = {
    "news_semantic_program_unconfigured": "未配置语义程序",
    "news_semantic_program_identity_mismatch": "语义程序身份校验失败",
    "news_program_route_deadline": "语义程序超时",
    "news_program_output_truncated": "语义程序输出被截断",
}

INCIDENT_CAUSE_ZH: Final[dict[str, str]] = {
    "planned_shutdown": "计划内重启",
    "network_connect": "网络连接失败",
    "authentication": "认证失败",
    "provider_close": "provider 关闭连接",
    "protocol_error": "协议错误",
    "idle_timeout": "长时间无帧",
    "broker_backpressure": "队列背压",
    "broker_unavailable": "队列不可用",
    "process_outage": "进程中断",
    "unknown": "未知原因",
}

DELIVERY_ERROR_ZH: Final[dict[str, str]] = {
    "delivery_unavailable": "推送未配置",
    "hourly_cap_reached": "已达每小时推送上限",
    "ambiguous_after_crash": "发送状态不确定（进程中断），不重发",
    "news_delivery_settlement_unavailable": "发送后未能记录结果",
    "news_delivery_attempts_exhausted": "投递尝试已耗尽，未送达",
}


def admission_zh(admission: str | None) -> str:
    return ADMISSION_ZH.get(str(admission or ""), str(admission or ""))


def error_code_zh(code: str | None) -> str:
    text = str(code or "")
    if not text:
        return ""
    if text in ERROR_CODE_ZH:
        return ERROR_CODE_ZH[text]
    return text


def incident_cause_zh(cause: str | None) -> str:
    return INCIDENT_CAUSE_ZH.get(str(cause or ""), str(cause or ""))


def delivery_error_zh(code: str | None) -> str:
    text = str(code or "")
    if not text:
        return ""
    if text in DELIVERY_ERROR_ZH:
        return DELIVERY_ERROR_ZH[text]
    if text.startswith("news_delivery_failed:"):
        return f"推送失败（{text.split(':', 1)[1]}）"
    return text


def storyline_key_zh(key: str | None) -> str:
    """The storyline registry owns every label but the symbol (#509 D4).

    There is no second table of storyline names here any more: `conflict:`/`actor:`/`geo:`/`topic:` read
    `label_zh` off the registry row, so a new storyline is one JSON row rather than a row plus a translation
    someone has to remember. A historical key whose entry has since been renamed away renders as itself."""

    text = str(key or "")
    if text.startswith("asset:"):
        return text.removeprefix("asset:")
    if text == NO_STORYLINE_KEY:
        return "无线索"
    prefix, separator, entry_id = text.partition(":")
    if separator and prefix in {"conflict", "actor", "geo", "topic"}:
        entry = storyline_entry(entry_id)
        if entry is not None:
            return entry.label_zh
    return text


# ------------------------------------------------------------------------------------------------ outcome
def event_outcome(
    *,
    admission: str | None,
    delivery: Mapping[str, Any] | None,
    delivery_queue: Mapping[str, Any] | None = None,
    semantic: Mapping[str, Any] | None = None,
    adopted: bool = False,
    notification: Mapping[str, Any] | None = None,
) -> Outcome:
    """Resolve durable reader receipts first, then current semantic and notification work."""
    state = str((delivery or {}).get("state") or "")
    queue_state = str((delivery_queue or {}).get("state") or "")
    if state == "sent":
        return _outcome("delivered", "已推送（重点）" if (delivery or {}).get("plan_key") else "已推送", "")
    if state == "sending":
        return _outcome("pending_delivery", "推送中", "")
    if state in {"terminal", "ambiguous"}:
        if queue_state == "pending" and semantic is not None:
            return _outcome("pending_delivery", "待推送", "上一张卡未送达，新的通知待发送")
        if state == "ambiguous":
            return _outcome("delivery_ambiguous", "发送结果不确定", "发送后未能确认是否送达，不重发，等待对账")
        return _outcome("delivery_failed", "未送达", delivery_error_zh((delivery or {}).get("error_code")))
    if queue_state == "dead":
        return _outcome("delivery_failed", "未送达", delivery_error_zh((delivery_queue or {}).get("error_code")))
    admission_text = str(admission or "")
    if admission_text == "recovery":
        return _outcome("held_recovery", "补抄件，不推送", ADMISSION_ZH["recovery"])
    if admission_text not in ADMITTED_ADMISSIONS:
        return _outcome("held_gate", "未送审", admission_zh(admission_text))
    if semantic is not None:
        return _update_outcome(semantic, adopted=adopted, notification=notification)
    return _outcome("no_update", "仅有来源", "没有当前语义工作记录")


def _update_outcome(semantic: Mapping[str, Any], *, adopted: bool, notification: Mapping[str, Any] | None) -> Outcome:
    """The EventUpdate path after the ledger and the Gate: semantic work, the head, then the plan."""

    semantic_now = semantic_state(semantic)
    if semantic_now == "pending":
        return _outcome("queued_semantic", "理解中", "等待语义处理新的材料版本")
    if not adopted:
        if semantic_now == "failed":
            return _outcome(
                "semantic_failed",
                "语义处理失败",
                error_code_zh(semantic.get("last_error_code")) or "语义处理多次失败，等待新的材料版本",
            )
        return _outcome("no_update", "无可采用内容", "语义处理完成，未形成可采用的事件更新")
    plan_state = notification_state(notification or {})
    if plan_state == "exhausted":
        return _outcome("notification_exhausted", "通知规划已耗尽", "本版本不再自动规划，可重试指定版本或等待新事实")
    action = str((notification or {}).get("action") or "")
    if action == "unresolved":
        return _outcome("notification_deferred", "等待前序发送", "重叠的通知发送结果尚未确定")
    if notification is None or plan_state == "pending":
        return _outcome("queued_notification", "待决定通知", "已采用事件更新，等待通知选择")
    if action == "notify":
        return _outcome("pending_delivery", "待推送", "通知已选择，等待发送")
    return _outcome(
        "not_notified",
        "未通知",
        claim_reasons_zh((notification or {}).get("claim_decisions")) or "没有未覆盖且可通知的命题",
    )


def _outcome(kind: OutcomeKind, text_zh: str, reason_zh: str) -> Outcome:
    return Outcome(kind=kind, text_zh=text_zh, reason_zh=reason_zh, group=OUTCOME_GROUP[kind])


__all__ = [
    "ADMISSION_ZH",
    "DELIVERY_ERROR_ZH",
    "ERROR_CODE_ZH",
    "INCIDENT_CAUSE_ZH",
    "OUTCOME_GROUP",
    "OUTCOME_VERSION",
    "Outcome",
    "OutcomeKind",
    "admission_zh",
    "delivery_error_zh",
    "error_code_zh",
    "event_outcome",
    "incident_cause_zh",
    "storyline_key_zh",
]
