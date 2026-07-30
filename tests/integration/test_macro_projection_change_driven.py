from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any

from psycopg import pq

import tracefold.macro.projection as projection_module
from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.macro import SeriesFact, require_dataset
from tracefold.macro.projection import MacroProjectionService

NOW_MS = 1_779_000_000_000


class _SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        with repository_session_for_connection(self.conn) as repos:
            yield repos


def test_macro_projection_skips_unchanged_inputs_and_computes_outside_transaction(
    monkeypatch: Any,
) -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        transaction_states: list[pq.TransactionStatus] = []
        real_calculate_features = projection_module.calculate_features
        real_build_typed_module_payload = projection_module.build_typed_module_payload

        def checked_calculate_features(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            transaction_states.append(conn.info.transaction_status)
            return real_calculate_features(rows)

        def checked_build_typed_module_payload(**kwargs: Any) -> dict[str, Any]:
            transaction_states.append(conn.info.transaction_status)
            return real_build_typed_module_payload(**kwargs)

        monkeypatch.setattr(projection_module, "calculate_features", checked_calculate_features)
        monkeypatch.setattr(
            projection_module,
            "build_typed_module_payload",
            checked_build_typed_module_payload,
        )
        service = MacroProjectionService(
            db=_SingleConnectionDB(conn),
            settings=SimpleNamespace(statement_timeout_seconds=30),
            backfill_worker_enabled=True,
            clock_ms=lambda: NOW_MS,
        )

        first = service.rebuild(now_ms=NOW_MS)
        assert first["projection_status"] == "rebuilt"
        assert first["modules_computed"] == 6
        assert transaction_states
        assert set(transaction_states) == {pq.TransactionStatus.IDLE}

        transaction_states.clear()
        second = service.rebuild(now_ms=NOW_MS + 60_000)
        assert second["projection_status"] == "unchanged_input"
        assert second["modules_computed"] == 0
        assert second["features_computed"] == 0
        assert second["source_rows_loaded"] == {}
        assert transaction_states == []

        spec = require_dataset("fred.dgs10")
        with repository_session_for_connection(conn) as repos, repos.transaction():
            assert (
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
                        raw_data={"fixture": "change-driven-projection"},
                    )
                )
                == 1
            )

        third = service.rebuild(now_ms=NOW_MS + 120_000)
        assert third["projection_status"] == "rebuilt"
        assert third["modules_computed"] == 6
        assert third["source_rows"] == 1
        assert set(transaction_states) == {pq.TransactionStatus.IDLE}
    finally:
        conn.close()


def test_macro_projection_rejects_stale_snapshot_and_rebuilds_latest_fact(
    monkeypatch: Any,
) -> None:
    conn = connect_postgres_test()
    writer_conn = None
    try:
        reset_postgres_schema(conn)
        writer_conn = connect_postgres_test()
        spec = require_dataset("fred.dgs10")
        inserted = False
        real_calculate_features = projection_module.calculate_features

        def advance_fact_after_snapshot(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal inserted
            features = real_calculate_features(rows)
            if not inserted:
                inserted = True
                with repository_session_for_connection(writer_conn) as repos, repos.transaction():
                    assert (
                        repos.macro.insert_series_fact(
                            SeriesFact(
                                dataset_id=spec.dataset_id,
                                series_id=spec.series_id,
                                reference_date=date(2026, 7, 30),
                                vintage_date=date(2026, 7, 30),
                                value_numeric=4.5,
                                value_text=None,
                                unit=spec.unit,
                                published_at_ms=NOW_MS,
                                received_at_ms=NOW_MS,
                                source_url=spec.source_url,
                                raw_data={"fixture": "stale-snapshot"},
                            )
                        )
                        == 1
                    )
            return features

        monkeypatch.setattr(
            projection_module,
            "calculate_features",
            advance_fact_after_snapshot,
        )
        service = MacroProjectionService(
            db=_SingleConnectionDB(conn),
            settings=SimpleNamespace(statement_timeout_seconds=30),
            backfill_worker_enabled=True,
            clock_ms=lambda: NOW_MS,
        )

        stale = service.rebuild(now_ms=NOW_MS)
        assert stale["projection_status"] == "stale_snapshot"
        assert stale["rows_written"] == 0
        assert conn.execute("SELECT count(*) AS count FROM macro_projection_state").fetchone()["count"] == 0

        rebuilt = service.rebuild(now_ms=NOW_MS + 1)
        assert rebuilt["projection_status"] == "rebuilt"
        assert rebuilt["modules_computed"] == 6
        assert rebuilt["source_rows"] == 1

        unchanged = service.rebuild(now_ms=NOW_MS + 2)
        assert unchanged["projection_status"] == "unchanged_input"
        assert unchanged["modules_computed"] == 0
    finally:
        if writer_conn is not None:
            writer_conn.close()
        conn.close()
