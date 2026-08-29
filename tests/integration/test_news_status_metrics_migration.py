"""Upgrade-path evidence for News status metric promotion (#221)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]

BEFORE_METRIC_PROMOTION = "20260826_0310"
NOW = 1_900_000_000_000


def _upgrade(revision: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    command.upgrade(config, revision)


def _fresh_schema_at(revision: str) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    _upgrade(revision)


def _seed_pre_promotion_verdicts(conn: Any) -> None:
    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
          provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
        )
        SELECT 'metric-item-' || g, 'opennews', 'metric-key-' || g, 'headline ' || g,
               %s, %s, '{}'::jsonb, 'live', %s, %s
          FROM generate_series(1, 5) AS g
        """,
        (NOW, NOW, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, family, comparison_fingerprint, comparison_title,
          leader_title, focus_fact_id, opened_at_ms, last_member_at_ms, expires_at_ms, admission,
          storyline_key, ingest_mode, created_at_ms, updated_at_ms
        )
        SELECT 'metric-event-' || g, 'metric-item-' || g, 'general', 'metric-fingerprint-' || g,
               'comparison', 'leader ' || g, 'fact:' || g, %s, %s, %s + 3600000,
               'candidate', 'asset:METRIC' || g, 'live', %s, %s
          FROM generate_series(1, 5) AS g
        """,
        (NOW, NOW, NOW, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO news_verdicts (
          event_id, stage, policy_version, rule_baseline_decision, final_decision,
          verdict, degraded, trace, created_at_ms
        )
        SELECT 'metric-event-' || g, 'triage', 'v6', 'drop', 'drop', '{}'::jsonb, false,
               CASE g
                 WHEN 1 THEN jsonb_build_object(
                   'latency_ms', 10.125, 'queue_lag_ms', 1.25, 'reasked_after_told_change', true
                 )
                 WHEN 2 THEN jsonb_build_object(
                   'latency_ms', 20.5, 'queue_lag_ms', 2.5, 'reasked_after_told_change', false
                 )
                 WHEN 3 THEN jsonb_build_object(
                   'latency_ms', 30.875, 'queue_lag_ms', 3.75, 'novelty_defaulted', true
                 )
                 WHEN 4 THEN jsonb_build_object(
                   'latency_ms', 40.25, 'queue_lag_ms', 5.0,
                   'reasked_after_told_change', true, 'novelty_defaulted', true, 'seen_scope', 'all'
                 )
                 ELSE '{"unrelated":"history"}'::jsonb
               END,
               %s
          FROM generate_series(1, 5) AS g
        """,
        (NOW,),
    )
    conn.commit()


def _legacy_pipeline_metrics(conn: Any) -> dict[str, float | int]:
    return dict(
        conn.execute(
            """
            SELECT
              percentile_cont(0.5) WITHIN GROUP (
                ORDER BY (trace ->> 'latency_ms')::double precision
              ) AS triage_p50_ms,
              percentile_cont(0.95) WITHIN GROUP (
                ORDER BY (trace ->> 'latency_ms')::double precision
              ) AS triage_p95_ms,
              percentile_cont(0.95) WITHIN GROUP (
                ORDER BY (trace ->> 'queue_lag_ms')::double precision
              ) AS queue_lag_p95_ms,
              count(*) FILTER (
                WHERE COALESCE((trace ->> 'reasked_after_told_change')::boolean, false)
              ) AS reasked_24h,
              count(*) FILTER (
                WHERE COALESCE((trace ->> 'novelty_defaulted')::boolean, false)
              ) AS novelty_defaulted_24h
              FROM news_verdicts
             WHERE stage = 'triage' AND created_at_ms >= %s
            """,
            (NOW - 24 * 3_600_000,),
        ).fetchone()
    )


def test_0311_backfills_metrics_and_preserves_the_pipeline_percentile_bytes() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_METRIC_PROMOTION)
        conn = connect_postgres_test(read_only=False)
        _seed_pre_promotion_verdicts(conn)
        before = _legacy_pipeline_metrics(conn)
        before_bytes = json.dumps(before, sort_keys=True, separators=(",", ":"))
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        generated = {
            str(row["column_name"]): str(row["is_generated"])
            for row in conn.execute(
                "SELECT column_name, is_generated FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'news_verdicts' "
                "AND column_name IN ("
                "'latency_ms', 'queue_lag_ms', 'reasked_after_told_change', 'novelty_defaulted', 'seen_scope'"
                ")"
            ).fetchall()
        }
        snapshot = repositories_for_connection(conn).news.status_snapshot(now_ms=NOW + 1)["pipeline"]
        after = {key: snapshot[key] for key in before}

        assert revision == "20260828_0324"
        assert generated == {
            "latency_ms": "ALWAYS",
            "queue_lag_ms": "ALWAYS",
            "reasked_after_told_change": "ALWAYS",
            "novelty_defaulted": "ALWAYS",
            "seen_scope": "ALWAYS",
        }
        assert json.dumps(after, sort_keys=True, separators=(",", ":")) == before_bytes

        conn.execute(
            """
            UPDATE news_verdicts
               SET trace = trace || '{
                 "latency_ms":99.5,
                 "queue_lag_ms":199.0,
                 "reasked_after_told_change":false,
                 "novelty_defaulted":true,
                 "seen_scope":"throttled"
               }'::jsonb
             WHERE event_id = 'metric-event-1'
            """
        )
        refreshed = conn.execute(
            "SELECT latency_ms, queue_lag_ms, reasked_after_told_change, novelty_defaulted, seen_scope "
            "FROM news_verdicts WHERE event_id = 'metric-event-1'"
        ).fetchone()
        assert refreshed == {
            "latency_ms": 99.5,
            "queue_lag_ms": 199.0,
            "reasked_after_told_change": False,
            "novelty_defaulted": True,
            "seen_scope": "throttled",
        }
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()
