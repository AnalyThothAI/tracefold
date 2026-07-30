from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.integration.test_token_radar_idempotency import (
    _run_radar_projection,
)
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
from tracefold.app.bootstrap import _PooledIngestStore
from tracefold.app.database import WorkerDatabase
from tracefold.app.http.app import create_app
from tracefold.app.provider_types import AssetMarketProviders
from tracefold.app.runtime_resources import (
    ProviderGovernor,
    RuntimeResources,
)
from tracefold.app.worker_manifest import worker_names
from tracefold.app.worker_runtime_status import (
    WorkerRuntimeStatusRepository,
)
from tracefold.market import CollectorService, EventAnchorBackfillWorker
from tracefold.platform.observability import TelemetryRegistry


@pytest.mark.e2e
def test_complete_backend_hot_path_to_token_radar(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    e2e_postgres: str,
) -> None:
    monkeypatch.setenv("GMGN_TEST_POSTGRES_DSN", e2e_postgres)
    prepare_postgres_database()
    settings = backend_hot_path_settings(tmp_path)
    app = create_app(settings=settings)
    frame = load_provider_fixture("gmgn_public_tw_complete.json")
    worker_db = WorkerDatabase.create(settings)
    resources = RuntimeResources()
    governor = ProviderGovernor()
    telemetry = TelemetryRegistry()
    providers = AssetMarketProviders(
        dex_quote_market=FakeDexQuoteProvider(observed_at_ms=FIXED_NOW_MS + 500),
        cex_market=None,
    )
    collector = CollectorService(
        name="collector",
        settings=settings.workers.collector,
        db=worker_db,
        telemetry=telemetry,
        store=_PooledIngestStore(
            worker_db,
            providers=providers,
            event_anchor_active_window_ms=(settings.workers.event_anchor_capture.active_window_ms),
        ),
        upstream_client=None,
    )
    collector.bind_runtime_id(str(uuid4()))
    collector.bind_runtime_resources(resources)
    collector.bind_provider_governor(governor)
    upstream = FakeGmgnUpstreamClient(
        [frame],
        collector.handle_frame,
        received_at_ms=FIXED_NOW_MS,
    )
    try:
        asyncio.run(upstream.run())
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
        backfill_settings = settings.workers.event_anchor_capture.model_copy(
            update={
                "batch_size": 10,
                "min_age_ms": 0,
                "max_anchor_lag_ms": backfill_window_ms,
            }
        )
        backfill = EventAnchorBackfillWorker(
            name="event_anchor_capture",
            pool_bundle=worker_db,
            providers=providers,
            settings=backfill_settings,
            clock=lambda: backfill_now_ms,
        )
        backfill.bind_runtime_id(str(uuid4()))
        backfill.bind_runtime_resources(resources)
        backfill.bind_provider_governor(governor)
        backfill_result = asyncio.run(backfill.run_once())
        assert backfill_result.processed == 1
        _assert_counts({"market_ticks": 1, "ready_enriched_events": 1})

        conn = connect_postgres_test(read_only=False)
        try:
            radar_result = _run_radar_projection(
                conn,
                window="1h",
                now_ms=FIXED_NOW_MS + 2_000,
            )
        finally:
            conn.close()
        assert radar_result["rows_written"] >= 1
        _assert_counts({"token_radar_current_rows": 1})

        _publish_healthy_worker_status(worker_db)
        with TestClient(app) as client:
            _assert_http_surfaces(client)
            _assert_websocket_surfaces(client, worker_db)
    finally:
        resources.close()
        asyncio.run(worker_db.aclose())


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


def _assert_websocket_surfaces(
    client: TestClient,
    worker_db: WorkerDatabase,
) -> None:
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
        payload = {
            "type": "live_market_update",
            "target_type": MARKET_TARGET_TYPE,
            "target_id": MARKET_TARGET_ID,
            "price_usd": 0.129,
        }
        with worker_db.worker_session("e2e_persisted_live") as repos, repos.transaction():
            repos.persisted_live.append(
                source_key=f"e2e-market:{time.time_ns()}",
                event_kind="live_market_update",
                target_type=MARKET_TARGET_TYPE,
                target_id=MARKET_TARGET_ID,
                payload=payload,
                committed_at_ms=_wall_now_ms(),
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


def _publish_healthy_worker_status(worker_db: WorkerDatabase) -> None:
    now_ms = _wall_now_ms()
    with worker_db.worker_session("e2e_runtime_status") as repos, repos.transaction():
        WorkerRuntimeStatusRepository(repos.conn).publish(
            runtime_id=str(uuid4()),
            runtime_version="e2e",
            statuses={
                name: {
                    "effective_status": "running",
                    "last_started_at_ms": now_ms,
                    "last_finished_at_ms": now_ms,
                    "last_result": None,
                    "last_error": None,
                    "deadline_at_ms": None,
                    "queue_depth": 0,
                    "oldest_due_at_ms": None,
                    "quarantine_count": 0,
                }
                for name in worker_names()
            },
            now_ms=now_ms,
        )
