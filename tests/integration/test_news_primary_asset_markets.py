"""The market guard metric counts adopted extraction assets from the News ledger."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage
from tests.support.news_update_pg import EVENT, seed_event, sql, store
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import NewsSettings, Settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def analysis(key, completed, assets, *, adopted=True, origin="semantic") -> None:
    sql(
        """INSERT INTO news_analyses
           (analysis_id, event_id, origin, input_revision, completed_at_ms, work_id, input_sha256,
            program_identity, understanding, repair, content_revision, update_ref, adopted_at_ms, document)
           VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb)""",
        (
            key,
            EVENT,
            origin,
            completed,
            key if origin == "semantic" else None,
            key if origin == "semantic" else None,
            "program-test" if origin == "semantic" else None,
            json.dumps({"claims": [{"fields": {"assets": assets}}]}) if origin == "semantic" else None,
            "{}" if origin == "scope_repair" else None,
            key if adopted else None,
            key if adopted else None,
            completed if adopted else None,
            "{}" if adopted else None,
        ),
    )


def test_market_distribution_uses_adopted_primary_occurrences_and_24h_window(tmp_path) -> None:
    now = int(time.time() * 1000)
    seed_event(at_ms=now)
    _pg, db, _clock = store()

    def primary(market):
        return {"symbol": "SAME", "market_type": market, "role": "primary"}

    analysis(
        "first",
        now - 100,
        [primary("crypto"), primary("unknown"), {"symbol": "OTHER", "market_type": "unknown", "role": "mentioned"}],
    )
    # A second adopted revision counts its own primary occurrences, even for the same symbol.
    analysis("second", now - 50, [primary("crypto"), primary("commodity"), primary("forex"), primary("fund")])
    analysis("old", now - 86_400_001, [primary("unknown")])
    analysis("future", now + 86_400_000, [primary("unknown")])
    analysis("unadopted", now - 25, [primary("unknown")], adopted=False)
    analysis("repair", now - 25, [], origin="scope_repair")
    result = asyncio.run(db.read("markets", lambda repos: repos.news.primary_asset_markets_24h(now_ms=now)))
    assert result == {
        "total": 6,
        "unknown": 2,
        "unknown_share": 0.3333,
        "by_market": {"crypto": 2, "commodity": 1, "fx": 1, "unknown": 2, "equity": 0, "index": 0, "pre_ipo": 0},
    }
    settings = Settings(ws_token="test", news=NewsSettings(), storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "home")
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/api/news/status", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert response.json()["data"]["primary_asset_markets_24h"] == result


def test_empty_sample_has_counts_but_no_unknown_share() -> None:
    _pg, db, clock = store()
    result = asyncio.run(db.read("markets", lambda repos: repos.news.primary_asset_markets_24h(now_ms=clock())))
    assert result["total"] == result["unknown"] == sum(result["by_market"].values()) == 0
    assert result["unknown_share"] is None
