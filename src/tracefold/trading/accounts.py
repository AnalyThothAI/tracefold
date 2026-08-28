"""Pure account-binding identities shared by Trading execution roots."""

from __future__ import annotations

from .contracts import canonical_sha256


def trading_credential_fingerprint(*, venue: str, api_key: str, api_secret: str) -> str:
    """Bind exact credentials to one venue without persisting either credential."""

    return canonical_sha256(
        {
            "version": "trading_credential_fingerprint_v1",
            "venue": str(venue),
            "api_key": str(api_key),
            "api_secret": str(api_secret),
        }
    )


def trading_provider_account_fingerprint(*, venue: str, provider_account_id: str) -> str:
    """Bind the provider's account alias/UID without persisting that identifier."""

    normalized_venue = str(venue or "").strip()
    normalized_account_id = str(provider_account_id or "").strip()
    if not normalized_venue or not normalized_account_id or len(normalized_account_id) > 256:
        raise ValueError("trading_provider_account_identity_invalid")
    return canonical_sha256(
        {
            "version": "trading_provider_account_fingerprint_v1",
            "venue": normalized_venue,
            "provider_account_id": normalized_account_id,
        }
    )


__all__ = ["trading_credential_fingerprint", "trading_provider_account_fingerprint"]
