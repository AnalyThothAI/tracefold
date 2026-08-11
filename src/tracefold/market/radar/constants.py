from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION = "token_radar_snapshot_v3"
TOKEN_RADAR_CURRENT_WINDOW_MS = 60 * 60 * 1000
TOKEN_RADAR_PRIOR_WINDOW_MS = 60 * 60 * 1000
TOKEN_RADAR_REPLAY_TRANSITION_MS = 60 * 60 * 1000
TOKEN_RADAR_SOURCE_HORIZON_MS = (
    TOKEN_RADAR_REPLAY_TRANSITION_MS + TOKEN_RADAR_CURRENT_WINDOW_MS + TOKEN_RADAR_PRIOR_WINDOW_MS
)
TOKEN_RADAR_EPISODE_TTL_MS = 60 * 60 * 1000
TOKEN_RADAR_LIVE_LAG_MS = 2 * 60 * 1000
TOKEN_RADAR_MAX_ITEMS = 50
TOKEN_RADAR_INPUT_ROW_CAP = 10_000
TOKEN_RADAR_INPUT_BYTE_CAP = 8 * 1024 * 1024
TOKEN_RADAR_OUTPUT_BYTE_CAP = 96 * 1024
TOKEN_RADAR_REDUCER_BUDGET_SECONDS = 5.0
TOKEN_RADAR_REFRESH_SECONDS = 30.0
TOKEN_RADAR_SOURCE_PROVIDER = "gmgn"
TOKEN_RADAR_SOURCE_TRANSPORT = "direct_ws"
TOKEN_RADAR_SOURCE_COVERAGE = "public_stream"
TOKEN_RADAR_SOURCE_CHANNELS = (
    "twitter_monitor_basic",
    "twitter_monitor_token",
    "twitter_monitor_translation",
    "twitter_monitor_express",
)
TOKEN_RADAR_ALLOWED_ACTIONS = ("tweet", "quote", "reply", "repost")
TOKEN_RADAR_RESOLVED_STATUSES = ("EXACT", "UNIQUE_BY_CONTEXT")
TOKEN_RADAR_RESOLVED_TARGET_TYPES = ("Asset", "CexToken")


@dataclass(frozen=True, slots=True)
class _TokenRadarSemantics:
    """The complete code-owned replay semantics; never a public audit payload."""

    version: str
    replay_semantics_version: str
    snapshot_schema_version: str
    source_provider: str
    source_transport: str
    source_coverage: str
    source_channels: tuple[str, ...]
    actions: tuple[str, ...]
    resolution_statuses: tuple[str, ...]
    target_types: tuple[str, ...]
    maximum_live_lag_ms: int
    replay_transition_ms: int
    current_window_ms: int
    prior_window_ms: int
    source_horizon_ms: int
    episode_ttl_ms: int
    minimum_attention_delta: int
    minimum_independent_authors: int
    maximum_duplicate_share: float
    maximum_propagation_ms: int
    max_items: int


TOKEN_RADAR_SEMANTICS = _TokenRadarSemantics(
    version="token_radar_rules_v1",
    replay_semantics_version="token_radar_causal_replay_v3",
    snapshot_schema_version=TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
    source_provider=TOKEN_RADAR_SOURCE_PROVIDER,
    source_transport=TOKEN_RADAR_SOURCE_TRANSPORT,
    source_coverage=TOKEN_RADAR_SOURCE_COVERAGE,
    source_channels=TOKEN_RADAR_SOURCE_CHANNELS,
    actions=TOKEN_RADAR_ALLOWED_ACTIONS,
    resolution_statuses=TOKEN_RADAR_RESOLVED_STATUSES,
    target_types=TOKEN_RADAR_RESOLVED_TARGET_TYPES,
    maximum_live_lag_ms=TOKEN_RADAR_LIVE_LAG_MS,
    replay_transition_ms=TOKEN_RADAR_REPLAY_TRANSITION_MS,
    current_window_ms=TOKEN_RADAR_CURRENT_WINDOW_MS,
    prior_window_ms=TOKEN_RADAR_PRIOR_WINDOW_MS,
    source_horizon_ms=TOKEN_RADAR_SOURCE_HORIZON_MS,
    episode_ttl_ms=TOKEN_RADAR_EPISODE_TTL_MS,
    minimum_attention_delta=2,
    minimum_independent_authors=3,
    maximum_duplicate_share=0.5,
    maximum_propagation_ms=30 * 60 * 1000,
    max_items=TOKEN_RADAR_MAX_ITEMS,
)


def _semantics_fingerprint(semantics: _TokenRadarSemantics) -> str:
    encoded = json.dumps(
        asdict(semantics),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


TOKEN_RADAR_SEMANTICS_FINGERPRINT = _semantics_fingerprint(TOKEN_RADAR_SEMANTICS)
