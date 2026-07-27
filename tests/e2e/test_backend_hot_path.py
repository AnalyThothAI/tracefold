from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import connect_postgres_test, prepare_postgres_database
from tests.support.db_seeds import (
    assert_count_at_least,
    hot_path_counts,
)
from tests.support.fake_providers import (
    FakeDexQuoteProvider,
    FakeGmgnUpstreamClient,
)
from tests.support.hot_path_runtime import (
    EVENT_ID,
    FIXED_NOW_MS,
    MARKET_TARGET_ID,
    MARKET_TARGET_TYPE,
    SYMBOL,
    WS_TOKEN,
    auth_headers,
    backend_hot_path_settings,
)
from tests.support.provider_fixtures import load_provider_fixture
from tracefold.app.http.app import create_app
from tracefold.app.provider_types import AssetMarketProviders
from tracefold.market import EventAnchorBackfillWorker, TokenRadarProjectionWorker


@pytest.mark.e2e
def test_complete_backend_hot_path_to_token_radar(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    e2e_postgres: str,
) -> None:
    monkeypatch.setenv("GMGN_TEST_POSTGRES_DSN", e2e_postgres)
    prepare_postgres_database()
    settings = backend_hot_path_settings(tmp_path)
    app = create_app(settings=settings, start_collector=False)
    frame = load_provider_fixture("gmgn_public_tw_complete.json")

    with TestClient(app) as client:
        runtime = client.app.state.service
        upstream = FakeGmgnUpstreamClient([frame], runtime.collector.handle_frame, received_at_ms=FIXED_NOW_MS)

        client.portal.call(upstream.run)
        _assert_counts(
            {
                "raw_frames": 1,
                "events": 1,
                "token_intents": 1,
                "token_intent_resolutions": 1,
                "enriched_events": 1,
                "event_anchor_jobs": 1,
            }
        )

        backfill_now_ms = _wall_now_ms()
        backfill_window_ms = max(
            30 * 24 * 60 * 60 * 1000,
            abs(backfill_now_ms - FIXED_NOW_MS) + 60_000,
        )
        backfill_settings = settings.workers.event_anchor_backfill.model_copy(
            update={
                "batch_size": 10,
                "concurrency": 2,
                "min_age_ms": 0,
                "max_anchor_lag_ms": backfill_window_ms,
            }
        )
        backfill_result = asyncio.run(
            EventAnchorBackfillWorker(
                pool_bundle=runtime.db,
                providers=AssetMarketProviders(
                    dex_quote_market=FakeDexQuoteProvider(observed_at_ms=FIXED_NOW_MS + 500),
                    cex_market=None,
                ),
                settings=backfill_settings,
                clock=lambda: backfill_now_ms,
            ).run_once()
        )
        assert backfill_result.processed == 1
        _assert_counts({"market_ticks": 1, "ready_enriched_events": 1})

        radar_result = asyncio.run(
            TokenRadarProjectionWorker(
                name="token_radar_projection",
                settings=runtime.settings.workers.token_radar_projection,
                db=runtime.db,
                telemetry=runtime.telemetry,
            ).run_once(now_ms=FIXED_NOW_MS + 2_000)
        )
        assert radar_result.notes["rows_written"] >= 1
        _assert_counts({"token_radar_current_rows": 1})

        _assert_http_surfaces(client)
        _assert_websocket_surfaces(client, runtime)


def _assert_counts(expected_minimums: dict[str, int]) -> dict[str, int]:
    conn = connect_postgres_test(read_only=False)
    try:
        counts = hot_path_counts(conn, event_id=EVENT_ID)
    finally:
        conn.close()
    for name, minimum in expected_minimums.items():
        assert_count_at_least(counts, name, minimum)
    return counts


def _assert_http_surfaces(client: TestClient) -> None:
    ready = client.get("/readyz")
    assert ready.status_code == 200, ready.text
    assert ready.json()["ok"] is True

    metrics = client.get("/metrics")
    assert metrics.status_code == 200, metrics.text
    assert "tracefold_db_pool_wait_ms" in metrics.text

    recent = client.get("/api/recent", params={"limit": 10}, headers=auth_headers())
    assert recent.status_code == 200, recent.text
    assert EVENT_ID in json.dumps(recent.json(), default=str)

    radar = client.get("/api/token-radar", params={"window": "1h", "limit": 10}, headers=auth_headers())
    assert radar.status_code == 200, radar.text
    radar_text = json.dumps(radar.json(), default=str)
    assert SYMBOL in radar_text


def _assert_websocket_surfaces(client: TestClient, runtime: Any) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": WS_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "subscribe", "symbols": [SYMBOL], "replay": 10})
        replay = _receive_matching(ws, lambda msg: msg.get("type") == "event")
        assert replay["event"]["event_id"] == EVENT_ID

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": WS_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json(
            {
                "type": "subscribe",
                "market_targets": [{"target_type": MARKET_TARGET_TYPE, "target_id": MARKET_TARGET_ID}],
                "replay": 0,
            }
        )
        client.portal.call(
            runtime.hub.publish,
            {
                "type": "live_market_update",
                "target_type": MARKET_TARGET_TYPE,
                "target_id": MARKET_TARGET_ID,
                "price_usd": 0.129,
            },
        )
        market_update = ws.receive_json()
        assert market_update["type"] == "live_market_update"
        assert market_update["target_id"] == MARKET_TARGET_ID


def _receive_matching(ws: Any, predicate: Any) -> dict[str, Any]:
    for _ in range(10):
        message = ws.receive_json()
        if predicate(message):
            return message
    raise AssertionError("websocket did not receive matching message")


def _wall_now_ms() -> int:
    return int(time.time() * 1000)
