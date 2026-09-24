"""A selected root keeps contemporaneous market evidence without a model decision."""

from __future__ import annotations

import asyncio
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
            assert request.native_symbol == "SOLUSDT" and request.environment == "live"
            assert request.start_ms == 60_000 and request.end_ms == 180_000
            bar = {"event_at_ms": 120_000, "received_at_ms": now, "close": "100", "high": "101", "low": "99"}
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
    assert tape["quotes"][0]["bid_quantity"] == "2"
    assert tape["quotes"][0]["units_per_contract"] == "1"
    assert tape["closed_bars"][0]["received_at_ms"] == now
    assert tape["coverage"][0]["bar_status"] == "partial"
    assert files.read(tape["quotes"][0]["quote_ref"])["status"] == "ok"
