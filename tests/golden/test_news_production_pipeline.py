"""One real broker-driven News path, from an OpenNews frame to the HTTP reader projection."""

from __future__ import annotations

import time
from typing import Any

import httpx


def test_opennews_frame_crosses_production_workers_and_reaches_the_reader(golden_runtime: Any) -> None:
    """RabbitMQ, production Workers wiring and PostgreSQL must all participate in this result."""

    title = "BTC OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
    golden_runtime.publish_opennews(
        {
            "id": 2_850_001,
            "newsType": "strategy",
            "engineType": "market",
            "text": title,
            "source": "binance",
            "coins": [],
            "ts": int(time.time() * 1_000),
            "strategy": {
                "id": 1019,
                "name": "OI Event Monitor",
                "engineType": "market",
                "sourceType": "market",
            },
        }
    )

    event = _wait_for_event(golden_runtime, title=title)
    data = _wait_for_complete_detail(golden_runtime, event_id=event["event_id"])

    assert data["event"]["admission"] == "telemetry_deterministic"
    assert data["event"]["published_at_ms"] is not None
    assert data["members"] and data["members"][0]["reporting_origin"] == "binance"
    assert data["verdicts"][-1]["program_version"] == "news_oi_signal_v2"
    assert data["verdicts"][-1]["final_decision"] == "push"
    assert len(data["deliveries"]) == 1
    delivery = data["deliveries"][0]
    assert (delivery["kind"], delivery["state"], delivery["error_code"]) == (
        "first",
        "sent",
        None,
    )
    assert delivery["card"]["elements"]
    assert delivery["receipt"] == {"provider": "feishu", "code": 0, "status_code": 200}
    assert data["reader_receipt"]["state"] == "received"
    # #400: the final topology is three business queues and the dead-letter queue. There is no retry
    # lane left to drain, so an empty pipeline is exactly these four names at zero.
    assert golden_runtime.queue_depths() == {
        "news.raw": 0,
        "news.triage": 0,
        "news.deliver": 0,
        "news.dead": 0,
    }


def _wait_for_event(golden_runtime: Any, *, title: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30.0
    last_body = ""
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{golden_runtime.base_url}/api/news/feed",
            headers=golden_runtime.headers,
            timeout=5.0,
        )
        last_body = response.text
        if response.status_code == 200:
            for event in response.json()["data"]["events"]:
                if event["leader_title"] == title:
                    return dict(event)
        time.sleep(0.1)
    raise AssertionError(f"golden Event never reached the HTTP feed: {last_body}")


def _wait_for_complete_detail(golden_runtime: Any, *, event_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30.0
    last_body = ""
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{golden_runtime.base_url}/api/news/events/{event_id}",
            headers=golden_runtime.headers,
            timeout=5.0,
        )
        last_body = response.text
        if response.status_code == 200:
            data = response.json()["data"]
            deliveries = data["deliveries"]
            if data["verdicts"] and deliveries:
                state = deliveries[0]["state"]
                if state == "terminal" or (state == "sent" and data.get("reader_receipt") is not None):
                    return dict(data)
        time.sleep(0.1)
    raise AssertionError(f"golden Event detail never reached a terminal delivery: {last_body}")
