"""`/api/news/status` capacity contract for the production-sized verdict trace corpus (#221)."""

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
            (now_ms, now_ms, now_ms, now_ms, now_ms, VERDICTS),
        )
        seed_current_news_evidence(conn)
        conn.execute(
            """
            WITH payload AS (
              SELECT g,
                     jsonb_build_object(
                       'novelty', 'new_fact', 'restates', -1, 'assets', '[]'::jsonb,
                       'direction', 'neutral', 'scope', 'single_name', 'magnitude', 0,
                       'confidence', 1.0, 'audience', 'none',
                       'headline_zh', '状态样本 ' || g, 'why_zh', ''
                     ) AS verdict,
                     '{
                       "final":"drop","override_rule":null,"throttled_by":null,
                       "rule_baseline":"drop","watchlist_hits":[],"seen_similarity":null,
                       "seen_against":-1,"seen_scope":""
                     }'::jsonb AS decision
                FROM generate_series(1, %s) AS g
            ), judgment AS (
              SELECT g, verdict, jsonb_build_object(
                       'judgment_contract_version', 'news_judgment_v2',
                       'origin', 'degraded', 'verdict', verdict, 'decision', decision,
                       'error_code', 'status_scale_fixture'
                     ) AS atom
                FROM payload
            ), hashed AS (
              SELECT g, verdict, atom,
                     encode(digest(convert_to(news_canonical_jsonb(verdict), 'UTF8'), 'sha256'), 'hex')
                       AS verdict_sha256,
                     encode(digest(convert_to(news_canonical_jsonb(atom), 'UTF8'), 'sha256'), 'hex')
                       AS judgment_sha256
                FROM judgment
            )
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, judgment_contract_version, judgment_origin,
              rule_baseline_decision, final_decision, verdict, degraded, error_code,
              trace, scored_judgment_sha256, runtime_manifest_sha, program_version,
              program_sha256, evidence_version, evidence_sha256, focus_fact_id, created_at_ms
            )
            SELECT 'status-event-' || g, 'triage', 'news_triage_policy_v11',
                   'news_judgment_v2', 'degraded', 'drop', 'drop', verdict, true,
                   'status_scale_fixture',
                   jsonb_build_object(
                     'judgment_contract_version', 'news_judgment_v2',
                     'judgment_origin', 'degraded',
                     'judgment_sha256', judgment_sha256,
                     'verdict_sha256', verdict_sha256,
                     'runtime_manifest_sha', repeat('a', 64),
                     'evidence_version', 1,
                     'evidence_sha256', evidence.evidence_sha256,
                     'focus_fact_id', 'fact:' || g,
                     'program_version', 'news_semantic_program_v8',
                     'program_sha256', repeat('b', 64),
                     'told', '[]'::jsonb,
                     'told_count', 0,
                     'judgment', atom,
                     'latency_ms', g::double precision + 0.125,
                     'queue_lag_ms', g::double precision * 2 + 0.25,
                     'reasked_after_told_change', g %% 10 = 0,
                     'payload', (
                       SELECT string_agg(md5(g::text || ':' || chunk::text), '')
                         FROM generate_series(1, %s) AS chunk
                     )
                   ),
                   judgment_sha256, repeat('a', 64), 'news_semantic_program_v8',
                   repeat('b', 64), 1, evidence.evidence_sha256, 'fact:' || g, %s
              FROM hashed
              JOIN news_event_evidence_snapshots evidence
                ON evidence.event_id = 'status-event-' || g AND evidence.evidence_version = 1
            """,
            (VERDICTS, TRACE_CHUNKS, now_ms),
        )
        conn.execute("ANALYZE news_events")
        conn.execute("ANALYZE news_verdicts")
        conn.commit()
    finally:
        conn.close()


def test_news_status_serves_a_production_sized_corpus_within_the_native_timeout(tmp_path: Path) -> None:
    """Exercise the real query; shared-runner wall time is diagnostic, not correctness."""

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
