from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from tracefold.integrations.binance import providers as binance
from tracefold.integrations.gmgn import providers as gmgn
from tracefold.market import CexMarketProvider, DexTokenQuoteProvider
from tracefold.platform.config.settings import Settings


class _SyncCloseProvider(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AssetMarketProviders:
    """The two market data providers: GMGN OpenAPI for DEX quotes, Binance USD-M futures for CEX ticks."""

    cex_market: CexMarketProvider | None = None
    dex_quote_market: DexTokenQuoteProvider | None = None


def wire_asset_market(settings: Settings) -> AssetMarketProviders:
    binance_cex_market: CexMarketProvider | None = None
    gmgn_dex_market: DexTokenQuoteProvider | None = None
    try:
        if settings.providers.binance.enabled:
            binance_cex_market = binance.binance_usdm_futures_market(settings)
        if settings.gmgn_configured:
            gmgn_dex_market = gmgn.gmgn_dex_market(settings)
        return AssetMarketProviders(cex_market=binance_cex_market, dex_quote_market=gmgn_dex_market)
    except Exception as exc:
        _close_partial_providers(exc, binance_cex_market, gmgn_dex_market)
        raise


def _close_partial_providers(error: BaseException, *providers: object | None) -> None:
    seen: set[int] = set()
    for provider in providers:
        if provider is None or id(provider) in seen:
            continue
        seen.add(id(provider))
        try:
            cast(_SyncCloseProvider, provider).close()
        except Exception as exc:
            error.add_note(f"partial provider cleanup failed: {type(exc).__name__}: {exc}")


__all__ = ["AssetMarketProviders", "wire_asset_market"]
