"""Outcome / vocabulary / timeline / health: the one conclusion per Event and the thresholded status page (pure)."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from tracefold.news.health import status_health
from tracefold.news.outcome import (
    OUTCOME_GROUP,
    OVERRIDE_RULE_ZH,
    admission_zh,
    error_code_zh,
    event_outcome,
    override_rule_zh,
    storyline_key_zh,
    throttled_by_zh,
)
from tracefold.news.timeline import event_timeline

NOW = 1_800_000_000_000
triage_rules = importlib.import_module("tracefold.news.triage_rules")


def _triage(final: str, **over: object) -> dict[str, object]:
    return {
        "final_decision": final,
        "override_rule": over.get("override_rule"),
        "throttled_by": over.get("throttled_by"),
        "degraded": bool(over.get("degraded", False)),
        "error_code": over.get("error_code"),
        "created_at_ms": NOW + 5_000,
        "stage": "triage",
        "verdict": over.get("verdict", {}),
        "trace": {},
    }


@pytest.mark.parametrize(
    ("admission", "published", "triage", "delivery", "kind", "text"),
    [
        ("recovery", None, None, None, "held_recovery", "补抄件，不推送"),
        ("suppressed_pr_template", None, None, None, "held_gate", "未送审"),
        ("candidate", None, None, None, "queued_publish", "待处理"),
        ("candidate", NOW, None, None, "queued_triage", "审稿中"),
        ("candidate", NOW, _triage("drop", override_rule="noise"), None, "dropped", "未推送"),
        (
            "candidate",
            NOW,
            _triage("throttled", override_rule="model_push_actionable", throttled_by="storyline:asset:BTC"),
            None,
            "throttled",
            "未推送（历史限流）",
        ),
        (
            "candidate",
            NOW,
            _triage("drop", override_rule="fail_closed_fallback", degraded=True, error_code="news_triage_timeout"),
            None,
            "degraded_dropped",
            "未推送（规则兜底）",
        ),
        ("candidate", NOW, _triage("push", override_rule="model_push_actionable"), None, "pending_delivery", "待推送"),
        ("candidate", NOW, _triage("escalate", override_rule="magnitude3"), None, "pending_delivery", "待推送（重点）"),
        (
            "candidate",
            NOW,
            _triage("push", override_rule="watchlist"),
            {"state": "sent", "error_code": None},
            "delivered",
            "已推送",
        ),
        (
            "candidate",
            NOW,
            _triage("escalate", override_rule="magnitude3"),
            {"state": "sent", "error_code": None},
            "delivered",
            "已推送（重点）",
        ),
        (
            "listing_deterministic",
            NOW,
            _triage("push", override_rule="model_push_actionable"),
            {"state": "terminal", "error_code": "feishu_http_500"},
            "delivery_failed",
            "未送达",
        ),
    ],
)
def test_event_outcome_covers_every_kind_in_priority_order(admission, published, triage, delivery, kind, text) -> None:
    outcome = event_outcome(admission=admission, published_at_ms=published, triage=triage, delivery=delivery)
    assert outcome.kind == kind and outcome.text_zh == text
    assert outcome.group == OUTCOME_GROUP[kind]
    assert outcome.as_dict() == {
        "kind": kind,
        "text_zh": text,
        "reason_zh": outcome.reason_zh,
        "group": outcome.group,
    }


def test_outcome_reasons_are_chinese_never_bare_keys() -> None:
    throttled = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage("throttled", throttled_by="storyline:theme:trade:cap3"),
        delivery=None,
    )
    assert throttled.reason_zh == "「贸易与关税」话题 4 小时内已推 3 条"
    duplicate = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage("throttled", throttled_by="storyline:asset:BTC:seen"),
        delivery=None,
    )
    assert duplicate.text_zh == "未推送（重复）"
    dropped = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage("drop", override_rule="unclear_direction"),
        delivery=None,
    )
    assert dropped.reason_zh == "方向不明，未达推送标准"
    degraded = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage("drop", degraded=True, error_code="news_triage_output_truncated"),
        delivery=None,
    )
    assert degraded.reason_zh == "模型不可用，按规则兜底不推：模型输出被截断"
    failed = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage("push", override_rule="model_push_actionable"),
        delivery={"state": "terminal", "error_code": "ambiguous_after_crash"},
    )
    assert failed.reason_zh == "发送状态不确定（进程中断），不重发"
    fallback_push = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage("push", override_rule="fail_closed_fallback", degraded=True, error_code="news_triage_timeout"),
        delivery={"state": "sent", "error_code": None},
    )
    assert fallback_push.kind == "delivered" and fallback_push.reason_zh == "模型不可用，按规则兜底推送：模型超时"


def test_vocabulary_covers_every_decide_rule_and_falls_back_to_the_key() -> None:
    # Every rule name decide() can emit must have a Chinese label — read them from the source so a new rule
    # in triage_rules.py fails this test until outcome.py names it.
    source = (Path(triage_rules.__file__)).read_text(encoding="utf-8")
    emitted = set(
        re.findall(
            r'(?:final, rule = "[a-z]+", |DecisionResult\((?:"[a-z]+"|baseline), |^\s+rule = )"([a-z0-9_]+)"',
            source,
            re.M,
        )
    )
    assert emitted >= {
        "degraded_listing_objective",
        "degraded_no_objective_guard",
        "degraded_telemetry_objective",
        "degraded_watchlist_objective",
        "listing_deterministic",
        "restatement",
        "stale_source_artifact",
        "telemetry_deterministic",
        "trade_relevance_escalate",
        "trade_relevance_inconsistent",
        "trade_relevance_realtime",
        "watchlist_objective_guard",
    }
    missing = sorted(rule for rule in emitted if rule not in OVERRIDE_RULE_ZH)
    assert missing == []
    assert override_rule_zh("brand_new_rule") == "brand_new_rule"
    # Keys that only exist on historical rows (retired policies/lanes) still get Chinese copy.
    assert override_rule_zh("magnitude2_actionable").startswith("影响明显")
    assert admission_zh("suppressed_ungrounded").endswith("（已退役）")
    assert error_code_zh("news_analyst_failed:GraphRecursionError") == "分析模型失败（已退役通道）"
    assert admission_zh("candidate") == "已送审"
    assert error_code_zh("news_triage_model_failed:RateLimitError") == "模型调用失败（RateLimitError）"
    assert throttled_by_zh("hourly_cap") == "已达每小时推送上限"
    assert throttled_by_zh("storyline:asset:XYZ-HD") == "XYZ-HD 同一话题在节流窗口内已推过同等或更重要的消息"
    assert throttled_by_zh("storyline:macro:general:cap3") == "「综合」类 4 小时内已推 3 条"
    # Policy v3 (issue #61): the novelty vocabulary, still on historical rows.
    assert override_rule_zh("restatement") == "重复：读者已收到同一事实"
    assert override_rule_zh("novel_bypass").endswith("（旧规则）")
    # Historical v5/v6 rules remain readable after policy v7 removed quotas.
    assert override_rule_zh("distinct_bypass") == "与读者刚收到的卡片都不同，放行"
    assert throttled_by_zh("storyline:asset:BTC:hard6") == "BTC 2 小时内已推 6 条，达到防洪上限"
    assert throttled_by_zh("storyline:theme:rates:hard18") == "「利率与央行」话题 4 小时内已推 18 条，达到防洪上限"
    assert throttled_by_zh("storyline:asset:BTC:seen") == "重复：读者刚收到过内容高度相近的卡片"
    assert throttled_by_zh("storyline:macro:general:cap3:seen") == "重复：读者刚收到过内容高度相近的卡片"
    # Policy v7 emits only this content-based shape. `:seen` short-circuits
    # ahead of historical cap/hard regexes.
    assert throttled_by_zh("storyline:asset:KLAC:seen") == "重复：读者刚收到过内容高度相近的卡片"
    assert throttled_by_zh("storyline:theme:mideast_energy:seen") == "重复：读者刚收到过内容高度相近的卡片"
    assert throttled_by_zh("storyline:macro:general:seen") == "重复：读者刚收到过内容高度相近的卡片"
    # Historical keys (the pre-v5 hard caps) still read as sentences.
    assert throttled_by_zh("storyline:theme:rates:hard6") == "「利率与央行」话题 4 小时内已推 6 条，达到防洪上限"
    assert throttled_by_zh("storyline:macro:general:hard6") == "「综合」类 4 小时内已推 6 条，达到防洪上限"
    assert storyline_key_zh("theme:mideast_energy") == "中东与能源" and storyline_key_zh("asset:BTC") == "BTC"


def _event(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_id": "ev-1",
        "leader_title": "Binance will list XYZ",
        "reporting_origin": "binance",
        "opened_at_ms": NOW,
        "member_count": 3,
        "admission": "candidate",
        "asset_class": "crypto",
        "grounded_assets": ["XYZ"],
        "watchlist_hits": [],
        "macro_lexicon": False,
        "storyline_key": "asset:XYZ",
        "published_at_ms": NOW + 100,
        "ingest_mode": "live",
        "provenance": ["1353"],
    }
    base.update(over)
    return base


def test_timeline_tells_the_story_in_order_with_chinese_summaries() -> None:
    verdict = {
        "headline_zh": "币安上线 XYZ",
        "direction": "bullish",
        "magnitude": 2,
        "event_type": "listing",
        "scope": "single_name",
    }
    triage = {
        **_triage("push", override_rule="trade_relevance_realtime", verdict=verdict),
        "model_decision": "push",
        "trace": {"storyline_key": "asset:XYZ", "queue_lag_ms": 800},
    }
    members = [
        {"reporting_origin": "binance"},
        {"reporting_origin": "coindesk"},
        {"reporting_origin": "binance"},
    ]
    deliveries = [
        {
            "kind": "first",
            "state": "sent",
            "error_code": None,
            "attempted_at_ms": NOW + 9_000,
            "settled_at_ms": NOW + 9_500,
        }
    ]

    outcome, steps = event_timeline(event=_event(), members=members, verdicts=[triage], deliveries=deliveries)

    assert outcome.kind == "delivered"
    assert [s["stage"] for s in steps] == ["received", "gate", "triage", "decide", "delivery"]
    assert steps[0]["summary_zh"] == "来源 binance · 归并 3 条同类报道（2 个来源）"
    assert steps[1]["summary_zh"] == "已送审 · 关联 XYZ"
    _, cl_steps = event_timeline(
        event=_event(grounded_assets=["CL", "XYZ-CL", "BTC"]), members=[], verdicts=[], deliveries=[]
    )
    assert cl_steps[1]["summary_zh"] == "已送审 · 关联 CL BTC"
    assert steps[2]["summary_zh"] == "币安上线 XYZ · 利多 / 影响明显 / 上币 · 模型建议：推送"
    assert steps[3]["summary_zh"] == "推送 · 交易相关性达到实时推送标准"
    assert steps[4]["summary_zh"] == "已推送到飞书" and steps[4]["at_ms"] == NOW + 9_500
    assert steps[3]["facts"]["storyline_zh"] == "XYZ"


def test_timeline_for_a_recovery_event_stops_at_the_gate() -> None:
    outcome, steps = event_timeline(
        event=_event(admission="recovery", ingest_mode="recovery", published_at_ms=None, member_count=1),
        members=[],
        verdicts=[],
        deliveries=[],
    )
    assert outcome.kind == "held_recovery"
    assert [s["stage"] for s in steps] == ["received", "gate"]
    assert "断线补抄" in steps[0]["summary_zh"]


def _status_inputs(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ingest": {"connected": True, "last_frame_at_ms": NOW - 60_000, "open_incidents": []},
        "broker": {
            "configured": True,
            "connected": True,
            "queues": {
                "news.raw": {"messages": 0, "consumers": 1},
                "news.triage": {"messages": 3, "consumers": 1},
                "news.deliver": {"messages": 0, "consumers": 1},
                "news.dead": {"messages": 0, "consumers": 0},
            },
        },
        "pipeline": {
            "events_1h": 10,
            "events_24h": 200,
            "candidates_24h": 150,
            "triage_24h": 150,
            "triage_degraded_24h": 3,
            "decided_push_24h": 20,
            "triage_p95_ms": 3200,
            "suppressed_by_reason": {"suppressed_pr_template": 5},
            "dropped_by_rule": {"noise": 60, "unclear_direction": 30},
            "throttled_by_key": {"storyline:asset:BTC": 12},
            "pushed_by_rule": {"model_push_actionable": 18, "magnitude3": 2},
            "triage_degraded_by_code_24h": {"news_triage_timeout": 3},
            "tagged_24h": 150,
            "grounded_24h": 144,
            "ungrounded_by_symbol_24h": {"SPOT": 38, "NEAR": 9},
        },
        "delivery": {
            "delivery_available": True,
            "sent_24h": 19,
            "sent_1h": 2,
            "terminal_24h": 1,
            "last_error_code": None,
        },
        "workers_state": "running",
        "now_ms": NOW,
        "enabled": True,
        "model_configured": True,
    }
    base.update(over)
    return base


def test_status_health_is_green_with_funnel_and_named_reasons() -> None:
    out = status_health(**_status_inputs())  # type: ignore[arg-type]
    health = out["health"]
    assert health["overall"] == "ok"
    assert {k: v["level"] for k, v in health.items() if k != "overall"} == {
        "ingest": "ok",
        "broker": "ok",
        "model": "ok",
        "delivery": "ok",
    }
    assert out["funnel_24h"] == {
        "received": 200,
        "candidates": 150,
        "triaged": 150,
        # #87: how many of the same Events named an asset that exists on a venue. It sits between "sent to
        # the model" and "decided" because that is where the reader asks it, not because it is a stage.
        # `tagged` travels with it: it is the only population `grounded` can be compared against.
        "tagged": 150,
        "grounded": 144,
        "decided_push": 20,
        "delivered": 19,
        "received_1h": 10,
        "delivered_1h": 2,
    }
    reasons = out["reasons_24h"]
    assert reasons[0] == {"stage": "drop", "key": "noise", "label_zh": "模型判定为噪音", "count": 60}
    assert {r["stage"] for r in reasons} == {"gate", "drop", "throttle", "push", "degraded", "ungrounded"}
    assert all(r["label_zh"] for r in reasons)
    # The provider tag is its own label — inventing the English word it collided with would be a guess.
    assert {"stage": "ungrounded", "key": "SPOT", "label_zh": "SPOT", "count": 38} in reasons


def test_status_health_thresholds_turn_amber_and_red() -> None:
    degraded = status_health(**_status_inputs(pipeline={**_status_inputs()["pipeline"], "triage_degraded_24h": 30}))  # type: ignore[arg-type]
    assert degraded["health"]["model"]["level"] == "bad" and "20%" in degraded["health"]["model"]["summary_zh"]
    assert degraded["health"]["overall"] == "bad"

    amber = status_health(**_status_inputs(pipeline={**_status_inputs()["pipeline"], "triage_degraded_24h": 8}))  # type: ignore[arg-type]
    assert amber["health"]["model"]["level"] == "warn"

    stale = status_health(**_status_inputs(ingest={"connected": True, "last_frame_at_ms": NOW - 40 * 60_000}))  # type: ignore[arg-type]
    assert stale["health"]["ingest"]["level"] == "bad" and "40 分钟" in stale["health"]["ingest"]["summary_zh"]

    # A lingering model-circuit incident belongs to the model item, not the WSS lane.
    circuit = status_health(
        **_status_inputs(
            ingest={
                "connected": True,
                "last_frame_at_ms": NOW - 60_000,
                "open_incidents": [{"cause_class": "triage_circuit_open", "planned": False}],
            }
        )
    )  # type: ignore[arg-type]
    assert circuit["health"]["ingest"]["level"] == "ok"
    wss = status_health(
        **_status_inputs(
            ingest={
                "connected": True,
                "last_frame_at_ms": NOW - 60_000,
                "open_incidents": [{"cause_class": "idle_timeout", "planned": False}],
            }
        )
    )  # type: ignore[arg-type]
    assert wss["health"]["ingest"] == {
        "level": "warn",
        "summary_zh": "已连接，有未关闭的接入事故",
        "detail_zh": "长时间无帧",
    }

    backlog = status_health(
        **_status_inputs(
            broker={"configured": True, "connected": True, "queues": {"news.triage": {"messages": 260, "consumers": 1}}}
        )
    )  # type: ignore[arg-type]
    assert (
        backlog["health"]["broker"]["level"] == "bad"
        and backlog["health"]["broker"]["summary_zh"] == "news.triage 积压 260 条"
    )

    # There is no operator pause any more: only real delivery failures can turn this item amber.
    failing = status_health(
        **_status_inputs(
            delivery={
                "delivery_available": True,
                "sent_24h": 90,
                "sent_1h": 4,
                "terminal_24h": 10,
                "last_error_code": "feishu_http_500",
            }
        )
    )  # type: ignore[arg-type]
    assert failing["health"]["delivery"]["level"] in {"warn", "bad"}

    off = status_health(**_status_inputs(delivery={"delivery_available": False}, model_configured=False))  # type: ignore[arg-type]
    assert off["health"]["delivery"]["level"] == "off" and off["health"]["model"]["level"] == "bad"
    assert off["health"]["overall"] == "bad"


def test_timeline_fills_the_empty_title_sentinel() -> None:
    """#101: an empty `title_zh` means "same as headline_zh". The console shows a title either way — the sentinel
    saves output tokens, it does not take the field away from the operator."""

    verdict = {"headline_zh": "币安上线 XYZ", "title_zh": "", "direction": "bullish", "magnitude": 2}
    _, steps = event_timeline(event=_event(), members=[], verdicts=[_triage("push", verdict=verdict)], deliveries=[])
    assert steps[2]["facts"]["title_zh"] == "币安上线 XYZ"

    condensed = {**verdict, "title_zh": "币安公告将于本周上线 XYZ 现货交易对"}
    _, steps = event_timeline(event=_event(), members=[], verdicts=[_triage("push", verdict=condensed)], deliveries=[])
    assert steps[2]["facts"]["title_zh"] == "币安公告将于本周上线 XYZ 现货交易对"


def test_console_read_sites_fill_the_empty_title_sentinel() -> None:
    """#101 AC#2: the two surfaces the operator actually reads — the detail hero's translated line and the feed
    row — both resolve the sentinel. Neither had an assertion; the timeline test above covers only the collapsed
    technical panel, and the HTTP contract test uses a fake repository."""

    from tracefold.news.storage.feed import _feed_row, _triage_summary

    sentinel = {"headline_zh": "币安上线 XYZ", "title_zh": "", "direction": "bullish", "magnitude": 2}
    summary = _triage_summary(final_decision="push", verdict=sentinel)
    assert summary is not None and summary["title_zh"] == "币安上线 XYZ"

    condensed = {**sentinel, "title_zh": "币安公告将于本周上线 XYZ 现货交易对"}
    filled = _triage_summary(final_decision="push", verdict=condensed)
    assert filled is not None and filled["title_zh"] == "币安公告将于本周上线 XYZ 现货交易对"

    # The feed row reads the flattened SQL columns, not the verdict blob — a separate path with the same rule.
    row = {
        "event_id": "ev-1",
        "family": "general",
        "last_member_at_ms": NOW,
        "expires_at_ms": NOW + 3600_000,
        "comparison_title": "binance will list xyz",
        "engine_type": "listing",
        "macro_lexicon": False,
        "provider_score_max": 80.0,
        "context_line": "",
        "published_at_ms": NOW,
        "trace_id": "t-1",
        "opened_at_ms": NOW,
        "leader_title": "Binance will list XYZ",
        "reporting_origin": "binance",
        "admission": "candidate",
        "asset_class": "crypto",
        "grounded_assets": ["XYZ"],
        "watchlist_hits": [],
        "storyline_key": "asset:XYZ",
        "member_count": 1,
        "ingest_mode": "live",
        "provenance": [],
        "final_decision": "push",
        "headline_zh": "币安上线 XYZ",
        "title_zh": "",
    }
    assert _feed_row(row)["title_zh"] == "币安上线 XYZ"
    assert _feed_row({**row, "title_zh": "币安公告将于本周上线 XYZ 现货交易对"})["title_zh"] == (
        "币安公告将于本周上线 XYZ 现货交易对"
    )
    # Nothing to show at all stays None rather than becoming an empty string in the API payload.
    assert _feed_row({**row, "headline_zh": None, "title_zh": ""})["title_zh"] is None
