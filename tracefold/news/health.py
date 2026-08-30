"""Status health: thresholded red/amber/green over the status snapshot, plus the 24 h funnel and named reasons (pure).

The console renders ``level`` and ``summary_zh`` verbatim; thresholds live here (code-owned) so the page never has to
know what "too many" means. Inputs are the plain dicts from News storage's ``status_snapshot`` and the HTTP route
already assembles; nothing here touches I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from .outcome import admission_zh, error_code_zh, incident_cause_zh, override_rule_zh, throttled_by_zh

HEALTH_VERSION: Final = "news_health_v1"

Level = Literal["ok", "warn", "bad", "off"]

# Thresholds (code-owned). Documented in docs/OPERATIONS.md.
FRAME_STALE_WARN_MS: Final = 10 * 60_000
FRAME_STALE_BAD_MS: Final = 30 * 60_000
QUEUE_DEPTH_WARN: Final = 50
QUEUE_DEPTH_BAD: Final = 200
DEGRADED_SHARE_WARN: Final = 0.03
DEGRADED_SHARE_BAD: Final = 0.10
DELIVERY_FAIL_SHARE_WARN: Final = 0.10
DELIVERY_FAIL_SHARE_BAD: Final = 0.30
_BUSINESS_QUEUES: Final = ("news.raw", "news.triage", "news.deliver")
_NON_INGEST_INCIDENTS: Final = frozenset({"triage_circuit_open", "broker_backpressure", "broker_unavailable"})

_LEVEL_ORDER: Final = {"ok": 0, "off": 0, "warn": 1, "bad": 2}


@dataclass(frozen=True, slots=True)
class HealthItem:
    level: Level
    summary_zh: str
    detail_zh: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "summary_zh": self.summary_zh, "detail_zh": self.detail_zh}


def _worst(*levels: Level) -> Level:
    return max(levels, key=lambda level: _LEVEL_ORDER[level]) if levels else "ok"


def _minutes(ms: int) -> str:
    minutes = max(0, int(ms // 60_000))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def _pct(share: float) -> str:
    return f"{share * 100:.0f}%" if share >= 0.1 else f"{share * 100:.1f}%"


# ------------------------------------------------------------------------------------------------ items
def ingest_health(ingest: Mapping[str, Any], *, now_ms: int, workers_state: str | None, enabled: bool) -> HealthItem:
    if not enabled or not ingest.get("token_configured", True):
        return HealthItem("off", "接入未配置", "news.enabled 或 OpenNews token 未设置")
    if workers_state == "recovering":
        return HealthItem("warn", "Workers 启动/恢复中", "等待心跳进入 running")
    if workers_state and workers_state != "running":
        return HealthItem("bad", "Workers 未运行", f"workers_state={workers_state}")
    # Model and broker incidents are reported by their own health items; the ingest item only owns the WSS lane.
    incidents = [
        i
        for i in (ingest.get("open_incidents") or [])
        if not i.get("planned") and str(i.get("cause_class") or "") not in _NON_INGEST_INCIDENTS
    ]
    last_frame = ingest.get("last_frame_at_ms")
    age = int(now_ms) - int(last_frame) if last_frame else None
    if not ingest.get("connected"):
        detail = f"最近一帧 {_minutes(age)} 前" if age is not None else "尚未收到任何帧"
        return HealthItem("bad", "OpenNews 未连接", detail)
    if age is not None and age >= FRAME_STALE_BAD_MS:
        return HealthItem("bad", f"已连接，但 {_minutes(age)} 没有新帧", "检查 provider 策略是否仍在触发")
    if age is not None and age >= FRAME_STALE_WARN_MS:
        return HealthItem("warn", f"已连接，{_minutes(age)} 没有新帧", "")
    if incidents:
        causes = "、".join(incident_cause_zh(i.get("cause_class")) for i in incidents)
        return HealthItem("warn", "已连接，有未关闭的接入事故", causes)
    # No wall-clock text on the healthy path: the status ETag must not churn while nothing changes.
    return HealthItem("ok", "已连接，正在收帧", "")


def broker_health(broker: Mapping[str, Any], *, open_causes: frozenset[str] = frozenset()) -> HealthItem:
    if not broker.get("configured"):
        return HealthItem("off", "队列未配置", "")
    if broker.get("connected") is False or "broker_unavailable" in open_causes:
        return HealthItem(
            "bad", "RabbitMQ 未连接", str(broker.get("error_code") or incident_cause_zh("broker_unavailable"))
        )
    if "broker_backpressure" in open_causes:
        return HealthItem("bad", "队列背压：raw 队列已满，正在拒收", "断线补抄会在事故关闭后回填")
    queues = broker.get("queues") or {}
    depths = {name: int((queues.get(name) or {}).get("messages") or 0) for name in _BUSINESS_QUEUES}
    dead = int((queues.get("news.dead") or {}).get("messages") or 0)
    worst_name, worst_depth = max(depths.items(), key=lambda kv: kv[1], default=("", 0))
    consumers_missing = [
        name for name in _BUSINESS_QUEUES if name in queues and int((queues.get(name) or {}).get("consumers") or 0) == 0
    ]
    if consumers_missing:
        return HealthItem("bad", "有队列没有消费者", "、".join(consumers_missing))
    if worst_depth >= QUEUE_DEPTH_BAD:
        return HealthItem("bad", f"{worst_name} 积压 {worst_depth} 条", "消费速度跟不上，检查模型延迟与 Workers")
    if worst_depth >= QUEUE_DEPTH_WARN:
        return HealthItem("warn", f"{worst_name} 积压 {worst_depth} 条", "")
    if dead > 0:
        return HealthItem("warn", f"死信队列有 {dead} 条", "需要人工查看 news.dead")
    if broker.get("connected") is None:
        return HealthItem("warn", "队列状态未知", "Janitor 还没有上报快照")
    summary = f"raw {depths['news.raw']} · triage {depths['news.triage']} · deliver {depths['news.deliver']}"
    return HealthItem("ok", "队列畅通", summary)


def model_health(
    pipeline: Mapping[str, Any],
    *,
    model_configured: bool,
    enabled: bool = True,
    open_causes: frozenset[str] = frozenset(),
) -> HealthItem:
    if not enabled:
        return HealthItem("off", "News 未启用", "")
    if not model_configured:
        if pipeline.get("triage_model") and not pipeline.get("reader_card_model"):
            return HealthItem("bad", "Reader 模型不可用", "ReaderCard 配置无效；所有事件按规则兜底")
        return HealthItem("bad", "未配置 Triage 模型", "所有事件按规则兜底")
    if "triage_circuit_open" in open_causes:
        return HealthItem("bad", "模型熔断中", "连续调用失败后暂停调用；此期间所有事件按规则兜底")
    # The model's own denominator, not the funnel's: a deterministic telemetry judgment is never
    # degraded, so counting ~190 of them a day here would dilute the share and make the model look
    # healthier than it is (#137).
    total = int(pipeline.get("model_triage_24h") or 0)
    degraded = int(pipeline.get("triage_degraded_24h") or 0)
    by_code = dict(pipeline.get("triage_degraded_by_code_24h") or {})
    ranked = sorted(by_code.items(), key=lambda kv: -kv[1])
    detail = "、".join(f"{error_code_zh(code)} {count}" for code, count in ranked)
    if total == 0:
        return HealthItem("ok", "24 小时内没有送审事件", "")
    share = degraded / total
    p95 = pipeline.get("triage_p95_ms")
    latency = f"p95 {int(p95) / 1000:.1f} 秒" if p95 else ""
    if share >= DEGRADED_SHARE_BAD:
        return HealthItem("bad", f"24 小时降级率 {_pct(share)}（{degraded}/{total}）", detail or latency)
    if share >= DEGRADED_SHARE_WARN:
        return HealthItem("warn", f"24 小时降级率 {_pct(share)}（{degraded}/{total}）", detail or latency)
    summary = f"模型正常，24 小时降级 {degraded}/{total}" if degraded else f"模型正常，24 小时 {total} 次判断"
    return HealthItem("ok", summary, latency)


def delivery_health(delivery: Mapping[str, Any]) -> HealthItem:
    if not delivery.get("delivery_available"):
        return HealthItem("off", "推送未配置", "news.push 未启用、配置无效或 Workers 未运行")
    sent = int(delivery.get("sent_24h") or 0)
    terminal = int(delivery.get("terminal_24h") or 0)
    total = sent + terminal
    share = terminal / total if total else 0.0
    last_error = delivery.get("last_error_code")
    if total and share >= DELIVERY_FAIL_SHARE_BAD:
        return HealthItem("bad", f"24 小时 {terminal}/{total} 条未送达", str(last_error or ""))
    if total and share >= DELIVERY_FAIL_SHARE_WARN:
        return HealthItem("warn", f"24 小时 {terminal}/{total} 条未送达", str(last_error or ""))
    return HealthItem("ok", f"24 小时已推送 {sent} 条，最近 1 小时 {int(delivery.get('sent_1h') or 0)} 条", "")


# ------------------------------------------------------------------------------------------------ status
def status_health(
    *,
    ingest: Mapping[str, Any],
    broker: Mapping[str, Any],
    pipeline: Mapping[str, Any],
    delivery: Mapping[str, Any],
    workers_state: str | None,
    now_ms: int,
    enabled: bool,
    model_configured: bool,
) -> dict[str, Any]:
    """``health`` (four thresholded items + overall), ``funnel_24h`` and ``reasons_24h`` (Chinese-labelled)."""

    open_causes = frozenset(
        str(i.get("cause_class") or "") for i in (ingest.get("open_incidents") or []) if not i.get("planned")
    )
    items = {
        "ingest": ingest_health(ingest, now_ms=now_ms, workers_state=workers_state, enabled=enabled),
        "broker": broker_health(broker, open_causes=open_causes),
        "model": model_health(pipeline, model_configured=model_configured, enabled=enabled, open_causes=open_causes),
        "delivery": delivery_health(delivery),
    }
    overall = _worst(*(item.level for item in items.values()))
    funnel = {
        "received": int(pipeline.get("funnel_received_24h") or 0),
        "admitted": int(pipeline.get("funnel_admitted_24h") or 0),
        "candidates": int(pipeline.get("candidates_24h") or 0),
        "triaged": int(pipeline.get("funnel_triaged_24h") or 0),
        # #87: between "sent to the model" and "decided", the reader wants to know how many Events named an
        # asset that actually exists on a venue. It is a property of the same Events, not a separate stage.
        # `tagged` travels with it because it is the only population `grounded` can honestly be compared
        # against — Events that carried no coin tag at all never offered a symbol to resolve.
        "tagged": int(pipeline.get("tagged_24h") or 0),
        "grounded": int(pipeline.get("grounded_24h") or 0),
        "decided_push": int(pipeline.get("decided_push_24h") or 0),
        "delivered": int(pipeline.get("funnel_delivered_24h") or 0),
        "received_1h": int(pipeline.get("events_1h") or 0),
        "delivered_1h": int(delivery.get("sent_1h") or 0),
    }
    reasons: list[dict[str, Any]] = []
    for key, count in (pipeline.get("suppressed_by_reason") or {}).items():
        reasons.append({"stage": "gate", "key": key, "label_zh": admission_zh(key), "count": int(count)})
    for key, count in (pipeline.get("dropped_by_rule") or {}).items():
        reasons.append({"stage": "drop", "key": key, "label_zh": override_rule_zh(key), "count": int(count)})
    for key, count in (pipeline.get("throttled_by_key") or {}).items():
        reasons.append({"stage": "throttle", "key": key, "label_zh": throttled_by_zh(key), "count": int(count)})
    for key, count in (pipeline.get("pushed_by_rule") or {}).items():
        reasons.append({"stage": "push", "key": key, "label_zh": override_rule_zh(key), "count": int(count)})
    for key, count in (pipeline.get("triage_degraded_by_code_24h") or {}).items():
        reasons.append({"stage": "degraded", "key": key, "label_zh": error_code_zh(key), "count": int(count)})
    # The provider tag is its own label here: "SPOT" and "NEAR" say more to an operator than any sentence we
    # could wrap around them, and inventing the English word they came from would be a guess.
    for key, count in (pipeline.get("ungrounded_by_symbol_24h") or {}).items():
        reasons.append({"stage": "ungrounded", "key": key, "label_zh": str(key), "count": int(count)})
    reasons.sort(key=lambda r: (-int(r["count"]), str(r["stage"]), str(r["key"])))
    return {
        "health": {**{name: item.as_dict() for name, item in items.items()}, "overall": overall},
        "funnel_24h": funnel,
        "reasons_24h": reasons,
    }


__all__ = [
    "DEGRADED_SHARE_BAD",
    "DEGRADED_SHARE_WARN",
    "DELIVERY_FAIL_SHARE_BAD",
    "DELIVERY_FAIL_SHARE_WARN",
    "FRAME_STALE_BAD_MS",
    "FRAME_STALE_WARN_MS",
    "HEALTH_VERSION",
    "QUEUE_DEPTH_BAD",
    "QUEUE_DEPTH_WARN",
    "HealthItem",
    "Level",
    "broker_health",
    "delivery_health",
    "ingest_health",
    "model_health",
    "status_health",
]
