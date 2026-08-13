from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from psycopg import pq

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.macro import ReleaseFact, SeriesFact, require_dataset
from tracefold.macro.calculations import calculate_series_statistics
from tracefold.macro.dependencies import (
    MODULE_DATASET_DEPENDENCIES,
    module_input_fingerprint,
    module_projection_version,
)
from tracefold.macro.projection import (
    MacroProjectionService,
    compute_macro_module_projection,
    rebuild_all_macro_modules_for_maintenance,
)
from tracefold.market import MarketObservationFact
from tracefold.platform.postgres.projection_frontier import MACRO_FRONTIER

NOW_MS = 1_779_000_000_000


class _SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        try:
            with repository_session_for_connection(self.conn) as repos:
                yield repos
        finally:
            if self.conn.info.transaction_status != pq.TransactionStatus.IDLE:
                self.conn.rollback()


def test_macro_projection_processes_one_module_with_no_database_during_compute() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        _seed_rates_frontier(conn)
        service = _service(conn)
        runtime_id = str(uuid4())

        due = service.next_due_module(now_ms=NOW_MS + 60_000)
        assert due is not None
        assert due["module_id"] == "rates_fed"
        claim = service.claim_module(
            module_id="rates_fed",
            runtime_id=runtime_id,
            now_ms=NOW_MS + 60_000,
        )
        assert claim is not None

        loaded = service.load_module(claim, now_ms=NOW_MS + 60_000)
        assert loaded["status"] == "loaded"
        assert conn.info.transaction_status == pq.TransactionStatus.IDLE

        output = compute_macro_module_projection(loaded)
        assert conn.info.transaction_status == pq.TransactionStatus.IDLE
        assert output["module_id"] == "rates_fed"

        result = service.publish_module(
            claim,
            output,
            now_ms=NOW_MS + 60_100,
        )
        assert result["projection_status"] == "published"
        assert conn.execute("SELECT count(*) AS count FROM macro_module_current").fetchone()["count"] == 1
        frontier = conn.execute(
            """
            SELECT status, attempt_count, first_dirty_at_ms, deadline_at_ms
            FROM macro_module_frontiers
            WHERE module_id = 'rates_fed'
            """
        ).fetchone()
        assert frontier == {
            "status": "clean",
            "attempt_count": 0,
            "first_dirty_at_ms": None,
            "deadline_at_ms": None,
        }
    finally:
        conn.close()


def test_macro_projection_publish_rejects_changed_dataset_fingerprint() -> None:
    conn = connect_postgres_test()
    writer_conn = None
    try:
        reset_postgres_schema(conn)
        _seed_rates_frontier(conn)
        writer_conn = connect_postgres_test()
        service = _service(conn)
        runtime_id = str(uuid4())
        claim = service.claim_module(
            module_id="rates_fed",
            runtime_id=runtime_id,
            now_ms=NOW_MS + 60_000,
        )
        assert claim is not None
        loaded = service.load_module(claim, now_ms=NOW_MS + 60_000)
        output = compute_macro_module_projection(loaded)

        with repository_session_for_connection(writer_conn) as repos, repos.transaction():
            repos.macro.upsert_dataset_projection_state(
                dataset_id="fred.dgs10",
                material_fingerprint="sha256:changed",
                acquisition_status="current",
                source_frontier_ms=NOW_MS + 1,
                updated_at_ms=NOW_MS + 1,
            )
            states = repos.macro.dataset_projection_states(
                dataset_ids=MODULE_DATASET_DEPENDENCIES["rates_fed"],
            )
            repos.projection_frontiers.mark_dirty(
                MACRO_FRONTIER,
                key={"module_id": "rates_fed"},
                dirty_at_ms=NOW_MS + 1,
                deadline_at_ms=NOW_MS + 60_001,
                input_fingerprint=module_input_fingerprint("rates_fed", states),
                version=module_projection_version("rates_fed"),
                extra_insert={"source_frontier_ms": NOW_MS + 1},
            )

        result = service.publish_module(claim, output, now_ms=NOW_MS + 60_100)
        assert result["projection_status"] == "stale_snapshot"
        assert result["rows_written"] == 0
        assert conn.execute("SELECT count(*) AS count FROM macro_module_current").fetchone()["count"] == 0
        frontier = conn.execute(
            """
            SELECT status, attempt_count, input_fingerprint
            FROM macro_module_frontiers
            WHERE module_id = 'rates_fed'
            """
        ).fetchone()
        assert frontier["status"] == "dirty"
        assert frontier["attempt_count"] == 0
        assert frontier["input_fingerprint"] != claim.input_fingerprint
    finally:
        if writer_conn is not None:
            writer_conn.close()
        conn.close()


