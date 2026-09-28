"""`/api/news/status` capacity contract for the current Event evidence corpus."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage, seed_current_news_evidence
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import NewsSettings, Settings

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.usefixtures("postgres_clone_dsn")]

EVENTS = 1_948


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(ws_token="secret", news=NewsSettings(), storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    return settings


def _seed_production_sized_event_corpus(*, now_ms: int) -> None:
    """Seed 1,948 Events with the current observed evidence contract."""

    conn: Any = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
              provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
            )
            SELECT 'status-item-' || g, 'opennews', 'status-key-' || g, 'headline ' || g,
                   %s, %s, '{}'::jsonb, 'live', %s, %s
              FROM generate_series(1, %s) AS g
            """,
            (now_ms, now_ms, now_ms, now_ms, EVENTS),
        )
        conn.execute(
            """
            INSERT INTO news_events (
              event_id, leader_item_id, dedupe_family, event_kind, comparison_fingerprint, comparison_title,
              leader_title, focus_fact_id, focus_fact_text, focus_fact_method,
              opened_at_ms, last_member_at_ms, expires_at_ms, admission,
              storyline_key, ingest_mode, created_at_ms, updated_at_ms
            )
            SELECT 'status-event-' || g, 'status-item-' || g, 'general', 'news', 'status-fingerprint-' || g,
                   'comparison', 'leader ' || g, 'fact:' || g, 'leader ' || g, 'whole_item',
                   %s, %s, %s + 3600000,
                   'candidate', 'asset:STATUS' || g, 'live', %s, %s
              FROM generate_series(1, %s) AS g
            """,
            (now_ms, now_ms, now_ms, now_ms, now_ms, EVENTS),
        )
        seed_current_news_evidence(conn)
        conn.execute("ANALYZE news_events")
        conn.commit()
    finally:
        conn.close()


def test_news_status_serves_a_production_sized_current_event_corpus(tmp_path: Path) -> None:
    """Exercise the real query; shared-runner wall time is diagnostic, not correctness."""

    now_ms = int(time.time() * 1_000)
    _seed_production_sized_event_corpus(now_ms=now_ms)
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/api/news/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    pipeline = response.json()["data"]["pipeline"]
    assert pipeline["events_24h"] == EVENTS
    assert pipeline["candidates_24h"] == EVENTS
    assert pipeline["funnel_admitted_24h"] == EVENTS
    assert pipeline["funnel_adopted_24h"] == 0
    assert pipeline["decisions_24h"] == 0
    assert "triage_p50_ms" not in pipeline
