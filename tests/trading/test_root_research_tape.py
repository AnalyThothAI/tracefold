"""A selected root keeps contemporaneous market evidence without a model decision."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from tracefold.app import trading_analysis
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.trading.engine.marketdata import MarketDataResult


def test_root_tape_archives_quote_and_closed_bars_without_decision(tmp_path, monkeypatch) -> None:
    now = 180_000
    monkeypatch.setattr(trading_analysis, "_clock_ms", lambda: now)
    files = AnalysisFiles(tmp_path)
    row = {
        "case_id": "root-1",
        "tape_ref": None,
        "next_sample_at_ms": now,
        "first_visible_at_ms": 100_000,
        "created_at_ms": 110_000,
        "root_expires_at_ms": 700_000,
        "target_selection": {
            "instrument": {
                "native_symbol": "SOLUSDT",
                "environment": "live",
                "mapping_semantics_digest": "mapping-1",
                "units_per_contract": "1",
            }
        },
    }

    class Repo:
        def due_root_research_tapes(self, *, now_ms, limit):
            return [dict(row)] if row["next_sample_at_ms"] <= now_ms else []

        def record_root_research_sample(self, *, case_id, prior_ref, tape_ref, sampled_at_ms):
            assert case_id == "root-1" and row["tape_ref"] == prior_ref
            row["tape_ref"] = tape_ref
            row["next_sample_at_ms"] = sampled_at_ms + 60_000
            return True

    async def db_async(call, **_kwargs):
        return call(SimpleNamespace(trading=Repo()))

    async def quote(_case):
        return {
            "status": "ok",
            "environment": "live",
            "native_symbol": "SOLUSDT",
            "mapping_semantics_digest": "mapping-1",
            "units_per_contract": "1",
            "payload": ({"received_at_ms": now, "bid": "99", "ask": "101", "bid_quantity": "2", "ask_quantity": "3"},),
        }

    class Market:
        async def fetch(self, request):
            assert request.dataset in ("perp_bars", "mark_bars", "funding_history")
            assert request.native_symbol == "SOLUSDT" and request.environment == "live"
            if request.dataset == "funding_history":
                assert request.start_ms == row["created_at_ms"]
                return MarketDataResult(
                    status="ok",
                    payload=(),
                    schema_version="fixture",
                    source_version="fixture",
                    unit_definition=request.unit_definition,
                    source_identity=request.source_identity,
                    event_start_ms=None,
                    event_end_ms=None,
                    received_at_ms=None,
                    missing_reasons=(),
                    request_receipts=({"endpoint": "/fapi/v1/fundingRate", "http_status": 200},),
                )
            assert request.start_ms == min(
                now // 60_000 * 60_000 - 60_000,
                max(row["created_at_ms"] // 60_000 * 60_000, now // 60_000 * 60_000 - 240_000),
            )
            assert request.end_ms == now // 60_000 * 60_000
            bar = {
                "event_at_ms": 180_000 if now == 300_000 else request.end_ms - 60_000,
                "received_at_ms": now,
                "close": "100",
                "high": "101",
                "low": "99",
            }
            return MarketDataResult(
                status="partial",
                payload=(bar,),
                schema_version="fixture",
                source_version="fixture",
                unit_definition=request.unit_definition,
                source_identity=request.source_identity,
                event_start_ms=120_000,
                event_end_ms=120_000,
                received_at_ms=now,
                missing_reasons=("coverage_incomplete",),
                request_receipts=(),
            )

    runner = SimpleNamespace(
        _db_async=db_async,
        _read_executable_quote=quote,
        reader=SimpleNamespace(market_data=Market()),
        files=files,
    )
    assert asyncio.run(trading_analysis.AnalysisRunner.sample_root_research_once(runner)) == 1
    tape = files.read(row["tape_ref"])
    assert tape["source_first_visible_at_ms"] == 100_000
    assert tape["root_accepted_at_ms"] == 110_000
    assert tape["quotes"][0]["bid_quantity"] == "2"
    assert tape["quotes"][0]["units_per_contract"] == "1"
    assert tape["closed_bars"][0]["received_at_ms"] == now
    assert tape["mark_bars"][0]["received_at_ms"] == now
    assert tape["coverage"][0]["bar_status"] == "partial"
    assert tape["coverage"][0]["mark_status"] == "partial"
    assert files.read(tape["quotes"][0]["quote_ref"])["status"] == "ok"
    now = 300_000
    monkeypatch.setattr(trading_analysis, "_clock_ms", lambda: now)
    assert asyncio.run(trading_analysis.AnalysisRunner.sample_root_research_once(runner)) == 1
    tape = files.read(row["tape_ref"])
    assert [bar["event_at_ms"] for bar in tape["mark_bars"]] == [120_000, 180_000]
    assert tape["mark_bars"][1]["received_at_ms"] == 300_000
    now = row["root_expires_at_ms"] + 14_400_000 + 120_000 + 120_000
    monkeypatch.setattr(trading_analysis, "_clock_ms", lambda: now)
    assert asyncio.run(trading_analysis.AnalysisRunner.sample_root_research_once(runner)) == 1
    tape = files.read(row["tape_ref"])
    assert tape["funding_history"]["status"] == "ok"
    assert tape["funding_history"]["payload"] == []
    assert tape["funding_history"]["scan_received_at_ms"] == now
    assert files.read(tape["funding_history"]["snapshot_ref"])["request_receipts"][0]["http_status"] == 200


def test_shadow_evaluation_consumes_contemporaneous_root_tape_without_historical_refetch(tmp_path, monkeypatch) -> None:
    files = AnalysisFiles(tmp_path)
    native = "SOLUSDT"
    mapping = "mapping-1"
    instrument = {
        "native_symbol": native,
        "environment": "live",
        "mapping_semantics_digest": mapping,
        "units_per_contract": "1",
    }
    quote_base = {
        "status": "ok",
        "environment": "live",
        "native_symbol": native,
        "mapping_semantics_digest": mapping,
        "units_per_contract": "1",
    }
    entry_ref = files.write(
        {
            **quote_base,
            "payload": [
                {"received_at_ms": 1_000, "bid": "99", "ask": "101", "bid_quantity": "20", "ask_quantity": "20"}
            ],
        }
    )
    exit_ref = files.write({**quote_base, "received_at_ms": 60_100})
    quote_tape_ref = files.write(
        {
            "version": "shadow_quote_tape_v1",
            "samples": [
                {
                    **quote_base,
                    "quote_ref": exit_ref,
                    "received_at_ms": 60_100,
                    "bid": "98",
                    "ask": "99",
                    "bid_quantity": "20",
                    "ask_quantity": "20",
                }
            ],
        }
    )
    rules = {
        "native_symbol": native,
        "trading_status": "TRADING",
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
        "settlement_asset": "USDT",
        "price_tick_size": "0.01",
        "market_min_quantity": "0.01",
        "market_max_quantity": "1000",
        "market_step_size": "0.01",
        "minimum_notional": "5",
    }
    evidence_ref = files.write(
        {
            "data_environment": "live",
            "market": {
                "instrument_rules": {
                    "status": "ok",
                    "unit_definition": "binance_usdm_contract_rules_v1",
                    "received_at_ms": 900,
                    "payload": [rules],
                }
            },
        }
    )
    root_expiry = 10_000
    funding_ref = files.write({"status": "ok", "payload": []})
    mark_snapshot_ref = files.write(
        {"status": "ok", "payload": [{"event_at_ms": 60_000, "high": "106", "low": "98", "close": "100"}]}
    )
    root_tape_ref = files.write(
        {
            "version": "root_research_tape_v2",
            "case_id": "root-1",
            "native_symbol": native,
            "environment": "live",
            "mapping_semantics_digest": mapping,
            "root_accepted_at_ms": 500,
            "root_expires_at_ms": root_expiry,
            "mark_bars": [
                {
                    "event_at_ms": 60_000,
                    "received_at_ms": 60_100,
                    "high": "106",
                    "low": "98",
                    "close": "100",
                    "snapshot_ref": mark_snapshot_ref,
                }
            ],
            "funding_history": {
                "status": "ok",
                "payload": [],
                "snapshot_ref": funding_ref,
                "scan_received_at_ms": root_expiry + 14_400_000 + 120_000 + 120_000,
            },
        }
    )
    due = {
        "case_id": "child-1",
        "decision": {
            "side": "long",
            "exit_plan": {"stop_distance_bps": 200, "take_profit_bps": 400, "max_holding_seconds": 14_400},
        },
        "decision_at_ms": 1_000,
        "scheduled_at_ms": 1_000,
        "due_at_ms": 14_401_000,
        "target_selection": {"instrument": instrument},
        "decision_quote_ref": entry_ref,
        "planned_quote_ref": entry_ref,
        "quote_tape_ref": quote_tape_ref,
        "evidence_ref": evidence_ref,
        "root_market_tape_ref": root_tape_ref,
        "root_case_id": "root-1",
        "root_accepted_at_ms": 500,
        "root_expires_at_ms": root_expiry,
    }
    monkeypatch.setattr(trading_analysis, "_clock_ms", lambda: due["due_at_ms"] + 48 * 3_600_000 + 1)
    settled = []

    class Repo:
        def due_shadow_evaluations(self, *, now_ms, limit):
            return [due]

        def settle_shadow_evaluation(self, **kwargs):
            settled.append(kwargs)
            return True

    async def db_async(call, **_kwargs):
        return call(SimpleNamespace(trading=Repo()))

    class NoHistoricalFetch:
        async def fetch(self, _request):
            raise AssertionError("shadow must use the frozen root tape")

    runner = SimpleNamespace(
        _db_async=db_async,
        files=files,
        reader=SimpleNamespace(market_data=NoHistoricalFetch()),
        settings=SimpleNamespace(
            trading=SimpleNamespace(
                execution=SimpleNamespace(max_risk_per_trade_usd=Decimal("1")),
                analysis=SimpleNamespace(shadow_fee_bps_per_side=Decimal("5")),
            )
        ),
        _config_digest="fixture",
    )
    assert asyncio.run(trading_analysis.AnalysisRunner.evaluate_once(runner)) == 1
    assert settled[0]["result"]["status"] == "simulated"
    assert settled[0]["mark_path_ref"] == root_tape_ref
    assert settled[0]["funding_ref"] == funding_ref
    due["root_market_tape_ref"] = None
    settled.clear()
    assert asyncio.run(trading_analysis.AnalysisRunner.evaluate_once(runner)) == 1
    assert settled[0]["result"]["status"] == "unevaluable"
    assert settled[0]["result"]["reason"] == "mark_path_incomplete"
