"""Migrated PostgreSQL proof for the frozen Case, six policies, paper and scoreboard."""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest

from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tracefold.app import trading_replay
from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.app.trading_assessor import AssessmentResult
from tracefold.platform.config.models import PostgresConfig, Settings
from tracefold.platform.market_identity import AssetId, InstrumentRef
from tracefold.trading.engine.case_view import BaseRates, build_case_view
from tracefold.trading.engine.forecast import Forecast, LegProbabilities, PolicyConfig, all_policy_decisions
from tracefold.trading.engine.marketdata import MarketDataRequest, MarketDataResult
from tracefold.trading.engine.paper import Bar, LegGeometry, both_legs
from tracefold.trading.engine.target import TargetSelection
from tracefold.trading.storage.root import TradingRepository

pytestmark = pytest.mark.integration


def _selection() -> TargetSelection:
    asset = AssetId("crypto", "SOL")
    instrument = InstrumentRef(
        venue="binance.usdm",
        environment="live",
        product="perpetual",
        native_symbol="SOLUSDT",
        asset_id=asset,
        quote_asset="USDT",
        settlement_asset="USDT",
        units_per_contract=Decimal(1),
        price_unit="native_quote",
        quantity_unit="native_base",
    )
    return TargetSelection("selected", asset, instrument, (asset.key,), "live-catalog")


