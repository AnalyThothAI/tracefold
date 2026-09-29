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
from .update_view import claim_reasons_zh, semantic_state

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
    "notification_failed",
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
    "notification_failed": "held",
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
    "news_notification_exhausted_legacy": "旧版通知规划已耗尽，未记录原因",
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
    """Project current work first; a historical receipt remains a separate delivery fact."""

    admission_text = str(admission or "")
    if admission_text == "recovery":
        return _outcome("held_recovery", "补抄件，不推送", ADMISSION_ZH["recovery"])
    if admission_text not in ADMITTED_ADMISSIONS:
        return _outcome("held_gate", "未送审", admission_zh(admission_text))

    if semantic is not None:
        wanted = int(semantic.get("wanted_revision") or 0)
        done = int(semantic.get("done_revision") or 0)
        if wanted > done:
            if semantic_state(semantic) == "failed":
                return _outcome(
                    "semantic_failed",
                    "语义处理失败",
                    error_code_zh(semantic.get("last_error_code")) or "语义处理失败，等待指定版本重试",
                )
            return _outcome("queued_semantic", "理解中", "等待语义处理新的材料版本")
    if not adopted:
        if (delivery or {}).get("state") == "sent":
            return _outcome("delivered", "已推送", "")
        return _outcome("no_update", "无可采用内容", "语义处理完成，未形成可采用的事件更新")

    work_state = str((notification or {}).get("state") or "")
    action = str((notification or {}).get("action") or "")
    target = str((notification or {}).get("content_revision") or "")
    queue_current = delivery_queue is not None and (not target or delivery_queue.get("content_revision") == target)
    delivery_current = delivery is not None and (not target or delivery.get("content_revision") == target)
    state = str((delivery or {}).get("state") or "")
    queue_state = str((delivery_queue or {}).get("state") or "")

    if work_state == "failed":
        error = (notification or {}).get("last_error_code")
        return _outcome(
            "notification_failed",
            "通知失败",
            f"{delivery_error_zh(error) or '未知错误'}；本版本不再自动重试，可重试指定版本",
        )
    if notification is None and state in {"sent", "ambiguous", "terminal"}:
        if queue_state == "pending":
            return _outcome("pending_delivery", "待推送", "仍有未完成的通知任务")
        if state == "sent":
            return _outcome("delivered", "已推送（重点）" if (delivery or {}).get("plan_key") else "已推送", "")
        if state == "ambiguous":
            return _outcome("delivery_ambiguous", "发送结果不确定", "发送后未能确认是否送达，不重发，等待对账")
        return _outcome("delivery_failed", "未送达", delivery_error_zh((delivery or {}).get("error_code")))
    if notification is None:
        if queue_state == "pending":
            return _outcome("pending_delivery", "待推送", "仍有未完成的通知任务")
        if queue_state == "dead":
            return _outcome("delivery_failed", "未送达", delivery_error_zh((delivery_queue or {}).get("error_code")))
        return _outcome("not_notified", "没有待执行通知", "当前没有未完成通知责任")
    if work_state == "pending":
        if action == "unresolved":
            return _outcome("notification_deferred", "等待前序发送", "本事件仍有发送进行中")
        if action == "notify":
            if delivery_current and state == "sending":
                return _outcome("pending_delivery", "推送中", "通知已获发送许可，等待实际结果")
            if queue_current and queue_state == "pending":
                if (delivery_queue or {}).get("frozen_card"):
                    return _outcome("pending_delivery", "待推送", "通知卡已冻结，等待发送")
                return _outcome("pending_delivery", "待准备通知卡", "已决定通知，等待生成通知卡")
            return _outcome("pending_delivery", "待准备通知卡", "已决定通知，等待生成通知卡")
        return _outcome("queued_notification", "待决定通知", "已采用事件更新，等待通知选择")

    if action == "no_notification":
        return _outcome(
            "not_notified",
            "未通知",
            claim_reasons_zh((notification or {}).get("claim_decisions")) or "没有未覆盖且可通知的命题",
        )
    if action == "notify" and queue_current and queue_state == "pending":
        return _outcome("pending_delivery", "待推送", "已决定通知，等待发送")
    if state == "sent":
        return _outcome("delivered", "已推送（重点）" if (delivery or {}).get("plan_key") else "已推送", "")
    if state == "ambiguous":
        return _outcome("delivery_ambiguous", "发送结果不确定", "发送后未能确认是否送达，不重发，等待对账")
    if state == "terminal" or (queue_current and queue_state == "dead"):
        error = (delivery or {}).get("error_code") or (delivery_queue or {}).get("error_code")
        return _outcome("delivery_failed", "未送达", delivery_error_zh(error))
    if state == "sending":
        return _outcome("pending_delivery", "推送中", "通知已获发送许可，等待实际结果")
    if action == "notify":
        return _outcome("delivery_failed", "通知状态异常", "已完成的通知责任缺少可核验的发送结果")
    return _outcome("not_notified", "未通知", "当前没有未完成通知责任")


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
