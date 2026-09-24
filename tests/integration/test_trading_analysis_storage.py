"""PostgreSQL proof for relay crash, same-asset work and stale model fencing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.market_identity import DEFAULT_UNIVERSE, AssetId, AssetRegistry, InstrumentRef
from tracefold.trading.engine.target import SourceAsset, select_target
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]


def _selection(symbol: str = "SOL"):
    registry = AssetRegistry(
        snapshot_ref="test-catalogue",
        instruments=(
            InstrumentRef(
                venue="binance.usdm",
                environment="demo",
                product="perpetual",
                native_symbol=f"{symbol}USDT",
                asset_id=AssetId("crypto", symbol),
                quote_asset="USDT",
                settlement_asset="USDT",
                units_per_contract=Decimal(1),
                price_unit=f"USDT/{symbol}",
                quantity_unit=symbol,
            ),
        ),
    )
    return select_target(
        kind="oi",
        assets=(SourceAsset(symbol, "crypto", "primary"),),
        registry=registry,
        universe=DEFAULT_UNIVERSE,
        execution_environment="demo",
    )


def test_shadow_quote_tape_storage_is_due_and_compare_and_swap_fenced(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "shadow-quote-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="shadow-quote",
                source_revision="v1",
                payload_sha256="f" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": 1_000,
                    "provider_event_at_ms": 900,
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
            trading.record_shadow_evaluation(
                case_id=case_id,
                decision_at_ms=1_500,
                scheduled_at_ms=2_000,
                due_at_ms=182_000,
                decision_quote_ref="decision-ref",
                planned_quote_ref="planned-ref",
                initial_result=None,
            )
        assert trading.due_shadow_quote_samples(now_ms=61_999) == []
        due = trading.due_shadow_quote_samples(now_ms=62_000)
        assert len(due) == 1 and due[0]["case_id"] == case_id
        with conn.transaction():
            assert trading.record_shadow_quote_sample(
                case_id=case_id, prior_ref=None, tape_ref="tape-1", sampled_at_ms=62_000
            )
            assert not trading.record_shadow_quote_sample(
                case_id=case_id, prior_ref=None, tape_ref="stale", sampled_at_ms=62_000
            )
        assert trading.due_shadow_quote_samples(now_ms=121_999) == []
        assert trading.due_shadow_quote_samples(now_ms=122_000)[0]["quote_tape_ref"] == "tape-1"
    finally:
        conn.close()


def test_price_path_v2_correction_appends_without_overwriting_v1(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "path-correction-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="historical-path",
                source_revision="v1",
                payload_sha256="e" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": 1_000,
                    "provider_event_at_ms": 900,
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
            conn.execute(
                "DELETE FROM trading_case_outcomes WHERE case_id=%s AND axis='source' AND horizon_seconds=900",
                (case_id,),
            )
            conn.execute(
                "INSERT INTO trading_case_outcomes "
                "(case_id,axis,horizon_seconds,label_version,status,return_bps,available_at_ms,path_ref) "
                "VALUES (%s,'source',900,'price_path_v1','ok',100,901000,'legacy-ref')",
                (case_id,),
            )
            assert trading.queue_price_path_v2_corrections() == 1
            assert trading.queue_price_path_v2_corrections() == 0
        rows = conn.execute(
            "SELECT label_version,status,return_bps,path_ref FROM trading_case_outcomes "
            "WHERE case_id=%s AND axis='source' AND horizon_seconds=900 ORDER BY label_version",
            (case_id,),
        ).fetchall()
        assert [row["label_version"] for row in rows] == ["price_path_v1", "price_path_v2"]
        assert rows[0]["status"] == "ok" and rows[0]["return_bps"] == 100
        assert rows[0]["path_ref"] == "legacy-ref"
        assert rows[1]["status"] == "pending" and rows[1]["return_bps"] is None
        assert rows[1]["path_ref"] is None
    finally:
        conn.close()


def test_claim_serializes_one_asset_without_blocking_another(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "claim-fairness-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        for index, symbol in enumerate(("SOL", "SOL", "ADA")):
            at_ms = 1_100 + index * 100
            with conn.transaction():
                trading.accept_trigger(
                    kind="oi",
                    source_fact_key=f"fair-{index}",
                    source_revision="v1",
                    payload_sha256=f"{index + 1:064x}",
                    payload={
                        "kind": "oi",
                        "source_recorded_at_ms": 1_000,
                        "provider_event_at_ms": 900,
                        "assets": [{"symbol": symbol, "market_type": "crypto", "role": "primary"}],
                    },
                    selection=_selection(symbol),
                    now_ms=at_ms,
                    root_ttl_ms=10_000,
                )
        with conn.transaction():
            first = trading.claim_analysis_case(now_ms=1_500, lease_ms=2_000)
        with conn.transaction():
            second = trading.claim_analysis_case(now_ms=1_600, lease_ms=2_000)
        assert first is not None and second is not None
        assert (first["target_asset_id"], second["target_asset_id"]) == ("crypto:SOL", "crypto:ADA")
        with conn.transaction():
            assert trading.claim_analysis_case(now_ms=1_700, lease_ms=2_000) is None
            assert trading.finish_analysis_case(
                case_id=first["case_id"],
                claim_token=first["claim_token"],
                now_ms=1_800,
                analysis_status="analyzed",
                evidence_ref="fixture",
                decision={"action": "NO_TRADE", "side": None, "reason": "fixture"},
            )
        with conn.transaction():
            next_sol = trading.claim_analysis_case(now_ms=1_900, lease_ms=2_000)
        assert next_sol is not None and next_sol["target_asset_id"] == "crypto:SOL"
        assert next_sol["case_id"] != first["case_id"]
    finally:
        conn.close()


def test_relay_retries_reuse_case_and_old_claim_cannot_finish(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "analysis-db", read_only=False)
    try:
        migrate(conn)
        news = NewsRepository(conn)
        trading = TradingRepository(conn)
        payload = {
            "kind": "oi",
            "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
            "source_recorded_at_ms": 1_000,
            "provider_event_at_ms": 900,
            "oi_change_bps": 500,
            "oi_value_usd": 1_000_000,
        }
        with conn.transaction():
            assert news.enqueue_trade_event(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="metric-v1",
                payload=payload,
                source_recorded_at_ms=1_000,
            )
        event = news.unacknowledged_trade_events(limit=10)[0]
        with conn.transaction():
            trigger_id, case_id, result = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="metric-v1",
                payload_sha256=event["payload_sha256"],
                payload=event["payload"],
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
        assert result == "accepted"
        # The process died after Trading committed and before News acknowledgement.
        with conn.transaction():
            repeated = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="metric-v1",
                payload_sha256=event["payload_sha256"],
                payload=event["payload"],
                selection=_selection(),
                now_ms=1_200,
                root_ttl_ms=10_000,
            )
        assert repeated == (trigger_id, case_id, "duplicate")
        count = conn.execute(
            "SELECT count(*) AS n FROM trading_cases WHERE trigger_id=%s",
            (trigger_id,),
        ).fetchone()
        assert count["n"] == 1
        with conn.transaction():
            assert news.acknowledge_trade_event(
                event_id=event["event_id"], payload_sha256=event["payload_sha256"], now_ms=1_300
            )
        assert news.unacknowledged_trade_events(limit=10) == []

        with conn.transaction():
            _, next_case_id, next_result = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-2",
                source_revision="metric-v1",
                payload_sha256="d" * 64,
                payload=payload,
                selection=_selection(),
                now_ms=1_400,
                root_ttl_ms=10_000,
            )
        assert next_result == "accepted"
        next_trigger_id = conn.execute(
            "SELECT trigger_id FROM trading_cases WHERE case_id=%s",
            (next_case_id,),
        ).fetchone()["trigger_id"]
        history = trading.recent_asset_source_context(
            asset_id="crypto:SOL",
            known_at_ms=1_400,
            exclude_trigger_id=next_trigger_id,
        )
        assert [item["source_fact_key"] for item in history] == ["frame-1"]
        assert (
            trading.recent_asset_source_context(
                asset_id="crypto:SOL",
                known_at_ms=1_099,
                exclude_trigger_id="not-a-trigger",
            )
            == []
        )

        with conn.transaction():
            first = trading.claim_analysis_case(now_ms=1_500, lease_ms=2_000)
        assert first is not None and first["case_id"] == case_id
        started = conn.execute(
            "SELECT analysis_status,started_at_ms FROM trading_case_attempts WHERE case_id=%s AND claim_attempt=1",
            (case_id,),
        ).fetchone()
        assert started == {"analysis_status": "running", "started_at_ms": 1_500}
        with conn.transaction():
            assert trading.record_model_call_start(
                case_id=case_id,
                claim_attempt=1,
                claim_token=first["claim_token"],
                call_index=0,
                request_ref="request-ref",
                now_ms=1_600,
                timeout_ms=5_000,
                reserved_cost_microusd=100,
            )
        requested = conn.execute(
            "SELECT status,timeout_ms,remaining_deadline_ms,request_ref,response_ref "
            "FROM trading_model_calls WHERE case_id=%s AND claim_attempt=1",
            (case_id,),
        ).fetchone()
        assert requested == {
            "status": "requested",
            "timeout_ms": 1_900,
            "remaining_deadline_ms": 1_900,
            "request_ref": "request-ref",
            "response_ref": None,
        }
        with conn.transaction():
            reclaimed = trading.claim_analysis_case(now_ms=3_600, lease_ms=2_000)
        assert reclaimed is not None and reclaimed["claim_token"] != first["claim_token"]
        assert (
            conn.execute(
                "SELECT status FROM trading_model_calls WHERE case_id=%s AND claim_attempt=1",
                (case_id,),
            ).fetchone()["status"]
            == "result_unknown"
        )
        decision = {"action": "NO_TRADE", "side": None, "reason": "No durable directional edge."}

        def record_attempt(claim: dict[str, object]) -> None:
            trading.record_analysis_attempt(
                case_id=case_id,
                claim_attempt=int(claim["claim_attempt"]),
                claim_token=str(claim["claim_token"]),
                brief_ref="brief-ref",
                evidence_ref="evidence-ref",
                assessment_ref="assessment-ref",
                model_name="fixture",
                prompt_sha="a" * 64,
                started_at_ms=1_600,
                ended_at_ms=3_700,
                provider_status="invalid_output",
                analysis_status="model_schema_invalid",
                error_code="model_schema_invalid",
                validation_errors=({"field": "assessment.action", "type": "literal_error"},),
                input_tokens=100,
                output_tokens=50,
                cost_microusd=None,
                calls=(
                    {
                        "request_ref": "request-ref",
                        "response_ref": "response-ref",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cost_microusd": None,
                        "cost_unknown_reason": "provider_cost_unavailable",
                    },
                ),
            )

        with conn.transaction():
            record_attempt(first)
            record_attempt(first)  # Same physical claim is indexed once.
            record_attempt(reclaimed)
            assert not trading.finish_analysis_case(
                case_id=case_id,
                claim_token=first["claim_token"],
                now_ms=3_700,
                analysis_status="analyzed",
                evidence_ref="evidence-ref",
                decision=decision,
            )
            assert trading.finish_analysis_case(
                case_id=case_id,
                claim_token=reclaimed["claim_token"],
                now_ms=3_700,
                analysis_status="analyzed",
                evidence_ref="evidence-ref",
                decision=decision,
            )
        row = conn.execute("SELECT state, analysis_status FROM trading_cases WHERE case_id=%s", (case_id,)).fetchone()
        assert row == {"state": "DONE", "analysis_status": "analyzed"}
        attempts = conn.execute(
            "SELECT claim_attempt,settled,cost_microusd,cost_unknown_reason "
            "FROM trading_case_attempts WHERE case_id=%s ORDER BY claim_attempt",
            (case_id,),
        ).fetchall()
        assert len(attempts) == 2
        assert [item["settled"] for item in attempts] == [False, True]
        assert all(item["cost_microusd"] is None for item in attempts)
        assert all(item["cost_unknown_reason"] == "one_or_more_physical_costs_unavailable" for item in attempts)
        call_count = conn.execute(
            "SELECT count(*) AS n FROM trading_model_calls WHERE case_id=%s",
            (case_id,),
        ).fetchone()["n"]
        assert call_count == 2
    finally:
        conn.close()


def test_valid_trade_analysis_records_publication_refusal(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "publication-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        with conn.transaction():
            _, case_id, result = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-blocked",
                source_revision="metric-v1",
                payload_sha256="c" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": 1_000,
                    "provider_event_at_ms": 900,
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
        assert result == "accepted"
        with conn.transaction():
            claim = trading.claim_analysis_case(now_ms=1_500, lease_ms=2_000)
            assert claim is not None
            assert trading.finish_analysis_case(
                case_id=case_id,
                claim_token=claim["claim_token"],
                now_ms=1_600,
                analysis_status="analyzed",
                evidence_ref="evidence-ref",
                decision={"action": "TRADE", "side": "long", "reason": "test"},
                publish_block_reason="analysis_signal_expired",
            )
        row = conn.execute(
            "SELECT c.state,c.analysis_status,d.publish_status,d.publish_reason "
            "FROM trading_cases c JOIN trading_case_decisions d USING(case_id) "
            "WHERE c.case_id=%s",
            (case_id,),
        ).fetchone()
        assert row == {
            "state": "DONE",
            "analysis_status": "analyzed",
            "publish_status": "blocked",
            "publish_reason": "analysis_signal_expired",
        }
        assert conn.execute("SELECT count(*) AS n FROM trading_trade_signals").fetchone()["n"] == 0
    finally:
        conn.close()


def test_watch_condition_creates_one_child_only_after_adjacent_closed_cross(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "watch-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="watch-source",
                source_revision="v1",
                payload_sha256="e" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": 1_000_000,
                    "provider_event_at_ms": 999_000,
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=1_000_000,
                root_ttl_ms=600_000,
            )
        with conn.transaction():
            claim = trading.claim_analysis_case(now_ms=1_021_000, lease_ms=300_000)
            assert claim is not None
            watch = {
                "kind": "closed_1m_range_cross",
                "upper_level": "101",
                "lower_level": "99",
                "previous_close": "100",
                "source_first_visible_at_ms": 1_000_000,
                "exit_plan": {"stop_distance_bps": 400, "take_profit_bps": 800, "max_holding_seconds": 14_400},
                "unit": "USDT/base_asset",
                "frozen_at_ms": 1_020_000,
                "expires_at_ms": 1_600_000,
            }
            assert trading.finish_analysis_case(
                case_id=case_id,
                claim_token=claim["claim_token"],
                now_ms=1_022_000,
                analysis_status="analyzed",
                evidence_ref="evidence-ref",
                decision={
                    "action": "WATCH",
                    "side": None,
                    "reason_code": "model_watch",
                    "reason": "wait",
                    "watch_condition": watch,
                },
            )
        assert conn.execute("SELECT count(*) AS n FROM trading_cases").fetchone()["n"] == 1
        with pytest.raises(ValueError, match="watch_observation_condition_unmet"), conn.transaction():
            trading.advance_watch_observation(
                parent_case_id=case_id,
                now_ms=1_081_000,
                observation_status="satisfied",
                observed_at_ms=1_080_000,
                observed_value="100",
                previous_close="100",
                trigger_side="long",
                observed_path=((1_080_000, "100"),),
                observation_ref="archive:invalid",
            )
        with conn.transaction():
            assert trading.advance_watch_observation(
                parent_case_id=case_id,
                now_ms=1_081_000,
                observation_status="not_met",
                observed_at_ms=1_080_000,
                observed_value="100",
                previous_close="100",
                observed_path=((1_080_000, "100"),),
                observation_ref="archive:first-bar",
            )
        assert conn.execute("SELECT count(*) AS n FROM trading_cases").fetchone()["n"] == 1
        with conn.transaction():
            assert trading.advance_watch_observation(
                parent_case_id=case_id,
                now_ms=1_141_000,
                observation_status="satisfied",
                observed_at_ms=1_140_000,
                observed_value="102",
                previous_close="100",
                trigger_side="long",
                observed_path=((1_140_000, "102"),),
                observation_ref="archive:watch-hit",
            )
            assert not trading.advance_watch_observation(
                parent_case_id=case_id,
                now_ms=1_141_001,
                observation_status="satisfied",
                observed_at_ms=1_140_000,
                observed_value="102",
                previous_close="100",
                trigger_side="long",
                observed_path=((1_140_000, "102"),),
                observation_ref="archive:watch-hit",
            )
        row = conn.execute(
            "SELECT w.status,w.child_case_id,w.last_observation_ref,c.manifest,"
            "c.run_kind,c.recheck_seq,c.work_deadline_at_ms "
            "FROM trading_watch_observations w JOIN trading_cases c ON c.case_id=w.child_case_id "
            "WHERE w.parent_case_id=%s",
            (case_id,),
        ).fetchone()
        assert row["status"] == "triggered"
        assert row["last_observation_ref"] == "archive:watch-hit"
        assert row["manifest"]["watch_observation_ref"] == "archive:watch-hit"
        assert row["manifest"]["watch_trigger_side"] == "long"
        assert row["run_kind"] == "conditional" and row["recheck_seq"] == 1
        assert row["work_deadline_at_ms"] == 1_260_000
        assert conn.execute("SELECT count(*) AS n FROM trading_cases").fetchone()["n"] == 2
    finally:
        conn.close()
