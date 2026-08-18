from __future__ import annotations

# Persisted policy identity for ``token_intent_resolutions.resolver_policy_version``.
# Search and Token Case read paths filter current resolutions by this exact value,
# so the literal is retained verbatim after the Token Radar product removal:
# renaming it would hide every already-resolved intent from serving reads until a
# full re-resolution. It is only an identity resolver policy version, not a Radar
# dependency.
TOKEN_RESOLVER_POLICY_VERSION = "token_radar_v5_identity_resolver"

__all__ = ["TOKEN_RESOLVER_POLICY_VERSION"]
