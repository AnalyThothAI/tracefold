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
from .models import ADMITTED_ADMISSIONS, OUTBOX_MAX_AGE_MS
from .triage_rules import STALE_SOURCE_KEY

OUTCOME_VERSION: Final = "news_outcome_v1"

OutcomeKind = Literal[
    "held_recovery",
    "held_gate",
    "expired_triage_handoff",
    "expired_delivery_handoff",
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
    "expired_triage_handoff": "held",
    "expired_delivery_handoff": "held",
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
    "telemetry_deterministic": "持仓异动遥测（规则判断，不过模型）",
    "liquidation_deterministic": "强平遥测（规则解析，不过模型）",
    "suppressed_pr_template": "律所推广模板，规则直接拦截",
    "suppressed_low_signal": "低分社媒/盘口噪音，规则直接拦截",
    "unsupported_market_contract": "市场/钱包数据合同未支持，安全落库但不送审",
    "recovery": "断线期间补抄的旧闻，仅用于去重与历史",
}

OVERRIDE_RULE_ZH: Final[dict[str, str]] = {
    "degraded_listing_objective": "模型不可用，上币/下币客观规则兜底推送",
    "degraded_watchlist_objective": "模型不可用，关注列表客观规则兜底推送",
    "degraded_no_objective_guard": "模型不可用且未命中客观推送条件",
    "listing_deterministic": "上币/下币公告按客观规则推送",
    "stored": "持仓异动帧已解析入账，推送由 Signal 通道负责",
    "liquidation_fact_only": "已发生强平事实，不推断后续方向",
    "oi_parse_failed": "持仓异动供应商格式无法解析，已安全拦截",
    "liquidation_parse_failed": "强平供应商格式无法解析，已安全拦截",
    "watchlist_objective_guard": "命中关注列表客观条件",
    "trade_relevance_escalate": "交易相关性达到重点推送标准",
    "trade_relevance_escalate_uncorroborated": "达到重点标准但来源权威未知且仅单条来源，降为普通推送",
    "trade_relevance_realtime": "交易相关性达到实时推送标准",
    "single_name_without_instrument": "单一标的事实未给出可交易标的，不推送",
    "trade_relevance_inconsistent": "交易相关性字段不一致，未达推送标准",
    "reader_value_background": "仅有背景价值，不实时推送",
    "reader_value_none": "无读者价值，不推送",
    "stale_source_artifact": "来源推文本身已过时，按旧闻扣下",
    "restatement": "重复：读者已收到同一事实",
}

ERROR_CODE_ZH: Final[dict[str, str]] = {
    "oi_parse_failed": "持仓异动供应商格式无法解析",
    "liquidation_parse_failed": "强平供应商格式无法解析",
    "news_triage_circuit_open": "模型熔断中（连续失败后暂停调用）",
    "news_semantic_program_unconfigured": "未配置语义程序",
    "news_semantic_program_identity_mismatch": "语义程序身份校验失败",
    "news_canary_artifact_missing": "候选语义程序制品缺失",
    "news_canary_assignment_identity_invalid": "候选分配身份校验失败",
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
    "delivery_unavailable": "推送未配置",
    "hourly_cap_reached": "已达每小时推送上限",
    "ambiguous_after_crash": "发送状态不确定（进程中断），不重发",
    "news_delivery_settlement_unavailable": "发送后未能记录结果",
}

