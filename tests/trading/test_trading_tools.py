"""A Case tool can only read a bounded, authorized fact and records the observation."""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.trading.news_public_updates import first_report
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.system_one import SystemOneConnection
from tracefold.app.trading_prepared import PreparedAnalysis
from tracefold.app.trading_tools import CaseToolContext
from tracefold.trading.engine.brief import AnalystBrief
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


class _BarMarket(_Market):
    def __init__(self) -> None:
        super().__init__()
        self.last_close = "100"

    async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
        self.requests.append(request)
        assert request.start_ms is not None and request.end_ms is not None
        now = int(time.time() * 1_000)
        rows = tuple(
            {
                "open_at_ms": stamp,
                "event_at_ms": stamp + 60_000,
                "received_at_ms": now,
                "high": "102",
                "low": "98",
                "close": self.last_close if stamp == request.end_ms - 60_000 else "100",
                "quote_volume": "10",
                "taker_buy_quote_volume": "4",
            }
            for stamp in range(request.start_ms, request.end_ms, 60_000)
        )
        return MarketDataResult(
            status="ok",
            payload=rows,
            schema_version="fixture",
            source_version="fixture",
            unit_definition=request.unit_definition,
            source_identity=request.source_identity,
            event_start_ms=rows[0]["event_at_ms"],
            event_end_ms=rows[-1]["event_at_ms"],
            received_at_ms=now,
            missing_reasons=(),
            request_receipts=(),
        )


