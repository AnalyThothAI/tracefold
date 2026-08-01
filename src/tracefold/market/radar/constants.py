from __future__ import annotations

# Pure constants for the token_intel domain.
# This module intentionally has no intra-domain imports to avoid circular dependencies.

TOKEN_RADAR_PROJECTION_NAME = "token-radar"
TOKEN_RADAR_PROJECTION_VERSION = "token-radar-v15-provider-neutral"
TOKEN_RADAR_RESOLVER_POLICY_VERSION = "token_radar_v5_identity_resolver"
TOKEN_RADAR_DEFAULT_VENUE = "all"
TOKEN_RADAR_VENUES = ("all", "sol", "eth", "base", "bsc", "cex")
TOKEN_FACTOR_SNAPSHOT_VERSION = "token_factor_snapshot_v5_provider_neutral"
TOKEN_RADAR_DECISIONS = frozenset({"discard", "watch", "high_alert"})
TOKEN_RADAR_FACTOR_FAMILIES = (
    "social_heat",
    "social_propagation",
    "timing_risk",
)
WINDOW_MS = {
    "5m": 5 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
}
RADAR_PROJECTION_DEADLINE_MS = {
    "5m": 10_000,
    "1h": 60_000,
    "4h": 60_000,
    "24h": 60_000,
}
