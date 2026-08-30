from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qsl

import httpx

from tracefold.integrations.binance_usdm_account import BinanceUsdmAccountIdentityClient
from tracefold.trading import trading_provider_account_fingerprint


def test_read_only_identity_client_hashes_the_signed_provider_alias() -> None:
    observed: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json=[
                {"asset": "USDT", "accountAlias": "demo-account-7"},
                {"asset": "USDC", "accountAlias": "demo-account-7"},
            ],
        )

    client = BinanceUsdmAccountIdentityClient(
        api_key="key-value",
        api_secret="secret-value",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: 1_900_000_000_000,
    )
    try:
        fingerprint = client.provider_account_fingerprint()
    finally:
        client.close()

    assert fingerprint == trading_provider_account_fingerprint(
        venue="binance_usdm_demo",
        provider_account_id="demo-account-7",
    )
    request = observed[0]
    assert request.method == "GET" and request.url.path == "/fapi/v3/balance"
    pairs = parse_qsl(request.url.query.decode())
    signature = dict(pairs)["signature"]
    signed = "&".join(f"{key}={value}" for key, value in pairs if key != "signature")
    assert signature == hmac.new(b"secret-value", signed.encode(), hashlib.sha256).hexdigest()
