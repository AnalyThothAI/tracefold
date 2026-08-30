"""Provider-native funding-ledger reconciliation for one closed TradeIntent lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx
import msgspec
from nautilus_trader.adapters.binance.futures.execution import BinanceFuturesExecutionClient
from nautilus_trader.adapters.hyperliquid.execution import HyperliquidExecutionClient
from nautilus_trader.core.nautilus_pyo3 import HttpMethod

_HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
_HTTP_TIMEOUT_SECONDS = 10.0


async def load_funding_cashflows(
    client: Any,
    *,
    provider_instrument_id: str,
    opened_at_ms: int,
    verified_at_ms: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    """Return complete signed funding cash flows, or raise rather than invent known-zero.

    Values follow provider cash-flow signs: negative means paid, positive means received.  An empty
    mapping is therefore meaningful only after a successful, bounded, complete ledger query.
    """

    if opened_at_ms < 0 or verified_at_ms < opened_at_ms:
        raise RuntimeError("nautilus_funding_window_invalid")
    if isinstance(client, BinanceFuturesExecutionClient):
        return await _binance_funding_cashflows(
            client,
            symbol=provider_instrument_id,
            opened_at_ms=opened_at_ms,
            verified_at_ms=verified_at_ms,
        )
    if isinstance(client, HyperliquidExecutionClient):
        return await _hyperliquid_funding_cashflows(
            client,
            coin=_hyperliquid_coin(provider_instrument_id),
            opened_at_ms=opened_at_ms,
            verified_at_ms=verified_at_ms,
            transport=transport,
        )
    raise RuntimeError("nautilus_funding_client_unsupported")


async def _binance_funding_cashflows(
    client: BinanceFuturesExecutionClient,
    *,
    symbol: str,
    opened_at_ms: int,
    verified_at_ms: int,
) -> dict[str, str]:
    raw = await client._http_client.sign_request(
        http_method=HttpMethod.GET,
        url_path="/fapi/v1/income",
        payload={
            "symbol": symbol,
            "incomeType": "FUNDING_FEE",
            "startTime": str(opened_at_ms),
            "endTime": str(verified_at_ms),
            "limit": "1000",
            "recvWindow": "5000",
            "timestamp": str(int(client._clock.timestamp_ms())),
        },
        ratelimiter_keys=["binance:/fapi/v1/income", "binance:global"],
    )
    payload = msgspec.json.decode(raw)
    if not isinstance(payload, Sequence) or isinstance(payload, str | bytes) or len(payload) >= 1000:
        raise RuntimeError("nautilus_binance_funding_coverage_unproven")
    totals: dict[str, Decimal] = {}
    identities: set[tuple[str, str, int]] = set()
    for value in payload:
        if not isinstance(value, Mapping):
            raise RuntimeError("nautilus_binance_funding_payload_invalid")
        asset = str(value.get("asset") or "")
        income_type = str(value.get("incomeType") or "")
        row_symbol = str(value.get("symbol") or "")
        time_ms = _integer(value.get("time"), "nautilus_binance_funding_time_invalid")
        identity = (str(value.get("tranId") or ""), str(value.get("tradeId") or ""), time_ms)
        if (
            not asset
            or income_type != "FUNDING_FEE"
            or row_symbol != symbol
            or time_ms < opened_at_ms
            or time_ms > verified_at_ms
            or not identity[0]
            or identity in identities
        ):
            raise RuntimeError("nautilus_binance_funding_payload_invalid")
        identities.add(identity)
        totals[asset] = totals.get(asset, Decimal(0)) + _decimal(
            value.get("income"),
            "nautilus_binance_funding_amount_invalid",
        )
    return _serialize_totals(totals)


async def _hyperliquid_funding_cashflows(
    client: HyperliquidExecutionClient,
    *,
    coin: str,
    opened_at_ms: int,
    verified_at_ms: int,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, str]:
    address = str(client._account_address or "")
    if not address:
        raise RuntimeError("nautilus_hyperliquid_funding_account_missing")
    proxy = None if transport is not None else client._config.proxy_url
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
        follow_redirects=False,
        transport=transport,
        proxy=proxy,
    ) as http:
        response = await http.post(
            _HYPERLIQUID_INFO_URL,
            json={
                "type": "userFunding",
                "user": address,
                "startTime": opened_at_ms,
                "endTime": verified_at_ms,
            },
        )
        response.raise_for_status()
        payload = response.json()
    # The official range endpoint caps responses at 500.  This lifecycle holds one instrument for
    # hours, so hitting the cap is an impossible-to-prove truncation, not a pagination invitation.
    if not isinstance(payload, list) or len(payload) >= 500:
        raise RuntimeError("nautilus_hyperliquid_funding_coverage_unproven")
    total = Decimal(0)
    matched = 0
    identities: set[tuple[int, str]] = set()
    for value in payload:
        if not isinstance(value, Mapping):
            raise RuntimeError("nautilus_hyperliquid_funding_payload_invalid")
        time_ms = _integer(value.get("time"), "nautilus_hyperliquid_funding_time_invalid")
        tx_hash = str(value.get("hash") or "")
        delta = value.get("delta")
        identity = (time_ms, tx_hash)
        if (
            not isinstance(delta, Mapping)
            or str(delta.get("type") or "") != "funding"
            or time_ms < opened_at_ms
            or time_ms > verified_at_ms
            or not tx_hash
            or identity in identities
        ):
            raise RuntimeError("nautilus_hyperliquid_funding_payload_invalid")
        identities.add(identity)
        if str(delta.get("coin") or "") != coin:
            continue
        matched += 1
        total += _decimal(delta.get("usdc"), "nautilus_hyperliquid_funding_amount_invalid")
    return {} if matched == 0 else {"USDC": _decimal_text(total)}


def _hyperliquid_coin(provider_instrument_id: str) -> str:
    parts = provider_instrument_id.split(":")
    if len(parts) == 2 and parts[0] == "main" and parts[1]:
        return parts[1]
    if len(parts) == 3 and parts[0] == "dex" and parts[1] and parts[2]:
        dex, symbol = parts[1], parts[2]
        prefix = f"{dex.upper()}-"
        return f"{dex}:{symbol.removeprefix(prefix)}"
    raise RuntimeError("nautilus_hyperliquid_funding_instrument_invalid")


def _integer(value: object, error: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(error)
    try:
        result = int(cast(Any, value))
    except (TypeError, ValueError):
        raise RuntimeError(error) from None
    return result


def _decimal(value: object, error: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RuntimeError(error) from None
    if not result.is_finite():
        raise RuntimeError(error)
    return result


def _decimal_text(value: Decimal) -> str:
    return "0" if value == 0 else format(value, "f")


def _serialize_totals(totals: Mapping[str, Decimal]) -> dict[str, str]:
    return {asset: _decimal_text(amount) for asset, amount in sorted(totals.items())}


__all__ = ["load_funding_cashflows"]