class _Budget:
    def __init__(self) -> None:
        self.deadline_at_monotonic = time.monotonic() + 60

    def remaining_ms(self) -> int:
        return max(1, int((self.deadline_at_monotonic - time.monotonic()) * 1000))

    async def start(self, request_payload: dict[str, Any]) -> tuple[int, int]:
        return 0, self.remaining_ms()

    async def finish(self, call_index: int, call: Any) -> None:
        return None


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

    async def history(
        cutoff: int,
        *,
        topic: str | None = None,
        lookback_minutes: int | None = None,
        include_probe: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        selected = tuple(
            row
            for row in history_rows
            if row["first_visible_at_ms"] <= cutoff
            and (lookback_minutes is None or row["first_visible_at_ms"] >= cutoff - lookback_minutes * 60_000)
            and (topic is None or topic.casefold() in json.dumps(row["payload"]).casefold())
        )
        return selected[: 9 if include_probe else 8]

    async def file_io(fn: Any, *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    context = CaseToolContext(
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
        prepared=PreparedAnalysis(
            brief=AnalystBrief(text="{}", sha="", plan_menu_sha="", evidence_catalog={"source": {"status": "ok"}}),
            plans=(),
            evidence_ref=seed_ref,
            brief_ref=seed_ref,
            reference_price=Decimal("100"),
            reference_at_ms=1_000,
            source_history=(),
            source_amendments=(),
        ),
        market_data=market,
        files=files,
        file_io=file_io,
        authorize=authorize,
        source_history_at=history,
        semantics=None,
    )
    context.tools(_Budget())
    return context


def test_tools_validate_before_fetch_and_archive_the_result(tmp_path: Path) -> None:
    async def run() -> None:
        market = _Market()
        context = _context(tmp_path, market)
        expired = _Budget()
        expired.deadline_at_monotonic = time.monotonic() - 1
        with pytest.raises(TimeoutError, match="analysis_tool_budget_expired"):
            context.tools(expired)
        assert len(context.tools(_Budget())) == 3
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


def test_market_defaults_real_environment_features_and_duplicate_menu(tmp_path: Path) -> None:
    async def run() -> None:
        instant = _Market()
        context = _context(tmp_path / "instant", instant)
        assert json.loads(await context.get_market_snapshot("open_interest"))["status"] == "ok"
        assert instant.requests[-1].start_ms is None
        count = len(instant.requests)
        assert json.loads(await context.get_market_snapshot("open_interest", 15))["reason"] == "market_window_invalid"
        assert len(instant.requests) == count

        market = _BarMarket()
        context = _context(tmp_path / "bars", market)
        context.case["target_selection"]["instrument"]["environment"] = "demo"
        context.case["root_expires_at_ms"] = int(time.time() * 1_000) + 600_000
        context.source["payload"]["measurement_definition"] = "exchange-oi-v1"
        context.evidence_catalog["source"] = {
            "status": "ok",
            "values": {"oi_change_bps": 100},
            "unit_definition": "bps",
            "event_at_ms": 1_000,
            "received_at_ms": 1_000,
            "knowledge_cutoff_ms": int(time.time() * 1_000),
        }
        first = json.loads(await context.get_market_snapshot("perp_bars"))
        assert first["effective_window_minutes"] == 60
        assert first["row_count"] == 61
        assert first["feature_values"]["perp_return_60m_bps"] == "0"
        feature_ref = first["feature_refs"]["perp_return_60m_bps"]
        assert context.evidence_catalog[feature_ref]["status"] == "ok"
        assert context.evidence_catalog[feature_ref]["row_count"] == 61
        assert len(first["added_plans"]) == 4
        again = json.loads(await context.get_market_snapshot("perp_bars", 60))
        assert again["added_plans"] == []
        assert len(context.plans) == 4
        market.last_close = "101"
        changed = json.loads(await context.get_market_snapshot("perp_bars", 60))
        assert len(changed["added_plans"]) == 4
        short = json.loads(await context.get_market_snapshot("perp_bars", 15))
        assert "perp_return_60m_bps" not in short["feature_values"]
        spot = json.loads(await context.get_market_snapshot("spot_bars", 60))
        assert spot["environment"] == market.requests[-1].environment == "live"
        assert market.requests[-1].product == "spot"
        assert market.requests[0].environment == "live"

    asyncio.run(run())


def test_partial_dynamic_frame_exposes_only_proven_feature_windows(tmp_path: Path) -> None:
    class _EarlyGapMarket(_BarMarket):
        async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
            complete = await super().fetch(request)
            return MarketDataResult(
                status="partial",
                payload=complete.payload[-61:],
                schema_version=complete.schema_version,
                source_version=complete.source_version,
                unit_definition=complete.unit_definition,
                source_identity=complete.source_identity,
                event_start_ms=complete.payload[-61]["event_at_ms"],
                event_end_ms=complete.event_end_ms,
                received_at_ms=complete.received_at_ms,
                missing_reasons=("early_gap",),
                request_receipts=(),
            )

    async def run() -> None:
        context = _context(tmp_path, _EarlyGapMarket())
        context.case["root_expires_at_ms"] = int(time.time() * 1_000) + 600_000
        result = json.loads(await context.get_market_snapshot("perp_bars", 240))
        assert result["status"] == "partial"
        assert result["coverage_complete"] is False
        assert "perp_return_240m_bps" not in result["feature_values"]
        assert result["feature_values"]["perp_return_60m_bps"] == "0"
        ref = result["feature_refs"]["perp_return_60m_bps"]
        assert context.evidence_catalog[ref]["status"] == "ok"
        assert context.evidence_catalog[ref]["row_count"] == 61
        assert context.evidence_catalog[result["ref"]]["status"] == "partial"

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


def test_event_context_reads_a_news_catalyst_as_its_public_text(tmp_path: Path) -> None:
    async def run() -> None:
        now = int(time.time() * 1_000)
        _, catalyst = first_report(
            event_id="event-sol", first_available_at_ms=now - 40_000, completed_at_ms=now - 35_000
        )
        context = _context(
            tmp_path,
            _Market(),
            history_rows=(
                {
                    "trigger_id": "older-catalyst",
                    "source_revision": catalyst.content_revision,
                    "first_visible_at_ms": now - 30_000,
                    "source_observed_at_ms": catalyst.first_available_at_ms,
                    "payload": catalyst.model_dump(mode="json"),
                },
            ),
        )
        result = json.loads(await context.get_event_context("swap fee", 60))
        assert result["status"] == "ok"
        assert result["events"][0]["text"] == catalyst.text
        ref = result["events"][0]["ref"]
        assert context.evidence_catalog[ref]["values"] == {"text": catalyst.text}
        assert context.evidence_catalog[ref]["event_at_ms"] == now - 40_000

    asyncio.run(run())


def test_jev_reads_the_selected_authorized_fragment_and_returns_distribution(tmp_path: Path) -> None:
    async def run() -> None:
        sent: list[dict[str, Any]] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            sent.append(json.loads(request.content))
            return httpx2.Response(
                200,
                json={
                    "id": "judgment-request-1",
                    "provider": "typesafe",
                    "model": "jev-fixture",
                    "usage": {"input_tokens": 20, "output_tokens": 3, "cost": 0.0001},
                    "answers": {
                        "verdict": {
                            "type": "choice",
                            "choice": "contradicts",
                            "confidence": 0.7,
                            "probabilities": {"supports": 0.1, "contradicts": 0.7, "mixed": 0.1, "insufficient": 0.1},
                        }
                    },
                },
            )

        connection = SystemOneConnection(
            base_url="https://fixture.invalid/api",
            api_key="fixture-key",
            model="jev-fixture",
            async_transport=httpx2.MockTransport(respond),
        )
        try:
            context = _context(tmp_path, _Market())
            context.semantics = connection
            material = "A" * 5_000 + "not executed"
            context.source["payload"] = {"kind": "catalyst_delta", "text": material}
            context.evidence_catalog["source"] = {
                "status": "ok",
                "values": {"text": material[:100]},
                "unit_definition": "text",
                "event_at_ms": 1_000,
                "received_at_ms": 1_000,
                "knowledge_cutoff_ms": int(time.time() * 1_000),
            }

            class Ledger:
                deadline_at_monotonic = time.monotonic() + 10

                def __init__(self) -> None:
                    self.calls: list[Any] = []

                def remaining_ms(self) -> int:
                    return 10_000

                async def start(self, _request: dict[str, Any]) -> tuple[int, int]:
                    return 0, 10_000

                async def finish(self, _index: int, call: Any) -> None:
                    self.calls.append(call)

            ledger = Ledger()
            context.tools(ledger)
            read = json.loads(await context.read_evidence("source", 5_000, 5_012))
            assert read["text"] == "not executed"
            answer = json.loads(
                await context.assess_claims(
                    "The action was executed.",
                    ["source"],
                    {"source": [5_000, 5_012]},
                )
            )
            assert answer["status"] == "ok" and answer["verdict"] == "contradicts"
            assert answer["probabilities"]["contradicts"] == 0.7
            assert answer["ranges"] == {"source": [5_000, 5_012]}
            assert sent[0]["state"]["inputs"]["evidence"][0]["material"] == "not executed"
            assert len(ledger.calls) == 1
            assert json.loads(await context.read_evidence("source", 6_000, 6_010))["status"] == "error"
            assert (
                json.loads(
                    await context.assess_claims(
                        "test",
                        ["source"],
                        {"other": [0, 10]},
                    )
                )["reason"]
                == "claim_ranges_invalid"
            )
            assert len(sent) == 1
        finally:
            await connection.aclose()

    asyncio.run(run())
