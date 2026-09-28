"""Shared, narrowly scoped Trading analysis fixtures."""

from __future__ import annotations

import time
from decimal import Decimal

from tracefold.app.trading_analyst import AnalystCallReceipt
from tracefold.platform.market_identity import DEFAULT_UNIVERSE, AssetId, AssetRegistry, InstrumentRef
from tracefold.trading.engine.marketdata import MarketDataRequest, MarketDataResult
from tracefold.trading.engine.plans import AnalysisProposal
from tracefold.trading.engine.target import SourceAsset, select_target


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
    )


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
        elif request.dataset == "instrument_rules":
            payload = (
                {
                    "event_at_ms": now_ms,
                    "received_at_ms": now_ms,
                    "native_symbol": request.native_symbol,
                    "base_asset": request.native_symbol.removesuffix("USDT"),
                    "quote_asset": "USDT",
                    "settlement_asset": "USDT",
                    "trading_status": "TRADING",
                    "contract_type": "PERPETUAL",
                },
            )
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
            selected_plan_id=None,
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
