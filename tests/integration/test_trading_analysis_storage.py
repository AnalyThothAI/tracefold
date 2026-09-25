"""PostgreSQL proof for relay crash, same-asset work and stale model fencing."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from scripts.export_trading_analysis_cohort import export_cases
from scripts.relabel_trading_price_paths import (
    _audit_v1_path,
    _pending_correction_count,
    _pending_corrections,
)
from scripts.trading_analysis_cohort import evaluate as evaluate_cohort
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.market_identity import DEFAULT_UNIVERSE, AssetId, AssetRegistry, InstrumentRef
from tracefold.trading.engine.target import SourceAsset, TargetSelection, select_target
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
            conn.execute(
                "INSERT INTO trading_case_decisions "
                "(case_id,decision_id,policy_id,policy_version,input_ref,action,decision,"
                "publish_status,decided_at_ms,valid_until_ms) "
                "VALUES (%s,'shadow-quote-decision','fixture','v4','fixture','TRADE',"
                '\'{"decision_version":"trade_decision_v4"}\'::jsonb,'
                "'disabled',1500,300000)",
                (case_id,),
            )
            conn.execute(
                "INSERT INTO trading_case_attempts "
                "(case_id,claim_attempt,claim_token,analysis_status,evidence_ref) "
                "VALUES (%s,1,'first','analyzed','first-evidence'),"
                "(%s,2,'second','analyzed','second-evidence')",
                (case_id, case_id),
            )
            conn.execute(
                "INSERT INTO trading_model_calls "
                "(case_id,claim_attempt,call_index,status,request_ref,cost_unknown_reason) "
                "VALUES (%s,1,0,'result_unknown','request-ref','response_unrecoverable')",
                (case_id,),
            )
            conn.execute(
                "UPDATE trading_case_attempts SET model_name='fixture-model',prompt_sha='fixture-prompt' "
                "WHERE case_id=%s AND claim_attempt=1",
                (case_id,),
            )
        due_evaluation = trading.due_shadow_evaluations(now_ms=182_000)
        assert len(due_evaluation) == 1
        assert due_evaluation[0]["evidence_ref"] == "first-evidence"
        root_due = trading.due_root_research_tapes(now_ms=1_100)
        assert len(root_due) == 1 and root_due[0]["case_id"] == case_id
        with conn.transaction():
            assert trading.record_root_research_sample(
                case_id=case_id, prior_ref=None, tape_ref="root-tape-1", sampled_at_ms=1_100
            )
            assert not trading.record_root_research_sample(
                case_id=case_id, prior_ref=None, tape_ref="stale-root", sampled_at_ms=1_100
            )
        due_evaluation = trading.due_shadow_evaluations(now_ms=182_000)
        assert due_evaluation[0]["root_market_tape_ref"] == "root-tape-1"
        assert due_evaluation[0]["root_case_id"] == case_id
        assert due_evaluation[0]["root_accepted_at_ms"] == 1_100
        assert due_evaluation[0]["root_expires_at_ms"] == root_due[0]["root_expires_at_ms"]
        assert trading.due_root_research_tapes(now_ms=61_099) == []
        assert trading.due_root_research_tapes(now_ms=61_100)[0]["tape_ref"] == "root-tape-1"
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
        export, manifest = export_cases(conn, AnalysisFiles(tmp_path / "missing-archive"), start_ms=1_100, end_ms=1_101)
        assert len(export) == 1 and export[0]["root_trigger_id"]
        assert export[0]["decision_policy_version"] == "v4"
        assert export[0]["attempts"][0]["evidence_ref"] == "first-evidence"
        assert export[0]["attempts"][0]["calls"][0]["request_ref"] == "request-ref"
        assert export[0]["attempts"][0]["physical_call_count"] == 0
        assert export[0]["rule_watch_status"] == "missing"
        assert export[0]["arm_evaluations"]["dspy"] == {
            "status": "pending",
            "source": "shadow_simulation",
            "reason": "receipt_pending",
            "due_at_ms": 182_000,
        }
        assert {item["kind"] for item in manifest["missing_archive_items"]} == {
            "evidence",
            "model_request",
            "root_market_tape",
            "dspy_decision_quote_ref",
            "dspy_planned_quote_ref",
            "dspy_quote_tape_ref",
        }
        report = evaluate_cohort(export, expected_roots=1, cutoff_ms=1_100, invalid_outputs=[], expected_invalid=0)
        assert report["arms"]["holdout"]["dspy"]["net_unknown"] == 1
        assert report["arms"]["holdout"]["dspy"]["model_cost_unknown_calls"] == 1
        assert report["model_identities"] == [{"model_name": "fixture-model", "prompt_sha": "fixture-prompt"}]
        with conn.transaction():
            conn.execute(
                "UPDATE trading_case_evaluations SET status='simulated',result=%s::jsonb WHERE case_id=%s",
                (
                    json.dumps(
                        {
                            "status": "simulated",
                            "source": "shadow_simulation",
                            "decision_quote_ref": "decision-ref",
                            "entry_quote_ref": "planned-ref",
                            "exit_quote_ref": "exit-ref",
                            "quote_tape_ref": "tape-1",
                            "instrument_rules_ref": "first-evidence",
                            "mark_path_ref": "root-tape-1",
                            "funding_ref": "funding-ref",
                            "fee_ref": "fee-ref",
                        }
                    ),
                    case_id,
                ),
            )
        settled, settled_manifest = export_cases(
            conn, AnalysisFiles(tmp_path / "missing-archive"), start_ms=1_100, end_ms=1_101
        )
        assert settled[0]["arm_evaluations"]["dspy"]["status"] == "unevaluable"
        assert settled[0]["arm_evaluations"]["dspy"]["reason"] == "receipt_archive_incomplete"
        assert "dspy_fee_ref" in {item["kind"] for item in settled_manifest["missing_archive_items"]}
        with conn.transaction():
            _, excluded_case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="ineligible",
                source_revision="v1",
                payload_sha256="a" * 64,
                payload={"kind": "oi", "assets": []},
                selection=TargetSelection("no_eligible_primary", None, None, (), "test-catalogue"),
                now_ms=3_000,
                root_ttl_ms=10_000,
            )
        excluded, excluded_manifest = export_cases(
            conn, AnalysisFiles(tmp_path / "missing-archive"), start_ms=3_000, end_ms=3_001
        )
        assert excluded[0]["case_id"] == excluded_case_id
        assert excluded[0]["rule_watch_status"] == "not_applicable"
        assert excluded_manifest["missing_archive_items"] == []
        excluded_report = evaluate_cohort(
            excluded, expected_roots=1, cutoff_ms=3_000, invalid_outputs=[], expected_invalid=0
        )
        assert excluded_report["arms"]["holdout"]["rule"]["ending_equity_usdt"] == "1000"
        assert excluded_report["arms"]["holdout"]["dspy"]["ending_equity_usdt"] == "1000"
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
        anchor = conn.execute(
            "SELECT source_observed_at_ms FROM trading_cases WHERE case_id=%s", (case_id,)
        ).fetchone()[0]
        start_at = ((anchor + 59_999) // 60_000) * 60_000
        end_at = ((anchor + 900_000 + 59_999) // 60_000) * 60_000
        archive = AnalysisFiles(tmp_path / "archive")
        legacy_ref = archive.write(
            {
                "case_id": case_id,
                "axis": "source",
                "horizon_seconds": 900,
                "version": "price_path_v1",
                "label_version": "price_path_v1",
                "status": "ok",
                "axis_anchor_ms": anchor,
                "target_ms": anchor + 900_000,
                "start_close_at_ms": start_at,
                "end_close_at_ms": end_at,
                "start_price": "100",
                "end_price": "101",
                "return_bps": "100",
                "measure": "underlying_close_to_close_gross",
                "execution_claim": False,
                "costs_included": False,
                "source_identity": "binance_public_v1",
                "source_version": "binance_public_v1",
                "unit_definition": "native_quote_v1",
                "market_status": "ok",
                "received_at_ms": end_at + 1_000,
                "request_receipts": [{"endpoint": "/fapi/v1/klines", "native_symbol": "SOLUSDT"}],
            }
        )
        with conn.transaction():
            conn.execute(
                "DELETE FROM trading_case_outcomes WHERE case_id=%s AND axis='source' AND horizon_seconds=900",
                (case_id,),
            )
            conn.execute(
                "INSERT INTO trading_case_outcomes "
                "(case_id,axis,horizon_seconds,label_version,status,return_bps,available_at_ms,path_ref) "
                "VALUES (%s,'source',900,'price_path_v1','ok',100,901000,%s)",
                (case_id, legacy_ref),
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
        assert rows[0]["path_ref"] == legacy_ref
        assert rows[1]["status"] == "pending" and rows[1]["return_bps"] is None
        assert rows[1]["path_ref"] is None
        pending = _pending_corrections(repositories_for_connection(conn), limit=10)
        assert len(pending) == 1 and pending[0]["case_id"] == case_id
        conn.row_factory = dict_row
        assert _pending_correction_count(repositories_for_connection(conn)) == 1
        audit = _audit_v1_path(pending[0], archive.read(legacy_ref))
        assert audit["status"] == "ok" and Decimal(audit["return_bps"]) == Decimal("100")
        correction_ref = archive.write(audit)
        with conn.transaction():
            assert trading.settle_analysis_outcome(
                case_id=case_id,
                axis="source",
                horizon_seconds=900,
                label_version="price_path_v2",
                status="ok",
                return_bps=audit["return_bps"],
                path_ref=correction_ref,
                now_ms=901_001,
            )
        settled = conn.execute(
            "SELECT label_version,status,return_bps,path_ref FROM trading_case_outcomes "
            "WHERE case_id=%s AND axis='source' AND horizon_seconds=900 ORDER BY label_version",
            (case_id,),
        ).fetchall()
        assert settled[0]["status"] == "ok" and settled[0]["path_ref"] == legacy_ref
        assert settled[1]["status"] == "ok" and settled[1]["return_bps"] == 100
        assert _pending_correction_count(repositories_for_connection(conn)) == 0
        assert archive.read(settled[1]["path_ref"])["historical_quality"] == "verified_endpoint_only"
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
        with conn.transaction():
            assert trading.record_root_research_sample(
                case_id=case_id, prior_ref=None, tape_ref="root-watch-tape", sampled_at_ms=1_141_000
            )
            conn.execute(
                "INSERT INTO trading_case_decisions "
                "(case_id,decision_id,policy_id,policy_version,input_ref,action,decision,"
                "publish_status,decided_at_ms,valid_until_ms) "
                "VALUES (%s,'child-shadow-decision','fixture','v4','fixture','TRADE',"
                '\'{"decision_version":"trade_decision_v4"}\'::jsonb,'
                "'shadow',1141000,1260000)",
                (row["child_case_id"],),
            )
            trading.record_shadow_evaluation(
                case_id=row["child_case_id"],
                decision_at_ms=1_141_000,
                scheduled_at_ms=1_142_000,
                due_at_ms=1_143_000,
                decision_quote_ref="decision-ref",
                planned_quote_ref="planned-ref",
                initial_result=None,
            )
        due = trading.due_shadow_evaluations(now_ms=1_143_000)
        assert len(due) == 1 and due[0]["case_id"] == row["child_case_id"]
        assert due[0]["root_case_id"] == case_id
        assert due[0]["root_market_tape_ref"] == "root-watch-tape"
    finally:
        conn.close()