def test_rates_projection_uses_target_state_for_shared_sofr_dependency() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        spec = require_dataset("fred.sofr")
        with repository_session_for_connection(conn) as repos, repos.transaction():
            assert (
                repos.macro.ensure_target(
                    spec,
                    now_ms=NOW_MS,
                    max_attempts=5,
                    reactivate_unavailable=False,
                )
                == 1
            )
            target = repos.macro.claim_target(
                clock_kind=spec.clock_kind,
                lease_owner="sofr-fixture",
                lease_ms=45_000,
                now_ms=NOW_MS,
                target_keys=(spec.target_key,),
            )
            assert target is not None
            assert repos.macro.complete_target(
                target_key=spec.target_key,
                lease_owner="sofr-fixture",
                cursor={"reference_date": "2026-05-18"},
                next_due_at_ms=NOW_MS + spec.refresh_seconds * 1_000,
                completed_at_ms=NOW_MS,
            )
            assert (
                repos.macro.insert_series_fact(
                    SeriesFact(
                        dataset_id=spec.dataset_id,
                        series_id=spec.series_id,
                        reference_date=date(2026, 5, 18),
                        vintage_date=date(2026, 5, 18),
                        value_numeric=4.31,
                        value_text=None,
                        unit=spec.unit,
                        published_at_ms=NOW_MS,
                        received_at_ms=NOW_MS,
                        source_url=spec.source_url,
                        raw_data={"fixture": "shared-sofr-rates-input"},
                    )
                )
                == 1
            )

        result = rebuild_all_macro_modules_for_maintenance(
            db=_SingleConnectionDB(conn),
            now_ms=NOW_MS,
        )
        with repository_session_for_connection(conn) as repos:
            persisted = repos.macro.module_current("rates_fed")
    finally:
        conn.close()

    assert result["modules_computed"] == 6
    assert persisted is not None
    sofr_state = next(
        state for state in persisted["payload_json"]["evidence"]["dataset_states"] if state["dataset_id"] == "fred.sofr"
    )
    assert sofr_state["required_for_current"] is True
    assert sofr_state["current_health"] == "current"
    assert sofr_state["current_reason"]["code"] == "within_freshness_budget"


def test_macro_maintenance_reseeds_clean_unchanged_frontiers() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        service = _service(conn)

        service.prepare_maintenance_frontiers(now_ms=NOW_MS)
        conn.execute(
            """
            UPDATE macro_module_frontiers
               SET status = 'clean',
                   first_dirty_at_ms = NULL,
                   deadline_at_ms = NULL
            """
        )
        service.prepare_maintenance_frontiers(now_ms=NOW_MS + 1)

        rows = conn.execute(
            """
            SELECT module_id, status, deadline_at_ms
            FROM macro_module_frontiers
            ORDER BY module_id
            """
        ).fetchall()
        assert len(rows) == 6
        assert all(row["status"] == "dirty" for row in rows)
        assert all(row["deadline_at_ms"] == NOW_MS + 1 for row in rows)
    finally:
        conn.close()


def test_macro_startup_reconcile_restores_missing_and_old_version_frontiers_only() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        service = _service(conn)
        service.prepare_maintenance_frontiers(now_ms=NOW_MS)
        conn.execute(
            """
            UPDATE macro_module_frontiers
               SET status = 'clean',
                   first_dirty_at_ms = NULL,
                   deadline_at_ms = NULL,
                   updated_at_ms = %s
            """,
            (NOW_MS + 1,),
        )
        conn.execute(
            """
            DELETE FROM macro_module_frontiers
            WHERE module_id IN ('rates_fed', 'economy_inflation', 'cross_asset')
            """
        )
        conn.execute(
            """
            UPDATE macro_module_frontiers
               SET projection_version = 'legacy-version'
             WHERE module_id = 'volatility'
            """
        )

        assert service.reconcile_frontiers(now_ms=NOW_MS + 2) == 4

        rows = conn.execute(
            """
            SELECT
              module_id, status, projection_version, first_dirty_at_ms,
              deadline_at_ms, updated_at_ms
            FROM macro_module_frontiers
            ORDER BY module_id
            """
        ).fetchall()
        assert len(rows) == 6
        by_module = {str(row["module_id"]): row for row in rows}
        dirty_modules = {
            "rates_fed",
            "economy_inflation",
            "cross_asset",
            "volatility",
        }
        assert {module_id for module_id, row in by_module.items() if row["status"] == "dirty"} == dirty_modules
        assert all(
            row["projection_version"] == module_projection_version(module_id) for module_id, row in by_module.items()
        )
        assert all(by_module[module_id]["first_dirty_at_ms"] == NOW_MS + 2 for module_id in dirty_modules)
        assert all(by_module[module_id]["deadline_at_ms"] == NOW_MS + 2 for module_id in dirty_modules)
        assert by_module["credit"]["updated_at_ms"] == NOW_MS + 1
        assert by_module["liquidity_funding"]["updated_at_ms"] == NOW_MS + 1
    finally:
        conn.close()


