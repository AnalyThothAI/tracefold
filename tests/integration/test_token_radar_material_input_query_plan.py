from __future__ import annotations

import json
import math
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.market.radar.constants import TOKEN_RADAR_INPUT_ROW_CAP
from tracefold.market.radar.reducer import (
    RadarSelectionKey,
    TokenRadarInputOverflow,
    token_radar_text_fingerprint,
)
from tracefold.market.radar.snapshot_repository import TokenRadarCurrentRepository

_NOW_MS = 1_800_000_000_000
_COLD_EVENT_COUNT = 10_000
_HOT_EVENT_COUNT = 1_000
_EVENT_COUNT = _COLD_EVENT_COUNT + _HOT_EVENT_COUNT
_NOISE_EVENT_COUNT = 60_000
_NON_RADAR_INTENT_COUNT = 25_000
_HOT_TAIL_HEAP_FETCH_MARGIN = 32
_EXPLAIN_AVERAGE_ROUNDING_MARGIN = 256
_PRESENTATION_TARGET_COUNT = 50
_PRESENTATION_NOISE_TICK_COUNT = 20_000


class _ExplainCaptureCursor:
    def __init__(self, conn: Any, *, name: str, capture: _ExplainCaptureConnection) -> None:
        self._conn = conn
        self._cursor = conn.cursor(name=name)
        self._capture = capture

    def __enter__(self) -> _ExplainCaptureCursor:
        self._cursor.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._cursor.__exit__(*args)

    def execute(self, sql: str, params: Any = None) -> None:
        explain_row = self._conn.execute(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}",
            params,
        ).fetchone()
        assert explain_row is not None
        self._capture.explain = dict(explain_row["QUERY PLAN"][0])
        self._cursor.execute(sql, params)

    def fetchmany(self, size: int) -> list[Any]:
        return self._cursor.fetchmany(size)


class _ExplainCaptureConnection:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.explain: dict[str, Any] | None = None

    def cursor(self, *, name: str) -> _ExplainCaptureCursor:
        return _ExplainCaptureCursor(self._conn, name=name, capture=self)


class _ExplainExecuteConnection:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.explain: dict[str, Any] | None = None

    def execute(self, sql: str, params: Any = None) -> Any:
        explain_row = self._conn.execute(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}",
            params,
        ).fetchone()
        assert explain_row is not None
        self.explain = dict(explain_row["QUERY PLAN"][0])
        return self._conn.execute(sql, params)


