"""One human-readable outcome per Event, and the Chinese vocabulary for every rule/reason key (pure).

The console, ``/api/news/*`` and ``tracefold news why`` all read the same ``event_outcome()`` so an operator sees one
conclusion everywhere; the raw keys stay on the API for engineers, this module only *names* them. The vocabulary lives
next to the rules on purpose: a new ``decide()`` rule or error code lands here in the same change, so the console never
renders a bare key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from .models import ADMITTED_ADMISSIONS

OUTCOME_VERSION: Final = "news_outcome_v1"

OutcomeKind = Literal[
    "held_recovery",
    "held_gate",
    "queued_publish",
    "queued_triage",
    "dropped",
    "throttled",
    "degraded_dropped",
    "pending_delivery",
    "delivered",
    "delivery_failed",
]

# Grouping the console uses for the task tabs: 已推送 / 被拦截 / 处理中. Kept here so CLI and HTTP agree.
OUTCOME_GROUP: Final[dict[str, str]] = {
    "held_recovery": "held",
    "held_gate": "held",
    "queued_publish": "pending",
    "queued_triage": "pending",
    "dropped": "held",
    "throttled": "held",
    "degraded_dropped": "held",
    "pending_delivery": "pending",
    "delivered": "pushed",
    "delivery_failed": "held",
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
    "suppressed_pr_template": "律所推广模板，规则直接拦截",
    "suppressed_low_signal": "低分社媒/盘口噪音，规则直接拦截",
    "recovery": "断线期间补抄的旧闻，仅用于去重与历史",
    # Retired Gate admissions (pre-#53) still present on historical Events.
    "suppressed_ungrounded": "无关联资产，旧规则拦截（已退役）",
    "suppressed_ungrounded_meme": "无关联资产的社媒，旧规则拦截（已退役）",
}

OVERRIDE_RULE_ZH: Final[dict[str, str]] = {
    "noise": "模型判定为噪音",
    "unclear_direction": "方向不明，未达推送标准",
    "below_threshold": "影响不够，未达推送标准",
    "muted": "已被静音，或推送处于暂停",
    "model_push_actionable": "模型判断值得推送",
    "unclear_but_clear_event": "方向不明但事件明确",
    "watchlist": "命中关注列表",
    "magnitude3": "重大事件",
    "high_priority_push": "高优先级来源，模型建议推送",
    "fail_closed_fallback": "模型不可用，按规则兜底",
    "restatement": "重复：读者已收到同一事实",
    "distinct_bypass": "与读者刚收到的卡片都不同，放行",
    # Retired rules (policy v1-v4 / Analyst lane) still present on historical verdicts.
    "novel_bypass": "新进展放行：模型自报是新事实（旧规则）",
    "magnitude2_actionable": "影响明显且可操作（旧规则）",
    "verify_failed": "分析结果未通过校验（已退役）",
}

ERROR_CODE_ZH: Final[dict[str, str]] = {
    "news_triage_timeout": "模型超时",
    "news_triage_output_truncated": "模型输出被截断",
    "news_triage_output_invalid": "模型输出格式错误",
    "news_triage_circuit_open": "模型熔断中（连续失败后暂停调用）",
    "news_triage_model_unconfigured": "未配置模型",
    "news_semantic_program_unconfigured": "未配置语义程序",
    "news_semantic_program_identity_mismatch": "语义程序身份校验失败",
    "news_canary_artifact_missing": "候选语义程序制品缺失",
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
    "triage_circuit_open": "模型熔断",
    "unknown": "未知原因",
}

DELIVERY_ERROR_ZH: Final[dict[str, str]] = {
    "delivery_paused": "推送已暂停",
    "delivery_unavailable": "推送未配置",
    "hourly_cap_reached": "已达每小时推送上限",
    "ambiguous_after_crash": "发送状态不确定（进程中断），不重发",
    "news_delivery_settlement_unavailable": "发送后未能记录结果",
}

THEME_ZH: Final[dict[str, str]] = {
    "crypto_treasury": "加密财库",
    "mideast_energy": "中东与能源",
    "rates": "利率与央行",
    "trade": "贸易与关税",
    "china_macro": "中国宏观",
    "metals": "金属",
    "us_equity_macro": "美股大盘",
    "us_macro_data": "美国宏观数据",
}

FAMILY_ZH: Final[dict[str, str]] = {
    "general": "综合",
    "filing": "公告/申报",
    "market_telemetry": "盘口数据",
    "disaster": "灾害",
}

EVENT_TYPE_ZH: Final[dict[str, str]] = {
    "listing": "上币",
    "delisting": "下币",
    "filing": "申报/公告",
    "regulation": "监管",
    "hack": "黑客攻击",
    "exploit": "漏洞利用",
    "partnership": "合作",
    "funding": "融资",
    "macro": "宏观",
    "rates": "利率",
    "oi_spike": "持仓异动",
    "liquidation": "清算",
    "whale": "巨鲸动向",
    "earnings": "财报",
    "product": "产品",
    "rumor": "传闻",
    "noise": "噪音",
}

DIRECTION_ZH: Final[dict[str, str]] = {
    "bullish": "利多",
    "bearish": "利空",
    "neutral": "中性",
    "unclear": "方向待定",
}
MAGNITUDE_ZH: Final[dict[int, str]] = {0: "影响很小", 1: "影响有限", 2: "影响明显", 3: "影响重大"}
SCOPE_ZH: Final[dict[str, str]] = {"macro": "宏观", "sector": "板块", "single_name": "个别标的"}
# The model's novelty judgment against the told ledger (issue #61) and the reader group the card is for.
NOVELTY_ZH: Final[dict[str, str]] = {"new_fact": "新事实", "progression": "新进展", "restatement": "复述"}
AUDIENCE_ZH: Final[dict[str, str]] = {"crypto": "加密", "us_equity": "美股", "macro": "宏观", "none": "无"}
PRIORITY_ZH: Final[dict[str, str]] = {"high": "高优先级", "normal": "普通"}
DECISION_ZH: Final[dict[str, str]] = {
    "push": "推送",
    "escalate": "重点推送",
    "drop": "不推",
    "throttled": "限流",
    "degraded": "降级",
}

# Policy v7 only writes `:seen`; cap/hard shapes remain here so historical
# verdicts stay intelligible after the hard cut.
_SEEN_SUFFIX: Final = ":seen"
_THROTTLE_ASSET_RE = re.compile(r"^storyline:asset:(?P<symbol>[^:]+)(?::hard(?P<hard>\d+))?$")
_THROTTLE_THEME_RE = re.compile(r"^storyline:theme:(?P<theme>[^:]+)(?::(?:cap(?P<cap>\d+)|hard(?P<hard>\d+)))?$")
_THROTTLE_FAMILY_RE = re.compile(r"^storyline:macro:(?P<family>[^:]+)(?::(?:cap(?P<cap>\d+)|hard(?P<hard>\d+)))?$")


def admission_zh(admission: str | None) -> str:
    return ADMISSION_ZH.get(str(admission or ""), str(admission or ""))


def override_rule_zh(rule: str | None) -> str:
    return OVERRIDE_RULE_ZH.get(str(rule or ""), str(rule or ""))


def error_code_zh(code: str | None) -> str:
    text = str(code or "")
    if not text:
        return ""
    if text in ERROR_CODE_ZH:
        return ERROR_CODE_ZH[text]
    if text.startswith("news_triage_model_failed:"):
        return f"模型调用失败（{text.split(':', 1)[1]}）"
    if text.startswith("news_program_transport_"):
        return f"语义程序调用失败（{text.removeprefix('news_program_transport_')}）"
    if text.startswith("news_program_dspy_output_") or (text.startswith("news_program_") and text.endswith("_invalid")):
        return "语义程序输出格式错误"
    if text.startswith("news_analyst_"):
        return "分析模型失败（已退役通道）"
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
        return f"飞书发送失败（{text.split(':', 1)[1]}）"
    return text


def storyline_key_zh(key: str | None) -> str:
    text = str(key or "")
    if text.startswith("asset:"):
        return text.removeprefix("asset:")
    if text.startswith("theme:"):
        theme = text.removeprefix("theme:")
        return THEME_ZH.get(theme, theme)
    if text.startswith("macro:"):
        family = text.removeprefix("macro:")
        return FAMILY_ZH.get(family, family)
    return text


def throttled_by_zh(key: str | None) -> str:
    text = str(key or "")
    if not text:
        return ""
    if text == "hourly_cap":
        return "已达每小时推送上限"
    if text.endswith(_SEEN_SUFFIX):
        return "重复：读者刚收到过内容高度相近的卡片"
    if (m := _THROTTLE_ASSET_RE.match(text)) is not None:
        if m.group("hard"):
            return f"{m.group('symbol')} 2 小时内已推 {m.group('hard')} 条，达到防洪上限"
        return f"{m.group('symbol')} 同一话题在节流窗口内已推过同等或更重要的消息"
    if (m := _THROTTLE_THEME_RE.match(text)) is not None:
        theme = THEME_ZH.get(m.group("theme"), m.group("theme"))
        return _window_cap_zh(f"「{theme}」话题", cap=m.group("cap"), hard=m.group("hard"))
    if (m := _THROTTLE_FAMILY_RE.match(text)) is not None:
        family = FAMILY_ZH.get(m.group("family"), m.group("family"))
        return _window_cap_zh(f"「{family}」类", cap=m.group("cap"), hard=m.group("hard"))
    return text


def _window_cap_zh(subject: str, *, cap: str | None, hard: str | None) -> str:
    if hard:
        return f"{subject} 4 小时内已推 {hard} 条，达到防洪上限"
    if cap:
        return f"{subject} 4 小时内已推 {cap} 条"
    return f"{subject} 4 小时内已推过更重要的消息"


def event_type_zh(value: str | None) -> str:
    return EVENT_TYPE_ZH.get(str(value or ""), str(value or ""))


def direction_zh(value: str | None) -> str:
    return DIRECTION_ZH.get(str(value or ""), str(value or ""))


def magnitude_zh(value: int | None) -> str:
    try:
        return MAGNITUDE_ZH.get(int(value), str(value)) if value is not None else ""
    except (TypeError, ValueError):
        return str(value)


def scope_zh(value: str | None) -> str:
    return SCOPE_ZH.get(str(value or ""), str(value or ""))


def family_zh(value: str | None) -> str:
    return FAMILY_ZH.get(str(value or ""), str(value or ""))


def decision_zh(value: str | None) -> str:
    return DECISION_ZH.get(str(value or ""), str(value or ""))


def novelty_zh(value: str | None) -> str:
    return NOVELTY_ZH.get(str(value or ""), str(value or ""))


def audience_zh(value: str | None) -> str:
    return AUDIENCE_ZH.get(str(value or ""), str(value or ""))


# ------------------------------------------------------------------------------------------------ outcome
_HELD_DECISIONS: Final = frozenset({"drop", "throttled", "degraded"})
_PUSH_DECISIONS: Final = frozenset({"push", "escalate"})


def event_outcome(
    *,
    admission: str | None,
    published_at_ms: int | None,
    triage: Mapping[str, Any] | None,
    delivery: Mapping[str, Any] | None,
) -> Outcome:
    """The one place that turns admission + latest triage verdict + first-card delivery into a conclusion.

    ``triage`` needs ``final_decision``, ``override_rule``, ``throttled_by``, ``degraded``, ``error_code``;
    ``delivery`` needs ``state`` and ``error_code``. Missing rows are ``None``.
    """

    admission_text = str(admission or "")
    if admission_text == "recovery":
        return _outcome("held_recovery", "补抄件，不推送", ADMISSION_ZH["recovery"])
    if admission_text not in ADMITTED_ADMISSIONS:
        return _outcome("held_gate", "未送审", admission_zh(admission_text))
    state = str((delivery or {}).get("state") or "")
    if state == "sent" and triage is None:
        # A sent card is a fact even when the verdict row is missing (replayed or repaired data): never call it queued.
        return _outcome("delivered", "已推送", "")
    if triage is None:
        if published_at_ms is None:
            return _outcome("queued_publish", "待处理", "已入库，等待送审")
        return _outcome("queued_triage", "审稿中", "排队等待模型判断")

    final = str(triage.get("final_decision") or "")
    degraded = bool(triage.get("degraded"))
    error_zh = error_code_zh(triage.get("error_code")) if degraded else ""
    if final == "throttled":
        throttled_by = str(triage.get("throttled_by") or "")
        text = "未推送（重复）" if throttled_by.endswith(_SEEN_SUFFIX) else "未推送（历史限流）"
        return _outcome("throttled", text, throttled_by_zh(throttled_by))
    if final in _HELD_DECISIONS:
        if degraded:
            reason = "模型不可用，按规则兜底不推" + (f"：{error_zh}" if error_zh else "")
            return _outcome("degraded_dropped", "未推送（规则兜底）", reason)
        return _outcome("dropped", "未推送", override_rule_zh(triage.get("override_rule")))
    if final not in _PUSH_DECISIONS:
        return _outcome("dropped", "未推送", override_rule_zh(triage.get("override_rule")) or final)

    important = final == "escalate"
    rule_zh = override_rule_zh(triage.get("override_rule"))
    if degraded:
        rule_zh = "模型不可用，按规则兜底推送" + (f"：{error_zh}" if error_zh else "")
    if state == "sent":
        return _outcome("delivered", "已推送（重点）" if important else "已推送", rule_zh)
    if state == "terminal":
        return _outcome("delivery_failed", "未送达", delivery_error_zh((delivery or {}).get("error_code")))
    if state == "sending":
        return _outcome("pending_delivery", "推送中", rule_zh)
    return _outcome("pending_delivery", "待推送（重点）" if important else "待推送", rule_zh)


def _outcome(kind: OutcomeKind, text_zh: str, reason_zh: str) -> Outcome:
    return Outcome(kind=kind, text_zh=text_zh, reason_zh=reason_zh, group=OUTCOME_GROUP[kind])


__all__ = [
    "ADMISSION_ZH",
    "AUDIENCE_ZH",
    "DECISION_ZH",
    "DELIVERY_ERROR_ZH",
    "DIRECTION_ZH",
    "ERROR_CODE_ZH",
    "EVENT_TYPE_ZH",
    "FAMILY_ZH",
    "INCIDENT_CAUSE_ZH",
    "MAGNITUDE_ZH",
    "NOVELTY_ZH",
    "OUTCOME_GROUP",
    "OUTCOME_VERSION",
    "OVERRIDE_RULE_ZH",
    "PRIORITY_ZH",
    "SCOPE_ZH",
    "THEME_ZH",
    "Outcome",
    "OutcomeKind",
    "admission_zh",
    "audience_zh",
    "decision_zh",
    "delivery_error_zh",
    "direction_zh",
    "error_code_zh",
    "event_outcome",
    "event_type_zh",
    "family_zh",
    "incident_cause_zh",
    "magnitude_zh",
    "novelty_zh",
    "override_rule_zh",
    "scope_zh",
    "storyline_key_zh",
    "throttled_by_zh",
]
