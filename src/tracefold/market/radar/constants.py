from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION = "token_radar_snapshot_v1"
TOKEN_RADAR_CURRENT_WINDOW_MS = 60 * 60 * 1000
TOKEN_RADAR_PRIOR_WINDOW_MS = 60 * 60 * 1000
TOKEN_RADAR_MAX_ITEMS = 8
TOKEN_RADAR_INPUT_ROW_CAP = 10_000
TOKEN_RADAR_INPUT_BYTE_CAP = 8 * 1024 * 1024
TOKEN_RADAR_OUTPUT_BYTE_CAP = 20 * 1024
TOKEN_RADAR_REDUCER_BUDGET_SECONDS = 5.0
TOKEN_RADAR_REFRESH_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class _TokenRadarRuleSet:
    """The code-owned boolean rules; never a weighted score."""

    version: str
    minimum_attention_delta: int
    minimum_independent_authors: int
    maximum_duplicate_share: float
    maximum_propagation_ms: int


TOKEN_RADAR_RULESET = _TokenRadarRuleSet(
    version="token_radar_rules_v1",
    minimum_attention_delta=2,
    minimum_independent_authors=3,
    maximum_duplicate_share=0.5,
    maximum_propagation_ms=30 * 60 * 1000,
)


def _ruleset_fingerprint(rules: _TokenRadarRuleSet) -> str:
    payload = {
        "version": rules.version,
        "minimum_attention_delta": rules.minimum_attention_delta,
        "minimum_independent_authors": rules.minimum_independent_authors,
        "maximum_duplicate_share": rules.maximum_duplicate_share,
        "maximum_propagation_ms": rules.maximum_propagation_ms,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


TOKEN_RADAR_RULESET_FINGERPRINT = _ruleset_fingerprint(TOKEN_RADAR_RULESET)
