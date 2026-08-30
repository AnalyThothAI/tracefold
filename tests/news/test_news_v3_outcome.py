"""Outcome / vocabulary / timeline / health: the one conclusion per Event and the thresholded status page (pure)."""

from __future__ import annotations

import pytest

from tracefold.news.health import status_health
from tracefold.news.outcome import (
    OUTCOME_GROUP,
    OVERRIDE_RULE_ZH,
    admission_zh,
    delivery_error_zh,
    event_outcome,
    override_rule_zh,
    storyline_key_zh,
    throttled_by_zh,
)
from tracefold.news.timeline import event_timeline

NOW = 1_800_000_000_000


def test_unexpected_delivery_error_copy_is_provider_neutral() -> None:
    assert delivery_error_zh("news_delivery_failed:ProviderError") == "推送失败（ProviderError）"


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
        (
            "unsupported_market_contract",
            NOW,
            _triage("push", override_rule="watchlist_objective_guard"),
            {"state": "sent", "error_code": None},
            "delivered",
            "已推送",
        ),
        (
            "unsupported_market_contract",
            NOW,
            _triage("push", override_rule="watchlist_objective_guard"),
            {"state": "sending", "error_code": None},
            "pending_delivery",
            "推送中",
        ),
        (
            "unsupported_market_contract",
            NOW,
            _triage("push", override_rule="watchlist_objective_guard"),
            {"state": "terminal", "error_code": "ambiguous_after_crash"},
            "delivery_failed",
            "未送达",
        ),
        ("candidate", None, None, None, "queued_publish", "待处理"),
        ("candidate", NOW, None, None, "queued_triage", "审稿中"),
        ("candidate", NOW, _triage("drop", override_rule="reader_value_none"), None, "dropped", "未推送"),
        (
            "candidate",
            NOW,
            _triage("throttled", override_rule="trade_relevance_realtime", throttled_by="storyline:asset:BTC:seen"),
            None,
            "throttled",
            "未推送（重复）",
        ),
        (
            "candidate",
            NOW,
            _triage(
                "drop",
                override_rule="degraded_no_objective_guard",
                degraded=True,
                error_code="news_program_route_deadline",
            ),
            None,
            "degraded_dropped",
            "未推送（规则兜底）",
        ),
        (
            "candidate",
            NOW,
            _triage("push", override_rule="trade_relevance_realtime"),
            None,
            "pending_delivery",
            "待推送",
        ),
        (
            "candidate",
            NOW,
            _triage("escalate", override_rule="trade_relevance_escalate"),
            None,
            "pending_delivery",
            "待推送（重点）",
        ),
        (
            "candidate",
            NOW,
            _triage("push", override_rule="watchlist_objective_guard"),
            {"state": "sent", "error_code": None},
            "delivered",
            "已推送",
        ),
        (
            "candidate",
            NOW,
            _triage("escalate", override_rule="trade_relevance_escalate"),
            {"state": "sent", "error_code": None},
            "delivered",
            "已推送（重点）",
        ),
        (
            "listing_deterministic",
            NOW,
            _triage("push", override_rule="listing_deterministic"),
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
        triage=_triage("throttled", throttled_by="storyline:theme:trade:seen"),
        delivery=None,
    )
    assert throttled.reason_zh == "重复：读者刚收到过内容高度相近的卡片"
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
        triage=_triage("drop", override_rule="reader_value_none"),
        delivery=None,
    )
    assert dropped.reason_zh == "无读者价值，不推送"
    degraded = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage("drop", degraded=True, error_code="news_program_output_truncated"),
        delivery=None,
    )
    assert degraded.reason_zh == "模型不可用，按规则兜底不推：语义程序输出被截断"
    failed = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage("push", override_rule="trade_relevance_realtime"),
        delivery={"state": "terminal", "error_code": "ambiguous_after_crash"},
    )
    assert failed.reason_zh == "发送状态不确定（进程中断），不重发"
    fallback_push = event_outcome(
        admission="candidate",
        published_at_ms=NOW,
        triage=_triage(
            "push",
            override_rule="degraded_watchlist_objective",
            degraded=True,
            error_code="news_program_route_deadline",
        ),
        delivery={"state": "sent", "error_code": None},
    )
    assert fallback_push.kind == "delivered" and fallback_push.reason_zh == "模型不可用，按规则兜底推送：语义程序超时"


