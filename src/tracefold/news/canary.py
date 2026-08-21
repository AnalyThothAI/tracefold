"""Small, deterministic online canary selector and durable control commands.

The selector sees only facts known before the model call.  It never evaluates a
verdict, market outcome, or operator review.  Persistence is delegated to the
News repository so an Event keeps one arm across retries and process restarts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

CANARY_SELECTOR_VERSION = "news_canary_selector_v1"
CANARY_EXPOSURE_BPS = 1_000
CANARY_ELIGIBILITY_PROFILE: dict[str, Any] = {
    "live_only": True,
    "excluded_admissions": ["recovery", "listing_deterministic"],
    "excluded_priorities": ["high"],
}
CANARY_ROLLING_PROFILE: dict[str, Any] = {
    "evaluation_bucket_ms": 3_600_000,
    "lookback_ms": 6 * 3_600_000,
    "candidate_min_n": 8,
    "error_or_degraded_rate_max": 0.20,
    "consecutive_breach_buckets": 2,
}
CANARY_ELIGIBILITY_PROFILE_SHA = hashlib.sha256(
    json.dumps(CANARY_ELIGIBILITY_PROFILE, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
CANARY_ROLLING_PROFILE_SHA = hashlib.sha256(
    json.dumps(CANARY_ROLLING_PROFILE, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


@dataclass(frozen=True, slots=True)
class CanarySelection:
    arm: Literal["stable", "candidate"]
    bundle_sha: str
    eligibility_reason: str


@dataclass(frozen=True, slots=True)
class CanaryCommand:
    action: Literal["arm", "status", "hold", "resume", "trip", "close"]
    candidate_sha: str | None = None
    activation_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CanaryRuntimeArm:
    """One image-carried arm already composed with its model and policy."""

    bundle_sha: str
    model: Any
    policy: Any
    prompt_version: str
    prompt_sha256: str


def select_canary_arm(
    *,
    event_id: str,
    activation_id: str,
    baseline_bundle_sha: str,
    candidate_bundle_sha: str,
    exposure_bps: int,
    admission: str,
    priority: str,
    ingest_mode: str,
) -> CanarySelection:
    """Assign one arm from pre-call facts; the caller persists the result."""

    if ingest_mode != "live":
        return CanarySelection("stable", baseline_bundle_sha, "excluded_non_live")
    if admission in CANARY_ELIGIBILITY_PROFILE["excluded_admissions"]:
        return CanarySelection("stable", baseline_bundle_sha, f"excluded_admission:{admission}")
    if priority in CANARY_ELIGIBILITY_PROFILE["excluded_priorities"]:
        return CanarySelection("stable", baseline_bundle_sha, f"excluded_priority:{priority}")
    digest = hashlib.sha256(f"{activation_id}:{event_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % 10_000
    if bucket < int(exposure_bps):
        return CanarySelection("candidate", candidate_bundle_sha, "eligible_bucket")
    return CanarySelection("stable", baseline_bundle_sha, "eligible_control")


def parse_canary_control(payload: Mapping[str, Any]) -> CanaryCommand:
    raw_action = str(payload.get("action") or "")
    if raw_action not in {"arm", "status", "hold", "resume", "trip", "close"}:
        raise ValueError("news_canary_action_invalid")
    action = cast(Literal["arm", "status", "hold", "resume", "trip", "close"], raw_action)
    candidate = str(payload.get("candidate_sha") or "") or None
    activation = str(payload.get("activation_id") or "") or None
    reason = str(payload.get("reason") or "") or None
    if action == "arm" and (candidate is None or not _is_sha(candidate)):
        raise ValueError("news_canary_candidate_sha_required")
    if action in {"hold", "resume", "trip", "close"} and not activation:
        raise ValueError("news_canary_activation_required")
    if action in {"hold", "resume", "trip", "close"} and not reason:
        raise ValueError("news_canary_reason_required")
    return CanaryCommand(action=action, candidate_sha=candidate, activation_id=activation, reason=reason)


def apply_canary_control(
    repos: Any,
    command: CanaryCommand,
    *,
    stable_bundle_sha: str,
    shipped_candidates: Mapping[str, str],
    now_ms: int,
) -> dict[str, Any]:
    """Apply one CAS-backed transition through the existing News control seam."""

    if command.action == "status":
        return dict(repos.news.canary_status())
    if command.action == "arm":
        candidate = str(command.candidate_sha)
        candidate_bundle_sha = shipped_candidates.get(candidate)
        if candidate_bundle_sha is None:
            raise ValueError("news_canary_candidate_not_in_image")
        if not repos.news.canary_candidate_eligible(candidate):
            raise ValueError("news_canary_shadow_evidence_not_passed")
        activation_id = uuid.uuid4().hex
        repos.news.arm_canary(
            activation_id=activation_id,
            baseline_bundle_sha=stable_bundle_sha,
            candidate_manifest_sha=candidate,
            candidate_bundle_sha=candidate_bundle_sha,
            selector_version=CANARY_SELECTOR_VERSION,
            exposure_bps=CANARY_EXPOSURE_BPS,
            eligibility_profile_sha=CANARY_ELIGIBILITY_PROFILE_SHA,
            rolling_profile_sha=CANARY_ROLLING_PROFILE_SHA,
            now_ms=now_ms,
        )
        return dict(repos.news.canary_status())
    target_state = {
        "hold": "armed",
        "resume": "active",
        "trip": "tripped",
        "close": "closed",
    }[command.action]
    repos.news.transition_canary(
        activation_id=str(command.activation_id),
        target_state=target_state,
        reason=str(command.reason),
        now_ms=now_ms,
    )
    return dict(repos.news.canary_status())


def _is_sha(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


__all__ = [
    "CANARY_ELIGIBILITY_PROFILE_SHA",
    "CANARY_EXPOSURE_BPS",
    "CANARY_ROLLING_PROFILE",
    "CANARY_ROLLING_PROFILE_SHA",
    "CANARY_SELECTOR_VERSION",
    "CanaryCommand",
    "CanaryRuntimeArm",
    "CanarySelection",
    "apply_canary_control",
    "parse_canary_control",
    "select_canary_arm",
]
