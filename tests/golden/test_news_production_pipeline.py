"""One real broker-driven News path, from an OpenNews frame to the HTTP reader projection."""

from __future__ import annotations

import time
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row


def test_opennews_frame_crosses_production_workers_and_reaches_the_reader(golden_runtime: Any) -> None:
    """RabbitMQ, production Workers wiring and PostgreSQL must all participate in this result.

    The frame is an exchange listing announcement. #458 stopped the OI lane from pushing and #553
    took the liquidation lane out of the editorial plane entirely -- a liquidation is a stored market
    observation now and opens no Event, so neither can carry a test whose subject is "a frame reaches
    the reader". `listing_deterministic` is what remains: it is admitted, it is judged without a
    model, and its degraded verdict pushes, which is the whole path this test exists to cross.
    """

    title = "Binance will list ACMEUSDT perpetual futures on 2026-09-08"
    golden_runtime.publish_opennews(
        {
            "id": 2_850_001,
            "newsType": "strategy",
            "engineType": "listing",
            "text": title,
            "source": "binance",
            "coins": [],
            "ts": int(time.time() * 1_000),
            "strategy": {
                "id": 1353,
                "name": "Listing and Delisting Announcements",
                "engineType": "listing",
                "sourceType": "news",
            },
        }
    )

    event = _wait_for_event(golden_runtime, title=title)
    data = _wait_for_complete_detail(golden_runtime, event_id=event["event_id"])

    assert data["event"]["admission"] == "listing_deterministic"
    assert data["event"]["published_at_ms"] is not None
    assert data["members"] and data["members"][0]["reporting_origin"] == "binance"
    assert data["verdicts"][-1]["program_version"] == "news_liquidation_fact_v2"
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


def test_triage_publish_failure_uses_broker_retry_without_restarting_workers(golden_runtime: Any) -> None:
    """The production Triage/outbox path ends in evidence, while the same Workers root stays live."""

    title = "Binance will delist ACMEUSDT perpetual futures on 2026-09-09"
    initial_readiness = golden_runtime.workers_readiness()

    golden_runtime.set_verdict_route(enabled=False)
    started = time.monotonic()
    try:
        golden_runtime.publish_opennews(
            {
                "id": 2_850_440,
                "newsType": "strategy",
                "engineType": "listing",
                "text": title,
                "source": "binance",
                "coins": [],
                "ts": int(time.time() * 1_000),
                "strategy": {
                    "id": 1353,
                    "name": "Listing and Delisting Announcements",
                    "engineType": "listing",
                    "sourceType": "news",
                },
            }
        )
        event = _wait_for_event(golden_runtime, title=title)
        pending = _wait_for_verdict(golden_runtime, event_id=str(event["event_id"]), published=False)
        assert pending["final_decision"] == "push"

        during_retry = golden_runtime.workers_readiness()
        assert during_retry["runtime_id"] == initial_readiness["runtime_id"]
        assert during_retry["process_id"] == initial_readiness["process_id"]

        # A separate queue still reaches its own terminal settlement while Triage is held in the
        # native retry window. This proves the process did not merely keep a probe alive after losing
        # the business consumers.
        golden_runtime.publish_raw_probe(message_id="raw:broker-handler-peer")
        golden_runtime.wait_for_queue_depth("news.dead", 1, timeout=10.0)

        # delivery-limit=2 means three Triage attempts. The two broker-native 30 s waits must elapse
        # before its Event message joins the peer probe in the dead-letter queue.
        golden_runtime.wait_for_queue_depth("news.dead", 2, timeout=100.0)
        assert time.monotonic() - started >= 50.0
        after_terminal = golden_runtime.workers_readiness()
        assert after_terminal["runtime_id"] == initial_readiness["runtime_id"]
        assert after_terminal["process_id"] == initial_readiness["process_id"]
    finally:
        golden_runtime.set_verdict_route(enabled=True)

    dead = golden_runtime.dead_letters(limit=5)
    assert {row["message_id"] for row in dead} == {
        "raw:broker-handler-peer",
        f"event:{event['event_id']}",
    }
    triage_dead = next(row for row in dead if row["message_id"] == f"event:{event['event_id']}")
    assert triage_dead["reason"] == "delivery_limit"
    assert triage_dead["delivery_count"] == 3

    detail = _wait_for_complete_detail(golden_runtime, event_id=str(event["event_id"]), timeout=90.0)
    assert len(detail["verdicts"]) == 1
    assert len(detail["deliveries"]) == 1
    assert detail["deliveries"][0]["state"] == "sent"
    repaired = _wait_for_verdict(golden_runtime, event_id=str(event["event_id"]), published=True)
    assert repaired["published_at_ms"] is not None

    after_janitor = golden_runtime.workers_readiness()
    assert after_janitor["runtime_id"] == initial_readiness["runtime_id"]
    assert after_janitor["process_id"] == initial_readiness["process_id"]


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


def _wait_for_complete_detail(golden_runtime: Any, *, event_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
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


def _wait_for_verdict(golden_runtime: Any, *, event_id: str, published: bool) -> dict[str, Any]:
    deadline = time.monotonic() + 30.0
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        with psycopg.connect(golden_runtime.postgres_dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT final_decision, published_at_ms FROM news_verdicts WHERE event_id = %s AND stage = 'triage'",
                (event_id,),
            ).fetchone()
        last = dict(row) if row is not None else None
        if last is not None and (last["published_at_ms"] is not None) is published:
            return last
        time.sleep(0.1)
    raise AssertionError(f"verdict did not reach published={published}: {last}")
