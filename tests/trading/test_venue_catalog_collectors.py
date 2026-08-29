from __future__ import annotations

import asyncio
import json

import httpx

from tracefold.integrations.trading_catalog import fetch_binance_usdm_catalog, fetch_hyperliquid_perp_catalog


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
