from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from tracefold.integrations.gmgn.direct_ws import DirectGmgnWebSocketClient
from tracefold.integrations.gmgn.openapi_client import (
    GmgnOpenApiClient,
    GmgnOpenApiError,
    GmgnTokenInfoLookup,
)
from tracefold.integrations.gmgn.openapi_gateway import GmgnOpenApiGateway
from tracefold.market import (
    DexProviderTemporarilyUnavailable,
    DexTokenQuote,
    DexTokenQuoteRequest,
    UpstreamClientProtocol,
    canonical_chain_address,
)
from tracefold.platform.config.settings import Settings


class GmgnDexMarketProvider:
    def __init__(self, gateway: GmgnOpenApiGateway) -> None:
        self._gateway = gateway

    def token_quotes(self, tokens: list[DexTokenQuoteRequest]) -> list[DexTokenQuote]:
        observed_at_ms = int(time.time() * 1000)
        quotes: list[DexTokenQuote] = []
        for token in tokens:
            lookup = self._lookup_token_info(chain_id=token.chain_id, address=token.address)
            info = lookup.info
            if info is None:
                continue
            raw = {**info.raw, "cache_status": lookup.cache_status, "source_provider": "gmgn_dex_quote"}
            raw_price_payload = raw.get("price")
            price_payload: dict[str, Any] = raw_price_payload if isinstance(raw_price_payload, dict) else {}
            quotes.append(
                DexTokenQuote(
                    chain_id=info.chain,
                    address=canonical_chain_address(info.chain, info.address),
                    observed_at_ms=observed_at_ms,
                    price_usd=info.price,
                    raw=raw,
                    market_cap_usd=info.market_cap,
                    liquidity_usd=info.liquidity,
                    volume_24h_usd=_number_from_mapping(
                        {**price_payload, **info.raw},
                        "volume_24h_usd",
                        "volume24hUsd",
                        "volume_24h",
                    ),
                    holders=info.holder_count,
                )
            )
        return quotes

    def _lookup_token_info(self, *, chain_id: str, address: str) -> GmgnTokenInfoLookup:
        try:
            return self._gateway.lookup_token_info(chain=chain_id, address=address)
        except GmgnOpenApiError as exc:
            raise DexProviderTemporarilyUnavailable(str(exc)) from exc

    def close(self) -> None:
        self._gateway.close()


def gmgn_dex_market(settings: Settings) -> GmgnDexMarketProvider:
    return GmgnDexMarketProvider(
        GmgnOpenApiGateway(
            GmgnOpenApiClient(
                api_key=settings.gmgn.api_key or "",
                base_url=settings.gmgn.openapi_base_url,
                timeout_seconds=settings.gmgn.timeout_seconds,
            ),
            token_info_cache_ttl_seconds=settings.gmgn.token_info_cache_ttl_seconds,
        )
    )


def gmgn_upstream_client(
    settings: Settings,
    *,
    on_frame: Callable[[str], Awaitable[None]],
) -> UpstreamClientProtocol:
    return DirectGmgnWebSocketClient(
        app_version=settings.upstream.app_version,
        channels=list(settings.upstream.channels),
        chains=list(settings.upstream.chains),
        proxy=settings.upstream.proxy,
        reconnect_delay=settings.upstream.reconnect_delay,
        heartbeat_interval=settings.upstream.heartbeat_interval,
        idle_timeout=settings.upstream.idle_timeout,
        on_frame=on_frame,
    )


def _number_from_mapping(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


__all__ = [
    "GmgnDexMarketProvider",
    "gmgn_dex_market",
    "gmgn_upstream_client",
]
