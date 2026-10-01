"""Outcome / vocabulary / timeline / health: the one conclusion per Event and the thresholded status page (pure)."""

from __future__ import annotations

from tracefold.news.health import status_health
from tracefold.news.outcome import (
    admission_zh,
    delivery_error_zh,
    storyline_key_zh,
)
from tracefold.news.timeline import event_timeline

NOW = 1_800_000_000_000


def test_unexpected_delivery_error_copy_is_provider_neutral() -> None:
    assert delivery_error_zh("news_delivery_failed:ProviderError") == "推送失败（ProviderError）"


def test_current_admission_and_storyline_labels() -> None:
    assert admission_zh("candidate") == "已送审"
    assert storyline_key_zh("conflict:mideast_2026") == "美伊冲突" and storyline_key_zh("asset:BTC") == "BTC"
    assert storyline_key_zh("none") == "无线索" and storyline_key_zh("actor:boj") == "日本央行"


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


def test_timeline_for_a_recovery_event_stops_at_the_gate() -> None:
    outcome, steps = event_timeline(
        event=_event(admission="recovery", ingest_mode="recovery", published_at_ms=None, member_count=1),
        members=[],
        deliveries=[],
    )
    assert outcome.kind == "held_recovery"
    assert [s["stage"] for s in steps] == ["received", "gate"]
    assert "断线补抄" in steps[0]["summary_zh"]


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
                "news.dead": _queue(),
            },
        },
        "pipeline": {
            "admitted_24h": 150,
            "events_1h": 10,
            "events_24h": 200,
            "candidates_24h": 150,
            "funnel_received_24h": 200,
            "funnel_admitted_24h": 150,
            "funnel_adopted_24h": 142,
            "funnel_delivered_24h": 19,
            "selected_24h": 20,
            "decision_actions_24h": {"notify": 20, "no_notification": 60},
            "semantic_observations_24h": 147,
            "semantic_failed_24h": 3,
            "semantic_failed_by_code_24h": {"news_provider_unavailable:TimeoutError": 3},
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
        "adopted": 142,
        # #87: how many of the same Events named an asset that exists on a venue. It sits between "sent to
        # the model" and "decided" because that is where the reader asks it, not because it is a stage.
        # `tagged` travels with it: it is the only population `grounded` can be compared against.
        "tagged": 150,
        "grounded": 144,
        "selected": 20,
        "delivered": 19,
        "received_1h": 10,
        "delivered_1h": 2,
    }
    reasons = out["reasons_24h"]
    assert reasons[0] == {
        "stage": "decision",
        "key": "no_notification",
        "label_zh": "no_notification",
        "count": 60,
    }
    assert {r["stage"] for r in reasons} == {"decision", "ungrounded"}
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
            "news.dead": _queue(),
        }
    )
    assert (level, title) == ("bad", "死信投递被卡住")


def test_broker_health_warns_before_a_queue_reaches_its_byte_bound() -> None:
    warned, warned_title = _broker_health(
        **{
            "news.raw": _queue(messages=10, consumers=1, bytes_used_bps=5_200),
            "news.triage": _queue(consumers=1),
            "news.dead": _queue(),
        }
    )
    assert warned == "warn" and "字节额度" in warned_title
    bad, bad_title = _broker_health(
        **{
            "news.raw": _queue(messages=10, consumers=1, bytes_used_bps=9_100),
            "news.triage": _queue(consumers=1),
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
            "news.dead": dict(unknown),
        }
    )
    assert (level, title) == ("warn", "队列策略未知")


def test_broker_health_warns_when_only_one_queue_is_missing_from_the_management_rows() -> None:
    """A management API that answered about two queues has said nothing about the third.

    Two verified policies prove nothing about the delivery the third queue governs, so a partial
    answer is unknown, not healthy — and the warning names the queue nobody can vouch for.
    """

    item = status_health(
        **{  # type: ignore[arg-type]
            **_status_inputs(),
            "broker": {
                "configured": True,
                "connected": True,
                "queues": {
                    "news.raw": _queue(consumers=1),
                    "news.triage": _queue(consumers=1, policy_ok=None, bytes_used_bps=None),
                    "news.dead": _queue(),
                },
            },
        }
    )["health"]["broker"]

    assert (item["level"], item["summary_zh"]) == ("warn", "队列策略未知")
    assert "news.triage" in str(item["detail_zh"])


def test_an_unverifiable_policy_is_not_hidden_behind_the_standing_dead_letter_count() -> None:
    """`news.dead` is rarely empty in production, so its warning must not outrank an unknown contract."""

    level, title = _broker_health(
        **{
            "news.raw": _queue(consumers=1, policy_ok=None, bytes_used_bps=None),
            "news.triage": _queue(consumers=1),
            "news.dead": _queue(messages=38, ready=38),
        }
    )
    assert (level, title) == ("warn", "队列策略未知")


