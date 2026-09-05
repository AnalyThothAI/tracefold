"""One real broker-driven News path, from an OpenNews frame to the HTTP reader projection."""

from __future__ import annotations

import time
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from tracefold.news.program.runtime import PROGRAM_VERSION as SEMANTIC_PROGRAM_VERSION


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
    # No model is configured in the golden runtime, so the listing route fails open: the verdict is
    # degraded, carries the semantic Program identity it could not run, and still pushes. That is the
    # whole point of the frame -- it reaches a reader without a model, which is what makes this a
    # test of the broker/Workers/PostgreSQL/HTTP path rather than of a judgment.
    verdict = data["verdicts"][-1]
    assert verdict["program_version"] == SEMANTIC_PROGRAM_VERSION
    assert verdict["degraded"] is True
    assert verdict["error_code"] == "news_semantic_program_unconfigured"
    assert verdict["override_rule"] == "degraded_listing_objective"
    assert verdict["final_decision"] == "push"
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

    # Deliberately unlike the first frame in both ticker and wording. `listing` is an editorial kind
    # and takes the ordinary near-duplicate path, so two announcements differing in one word collapse
    # into one Event and the second frame would never open its own.
    title = "OKX schedules ZETAUSDT margin pair removal for 2026-09-11"
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


def test_an_oi_frame_crosses_production_workers_and_reaches_the_market_read(golden_runtime: Any) -> None:
    """#553. The market plane, through the same broker, Workers process and PostgreSQL as the Event one.

    Strategy 1019 is the deployed account's real traffic. The frame must become a stored observation
    with its parsed numbers, open no Event, and leave the editorial feed's counts exactly where they
    were -- which is the whole claim of the cut, measured end to end rather than at a storage seam.
    """

    title = "TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
    before = _feed_counts(golden_runtime)

    golden_runtime.publish_opennews(
        {
            "id": 2_850_900,
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

    group = _wait_for_market_group(golden_runtime, title=title)

    assert group["market_kind"] == "oi"
    latest = group["latest"]
    assert (latest["parse_status"], latest["parse_error"]) == ("parsed", None)
    assert (latest["symbol"], latest["raw_instrument"], latest["direction"]) == ("TRUMP", "TRUMP", "rise")
    assert (latest["oi_change_bps"], latest["oi_value_usd"]) == (455, 32_170_000)
    assert (latest["whale_long_profit_bps"], latest["whale_oi_ratio_bps"]) == (8_021, 10_071)
    assert latest["source_venue"] == "binance"
    assert latest["measurement_definition"] == "oi_signal_v1|opennews_oi_source_v1|300000"
    assert latest["historical"] is False
    # #553 PR-2 runs the notification loop in this same Workers process, against the same scripted
    # push sender the editorial path uses. The first observation of an OI group earns a first card and
    # that card is delivered, so the pair the reader sees is the send's own outcome -- reached with no
    # broker queue of the loop's own.
    notified = _wait_for_market_group(golden_runtime, title=title, notification_status="sent")
    assert notified["notification_reason"] == ""

    # No Event, and the editorial denominator is untouched.
    assert _feed_counts(golden_runtime) == before
    assert not any(event["leader_title"] == title for event in _feed_events(golden_runtime)), (
        "a market observation must not appear on the Event feed"
    )
    with psycopg.connect(golden_runtime.postgres_dsn, row_factory=dict_row) as conn:
        item_id = latest["item_id"]
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM news_events e"
                " JOIN news_event_members m ON m.event_id = e.event_id WHERE m.item_id = %s",
                (item_id,),
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM news_oi_signals WHERE source_item_id = %s", (item_id,)).fetchone()[
                "n"
            ]
            == 1
        )
        # One card, in the durable ledger the read model projects: the loop's to-do list is in
        # PostgreSQL, so what the reader was told is a row here rather than a counter somewhere.
        marker = conn.execute(
            "SELECT market_notify_state, market_notify_group_key, market_notify_delivery_key"
            " FROM news_items WHERE item_id = %s",
            (item_id,),
        ).fetchone()
        assert marker["market_notify_state"] == "processed"
        assert marker["market_notify_group_key"] and marker["market_notify_delivery_key"]
        delivery = conn.execute(
            "SELECT state, trigger_reason, attempts, covered_count, receipt, error"
            " FROM news_market_deliveries WHERE delivery_key = %s",
            (marker["market_notify_delivery_key"],),
        ).fetchone()
        assert (delivery["state"], delivery["trigger_reason"], delivery["attempts"]) == ("sent", "first", 1)
        assert (delivery["covered_count"], delivery["error"]) == (1, None)
        assert delivery["receipt"]["provider"] == "feishu"

    detail = httpx.get(
        f"{golden_runtime.base_url}/api/news/market/{latest['item_id']}",
        headers=golden_runtime.headers,
        timeout=5.0,
    )
    assert detail.status_code == 200
    body = detail.json()["data"]
    assert body["observation"]["item_id"] == latest["item_id"]
    assert body["provider_params"]["strategy"]["id"] == 1019
    assert [row["item_id"] for row in body["timeline"]] == [latest["item_id"]]


def _feed_events(golden_runtime: Any) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{golden_runtime.base_url}/api/news/feed",
        headers=golden_runtime.headers,
        timeout=5.0,
    )
    response.raise_for_status()
    return [dict(event) for event in response.json()["data"]["events"]]


def _feed_counts(golden_runtime: Any) -> dict[str, Any]:
    response = httpx.get(
        f"{golden_runtime.base_url}/api/news/feed",
        headers=golden_runtime.headers,
        timeout=5.0,
    )
    response.raise_for_status()
    return dict(response.json()["data"]["counts"])


def _wait_for_market_group(
    golden_runtime: Any, *, title: str, notification_status: str | None = None
) -> dict[str, Any]:
    """The group as the reader sees it, optionally once its notification pair has settled.

    #553 PR-2 runs the notification loop inside the same Workers process, so the pair moves on its own
    while this test polls. Waiting for the named status is what keeps the assertion about the loop's
    result rather than about which tick the HTTP read happened to land between.
    """

    deadline = time.monotonic() + 30.0
    last_body = ""
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{golden_runtime.base_url}/api/news/market",
            headers=golden_runtime.headers,
            timeout=5.0,
        )
        last_body = response.text
        if response.status_code == 200:
            for group in response.json()["data"]["groups"]:
                if group["latest"]["title"] != title:
                    continue
                if notification_status is None or group["notification_status"] == notification_status:
                    return dict(group)
        time.sleep(0.1)
    raise AssertionError(f"market observation never reached the HTTP market read: {last_body}")
