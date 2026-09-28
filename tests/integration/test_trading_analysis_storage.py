"""PostgreSQL proof for relay crash, same-asset work and stale model fencing."""

from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.support.trading_analysis import _selection
from tracefold.news.storage.root import NewsRepository
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_source_context_filters_before_limit_and_probes_only_matching_rows(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "source-search-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
            "AND indexname='trading_triggers_asset_visible_idx'"
        ).fetchone()
        assert index is not None
        assert "(asset_id, first_visible_at_ms DESC, trigger_id DESC)" in index["indexdef"]
        base = 100_000_000
        with conn.transaction():
            for index in range(10):
                trading.accept_trigger(
                    kind="oi",
                    source_fact_key=f"search-{index}",
                    source_revision="v1",
                    payload_sha256=f"{index + 1:064x}",
                    payload={
                        "kind": "oi",
                        "source_recorded_at_ms": base + index,
                        "provider_event_at_ms": base + index,
                        "oi_change_bps": 100,
                        "measurement_definition": "exchange-oi-v1",
                        "text": "special% catalyst" if index == 0 else "unrelated",
                        "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                    },
                    selection=_selection(),
                    now_ms=base + index,
                    root_ttl_ms=600_000,
                )
        recent = trading.recent_asset_source_context(
            asset_id="crypto:SOL",
            known_at_ms=base + 9,
            exclude_trigger_id="none",
        )
        assert len(recent) == 8 and "search-0" not in {item["source_fact_key"] for item in recent}
        matched = trading.recent_asset_source_context(
            asset_id="crypto:SOL",
            known_at_ms=base + 9,
            exclude_trigger_id="none",
            topic="special%",
            lookback_minutes=15,
            include_probe=True,
        )
        assert [item["source_fact_key"] for item in matched] == ["search-0"]
        assert (
            trading.recent_asset_source_context(
                asset_id="crypto:SOL",
                known_at_ms=base + 9,
                exclude_trigger_id="none",
                topic="symbol",
                lookback_minutes=15,
                include_probe=True,
            )
            == []
        )
        assert (
            trading.recent_asset_source_context(
                asset_id="crypto:SOL",
                known_at_ms=base - 1,
                exclude_trigger_id="none",
                topic="special%",
                lookback_minutes=15,
                include_probe=True,
            )
            == []
        )
        assert (
            trading.recent_asset_source_context(
                asset_id="crypto:SOL",
                known_at_ms=base + 9,
                exclude_trigger_id="none",
                topic="no-match",
                lookback_minutes=15,
                include_probe=True,
            )
            == []
        )
        with conn.transaction():
            claimed = trading.claim_analysis_case(now_ms=base + 100, lease_ms=2_000)
        assert claimed is not None
        with conn.transaction():
            assert trading.record_model_call_start(
                case_id=claimed["case_id"],
                claim_attempt=claimed["claim_attempt"],
                claim_token=claimed["claim_token"],
                call_index=0,
                request_ref="request-ref",
                now_ms=base + 101,
                timeout_ms=100,
                reserved_cost_microusd=5,
            )
            assert trading.record_model_call_finish(
                case_id=claimed["case_id"],
                claim_attempt=claimed["claim_attempt"],
                claim_token=claimed["claim_token"],
                call_index=0,
                response_ref=None,
                finished_at_ms=base + 102,
                status="not_dispatched",
                input_tokens=None,
                output_tokens=None,
                cost_microusd=None,
            )
        row = conn.execute(
            "SELECT status,cost_unknown_reason FROM trading_model_calls WHERE case_id=%s",
            (claimed["case_id"],),
        ).fetchone()
        assert row["status"] == row["cost_unknown_reason"] == "not_dispatched"
    finally:
        conn.close()


def test_directed_watch_ignores_reverse_cross_and_keeps_one_child(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "directed-watch-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="directed-watch",
                source_revision="v1",
                payload_sha256="d" * 64,
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
            claim = trading.claim_analysis_case(now_ms=1_021_000, lease_ms=300_000)
        assert claim is not None
        watch = {
            "kind": "closed_1m_directed_cross",
            "plan_id": "a" * 64,
            "side": "long",
            "level": "101",
            "previous_close": "100",
            "source_first_visible_at_ms": 1_000_000,
            "exit_plan": {"stop_distance_bps": 400, "take_profit_bps": 800, "max_holding_seconds": 14_400},
            "unit": "USDT/base_asset",
            "frozen_at_ms": 1_020_000,
            "expires_at_ms": 1_600_000,
        }
        with conn.transaction():
            assert trading.finish_analysis_case(
                case_id=case_id,
                claim_token=claim["claim_token"],
                now_ms=1_022_000,
                analysis_status="analyzed",
                evidence_ref="evidence-ref",
                decision={
                    "decision_version": "trade_decision_v4",
                    "action": "WATCH",
                    "side": "long",
                    "selected_plan_id": "a" * 64,
                    "reason": "wait",
                    "watch_condition": watch,
                },
            )
        with pytest.raises(ValueError, match="watch_observation_condition_unmet"), conn.transaction():
            trading.advance_watch_observation(
                parent_case_id=case_id,
                now_ms=1_081_000,
                observation_status="satisfied",
                observed_at_ms=1_080_000,
                observed_value="98",
                previous_close="100",
                trigger_side="short",
                observed_path=((1_080_000, "98"),),
                observation_ref="archive:reverse",
            )
        with conn.transaction():
            assert trading.advance_watch_observation(
                parent_case_id=case_id,
                now_ms=1_081_000,
                observation_status="not_met",
                observed_at_ms=1_080_000,
                observed_value="98",
                previous_close="100",
                observed_path=((1_080_000, "98"),),
                observation_ref="archive:first",
            )
            assert trading.advance_watch_observation(
                parent_case_id=case_id,
                now_ms=1_141_000,
                observation_status="satisfied",
                observed_at_ms=1_140_000,
                observed_value="102",
                previous_close="98",
                trigger_side="long",
                observed_path=((1_140_000, "102"),),
                observation_ref="archive:hit",
            )
        row = conn.execute(
            "SELECT w.status,w.child_case_id,c.manifest FROM trading_watch_observations w "
            "JOIN trading_cases c ON c.case_id=w.child_case_id WHERE w.parent_case_id=%s",
            (case_id,),
        ).fetchone()
        assert row["status"] == "triggered"
        assert row["manifest"]["watch_condition"]["plan_id"] == "a" * 64
        assert row["manifest"]["watch_trigger_side"] == "long"
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
                decision={
                    "decision_version": "trade_decision_v4",
                    "action": "NO_TRADE",
                    "selected_plan_id": None,
                    "side": None,
                    "reason": "fixture",
                },
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
        with conn.transaction():
            assert trading.record_analysis_snapshot(
                case_id=case_id,
                claim_attempt=1,
                claim_token=first["claim_token"],
                evidence_ref="frozen-evidence",
                brief_ref="frozen-brief",
                now_ms=1_550,
            )
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
        assert trading.prior_analysis_snapshot(case_id=case_id, before_claim_attempt=2) == {
            "evidence_ref": "frozen-evidence",
            "brief_ref": "frozen-brief",
        }
        with conn.transaction():
            assert not trading.record_analysis_snapshot(
                case_id=case_id,
                claim_attempt=1,
                claim_token=first["claim_token"],
                evidence_ref="late-evidence",
                brief_ref="late-brief",
                now_ms=3_600,
            )
        assert (
            conn.execute(
                "SELECT status FROM trading_model_calls WHERE case_id=%s AND claim_attempt=1",
                (case_id,),
            ).fetchone()["status"]
            == "result_unknown"
        )
        decision = {
            "decision_version": "trade_decision_v4",
            "action": "NO_TRADE",
            "selected_plan_id": None,
            "side": None,
            "reason": "No durable directional edge.",
        }

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
                        "status": "completed",
                        "finished_at_ms": 3_650,
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
        assert trading.prior_analysis_snapshot(case_id=case_id, before_claim_attempt=2) == {
            "evidence_ref": "frozen-evidence",
            "brief_ref": "frozen-brief",
        }
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
                decision={
                    "decision_version": "trade_decision_v4",
                    "action": "TRADE",
                    "selected_plan_id": "a" * 64,
                    "side": "long",
                    "reason": "test",
                },
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
                "kind": "closed_1m_directed_cross",
                "plan_id": "a" * 64,
                "side": "long",
                "level": "101",
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
                    "decision_version": "trade_decision_v4",
                    "action": "WATCH",
                    "selected_plan_id": "a" * 64,
                    "side": "long",
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