def test_case_claim_freeze_paper_and_scoreboard_roundtrip(tmp_path, postgres_clone_dsn: str, monkeypatch) -> None:
    conn = connect_postgres_test(tmp_path, read_only=False, dsn=postgres_clone_dsn)
    try:
        trading = TradingRepository(conn)
        at = 1_800_000_000_000
        with conn.transaction():
            _, case_id, result = trading.accept_trigger(
                kind="oi",
                source_fact_key="source-1",
                source_revision="v1",
                payload_sha256="a" * 64,
                payload={"kind": "oi", "source_recorded_at_ms": at, "provider_event_at_ms": at, "oi_change_bps": 350},
                selection=_selection(),
                now_ms=at,
                root_ttl_ms=600_000,
            )
            assert result == "accepted" and case_id is not None
            assert (
                trading.accept_trigger(
                    kind="oi",
                    source_fact_key="source-1",
                    source_revision="v1",
                    payload_sha256="a" * 64,
                    payload={"kind": "oi"},
                    selection=_selection(),
                    now_ms=at,
                    root_ttl_ms=600_000,
                )[2]
                == "duplicate"
            )
            claim = trading.claim_case(now_ms=at + 1_000, lease_ms=120_000)
            assert claim is not None and claim["case_id"] == case_id
            assert trading.claim_case(now_ms=at + 1_000, lease_ms=120_000) is None
            view = build_case_view(
                case_id=case_id,
                asset_id="crypto:SOL",
                trigger_kind="oi",
                decided_at_ms=at + 1_000,
                source_fact={"kind": "oi", "oi_change_bps": 350},
                features={"perp_return_15m_bps": "100"},
                geometry=LegGeometry(200, 400),
                half_spread_bps=Decimal("2"),
                base_rates=(BaseRates("long", 0, None), BaseRates("short", 0, None)),
            )
            assert trading.freeze_case(
                case_id=case_id,
                claim_token=claim["claim_token"],
                now_ms=at + 2_000,
                view=asdict(view),
                raw_snapshot_ref="b" * 64,
                geometry_version=view.geometry.version,
                stop_bps=200,
                tp_bps=400,
                half_spread_bps=Decimal("2"),
                reference_price=Decimal("100"),
            )
            forecast = Forecast(
                LegProbabilities(Decimal("0.7"), Decimal("0.2"), Decimal("0.1")),
                LegProbabilities(Decimal("0.2"), Decimal("0.7"), Decimal("0.1")),
            )
            trading.record_assessment(
                case_id=case_id,
                program_sha="c" * 64,
                route="fixture-model",
                status="ok",
                forecast=forecast,
                notes=(),
                usage={"input_tokens": 25, "output_tokens": 20},
                started_at_ms=at + 2_000,
                ended_at_ms=at + 3_000,
            )
            decisions = all_policy_decisions(view.features, forecast, PolicyConfig(200, 400, Decimal("2")))
            trading.record_policy_actions(case_id=case_id, program_sha="c" * 64, decisions=decisions, now_ms=at + 3_000)
            assert trading.finish_case(
                case_id=case_id,
                claim_token=claim["claim_token"],
                status="complete",
                failure_code=None,
                now_ms=at + 3_000,
            )
            anchor = (at // 60_000 + 1) * 60_000
            bars = (
                Bar(anchor, Decimal("100"), Decimal("100"), Decimal("100")),
                Bar(anchor + 60_000, Decimal("105"), Decimal("99"), Decimal("103")),
            )
            legs = both_legs(
                decided_at_ms=at + 3_000, bars=bars, leg_geometry=view.geometry, half_spread_bps=Decimal("2")
            )
            trading.record_paper_legs(
                case_id=case_id, legs=legs, geometry_version=view.geometry.version, now_ms=at + 100_000
            )
        detail = trading.analysis_case(case_id)
        assert detail is not None and detail["view_sha256"]
        assert len(detail["assessments"]) == 1
        assert len(detail["policy_actions"]) == 6
        assert len(detail["paper_legs"]) == 2
        board = trading.scoreboard(since_ms=at - 1, until_ms=at + 86_400_000)
        assert board["funnel"]["triggers"] == 1
        assert board["funnel"]["selected"] == 1
        assert board["funnel"]["assessed"] == 1
        assert len(board["programs"]) == 1
        assert len(board["programs"][0]["policies"]) == 6
        assert board["programs"][0]["forecast"]["status"] == "insufficient_data"
        settings = Settings()
        settings.storage.postgres = PostgresConfig(
            dsn=postgres_migration_test_dsn(postgres_clone_dsn), password_file=None
        )
        settings.trading.analysis.model_name = "candidate-model"
        settings.llm.api_key = "fixture-key"
        settings.llm.base_url = "https://fixture.invalid/v1"
        cache_calls: list[bool] = []

        def fake_lm(_endpoint, **kwargs):
            cache_calls.append(kwargs["cache"])
            return object()

        class FakeAssessor:
            def __init__(self, **_kwargs):
                pass

            async def assess(self, _view):
                return AssessmentResult(forecast, "complete", None, (), {"input_tokens": 1, "output_tokens": 1})

        monkeypatch.setattr(trading_replay, "generative_lm", fake_lm)
        monkeypatch.setattr(trading_replay, "TradingAssessor", FakeAssessor)
        artifact = Path(__file__).resolve().parents[2] / "tracefold/trading/programs/forecast_v1.json"
        result = asyncio.run(
            trading_replay.replay(settings, program_file=artifact, since_ms=at - 1, until_ms=at + 86_400_000)
        )
        assert result["cases"] == result["assessed"] == 1
        assert cache_calls == [True]
        first_replay = trading.analysis_case(case_id)
        assert first_replay is not None
        assert len(first_replay["policy_actions"]) == 12
        assert (
            asyncio.run(
                trading_replay.replay(settings, program_file=artifact, since_ms=at - 1, until_ms=at + 86_400_000)
            )
            == result
        )
        second_replay = trading.analysis_case(case_id)
        assert second_replay is not None
        assert second_replay["assessments"] == first_replay["assessments"]
        assert second_replay["policy_actions"] == first_replay["policy_actions"]
        assert cache_calls == [True, True]
        assert conn.execute("SELECT count(*) AS n FROM trading_signals").fetchone()["n"] == 0
    finally:
        conn.close()


def test_public_correction_blocks_the_original_catalyst_before_signal(tmp_path, postgres_clone_dsn: str) -> None:
    conn = connect_postgres_test(tmp_path, read_only=False, dsn=postgres_clone_dsn)
    try:
        trading = TradingRepository(conn)
        at = 1_800_000_000_000
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="catalyst",
                source_fact_key="event-sol",
                source_revision="v1",
                payload_sha256="a" * 64,
                payload={"kind": "catalyst_delta", "claim_refs": ["original-claim"]},
                selection=_selection(),
                now_ms=at,
                root_ttl_ms=600_000,
            )
            assert case_id is not None
            assert trading.publication_source_status(case_id=case_id, now_ms=at) is None
            assert (
                trading.receive_source_update(
                    update_id="correction-1",
                    source_fact_key="event-sol",
                    content_revision="v2",
                    affected_claim_refs=("original-claim",),
                    retired_claim_refs=("original-claim",),
                    payload={"kind": "source_update"},
                    payload_sha256="b" * 64,
                    now_ms=at + 1_000,
                )
                == "accepted"
            )
            assert trading.publication_source_status(case_id=case_id, now_ms=at + 1_000) == "source_corrected"
    finally:
        conn.close()


