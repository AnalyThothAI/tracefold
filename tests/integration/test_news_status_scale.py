"""`/api/news/status` capacity contract for the production-sized verdict trace corpus (#221)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage, prepare_postgres_database
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import NewsSettings, Settings

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.usefixtures("postgres_dsn")]

VERDICTS = 1_948
TRACE_CHUNKS = 400


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(ws_token="secret", news=NewsSettings(), storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    return settings


def _seed_production_sized_trace_corpus(*, now_ms: int) -> None:
    """Seed the 24 h production shape: 1,948 verdicts and roughly 26 MB of TOASTed trace JSON."""

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
            (now_ms, now_ms, now_ms, now_ms, VERDICTS),
        )
        conn.execute(
            """
            INSERT INTO news_events (
              event_id, leader_item_id, family, event_kind, comparison_fingerprint, comparison_title,
              leader_title, focus_fact_id, opened_at_ms, last_member_at_ms, expires_at_ms, admission,
              storyline_key, ingest_mode, created_at_ms, updated_at_ms
            )
            SELECT 'status-event-' || g, 'status-item-' || g, 'general', 'news', 'status-fingerprint-' || g,
                   'comparison', 'leader ' || g, 'fact:' || g, %s, %s, %s + 3600000,
                   'candidate', 'asset:STATUS' || g, 'live', %s, %s
              FROM generate_series(1, %s) AS g
            """,
            (now_ms, now_ms, now_ms, now_ms, now_ms, VERDICTS),
        )
        conn.execute(
            """
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, rule_baseline_decision, final_decision,
              verdict, degraded, trace, created_at_ms
            )
            SELECT 'status-event-' || g, 'triage', 'v6', 'drop', 'drop', '{}'::jsonb, false,
                   jsonb_build_object(
                     'latency_ms', g::double precision + 0.125,
                     'queue_lag_ms', g::double precision * 2 + 0.25,
                     'reasked_after_told_change', g %% 10 = 0,
                     'novelty_defaulted', g %% 20 = 0,
                     'payload', (
                       SELECT string_agg(md5(g::text || ':' || chunk::text), '')
                         FROM generate_series(1, %s) AS chunk
                     )
                   ),
                   %s
              FROM generate_series(1, %s) AS g
            """,
            (TRACE_CHUNKS, now_ms, VERDICTS),
        )
        conn.execute("ANALYZE news_events")
        conn.execute("ANALYZE news_verdicts")
        conn.commit()
    finally:
        conn.close()


def test_news_status_serves_a_production_sized_corpus_within_the_native_timeout(tmp_path: Path) -> None:
    """Exercise the real query; shared-runner wall time is diagnostic, not correctness."""

    prepare_postgres_database()
    now_ms = int(time.time() * 1_000)
    _seed_production_sized_trace_corpus(now_ms=now_ms)
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/api/news/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    assert {
        key: response.json()["data"]["pipeline"][key] for key in ("triage_p50_ms", "triage_p95_ms", "queue_lag_p95_ms")
    } == {
        "triage_p50_ms": 974.625,
        "triage_p95_ms": 1850.7749999999999,
        "queue_lag_p95_ms": 3701.5499999999997,
    }
