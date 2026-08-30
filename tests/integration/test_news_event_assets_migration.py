"""Upgrade-path evidence for the deterministic Event-asset backfill (#267)."""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]

BEFORE_BACKFILL = "20260827_0313"
NOW = 1_900_000_000_000
HOUR = 3_600_000


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


def _event(
    conn: Any,
    event_id: str,
    *,
    admission: str,
    editorial_origin: str,
    assets: list[dict[str, str]],
    opened_at_ms: int = NOW - 2 * HOUR,
) -> None:
    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
          provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
        ) VALUES (%s, 'opennews', %s, 'headline', %s, %s, '{}'::jsonb, 'live', %s, %s)
        """,
        (f"i-{event_id}", f"k-{event_id}", opened_at_ms, opened_at_ms, opened_at_ms, opened_at_ms),
    )
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, family, comparison_fingerprint, comparison_title, leader_title,
          focus_fact_id, opened_at_ms, last_member_at_ms, expires_at_ms, admission, storyline_key,
          ingest_mode, grounded_assets, created_at_ms, updated_at_ms
        ) VALUES (%s, %s, 'general', %s, 'c', 'leader', %s, %s, %s, %s, %s, %s, 'live', '[]'::jsonb, %s, %s)
        """,
        (
            event_id,
            f"i-{event_id}",
            event_id,
            f"fact:{event_id}",
            opened_at_ms,
            opened_at_ms,
            opened_at_ms + HOUR,
            admission,
            f"asset:{event_id}",
            opened_at_ms,
            opened_at_ms,
        ),
    )
    conn.execute(
        """
        INSERT INTO news_verdicts (
          event_id, stage, policy_version, rule_baseline_decision, final_decision, verdict, editorial,
          scored_judgment_sha256, runtime_manifest_sha, degraded, created_at_ms
        ) VALUES (%s, 'triage', 'v10', 'drop', 'drop',
                  jsonb_build_object('assets', %s::jsonb), jsonb_build_object('editorial_origin', %s::text),
                  %s, %s, false, %s)
        """,
        (event_id, _json(assets), editorial_origin, "a" * 64, "b" * 64, opened_at_ms),
    )
    conn.commit()


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _assets(conn: Any) -> list[tuple[str, str, str | None, int]]:
    return [
        (str(row["event_id"]), str(row["symbol"]), row["market_type"], int(row["opened_at_ms"]))
        for row in conn.execute(
            "SELECT event_id, symbol, market_type, opened_at_ms FROM news_event_assets ORDER BY event_id, symbol"
        ).fetchall()
    ]


def test_the_backfill_gives_already_judged_telemetry_events_the_assets_their_gate_missed() -> None:
    """Every frame judged before this revision has a verdict primary and no asset row. Both halves."""

    conn = None
    try:
        _fresh_schema_at(BEFORE_BACKFILL)
        conn = connect_postgres_test(read_only=False)
        _event(
            conn,
            "oi-parsed",
            admission="telemetry_deterministic",
            editorial_origin="telemetry_deterministic",
            assets=[{"symbol": "TRUMP", "role": "primary", "market_type": "perp"}],
        )
        # A frame that matched no template names nothing; there is no price to measure against it.
        _event(
            conn,
            "oi-parse-failed",
            admission="telemetry_deterministic",
            editorial_origin="telemetry_deterministic",
            assets=[],
        )
        # The `XYZ-` builder-DEX prefix, and a secondary role that is not the review's sample.
        _event(
            conn,
            "oi-prefixed",
            admission="telemetry_deterministic",
            editorial_origin="telemetry_deterministic",
            assets=[
                {"symbol": "xyz-unitree", "role": "primary", "market_type": "perp"},
                {"symbol": "DOGE", "role": "secondary", "market_type": "perp"},
            ],
        )
        _event(
            conn,
            "liq-fact",
            admission="liquidation_deterministic",
            editorial_origin="telemetry_deterministic",
            assets=[{"symbol": "ETH", "role": "primary", "market_type": "perp"}],
        )
        # A model-lane Event's assets are the Gate's grounding evidence, and this backfill is not
        # allowed to promote a model's own reading into that table.
        _event(
            conn,
            "model-event",
            admission="candidate",
            editorial_origin="model",
            assets=[{"symbol": "NVDA", "role": "primary", "market_type": "cex"}],
        )
        assert _assets(conn) == []
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260829_0340"
        assert _assets(conn) == [
            ("liq-fact", "ETH", "perp", NOW - 2 * HOUR),
            ("oi-parsed", "TRUMP", "perp", NOW - 2 * HOUR),
            ("oi-prefixed", "UNITREE", "perp", NOW - 2 * HOUR),
        ]

        # And the backfilled row is the same row the running code writes, byte for byte: re-recording
        # the frame's own primary changes nothing.
        repos = repositories_for_connection(conn)
        with repos.transaction():
            repos.news.record_event_assets(event_id="oi-parsed", assets=[("TRUMP", "perp")])
            repos.news.record_event_assets(event_id="oi-prefixed", assets=[("XYZ-UNITREE", "perp")])
        assert len(_assets(conn)) == 3

        # The historical assets remain byte-for-byte audit evidence, but their pre-cut Events are archive-only.
        due = repos.price.due_reactions(now_ms=NOW, limit=100)
        assert due == []
    finally:
        if conn is not None:
            conn.close()