def test_runner_freezes_live_data_and_finishes_without_executor(tmp_path, postgres_clone_dsn: str) -> None:
    class Market:
        def __init__(self) -> None:
            self.requests: list[MarketDataRequest] = []

        async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
            self.requests.append(request)
            assert request.environment == "live"
            received = int(time.time() * 1_000)
            if request.dataset in ("perp_bars", "spot_bars", "market_bars"):
                assert request.end_ms is not None
                count = 241 if request.dataset == "perp_bars" else 61
                rows = tuple(
                    {
                        "event_at_ms": request.end_ms - (count - index - 1) * 60_000,
                        "high": "101",
                        "low": "99",
                        "close": "100",
                        "quote_volume": "1000",
                        "taker_buy_quote_volume": "500",
                        "received_at_ms": received,
                    }
                    for index in range(count)
                )
            elif request.dataset == "book_ticker":
                rows = ({"bid": "99.99", "ask": "100.01"},)
            else:
                rows = ()
            return MarketDataResult(
                status="ok" if rows else "missing",
                payload=rows,
                schema_version="fixture",
                source_version="fixture",
                unit_definition=request.unit_definition,
                source_identity=request.source_identity,
                event_start_ms=rows[0].get("event_at_ms") if rows else None,
                event_end_ms=rows[-1].get("event_at_ms") if rows else None,
                received_at_ms=received if rows else None,
                missing_reasons=() if rows else ("fixture_optional_missing",),
                request_receipts=(),
            )

    class Assessor:
        async def assess(self, _view):
            return AssessmentResult(
                Forecast(
                    LegProbabilities(Decimal("0.7"), Decimal("0.2"), Decimal("0.1")),
                    LegProbabilities(Decimal("0.2"), Decimal("0.7"), Decimal("0.1")),
                ),
                "complete",
                None,
                (),
                {"input_tokens": 30, "output_tokens": 20},
            )

    at = int(time.time() * 1_000)
    conn = connect_postgres_test(tmp_path, read_only=False, dsn=postgres_clone_dsn)
    try:
        with conn.transaction():
            _, case_id, _ = TradingRepository(conn).accept_trigger(
                kind="oi",
                source_fact_key="runner-oi",
                source_revision="v1",
                payload_sha256="d" * 64,
                payload={"kind": "oi", "source_recorded_at_ms": at, "provider_event_at_ms": at, "oi_change_bps": 350},
                selection=_selection(),
                now_ms=at,
                root_ttl_ms=600_000,
            )
        assert case_id is not None
        settings = Settings()
        settings.set_config_dir(tmp_path)
        settings.storage.postgres = PostgresConfig(
            dsn=postgres_migration_test_dsn(postgres_clone_dsn), password_file=None
        )
        settings.trading.analysis.publish_signals = False
        market = Market()
        runner = AnalysisRunner(
            settings=settings,
            market_data=market,
            assessor=Assessor(),
            program_sha="e" * 64,
            raw_root=tmp_path / "archive" / "trading-cases",
        )
        try:
            assert asyncio.run(runner.analyze_one()) is True
        finally:
            runner._db_pool.shutdown(wait=True)
        row = TradingRepository(conn).analysis_case(case_id)
        assert row is not None and row["state"] == "complete"
        assert len(row["policy_actions"]) == 6
        assert row["view"]["model_input"]["trigger"] == "oi"
        assert all(item.environment == "live" for item in market.requests)
        assert len(market.requests) == 6
        digest = row["raw_snapshot_ref"]
        raw = tmp_path / "archive" / "trading-cases" / digest[:2] / f"{digest}.json.gz"
        snapshot = json.loads(gzip.decompress(raw.read_bytes()))
        assert snapshot["data_environment"] == "live"
        assert row["policy_actions"][0]["publish_status"] != "published"
    finally:
        conn.close()


def test_runner_records_both_missing_legs_when_live_geometry_cannot_be_frozen(
    tmp_path, postgres_clone_dsn: str
) -> None:
    class NoMarket:
        async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
            return MarketDataResult(
                status="missing",
                payload=(),
                schema_version="fixture",
                source_version="fixture",
                unit_definition=request.unit_definition,
                source_identity=request.source_identity,
                event_start_ms=None,
                event_end_ms=None,
                received_at_ms=None,
                missing_reasons=("fixture_no_live_bars",),
                request_receipts=(),
            )

    at = int(time.time() * 1_000)
    conn = connect_postgres_test(tmp_path, read_only=False, dsn=postgres_clone_dsn)
    try:
        with conn.transaction():
            _, case_id, _ = TradingRepository(conn).accept_trigger(
                kind="oi",
                source_fact_key="missing-market-oi",
                source_revision="v1",
                payload_sha256="f" * 64,
                payload={"kind": "oi", "source_recorded_at_ms": at, "provider_event_at_ms": at},
                selection=_selection(),
                now_ms=at,
                root_ttl_ms=600_000,
            )
        assert case_id is not None
        settings = Settings()
        settings.set_config_dir(tmp_path)
        settings.storage.postgres = PostgresConfig(
            dsn=postgres_migration_test_dsn(postgres_clone_dsn), password_file=None
        )
        runner = AnalysisRunner(
            settings=settings,
            market_data=NoMarket(),
            assessor=None,
            program_sha="e" * 64,
            raw_root=tmp_path / "archive" / "trading-cases",
        )
        try:
            assert asyncio.run(runner.analyze_one()) is True
        finally:
            runner._db_pool.shutdown(wait=True)
        row = TradingRepository(conn).analysis_case(case_id)
        assert row is not None and row["state"] == "failed"
        assert row["view"] is None and row["failure_code"] == "data_missing"
        assert [(leg["side"], leg["status"], leg["reason"]) for leg in row["paper_legs"]] == [
            ("long", "missing", "case_preparation_failed"),
            ("short", "missing", "case_preparation_failed"),
        ]
        assert not row["assessments"] and not row["policy_actions"]
    finally:
        conn.close()