def test_expired_handoffs_are_terminal_held_outcomes() -> None:
    expired_event = event_outcome(
        admission="candidate",
        opened_at_ms=NOW - 30 * 60_000 - 1,
        published_at_ms=None,
        triage=None,
        delivery=None,
        now_ms=NOW,
    )
    assert expired_event.kind == "expired_triage_handoff"
    assert expired_event.group == "held"

    triage = _triage("push", override_rule="model_push_actionable")
    triage["created_at_ms"] = NOW - 30 * 60_000 - 1
    triage["published_at_ms"] = None
    expired_verdict = event_outcome(
        admission="candidate",
        opened_at_ms=NOW - 60 * 60_000,
        published_at_ms=NOW - 59 * 60_000,
        triage=triage,
        delivery=None,
        now_ms=NOW,
    )
    assert expired_verdict.kind == "expired_delivery_handoff"
    assert expired_verdict.group == "held"


def test_handoff_deadline_boundary_is_still_pending_and_marker_wins() -> None:
    boundary = event_outcome(
        admission="candidate",
        opened_at_ms=NOW - 30 * 60_000,
        published_at_ms=None,
        triage=None,
        delivery=None,
        now_ms=NOW,
    )
    assert boundary.kind == "queued_publish"

    triage = _triage("push", override_rule="model_push_actionable")
    triage["created_at_ms"] = NOW - 31 * 60_000
    triage["published_at_ms"] = NOW - 1
    published = event_outcome(
        admission="candidate",
        opened_at_ms=NOW - 60 * 60_000,
        published_at_ms=NOW - 59 * 60_000,
        triage=triage,
        delivery=None,
        now_ms=NOW,
    )
    assert published.kind == "pending_delivery"


def test_vocabulary_names_current_public_rule_codes_and_falls_back_for_unknown_codes() -> None:
    current_rules = {
        "degraded_listing_objective",
        "degraded_no_objective_guard",
        "degraded_watchlist_objective",
        "beyond_window_rank",
        "listing_deterministic",
        "liquidation_fact_only",
        "liquidation_parse_failed",
        "oi_change_below_threshold",
        "oi_parse_failed",
        "opening_move_with_whale_concentration",
        "restatement",
        "stale_source_artifact",
        "trade_relevance_escalate",
        "trade_relevance_inconsistent",
        "trade_relevance_realtime",
        "whale_ratio_below_threshold",
        "watchlist_objective_guard",
    }
    missing = sorted(rule for rule in current_rules if rule not in OVERRIDE_RULE_ZH)
    assert missing == []
    assert override_rule_zh("brand_new_rule") == "brand_new_rule"
    assert admission_zh("candidate") == "已送审"
    assert override_rule_zh("restatement") == "重复：读者已收到同一事实"
    assert throttled_by_zh("storyline:asset:BTC:seen") == "重复：读者刚收到过内容高度相近的卡片"
    assert throttled_by_zh("storyline:asset:KLAC:seen") == "重复：读者刚收到过内容高度相近的卡片"
    assert throttled_by_zh("storyline:theme:mideast_energy:seen") == "重复：读者刚收到过内容高度相近的卡片"
    assert throttled_by_zh("storyline:macro:general:seen") == "重复：读者刚收到过内容高度相近的卡片"
    assert storyline_key_zh("theme:mideast_energy") == "中东与能源" and storyline_key_zh("asset:BTC") == "BTC"


