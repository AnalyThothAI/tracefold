from __future__ import annotations

from tracefold.integrations.binance.usdm_futures_client import (
    BinanceUsdmFuturesClient,
    BinanceUsdmFuturesClientError,
    BinanceUsdmTicker24hr,
)
from tracefold.market import CexTicker, MarketProviderExpectedError
from tracefold.platform.config.settings import Settings


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
    "binance_usdm_futures_market",
]
