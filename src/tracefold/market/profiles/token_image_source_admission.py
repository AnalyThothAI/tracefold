from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from tracefold.market.profiles.profile_source_ids import (
    BINANCE_CEX_PROFILE_PROVIDER,
    BINANCE_WEB3_PROFILE_PROVIDER,
    GMGN_DEX_PROFILE_PROVIDER,
    GMGN_STREAM_PROFILE_PROVIDER,
    OKX_DEX_PROFILE_PROVIDER,
)
from tracefold.market.profiles.token_image_mirror import is_allowed_token_image_source_url

TOKEN_IMAGE_MIRROR_WORKER = "token_image_mirror"

DIRTY_REASON = "token_profile_current_source_admission"

_COUNT_KEYS = (
    "candidates",
    "admitted",
    "ready_existing",
    "pending_existing",
    "error_existing",
    "unsupported_existing",
    "dirty_existing",
    "terminal_existing",
)


@dataclass(frozen=True)
class TokenImageSourceCandidate:
    target_type: str
    target_id: str
    source_url: str
    source_provider: str
    source_kind: str
    source_watermark_ms: int
    priority: int
    raw_ref_json: dict[str, Any]

    @property
    def source_url_hash(self) -> str:
        return sha256(self.source_url.encode("utf-8")).hexdigest()

    def as_dirty_target(self, *, due_at_ms: int) -> dict[str, Any]:
        return {
            "source_url_hash": self.source_url_hash,
            "source_url": self.source_url,
            "source_provider": self.source_provider,
            "source_kind": self.source_kind,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "raw_ref_json": dict(self.raw_ref_json),
            "source_watermark_ms": int(self.source_watermark_ms),
            "priority": int(self.priority),
            "due_at_ms": int(due_at_ms),
        }


@dataclass(frozen=True)
class TokenImageSourceAdmissionResult:
    counts: dict[str, int]
    image_states_by_source_key: dict[tuple[str, str, str], dict[str, Any]]


def image_source_candidates_for_target(
    *,
    target: dict[str, Any],
    gmgn_openapi: dict[str, Any] | None,
    binance_web3: dict[str, Any] | None,
    gmgn_stream: dict[str, Any] | None,
    okx_dex: dict[str, Any] | None,
    cex_profile: dict[str, Any] | None,
) -> list[TokenImageSourceCandidate]:
    target_type = _clean(target.get("target_type"))
    target_id = _clean(target.get("target_id"))
    if not target_type or not target_id:
        return []

    if target_type == "CexToken":
        return _candidate_list(
            target_type=target_type,
            target_id=target_id,
            rows=[
                _CandidateSource(
                    row=cex_profile,
                    source_url=(cex_profile or {}).get("logo_url"),
                    source_provider=_clean((cex_profile or {}).get("provider")) or BINANCE_CEX_PROFILE_PROVIDER,
                    source_kind="cex_token_profiles.logo_url",
                    priority=20,
                    raw_ref_keys=("cex_token_id", "source_ref", "provider"),
                    raw_ref_defaults={"provider": BINANCE_CEX_PROFILE_PROVIDER},
                )
            ],
        )

    return _candidate_list(
        target_type=target_type,
        target_id=target_id,
        rows=[
            _CandidateSource(
                row=gmgn_openapi,
                source_url=(gmgn_openapi or {}).get("logo_url"),
                source_provider=GMGN_DEX_PROFILE_PROVIDER,
                source_kind="asset_profiles.logo_url",
                priority=20,
                raw_ref_keys=("asset_id", "source_ref", "provider"),
                raw_ref_defaults={"provider": GMGN_DEX_PROFILE_PROVIDER},
            ),
            _CandidateSource(
                row=binance_web3,
                source_url=(binance_web3 or {}).get("logo_url"),
                source_provider=BINANCE_WEB3_PROFILE_PROVIDER,
                source_kind="asset_profiles.logo_url",
                priority=30,
                raw_ref_keys=("asset_id", "source_ref", "provider"),
                raw_ref_defaults={"provider": BINANCE_WEB3_PROFILE_PROVIDER},
            ),
            _CandidateSource(
                row=gmgn_stream,
                source_url=_raw(gmgn_stream).get("i"),
                source_provider=GMGN_STREAM_PROFILE_PROVIDER,
                source_kind="asset_identity_evidence.raw_payload_json.i",
                priority=40,
                raw_ref_keys=("asset_id", "evidence_id", "source_event_id", "provider"),
                raw_ref_defaults={"provider": GMGN_STREAM_PROFILE_PROVIDER},
            ),
            _CandidateSource(
                row=okx_dex,
                source_url=_raw(okx_dex).get("tokenLogoUrl"),
                source_provider=OKX_DEX_PROFILE_PROVIDER,
                source_kind="asset_identity_evidence.raw_payload_json.tokenLogoUrl",
                priority=50,
                raw_ref_keys=("asset_id", "evidence_id", "source_event_id", "provider"),
                raw_ref_defaults={"provider": OKX_DEX_PROFILE_PROVIDER},
            ),
        ],
    )


