from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from psycopg import pq

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.macro import SeriesFact, require_dataset
from tracefold.macro.calculations import calculate_series_statistics
from tracefold.macro.dependencies import (
    MODULE_DATASET_DEPENDENCIES,
    module_input_fingerprint,
    module_projection_version,
)
from tracefold.macro.projection import (
    MacroProjectionService,
    compute_macro_module_projection,
)
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
    return MacroProjectionService(
        db=_SingleConnectionDB(conn),
        settings=SimpleNamespace(),
        backfill_worker_enabled=False,
    )
