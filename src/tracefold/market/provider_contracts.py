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


class CexMarketProvider(Protocol):
    def tickers(self, *, inst_type: str) -> list[CexTicker]: ...

    def ticker(self, *, inst_id: str) -> CexTicker | None: ...

    def close(self) -> None: ...


class DexTokenQuoteProvider(Protocol):
    def token_quotes(self, tokens: list[DexTokenQuoteRequest]) -> list[DexTokenQuote]: ...

    def close(self) -> None: ...


class AssetMarketProviderBundle(Protocol):
    cex_market: CexMarketProvider | None
    dex_quote_market: DexTokenQuoteProvider | None


__all__ = [
    "AssetMarketProviderBundle",
    "CexMarketProvider",
    "CexTicker",
    "DexProviderTemporarilyUnavailable",
    "DexTokenQuote",
    "DexTokenQuoteProvider",
    "DexTokenQuoteRequest",
    "MarketProviderExpectedError",
]
