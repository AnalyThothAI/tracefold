from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tracefold.market.profiles.profile_source_ids import BINANCE_CEX_PROFILE_PROVIDER


def sync_cex_token_profiles(
    *,
    repos: Any,
    profiles: Iterable[Mapping[str, Any]],
    observed_at_ms: int,
) -> dict[str, Any]:
    formal_profiles = [_formal_profile(profile) for profile in profiles]
    profiles_seen = 0
    profiles_updated = 0
    missing_cex_tokens = 0
    affected_lookup_keys: set[str] = set()
    provider_name = None

    with repos.transaction():
        for profile in formal_profiles:
            base_symbol = profile["base_symbol"]
            logo_url = profile["logo_url"]
            provider = profile["provider"]
            profiles_seen += 1
            provider_name = provider_name or provider
            row = repos.cex_token_profiles.upsert_ready_profile_if_token_exists(
                base_symbol=base_symbol,
                provider=provider,
                symbol=profile["symbol"],
                name=profile["name"],
                logo_url=logo_url,
                source_ref=profile["source_ref"],
                raw_payload=profile["raw_payload"],
                observed_at_ms=int(observed_at_ms),
            )
            if row is None:
                missing_cex_tokens += 1
                continue
            profiles_updated += 1
            affected_lookup_keys.update(_symbol_lookup_keys(base_symbol))
    return {
        "profiles_seen": profiles_seen,
        "profiles_updated": profiles_updated,
        "missing_cex_tokens": missing_cex_tokens,
        "affected_lookup_keys": sorted(affected_lookup_keys),
        "provider": provider_name or BINANCE_CEX_PROFILE_PROVIDER,
    }


def _formal_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise TypeError("cex_token_profile_sync_profile_mapping_required")
    return {
        "base_symbol": _required_text(profile, "base_symbol"),
        "provider": _required_text(profile, "provider"),
        "symbol": _required_text(profile, "symbol"),
        "name": _optional_text(profile, "name"),
        "logo_url": _required_url(profile, "logo_url"),
        "source_ref": _required_text(profile, "source_ref"),
        "raw_payload": _required_raw_payload(profile),
    }


def _optional_text(profile: Mapping[str, Any], key: str) -> str | None:
    value = profile.get(key)
    text = str(value or "").strip()
    return text or None


def _required_text(profile: Mapping[str, Any], key: str) -> str:
    text = _optional_text(profile, key)
    if text is None:
        raise ValueError(f"cex_token_profile_sync_{key}_required")
    return text


def _required_url(profile: Mapping[str, Any], key: str) -> str:
    text = _required_text(profile, key)
    if not text.startswith(("http://", "https://")):
        raise ValueError(f"cex_token_profile_sync_{key}_invalid")
    return text


def _required_raw_payload(profile: Mapping[str, Any]) -> dict[str, Any]:
    raw = profile.get("raw_payload")
    if raw is None:
        raise TypeError("cex_token_profile_sync_raw_payload_required")
    if not isinstance(raw, Mapping):
        raise TypeError("cex_token_profile_sync_raw_payload_invalid")
    return dict(raw)


def _symbol_lookup_keys(symbol: Any) -> set[str]:
    normalized = str(symbol or "").strip().lstrip("$").upper()
    if not normalized:
        return set()
    return {f"symbol:{normalized}", f"project_symbol:{normalized}", f"cex_token:{normalized}"}
