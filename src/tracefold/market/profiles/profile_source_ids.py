from __future__ import annotations

GMGN_DEX_PROFILE_PROVIDER = "gmgn_dex_profile"
BINANCE_WEB3_PROFILE_PROVIDER = "binance_web3_profile"
GMGN_STREAM_PROFILE_PROVIDER = "gmgn_stream_snapshot"
OKX_DEX_PROFILE_PROVIDER = "okx_dex_evidence"
BINANCE_CEX_PROFILE_PROVIDER = "binance_cex_profile"

ASSET_PROFILE_REFRESH_PROVIDERS = frozenset(
    {
        GMGN_DEX_PROFILE_PROVIDER,
        BINANCE_WEB3_PROFILE_PROVIDER,
    }
)
INACTIVE_PROFILE_TARGET_DELETE_BATCH = 500


def inactive_asset_profile_provider_ids(active_providers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(ASSET_PROFILE_REFRESH_PROVIDERS - set(active_providers)))


__all__ = [
    "ASSET_PROFILE_REFRESH_PROVIDERS",
    "BINANCE_CEX_PROFILE_PROVIDER",
    "BINANCE_WEB3_PROFILE_PROVIDER",
    "GMGN_DEX_PROFILE_PROVIDER",
    "GMGN_STREAM_PROFILE_PROVIDER",
    "INACTIVE_PROFILE_TARGET_DELETE_BATCH",
    "OKX_DEX_PROFILE_PROVIDER",
    "inactive_asset_profile_provider_ids",
]
