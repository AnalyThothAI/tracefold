from __future__ import annotations

from threading import Lock
from typing import Any

from tracefold.integrations.okx.chains import OKX_CHAIN_INDEX_TO_CHAIN, OKX_CHAIN_TO_CHAIN_INDEX
from tracefold.integrations.okx.dex_client import OkxDexClient
from tracefold.integrations.okx.http_utils import OkxClientError
from tracefold.market import (
    DexProviderTemporarilyUnavailable,
    DexTokenCandidate,
    DexTokenDiscoveryProvider,
    DexTokenQuote,
    DexTokenQuoteRequest,
    canonical_chain_address,
    canonical_chain_id,
)
from tracefold.platform.config.settings import Settings


class OkxDexDiscoveryProvider:
    def __init__(self, client: OkxDexClient) -> None:
        self._client = client

    def search_tokens(self, *, query: str, chain_ids: tuple[str, ...]) -> list[DexTokenCandidate]:
        chain_indexes = tuple(index for chain_id in chain_ids if (index := okx_chain_index(chain_id)))
        if not chain_indexes:
            return []
        try:
            candidates = self._client.search_tokens(query=query, chain_indexes=chain_indexes)
        except OkxClientError as exc:
            raise DexProviderTemporarilyUnavailable(str(exc)) from exc
        return [_dex_token_candidate(candidate) for candidate in candidates]

    def close(self) -> None:
        self._client.close()


class OkxDexQuoteProvider:
    def __init__(self, client: OkxDexClient) -> None:
        self._client = client

    def token_quotes(self, tokens: list[DexTokenQuoteRequest]) -> list[DexTokenQuote]:
        request_items: list[dict[str, str]] = []
        for token in tokens:
            chain_index = okx_chain_index(token.chain_id)
            if not chain_index:
                continue
            request_items.append(
                {
                    "chainIndex": chain_index,
                    "tokenContractAddress": canonical_chain_address(token.chain_id, token.address),
                }
            )
        if not request_items:
            return []
        try:
            prices = self._client.token_prices(request_items)
        except OkxClientError as exc:
            raise DexProviderTemporarilyUnavailable(str(exc)) from exc
        return [_dex_token_quote(price) for price in prices]

    def close(self) -> None:
        self._client.close()


class SerializedDiscoveryProvider:
    def __init__(self, provider: DexTokenDiscoveryProvider) -> None:
        self._provider = provider
        self._lock = Lock()
        self._closed = False

    def search_tokens(self, *, query: str, chain_ids: tuple[str, ...]) -> list[DexTokenCandidate]:
        with self._lock:
            return self._provider.search_tokens(query=query, chain_ids=chain_ids)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._provider.close()
            self._closed = True


def okx_dex_discovery_market(settings: Settings) -> OkxDexDiscoveryProvider:
    return OkxDexDiscoveryProvider(
        OkxDexClient(
            base_url=settings.providers.okx.dex_base_url,
            api_key=settings.providers.okx.dex_api_key,
            secret_key=settings.providers.okx.dex_secret_key,
            passphrase=settings.providers.okx.dex_passphrase,
            timeout_seconds=min(float(settings.providers.okx.timeout_seconds), 8.0),
        )
    )


def okx_dex_quote_market(settings: Settings) -> OkxDexQuoteProvider:
    return OkxDexQuoteProvider(
        OkxDexClient(
            base_url=settings.providers.okx.dex_base_url,
            api_key=settings.providers.okx.dex_api_key,
            secret_key=settings.providers.okx.dex_secret_key,
            passphrase=settings.providers.okx.dex_passphrase,
            timeout_seconds=min(float(settings.providers.okx.timeout_seconds), 8.0),
        )
    )


def okx_chain_indexes_to_chain_ids(chain_indexes: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    chain_ids = []
    for value in chain_indexes:
        chain_id = okx_index_to_chain_id(str(value))
        if chain_id:
            chain_ids.append(chain_id)
    return tuple(dict.fromkeys(chain_ids))


def okx_index_to_chain_id(value: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized.startswith("eip155:"):
        return normalized
    chain = OKX_CHAIN_INDEX_TO_CHAIN.get(normalized, normalized)
    return canonical_chain_id(chain)


def okx_chain_index(chain_id: Any) -> str | None:
    normalized = str(chain_id or "").strip().lower()
    if not normalized:
        return None
    if normalized.startswith("eip155:"):
        return normalized.split(":", 1)[1]
    if normalized.isdecimal():
        return normalized
    return OKX_CHAIN_TO_CHAIN_INDEX.get(normalized)


def _dex_token_candidate(candidate: Any) -> DexTokenCandidate:
    chain_id = okx_index_to_chain_id(candidate.chain_index) or str(candidate.chain_index)
    return DexTokenCandidate(
        chain_id=chain_id,
        address=canonical_chain_address(chain_id, candidate.address),
        symbol=candidate.symbol,
        name=candidate.name,
        price_usd=candidate.price_usd,
        market_cap_usd=candidate.market_cap_usd,
        liquidity_usd=candidate.liquidity_usd,
        holders=candidate.holders,
        community_recognized=candidate.community_recognized,
        raw=candidate.raw,
    )


def _dex_token_quote(price: Any) -> DexTokenQuote:
    chain_id = okx_index_to_chain_id(price.chain_index) or str(price.chain_index)
    return DexTokenQuote(
        chain_id=chain_id,
        address=canonical_chain_address(chain_id, price.address),
        observed_at_ms=price.observed_at_ms,
        price_usd=price.price_usd,
        raw=price.raw,
        market_cap_usd=price.market_cap_usd,
        liquidity_usd=price.liquidity_usd,
        volume_24h_usd=None,
        holders=price.holders,
    )


__all__ = [
    "OkxDexDiscoveryProvider",
    "OkxDexQuoteProvider",
    "SerializedDiscoveryProvider",
    "okx_chain_index",
    "okx_chain_indexes_to_chain_ids",
    "okx_dex_discovery_market",
    "okx_dex_quote_market",
    "okx_index_to_chain_id",
]