DEDUPE_FAMILY_ZH: Final[dict[str, str]] = {
    "general": "综合",
    "filing": "公告/申报",
    "market_telemetry": "盘口数据",
    "disaster": "灾害",
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

_SEEN_SUFFIX: Final = ":seen"
# #504 D2: the per-storyline budget withhold, `storyline:<key>:budget`.
_BUDGET_SUFFIX: Final = ":budget"
# #154. Constant rather than per-age so the top-10 `throttled_by_key` map keeps one bucket for the rule.
_STALE_ARTIFACT_KEY: Final = STALE_SOURCE_KEY


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


def throttled_by_zh(key: str | None) -> str:
    text = str(key or "")
    if not text:
        return ""
    if text == _STALE_ARTIFACT_KEY:
        return "旧闻：这条推文在 provider 推送时就已过时"
    if text.endswith(_SEEN_SUFFIX):
        return "重复：读者刚收到过内容高度相近的卡片"
    if text.endswith(_BUDGET_SUFFIX):
        return "同线索预算：过去一小时该线索已推送达到上限，且本条并非方向反转"
    return text


def direction_zh(value: str | None) -> str:
    return DIRECTION_ZH.get(str(value or ""), str(value or ""))


def magnitude_zh(value: int | None) -> str:
    try:
        return MAGNITUDE_ZH.get(int(value), str(value)) if value is not None else ""
    except (TypeError, ValueError):
        return str(value)


def scope_zh(value: str | None) -> str:
    return SCOPE_ZH.get(str(value or ""), str(value or ""))


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
    opened_at_ms: int | None = None,
    now_ms: int | None = None,
) -> Outcome:
    """The one place that turns admission + latest triage verdict + first-card delivery into a conclusion.

    ``triage`` needs ``final_decision``, ``override_rule``, ``throttled_by``, ``degraded``, ``error_code``,
    ``created_at_ms`` and ``published_at_ms``;
    ``delivery`` needs ``state`` and ``error_code``. Missing rows are ``None``.
    """

    state = str((delivery or {}).get("state") or "")
    if state in {"sent", "terminal", "sending"}:
        # The outbound ledger is a material fact and outranks a later routing hard cut. This also covers
        # repaired/replayed data whose immutable verdict is absent.
        final = str((triage or {}).get("final_decision") or "")
        degraded = bool((triage or {}).get("degraded"))
        error_zh = error_code_zh((triage or {}).get("error_code")) if degraded else ""
        rule_zh = override_rule_zh((triage or {}).get("override_rule"))
        if degraded:
            rule_zh = "模型不可用，按规则兜底推送" + (f"：{error_zh}" if error_zh else "")
        if state == "sent":
            return _outcome("delivered", "已推送（重点）" if final == "escalate" else "已推送", rule_zh)
        if state == "terminal":
            return _outcome("delivery_failed", "未送达", delivery_error_zh((delivery or {}).get("error_code")))
        return _outcome("pending_delivery", "推送中", rule_zh)

    admission_text = str(admission or "")
    if admission_text == "recovery":
        return _outcome("held_recovery", "补抄件，不推送", ADMISSION_ZH["recovery"])
    if admission_text not in ADMITTED_ADMISSIONS:
        return _outcome("held_gate", "未送审", admission_zh(admission_text))
    if triage is None:
        if published_at_ms is None:
            if _handoff_expired(started_at_ms=opened_at_ms, now_ms=now_ms):
                return _outcome(
                    "expired_triage_handoff",
                    "未送审（交接过期）",
                    "入库后 30 分钟内未完成送审交接，已停止补发",
                )
            return _outcome("queued_publish", "待处理", "已入库，等待送审")
        return _outcome("queued_triage", "审稿中", "排队等待模型判断")

    final = str(triage.get("final_decision") or "")
    degraded = bool(triage.get("degraded"))
    error_zh = error_code_zh(triage.get("error_code")) if degraded else ""
    if final == "throttled":
        throttled_by = str(triage.get("throttled_by") or "")
        if throttled_by.endswith(_SEEN_SUFFIX):
            text = "未推送（重复）"
        elif throttled_by.endswith(_BUDGET_SUFFIX):
            text = "未推送（同线索预算）"
        elif throttled_by == _STALE_ARTIFACT_KEY:
            text = "未推送（旧闻）"
        else:
            text = "未推送（历史限流）"
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
    if (
        delivery is None
        and triage.get("published_at_ms") is None
        and _handoff_expired(started_at_ms=triage.get("created_at_ms"), now_ms=now_ms)
    ):
        return _outcome(
            "expired_delivery_handoff",
            "未推送（交接过期）",
            "判定后 30 分钟内未完成投递交接，已停止补发",
        )
    return _outcome("pending_delivery", "待推送（重点）" if important else "待推送", rule_zh)


def _handoff_expired(*, started_at_ms: Any, now_ms: int | None) -> bool:
    if started_at_ms is None or now_ms is None:
        return False
    try:
        return int(now_ms) - int(started_at_ms) > OUTBOX_MAX_AGE_MS
    except (TypeError, ValueError):
        return False


def _outcome(kind: OutcomeKind, text_zh: str, reason_zh: str) -> Outcome:
    return Outcome(kind=kind, text_zh=text_zh, reason_zh=reason_zh, group=OUTCOME_GROUP[kind])


__all__ = [
    "ADMISSION_ZH",
    "AUDIENCE_ZH",
    "DECISION_ZH",
    "DEDUPE_FAMILY_ZH",
    "DELIVERY_ERROR_ZH",
    "DIRECTION_ZH",
    "ERROR_CODE_ZH",
    "INCIDENT_CAUSE_ZH",
    "MAGNITUDE_ZH",
    "NOVELTY_ZH",
    "OUTCOME_GROUP",
    "OUTCOME_VERSION",
    "OVERRIDE_RULE_ZH",
    "PRIORITY_ZH",
    "SCOPE_ZH",
    "Outcome",
    "OutcomeKind",
    "admission_zh",
    "audience_zh",
    "decision_zh",
    "delivery_error_zh",
    "direction_zh",
    "error_code_zh",
    "event_outcome",
    "incident_cause_zh",
    "magnitude_zh",
    "novelty_zh",
    "override_rule_zh",
    "scope_zh",
    "storyline_key_zh",
    "throttled_by_zh",
]