def test_macro_startup_reconcile_does_not_invent_frontiers_without_dataset_state() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)

        assert _service(conn).reconcile_frontiers(now_ms=NOW_MS) == 0
        assert conn.execute("SELECT count(*) AS count FROM macro_module_frontiers").fetchone()["count"] == 0
    finally:
        conn.close()


def test_macro_series_reducer_preserves_capped_history_percentile_semantics() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        spec = require_dataset("fred.bamlc0a0cm")
        first_date = date(2024, 1, 1)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            for index in range(600):
                reference_date = first_date + timedelta(days=index)
                repos.macro.insert_series_fact(
                    SeriesFact(
                        dataset_id=spec.dataset_id,
                        series_id=spec.series_id,
                        reference_date=reference_date,
                        vintage_date=reference_date,
                        value_numeric=float(600 - index),
                        value_text=None,
                        unit=spec.unit,
                        published_at_ms=NOW_MS + index,
                        received_at_ms=NOW_MS + index,
                        source_url=spec.source_url,
                        raw_data={"index": index},
                    )
                )
        with repository_session_for_connection(conn) as repos:
            rows = repos.macro.series_projection_history(
                history_limits={spec.dataset_id: 10_000},
                row_cap=10_000,
            )

        assert len(rows) == 500
        statistics = calculate_series_statistics(
            rows,
            (spec.dataset_id,),
            percentile_dataset_ids=frozenset({spec.dataset_id}),
        )
        assert statistics[0]["sample_count"] == 600
        assert statistics[0]["history_start"] == str(first_date)
        assert statistics[0]["percentile"] == 0.17
        assert len(statistics[0]["history"]) == 500
    finally:
        conn.close()


def test_macro_market_history_keeps_the_last_fact_for_the_latest_actual_days() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        spec = require_dataset("yfinance.spy.intraday")
        day_ms = 86_400_000
        first_day_ms = 1_778_112_000_000
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.macro_market.ensure_instrument(spec, now_ms=first_day_ms)
            for day in range(3):
                for observation in range(2):
                    observed_at_ms = first_day_ms + day * day_ms + observation * 3_600_000
                    repos.macro_market.insert_observation(
                        MarketObservationFact(
                            dataset_id=spec.dataset_id,
                            instrument_id=str(spec.instrument_id),
                            source_id=spec.source_id,
                            field_name="close",
                            value_numeric=float(day * 10 + observation),
                            unit=spec.unit,
                            observed_at_ms=observed_at_ms,
                            published_at_ms=None,
                            received_at_ms=observed_at_ms + 1_000,
                            trust_tier=spec.trust_tier,
                            source_url=spec.source_url,
                            raw_data={"day": day, "observation": observation},
                        )
                    )
        with repository_session_for_connection(conn) as repos:
            rows = repos.macro_market.market_projection_history(
                history_limits={spec.dataset_id: 2},
                row_cap=10,
            )

        assert [float(row["value_numeric"]) for row in rows] == [11.0, 21.0]
        assert [int(row["row_number"]) for row in rows] == [2, 1]
    finally:
        conn.close()


