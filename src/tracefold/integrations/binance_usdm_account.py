"""Read-only Binance USD-M Demo provider-account identity boundary."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from tracefold.trading import trading_provider_account_fingerprint

BINANCE_USDM_DEMO_BASE_URL: Final = "https://demo-fapi.binance.com"
_TIMEOUT_SECONDS: Final = 6.5
_RECV_WINDOW_MS: Final = 5_000
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{3,256}$")


class BinanceUsdmAccountIdentityError(RuntimeError):
    """Sanitized failure from the read-only provider identity probe."""


def binance_usdm_provider_account_fingerprint(balances: Sequence[Any]) -> str:
    aliases: set[str] = set()
    for row in balances:
        alias = row.get("accountAlias") if isinstance(row, Mapping) else None
        if not isinstance(alias, str) or not alias.strip():
            raise BinanceUsdmAccountIdentityError("binance_usdm_account_identity_invalid")
        aliases.add(alias.strip())
    if len(aliases) != 1:
        raise BinanceUsdmAccountIdentityError("binance_usdm_account_identity_invalid")
    return trading_provider_account_fingerprint(
        venue="binance_usdm_demo",
        provider_account_id=aliases.pop(),
    )


class BinanceUsdmAccountIdentityClient:
    """Minimal signed reader shared by the separate Manual and Auto roots."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        transport: httpx.BaseTransport | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        normalized_key = str(api_key or "").strip()
        normalized_secret = str(api_secret or "").strip()
        if _API_KEY_RE.fullmatch(normalized_key) is None or not 3 <= len(normalized_secret) <= 512:
            raise ValueError("binance_usdm_account_identity_credentials_invalid")
        self._api_secret = normalized_secret.encode()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._client = httpx.Client(
            base_url=BINANCE_USDM_DEMO_BASE_URL,
            timeout=httpx.Timeout(_TIMEOUT_SECONDS),
            follow_redirects=False,
            headers={"Accept": "application/json", "X-MBX-APIKEY": normalized_key},
            transport=transport,
        )

    def provider_account_fingerprint(self) -> str:
        unsigned = urlencode(
            {
                "recvWindow": str(_RECV_WINDOW_MS),
                "timestamp": str(int(self._clock_ms())),
            }
        )
        signature = hmac.new(self._api_secret, unsigned.encode(), hashlib.sha256).hexdigest()
        try:
            response = self._client.get("/fapi/v3/balance", params=f"{unsigned}&signature={signature}")
        except (httpx.TimeoutException, httpx.TransportError):
            raise BinanceUsdmAccountIdentityError("binance_usdm_account_identity_transport_failed") from None
        if response.status_code != 200:
            raise BinanceUsdmAccountIdentityError("binance_usdm_account_identity_provider_rejected")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, str | bytes) or not isinstance(payload, Sequence):
            raise BinanceUsdmAccountIdentityError("binance_usdm_account_identity_response_invalid")
        return binance_usdm_provider_account_fingerprint(payload)

    def close(self) -> None:
        self._client.close()


__all__ = [
    "BINANCE_USDM_DEMO_BASE_URL",
    "BinanceUsdmAccountIdentityClient",
    "BinanceUsdmAccountIdentityError",
    "binance_usdm_provider_account_fingerprint",
]