def admit_token_image_sources(
    *,
    repos: Any,
    candidates: list[TokenImageSourceCandidate],
    now_ms: int,
) -> TokenImageSourceAdmissionResult:
    return _resolve_token_image_sources(
        repos=repos,
        candidates=candidates,
        now_ms=now_ms,
        persist=True,
    )


def inspect_token_image_sources(
    *,
    repos: Any,
    candidates: list[TokenImageSourceCandidate],
    now_ms: int,
) -> TokenImageSourceAdmissionResult:
    """Return the exact post-admission image states without mutating recovery state."""

    return _resolve_token_image_sources(
        repos=repos,
        candidates=candidates,
        now_ms=now_ms,
        persist=False,
    )


def _resolve_token_image_sources(
    *,
    repos: Any,
    candidates: list[TokenImageSourceCandidate],
    now_ms: int,
    persist: bool,
) -> TokenImageSourceAdmissionResult:
    unique_candidates = _dedupe_candidates(
        [candidate for candidate in candidates if _is_admissible_source_url(candidate.source_url)]
    )
    counts = {key: 0 for key in _COUNT_KEYS}
    counts["candidates"] = len(unique_candidates)
    if not unique_candidates:
        return TokenImageSourceAdmissionResult(counts=counts, image_states_by_source_key={})

    source_urls = [candidate.source_url for candidate in unique_candidates]
    asset_rows = repos.token_image_assets.by_source_urls(source_urls)
    dirty_identities = [_dirty_identity(candidate) for candidate in unique_candidates]
    dirty_rows = repos.token_image_source_dirty_targets.existing_by_source_targets(dirty_identities)
    terminal_rows = repos.token_image_source_dirty_targets.unresolved_terminal_by_source_targets(
        dirty_identities,
        owner_key=TOKEN_IMAGE_MIRROR_WORKER,
    )

    enqueues: list[dict[str, Any]] = []
    image_states: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in unique_candidates:
        source_key = _candidate_key(candidate)
        asset_row = asset_rows.get(candidate.source_url)
        terminal_row = terminal_rows.get(source_key)
        if asset_row:
            status = _clean(asset_row.get("status"))
            if status in {"ready", "unsupported"}:
                counts[f"{status}_existing"] += 1
                image_states[source_key] = dict(asset_row)
                continue
            if status == "pending":
                counts["pending_existing"] += 1
                if terminal_row is not None:
                    counts["terminal_existing"] += 1
                    image_states[source_key] = _terminal_image_state(
                        candidate,
                        terminal_row=terminal_row,
                        asset_row=asset_row,
                    )
                    continue
                dirty_row = dirty_rows.get(source_key)
                if dirty_row is not None:
                    counts["dirty_existing"] += 1
                    image_states[source_key] = {
                        "status": "mirror_pending",
                        **dict(dirty_row),
                        "asset_status": "pending",
                        "source_url": candidate.source_url,
                        "source_provider": candidate.source_provider,
                        "source_url_hash": candidate.source_url_hash,
                    }
                else:
                    enqueues.append(candidate.as_dirty_target(due_at_ms=int(now_ms)))
                    image_states[source_key] = {
                        "status": "mirror_pending",
                        "asset_status": "pending",
                        "source_url": candidate.source_url,
                        "source_provider": candidate.source_provider,
                        "source_url_hash": candidate.source_url_hash,
                        "target_type": candidate.target_type,
                        "target_id": candidate.target_id,
                    }
                continue
            if status == "error":
                counts["error_existing"] += 1
                if terminal_row is not None:
                    counts["terminal_existing"] += 1
                    image_states[source_key] = _terminal_image_state(
                        candidate,
                        terminal_row=terminal_row,
                        asset_row=asset_row,
                    )
                    continue
                dirty_row = dirty_rows.get(source_key)
                if dirty_row is not None:
                    counts["dirty_existing"] += 1
                    image_states[source_key] = {
                        "status": "mirror_pending",
                        **dict(dirty_row),
                        "asset_status": "error",
                        "last_error": asset_row.get("last_error"),
                        "next_refresh_at_ms": asset_row.get("next_refresh_at_ms"),
                        "source_url": candidate.source_url,
                        "source_provider": candidate.source_provider,
                        "source_url_hash": candidate.source_url_hash,
                    }
                else:
                    image_states[source_key] = dict(asset_row)
                    due_at_ms = max(int(now_ms), _int_or_zero(asset_row.get("next_refresh_at_ms")))
                    enqueues.append(candidate.as_dirty_target(due_at_ms=due_at_ms))
                continue

        if terminal_row is not None:
            counts["terminal_existing"] += 1
            image_states[source_key] = _terminal_image_state(candidate, terminal_row=terminal_row, asset_row=None)
            continue

        dirty_row = dirty_rows.get(source_key)
        if dirty_row is not None:
            counts["dirty_existing"] += 1
            image_states[source_key] = {"status": "mirror_pending", **dict(dirty_row)}
            continue

        enqueues.append(candidate.as_dirty_target(due_at_ms=int(now_ms)))
        image_states[source_key] = {
            "status": "mirror_pending",
            "source_url": candidate.source_url,
            "source_provider": candidate.source_provider,
            "source_url_hash": candidate.source_url_hash,
            "target_type": candidate.target_type,
            "target_id": candidate.target_id,
        }

    if enqueues and persist:
        enqueue_result = repos.token_image_source_dirty_targets.enqueue_targets(
            enqueues,
            reason=DIRTY_REASON,
            now_ms=int(now_ms),
        )
        counts["admitted"] = int(enqueue_result.get("targets") or 0)
    elif enqueues:
        counts["admitted"] = len(enqueues)

    return TokenImageSourceAdmissionResult(counts=counts, image_states_by_source_key=image_states)