def test_economy_projection_loads_only_the_recent_release_window() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        spec = require_dataset("bls.cpi.release")
        with repository_session_for_connection(conn) as repos, repos.transaction():
            for period_index in range(30):
                year = 2024 + period_index // 12
                month = period_index % 12 + 1
                reference_period = f"{year}-M{month:02d}"
                for revision, value in enumerate((float(period_index), float(period_index) + 0.5)):
                    repos.macro.insert_release_fact(
                        ReleaseFact(
                            dataset_id=spec.dataset_id,
                            release_id=f"BLS:{spec.series_id}:{reference_period}",
                            series_id=str(spec.series_id),
                            reference_period=reference_period,
                            scheduled_at_ms=None,
                            published_at_ms=None,
                            received_at_ms=period_index * 10 + revision + 1,
                            actual_value=value,
                            prior_value=None,
                            revised_prior_value=None,
                            estimate_value=None,
                            unit=spec.unit,
                            importance_tier=3,
                            source_url=spec.source_url,
                            raw_data={"revision": revision},
                        )
                    )
            for dataset_id in MODULE_DATASET_DEPENDENCIES["economy_inflation"]:
                repos.macro.upsert_dataset_projection_state(
                    dataset_id=dataset_id,
                    material_fingerprint=(
                        "sha256:bounded-release-history"
                        if dataset_id == spec.dataset_id
                        else f"sha256:missing:{dataset_id}"
                    ),
                    acquisition_status="current" if dataset_id == spec.dataset_id else "uninitialized",
                    source_frontier_ms=300 if dataset_id == spec.dataset_id else 0,
                    updated_at_ms=NOW_MS,
                )
            states = repos.macro.dataset_projection_states(
                dataset_ids=MODULE_DATASET_DEPENDENCIES["economy_inflation"],
            )
            repos.projection_frontiers.mark_dirty(
                MACRO_FRONTIER,
                key={"module_id": "economy_inflation"},
                dirty_at_ms=NOW_MS,
                deadline_at_ms=NOW_MS,
                input_fingerprint=module_input_fingerprint("economy_inflation", states),
                version=module_projection_version("economy_inflation"),
                extra_insert={"source_frontier_ms": 300},
            )

        service = _service(conn)
        claim = service.claim_module(
            module_id="economy_inflation",
            runtime_id=str(uuid4()),
            now_ms=NOW_MS,
        )
        assert claim is not None
        loaded = service.load_module(claim, now_ms=NOW_MS)
    finally:
        conn.close()

    cpi_rows = [row for row in loaded["release_rows"] if row["dataset_id"] == spec.dataset_id]
    assert len(cpi_rows) == 24
    assert cpi_rows[0]["reference_period"] == "2024-M07"
    assert float(cpi_rows[0]["actual_value"]) == 6.5
    assert cpi_rows[-1]["reference_period"] == "2026-M06"
    assert float(cpi_rows[-1]["actual_value"]) == 29.5


def _seed_rates_frontier(conn: Any) -> None:
    spec = require_dataset("fred.dgs10")
    with repository_session_for_connection(conn) as repos, repos.transaction():
        repos.macro.insert_series_fact(
            SeriesFact(
                dataset_id=spec.dataset_id,
                series_id=spec.series_id,
                reference_date=date(2026, 7, 30),
                vintage_date=date(2026, 7, 30),
                value_numeric=4.25,
                value_text=None,
                unit=spec.unit,
                published_at_ms=NOW_MS,
                received_at_ms=NOW_MS,
                source_url=spec.source_url,
                raw_data={"fixture": "module-local-projection"},
            )
        )
        for dataset_id in MODULE_DATASET_DEPENDENCIES["rates_fed"]:
            repos.macro.upsert_dataset_projection_state(
                dataset_id=dataset_id,
                material_fingerprint=("sha256:dgs10" if dataset_id == "fred.dgs10" else f"sha256:missing:{dataset_id}"),
                acquisition_status="current" if dataset_id == "fred.dgs10" else "uninitialized",
                source_frontier_ms=NOW_MS if dataset_id == "fred.dgs10" else 0,
                updated_at_ms=NOW_MS,
            )
        states = repos.macro.dataset_projection_states(
            dataset_ids=MODULE_DATASET_DEPENDENCIES["rates_fed"],
        )
        repos.projection_frontiers.mark_dirty(
            MACRO_FRONTIER,
            key={"module_id": "rates_fed"},
            dirty_at_ms=NOW_MS,
            deadline_at_ms=NOW_MS + 60_000,
            input_fingerprint=module_input_fingerprint("rates_fed", states),
            version=module_projection_version("rates_fed"),
            extra_insert={"source_frontier_ms": NOW_MS},
        )


def _service(conn: Any) -> MacroProjectionService:
    return MacroProjectionService(db=_SingleConnectionDB(conn))
