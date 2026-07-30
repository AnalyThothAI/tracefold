from __future__ import annotations

from typing import Any

from tracefold.market.provider_contracts import DexProfileSource, DexTokenProfile


def fetch_asset_profile(*, profile_source: DexProfileSource, row: dict[str, Any]) -> DexTokenProfile | None:
    profile: object = profile_source.market.token_profile(
        chain_id=str(row["chain_id"]),
        address=str(row["address"]),
    )
    if isinstance(profile, DexTokenProfile):
        return profile
    return None


def write_ready_asset_profile(
    *,
    repos: Any,
    provider: str,
    row: dict[str, Any],
    profile: DexTokenProfile,
    now_ms: int,
    next_refresh_at_ms: int,
) -> None:
    _write_ready_profile(
        repos=repos,
        provider=provider,
        row=row,
        profile=profile,
        now_ms=now_ms,
        next_refresh_at_ms=next_refresh_at_ms,
    )


def write_missing_asset_profile(
    *, repos: Any, provider: str, row: dict[str, Any], now_ms: int, next_refresh_at_ms: int
) -> None:
    _write_missing_profile(
        repos=repos,
        provider=provider,
        row=row,
        now_ms=now_ms,
        next_refresh_at_ms=next_refresh_at_ms,
    )


def write_error_asset_profile(
    *, repos: Any, provider: str, row: dict[str, Any], exc: Exception, now_ms: int, next_refresh_at_ms: int
) -> None:
    _write_error_profile(
        repos=repos,
        provider=provider,
        row=row,
        exc=exc,
        now_ms=now_ms,
        next_refresh_at_ms=next_refresh_at_ms,
    )


def write_unsupported_asset_profile(
    *,
    repos: Any,
    provider: str,
    row: dict[str, Any],
    exc: Exception,
    now_ms: int,
) -> None:
    repos.asset_profiles.upsert_status(
        asset_id=str(row["target_id"]),
        provider=provider,
        status="unsupported",
        observed_at_ms=int(now_ms),
        next_refresh_at_ms=int(now_ms),
        last_error=str(exc)[:500],
    )


def _write_ready_profile(
    *,
    repos: Any,
    provider: str,
    row: dict[str, Any],
    profile: DexTokenProfile,
    now_ms: int,
    next_refresh_at_ms: int,
) -> None:
    repos.asset_profiles.upsert_ready_profile(
        asset_id=str(row["target_id"]),
        provider=provider,
        symbol=profile.symbol,
        name=profile.name,
        logo_url=profile.logo_url,
        banner_url=profile.banner_url,
        website_url=profile.website,
        twitter_username=profile.twitter_username,
        twitter_url=None,
        telegram_url=profile.telegram,
        gmgn_url=profile.gmgn_url,
        geckoterminal_url=profile.geckoterminal_url,
        description=profile.description,
        raw_payload=profile.raw,
        observed_at_ms=int(now_ms),
        next_refresh_at_ms=int(next_refresh_at_ms),
    )


def _write_missing_profile(
    *, repos: Any, provider: str, row: dict[str, Any], now_ms: int, next_refresh_at_ms: int
) -> None:
    repos.asset_profiles.upsert_status(
        asset_id=str(row["target_id"]),
        provider=provider,
        status="missing",
        observed_at_ms=int(now_ms),
        next_refresh_at_ms=int(next_refresh_at_ms),
        last_error=None,
    )


def _write_error_profile(
    *,
    repos: Any,
    provider: str,
    row: dict[str, Any],
    exc: Exception,
    now_ms: int,
    next_refresh_at_ms: int,
) -> None:
    repos.asset_profiles.upsert_status(
        asset_id=str(row["target_id"]),
        provider=provider,
        status="error",
        observed_at_ms=int(now_ms),
        next_refresh_at_ms=int(next_refresh_at_ms),
        last_error=str(exc)[:500],
    )
