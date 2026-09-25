"""Research filters and source links cross the real PostgreSQL-to-HTTP seam (#621)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from tests.helpers.prepared_signal_v3 import prepared_v3_signal
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings
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
                prepared_v3_signal(
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


def test_analysis_case_list_exposes_action_publication_and_source_identity(tmp_path) -> None:
    """The #683 trigger payload owns the OI Item link; a shadow TRADE is not a published Signal."""

    now = int(time.time() * 1000)
    source_item_id = "f" * 64
    trigger_id = "a" * 64
    case_id = "research-agent-shadow"
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            conn.execute(
                """INSERT INTO trading_triggers
                   (trigger_id,kind,source_fact_key,source_revision,payload_sha256,payload,
                    target_selection,first_visible_at_ms,source_observed_at_ms,root_expires_at_ms,created_at_ms)
                   VALUES (%s,'oi',%s,'revision-1',%s,%s::jsonb,'{}'::jsonb,%s,%s,%s,%s)""",
                (
                    trigger_id,
                    "oi:research-shadow",
                    "b" * 64,
                    json.dumps({"evidence_ref": source_item_id}),
                    now,
                    now,
                    now + 60_000,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO trading_cases
                   (case_id,underlying_key,trigger_kind,primary_source_key,manifest,manifest_sha256,
                    state,policy_decision,policy_reason,observed_at_ms,created_at_ms,decided_at_ms,
                    updated_at_ms,trigger_id,analysis_status)
                   VALUES (%s,'crypto:BTC','oi','oi:research-shadow','{}'::jsonb,%s,
                           'DONE','short','analysis_complete',%s,%s,%s,%s,%s,'done')""",
                (case_id, "c" * 64, now, now, now, now, trigger_id),
            )
            conn.execute(
                """INSERT INTO trading_case_decisions
                   (case_id,decision_id,policy_id,policy_version,input_ref,action,decision,
                    publish_status,decided_at_ms,valid_until_ms)
                   VALUES (%s,%s,'trade_assessment','v4','research-evidence','TRADE',
                           '{"side":"short","decision_version":"trade_decision_v4"}'::jsonb,'unpublished',%s,%s)""",
                (case_id, "d" * 64, now, now + 60_000),
            )
    finally:
        conn.close()

    settings = Settings(ws_token="research-test-token", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    with TestClient(create_app(settings=settings)) as client:
        auth = {"Authorization": "Bearer research-test-token"}
        listed = client.get("/api/trading/cases", headers=auth, params={"view": "list", "state": "DONE"})
        assert listed.status_code == 200, listed.text
        data = listed.json()["data"]
        assert {"action": "TRADE", "publish_status": "unpublished", "count": 1} in data["decision_counts_24h"]
        row = next(row for row in data["cases"] if row["case_id"] == case_id)
        assert (row["analysis_action"], row["analysis_publish_status"], row["analysis_side"]) == (
            "TRADE",
            "unpublished",
            "short",
        )
        assert row["source_item_id"] == source_item_id
        linked = client.get("/api/trading/cases", headers=auth, params={"source_item_id": source_item_id})
        assert linked.status_code == 200, linked.text
        assert [row["case_id"] for row in linked.json()["data"]["cases"]] == [case_id]