@dataclass(frozen=True)
class _CandidateSource:
    row: dict[str, Any] | None
    source_url: Any
    source_provider: str
    source_kind: str
    priority: int
    raw_ref_keys: tuple[str, ...]
    raw_ref_defaults: dict[str, Any]


def _candidate_list(
    *,
    target_type: str,
    target_id: str,
    rows: list[_CandidateSource],
) -> list[TokenImageSourceCandidate]:
    candidates: list[TokenImageSourceCandidate] = []
    for source in rows:
        source_url = _clean(source.source_url)
        if not _is_admissible_source_url(source_url):
            continue
        row = source.row or {}
        candidates.append(
            TokenImageSourceCandidate(
                target_type=target_type,
                target_id=target_id,
                source_url=str(source_url),
                source_provider=source.source_provider,
                source_kind=source.source_kind,
                source_watermark_ms=_source_watermark_ms(row),
                priority=source.priority,
                raw_ref_json=_bounded_raw_ref(
                    row,
                    keys=source.raw_ref_keys,
                    defaults=source.raw_ref_defaults,
                ),
            )
        )
    return _dedupe_candidates(candidates)


def _dedupe_candidates(candidates: list[TokenImageSourceCandidate]) -> list[TokenImageSourceCandidate]:
    unique: dict[tuple[str, str, str], TokenImageSourceCandidate] = {}
    for candidate in candidates:
        key = _candidate_key(candidate)
        current = unique.get(key)
        if current is None or candidate.priority < current.priority:
            unique[key] = candidate
    return list(unique.values())


def _dirty_identity(candidate: TokenImageSourceCandidate) -> dict[str, Any]:
    return {
        "source_url_hash": candidate.source_url_hash,
        "source_url": candidate.source_url,
        "target_type": candidate.target_type,
        "target_id": candidate.target_id,
    }


def _terminal_image_state(
    candidate: TokenImageSourceCandidate,
    *,
    terminal_row: dict[str, Any],
    asset_row: dict[str, Any] | None,
) -> dict[str, Any]:
    state = {
        "status": "error",
        "source_url": candidate.source_url,
        "source_provider": candidate.source_provider,
        "source_url_hash": candidate.source_url_hash,
        "target_type": candidate.target_type,
        "target_id": candidate.target_id,
        "terminal_id": terminal_row.get("terminal_id"),
        "terminal_final_reason": terminal_row.get("final_reason"),
        "terminalized_at_ms": terminal_row.get("terminalized_at_ms"),
    }
    if asset_row is not None:
        state["asset_status"] = asset_row.get("status")
        state["last_error"] = asset_row.get("last_error")
        state["next_refresh_at_ms"] = asset_row.get("next_refresh_at_ms")
    else:
        state["last_error"] = terminal_row.get("final_reason")
    return state


def _candidate_key(candidate: TokenImageSourceCandidate) -> tuple[str, str, str]:
    return (candidate.source_url_hash, candidate.target_type, candidate.target_id)


def _is_admissible_source_url(value: str | None) -> bool:
    if not value or "/default-logo/" in value:
        return False
    try:
        return is_allowed_token_image_source_url(value)
    except ValueError:
        return False


def _bounded_raw_ref(
    row: dict[str, Any],
    *,
    keys: tuple[str, ...],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    raw_ref: dict[str, Any] = {}
    for key in keys:
        value = row.get(key, defaults.get(key))
        text = _clean(value)
        if text is not None:
            raw_ref[key] = text[:256]
    return raw_ref


def _source_watermark_ms(row: dict[str, Any]) -> int:
    value = row.get("observed_at_ms")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("token_image_source_admission_source_watermark_required")
    if value <= 0:
        raise ValueError("token_image_source_admission_source_watermark_required")
    return int(value)


def _raw(row: dict[str, Any] | None) -> dict[str, Any]:
    raw = (row or {}).get("raw_payload_json")
    return dict(raw) if isinstance(raw, dict) else {}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _int_or_zero(value: Any) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else 0
