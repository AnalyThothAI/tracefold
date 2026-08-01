from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.macro import FetchBatch, SeriesFact, require_dataset
from tracefold.macro.acquisition import MacroAcquisitionService
from tracefold.market import MarketObservationFact, MarketSettlementFact


class _TestDb:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        with repository_session_for_connection(self.conn) as repos:
            yield repos


class _FixedFredClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(
        self,
        spec: Any,
        *,
        partition_key: str,
        cursor: dict[str, Any],
        now_ms: int | None = None,
    ) -> FetchBatch:
        self.calls += 1
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
            response_hash=f"sha256:fixed:{self.calls}",
            source_url=spec.source_url,
            http_status=200,
        )

    def close(self) -> None:
        return None


class _FixedYFinanceClient:
    def fetch(
        self,
        spec: Any,
        *,
        partition_key: str,
        cursor: dict[str, Any],
        now_ms: int | None = None,
    ) -> FetchBatch:
        received_at_ms = int(now_ms or 0)
        observed_at_ms = received_at_ms - 900_000
        return FetchBatch(
            dataset_id=spec.dataset_id,
            partition_key=partition_key,
            facts=(
                MarketObservationFact(
                    dataset_id=spec.dataset_id,
                    instrument_id=str(spec.instrument_id),
                    source_id=spec.source_id,
                    field_name="close",
                    value_numeric=738.5,
                    unit=spec.unit,
                    observed_at_ms=observed_at_ms,
                    published_at_ms=None,
                    received_at_ms=received_at_ms,
                    trust_tier=spec.trust_tier,
                    source_url=spec.source_url,
                    raw_data={"provider_symbol": spec.series_id, "interval": "5m"},
                ),
            ),
            cursor={"observed_at_ms": observed_at_ms},
            response_hash="sha256:yfinance-fixed",
            source_url=spec.source_url,
            diagnostics={"provider": "yfinance", "provider_delay_seconds": 900},
        )

    def close(self) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000

    def __call__(self) -> int:
        self.now += 1_000
        return self.now


class _EmptyCompletedBackfillClient:
    def fetch(
        self,
        spec: Any,
        *,
        partition_key: str,
        cursor: dict[str, Any],
        now_ms: int | None = None,
    ) -> FetchBatch:
        return FetchBatch(
            dataset_id=spec.dataset_id,
            partition_key=partition_key,
            facts=(),
            cursor={**cursor, "backfill_complete": True},
            response_hash="sha256:empty",
            source_url=spec.source_url,
            http_status=200,
        )


class _FailingClient:
    def fetch(self, *_args: Any, **_kwargs: Any) -> FetchBatch:
        raise RuntimeError("official_source_failed")


def test_explicit_backfill_claims_only_requested_target_keys(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            requested = repos.macro.enqueue_backfill_target(
                require_dataset("fred.dgs10"),
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2),
                now_ms=1_000,
                max_attempts=5,
            )
            unrequested = repos.macro.enqueue_backfill_target(
                require_dataset("fred.dgs2"),
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2),
                now_ms=1_000,
                max_attempts=5,
            )
        service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_backfill",
            clock_kind="backfill",
            source_client=_EmptyCompletedBackfillClient(),
            clock_ms=lambda: 2_000,
            target_keys=(str(requested["target_key"]),),
        )

        result = service.run_once()
        states = {
            str(row["target_key"]): str(row["status"])
            for row in conn.execute(
                """
                SELECT target_key, status
                FROM macro_acquisition_targets
                WHERE clock_kind = 'backfill'
                """
            ).fetchall()
        }
    finally:
        conn.close()

    assert result is not None
    assert result["dataset_id"] == "fred.dgs10"
    assert states[str(requested["target_key"])] == "current"
    assert states[str(unrequested["target_key"])] == "backfilling"


