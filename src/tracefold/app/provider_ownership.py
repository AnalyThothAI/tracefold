from __future__ import annotations

from tracefold.market import BINANCE_WEB3_PROFILE_PROVIDER, GMGN_DEX_PROFILE_PROVIDER
from tracefold.platform.config.settings import Settings


def gmgn_stream_enabled(settings: Settings) -> bool:
    """Return whether this configuration owns the anonymous GMGN stream."""

    return bool(settings.upstream.channels)


def configured_profile_provider_ids(settings: Settings) -> tuple[str, ...]:
    """Return the profile queues owned by this configuration."""

    providers: list[str] = []
    if settings.gmgn_configured:
        providers.append(GMGN_DEX_PROFILE_PROVIDER)
    if settings.providers.binance.enabled:
        providers.append(BINANCE_WEB3_PROFILE_PROVIDER)
    return tuple(providers)


__all__ = ["configured_profile_provider_ids", "gmgn_stream_enabled"]
