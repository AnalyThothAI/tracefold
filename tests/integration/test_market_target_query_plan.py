from __future__ import annotations

import json
from typing import Any

from alembic import command

from tests.postgres_test_utils import (
    connect_postgres_test,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as _test_postgres_dsn,
)
from tracefold.market import RegistryRepository
from tracefold.platform.postgres.postgres_migrations import alembic_config

_SCHEMA_REVISION = "20260813_0263"
_SINCE_MS = 2_000_000
_NOISE_EVENT_COUNT = 40_000
_RECENT_NOISE_EVENT_COUNT = 10_000
_HOT_RECENT_NOISE_EVENT_COUNT = _RECENT_NOISE_EVENT_COUNT // 10
_HISTORICAL_RESOLUTIONS_PER_INTENT = 3


class _ExplainCaptureConnection:
    def __init__(self, conn: Any):
        self._conn = conn
        self.explain: dict[str, Any] | None = None

    def execute(self, sql: str, params: Any = None, *args: Any, **kwargs: Any):
        if self.explain is None:
            explain_row = self._conn.execute(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}",
                params,
                *args,
                **kwargs,
            ).fetchone()
            assert explain_row is not None
            self.explain = dict(explain_row["QUERY PLAN"][0])
        return self._conn.execute(sql, params, *args, **kwargs)


def test_ranked_market_targets_bounds_hot_tail_reads_without_changing_intent_join_semantics() -> None:
    _reset_schema(_SCHEMA_REVISION)
    conn = connect_postgres_test(read_only=False)
    try:
        _seed_market_target_plan_fixture(conn)
        for table_name in ("events", "token_intents", "token_intent_resolutions", "registry_assets"):
            conn.execute(f"VACUUM (ANALYZE) {table_name}")
        _append_market_target_hot_tail(conn)
        for table_name in ("events", "token_intents", "token_intent_resolutions", "registry_assets"):
            conn.execute(f"ANALYZE {table_name}")

        reference = _reference_ranked_chain_targets(conn, since_ms=_SINCE_MS)
        captured_conn = _ExplainCaptureConnection(conn)
        actual = RegistryRepository(captured_conn).ranked_market_targets(
            since_ms=_SINCE_MS,
            target_types=("chain_token",),
            limit=20,
            priority_product_targets=(),
        )
        explain = captured_conn.explain
    finally:
        conn.close()

    expected = [
        _chain_target("fixture-hot-address"),
        _chain_target("fixture-fast-address"),
        _chain_target("fixture-slow-address"),
        _chain_target("fixture-intent-join-address"),
    ]
    assert reference == expected
    assert actual == reference
    assert explain is not None

    nodes = list(_plan_nodes(explain["Plan"]))
    event_nodes = [node for node in nodes if node.get("Relation Name") == "events"]
    intent_seq_scans = [
        node for node in nodes if node.get("Relation Name") == "token_intents" and node.get("Node Type") == "Seq Scan"
    ]
    intent_clock_nodes = [
        node for node in nodes if node.get("Index Name") == "idx_token_intents_market_targets_created"
    ]
    summary = _plan_summary(nodes)
    assert not event_nodes, summary
    assert not intent_seq_scans, summary
    assert intent_clock_nodes, summary
    assert _shared_buffer_blocks(explain["Plan"]) < _NOISE_EVENT_COUNT // 2, summary
    assert sum(int(node.get("Temp Read Blocks", 0)) for node in nodes) == 0, summary
    assert sum(int(node.get("Temp Written Blocks", 0)) for node in nodes) == 0, summary
    assert all(node.get("Sort Space Type") != "Disk" for node in nodes), summary


def _reset_schema(revision: str) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
    finally:
        conn.close()
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    command.upgrade(config, revision)