def _event(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_id": "ev-1",
        "leader_title": "Binance will list XYZ",
        "reporting_origin": "binance",
        "dedupe_family": "general",
        "event_kind": "news",
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
        "scope": "single_name",
    }
    triage = {
        **_triage("push", override_rule="trade_relevance_realtime", verdict=verdict),
        "judgment_origin": "model",
        "judgment_contract_version": "news_judgment_v2",
        "model_editorial": {
            "taxonomy": {
                "event_family": "market_access",
                "change_state": "effective",
                "assertion_status": "confirmed",
                "source_authority": "issuer_first_party",
            },
            "relevance": {"reader_value": "realtime"},
        },
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
    assert steps[2]["summary_zh"] == "币安上线 XYZ · 利多 / 影响明显 / 市场准入"
    assert steps[2]["facts"]["taxonomy"]["event_family"] == "market_access"
    assert steps[2]["facts"]["relevance"] == {"reader_value": "realtime"}
    assert steps[3]["summary_zh"] == "推送 · 交易相关性达到实时推送标准"
    assert steps[4]["summary_zh"] == "已送达" and steps[4]["at_ms"] == NOW + 9_500
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


def test_news_why_names_the_restated_card_time_and_targeted_retrieval_reason() -> None:
    verdict = {
        "novelty": "restatement",
        "restates": 0,
        "headline_zh": "重复卡片",
        "direction": "bearish",
        "magnitude": 2,
    }
    triage = {
        **_triage("drop", override_rule="restatement", verdict=verdict),
        "trace": {
            "restates_event_id": "prior",
            "told": [
                {
                    "event_id": "prior",
                    "at_ms": NOW - 24 * 3_600_000,
                    "headline_zh": "昨日原卡",
                    "history_scope": "targeted",
                    "retrieval_reason": "exact_fingerprint",
                }
            ],
        },
    }

    _, steps = event_timeline(event=_event(), members=[], verdicts=[triage], deliveries=[])

    decide = next(step for step in steps if step["stage"] == "decide")
    assert "昨日原卡" in decide["summary_zh"] and "精确事实定向召回" in decide["summary_zh"]
    assert decide["facts"]["restated_at_ms"] == NOW - 24 * 3_600_000
    assert decide["facts"]["restated_retrieval_reason"] == "exact_fingerprint"


def _queue(
    *,
    messages: int = 0,
    consumers: int = 0,
    ready: int = 0,
    delayed: int = 0,
    dead_letter_pending: int = 0,
    bytes_used_bps: int | None = 0,
    policy_ok: bool | None = True,
    missing: bool = False,
) -> dict[str, object]:
    """One row of the #400 broker snapshot: depth from AMQP, the rest from the management API."""

    return {
        "messages": messages,
        "consumers": consumers,
        "ready": ready,
        "unacked": max(0, messages - ready),
        "delayed": delayed,
        "dead_letter_pending": dead_letter_pending,
        "message_bytes": messages * 512,
        "max_length_bytes": 4 * 1024 * 1024,
        "bytes_used_bps": bytes_used_bps,
        "policy_ok": policy_ok,
        "missing": missing,
    }


def _status_inputs(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ingest": {"connected": True, "last_frame_at_ms": NOW - 60_000, "open_incidents": []},
        "broker": {
            "configured": True,
            "connected": True,
            "queues": {
                "news.raw": _queue(consumers=1),
                "news.triage": _queue(messages=3, ready=3, consumers=1),
                "news.deliver": _queue(consumers=1),
                "news.dead": _queue(),
            },
        },
        "pipeline": {
            "admitted_24h": 150,
            "events_1h": 10,
            "events_24h": 200,
            "candidates_24h": 150,
            "triage_24h": 150,
            "model_triage_24h": 150,
            "triage_degraded_24h": 3,
            "decided_push_24h": 20,
            "funnel_received_24h": 200,
            "funnel_admitted_24h": 150,
            "funnel_triaged_24h": 150,
            "funnel_delivered_24h": 19,
            "triage_p95_ms": 3200,
            "suppressed_by_reason": {"suppressed_pr_template": 5},
            "dropped_by_rule": {"reader_value_none": 60, "reader_value_background": 30},
            "throttled_by_key": {"storyline:asset:BTC:seen": 12},
            "pushed_by_rule": {"trade_relevance_realtime": 18, "trade_relevance_escalate": 2},
            "triage_degraded_by_code_24h": {"news_program_route_deadline": 3},
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
        "admitted": 150,
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
    assert reasons[0] == {
        "stage": "drop",
        "key": "reader_value_none",
        "label_zh": "无读者价值，不推送",
        "count": 60,
    }
    assert {r["stage"] for r in reasons} == {"gate", "drop", "throttle", "push", "degraded", "ungrounded"}
    assert all(r["label_zh"] for r in reasons)
    # The provider tag is its own label — inventing the English word it collided with would be a guess.
    assert {"stage": "ungrounded", "key": "SPOT", "label_zh": "SPOT", "count": 38} in reasons


def _broker_health(**queues: object) -> tuple[str, str]:
    inputs = _status_inputs()
    inputs["broker"] = {"configured": True, "connected": True, "queues": queues}
    item = status_health(**inputs)["health"]["broker"]  # type: ignore[arg-type]
    return str(item["level"]), str(item["summary_zh"])


def test_broker_health_is_bad_when_the_retry_policy_does_not_match_the_contract() -> None:
    """#400: without the policy there is no delay, no delivery limit and no at-least-once dead lettering.

    Nothing else on this page would show that, because depths and consumer counts look exactly the same.
    """

    level, title = _broker_health(
        **{
            "news.raw": _queue(consumers=1),
            "news.triage": _queue(consumers=1, policy_ok=False),
            "news.deliver": _queue(consumers=1),
            "news.dead": _queue(),
        }
    )
    assert (level, title) == ("bad", "队列策略与契约不符")


def test_broker_health_is_bad_when_a_queue_is_not_declared_at_all() -> None:
    """A queue that no longer exists must not read as an idle queue at depth zero."""

    level, title = _broker_health(
        **{
            "news.raw": _queue(consumers=1),
            "news.triage": _queue(consumers=1),
            "news.deliver": _queue(consumers=1),
            "news.dead": _queue(missing=True, policy_ok=None, bytes_used_bps=None),
        }
    )
    assert (level, title) == ("bad", "队列不存在")


def test_broker_health_is_bad_when_a_dead_letter_is_stuck_on_its_source_queue() -> None:
    """at-least-once dead lettering holds the message rather than dropping it, and that must be visible."""

    level, title = _broker_health(
        **{
            "news.raw": _queue(consumers=1),
            "news.triage": _queue(messages=1, consumers=1, dead_letter_pending=1),
            "news.deliver": _queue(consumers=1),
            "news.dead": _queue(),
        }
    )
    assert (level, title) == ("bad", "死信投递被卡住")


def test_broker_health_warns_before_a_queue_reaches_its_byte_bound() -> None:
    warned, warned_title = _broker_health(
        **{
            "news.raw": _queue(messages=10, consumers=1, bytes_used_bps=5_200),
            "news.triage": _queue(consumers=1),
            "news.deliver": _queue(consumers=1),
            "news.dead": _queue(),
        }
    )
    assert warned == "warn" and "字节额度" in warned_title
    bad, bad_title = _broker_health(
        **{
            "news.raw": _queue(messages=10, consumers=1, bytes_used_bps=9_100),
            "news.triage": _queue(consumers=1),
            "news.deliver": _queue(consumers=1),
            "news.dead": _queue(),
        }
    )
    assert bad == "bad" and "接近字节上限" in bad_title


def test_broker_health_warns_when_the_management_api_could_not_be_read() -> None:
    """AMQP answered, the management API did not: depths are real, the retry contract is unknown."""

    unknown = _queue(policy_ok=None, bytes_used_bps=None)
    level, title = _broker_health(
        **{
            "news.raw": {**unknown, "consumers": 1},
            "news.triage": {**unknown, "consumers": 1},
            "news.deliver": {**unknown, "consumers": 1},
            "news.dead": dict(unknown),
        }
    )
    assert (level, title) == ("warn", "队列策略未知")


def test_status_funnel_reads_the_single_event_cohort() -> None:
    inputs = _status_inputs()
    inputs["pipeline"] = {
        **inputs["pipeline"],
        "funnel_received_24h": 12,
        "funnel_admitted_24h": 9,
        "funnel_triaged_24h": 8,
        "funnel_delivered_24h": 3,
    }
    out = status_health(**inputs)  # type: ignore[arg-type]
    assert {stage: out["funnel_24h"][stage] for stage in ("received", "admitted", "triaged", "delivered")} == {
        "received": 12,
        "admitted": 9,
        "triaged": 8,
        "delivered": 3,
    }


def test_status_health_does_not_fall_back_to_throughput_counts() -> None:
    inputs = _status_inputs()
    inputs["pipeline"] = {
        "events_24h": 200,
        "admitted_24h": 150,
        "triage_24h": 150,
        "triage_degraded_24h": 3,
    }
    out = status_health(**inputs)  # type: ignore[arg-type]

    assert out["health"]["model"]["summary_zh"] == "24 小时内没有送审事件"
    assert {stage: out["funnel_24h"][stage] for stage in ("received", "admitted", "triaged", "delivered")} == {
        "received": 0,
        "admitted": 0,
        "triaged": 0,
        "delivered": 0,
    }


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
    assert off["health"]["delivery"]["detail_zh"] == "news.push 未启用、配置无效或 Workers 未运行"
    assert off["health"]["overall"] == "bad"


def test_timeline_exposes_only_the_current_reader_headline() -> None:
    verdict = {"headline_zh": "币安上线 XYZ", "direction": "bullish", "magnitude": 2}
    _, steps = event_timeline(event=_event(), members=[], verdicts=[_triage("push", verdict=verdict)], deliveries=[])
    facts = steps[2]["facts"]
    assert facts["headline_zh"] == "币安上线 XYZ"
    assert set(facts).isdisjoint({"event" + "_type", "action" + "able", "model" + "_decision", "title" + "_zh"})


def test_closed_pending_recovery_keeps_news_status_degraded() -> None:
    inputs = _status_inputs()
    inputs["ingest"] = {
        **inputs["ingest"],
        "recovery": {
            "pending_count": 2,
            "oldest_opened_at_ms": NOW - 20 * 60_000,
            "last_error_code": "opennews_history_rate_limited",
            "reason": "recovery_transient",
        },
    }
    out = status_health(**inputs)  # type: ignore[arg-type]
    assert out["health"]["overall"] == "warn"
    assert out["health"]["ingest"] == {
        "level": "warn",
        "summary_zh": "历史补抄待恢复 2 个事故窗口",
        "detail_zh": "最早事故 20 分钟前 · opennews_history_rate_limited",
    }
