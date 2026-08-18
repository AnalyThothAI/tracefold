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

# The public GMGN OpenAPI is paced at 1 request per second per key (docs.gmgn.ai: "The default
# rate limit is 1 request per second"; violations get the IP banned), and the token-info route is per
# token. A poll batch therefore quotes sequentially in the caller's order until the deadline and returns
# the finished part; the caller orders targets stalest-first so successive batches rotate the set.
QUOTE_BATCH_DEADLINE_SECONDS = 25.0


class GmgnDexMarketProvider:
    """DEX quotes from the GMGN OpenAPI token-info route (one request per token, 1 req/s pacing).

    One token's failure never fails the batch: unknown tokens and per-token provider errors are simply
    absent from the result, tokens not reached before the batch deadline are left for the next batch,
    and only a batch with zero successful lookups raises ``DexProviderTemporarilyUnavailable``
    (typically an open circuit after a rate-limit ban).
    """

    def __init__(
        self,
        gateway: GmgnOpenApiGateway,
        *,
        batch_deadline_seconds: float = QUOTE_BATCH_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gateway = gateway
        self._batch_deadline_seconds = float(batch_deadline_seconds)
        self._clock = clock

    def token_quotes(self, tokens: list[DexTokenQuoteRequest]) -> list[DexTokenQuote]:
        requests = list(tokens)
        if not requests:
            return []
        observed_at_ms = int(time.time() * 1000)
        deadline = self._clock() + self._batch_deadline_seconds
        quotes: list[DexTokenQuote] = []
        succeeded = 0
        first_error: GmgnOpenApiError | None = None
        for index, token in enumerate(requests):
            if index > 0 and self._clock() >= deadline:
                break
            try:
                lookup = self._gateway.lookup_token_info(chain=token.chain_id, address=token.address)
            except GmgnOpenApiError as exc:
                first_error = first_error or exc
                continue
            succeeded += 1
            quote = _quote_from_lookup(lookup, observed_at_ms=observed_at_ms)
            if quote is not None:
                quotes.append(quote)
        if succeeded == 0 and first_error is not None:
            raise DexProviderTemporarilyUnavailable(str(first_error)) from first_error
        return quotes

    def close(self) -> None:
        self._gateway.close()


def _quote_from_lookup(lookup: GmgnTokenInfoLookup, *, observed_at_ms: int) -> DexTokenQuote | None:
    info = lookup.info
    if info is None:
        return None
    raw = {**info.raw, "cache_status": lookup.cache_status, "source_provider": "gmgn_dex_quote"}
    raw_price_payload = raw.get("price")
    price_payload: dict[str, Any] = raw_price_payload if isinstance(raw_price_payload, dict) else {}
    return DexTokenQuote(
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