def _seed_market_target_plan_fixture(conn: Any) -> None:
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO events(
              event_id, logical_dedup_key, source_provider, source_transport,
              coverage, channel, action, timestamp_ms, received_at_ms,
              text, text_raw, text_clean, search_text,
              raw_json, event_json, created_at_ms, updated_at_ms
            )
            SELECT
              'noise-event-' || series_no,
              'noise-event-' || series_no,
              'fixture', 'fixture', 'public_stream', 'fixture', 'post',
              received_at_ms, received_at_ms,
              repeat('market target fixture ', 8),
              repeat('market target fixture ', 8),
              repeat('market target fixture ', 8),
              repeat('market target fixture ', 8),
              jsonb_build_object('series_no', series_no),
              jsonb_build_object('series_no', series_no),
              received_at_ms, received_at_ms
            FROM (
              SELECT
                series_no,
                CASE
                  WHEN series_no > %s - %s THEN %s + series_no
                  ELSE %s - series_no
                END AS received_at_ms
              FROM generate_series(1, %s) AS series_no
            ) fixture_events
            """,
            (
                _NOISE_EVENT_COUNT,
                _RECENT_NOISE_EVENT_COUNT,
                _SINCE_MS,
                _SINCE_MS - 100_000,
                _NOISE_EVENT_COUNT,
            ),
        )
        conn.execute(
            """
            INSERT INTO token_intents(
              intent_id, event_id, intent_key, construction_policy,
              display_symbol, intent_status, intent_confidence,
              created_at_ms, updated_at_ms
            )
            SELECT
              'noise-intent-' || series_no,
              'noise-event-' || series_no,
              'noise-key-' || series_no,
              'fixture', 'NOISE', 'resolved', 1.0,
              received_at_ms, received_at_ms
            FROM (
              SELECT
                series_no,
                CASE
                  WHEN series_no > %s - %s THEN %s + series_no
                  ELSE %s - series_no
                END AS received_at_ms
              FROM generate_series(1, %s) AS series_no
            ) fixture_intents
            """,
            (
                _NOISE_EVENT_COUNT,
                _RECENT_NOISE_EVENT_COUNT,
                _SINCE_MS,
                _SINCE_MS - 100_000,
                _NOISE_EVENT_COUNT,
            ),
        )
        conn.execute(
            """
            INSERT INTO token_intent_resolutions(
              resolution_id, intent_id, event_id, resolution_status,
              resolver_policy_version, target_type, target_id,
              record_status, is_current, decision_time_ms, created_at_ms
            )
            SELECT
              'noise-current-resolution-' || series_no,
              'noise-intent-' || series_no,
              'noise-event-' || series_no,
              'EXACT', 'fixture', 'Asset',
              'missing-noise-asset-' || (series_no %% 128),
              'current', true, %s, %s
            FROM generate_series(1, %s) AS series_no
            """,
            (_SINCE_MS, _SINCE_MS, _NOISE_EVENT_COUNT),
        )
        conn.execute(
            """
            INSERT INTO token_intent_resolutions(
              resolution_id, intent_id, event_id, resolution_status,
              resolver_policy_version, target_type, target_id,
              record_status, is_current, superseded_at_ms,
              decision_time_ms, created_at_ms
            )
            SELECT
              'noise-historical-resolution-' || history_no,
              'noise-intent-' || intent_no,
              'noise-event-' || intent_no,
              'NOT_FOUND', 'fixture', NULL, NULL,
              'superseded', false, %s, %s, %s
            FROM (
              SELECT
                history_no,
                ((history_no - 1) %% %s) + 1 AS intent_no
              FROM generate_series(1, %s) AS history_no
            ) fixture_history
            """,
            (
                _SINCE_MS,
                _SINCE_MS,
                _SINCE_MS,
                _NOISE_EVENT_COUNT,
                _NOISE_EVENT_COUNT * _HISTORICAL_RESOLUTIONS_PER_INTENT,
            ),
        )
        conn.execute(
            """
            INSERT INTO registry_assets(
              asset_id, chain_id, token_standard, address, status,
              first_seen_at_ms, updated_at_ms
            ) VALUES
              ('fixture-fast-asset', 'solana', 'spl', 'fixture-fast-address',
               'canonical', %s, %s),
              ('fixture-slow-asset', 'solana', 'spl', 'fixture-slow-address',
               'canonical', %s, %s),
              ('fixture-intent-join-asset', 'solana', 'spl', 'fixture-intent-join-address',
               'canonical', %s, %s),
              ('fixture-resolution-event-only-asset', 'solana', 'spl',
               'fixture-resolution-event-only-address', 'canonical', %s, %s)
            """,
            (_SINCE_MS,) * 8,
        )
        conn.execute(
            """
            INSERT INTO events(
              event_id, logical_dedup_key, source_provider, source_transport,
              coverage, channel, action, timestamp_ms, received_at_ms,
              raw_json, event_json, created_at_ms, updated_at_ms
            ) VALUES
              ('fixture-fast-event', 'fixture-fast-event', 'fixture', 'fixture',
               'public_stream', 'fixture', 'post', %s, %s, '{}'::jsonb, '{}'::jsonb, %s, %s),
              ('fixture-slow-event', 'fixture-slow-event', 'fixture', 'fixture',
               'public_stream', 'fixture', 'post', %s, %s, '{}'::jsonb, '{}'::jsonb, %s, %s),
              ('fixture-intent-recent-event', 'fixture-intent-recent-event', 'fixture', 'fixture',
               'public_stream', 'fixture', 'post', %s, %s, '{}'::jsonb, '{}'::jsonb, %s, %s),
              ('fixture-intent-old-event', 'fixture-intent-old-event', 'fixture', 'fixture',
               'public_stream', 'fixture', 'post', %s, %s, '{}'::jsonb, '{}'::jsonb, %s, %s),
              ('fixture-resolution-old-anchor', 'fixture-resolution-old-anchor', 'fixture', 'fixture',
               'public_stream', 'fixture', 'post', %s, %s, '{}'::jsonb, '{}'::jsonb, %s, %s),
              ('fixture-resolution-recent-anchor', 'fixture-resolution-recent-anchor', 'fixture', 'fixture',
               'public_stream', 'fixture', 'post', %s, %s, '{}'::jsonb, '{}'::jsonb, %s, %s)
            """,
            (
                *(_timestamp_params(_SINCE_MS + 90_000)),
                *(_timestamp_params(_SINCE_MS + 80_000)),
                *(_timestamp_params(_SINCE_MS + 70_000)),
                *(_timestamp_params(_SINCE_MS - 20_000)),
                *(_timestamp_params(_SINCE_MS - 10_000)),
                *(_timestamp_params(_SINCE_MS + 60_000)),
            ),
        )
        conn.execute(
            """
            INSERT INTO token_intents(
              intent_id, event_id, intent_key, construction_policy,
              display_symbol, intent_status, intent_confidence,
              created_at_ms, updated_at_ms
            ) VALUES
              ('fixture-fast-intent', 'fixture-fast-event', 'fixture-fast-key',
               'fixture', 'FAST', 'resolved', 1.0, %s, %s),
              ('fixture-slow-intent', 'fixture-slow-event', 'fixture-slow-key',
               'fixture', 'SLOW', 'resolved', 1.0, %s, %s),
              ('fixture-intent-join-intent', 'fixture-intent-recent-event',
               'fixture-intent-join-key', 'fixture', 'INTENTJOIN', 'resolved', 1.0, %s, %s),
              ('fixture-resolution-event-only-intent', 'fixture-intent-old-event',
               'fixture-resolution-event-only-key', 'fixture', 'RESOLUTIONEVENT',
               'resolved', 1.0, %s, %s)
            """,
            (
                _SINCE_MS + 90_000,
                _SINCE_MS + 90_000,
                _SINCE_MS + 80_000,
                _SINCE_MS + 80_000,
                _SINCE_MS + 70_000,
                _SINCE_MS + 70_000,
                _SINCE_MS - 20_000,
                _SINCE_MS - 20_000,
            ),
        )
        conn.execute(
            """
            INSERT INTO token_intent_resolutions(
              resolution_id, intent_id, event_id, resolution_status,
              resolver_policy_version, target_type, target_id,
              record_status, is_current, decision_time_ms, created_at_ms
            ) VALUES
              ('fixture-fast-resolution', 'fixture-fast-intent', 'fixture-fast-event',
               'EXACT', 'fixture', 'Asset', 'fixture-fast-asset',
               'current', true, %s, %s),
              ('fixture-slow-resolution', 'fixture-slow-intent', 'fixture-slow-event',
               'EXACT', 'fixture', 'Asset', 'fixture-slow-asset',
               'current', true, %s, %s),
              ('fixture-intent-join-resolution', 'fixture-intent-join-intent',
               'fixture-resolution-old-anchor', 'EXACT', 'fixture', 'Asset',
               'fixture-intent-join-asset', 'current', true, %s, %s),
              ('fixture-resolution-event-only-resolution',
               'fixture-resolution-event-only-intent', 'fixture-resolution-recent-anchor',
               'EXACT', 'fixture', 'Asset', 'fixture-resolution-event-only-asset',
               'current', true, %s, %s)
            """,
            (_SINCE_MS,) * 8,
        )


def _timestamp_params(timestamp_ms: int) -> tuple[int, int, int, int]:
    return (timestamp_ms, timestamp_ms, timestamp_ms, timestamp_ms)


def _append_market_target_hot_tail(conn: Any) -> None:
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO events(
              event_id, logical_dedup_key, source_provider, source_transport,
              coverage, channel, action, timestamp_ms, received_at_ms,
              text, text_raw, text_clean, search_text,
              raw_json, event_json, created_at_ms, updated_at_ms
            )
            SELECT
              'hot-noise-event-' || series_no,
              'hot-noise-event-' || series_no,
              'fixture', 'fixture', 'public_stream', 'fixture', 'post',
              %s + series_no, %s + series_no,
              repeat('hot market target fixture ', 8),
              repeat('hot market target fixture ', 8),
              repeat('hot market target fixture ', 8),
              repeat('hot market target fixture ', 8),
              jsonb_build_object('series_no', series_no),
              jsonb_build_object('series_no', series_no),
              %s + series_no, %s + series_no
            FROM generate_series(1, %s) AS series_no
            """,
            (
                _SINCE_MS + 100_000,
                _SINCE_MS + 100_000,
                _SINCE_MS + 100_000,
                _SINCE_MS + 100_000,
                _HOT_RECENT_NOISE_EVENT_COUNT,
            ),
        )
        conn.execute(
            """
            INSERT INTO token_intents(
              intent_id, event_id, intent_key, construction_policy,
              display_symbol, intent_status, intent_confidence,
              created_at_ms, updated_at_ms
            )
            SELECT
              'hot-noise-intent-' || series_no,
              'hot-noise-event-' || series_no,
              'hot-noise-key-' || series_no,
              'fixture', 'HOTNOISE', 'resolved', 1.0,
              %s + series_no, %s + series_no
            FROM generate_series(1, %s) AS series_no
            """,
            (_SINCE_MS + 100_000, _SINCE_MS + 100_000, _HOT_RECENT_NOISE_EVENT_COUNT),
        )
        conn.execute(
            """
            INSERT INTO token_intent_resolutions(
              resolution_id, intent_id, event_id, resolution_status,
              resolver_policy_version, target_type, target_id,
              record_status, is_current, decision_time_ms, created_at_ms
            )
            SELECT
              'hot-noise-resolution-' || series_no,
              'hot-noise-intent-' || series_no,
              'hot-noise-event-' || series_no,
              'EXACT', 'fixture', 'Asset',
              'missing-hot-noise-asset-' || series_no,
              'current', true, %s, %s
            FROM generate_series(1, %s) AS series_no
            """,
            (_SINCE_MS + 100_000, _SINCE_MS + 100_000, _HOT_RECENT_NOISE_EVENT_COUNT),
        )
        conn.execute(
            """
            INSERT INTO registry_assets(
              asset_id, chain_id, token_standard, address, status,
              first_seen_at_ms, updated_at_ms
            ) VALUES (
              'fixture-hot-asset', 'solana', 'spl', 'fixture-hot-address',
              'canonical', %s, %s
            )
            """,
            (_SINCE_MS + 200_000, _SINCE_MS + 200_000),
        )
        conn.execute(
            """
            INSERT INTO events(
              event_id, logical_dedup_key, source_provider, source_transport,
              coverage, channel, action, timestamp_ms, received_at_ms,
              text, text_raw, text_clean, search_text,
              raw_json, event_json, created_at_ms, updated_at_ms
            ) VALUES (
              'fixture-hot-event', 'fixture-hot-event', 'fixture', 'fixture',
              'public_stream', 'fixture', 'post', %s, %s,
              repeat('hot returned market target fixture ', 8),
              repeat('hot returned market target fixture ', 8),
              repeat('hot returned market target fixture ', 8),
              repeat('hot returned market target fixture ', 8),
              '{}'::jsonb, '{}'::jsonb, %s, %s
            )
            """,
            _timestamp_params(_SINCE_MS + 200_000),
        )
        conn.execute(
            """
            INSERT INTO token_intents(
              intent_id, event_id, intent_key, construction_policy,
              display_symbol, intent_status, intent_confidence,
              created_at_ms, updated_at_ms
            ) VALUES (
              'fixture-hot-intent', 'fixture-hot-event', 'fixture-hot-key',
              'fixture', 'HOT', 'resolved', 1.0, %s, %s
            )
            """,
            (_SINCE_MS + 200_000, _SINCE_MS + 200_000),
        )
        conn.execute(
            """
            INSERT INTO token_intent_resolutions(
              resolution_id, intent_id, event_id, resolution_status,
              resolver_policy_version, target_type, target_id,
              record_status, is_current, decision_time_ms, created_at_ms
            ) VALUES (
              'fixture-hot-resolution', 'fixture-hot-intent', 'fixture-hot-event',
              'EXACT', 'fixture', 'Asset', 'fixture-hot-asset',
              'current', true, %s, %s
            )
            """,
            (_SINCE_MS + 200_000, _SINCE_MS + 200_000),
        )