def test_material_input_repository_uses_covering_event_read_with_only_hot_tail_heap_fetches(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _insert_material_events(conn, start=1, stop=_COLD_EVENT_COUNT)
        _insert_noise_events(conn)
        _insert_non_radar_intents(conn)
        conn.commit()
        for table_name in ("events", "token_intents", "token_intent_resolutions"):
            _vacuum_analyze(conn, table_name)

        _insert_material_events(conn, start=_COLD_EVENT_COUNT + 1, stop=_EVENT_COUNT)
        conn.commit()
        for table_name in ("events", "token_intents", "token_intent_resolutions"):
            conn.execute(f"ANALYZE {table_name}")
        conn.commit()

        captured_conn = _ExplainCaptureConnection(conn)
        with conn.transaction():
            revisions = TokenRadarCurrentRepository(captured_conn).load_material_inputs(
                now_ms=_NOW_MS,
            )
        explain = captured_conn.explain
    finally:
        conn.close()

    assert len(revisions) == _EVENT_COUNT
    assert revisions[0].event_id == "radar-plan-event-00001"
    assert revisions[-1].event_id == f"radar-plan-event-{_EVENT_COUNT:05d}"
    assert revisions[0].text_fingerprint == token_radar_text_fingerprint("  $RADAR\tStraße\nindependent\revidence\f1  ")
    assert explain is not None

    nodes = list(_plan_nodes(explain["Plan"]))
    event_index_only_scans = [
        node
        for node in nodes
        if node.get("Relation Name") == "events"
        and node.get("Node Type") == "Index Only Scan"
        and node.get("Index Name") == "idx_events_token_radar_source_time"
    ]
    other_event_scans = [
        node for node in nodes if node.get("Relation Name") == "events" and node not in event_index_only_scans
    ]
    diagnostic = _plan_summary(nodes)
    assert event_index_only_scans, diagnostic
    assert not other_event_scans, diagnostic
    assert sum(_executed_rows(node) for node in event_index_only_scans) <= (
        _EVENT_COUNT + _NOISE_EVENT_COUNT + _EXPLAIN_AVERAGE_ROUNDING_MARGIN
    ), diagnostic
    assert max(int(node.get("Heap Fetches", 0)) for node in event_index_only_scans) <= (
        _HOT_EVENT_COUNT + _HOT_TAIL_HEAP_FETCH_MARGIN
    ), diagnostic
    assert sum(int(node.get("Temp Read Blocks", 0)) for node in nodes) == 0, diagnostic
    assert sum(int(node.get("Temp Written Blocks", 0)) for node in nodes) == 0, diagnostic


def test_material_input_row_overflow_stops_the_source_time_plan_without_spilling(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _insert_material_events(conn, start=1, stop=TOKEN_RADAR_INPUT_ROW_CAP + 5_000)
        conn.commit()
        for table_name in ("events", "token_intents", "token_intent_resolutions"):
            _vacuum_analyze(conn, table_name)

        captured_conn = _ExplainCaptureConnection(conn)
        with (
            conn.transaction(),
            pytest.raises(
                TokenRadarInputOverflow,
                match="token_radar_input_row_overflow",
            ),
        ):
            TokenRadarCurrentRepository(captured_conn).load_material_inputs(now_ms=_NOW_MS)
        explain = captured_conn.explain
    finally:
        conn.close()

    assert explain is not None
    nodes = list(_plan_nodes(explain["Plan"]))
    diagnostic = _plan_summary(nodes)
    expected_indexes = {
        "events": "idx_events_token_radar_source_time",
        "token_intents": "idx_token_intents_event_intent",
        "token_intent_resolutions": "idx_token_intent_resolutions_token_radar_material",
    }
    for relation_name, index_name in expected_indexes.items():
        scans = [
            node
            for node in nodes
            if node.get("Relation Name") == relation_name
            and node.get("Index Name") == index_name
            and node.get("Node Type") == "Index Only Scan"
        ]
        assert scans, diagnostic
        assert max(int(node.get("Actual Loops", 0)) for node in scans) <= (
            TOKEN_RADAR_INPUT_ROW_CAP + _HOT_TAIL_HEAP_FETCH_MARGIN
        ), diagnostic
        assert sum(_executed_rows(node) for node in scans) <= (
            TOKEN_RADAR_INPUT_ROW_CAP + _HOT_TAIL_HEAP_FETCH_MARGIN
        ), diagnostic

    assert sum(int(node.get("Temp Read Blocks", 0)) for node in nodes) == 0, diagnostic
    assert sum(int(node.get("Temp Written Blocks", 0)) for node in nodes) == 0, diagnostic


def test_selected_target_presentation_probes_market_caps_by_target_instead_of_scanning_recent_noise(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _insert_presentation_dimensions_and_selected_ticks(conn)
        _insert_presentation_noise_ticks(
            conn,
            prefix="historical",
            base_ms=_NOW_MS - 600_000,
        )
        conn.commit()
        for table_name in ("registry_assets", "market_ticks_default"):
            _vacuum_analyze(conn, table_name)
        _insert_presentation_noise_ticks(
            conn,
            prefix="recent",
            base_ms=_NOW_MS,
        )
        conn.commit()

        selections = [
            RadarSelectionKey(
                target_type="Asset",
                target_id=f"radar-plan-asset-{index:02d}",
                trigger_event_id=f"missing-event-{index:02d}",
                trigger_intent_id=f"missing-intent-{index:02d}",
                trigger_resolution_id=f"missing-resolution-{index:02d}",
            )
            for index in range(1, _PRESENTATION_TARGET_COUNT + 1)
        ]
        captured_conn = _ExplainExecuteConnection(conn)
        with conn.transaction():
            rows = TokenRadarCurrentRepository(captured_conn).load_presentation_facts(
                selections,
                now_ms=_NOW_MS,
            )
        explain = captured_conn.explain
    finally:
        conn.close()

    assert len(rows) == _PRESENTATION_TARGET_COUNT
    assert rows[0]["target_id"] == "radar-plan-asset-01"
    assert rows[0]["market_cap_usd"] == 100_001
    assert rows[-1]["target_id"] == f"radar-plan-asset-{_PRESENTATION_TARGET_COUNT:02d}"
    assert rows[-1]["market_cap_usd"] == 100_000 + _PRESENTATION_TARGET_COUNT
    assert explain is not None

    nodes = list(_plan_nodes(explain["Plan"]))
    market_tick_scans = [
        node
        for node in nodes
        if str(node.get("Relation Name") or "").startswith("market_ticks")
        and str(node.get("Node Type") or "").endswith("Scan")
    ]
    diagnostic = _plan_summary(nodes)
    assert market_tick_scans, diagnostic
    assert sum(_executed_rows(node) for node in market_tick_scans) <= (_PRESENTATION_TARGET_COUNT * 3), diagnostic
    assert sum(int(node.get("Temp Read Blocks", 0)) for node in nodes) == 0, diagnostic
    assert sum(int(node.get("Temp Written Blocks", 0)) for node in nodes) == 0, diagnostic


def _insert_material_events(conn: Any, *, start: int, stop: int) -> None:
    conn.execute(
        """
        INSERT INTO events(
          event_id, logical_dedup_key, source_provider, source_transport,
          coverage, channel, action, timestamp_ms, received_at_ms,
          author_handle, text, text_clean, search_text, raw_json, event_json,
          created_at_ms, updated_at_ms
        )
        SELECT
          'radar-plan-event-' || lpad(series_no::text, 5, '0'),
          'radar-plan-dedup-' || lpad(series_no::text, 5, '0'),
          'gmgn', 'direct_ws', 'public_stream', 'twitter_monitor_basic', 'tweet',
          %s - 36000000 + series_no * 1000,
          %s - 36000000 + series_no * 1000 + 100,
          'radar-author-' || series_no,
          CASE WHEN series_no = 1
               THEN '  $RADAR' || chr(9) || 'Straße' || chr(10)
                    || 'independent' || chr(13) || 'evidence' || chr(12) || '1  '
               ELSE 'radar independent evidence ' || series_no END,
          CASE WHEN series_no = 1
               THEN '  $RADAR' || chr(9) || 'Straße' || chr(10)
                    || 'independent' || chr(13) || 'evidence' || chr(12) || '1  '
               ELSE 'radar independent evidence ' || series_no END,
          'radar independent evidence ' || series_no,
          jsonb_build_object(
            'series_no', series_no,
            'padding', repeat(md5(series_no::text), 48)
          ),
          jsonb_build_object(
            'series_no', series_no,
            'padding', repeat(md5((series_no + 1)::text), 48)
          ),
          %s - 36000000 + series_no * 1000 + 200,
          %s - 36000000 + series_no * 1000 + 200
        FROM generate_series(%s::integer, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, _NOW_MS, _NOW_MS, start, stop),
    )
    conn.execute(
        """
        INSERT INTO token_intents(
          intent_id, event_id, intent_key, construction_policy,
          intent_status, intent_confidence, created_at_ms, updated_at_ms
        )
        SELECT
          'radar-plan-intent-' || lpad(series_no::text, 5, '0'),
          'radar-plan-event-' || lpad(series_no::text, 5, '0'),
          'radar-plan-intent-key-' || lpad(series_no::text, 5, '0'),
          'radar_plan_fixture', 'resolved', 1.0,
          %s - 36000000 + series_no * 1000 + 200,
          %s - 36000000 + series_no * 1000 + 200
        FROM generate_series(%s::integer, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, start, stop),
    )
    conn.execute(
        """
        INSERT INTO token_intent_resolutions(
          resolution_id, intent_id, event_id, resolution_status,
          decision_time_ms, created_at_ms, target_type, target_id
        )
        SELECT
          'radar-plan-resolution-' || lpad(series_no::text, 5, '0'),
          'radar-plan-intent-' || lpad(series_no::text, 5, '0'),
          'radar-plan-event-' || lpad(series_no::text, 5, '0'),
          'EXACT',
          %s - 36000000 + series_no * 1000 + 300,
          %s - 36000000 + series_no * 1000 + 300,
          'Asset', 'radar-plan-asset-' || series_no
        FROM generate_series(%s::integer, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, start, stop),
    )


def _insert_noise_events(conn: Any) -> None:
    conn.execute(
        """
        INSERT INTO events(
          event_id, logical_dedup_key, source_provider, source_transport,
          coverage, channel, action, timestamp_ms, received_at_ms,
          author_handle, text, raw_json, event_json, created_at_ms, updated_at_ms
        )
        SELECT
          'radar-plan-noise-' || lpad(series_no::text, 5, '0'),
          'radar-plan-noise-dedup-' || lpad(series_no::text, 5, '0'),
          'gmgn', 'direct_ws', 'public_stream', 'twitter_monitor_basic', 'tweet',
          %s - series_no,
          %s - series_no,
          'noise-author-' || series_no,
          'noise ' || series_no,
          '{}'::jsonb, '{}'::jsonb,
          %s - series_no,
          %s - series_no
        FROM generate_series(1, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, _NOW_MS, _NOW_MS, _NOISE_EVENT_COUNT),
    )


def _insert_non_radar_intents(conn: Any) -> None:
    conn.execute(
        """
        INSERT INTO events(
          event_id, logical_dedup_key, source_provider, source_transport,
          coverage, channel, action, timestamp_ms, received_at_ms,
          author_handle, text, raw_json, event_json, created_at_ms, updated_at_ms
        )
        SELECT
          'radar-plan-other-event-' || lpad(series_no::text, 5, '0'),
          'radar-plan-other-dedup-' || lpad(series_no::text, 5, '0'),
          'other_provider', 'fixture', 'fixture', 'fixture', 'tweet',
          %s - 30000000 + series_no,
          %s - 30000000 + series_no,
          'other-author-' || series_no,
          'other evidence ' || series_no,
          '{}'::jsonb, '{}'::jsonb,
          %s - 30000000 + series_no,
          %s - 30000000 + series_no
        FROM generate_series(1, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, _NOW_MS, _NOW_MS, _NON_RADAR_INTENT_COUNT),
    )
    conn.execute(
        """
        INSERT INTO token_intents(
          intent_id, event_id, intent_key, construction_policy,
          intent_status, intent_confidence, created_at_ms, updated_at_ms
        )
        SELECT
          'radar-plan-other-intent-' || lpad(series_no::text, 5, '0'),
          'radar-plan-other-event-' || lpad(series_no::text, 5, '0'),
          'radar-plan-other-key-' || lpad(series_no::text, 5, '0'),
          'radar_plan_fixture', 'resolved', 1.0,
          %s - 30000000 + series_no,
          %s - 30000000 + series_no
        FROM generate_series(1, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, _NON_RADAR_INTENT_COUNT),
    )
    conn.execute(
        """
        INSERT INTO token_intent_resolutions(
          resolution_id, intent_id, event_id, resolution_status,
          decision_time_ms, created_at_ms, target_type, target_id
        )
        SELECT
          'radar-plan-other-resolution-' || lpad(series_no::text, 5, '0'),
          'radar-plan-other-intent-' || lpad(series_no::text, 5, '0'),
          'radar-plan-other-event-' || lpad(series_no::text, 5, '0'),
          'EXACT',
          %s - 30000000 + series_no,
          %s - 30000000 + series_no,
          'Asset', 'radar-plan-other-asset-' || series_no
        FROM generate_series(1, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, _NON_RADAR_INTENT_COUNT),
    )


def _insert_presentation_dimensions_and_selected_ticks(conn: Any) -> None:
    conn.execute(
        """
        INSERT INTO registry_assets(
          asset_id, chain_id, token_standard, address, status,
          first_seen_at_ms, updated_at_ms
        )
        SELECT
          'radar-plan-asset-' || lpad(series_no::text, 2, '0'),
          'solana', 'spl',
          'radar-plan-mint-' || lpad(series_no::text, 2, '0'),
          'canonical', %s - 10000, %s - 10000
        FROM generate_series(1, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, _PRESENTATION_TARGET_COUNT),
    )
    conn.execute(
        """
        INSERT INTO market_ticks(
          observed_at_ms, tick_id, target_type, target_id,
          chain, token_address, source_tier, source_provider,
          received_at_ms, price_usd, market_cap_usd,
          raw_payload_json, payload_hash, created_at_ms
        )
        SELECT
          %s - series_no,
          'radar-plan-selected-tick-' || lpad(series_no::text, 2, '0'),
          'chain_token',
          'solana:radar-plan-mint-' || lpad(series_no::text, 2, '0'),
          'solana', 'radar-plan-mint-' || lpad(series_no::text, 2, '0'),
          'tier2_poll', 'okx_dex_rest',
          %s - series_no,
          10 + series_no,
          100000 + series_no,
          '{}'::jsonb,
          'radar-plan-selected-hash-' || series_no,
          %s - series_no
        FROM generate_series(1, %s::integer) AS series_no
        """,
        (_NOW_MS, _NOW_MS, _NOW_MS, _PRESENTATION_TARGET_COUNT),
    )
    conn.execute(
        """
        INSERT INTO market_ticks(
          observed_at_ms, tick_id, target_type, target_id,
          chain, token_address, source_tier, source_provider,
          received_at_ms, price_usd, market_cap_usd,
          raw_payload_json, payload_hash, created_at_ms
        ) VALUES (
          %s, 'radar-plan-selected-tick-01-older',
          'chain_token', 'solana:radar-plan-mint-01',
          'solana', 'radar-plan-mint-01',
          'tier2_poll', 'okx_dex_rest',
          %s, 9, 42, '{}'::jsonb,
          'radar-plan-selected-hash-01-older', %s
        )
        """,
        (_NOW_MS - 200_000, _NOW_MS - 200_000, _NOW_MS - 200_000),
    )


def _insert_presentation_noise_ticks(conn: Any, *, prefix: str, base_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO market_ticks(
          observed_at_ms, tick_id, target_type, target_id,
          chain, token_address, source_tier, source_provider,
          received_at_ms, price_usd, market_cap_usd,
          raw_payload_json, payload_hash, created_at_ms
        )
        SELECT
          %s - (series_no %% 240000),
          'radar-plan-' || %s || '-noise-tick-' || lpad(series_no::text, 5, '0'),
          'chain_token',
          'solana:radar-plan-' || %s || '-noise-' || lpad(series_no::text, 5, '0'),
          'solana',
          'radar-plan-' || %s || '-noise-' || lpad(series_no::text, 5, '0'),
          'tier2_poll', 'okx_dex_rest',
          %s - (series_no %% 240000),
          1, 100,
          '{}'::jsonb,
          'radar-plan-' || %s || '-noise-hash-' || series_no,
          %s - (series_no %% 240000)
        FROM generate_series(1, %s::integer) AS series_no
        """,
        (
            base_ms,
            prefix,
            prefix,
            prefix,
            base_ms,
            prefix,
            base_ms,
            _PRESENTATION_NOISE_TICK_COUNT,
        ),
    )


def _vacuum_analyze(conn: Any, table_name: str) -> None:
    conn.execute(f"VACUUM (ANALYZE) {table_name}")


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
                "Alias",
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


def _executed_rows(node: dict[str, Any]) -> int:
    return math.ceil(float(node.get("Actual Rows") or 0) * float(node.get("Actual Loops") or 0))
