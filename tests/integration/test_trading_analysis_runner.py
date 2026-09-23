"""One claimed Case freezes provider data, records an assessment and settles independently."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from tests.integration.test_trading_analysis_storage import _selection
from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    reset_postgres_schema,
)
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.trading_analysis import AnalysisRunner, FrameReader
from tracefold.app.trading_analyst import AnalystCallReceipt
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.config.models import PostgresConfig, Settings
from tracefold.trading.engine.contracts import AgentAssessment, CandidateAssessment, FactorAssessment
from tracefold.trading.engine.marketdata import MarketDataRequest, MarketDataResult
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
        menu = json.loads(brief.text)["candidate_menu"]
        factors = tuple(
            FactorAssessment(
                factor_id=factor,
                weight_bps=5_000 if index < 2 else 0,
                support_score=-20,
                status="known",
                evidence_refs=("market:perp_bars",),
            )
            for index, factor in enumerate(
                (
                    "catalyst",
                    "price_structure",
                    "volume_and_oi",
                    "crowding",
                    "entry_timing",
                    "trading_cost",
                )
            )
        )
        answer = AgentAssessment(
            brief_sha=brief.sha,
            candidate_menu_sha=brief.candidate_menu_sha,
            action="NO_TRADE",
            candidate_assessments=tuple(
                CandidateAssessment(candidate_id=item["candidate_id"], factors=factors) for item in menu
            ),
            public_rationale="The frozen evidence is insufficient to trade.",
            supporting_evidence=("market:perp_bars",),
        )
        return AnalystCallReceipt(
            brief_sha=brief.sha,
            menu_sha=brief.candidate_menu_sha,
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


def test_oi_case_requires_current_oi_evidence_before_model(tmp_path) -> None:
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
        "target_selection": {
            "reason": "selected",
            "asset_id": "crypto:SOL",
            "instrument": {"native_symbol": "SOLUSDT", "environment": "demo", "mapping_semantics_digest": "a" * 64},
        },
    }
    source = {"kind": "oi", "oi_value_usd": 1_000_000}
    with pytest.raises(ValueError, match="required_market_oi_unavailable"):
        asyncio.run(reader.prepare(case=case, source_fact=source))


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