def _reference_ranked_chain_targets(conn: Any, *, since_ms: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          'chain_token' AS target_type,
          asset.chain_id || ':' || asset.address AS target_id,
          asset.chain_id,
          asset.address,
          NULL::text AS native_market_id,
          NULL::text AS quote_symbol,
          'okx' AS provider,
          NULL::text AS pricefeed_id
        FROM registry_assets asset
        JOIN (
          SELECT resolution.target_id, max(event.received_at_ms) AS latest_received_at_ms
          FROM token_intent_resolutions resolution
          JOIN token_intents intent
            ON intent.intent_id = resolution.intent_id
          JOIN events event
            ON event.event_id = intent.event_id
          WHERE resolution.is_current
            AND resolution.resolution_status IN ('EXACT', 'UNIQUE_BY_CONTEXT')
            AND resolution.target_type = 'Asset'
            AND event.received_at_ms >= %s
          GROUP BY resolution.target_id
        ) eligible ON eligible.target_id = asset.asset_id
        WHERE asset.status IN ('candidate', 'canonical')
        ORDER BY eligible.latest_received_at_ms DESC, target_id
        """,
        (since_ms,),
    ).fetchall()
    return [dict(row) for row in rows]


def _chain_target(address: str) -> dict[str, Any]:
    return {
        "target_type": "chain_token",
        "target_id": f"solana:{address}",
        "chain_id": "solana",
        "address": address,
        "native_market_id": None,
        "quote_symbol": None,
        "provider": "okx",
        "pricefeed_id": None,
    }


def _plan_nodes(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


def _plan_summary(nodes: list[dict[str, Any]]) -> str:
    details = [
        {
            key: node[key]
            for key in (
                "Node Type",
                "Relation Name",
                "Index Name",
                "Actual Rows",
                "Actual Loops",
                "Heap Fetches",
                "Shared Hit Blocks",
                "Shared Read Blocks",
                "Temp Read Blocks",
                "Temp Written Blocks",
                "Sort Space Type",
            )
            if key in node
        }
        for node in nodes
    ]
    return json.dumps(details, indent=2, sort_keys=True)


def _shared_buffer_blocks(node: dict[str, Any]) -> int:
    return int(node.get("Shared Hit Blocks", 0)) + int(node.get("Shared Read Blocks", 0))
