from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.macro import FetchBatch, SeriesFact
from tracefold.macro.acquisition import MacroAcquisitionService


class _TestDb:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        with repository_session_for_connection(self.conn) as repos:
            yield repos


class _FixedFredClient:
    def fetch(
        self,
        spec: Any,
        *,
        partition_key: str,
        cursor: dict[str, Any],
        now_ms: int | None = None,
    ) -> FetchBatch:
        received_at_ms = int(now_ms or 0)
        return FetchBatch(
            dataset_id=spec.dataset_id,
            partition_key=partition_key,
            facts=(
                SeriesFact(
                    dataset_id=spec.dataset_id,
                    series_id=spec.series_id,
                    reference_date=date(2026, 7, 25),
                    vintage_date=date(2026, 7, 27),
                    value_numeric=4.25,
                    value_text=None,
                    unit=spec.unit,
                    published_at_ms=None,
                    received_at_ms=received_at_ms,
                    source_url=spec.source_url,
                    raw_data={"date": "2026-07-25", "value": "4.25"},
                ),
            ),
            cursor={"reference_date": "2026-07-25"},
            response_hash="sha256:fixed",
            source_url=spec.source_url,
            http_status=200,
        )

    def close(self) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000

    def __call__(self) -> int:
        self.now += 1_000
        return self.now


def test_acquisition_replay_writes_one_fact_and_two_receipts_without_legacy_storage(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        clock = _Clock()
        service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_official_state",
            clock_kind="official_state",
            settings=SimpleNamespace(
                max_attempts=3,
                lease_ms=60_000,
                retry_ms=60_000,
                statement_timeout_seconds=30,
            ),
            source_client=_FixedFredClient(),
            clock_ms=clock,
        )
        service.ensure_targets()
        conn.execute(
            """
            UPDATE macro_acquisition_targets
            SET next_due_at_ms = CASE
              WHEN target_key = 'fred.dgs10:latest' THEN 0
              ELSE 253402300799000
            END
            WHERE clock_kind = 'official_state'
            """
        )
        conn.commit()
        first = service.run_once()
        conn.execute(
            """
            UPDATE macro_acquisition_targets
            SET next_due_at_ms = 0
            WHERE target_key = 'fred.dgs10:latest'
            """
        )
        conn.commit()
        second = service.run_once()

        fact_count = conn.execute(
            """
            SELECT COUNT(*)::int AS count
            FROM macro_series_facts
            WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchone()["count"]
        receipt_count = conn.execute(
            """
            SELECT COUNT(*)::int AS count
            FROM macro_source_receipts
            WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchone()["count"]
        legacy_tables = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN (
                'macro_observations', 'macro_sync_windows', 'macro_sync_runs'
              )
            """
        ).fetchall()
    finally:
        conn.close()

    assert first is not None and first["rows_inserted"] == 1
    assert second is not None and second["rows_inserted"] == 0
    assert fact_count == 1
    assert receipt_count == 2
    assert legacy_tables == []


def test_all_macro_history_queries_accept_an_absent_cutoff(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos:
            assert repos.macro.series_history(dataset_ids=("fred.dgs10",)) == []
            assert repos.macro.release_history(dataset_ids=("bls.cpi.release",)) == []
            assert repos.macro.document_history(dataset_ids=("federal_reserve.monetary_policy.documents",)) == []
            assert repos.macro_market.market_history(dataset_ids=("nasdaq.spy.history",)) == []
            assert repos.macro_market.settlement_history(dataset_ids=("cboe.cfe.vx.settlement",)) == []
            assert repos.macro_market.position_history(dataset_ids=("cftc.tff.rates_positions",)) == []
    finally:
        conn.close()
