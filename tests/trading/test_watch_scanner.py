"""The first closed crossing remains auditable when the watcher wakes after expiry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tracefold.app import trading_analysis
from tracefold.trading.engine.marketdata import MarketDataResult


def test_late_watch_scan_records_first_cross_as_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = 1_620_000
    expiry = frozen + 180_000
    monkeypatch.setattr(trading_analysis, "_clock_ms", lambda: expiry + 60_001)
    writes: list[dict[str, object]] = []
    advances: list[dict[str, object]] = []
    row = {
        "parent_case_id": "case-1",
        "condition": {
            "frozen_at_ms": frozen,
            "expires_at_ms": expiry,
            "previous_close": "100",
            "upper_level": "101",
            "lower_level": "99",
        },
        "last_observed_at_ms": None,
        "last_observed_value": None,
        "expires_at_ms": expiry,
        "target_selection": {"instrument": {"native_symbol": "BTCUSDT", "environment": "mainnet"}},
    }

    class Market:
        async def fetch(self, request):
            assert request.end_ms == expiry
            payload = (
                {"event_at_ms": frozen + 60_000, "close": "100"},
                {"event_at_ms": frozen + 120_000, "close": "102"},
                {"event_at_ms": expiry, "close": "103"},
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
                received_at_ms=expiry + 60_001,
                missing_reasons=(),
                request_receipts=(),
            )

    class Repo:
        def due_watch_observations(self, **_kwargs):
            return [row]

        def advance_watch_observation(self, **kwargs):
            advances.append(kwargs)
            return True

    async def db_async(call, **_kwargs):
        return call(SimpleNamespace(trading=Repo()))

    def write(value):
        writes.append(value)
        return "archive:watch-observation"

    runner = SimpleNamespace(
        _db_async=db_async,
        reader=SimpleNamespace(market_data=Market()),
        files=SimpleNamespace(write=write),
    )
    assert asyncio.run(trading_analysis.AnalysisRunner.watch_once(runner)) == 1
    assert writes[0]["observed_path"] == [(frozen + 60_000, "100"), (frozen + 120_000, "102")]
    assert advances[0]["observation_status"] == "missed"
    assert advances[0]["trigger_side"] == "long"
