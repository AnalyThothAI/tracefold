from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class MarketCapability(StrEnum):
    QUOTE_CEX = "quote_cex"
    QUOTE_DEX_EXACT = "quote_dex_exact"
    STREAM_DEX = "stream_dex"
    SEARCH_DEX = "search_dex"
    PROFILE_CEX = "profile_cex"
    PROFILE_DEX_EXACT = "profile_dex_exact"


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    capabilities: frozenset[MarketCapability]
    configured: bool
    last_error: str | None = None


class MarketProviderExpectedError(RuntimeError):
    """A declared finite provider failure safe for domain persistence."""


class DexProviderTemporarilyUnavailable(MarketProviderExpectedError):
    pass


class MarketStreamExpectedError(RuntimeError):
    """A declared reconnectable stream transport/protocol failure."""


@dataclass(frozen=True, slots=True)
class CexTicker:
    inst_id: str
    inst_type: str
    last_price: float | None
    volume_24h: float | None
    open_interest: float | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DexTokenCandidate:
    chain_id: str
    address: str
    symbol: str
    name: str | None
    price_usd: float | None
    market_cap_usd: float | None
    liquidity_usd: float | None
    holders: int | None
    community_recognized: bool | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DexTokenQuote:
    chain_id: str
    address: str
    observed_at_ms: int
    price_usd: float | None
    raw: dict[str, Any]
    market_cap_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    holders: int | None = None


@dataclass(frozen=True, slots=True)
class DexTokenQuoteRequest:
    chain_id: str
    address: str


@dataclass(frozen=True, slots=True)
class DexTokenProfile:
    chain_id: str
    address: str
    symbol: str | None
    name: str | None
    logo_url: str | None
    banner_url: str | None
    website: str | None
    twitter_username: str | None
    telegram: str | None
    gmgn_url: str | None
    geckoterminal_url: str | None
    description: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DexProfileSource:
    provider: str
    market: DexTokenProfileProvider


@dataclass(frozen=True, slots=True)
class DexMarketStreamTarget:
    chain_id: str
    address: str
    subject_type: str
    subject_id: str
    pricefeed_id: str | None = None


@dataclass(frozen=True, slots=True)
class DexMarketFactUpdate:
    chain_id: str
    address: str
    observed_at_ms: int
    price_usd: float | None = None
    market_cap_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    open_interest_usd: float | None = None
    holders: int | None = None
    raw: dict[str, Any] | None = None


class CexMarketProvider(Protocol):
    def tickers(self, *, inst_type: str) -> list[CexTicker]: ...

    def ticker(self, *, inst_id: str) -> CexTicker | None: ...

    def close(self) -> None: ...


class DexTokenDiscoveryProvider(Protocol):
    def search_tokens(self, *, query: str, chain_ids: tuple[str, ...]) -> list[DexTokenCandidate]: ...

    def close(self) -> None: ...


class DexTokenQuoteProvider(Protocol):
    def token_quotes(self, tokens: list[DexTokenQuoteRequest]) -> list[DexTokenQuote]: ...

    def close(self) -> None: ...


class DexTokenProfileProvider(Protocol):
    def token_profile(self, *, chain_id: str, address: str) -> DexTokenProfile | None: ...

    def close(self) -> None: ...


class DexMarketStreamProvider(Protocol):
    async def replace_subscriptions(self, targets: list[DexMarketStreamTarget]) -> None: ...

    def iter_price_info(self) -> AsyncIterator[DexMarketFactUpdate]: ...

    def connection_state_payload(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class AssetMarketProviderBundle(Protocol):
    cex_market: CexMarketProvider | None
    dex_quote_market: DexTokenQuoteProvider | None


__all__ = [
    "AssetMarketProviderBundle",
    "CexMarketProvider",
    "CexTicker",
    "DexMarketFactUpdate",
    "DexMarketStreamProvider",
    "DexMarketStreamTarget",
    "DexProfileSource",
    "DexProviderTemporarilyUnavailable",
    "DexTokenCandidate",
    "DexTokenDiscoveryProvider",
    "DexTokenProfile",
    "DexTokenProfileProvider",
    "DexTokenQuote",
    "DexTokenQuoteProvider",
    "DexTokenQuoteRequest",
    "MarketCapability",
    "MarketProviderExpectedError",
    "MarketStreamExpectedError",
    "ProviderHealth",
]
