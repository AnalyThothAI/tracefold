"""A Case tool can only read a bounded, authorized fact and records the observation."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.trading_tools import CaseToolContext
from tracefold.trading.engine.marketdata import MarketDataRequest, MarketDataResult


class _Market:
    def __init__(self, *, wrong_identity: bool = False) -> None:
        self.requests: list[MarketDataRequest] = []
        self.wrong_identity = wrong_identity

    async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
        self.requests.append(request)
        now = int(time.time() * 1_000)
        return MarketDataResult(
            status="ok",
            payload=(
                {"event_at_ms": request.start_ms, "sum_open_interest_quantity": "100"},
                {"event_at_ms": request.end_ms, "sum_open_interest_quantity": "105"},
            ),
            schema_version="binance_market_v1",
            source_version="binance_public_v1",
            unit_definition=request.unit_definition,
            source_identity="other" if self.wrong_identity else request.source_identity,
            event_start_ms=request.start_ms,
            event_end_ms=request.end_ms,
            received_at_ms=now,
            missing_reasons=(),
            request_receipts=(),
        )


def _context(
    root: Path,
    market: _Market,
    *,
    allowed: bool = True,
    history_rows: tuple[dict[str, Any], ...] = (),
) -> CaseToolContext:
    files = AnalysisFiles(root)
    seed_ref = files.write({"market": {}})

    async def authorize() -> bool:
        return allowed

    async def history(_cutoff: int) -> tuple[dict[str, Any], ...]:
        return history_rows

    async def file_io(fn: Any, *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    return CaseToolContext(
        case={
            "case_id": "case-1",
            "claim_attempt": 1,
            "target_asset_id": "crypto:SOL",
            "target_selection": {
                "instrument": {
                    "native_symbol": "SOLUSDT",
                    "environment": "live",
                    "mapping_semantics_digest": "a" * 64,
                }
            },
        },
        source={"source_revision": "v1", "payload": {"kind": "oi", "oi_change_bps": 100}},
        source_first_visible_at_ms=1_000,
        prepared=SimpleNamespace(
            brief=SimpleNamespace(evidence_catalog={"source": {"status": "ok"}}),
            plans=(),
            evidence_ref=seed_ref,
        ),
        market_data=market,
        files=files,
        file_io=file_io,
        authorize=authorize,
        source_history_at=history,
        semantics=None,
    )


def test_tools_validate_before_fetch_and_archive_the_result(tmp_path: Path) -> None:
    async def run() -> None:
        market = _Market()
        context = _context(tmp_path, market)
        assert len(context.tools(object())) == 3
        invalid = json.loads(await context.get_market_snapshot("unknown", 60))
        assert invalid["status"] == "error"
        assert market.requests == []
        result = json.loads(await context.get_market_snapshot("open_interest_history", 60))
        assert result["status"] == "ok"
        assert result["values"]["oi_change_bps"] == "500.00"
        assert market.requests[0].environment == "live"
        assert market.requests[0].dataset == "open_interest_history"
        archived = context.files.read(result["tool_ref"])
        assert archived["tool"] == "get_market_snapshot"
        assert archived["result"]["ref"] in context.evidence_catalog
        excerpt = json.loads(await context.read_evidence(result["ref"], 0, 100))
        assert excerpt["status"] == "ok"
        assert len(context.tool_refs) == 3

    asyncio.run(run())


def test_expired_scope_and_response_identity_fail_closed(tmp_path: Path) -> None:
    async def run() -> None:
        market = _Market()
        denied = _context(tmp_path / "denied", market, allowed=False)
        with pytest.raises(RuntimeError, match="analysis_tool_scope_expired"):
            await denied.get_market_snapshot("open_interest_history", 60)
        assert market.requests == []
        mismatched = _context(tmp_path / "mismatched", _Market(wrong_identity=True))
        with pytest.raises(RuntimeError, match="market_response_identity_mismatch"):
            await mismatched.get_market_snapshot("open_interest_history", 60)
        assert mismatched.fatal_error() == "market_response_identity_mismatch"

    asyncio.run(run())


def test_event_context_adds_citable_same_asset_fact_with_bounded_text(tmp_path: Path) -> None:
    async def run() -> None:
        now = int(time.time() * 1_000)
        context = _context(
            tmp_path,
            _Market(),
            history_rows=(
                {
                    "trigger_id": "older",
                    "source_revision": "v2",
                    "first_visible_at_ms": now - 30_000,
                    "source_observed_at_ms": now - 40_000,
                    "payload": {"headline": "SOL changed its network", "body": "x" * 10_000},
                },
            ),
        )
        result = json.loads(await context.get_event_context("SOL", 60))
        assert result["status"] == "ok"
        assert len(result["events"][0]["text"]) == 2_048
        ref = result["events"][0]["ref"]
        assert context.evidence_catalog[ref]["source_ref"] == context.context_artifacts[ref]
        excerpt = json.loads(await context.read_evidence(ref, 0, 120))
        assert excerpt["status"] == "ok"

    asyncio.run(run())
