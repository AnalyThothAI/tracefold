"""Real PostgreSQL to current read-only Trading HTTP seam."""

from __future__ import annotations

import time
from argparse import Namespace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.cli.commands import trading as trading_cli
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_selected_case_and_scoreboard_use_the_migrated_ledger(tmp_path, monkeypatch) -> None:
    now = int(time.time() * 1_000)
    case_id = "a" * 64
    trigger_id = "b" * 64
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO trading_inputs (input_id,kind,source_fact_key,source_revision,payload_sha256,"
                "payload,first_visible_at_ms,source_observed_at_ms,selected_asset_id,target_selection,"
                "exclusion_reason,received_at_ms) VALUES (%s,'oi','api-source','v1',%s,%s::jsonb,%s,%s,"
                "'crypto:SOL','{}'::jsonb,NULL,%s)",
                (trigger_id, "c" * 64, '{"evidence_ref":"api-source-item"}', now, now, now),
            )
            conn.execute(
                "INSERT INTO trading_cases (case_id,trigger_id,trigger_kind,asset_id,native_symbol,"
                "mapping_digest,created_at_ms,root_expires_at_ms,state,updated_at_ms) "
                "VALUES (%s,%s,'oi','crypto:SOL','SOLUSDT',%s,%s,%s,'pending',%s)",
                (case_id, trigger_id, "d" * 64, now, now + 600_000, now),
            )
    finally:
        conn.close()

    settings = Settings(ws_token="research-test-token", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    with TestClient(create_app(settings=settings)) as client:
        auth = {"Authorization": "Bearer research-test-token"}
        listing = client.get("/api/trading/cases", headers=auth)
        assert listing.status_code == 200, listing.text
        assert [row["case_id"] for row in listing.json()["data"]["cases"]] == [case_id]
        detail = client.get("/api/trading/cases", headers=auth, params={"case_id": case_id})
        assert detail.status_code == 200
        assert detail.json()["data"]["cases"][0]["asset_id"] == "crypto:SOL"
        since_ms, until_ms = now - 60_000, now + 60_000
        board = client.get("/api/trading/scoreboard", headers=auth, params={"since_ms": since_ms, "until_ms": until_ms})
        assert board.status_code == 200
        assert board.json()["data"]["funnel"]["triggers"] == 1
        assert board.json()["data"]["funnel"]["selected"] == 1
        monkeypatch.setattr(trading_cli, "load_settings", lambda **_kwargs: settings)
        code, cli_result = trading_cli.handle_trading(
            Namespace(
                trading_command="scoreboard",
                since=datetime.fromtimestamp(since_ms / 1_000, tz=UTC).isoformat(),
                until=datetime.fromtimestamp(until_ms / 1_000, tz=UTC).isoformat(),
                program=None,
            )
        )
        assert code == 0
        assert cli_result["data"] == board.json()["data"]
        linked = client.get("/api/trading/cases", headers=auth, params={"source_item_id": "api-source-item"})
        assert [row["case_id"] for row in linked.json()["data"]["cases"]] == [case_id]
        unrelated = client.get("/api/trading/cases", headers=auth, params={"source_item_id": "other-item"})
        assert unrelated.json()["data"]["cases"] == []
        assert (
            client.get(
                "/api/trading/cases", headers=auth, params={"case_id": case_id, "source_item_id": "api-source-item"}
            ).status_code
            == 400
        )
        assert client.get("/api/trading/cases").status_code == 401