def test_acquisition_replay_writes_one_fact_and_keeps_current_cursor(
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
        first_material_fingerprint = conn.execute(
            """
            SELECT material_fingerprint
            FROM macro_dataset_projection_states
            WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchone()["material_fingerprint"]
        conn.execute(
            """
            UPDATE macro_acquisition_targets
            SET next_due_at_ms = 0
            WHERE target_key = 'fred.dgs10:latest'
            """
        )
        conn.commit()
        second = service.run_once()
        projection_states = conn.execute(
            """
            SELECT dataset_id, material_fingerprint, acquisition_status,
                   source_frontier_ms
            FROM macro_dataset_projection_states
            WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchall()
        module_frontiers = conn.execute(
            """
            SELECT module_id, status, first_dirty_at_ms, deadline_at_ms,
                   input_fingerprint, projection_version
            FROM macro_module_frontiers
            WHERE module_id IN ('rates_fed', 'credit')
            ORDER BY module_id
            """
        ).fetchall()

        fact_count = conn.execute(
            """
            SELECT COUNT(*)::int AS count
            FROM macro_series_facts
            WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchone()["count"]
        target = conn.execute(
            """
            SELECT status, cursor_json, last_success_at_ms
            FROM macro_acquisition_targets
            WHERE target_key = 'fred.dgs10:latest'
            """
        ).fetchone()
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
    assert target["status"] == "current"
    assert target["cursor_json"] == {"reference_date": "2026-07-25"}
    assert target["last_success_at_ms"] is not None
    assert legacy_tables == []
    assert len(projection_states) == 1
    assert projection_states[0]["acquisition_status"] == "current"
    assert projection_states[0]["source_frontier_ms"] == 3_000
    assert projection_states[0]["material_fingerprint"] == first_material_fingerprint
    assert [row["module_id"] for row in module_frontiers] == ["credit", "rates_fed"]
    assert all(row["status"] == "dirty" for row in module_frontiers)
    assert all(row["deadline_at_ms"] - row["first_dirty_at_ms"] == 60_000 for row in module_frontiers)


def test_acquisition_failure_does_not_replace_the_material_fingerprint(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        clock = _Clock()
        fixed_service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_official_state",
            clock_kind="official_state",
            source_client=_FixedFredClient(),
            clock_ms=clock,
            target_keys=("fred.dgs10:latest",),
        )
        failing_service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_official_state",
            clock_kind="official_state",
            source_client=_FailingClient(),
            clock_ms=clock,
            target_keys=("fred.dgs10:latest",),
        )
        fixed_service.ensure_targets(now_ms=1_000)

        first = fixed_service.run_once()
        first_fingerprint = conn.execute(
            """
            SELECT material_fingerprint
            FROM macro_dataset_projection_states
            WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchone()["material_fingerprint"]

        conn.execute(
            """
            UPDATE macro_acquisition_targets
            SET next_due_at_ms = 0
            WHERE target_key = 'fred.dgs10:latest'
            """
        )
        conn.commit()
        failed = failing_service.run_once()
        failure_fingerprint = conn.execute(
            """
            SELECT material_fingerprint
            FROM macro_dataset_projection_states
            WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchone()["material_fingerprint"]

        conn.execute(
            """
            UPDATE macro_acquisition_targets
            SET next_due_at_ms = 0
            WHERE target_key = 'fred.dgs10:latest'
            """
        )
        conn.commit()
        replay = fixed_service.run_once()
        final_state = conn.execute(
            """
            SELECT material_fingerprint, acquisition_status
            FROM macro_dataset_projection_states
            WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchone()
    finally:
        conn.close()

    assert first is not None and first["rows_inserted"] == 1
    assert failed is not None and failed["status"] == "failed"
    assert replay is not None and replay["rows_inserted"] == 0
    assert failure_fingerprint == first_fingerprint
    assert final_state["material_fingerprint"] == first_fingerprint
    assert final_state["acquisition_status"] == "current"


def test_acquisition_lost_claim_does_not_publish_facts(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_official_state",
            clock_kind="official_state",
            source_client=_FixedFredClient(),
            clock_ms=lambda: 3_000,
            target_keys=("fred.dgs10:latest",),
        )
        service.ensure_targets(now_ms=1_000)
        claim = service.claim_next(now_ms=2_000)
        assert claim is not None
        batch = service.fetch_claim(claim)
        conn.execute(
            """
            UPDATE macro_acquisition_targets
               SET status = 'current', lease_owner = NULL, leased_until_ms = NULL
             WHERE target_key = 'fred.dgs10:latest'
            """
        )
        conn.commit()

        result = service.publish_success(claim, batch)
        fact_count = conn.execute(
            """
            SELECT COUNT(*)::int AS count
              FROM macro_series_facts
             WHERE dataset_id = 'fred.dgs10'
            """
        ).fetchone()["count"]
    finally:
        conn.close()

    assert result is None
    assert fact_count == 0


def test_yfinance_intraday_acquisition_persists_market_fact_and_cursor(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        clock = _Clock()
        clock.now = 2_000_000
        service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_intraday_market",
            clock_kind="intraday_market",
            source_client=_FixedYFinanceClient(),
            clock_ms=clock,
        )
        assert service.ensure_targets() > 0
        conn.execute(
            """
            UPDATE macro_acquisition_targets
            SET next_due_at_ms = CASE
              WHEN target_key = 'yfinance.spy.intraday:latest' THEN 0
              ELSE 253402300799000
            END
            WHERE clock_kind = 'intraday_market'
            """
        )
        conn.commit()

        result = service.run_once()
        observation = conn.execute(
            """
            SELECT dataset_id, instrument_id, source_id, value_numeric,
                   observed_at_ms, received_at_ms, trust_tier
              FROM market_observations
             WHERE dataset_id = 'yfinance.spy.intraday'
            """
        ).fetchone()
        target = conn.execute(
            """
            SELECT status, cursor_json, last_success_at_ms
              FROM macro_acquisition_targets
             WHERE target_key = 'yfinance.spy.intraday:latest'
            """
        ).fetchone()
    finally:
        conn.close()

    assert result is not None
    assert result["status"] == "current"
    assert result["rows_inserted"] == 1
    assert observation["dataset_id"] == "yfinance.spy.intraday"
    assert observation["instrument_id"] == "spy"
    assert observation["source_id"] == "yahoo_finance"
    assert float(observation["value_numeric"]) == 738.5
    assert observation["trust_tier"] == "untrusted_proxy"
    assert target["status"] == "current"
    assert target["cursor_json"]["observed_at_ms"] == observation["observed_at_ms"]
    assert target["last_success_at_ms"] >= observation["received_at_ms"]


def test_all_macro_history_queries_accept_an_absent_cutoff(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos:
            assert repos.macro.series_history(history_limits={"fred.dgs10": 500}) == []
            assert repos.macro.release_history(dataset_ids=("bls.cpi.release",)) == []
            assert repos.macro.document_history(dataset_ids=("federal_reserve.fomc.documents",)) == []
            assert repos.macro_market.market_history(history_limits={"yfinance.spy.intraday": 5_000}) == []
            assert repos.macro_market.settlement_history(dataset_ids=("cboe.cfe.vx.settlement",)) == []
            assert repos.macro_market.position_history(dataset_ids=("cftc.tff.rates_positions",)) == []
    finally:
        conn.close()


def test_settlement_history_collapses_revisions_at_the_requested_cutoff(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        spec = require_dataset("cboe.cfe.vx.settlement")
        assert spec.instrument_id is not None
        common = {
            "fact_schema_version": "market_settlement_v2",
            "dataset_id": spec.dataset_id,
            "instrument_id": spec.instrument_id,
            "source_id": spec.source_id,
            "trade_date": date(2026, 7, 27),
            "contract_code": "VX/U6",
            "contract_expiration_date": date(2026, 9, 16),
            "open_interest": 10_000.0,
            "volume": 5_000.0,
            "unit": spec.unit,
            "published_at_ms": 100,
            "source_url": spec.source_url,
            "raw_data": {},
        }
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.macro_market.ensure_instrument(spec, now_ms=50)
            assert (
                repos.macro_market.insert_settlement(
                    MarketSettlementFact(
                        **common,
                        settlement_price=19.2,
                        received_at_ms=100,
                    )
                )
                == 1
            )
            assert (
                repos.macro_market.insert_settlement(
                    MarketSettlementFact(
                        **common,
                        settlement_price=19.8,
                        received_at_ms=200,
                    )
                )
                == 1
            )
        with repository_session_for_connection(conn) as repos:
            current = repos.macro_market.settlement_history(dataset_ids=(spec.dataset_id,))
            historical = repos.macro_market.settlement_history(
                dataset_ids=(spec.dataset_id,),
                received_before_ms=150,
            )
    finally:
        conn.close()

    assert len(current) == 1
    assert float(current[0]["settlement_price"]) == 19.8
    assert current[0]["received_at_ms"] == 200
    assert len(historical) == 1
    assert float(historical[0]["settlement_price"]) == 19.2
    assert historical[0]["received_at_ms"] == 100


def test_empty_bounded_backfill_finishes_current_with_its_cursor(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        clock = _Clock()
        spec = require_dataset("fred.demcc")
        with repository_session_for_connection(conn) as repos, repos.transaction():
            target = repos.macro.enqueue_backfill_target(
                spec,
                start_date=date(1900, 1, 1),
                end_date=date(2026, 7, 27),
                now_ms=clock(),
                max_attempts=3,
                history_class="optional_maximum_public_history",
                priority=75,
            )
        service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_backfill",
            clock_kind="backfill",
            source_client=_EmptyCompletedBackfillClient(),
            clock_ms=clock,
        )

        result = service.run_once()
        stored = conn.execute(
            """
            SELECT status, priority, cursor_json
            FROM macro_acquisition_targets
            WHERE target_key = %s
            """,
            (target["target_key"],),
        ).fetchone()
        with repository_session_for_connection(conn) as repos, repos.transaction():
            promoted = repos.macro.promote_covering_backfill_target(
                spec,
                start_date=date(2021, 7, 27),
                end_date=date(2026, 7, 27),
                history_class="trailing_five_years",
                priority=25,
                now_ms=clock(),
            )
    finally:
        conn.close()

    assert result == {
        "dataset_id": "fred.demcc",
        "status": "current",
        "rows_seen": 0,
        "rows_inserted": 0,
    }
    assert stored["status"] == "current"
    assert stored["priority"] == 75
    assert stored["cursor_json"]["backfill_complete"] is True
    assert stored["cursor_json"]["history_class"] == "optional_maximum_public_history"

    assert promoted is not None
    assert promoted["target_key"] == target["target_key"]
    assert promoted["partition_key"] == "1900-01-01..2026-07-27"
    assert promoted["status"] == "current"
    assert promoted["priority"] == 25
    assert promoted["cursor_json"]["history_class"] == "trailing_five_years"


def test_promoting_covering_backfill_removes_redundant_unclaimed_targets(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        clock = _Clock()
        spec = require_dataset("fred.demcc")
        with repository_session_for_connection(conn) as repos, repos.transaction():
            covering = repos.macro.enqueue_backfill_target(
                spec,
                start_date=date(2021, 7, 23),
                end_date=date(2026, 7, 27),
                now_ms=clock(),
                max_attempts=3,
            )
            redundant = repos.macro.enqueue_backfill_target(
                spec,
                start_date=date(2021, 7, 27),
                end_date=date(2026, 7, 27),
                now_ms=clock(),
                max_attempts=3,
            )
            conn.execute(
                """
                UPDATE macro_acquisition_targets
                SET status = 'current',
                    cursor_json = cursor_json || '{"backfill_complete": true}'::jsonb,
                    next_due_at_ms = 253402300799000
                WHERE target_key = %s
                """,
                (covering["target_key"],),
            )
            promoted = repos.macro.promote_covering_backfill_target(
                spec,
                start_date=date(2021, 7, 27),
                end_date=date(2026, 7, 27),
                history_class="trailing_five_years",
                priority=25,
                now_ms=clock(),
            )
        targets = conn.execute(
            """
            SELECT target_key, status
            FROM macro_acquisition_targets
            WHERE dataset_id = %s
              AND clock_kind = 'backfill'
            ORDER BY target_key
            """,
            (spec.dataset_id,),
        ).fetchall()
    finally:
        conn.close()

    assert promoted is not None
    assert promoted["target_key"] == covering["target_key"]
    assert redundant["target_key"] != covering["target_key"]
    assert [dict(row) for row in targets] == [{"target_key": covering["target_key"], "status": "current"}]


def test_acquisition_stops_claiming_after_max_attempts(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        clock = _Clock()
        spec = require_dataset("fred.demcc")
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.macro.enqueue_backfill_target(
                spec,
                start_date=date(1900, 1, 1),
                end_date=date(2026, 7, 27),
                now_ms=clock(),
                max_attempts=2,
            )
        service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_backfill",
            clock_kind="backfill",
            source_client=_FailingClient(),
            clock_ms=clock,
        )

        first = service.run_once()
        clock.now = 1_000_000
        second = service.run_once()
        third = service.run_once()
        stored = conn.execute(
            """
            SELECT status, attempt_count, last_error_code
            FROM macro_acquisition_targets
            WHERE dataset_id = 'fred.demcc'
              AND clock_kind = 'backfill'
            """
        ).fetchone()
    finally:
        conn.close()

    assert first is not None and first["status"] == "failed"
    assert second is not None and second["status"] == "failed"
    assert third is None
    assert dict(stored) == {
        "status": "stale",
        "attempt_count": 2,
        "last_error_code": "RuntimeError",
    }


def test_steady_acquisition_keeps_retrying_transport_failures(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        clock = _Clock()
        spec = require_dataset("fred.dgs10")
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.macro.ensure_target(spec, now_ms=clock(), max_attempts=2)
        service = MacroAcquisitionService(
            db=_TestDb(conn),
            worker_name="macro_daily_settlement",
            clock_kind=spec.clock_kind,
            source_client=_FailingClient(),
            clock_ms=clock,
        )

        first = service.run_once()
        clock.now = 1_000_000
        second = service.run_once()
        clock.now = 2_000_000
        third = service.run_once()
        stored = conn.execute(
            """
            SELECT status, attempt_count, last_error_code
            FROM macro_acquisition_targets
            WHERE target_key = %s
            """,
            (spec.target_key,),
        ).fetchone()
    finally:
        conn.close()

    assert first is not None and first["status"] == "failed"
    assert second is not None and second["status"] == "failed"
    assert third is not None and third["status"] == "failed"
    assert dict(stored) == {
        "status": "delayed",
        "attempt_count": 3,
        "last_error_code": "RuntimeError",
    }


def test_ensure_target_revives_stale_steady_target(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        reset_postgres_schema(conn)
        spec = require_dataset("fred.dgs10")
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.macro.ensure_target(spec, now_ms=1_000, max_attempts=2)
            conn.execute(
                """
                UPDATE macro_acquisition_targets
                SET status = 'stale', attempt_count = 2, next_due_at_ms = 9_000
                WHERE target_key = %s
                """,
                (spec.target_key,),
            )
            repos.macro.ensure_target(spec, now_ms=2_000, max_attempts=2)
        stored = conn.execute(
            """
            SELECT status, attempt_count, next_due_at_ms
            FROM macro_acquisition_targets
            WHERE target_key = %s
            """,
            (spec.target_key,),
        ).fetchone()
    finally:
        conn.close()

    assert dict(stored) == {
        "status": "delayed",
        "attempt_count": 0,
        "next_due_at_ms": 2_000,
    }
