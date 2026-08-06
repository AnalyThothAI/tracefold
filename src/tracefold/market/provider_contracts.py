from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class MarketProviderExpectedError(RuntimeError):
    """A declared finite provider failure safe for domain persistence."""


class DexProviderTemporarilyUnavailable(MarketProviderExpectedError):
    pass


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


class AssetMarketProviderBundle(Protocol):
    cex_market: CexMarketProvider | None
    dex_quote_market: DexTokenQuoteProvider | None


__all__ = [
    "AssetMarketProviderBundle",
    "CexMarketProvider",
    "CexTicker",
    "DexProfileSource",
    "DexProviderTemporarilyUnavailable",
    "DexTokenCandidate",
    "DexTokenDiscoveryProvider",
    "DexTokenProfile",
    "DexTokenProfileProvider",
    "DexTokenQuote",
    "DexTokenQuoteProvider",
    "DexTokenQuoteRequest",
    "MarketProviderExpectedError",
]
