from __future__ import annotations

from tracefold.app.provider_types import (
    AssetMarketProviders,
    IngestionProviders,
    WiredProviders,
)
from tracefold.platform.config.settings import Settings


def wire_providers(
    settings: Settings,
    *,
    start_collector: bool,
) -> WiredProviders:
    from tracefold.app import market_providers
    from tracefold.integrations.gmgn import providers as gmgn

    return WiredProviders(
        ingestion=IngestionProviders(
            upstream_client_factory=gmgn.gmgn_upstream_factory(settings) if start_collector else None,
        ),
        asset_market=market_providers.wire_asset_market(settings),
    )


__all__ = [
    "AssetMarketProviders",
    "IngestionProviders",
    "WiredProviders",
    "wire_providers",
]
