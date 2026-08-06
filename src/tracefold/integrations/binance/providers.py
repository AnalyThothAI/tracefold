from __future__ import annotations

import httpx

from tracefold.integrations.binance.usdm_futures_client import (
    BinanceUsdmFuturesClient,
    BinanceUsdmFuturesClientError,
    BinanceUsdmTicker24hr,
)
from tracefold.integrations.binance.web3_token_client import BinanceWeb3TokenClient
from tracefold.market import (
    CexTicker,
    DexTokenProfile,
    MarketProviderExpectedError,
    canonical_chain_address,
)
from tracefold.platform.config.settings import Settings


class BinanceWeb3DexProfileProvider:
    def __init__(self, client: BinanceWeb3TokenClient) -> None:
        self._client = client

    def token_profile(self, *, chain_id: str, address: str) -> DexTokenProfile | None:
        try:
            metadata = self._client.token_metadata(chain_id=chain_id, address=address)
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketProviderExpectedError(f"binance_web3_profile_expected:{type(exc).__name__}") from exc
        if metadata is None:
            return None
        return DexTokenProfile(
            chain_id=metadata.chain_id,
            address=canonical_chain_address(metadata.chain_id, metadata.address),
            symbol=metadata.symbol,
            name=metadata.name,
            logo_url=metadata.logo_url,
            banner_url=None,
            website=metadata.website,
            twitter_username=metadata.twitter_username,
            telegram=metadata.telegram,
            gmgn_url=None,
            geckoterminal_url=None,
            description=metadata.description,
            raw=metadata.raw,
        )

    def close(self) -> None:
        self._client.close()


class BinanceUsdmFuturesMarketProvider:
    def __init__(self, client: BinanceUsdmFuturesClient) -> None:
        self._client = client

    def tickers(self, *, inst_type: str) -> list[CexTicker]:
        if str(inst_type or "").strip().upper() not in {"SWAP", "PERP", "PERPETUAL"}:
            return []
        try:
            tickers = self._client.ticker_24hr()
        except BinanceUsdmFuturesClientError as exc:
            raise MarketProviderExpectedError(str(exc)) from exc
        rows = tickers if isinstance(tickers, list) else [tickers]
        return [_cex_ticker(row) for row in rows]

    def ticker(self, *, inst_id: str) -> CexTicker | None:
        try:
            ticker = self._client.ticker_24hr(symbol=inst_id)
        except BinanceUsdmFuturesClientError as exc:
            raise MarketProviderExpectedError(str(exc)) from exc
        if isinstance(ticker, list):
            return _cex_ticker(ticker[0]) if ticker else None
        return _cex_ticker(ticker)

    def close(self) -> None:
        self._client.close()


def binance_web3_profile_market(settings: Settings) -> BinanceWeb3DexProfileProvider:
    return BinanceWeb3DexProfileProvider(
        BinanceWeb3TokenClient(
            base_url=settings.providers.binance.web3_base_url,
            timeout_seconds=settings.providers.binance.timeout_seconds,
        )
    )


def binance_usdm_futures_market(settings: Settings) -> BinanceUsdmFuturesMarketProvider:
    return BinanceUsdmFuturesMarketProvider(
        BinanceUsdmFuturesClient(
            base_url=settings.providers.binance.usdm_futures_base_url,
            timeout_seconds=min(float(settings.providers.binance.timeout_seconds), 8.0),
        )
    )


def _cex_ticker(ticker: BinanceUsdmTicker24hr) -> CexTicker:
    return CexTicker(
        inst_id=ticker.symbol,
        inst_type="SWAP",
        last_price=ticker.last_price,
        volume_24h=ticker.quote_volume_24h,
        open_interest=None,
        raw=ticker.raw,
    )


__all__ = [
    "BinanceUsdmFuturesMarketProvider",
    "BinanceWeb3DexProfileProvider",
    "binance_usdm_futures_market",
    "binance_web3_profile_market",
]
