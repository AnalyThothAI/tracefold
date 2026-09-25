"""One claimed Case freezes provider data, records an assessment and settles independently."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from dspy.lm15 import Message, Response, Usage

from tests.integration.test_trading_analysis_storage import _selection
from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    reset_postgres_schema,
)
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.llm import ConfiguredLMEndpoint
from tracefold.app.trading_analysis import AnalysisRunner, FrameReader
from tracefold.app.trading_analyst import AnalystCallReceipt, TradeAnalyst
from tracefold.news.program.lm import ScriptedLM
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.config.models import PostgresConfig, Settings
from tracefold.trading.engine.marketdata import MarketDataRequest, MarketDataResult
from tracefold.trading.engine.plans import AnalysisProposal
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]


class _Market:
    async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
        now_ms = int(time.time() * 1000)
        if request.dataset.endswith("bars"):
            assert request.start_ms is not None and request.end_ms is not None
            payload = tuple(
                {
                    "event_at_ms": stamp + 60_000,
                    "open_at_ms": stamp,
                    "received_at_ms": now_ms,
                    "close": "100",
                    "high": "101",
                    "low": "99",
                    "quote_volume": "1000",
                    "taker_buy_quote_volume": "500",
                }
                for stamp in range(request.start_ms, request.end_ms, 60_000)
            )
        elif request.dataset == "open_interest":
            payload = ({"event_at_ms": now_ms, "received_at_ms": now_ms, "open_interest_quantity": "10000"},)
        else:
            payload = (
                {
                    "event_at_ms": now_ms,
                    "received_at_ms": now_ms,
                    "mark_price": "100",
                    "index_price": "100",
                    "last_funding_rate": "0.0001",
                },
            )
        return MarketDataResult(
            status="ok",
            payload=payload,
            schema_version="fixture_v1",
            source_version="fixture_v1",
            unit_definition=request.unit_definition,
            source_identity=request.source_identity,
            event_start_ms=payload[0]["event_at_ms"],
            event_end_ms=payload[-1]["event_at_ms"],
            received_at_ms=now_ms,
            missing_reasons=(),
            request_receipts=(),
        )


class _Analyst:
    async def assess(self, brief):
        answer = AnalysisProposal(
            action="NO_TRADE",
            public_rationale="The frozen evidence is insufficient to trade.",
            supporting_evidence=("market:perp_bars",),
        )
        return AnalystCallReceipt(
            brief_sha=brief.sha,
            menu_sha=brief.plan_menu_sha,
            prompt_sha="a" * 64,
            model="fixture",
            started_at_ms=1,
            ended_at_ms=2,
            status="provider_success",
            input_tokens=100,
            output_tokens=50,
            cost_microusd=None,
            assessment=answer,
            error_code=None,
            request_payload={"brief_sha": brief.sha},
            response_payload=answer.model_dump(mode="json"),
        )


def test_oi_case_does_not_require_optional_current_market_oi(tmp_path) -> None:
    class MissingOi(_Market):
        async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
            if request.dataset == "open_interest":
                return MarketDataResult(
                    status="missing",
                    payload=(),
                    schema_version="fixture_v1",
                    source_version="fixture_v1",
                    unit_definition=request.unit_definition,
                    source_identity=request.source_identity,
                    event_start_ms=None,
                    event_end_ms=None,
                    received_at_ms=None,
                    missing_reasons=("fixture_missing",),
                    request_receipts=(),
                )
            return await super().fetch(request)

    reader = FrameReader(MissingOi(), AnalysisFiles(tmp_path / "oi-evidence"))
    case = {
        "case_id": "missing-oi",
        "trigger_id": "missing-oi-trigger",
        "root_expires_at_ms": int(time.time() * 1000) + 600_000,
        "target_selection": {
            "reason": "selected",
            "asset_id": "crypto:SOL",
            "instrument": {"native_symbol": "SOLUSDT", "environment": "demo", "mapping_semantics_digest": "a" * 64},
        },
    }
    source = {"kind": "oi", "oi_change_bps": 100, "measurement_definition": "exchange-oi-v1"}
    prepared = asyncio.run(
        reader.prepare(case=case, source_fact=source, source_first_visible_at_ms=int(time.time() * 1000) - 30_000)
    )
    assert len(prepared.plans) >= 2


def test_catalyst_source_headline_is_citable_only_when_present(tmp_path) -> None:
    now_ms = int(time.time() * 1000)
    reader = FrameReader(_Market(), AnalysisFiles(tmp_path / "catalyst-evidence"))
    case = {
        "case_id": "catalyst-source",
        "trigger_id": "catalyst-source-trigger",
        "root_expires_at_ms": now_ms + 600_000,
        "created_at_ms": now_ms - 500,
        "target_selection": {
            "reason": "selected",
            "asset_id": "crypto:SOL",
            "instrument": {"native_symbol": "SOLUSDT", "environment": "live", "mapping_semantics_digest": "a" * 64},
        },
    }
    source = {
        "kind": "catalyst",
        "headline": "A visible source headline",
        "why": "A source explanation",
        "source_recorded_at_ms": now_ms - 20_000,
    }
    prepared = asyncio.run(reader.prepare(case=case, source_fact=source, source_first_visible_at_ms=now_ms - 10_000))
    item = prepared.brief.evidence_catalog["source"]
    assert item["status"] == "ok"
    assert item["values"] == {"headline": source["headline"], "why": source["why"]}
    assert item["unit_definition"] == {"headline": "text", "why": "text"}
    assert "source" in json.loads(prepared.brief.text)["citable_evidence_ids"]

    empty = asyncio.run(
        reader.prepare(
            case={**case, "case_id": "catalyst-source-empty"},
            source_fact={"kind": "catalyst", "source_recorded_at_ms": now_ms - 20_000},
            source_first_visible_at_ms=now_ms - 10_000,
        )
    )
    assert empty.brief.evidence_catalog["source"]["status"] == "missing"
    assert "source" not in json.loads(empty.brief.text)["citable_evidence_ids"]


def test_poison_outbox_event_does_not_block_later_fact(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "poison-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        news = NewsRepository(conn)
        now_ms = int(time.time() * 1000)
        with conn.transaction():
            news.enqueue_trade_event(
                kind="oi",
                source_fact_key="poison",
                source_revision="v1",
                payload={"kind": "oi"},
                source_recorded_at_ms=now_ms,
            )
            news.enqueue_trade_event(
                kind="oi",
                source_fact_key="after-poison",
                source_revision="v1",
                payload={
                    "kind": "oi",
                    "assets": [
                        {"symbol": "SOL", "market_type": "crypto", "role": "primary"},
                    ],
                    "source_recorded_at_ms": now_ms,
                    "provider_event_at_ms": now_ms,
                },
                source_recorded_at_ms=now_ms,
            )
        settings = Settings()
        settings.storage.postgres = PostgresConfig(dsn=postgres_migration_test_dsn(), password_file=None)
        runner = AnalysisRunner(
            settings=settings, market_data=_Market(), analyst=None, files_root=tmp_path / "poison-archive"
        )

        async def relay() -> int:
            try:
                return await runner.relay_once()
            finally:
                runner._db_executor.shutdown(wait=True)

        assert asyncio.run(relay()) == 2
        events = conn.execute(
            "SELECT source_fact_key,acknowledged_at_ms,rejected_reason FROM news_trade_events ORDER BY event_id",
        ).fetchall()
        assert events[0]["source_fact_key"] == "poison"
        assert events[0]["rejected_reason"] == "trade_event_payload_invalid"
        assert events[1]["source_fact_key"] == "after-poison"
        assert events[1]["acknowledged_at_ms"] is not None
        assert conn.execute("SELECT count(*) AS n FROM trading_triggers").fetchone()["n"] == 1
    finally:
        conn.close()


def test_runner_finishes_frozen_shadow_case(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "runner-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        trading = TradingRepository(conn)
        now_ms = int(time.time() * 1000)
        payload = {
            "kind": "oi",
            "source_recorded_at_ms": now_ms,
            "provider_event_at_ms": now_ms - 1_000,
            "oi_value_usd": 1_000_000,
            "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
        }
        with conn.transaction():
            _, case_id, result = trading.accept_trigger(
                kind="oi",
                source_fact_key="fixture-oi",
                source_revision="v1",
                payload_sha256="b" * 64,
                payload=payload,
                selection=_selection(),
                now_ms=now_ms,
                root_ttl_ms=600_000,
            )
        assert result == "accepted"
        with conn.transaction():
            trading.heartbeat_analysis_runtime(
                runtime_id="binance_usdm_primary:disabled",
                now_ms=now_ms,
                active_policy="trade_assessment_v1",
                model_name="fixture",
                model_configured=True,
                publish_signals=False,
                config_digest="a" * 64,
            )
        heartbeat = trading.analysis_runtime("binance_usdm_primary:disabled")
        assert heartbeat is not None
        assert heartbeat["model_configured"] and heartbeat["heartbeat_at_ms"] == now_ms
        settings = Settings()
        settings.storage.postgres = PostgresConfig(dsn=postgres_migration_test_dsn(), password_file=None)
        files_root = tmp_path / "analysis-archive"
        runner = AnalysisRunner(settings=settings, market_data=_Market(), analyst=_Analyst(), files_root=files_root)

        async def process() -> None:
            assert await runner.analyze_one()
            runner._db_executor.shutdown(wait=True)

        asyncio.run(process())
        row = conn.execute(
            "SELECT c.state,c.analysis_status,c.evidence_ref,d.assessment_ref,d.action,"
            "d.publish_status FROM trading_cases c JOIN trading_case_decisions d USING(case_id) "
            "WHERE c.case_id=%s",
            (case_id,),
        ).fetchone()
        assert row is not None, conn.execute(
            "SELECT c.state,c.analysis_status,a.error_code FROM trading_cases c "
            "LEFT JOIN trading_case_attempts a USING(case_id) WHERE c.case_id=%s",
            (case_id,),
        ).fetchone()
        assert row["state"] == "DONE" and row["analysis_status"] == "analyzed"
        assert row["action"] == "NO_TRADE" and row["publish_status"] == "not_applicable"
        files = AnalysisFiles(files_root)
        assert files.read(row["evidence_ref"])["snapshot_version"] == "evidence_snapshot_v1"
        recorded = files.read(row["assessment_ref"])
        assert recorded["validation_status"] == "analyzed"
        assert files.read(recorded["request_ref"])["brief_sha"] == recorded["brief_sha"]
        assert (
            conn.execute("SELECT count(*) AS n FROM trading_case_outcomes WHERE case_id=%s", (case_id,)).fetchone()["n"]
            == 8
        )
    finally:
        conn.close()


def test_required_market_failure_preserves_partial_frozen_evidence_on_attempt(tmp_path) -> None:
    class MissingPerp(_Market):
        async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
            if request.dataset != "perp_bars":
                return await super().fetch(request)
            return MarketDataResult(
                status="missing",
                payload=(),
                schema_version="fixture_v1",
                source_version="fixture_v1",
                unit_definition=request.unit_definition,
                source_identity=request.source_identity,
                event_start_ms=None,
                event_end_ms=None,
                received_at_ms=None,
                missing_reasons=("price_unavailable",),
                request_receipts=(),
            )

    conn = connect_postgres_test(tmp_path / "evidence-failure-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        now_ms = int(time.time() * 1000)
        trading = TradingRepository(conn)
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="missing-price",
                source_revision="v1",
                payload_sha256="e" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": now_ms,
                    "provider_event_at_ms": now_ms - 1_000,
                    "measurement_definition": "exchange-oi-v1",
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=now_ms,
                root_ttl_ms=600_000,
            )
        settings = Settings()
        settings.storage.postgres = PostgresConfig(dsn=postgres_migration_test_dsn(), password_file=None)
        files_root = tmp_path / "evidence-failure-archive"
        runner = AnalysisRunner(settings=settings, market_data=MissingPerp(), analyst=_Analyst(), files_root=files_root)

        async def process() -> None:
            try:
                assert await runner.analyze_one()
            finally:
                runner._db_executor.shutdown(wait=True)

        asyncio.run(process())
        row = conn.execute(
            "SELECT c.state,c.analysis_status,c.evidence_ref,a.evidence_ref AS attempt_ref,"
            "a.error_code,a.assessment_ref,a.settled "
            "FROM trading_cases c JOIN trading_case_attempts a USING(case_id) WHERE c.case_id=%s",
            (case_id,),
        ).fetchone()
        assert row["state"] == "FAILED" and row["analysis_status"] == "evidence_unavailable"
        assert row["error_code"] == "required_perp_price_unavailable"
        assert row["settled"] is True and row["assessment_ref"] is None
        assert row["evidence_ref"] == row["attempt_ref"] and row["evidence_ref"]
        snapshot = AnalysisFiles(files_root).read(row["attempt_ref"])
        assert snapshot["market"]["perp_bars"]["status"] == "missing"
        assert snapshot["market"]["instrument_rules"]["status"] == "ok"
        assert (
            conn.execute("SELECT count(*) FROM trading_case_decisions WHERE case_id=%s", (case_id,)).fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_runner_persists_physical_request_before_dispatch(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect_postgres_test(tmp_path / "physical-call-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        trading = TradingRepository(conn)
        now_ms = int(time.time() * 1000)
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="physical-call",
                source_revision="v1",
                payload_sha256="f" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": now_ms - 1_000,
                    "provider_event_at_ms": now_ms - 2_000,
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=now_ms,
                root_ttl_ms=600_000,
            )

        def provider(_request: object) -> Response:
            pre_dispatch = conn.execute(
                "SELECT status,response_ref FROM trading_model_calls WHERE case_id=%s", (case_id,)
            ).fetchone()
            assert pre_dispatch == {"status": "requested", "response_ref": None}
            return Response(
                id="fixture-call",
                model="scripted/test",
                message=Message.assistant(
                    json.dumps(
                        {
                            "next_thought": "Enough information.",
                            "next_tool_name": "finish",
                            "next_tool_args": {},
                        }
                    )
                ),
                finish_reason="stop",
                usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
                provider_data={"cost": 0.00001},
            )

        analyst = TradeAnalyst(
            ConfiguredLMEndpoint(
                model_name="scripted/test", api_key="fixture", api_base="http://localhost:1/v1", model_kwargs={}
            ),
            delegate=ScriptedLM(
                [
                    provider,
                    {
                        "reasoning": "No plan selected.",
                        "proposal": {
                            "action": "NO_TRADE",
                            "selected_plan_id": None,
                            "public_rationale": "No eligible entry.",
                            "supporting_evidence": [],
                            "opposing_evidence": [],
                            "judgment_refs": [],
                        },
                    },
                ]
            ),
        )
        settings = Settings()
        settings.storage.postgres = PostgresConfig(dsn=postgres_migration_test_dsn(), password_file=None)
        runner = AnalysisRunner(
            settings=settings,
            market_data=_Market(),
            analyst=analyst,
            files_root=tmp_path / "physical-call-archive",
        )

        async def process() -> None:
            try:
                assert await runner.analyze_one()
            finally:
                runner._db_executor.shutdown(wait=True)

        asyncio.run(process())
        call = conn.execute(
            "SELECT status,response_ref,finished_at_ms,input_tokens,output_tokens,cost_microusd "
            "FROM trading_model_calls WHERE case_id=%s",
            (case_id,),
        ).fetchone()
        assert call["status"] == "completed"
        assert call["response_ref"] is not None
        assert call["finished_at_ms"] is not None
        assert (call["input_tokens"], call["output_tokens"], call["cost_microusd"]) == (10, 5, 10)
    finally:
        conn.close()


@pytest.mark.parametrize("finish_callback_lost", [False, True])
def test_provider_timeout_keeps_requested_call_and_unknown_cost_on_failed_case(
    tmp_path, monkeypatch: pytest.MonkeyPatch, finish_callback_lost: bool
) -> None:
    conn = connect_postgres_test(tmp_path / "model-timeout-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        trading = TradingRepository(conn)
        now_ms = int(time.time() * 1000)
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="model-timeout",
                source_revision="v1",
                payload_sha256="f" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": now_ms - 1_000,
                    "provider_event_at_ms": now_ms - 2_000,
                    "measurement_definition": "exchange-oi-v1",
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=now_ms,
                root_ttl_ms=600_000,
            )

        def provider(_request: object) -> None:
            assert (
                conn.execute("SELECT status FROM trading_model_calls WHERE case_id=%s", (case_id,)).fetchone()["status"]
                == "requested"
            )
            raise TimeoutError("fixture timeout")

        if finish_callback_lost:

            def fail_finish(*_args: object, **_kwargs: object) -> None:
                raise TimeoutError("fixture finish callback lost")

            monkeypatch.setattr(TradingRepository, "record_model_call_finish", fail_finish)
        analyst = TradeAnalyst(
            ConfiguredLMEndpoint(
                model_name="scripted/test", api_key="fixture", api_base="http://localhost:1/v1", model_kwargs={}
            ),
            delegate=ScriptedLM([provider]),
        )
        settings = Settings()
        settings.storage.postgres = PostgresConfig(dsn=postgres_migration_test_dsn(), password_file=None)
        files_root = tmp_path / "model-timeout-archive"
        runner = AnalysisRunner(settings=settings, market_data=_Market(), analyst=analyst, files_root=files_root)

        async def process() -> None:
            try:
                assert await runner.analyze_one()
            finally:
                runner._db_executor.shutdown(wait=True)

        asyncio.run(process())
        row = conn.execute(
            "SELECT c.state,c.analysis_status,a.error_code,a.assessment_ref,a.physical_call_count,"
            "a.cost_unknown_reason,a.settled,call.request_ref,call.response_ref,"
            "call.status AS call_status,call.finished_at_ms,"
            "call.cost_unknown_reason AS call_cost_reason "
            "FROM trading_cases c JOIN trading_case_attempts a ON a.case_id=c.case_id "
            "JOIN trading_model_calls call ON call.case_id=a.case_id AND call.claim_attempt=a.claim_attempt "
            "WHERE c.case_id=%s",
            (case_id,),
        ).fetchone()
        expected_error = "model_call_record_failed" if finish_callback_lost else "model_timeout"
        assert row["state"] == "FAILED" and row["analysis_status"] == expected_error
        assert row["error_code"] == expected_error and row["settled"] is True
        assert (
            row["physical_call_count"] == 1 and row["cost_unknown_reason"] == "one_or_more_physical_costs_unavailable"
        )
        assert row["call_cost_reason"] == "provider_cost_unavailable"
        assert row["call_status"] == "result_unknown" and row["finished_at_ms"] is not None
        files = AnalysisFiles(files_root)
        assert files.read(row["request_ref"])["request"]["messages"]
        assert row["response_ref"] is None
        assert files.read(row["assessment_ref"])["validation_status"] == expected_error
        assert files.read(row["assessment_ref"])["physical_calls"][0]["error_type"] == "TimeoutError"
        assert (
            conn.execute("SELECT count(*) FROM trading_case_decisions WHERE case_id=%s", (case_id,)).fetchone()[0] == 0
        )
    finally:
        conn.close()
