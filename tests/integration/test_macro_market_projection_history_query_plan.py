from __future__ import annotations

import json
from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.macro.dependencies import MODULE_DATASET_DEPENDENCIES
from tracefold.macro.history_policy import market_history_limits
from tracefold.macro.registry import DATASET_REGISTRY

_DAY_MS = 86_400_000
_BASE_DAY_MS = 20_833 * _DAY_MS
_CUTOFF_MS = _BASE_DAY_MS + 3_000 * _DAY_MS
_INTRADAY_BASELINE_ROWS = 4_500
_DAILY_BASELINE_ROWS = 1_300
_INTRADAY_BARS_PER_DAY = 100
_HOT_TAIL_ROWS_PER_DATASET = 3
_HOT_TAIL_HEAP_FETCH_MARGIN = 64
_SHARED_BUFFER_BLOCK_LIMIT = 5_000


class _ExplainCaptureConnection:
    def __init__(self, conn: Any) -> None:
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


def test_market_projection_history_preserves_exact_daily_points_on_one_covering_read(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        specs = _cross_asset_market_specs()
        _insert_baseline_history(conn, specs=specs)
        conn.commit()
        conn.execute("VACUUM (ANALYZE, FREEZE) market_observations")
        _append_hot_tail(conn, specs=specs)
        conn.commit()
        conn.execute("ANALYZE market_observations")

        history_limits = market_history_limits(spec.dataset_id for spec in specs)
        captured_conn = _ExplainCaptureConnection(conn)
        actual = repositories_for_connection(captured_conn).macro_market.market_projection_history(
            history_limits=history_limits,
            received_before_ms=_CUTOFF_MS,
            row_cap=10_000,
        )
        explain = captured_conn.explain
    finally:
        conn.close()

    expected = _expected_history(specs=specs)
    assert [_semantic_row(row) for row in actual] == expected
    assert explain is not None

    nodes = list(_plan_nodes(explain["Plan"]))
    observation_scans = [node for node in nodes if node.get("Relation Name") == "market_observations"]
    covering_scans = [
        node
        for node in observation_scans
        if node.get("Node Type") == "Index Only Scan"
        and node.get("Index Name") == "idx_market_observations_projection_history"
    ]
    diagnostic = _plan_summary(nodes)
    assert covering_scans, diagnostic
    assert observation_scans == covering_scans, diagnostic
    assert max(int(node.get("Heap Fetches", 0)) for node in covering_scans) <= (
        len(specs) * _HOT_TAIL_ROWS_PER_DATASET + _HOT_TAIL_HEAP_FETCH_MARGIN
    ), diagnostic
    assert _shared_buffer_blocks(explain["Plan"]) <= _SHARED_BUFFER_BLOCK_LIMIT, diagnostic
    assert sum(int(node.get("Temp Read Blocks", 0)) for node in nodes) == 0, diagnostic
    assert sum(int(node.get("Temp Written Blocks", 0)) for node in nodes) == 0, diagnostic
    assert all(node.get("Sort Space Type") != "Disk" for node in nodes), diagnostic


def _cross_asset_market_specs() -> tuple[Any, ...]:
    return tuple(
        DATASET_REGISTRY[dataset_id]
        for dataset_id in MODULE_DATASET_DEPENDENCIES["cross_asset"]
        if DATASET_REGISTRY[dataset_id].fact_family == "market_observation"
    )


def _insert_baseline_history(conn: Any, *, specs: tuple[Any, ...]) -> None:
    with conn.transaction():
        repository = repositories_for_connection(conn).macro_market
        for spec in specs:
            repository.ensure_instrument(spec, now_ms=_BASE_DAY_MS)
        conn.execute(
            """
            WITH requested AS (
              SELECT *
              FROM unnest(
                %s::text[], %s::text[], %s::text[], %s::text[], %s::text[],
                %s::text[], %s::text[], %s::integer[], %s::boolean[]
              ) WITH ORDINALITY AS requested(
                dataset_id, instrument_id, source_id, unit, trust_tier,
                source_url, frequency, row_count, intraday, dataset_no
              )
            ), generated AS (
              SELECT requested.*, row_no
              FROM requested
              CROSS JOIN LATERAL generate_series(0, requested.row_count - 1) AS row_no
            )
            INSERT INTO market_observations(
              observation_id, instrument_id, dataset_id, source_id, field_name,
              value_numeric, unit, observed_at_ms, published_at_ms,
              received_at_ms, trust_tier, source_url, fact_hash, raw_data_json
            )
            SELECT
              'macro-plan-' || lpad(dataset_no::text, 2, '0') || '-'
                || lpad(row_no::text, 5, '0'),
              instrument_id,
              dataset_id,
              source_id,
              'close',
              row_no::double precision,
              unit,
              CASE
                WHEN intraday THEN %s::bigint
                  + (row_no / %s::integer) * %s::bigint
                  + (row_no %% %s::integer) * 300000::bigint
                ELSE %s::bigint + row_no * 129600000::bigint
              END,
              NULL,
              CASE
                WHEN intraday THEN %s::bigint
                  + (row_no / %s::integer) * %s::bigint
                  + (row_no %% %s::integer) * 300000::bigint
                ELSE %s::bigint + row_no * 129600000::bigint
              END + 1000,
              trust_tier,
              source_url,
              'macro-plan-hash-' || lpad(dataset_no::text, 2, '0') || '-'
                || lpad(row_no::text, 5, '0'),
              jsonb_build_object('fixture', frequency, 'row_no', row_no)
            FROM generated
            """,
            (
                [spec.dataset_id for spec in specs],
                [spec.instrument_id for spec in specs],
                [spec.source_id for spec in specs],
                [spec.unit for spec in specs],
                [spec.trust_tier for spec in specs],
                [spec.source_url for spec in specs],
                [spec.frequency for spec in specs],
                [_INTRADAY_BASELINE_ROWS if spec.frequency == "intraday" else _DAILY_BASELINE_ROWS for spec in specs],
                [spec.frequency == "intraday" for spec in specs],
                _BASE_DAY_MS,
                _INTRADAY_BARS_PER_DAY,
                _DAY_MS,
                _INTRADAY_BARS_PER_DAY,
                _BASE_DAY_MS,
                _BASE_DAY_MS,
                _INTRADAY_BARS_PER_DAY,
                _DAY_MS,
                _INTRADAY_BARS_PER_DAY,
                _BASE_DAY_MS,
            ),
        )


def _append_hot_tail(conn: Any, *, specs: tuple[Any, ...]) -> None:
    conn.execute(
        """
        WITH requested AS (
          SELECT *
          FROM unnest(%s::text[], %s::double precision[])
            WITH ORDINALITY AS requested(dataset_id, hot_value, dataset_no)
        ), latest AS (
          SELECT
            requested.*,
            observations.instrument_id,
            observations.source_id,
            observations.unit,
            observations.trust_tier,
            observations.source_url,
            max(observations.observed_at_ms) + 2 * %s::bigint AS observed_at_ms
          FROM requested
          JOIN market_observations AS observations USING (dataset_id)
          GROUP BY
            requested.dataset_id, requested.hot_value, requested.dataset_no,
            observations.instrument_id, observations.source_id,
            observations.unit, observations.trust_tier, observations.source_url
        ), revisions AS (
          SELECT *
          FROM latest
          CROSS JOIN (
            VALUES
              ('superseded', -2::bigint, 0::double precision),
              ('current', -1::bigint, 1::double precision),
              ('future', 1::bigint, 2::double precision)
          ) AS revision(revision_name, received_offset, value_offset)
        )
        INSERT INTO market_observations(
          observation_id, instrument_id, dataset_id, source_id, field_name,
          value_numeric, unit, observed_at_ms, published_at_ms,
          received_at_ms, trust_tier, source_url, fact_hash, raw_data_json
        )
        SELECT
          'macro-plan-hot-' || revision_name || '-' || lpad(dataset_no::text, 2, '0'),
          instrument_id,
          dataset_id,
          source_id,
          'close',
          hot_value + value_offset,
          unit,
          observed_at_ms,
          NULL,
          %s::bigint + received_offset,
          trust_tier,
          source_url,
          'macro-plan-hot-hash-' || revision_name || '-' || lpad(dataset_no::text, 2, '0'),
          jsonb_build_object('fixture', 'hot_tail', 'revision', revision_name)
        FROM revisions
        """,
        (
            [spec.dataset_id for spec in specs],
            [90_000.0 + dataset_no for dataset_no, _spec in enumerate(specs, start=1)],
            _DAY_MS,
            _CUTOFF_MS,
        ),
    )


def _expected_history(*, specs: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    expected: list[tuple[Any, ...]] = []
    for dataset_no, spec in sorted(enumerate(specs, start=1), key=lambda item: item[1].dataset_id):
        if spec.frequency == "intraday":
            for day_no in range(10, 45):
                row_no = day_no * _INTRADAY_BARS_PER_DAY + 99
                observed_at_ms = _BASE_DAY_MS + day_no * _DAY_MS + 99 * 300_000
                expected.append(
                    _baseline_semantic_row(
                        dataset_no=dataset_no,
                        spec=spec,
                        row_no=row_no,
                        observed_at_ms=observed_at_ms,
                        row_number=46 - day_no,
                    )
                )
            latest_observed_at_ms = _BASE_DAY_MS + 44 * _DAY_MS + 99 * 300_000
        else:
            for row_no in range(1_041, 1_300):
                observed_at_ms = _BASE_DAY_MS + row_no * 129_600_000
                expected.append(
                    _baseline_semantic_row(
                        dataset_no=dataset_no,
                        spec=spec,
                        row_no=row_no,
                        observed_at_ms=observed_at_ms,
                        row_number=1_301 - row_no,
                    )
                )
            latest_observed_at_ms = _BASE_DAY_MS + 1_299 * 129_600_000
        expected.append(
            (
                f"macro-plan-hot-current-{dataset_no:02d}",
                spec.dataset_id,
                spec.instrument_id,
                spec.source_id,
                "close",
                90_001.0 + dataset_no,
                spec.unit,
                latest_observed_at_ms + 2 * _DAY_MS,
                None,
                _CUTOFF_MS - 1,
                spec.trust_tier,
                spec.source_url,
                f"macro-plan-hot-hash-current-{dataset_no:02d}",
                1,
            )
        )
    return expected


def _baseline_semantic_row(
    *,
    dataset_no: int,
    spec: Any,
    row_no: int,
    observed_at_ms: int,
    row_number: int,
) -> tuple[Any, ...]:
    return (
        f"macro-plan-{dataset_no:02d}-{row_no:05d}",
        spec.dataset_id,
        spec.instrument_id,
        spec.source_id,
        "close",
        float(row_no),
        spec.unit,
        observed_at_ms,
        None,
        observed_at_ms + 1_000,
        spec.trust_tier,
        spec.source_url,
        f"macro-plan-hash-{dataset_no:02d}-{row_no:05d}",
        row_number,
    )


def _semantic_row(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["observation_id"],
        row["dataset_id"],
        row["instrument_id"],
        row["source_id"],
        row["field_name"],
        float(row["value_numeric"]),
        row["unit"],
        int(row["observed_at_ms"]),
        row["published_at_ms"],
        int(row["received_at_ms"]),
        row["trust_tier"],
        row["source_url"],
        row["fact_hash"],
        int(row["row_number"]),
    )


def _plan_nodes(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


def _shared_buffer_blocks(node: dict[str, Any]) -> int:
    return int(node.get("Shared Hit Blocks", 0)) + int(node.get("Shared Read Blocks", 0))


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
                "Rows Removed by Filter",
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