def test_status_funnel_reads_the_single_event_cohort() -> None:
    inputs = _status_inputs()
    inputs["pipeline"] = {
        **inputs["pipeline"],
        "funnel_received_24h": 12,
        "funnel_admitted_24h": 9,
        "funnel_adopted_24h": 8,
        "funnel_delivered_24h": 3,
    }
    out = status_health(**inputs)  # type: ignore[arg-type]
    assert {stage: out["funnel_24h"][stage] for stage in ("received", "admitted", "adopted", "delivered")} == {
        "received": 12,
        "admitted": 9,
        "adopted": 8,
        "delivered": 3,
    }


def test_status_health_does_not_fall_back_to_throughput_counts() -> None:
    inputs = _status_inputs()
    inputs["pipeline"] = {
        "events_24h": 200,
        "admitted_24h": 150,
        "semantic_observations_24h": 0,
    }
    out = status_health(**inputs)  # type: ignore[arg-type]

    assert out["health"]["model"]["summary_zh"] == "24 小时内没有语义处理"
    assert {stage: out["funnel_24h"][stage] for stage in ("received", "admitted", "adopted", "delivered")} == {
        "received": 0,
        "admitted": 0,
        "adopted": 0,
        "delivered": 0,
    }


def test_status_health_thresholds_turn_amber_and_red() -> None:
    degraded = status_health(
        **_status_inputs(
            pipeline={**_status_inputs()["pipeline"], "semantic_observations_24h": 120, "semantic_failed_24h": 30}
        )
    )  # type: ignore[arg-type]
    assert degraded["health"]["model"]["level"] == "bad" and "20%" in degraded["health"]["model"]["summary_zh"]
    assert degraded["health"]["overall"] == "bad"

    amber = status_health(**_status_inputs(pipeline={**_status_inputs()["pipeline"], "semantic_failed_24h": 8}))  # type: ignore[arg-type]
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


def test_source_only_timeline_keeps_the_gate_and_a_current_receipt() -> None:
    receipt = {"kind": "update", "state": "sent", "attempted_at_ms": NOW + 9_000, "settled_at_ms": NOW + 9_500}
    outcome, steps = event_timeline(event=_event(), members=[], deliveries=[receipt], intents=[receipt])
    assert outcome.kind == "delivered"
    assert [step["stage"] for step in steps] == ["received", "gate", "delivery"]
    assert steps[-1]["facts"]["state"] == "sent"
    source_only, source_steps = event_timeline(event=_event(), members=[], deliveries=[])
    assert source_only.kind == "no_update"
    assert [step["stage"] for step in source_steps] == ["received", "gate"]


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


def test_a_failed_semantic_revision_is_a_named_parse_failure_and_keeps_the_model_amber() -> None:
    # #742 S4: the console names the failure with its code; a low failure share still raises it.
    from tracefold.news.outcome import event_outcome

    outcome = event_outcome(
        admission="candidate",
        delivery=None,
        semantic={
            "wanted_revision": 2,
            "done_revision": 1,
            "last_outcome": "failed",
            "last_error_code": "news_generation_output_truncated",
        },
    )
    assert (outcome.kind, outcome.text_zh) == ("semantic_failed", "解析失败")
    assert outcome.reason_zh == "模型输出被截断（news_generation_output_truncated）"
    pipeline = {**_status_inputs()["pipeline"], "semantic_failed_24h": 1, "semantic_failed_exhausted": 1}
    status = status_health(**_status_inputs(pipeline=pipeline))  # type: ignore[arg-type]
    assert status["health"]["model"]["level"] == "warn"
    assert "1 个事件解析失败待处理" in status["health"]["model"]["summary_zh"]


def test_cancellation_names_the_old_task_and_preserves_an_existing_delivery() -> None:
    from tracefold.news.outcome import event_outcome
    from tracefold.news.update_view import semantic_state

    semantic = {"wanted_revision": 2, "done_revision": 1, "last_outcome": "cancelled", "last_error_code": "old_error"}
    assert semantic_state(semantic) == "cancelled"
    cancelled = event_outcome(admission="candidate", delivery=None, semantic=semantic)
    assert cancelled.kind == "semantic_cancelled" and cancelled.group == "held"
    delivered = event_outcome(admission="candidate", delivery={"state": "sent"}, semantic=semantic, adopted=True)
    assert delivered.kind == "delivered" and delivered.group == "pushed"
