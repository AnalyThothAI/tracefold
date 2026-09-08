"""Research filters and source links cross the real PostgreSQL-to-HTTP seam (#621)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.execution_stream import prepare_trade_signal
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_filtered_decisions_page_past_the_old_limit_and_link_by_saved_identity(tmp_path) -> None:
    now = int(time.time() * 1000)
    source = "a" * 64
    old_at = now - 3 * 86_400_000
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            for index in range(126):
                stamp = old_at if index == 125 else now - 1000
                manifest = {"contexts": {"oi": {"source_item_id": source if index == 125 else "b" * 64}}}
                conn.execute(
                    """INSERT INTO trading_cases (
                       case_id, underlying_key, trigger_kind, primary_source_key, manifest, manifest_sha256,
                       state, policy_decision, policy_reason, observed_at_ms,
                       created_at_ms, decided_at_ms, updated_at_ms
                    ) VALUES (%s, 'crypto:BTC', 'oi', %s, %s::jsonb, %s,
                              'NO_TRADE', 'no_trade', 'test_floor', %s, %s, %s, %s)""",
                    (
                        f"research-{index:03}",
                        f"research-source-{index}",
                        json.dumps(manifest),
                        "c" * 64,
                        stamp,
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
            conn.execute(
                "UPDATE trading_cases SET state='SIGNAL_EMITTED', policy_decision='long' WHERE case_id='research-125'"
            )
            TradingRepository(conn).append_trade_signal(
                prepare_trade_signal(
                    signal_id="d" * 64,
                    case_id="research-125",
                    market_key="crypto:perp:BTC:USDT",
                    direction="long",
                    observed_at_ns=old_at * 1_000_000,
                    expires_at_ns=(old_at + 60_000) * 1_000_000,
                )
            )
    finally:
        conn.close()
    settings = Settings(ws_token="research-test-token", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    with TestClient(create_app(settings=settings)) as client:
        auth = {"Authorization": "Bearer research-test-token"}
        params = {"view": "list", "state": "NO_TRADE", "asset": "BTC", "reason": "test_floor", "limit": 25}
        seen: list[str] = []
        while True:
            response = client.get("/api/trading/cases", headers=auth, params=params)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["total"] == 125
            seen.extend(row["case_id"] for row in data["cases"])
            if not data["next_cursor"]:
                break
            params["cursor"] = data["next_cursor"]
            wrong = client.get("/api/trading/cases", headers=auth, params={**params, "asset": "ETH"})
            assert wrong.status_code == 400
        assert seen == [f"research-{i:03}" for i in range(124, -1, -1)]
        linked = client.get("/api/trading/cases", headers=auth, params={"source_item_id": source}).json()["data"]
        assert [row["case_id"] for row in linked["cases"]] == ["research-125"]
        assert linked["cases"][0]["source_item_id"] == source
        assert linked["window_from_ms"] == 0
        unmatched = client.get("/api/trading/cases", headers=auth, params={"source_item_id": "e" * 64})
        assert unmatched.json()["data"]["cases"] == []
        executions = client.get("/api/trading/executions", headers=auth, params={"case_id": "research-125"})
        assert executions.status_code == 200, executions.text
        assert [row["entry_id"] for row in executions.json()["data"]["executions"]] == ["d" * 64]
        assert client.get("/api/trading/executions", headers=auth).json()["data"]["executions"] == []
