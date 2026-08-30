from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tracefold.integrations import trading_catalog
from tracefold.integrations.trading_catalog import (
    VenueExpectedError,
    fetch_binance_usdm_catalog,
    fetch_hyperliquid_perp_catalog,
)
from tracefold.trading import (
    ExecutionInstrumentEvidenceV1,
    build_execution_capability_snapshot,
    build_venue_catalog_snapshot,
)


def test_binance_catalog_keeps_inactive_and_malformed_provider_rows() -> None:
    payload = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "pair": "BTCUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "onboardDate": 1,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                ],
            },
            {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "status": "SETTLING", "baseAsset": "OLD"},
            "malformed",
        ]
    }

    rows = asyncio.run(
        fetch_binance_usdm_catalog(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))
    )

    assert len(rows) == 3
    assert rows[0].price_increment == "0.10"
    assert rows[1].active is False
    assert rows[2].normalization_error == "provider_instrument_row_invalid"

    reordered = {"symbols": list(reversed(payload["symbols"]))}
    reordered_rows = asyncio.run(
        fetch_binance_usdm_catalog(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=reordered)))
    )
    assert sorted(row.raw_metadata_sha256 for row in rows) == sorted(row.raw_metadata_sha256 for row in reordered_rows)


def test_binance_catalog_keeps_non_ascii_assets_as_explicit_capability_exclusions() -> None:
    payload = {
        "symbols": [
            {
                "symbol": symbol,
                "pair": symbol,
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "baseAsset": base,
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
            for base, symbol in (
                ("BTC", "BTCUSDT"),
                ("币安人生", "币安人生USDT"),
                ("我踏马来了", "我踏马来了USDT"),
                ("牛来", "牛来USDT"),
                ("龙虾", "龙虾USDT"),
            )
        ]
    }

    rows = asyncio.run(
        fetch_binance_usdm_catalog(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))
    )
    catalog = build_venue_catalog_snapshot(
        binding="BINANCE_USDM",
        captured_at_ms=1_000,
        stale_after_ms=21_600_000,
        instruments=rows,
    )
    evidence = tuple(
        ExecutionInstrumentEvidenceV1(
            provider_instrument_id=row.provider_instrument_id,
            catalog_raw_metadata_sha256=row.raw_metadata_sha256,
            instrument_id=f"{row.provider_symbol}-PERP.BINANCE",
            native_symbol=row.provider_symbol,
            price_precision=4,
            size_precision=0,
            price_increment="0.0001",
            size_increment="1",
            min_quantity="1",
            min_notional="5",
            execution_eligible=True,
            protection_eligible=True,
        )
        for row in rows
    )

    snapshot = build_execution_capability_snapshot(
        catalog=catalog,
        execution_rows=evidence,
        app_revision="revision",
        app_image_digest="sha256:image",
        adapter_contract_sha256="1" * 64,
        quote_contract_sha256="2" * 64,
        protection_contract_sha256="3" * 64,
        client_runtime_identity="client-runtime",
    )

    assert [row.provider_instrument_id for row in rows] == [
        "BTCUSDT",
        "币安人生USDT",
        "我踏马来了USDT",
        "牛来USDT",
        "龙虾USDT",
    ]
    assert [row.canonical_asset for row in rows] == ["BTC", None, None, None, None]
    assert [row.normalization_error for row in rows] == [
        None,
        "canonical_asset_invalid",
        "canonical_asset_invalid",
        "canonical_asset_invalid",
        "canonical_asset_invalid",
    ]
    assert snapshot.catalog_instrument_count == snapshot.included_count + snapshot.excluded_count == 5
    assert [row.provider_instrument_id for row in snapshot.included.values()] == ["BTCUSDT"]
    assert {row.provider_instrument_id: row.reason for row in snapshot.excluded.values()} == {
        "币安人生USDT": "CATALOG_NORMALIZATION_FAILED",
        "我踏马来了USDT": "CATALOG_NORMALIZATION_FAILED",
        "牛来USDT": "CATALOG_NORMALIZATION_FAILED",
        "龙虾USDT": "CATALOG_NORMALIZATION_FAILED",
    }


def test_hyperliquid_catalog_preserves_dex_namespace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body == {"type": "perpDexs"}:
            return httpx.Response(200, json=[None, {"name": "xyz"}])
        if body == {"type": "meta", "dex": "xyz"}:
            return httpx.Response(200, json={"universe": [{"name": "XYZ-CL", "szDecimals": 2}]})
        return httpx.Response(200, json={"universe": [{"name": "BTC", "szDecimals": 5}]})

    rows = asyncio.run(fetch_hyperliquid_perp_catalog(transport=httpx.MockTransport(handler)))

    assert len(rows) == 2
    assert rows[0].canonical_namespace == "main"
    assert rows[1].canonical_namespace == "dex:xyz"
    assert rows[1].provider_instrument_id == "dex:xyz:XYZ-CL"


def test_hyperliquid_catalog_enforces_one_deadline_across_its_request_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.006)
        body = json.loads(request.content)
        if body == {"type": "perpDexs"}:
            return httpx.Response(200, json=[None, {"name": "xyz"}])
        return httpx.Response(200, json={"universe": []})

    monkeypatch.setattr(trading_catalog, "_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(VenueExpectedError, match="venue_timeout"):
        asyncio.run(fetch_hyperliquid_perp_catalog(transport=httpx.MockTransport(handler)))
