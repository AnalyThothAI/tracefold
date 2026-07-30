from __future__ import annotations

import hashlib
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from tracefold.market.identity.atomic_mention import HIGH_CONF_RESOLUTION_STATUSES, KOL_TIER_TAGS
from tracefold.market.radar.constants import (
    TOKEN_RADAR_DECISIONS,
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_FACTOR_FAMILIES,
    TOKEN_RADAR_PROJECTION_VERSION,
    TOKEN_RADAR_VENUES,
    WINDOW_MS,
)
from tracefold.market.radar.cross_section_normalizer import (
    MIN_COHORT_SIZE,
    NORMALIZER_VERSION,
    rank_factors_within_cohort,
    weighted_rank_score,
)
from tracefold.market.radar.factor_cohort import (
    COHORT_DEFINITION_VERSION,
    is_active_cohort_member,
)
from tracefold.market.radar.factor_snapshot import (
    build_token_factor_snapshot,
)
from tracefold.market.radar.factor_snapshot_contract import require_token_factor_snapshot
from tracefold.market.radar.token_radar_feature_builder import (
    build_radar_features,
)
from tracefold.market.radar.token_radar_repository import (
    token_radar_target_feature_payload,
)
from tracefold.platform.validation import require_nonnegative_int

PROJECTION_VERSION = TOKEN_RADAR_PROJECTION_VERSION
TOKEN_RADAR_DECISION_PRIORITY = {"high_alert": 0, "watch": 1, "discard": 2}
RANKED_NORMALIZATION_STATUSES = frozenset({"ranked", "no_signal"})
RANKED_COHORT_STATUSES = frozenset({"ready", "insufficient", "all_tied"})
DEX_DECISION_FLOORS = {
    "holders": 100,
    "liquidity_usd": 25_000.0,
    "market_cap_usd": 50_000.0,
}
LIVE_LATEST_MAX_AGE_MS = 90 * 1000
FRESH_LATEST_MAX_AGE_MS = 5 * 60 * 1000


class TokenRadarProjectionWindowError(ValueError):
    pass


def rank_compact_inputs(
    rank_inputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure cross-sectional rank normalization for one window/venue closure."""

    factor_scores: dict[str, dict[str, float | None]] = {}
    factor_weights: dict[str, dict[str, float]] = {}
    cohort: set[str] = set()
    cohort_metadata: dict[str, dict[str, Any]] = {}

    for row in rank_inputs:
        target_id = _compact_target_id(row)
        if not target_id:
            continue
        factor_scores[target_id] = {
            family: _compact_family_raw_score(row, family) for family in TOKEN_RADAR_FACTOR_FAMILIES
        }
        factor_weights[target_id] = {
            family: _compact_family_weight(row, family) for family in TOKEN_RADAR_FACTOR_FAMILIES
        }
        high_conf = int(row.get("cohort_high_confidence_mentions") or 0)
        kol_count = int(row.get("cohort_kol_mentions") or 0)
        first_seen_global = row.get("cohort_first_seen_global_24h") is True
        symbol = str(row.get("cohort_symbol") or "").upper()
        if is_active_cohort_member(
            target_id=target_id,
            symbol=symbol,
            high_confidence_mention_count=high_conf,
            kol_mention_count=kol_count,
            was_first_seen_global_24h=first_seen_global,
        ):
            cohort.add(target_id)
        cohort_metadata[target_id] = {
            "high_confidence_mentions": high_conf,
            "kol_mentions": kol_count,
            "followup_authors": int(row.get("cohort_followup_authors") or 0),
            "first_seen_global_24h": first_seen_global,
            "symbol": symbol,
        }

    cohort_status = _cohort_rank_status(
        factor_scores=factor_scores,
        cohort=cohort,
    )
    factor_ranks_by_id = rank_factors_within_cohort(
        factor_scores=factor_scores,
        cohort=cohort,
    )
    compact_rows: list[dict[str, Any]] = []
    for row in rank_inputs:
        target_id = _compact_target_id(row)
        factor_ranks = factor_ranks_by_id.get(target_id) or {family: None for family in TOKEN_RADAR_FACTOR_FAMILIES}
        weights = factor_weights.get(target_id) or {
            family: _compact_family_weight(row, family) for family in TOKEN_RADAR_FACTOR_FAMILIES
        }
        alpha_rank = weighted_rank_score(factor_ranks, weights)
        rank_score = (
            round(float(alpha_rank) * 100.0)
            if alpha_rank is not None
            else _rank_input_display_score(row, "raw_composite_score")
        )
        max_decision = _rank_input_decision(row, "gates_max_decision")
        decision = _decision_from_score_and_gates(
            rank_score,
            {"max_decision": max_decision},
        )
        compact_rows.append(
            {
                **dict(row),
                "rank_score": rank_score,
                "recommended_decision": decision,
                "normalization_status": ("ranked" if alpha_rank is not None else "no_signal"),
                "cohort_status": cohort_status,
                "cohort_in_cohort": target_id in cohort,
                "cohort_size": len(cohort),
                "cohort_metadata": cohort_metadata.get(target_id, {}),
                "factor_ranks": factor_ranks,
                "alpha_rank": alpha_rank,
            }
        )
    compact_rows.sort(key=_compact_rank_key)
    return compact_rows


def _cohort_rank_status(
    *,
    factor_scores: dict[str, dict[str, float | None]],
    cohort: set[str],
) -> str:
    rankable = [
        tuple(scores.get(family) for family in TOKEN_RADAR_FACTOR_FAMILIES)
        for token_id, scores in factor_scores.items()
        if token_id in cohort and any(scores.get(family) is not None for family in TOKEN_RADAR_FACTOR_FAMILIES)
    ]
    if len(rankable) < MIN_COHORT_SIZE:
        return "insufficient"
    if len(set(rankable)) <= 1:
        return "all_tied"
    return "ready"


def _window_ms(window: str) -> int:
    try:
        return WINDOW_MS[window]
    except KeyError as exc:
        raise TokenRadarProjectionWindowError(window) from exc


def _rank_change_payload_hash(row: Mapping[str, Any]) -> str:
    try:
        value = row["payload_hash"]
    except KeyError as exc:
        raise RuntimeError("token_radar_rank_change_payload_hash_required") from exc
    if value is None:
        raise RuntimeError("token_radar_rank_change_payload_hash_required")
    payload_hash = str(value).strip()
    if not payload_hash:
        raise RuntimeError("token_radar_rank_change_payload_hash_required")
    return payload_hash


def _required_projection_row_text(row: Mapping[str, Any], column: str) -> str:
    try:
        value = row[column]
    except KeyError as exc:
        raise RuntimeError("token_radar_current_identity_required") from exc
    if value is None:
        raise RuntimeError("token_radar_current_identity_required")
    text = str(value).strip()
    if not text:
        raise RuntimeError("token_radar_current_identity_required")
    return text


def _required_target_feature_current_row_text(row: Mapping[str, Any], column: str) -> str:
    try:
        value = row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_target_feature_current_row_required:{column}") from exc
    if value is None:
        raise RuntimeError(f"token_radar_target_feature_current_row_required:{column}")
    text = str(value).strip()
    if not text:
        raise RuntimeError(f"token_radar_target_feature_current_row_invalid:{column}")
    return text


def _required_target_feature_current_row_mapping(row: Mapping[str, Any], column: str) -> dict[str, Any]:
    try:
        value = row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_target_feature_current_row_required:{column}") from exc
    if value is None:
        raise RuntimeError(f"token_radar_target_feature_current_row_required:{column}")
    payload = _json_ready(value)
    if not isinstance(payload, Mapping) or not payload:
        raise RuntimeError(f"token_radar_target_feature_current_row_invalid:{column}")
    return dict(payload)


def _required_target_feature_current_row_string_list(
    row: Mapping[str, Any],
    column: str,
    *,
    allow_empty: bool,
) -> list[str]:
    try:
        value = row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_target_feature_current_row_required:{column}") from exc
    if value is None:
        raise RuntimeError(f"token_radar_target_feature_current_row_required:{column}")
    payload = _json_ready(value)
    if not isinstance(payload, list) or (not allow_empty and not payload):
        raise RuntimeError(f"token_radar_target_feature_current_row_invalid:{column}")
    if any(not isinstance(item, str) or not item.strip() for item in payload):
        raise RuntimeError(f"token_radar_target_feature_current_row_invalid:{column}")
    return [item.strip() for item in payload]


def _required_target_feature_nested_text(
    payload: Mapping[str, Any],
    *,
    parent: str,
    field: str,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"token_radar_target_feature_current_row_invalid:{parent}")
    return value.strip()


def _required_target_feature_nested_list(
    payload: Mapping[str, Any],
    *,
    parent: str,
    field: str,
) -> list[Any]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise RuntimeError(f"token_radar_target_feature_current_row_invalid:{parent}")
    return list(value)


def _required_target_feature_current_row_int(row: Mapping[str, Any], column: str) -> int:
    try:
        value = row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_target_feature_current_row_required:{column}") from exc
    if value is None:
        raise RuntimeError(f"token_radar_target_feature_current_row_required:{column}")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"token_radar_target_feature_current_row_invalid:{column}")
    return int(value)


def _rank_input_latest_event_received_at_ms(row: Mapping[str, Any]) -> int:
    try:
        value = row["latest_event_received_at_ms"]
    except KeyError as exc:
        raise RuntimeError("token_radar_rank_input_required:latest_event_received_at_ms") from exc
    if value is None:
        raise RuntimeError("token_radar_rank_input_required:latest_event_received_at_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("token_radar_rank_input_invalid:latest_event_received_at_ms")
    return int(value)


def _rank_input_lane(row: Mapping[str, Any]) -> str:
    try:
        value = row["lane"]
    except KeyError as exc:
        raise RuntimeError("token_radar_rank_input_required:lane") from exc
    if value is None:
        raise RuntimeError("token_radar_rank_input_required:lane")
    lane = str(value).strip()
    if lane not in {"resolved", "attention"}:
        raise RuntimeError("token_radar_rank_input_invalid:lane")
    return lane


def _rank_input_decision(row: Mapping[str, Any], column: str) -> str:
    try:
        value = row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_rank_input_required:{column}") from exc
    if value is None:
        raise RuntimeError(f"token_radar_rank_input_required:{column}")
    decision = str(value).strip()
    if decision not in TOKEN_RADAR_DECISIONS:
        raise RuntimeError(f"token_radar_rank_input_invalid:{column}")
    return decision


def _rank_input_number(row: Mapping[str, Any], column: str) -> float:
    try:
        value = row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_rank_input_required:{column}") from exc
    if value is None:
        raise RuntimeError(f"token_radar_rank_input_required:{column}")
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise RuntimeError(f"token_radar_rank_input_invalid:{column}")
    return float(value)


def _rank_input_nonnegative_int(row: Mapping[str, Any], column: str) -> int:
    try:
        value: object = row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_rank_input_required:{column}") from exc
    if value is None:
        raise RuntimeError(f"token_radar_rank_input_required:{column}")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"token_radar_rank_input_invalid:{column}")
    return value


def _ranked_row_required(row: Mapping[str, Any], column: str) -> Any:
    try:
        value = row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_ranked_row_required:{column}") from exc
    if value is None:
        raise RuntimeError(f"token_radar_ranked_row_required:{column}")
    return value


def _ranked_row_present(row: Mapping[str, Any], column: str) -> Any:
    try:
        return row[column]
    except KeyError as exc:
        raise RuntimeError(f"token_radar_ranked_row_required:{column}") from exc


def _ranked_row_mapping(row: Mapping[str, Any], column: str) -> dict[str, Any]:
    value = _json_ready(_ranked_row_required(row, column))
    if not isinstance(value, Mapping):
        raise RuntimeError(f"token_radar_ranked_row_invalid:{column}")
    return {str(key): item for key, item in value.items()}


def _ranked_row_status(row: Mapping[str, Any], column: str, allowed: frozenset[str]) -> str:
    value = _ranked_row_required(row, column)
    if not isinstance(value, str):
        raise RuntimeError(f"token_radar_ranked_row_invalid:{column}")
    status = value.strip()
    if status not in allowed:
        raise RuntimeError(f"token_radar_ranked_row_invalid:{column}")
    return status


def _ranked_row_non_negative_int(row: Mapping[str, Any], column: str) -> int:
    value = _ranked_row_required(row, column)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"token_radar_ranked_row_invalid:{column}")
    return int(value)


def _ranked_row_positive_int(row: Mapping[str, Any], column: str) -> int:
    value = _ranked_row_required(row, column)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"token_radar_ranked_row_invalid:{column}")
    return int(value)


def _ranked_row_bool(row: Mapping[str, Any], column: str) -> bool:
    value = _ranked_row_required(row, column)
    if not isinstance(value, bool):
        raise RuntimeError(f"token_radar_ranked_row_invalid:{column}")
    return value


def _ranked_alpha_rank(row: Mapping[str, Any], normalization_status: str) -> float | None:
    value = _ranked_row_present(row, "alpha_rank")
    if value is None:
        if normalization_status == "no_signal":
            return None
        raise RuntimeError("token_radar_ranked_row_invalid:alpha_rank")
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise RuntimeError("token_radar_ranked_row_invalid:alpha_rank")
    alpha_rank = float(value)
    if alpha_rank < 0.0 or alpha_rank > 1.0:
        raise RuntimeError("token_radar_ranked_row_invalid:alpha_rank")
    if normalization_status == "no_signal":
        raise RuntimeError("token_radar_ranked_row_invalid:alpha_rank")
    return alpha_rank


def _ranked_factor_ranks(ranked: Mapping[str, Any]) -> dict[str, float | None]:
    raw = _ranked_row_mapping(ranked, "factor_ranks")
    family_set = set(TOKEN_RADAR_FACTOR_FAMILIES)
    if set(raw) != family_set:
        raise RuntimeError("token_radar_ranked_row_invalid:factor_ranks")
    ranks: dict[str, float | None] = {}
    for family in TOKEN_RADAR_FACTOR_FAMILIES:
        value = raw[family]
        if value is None:
            ranks[family] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            raise RuntimeError("token_radar_ranked_row_invalid:factor_ranks")
        rank = float(value)
        if rank < 0.0 or rank > 1.0:
            raise RuntimeError("token_radar_ranked_row_invalid:factor_ranks")
        ranks[family] = rank
    return ranks


def _rank_input_display_score(row: Mapping[str, Any], column: str) -> int:
    score = _rank_input_number(row, column)
    return round(max(0.0, min(100.0, score)))


def _market_target_fields_from_target(target: Mapping[str, Any]) -> dict[str, str | None]:
    return _market_target_fields_from_subject(target_type=target.get("target_type"), subject=target)


def _market_target_fields_from_subject(*, target_type: Any, subject: Any) -> dict[str, str | None]:
    subject_map = _dict(subject)
    if str(target_type or "") == "Asset":
        return {
            "chain_id": _optional_text(subject_map.get("chain")),
            "address": _optional_text(subject_map.get("address")),
            "provider": None,
            "native_market_id": None,
        }
    if str(target_type or "") == "CexToken":
        pricefeed_provider, pricefeed_market_id = _cex_pricefeed_target(subject_map.get("pricefeed_id"))
        return {
            "chain_id": None,
            "address": None,
            "provider": _optional_text(pricefeed_provider),
            "native_market_id": _optional_text(pricefeed_market_id),
        }
    return {"chain_id": None, "address": None, "provider": None, "native_market_id": None}


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _cex_pricefeed_target(value: Any) -> tuple[str | None, str | None]:
    parts = str(value or "").strip().split(":")
    if len(parts) < 5 or parts[0] != "pricefeed" or parts[1] != "cex":
        return None, None
    return parts[2].strip().lower() or None, parts[-1].strip().upper() or None


def _json_ready(value: Any) -> Any:
    raw = getattr(value, "obj", value)
    if isinstance(raw, Mapping):
        return {str(key): _json_ready(item) for key, item in raw.items()}
    if isinstance(raw, list | tuple | set):
        return [_json_ready(item) for item in raw]
    return raw


def _project_group(
    rows: list[dict[str, Any]],
    *,
    now_ms: int,
    window: str,
    score_since_ms: int | None = None,
    window_ms: int | None = None,
    total_window_events: int | None = None,
) -> dict[str, Any] | None:
    resolved_window_ms = _window_ms(window) if window_ms is None else int(window_ms)
    if resolved_window_ms <= 0:
        raise TokenRadarProjectionWindowError(window)
    resolved_score_since_ms = (
        score_since_ms if score_since_ms is not None else min(int(row.get("received_at_ms") or 0) for row in rows)
    )
    window_rows = [row for row in rows if int(row.get("received_at_ms") or 0) >= resolved_score_since_ms]
    if not window_rows:
        return None
    previous_rows = [
        row
        for row in rows
        if resolved_score_since_ms - resolved_window_ms <= int(row.get("received_at_ms") or 0) < resolved_score_since_ms
    ]
    latest = max(window_rows, key=lambda row: int(row.get("received_at_ms") or 0))
    event_ids = sorted({str(row["event_id"]) for row in window_rows})
    latest_seen_ms = max(int(row.get("received_at_ms") or 0) for row in rows)
    resolution_status = _required_resolution_text(latest, "resolution_status")
    reason_codes_json = _required_resolution_list(latest, "reason_codes_json")
    candidate_ids_json = _required_resolution_list(latest, "candidate_ids_json")
    lookup_keys_json = _required_resolution_list(latest, "lookup_keys_json")
    target_type = str(latest.get("target_type") or "") or None
    target_id = str(latest.get("target_id") or "") or None
    target_type_key, identity_id = _projection_identity_key(latest)
    resolved = _has_resolved_target(latest, resolution_status=resolution_status)
    lane = "resolved" if resolved else "attention"
    target = _target(latest, resolved=resolved)
    market = _market_context(window_rows, resolved=resolved, now_ms=now_ms)
    scored_window_rows = [{**row, **_market_prefix_for_features(market)} for row in window_rows]
    features = build_radar_features(
        window_rows=scored_window_rows,
        context_rows=rows,
        previous_rows=previous_rows,
        now_ms=now_ms,
        window_ms=resolved_window_ms,
        total_window_events=total_window_events or len(event_ids),
    )
    factor_snapshot = build_token_factor_snapshot(
        target=target,
        attention=features.attention,
        social_quality={**features.quality, **features.propagation},
        market=market,
        timing=features.timing,
        source_event_ids=event_ids,
        computed_at_ms=now_ms,
    )
    decision = str(factor_snapshot["composite"]["recommended_decision"])
    # Cohort accounting fields are persisted as scalar rank inputs after each group settles.
    # These internal fields use the _cohort_* prefix and are stripped before persistence.
    cohort_high_conf_count = sum(
        1 for r in window_rows if (r.get("resolution_status") or "") in HIGH_CONF_RESOLUTION_STATUSES
    )
    cohort_kol_count = sum(1 for r in window_rows if set(r.get("author_tags_json") or ()) & KOL_TIER_TAGS)
    cohort_first_seen_global_24h = any(row.get("first_seen_global_24h") is True for row in window_rows)
    cohort_followup_count = int(features.propagation.get("followup_author_count") or 0)
    return {
        "source_max_received_at_ms": latest_seen_ms,
        "lane": lane,
        "rank": 0,
        "intent_id": latest["intent_id"],
        "event_id": latest["event_id"],
        "target_type_key": target_type_key,
        "identity_id": identity_id,
        "target_type": target_type,
        "target_id": target_id,
        "pricefeed_id": latest.get("pricefeed_id"),
        **_market_target_fields_from_target(target),
        "intent_json": {
            "intent_id": latest["intent_id"],
            "event_id": latest["event_id"],
            "display_symbol": _real_symbol(latest.get("display_symbol")),
            "display_name": latest.get("display_name"),
            "evidence": [],
        },
        "factor_snapshot_json": factor_snapshot,
        "factor_version": factor_snapshot["schema_version"],
        "resolution_json": {
            "status": resolution_status,
            "target_type": target_type,
            "target_id": target_id,
            "pricefeed_id": latest.get("pricefeed_id"),
            "reason_codes": reason_codes_json,
            "candidate_ids": candidate_ids_json,
            "lookup_keys": lookup_keys_json,
            "discovery": _resolution_discovery(lookup_keys_json=lookup_keys_json),
        },
        "decision": decision,
        "data_health_json": {
            "factor_snapshot": "ready",
            "identity": factor_snapshot["data_health"]["identity"],
            "market": factor_snapshot["data_health"]["market"],
            "social": factor_snapshot["data_health"]["social"],
            "alpha": factor_snapshot["data_health"]["alpha"],
        },
        "source_event_ids_json": event_ids,
        "created_at_ms": now_ms,
        # Internal cohort fields are converted to scalar rank inputs before persistence.
        "_cohort_high_conf_count": cohort_high_conf_count,
        "_cohort_kol_count": cohort_kol_count,
        "_cohort_first_seen_global_24h": cohort_first_seen_global_24h,
        "_cohort_followup_count": cohort_followup_count,
    }


def _projection_identity_key(row: Mapping[str, Any]) -> tuple[str, str]:
    target_type = str(row.get("target_type") or "").strip()
    target_id = str(row.get("target_id") or "").strip()
    if target_type and target_id:
        return target_type, target_id
    lookup_key = _first_discovery_lookup_key(_required_resolution_list(row, "lookup_keys_json"))
    if lookup_key:
        return "LookupKey", lookup_key
    raise RuntimeError("token_radar_projection_identity_required")


def _first_discovery_lookup_key(raw_keys: list[Any]) -> str | None:
    keys = _discovery_lookup_keys(raw_keys)
    return keys[0] if keys else None


def _has_resolved_target(row: dict[str, Any], *, resolution_status: str) -> bool:
    if resolution_status not in HIGH_CONF_RESOLUTION_STATUSES:
        return False
    target_type = _required_resolved_target_text(row, "target_type")
    if target_type not in {"Asset", "CexToken"}:
        raise RuntimeError("token_radar_projection_resolved_target_invalid:target_type")
    _required_resolved_target_text(row, "target_id")
    return target_type != "Asset" or row.get("asset_registry_status") in {"candidate", "canonical"}


def _resolution_discovery(*, lookup_keys_json: list[Any]) -> list[dict[str, Any]]:
    lookup_keys = _discovery_lookup_keys(lookup_keys_json)
    return [_not_searched_discovery(key) for key in lookup_keys]


def _discovery_lookup_keys(raw_keys: list[Any]) -> list[str]:
    out: list[str] = []
    for raw_key in raw_keys:
        key = str(raw_key or "")
        if key.startswith("symbol:") or key.startswith("address:"):
            out.append(key)
    return sorted(set(out))


def _required_resolution_text(row: Mapping[str, Any], field: str) -> str:
    if field not in row or row[field] is None:
        raise RuntimeError(f"token_radar_projection_resolution_required:{field}")
    value = str(row[field]).strip()
    if not value:
        raise RuntimeError(f"token_radar_projection_resolution_invalid:{field}")
    return value


def _required_resolution_list(row: Mapping[str, Any], field: str) -> list[Any]:
    if field not in row or row[field] is None:
        raise RuntimeError(f"token_radar_projection_resolution_required:{field}")
    value = row[field]
    if not isinstance(value, list):
        raise RuntimeError(f"token_radar_projection_resolution_invalid:{field}")
    return value


def _required_resolved_target_text(row: Mapping[str, Any], field: str) -> str:
    if field not in row or row[field] is None:
        raise RuntimeError(f"token_radar_projection_resolved_target_required:{field}")
    value = str(row[field]).strip()
    if not value:
        raise RuntimeError(f"token_radar_projection_resolved_target_invalid:{field}")
    return value


def _not_searched_discovery(lookup_key: str) -> dict[str, Any]:
    return {
        "lookup_key": lookup_key,
        "lookup_type": _lookup_type(lookup_key),
        "status": "not_searched",
        "candidate_count": 0,
        "last_lookup_at_ms": None,
        "next_refresh_at_ms": None,
        "last_error": None,
        "error_count": 0,
    }


def _lookup_type(lookup_key: str) -> str:
    if lookup_key.startswith("symbol:"):
        return "dex_symbol_lookup"
    if lookup_key.startswith("address:"):
        return "address_lookup"
    return "unknown_lookup"


def _target(row: dict[str, Any], *, resolved: bool) -> dict[str, Any]:
    target_type = row.get("target_type")
    target_id = row.get("target_id")
    if not target_type or not target_id:
        return {
            "target_type": None,
            "target_id": None,
            "symbol": _display_symbol(row),
            "status": _required_resolution_text(row, "resolution_status"),
        }
    if target_type == "CexToken":
        return {
            "target_type": "CexToken",
            "target_id": target_id,
            "symbol": _target_symbol(row),
            "status": row.get("cex_token_status"),
            "pricefeed_id": row.get("pricefeed_id"),
            "native_market_id": row.get("native_market_id"),
            "quote_symbol": row.get("pricefeed_quote_symbol"),
            "feed_type": row.get("feed_type"),
            "provider": row.get("pricefeed_provider"),
        }
    asset_target = {
        "target_type": "Asset",
        "target_id": target_id,
        "symbol": _target_symbol(row),
        "name": row.get("asset_name"),
        "chain": row.get("asset_chain_id"),
        "token_standard": row.get("asset_token_standard"),
        "address": row.get("asset_address"),
        "status": row.get("asset_registry_status"),
        "pricefeed_id": row.get("pricefeed_id"),
    }
    identity = _asset_identity_payload(row, required=resolved)
    if identity is not None:
        asset_target["identity"] = identity
    return asset_target


def _asset_identity_payload(row: Mapping[str, Any], *, required: bool) -> dict[str, Any] | None:
    if not required and all(
        row.get(field) is None
        for field in (
            "asset_identity_confidence",
            "asset_identity_reason_codes",
            "asset_identity_conflict_count",
        )
    ):
        return None
    return {
        "confidence": _required_asset_identity_text(row, "asset_identity_confidence"),
        "reason_codes": _required_asset_identity_list(row, "asset_identity_reason_codes"),
        "conflict_count": _required_asset_identity_int(row, "asset_identity_conflict_count"),
    }


def _required_asset_identity_text(row: Mapping[str, Any], field: str) -> str:
    if field not in row or row[field] is None:
        raise RuntimeError(f"token_radar_projection_asset_identity_required:{field}")
    value = str(row[field]).strip()
    if not value:
        raise RuntimeError(f"token_radar_projection_asset_identity_invalid:{field}")
    return value


def _required_asset_identity_list(row: Mapping[str, Any], field: str) -> list[Any]:
    if field not in row or row[field] is None:
        raise RuntimeError(f"token_radar_projection_asset_identity_required:{field}")
    value = row[field]
    if not isinstance(value, list):
        raise RuntimeError(f"token_radar_projection_asset_identity_invalid:{field}")
    return value


def _required_asset_identity_int(row: Mapping[str, Any], field: str) -> int:
    if field not in row or row[field] is None:
        raise RuntimeError(f"token_radar_projection_asset_identity_required:{field}")
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"token_radar_projection_asset_identity_invalid:{field}")
    return int(value)


def _market_context(window_rows: list[dict[str, Any]], *, resolved: bool, now_ms: int) -> dict[str, Any]:
    if not resolved:
        latest = max(window_rows, key=lambda item: int(item.get("received_at_ms") or 0)) if window_rows else {}
        return _market_context_dict(
            event_anchor=None,
            decision_latest=None,
            capture_method=None,
            capture_reason=None,
            tick_lag_ms=None,
            readiness=_market_readiness(
                event_anchor=None,
                decision_latest=None,
                target_type=latest.get("target_type"),
                now_ms=now_ms,
            ),
        )
    if not window_rows:
        return _market_context_dict(
            event_anchor=None,
            decision_latest=None,
            capture_method=None,
            capture_reason=None,
            tick_lag_ms=None,
            readiness=_market_readiness(
                event_anchor=None,
                decision_latest=None,
                target_type=None,
                now_ms=now_ms,
            ),
        )
    social_start = min(window_rows, key=lambda item: int(item.get("received_at_ms") or 0))
    event_anchor = _observation_from_row(
        social_start,
        prefix="event_price",
        source=social_start.get("event_price_capture_method"),
    )
    latest_row = max(
        window_rows,
        key=lambda item: int(item.get("latest_price_observed_at_ms") or 0),
    )
    decision_latest = _observation_from_row(
        latest_row,
        prefix="latest_price",
        source=latest_row.get("latest_price_source_tier"),
    )
    return _market_context_dict(
        event_anchor=event_anchor,
        decision_latest=decision_latest,
        capture_method=_optional_str(social_start.get("event_price_capture_method")),
        capture_reason=_optional_str(social_start.get("event_price_capture_reason")),
        tick_lag_ms=_int_or_none(social_start.get("event_price_tick_lag_ms")),
        readiness=_market_readiness(
            event_anchor=event_anchor,
            decision_latest=decision_latest,
            target_type=social_start.get("target_type"),
            now_ms=now_ms,
        ),
    )


def _market_context_dict(
    *,
    event_anchor: dict[str, Any] | None,
    decision_latest: dict[str, Any] | None,
    capture_method: str | None,
    capture_reason: str | None,
    tick_lag_ms: int | None,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_anchor": event_anchor,
        "decision_latest": decision_latest,
        "capture_method": capture_method,
        "capture_reason": capture_reason,
        "tick_lag_ms": tick_lag_ms,
        "readiness": readiness,
    }


def _observation_from_row(row: dict[str, Any], *, prefix: str, source: Any) -> dict[str, Any] | None:
    price_usd = row.get(_observation_column(prefix, "price_usd"))
    price_quote = row.get(_observation_column(prefix, "price_quote"))
    observed_at_ms = _int_or_none(row.get(f"{prefix}_observed_at_ms"))
    if observed_at_ms is None or (price_usd is None and price_quote is None):
        return None
    return {
        "target_type": row.get("target_type"),
        "target_id": row.get("target_id"),
        "observed_at_ms": observed_at_ms,
        "received_at_ms": _int_or_none(row.get(f"{prefix}_received_at_ms") or row.get("received_at_ms")),
        "source": str(source or ""),
        "provider": row.get(f"{prefix}_provider"),
        "pricefeed_id": row.get(f"{prefix}_pricefeed_id") or row.get("pricefeed_id"),
        "price_usd": _float_or_none(price_usd),
        "price_quote": _float_or_none(price_quote),
        "quote_symbol": row.get(f"{prefix}_quote_symbol"),
        "price_basis": row.get(_observation_column(prefix, "price_basis")),
        "market_cap_usd": _float_or_none(row.get(f"{prefix}_market_cap_usd")),
        "liquidity_usd": _float_or_none(row.get(f"{prefix}_liquidity_usd")),
        "holders": _int_or_none(row.get(f"{prefix}_holders")),
        "volume_24h_usd": _float_or_none(row.get(f"{prefix}_volume_24h_usd")),
        "open_interest_usd": _float_or_none(row.get(f"{prefix}_open_interest_usd")),
    }


def _observation_column(prefix: str, field: str) -> str:
    if prefix == "event_price" and field in {"price_usd", "price_quote", "price_basis"}:
        return f"event_{field}"
    if prefix == "latest_price" and field in {"price_usd", "price_quote", "price_basis"}:
        return f"latest_{field}"
    return f"{prefix}_{field}"


def _market_readiness(
    *,
    event_anchor: dict[str, Any] | None,
    decision_latest: dict[str, Any] | None,
    target_type: Any,
    now_ms: int,
) -> dict[str, Any]:
    missing_fields = _missing_decision_fields(decision_latest, target_type=target_type)
    stale_fields = []
    latest_status = _latest_status(decision_latest, now_ms=now_ms)
    if latest_status == "stale":
        stale_fields.append("decision_latest")
    return {
        "anchor_status": "ready" if event_anchor is not None else "missing",
        "latest_status": latest_status,
        "dex_floor_status": _dex_floor_status(decision_latest, target_type=target_type, missing_fields=missing_fields),
        "missing_fields": missing_fields,
        "stale_fields": stale_fields,
    }


def _missing_decision_fields(decision_latest: dict[str, Any] | None, *, target_type: Any) -> list[str]:
    if str(target_type or "") != "Asset":
        return []
    latest = decision_latest or {}
    return [field for field in DEX_DECISION_FLOORS if latest.get(field) is None]


def _latest_status(decision_latest: dict[str, Any] | None, *, now_ms: int) -> str:
    if decision_latest is None:
        return "missing"
    observed_at_ms = _int_or_none(decision_latest.get("received_at_ms") or decision_latest.get("observed_at_ms"))
    if observed_at_ms is None:
        return "missing"
    age_ms = max(0, int(now_ms) - observed_at_ms)
    if age_ms <= LIVE_LATEST_MAX_AGE_MS:
        return "live"
    if age_ms <= FRESH_LATEST_MAX_AGE_MS:
        return "fresh"
    return "stale"


def _dex_floor_status(
    decision_latest: dict[str, Any] | None,
    *,
    target_type: Any,
    missing_fields: list[str],
) -> str:
    if str(target_type or "") != "Asset":
        return "ready"
    if missing_fields:
        return "missing_fields"
    latest = decision_latest or {}
    for field, floor in DEX_DECISION_FLOORS.items():
        value = _float_or_none(latest.get(field))
        if value is None:
            return "missing_fields"
        if value < floor:
            return "below_floor"
    return "ready"


def _readiness_status(market: dict[str, Any]) -> str:
    readiness = _dict(market.get("readiness"))
    if readiness.get("anchor_status") != "ready":
        return "missing"
    latest_status = str(readiness.get("latest_status") or "missing")
    return "ready" if latest_status in {"live", "fresh"} else "partial"


def _price_change_between(current: dict[str, Any], base: dict[str, Any]) -> float | None:
    if current.get("price_usd") is not None and base.get("price_usd") is not None:
        return _pct_change(current.get("price_usd"), base.get("price_usd"))
    if current.get("quote_symbol") and current.get("quote_symbol") == base.get("quote_symbol"):
        return _pct_change(current.get("price_quote"), base.get("price_quote"))
    return None


def _market_prefix_for_features(market: dict[str, Any]) -> dict[str, Any]:
    event_anchor = _dict(market.get("event_anchor"))
    decision_latest = _dict(market.get("decision_latest"))
    return {
        "market_status": _readiness_status(market),
        "market_observation_status": _dict(market.get("readiness")).get("anchor_status"),
        "market_market_cap_usd": decision_latest.get("market_cap_usd"),
        "market_liquidity_usd": decision_latest.get("liquidity_usd"),
        "market_volume_24h_usd": decision_latest.get("volume_24h_usd"),
        "market_open_interest_usd": decision_latest.get("open_interest_usd"),
        "market_holders": decision_latest.get("holders"),
        "price_change_since_social_pct": _price_change_between(decision_latest, event_anchor),
        "price_change_before_social_pct": None,
    }


def _pct_change(current: Any, base: Any) -> float | None:
    current_value = _float_or_none(current)
    base_value = _float_or_none(base)
    if current_value is None or base_value is None or base_value == 0:
        return None
    return round(current_value / base_value - 1.0, 6)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _display_symbol(row: dict[str, Any]) -> str | None:
    for value in (
        row.get("display_symbol"),
        row.get("cex_base_symbol"),
        row.get("asset_symbol"),
        row.get("pricefeed_base_symbol"),
    ):
        symbol = _real_symbol(value)
        if symbol:
            return symbol
    return None


def _target_symbol(row: dict[str, Any]) -> str | None:
    if row.get("target_type") == "Asset":
        return _first_real_symbol(row.get("asset_symbol"))
    if row.get("target_type") == "CexToken":
        return _first_real_symbol(
            row.get("cex_base_symbol"),
            row.get("pricefeed_base_symbol"),
            row.get("display_symbol"),
        )
    return _display_symbol(row)


def _first_real_symbol(*values: Any) -> str | None:
    for value in values:
        symbol = _real_symbol(value)
        if symbol:
            return symbol
    return None


def _real_symbol(value: Any) -> str | None:
    if value is None:
        return None
    symbol = str(value).strip().lstrip("$")
    if not symbol:
        return None
    if _is_address_like_symbol(symbol):
        return None
    return symbol


def _is_address_like_symbol(symbol: str) -> bool:
    value = symbol.strip().upper()
    if value.startswith("0X") and len(value) >= 22:
        return all(char in "0123456789ABCDEF" for char in value[2:])
    if len(value) < 32:
        return False
    if value.endswith("PUMP"):
        value = value[:-4]
    return all(char.isdigit() or ("A" <= char <= "Z") for char in value)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compact_rank_key(row: dict[str, Any]) -> tuple[int, float, int, int]:
    decision = _rank_input_decision(row, "recommended_decision")
    rank_score = _rank_input_number(row, "rank_score")
    mentions_1h = _rank_input_nonnegative_int(row, "social_heat_mentions_1h")
    return (
        TOKEN_RADAR_DECISION_PRIORITY[decision],
        -rank_score,
        -mentions_1h,
        -int(row.get("social_heat_latest_seen_ms") or 0),
    )


def _select_top_ranked_by_lane(ranked: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    row_limit = require_nonnegative_int(limit, error_code="token_radar_rank_lane_limit_required")
    selected: list[dict[str, Any]] = []
    lane_order = ("resolved", "attention")
    for lane in lane_order:
        lane_rows = [row for row in ranked if _rank_input_lane(row) == lane]
        for rank, row in enumerate(lane_rows[:row_limit], start=1):
            selected.append({**row, "rank": rank})
    return selected


def token_radar_venue_for_rank_input(row: Mapping[str, Any]) -> str:
    target_type_value = row.get("target_type_key")
    if target_type_value is None:
        target_type_value = row.get("target_type")
    target_type = str(target_type_value or "").strip()
    if target_type == "CexToken":
        return "cex"
    if target_type != "Asset":
        return TOKEN_RADAR_DEFAULT_VENUE
    chain = (
        row.get("subject_chain")
        if "subject_chain" in row
        else _factor_snapshot_subject_chain(row.get("factor_snapshot_json"))
    )
    return _venue_for_chain(chain)


def _factor_snapshot_subject_chain(snapshot_value: Any) -> str | None:
    snapshot = require_token_factor_snapshot(_json_ready(snapshot_value), field_name="factor_snapshot_json")
    subject = snapshot["subject"]
    value = subject.get("chain")
    return str(value) if value is not None else None


def _venue_for_chain(value: Any) -> str:
    text = str(value or "").strip().lower()
    normalized = text.removeprefix("eip155:")
    mapping = {
        "1": "eth",
        "ethereum": "eth",
        "eth": "eth",
        "56": "bsc",
        "bsc": "bsc",
        "bnb": "bsc",
        "binance-smart-chain": "bsc",
        "binance_smart_chain": "bsc",
        "8453": "base",
        "base": "base",
        "sol": "sol",
        "solana": "sol",
    }
    venue = mapping.get(normalized, mapping.get(text, TOKEN_RADAR_DEFAULT_VENUE))
    return venue if venue in TOKEN_RADAR_VENUES else TOKEN_RADAR_DEFAULT_VENUE


def _row_from_target_feature(row: dict[str, Any], *, venue: str = TOKEN_RADAR_DEFAULT_VENUE) -> dict[str, Any]:
    projection_version = _required_target_feature_current_row_text(row, "projection_version")
    window = _required_target_feature_current_row_text(row, "window")
    lane = _required_target_feature_current_row_text(row, "lane")
    target_type_key = _required_projection_row_text(row, "target_type_key")
    identity_id = _required_projection_row_text(row, "identity_id")
    factor_snapshot = _required_target_feature_current_row_mapping(row, "factor_snapshot_json")
    intent = _required_target_feature_current_row_mapping(row, "intent_json")
    resolution = _required_target_feature_current_row_mapping(row, "resolution_json")
    intent_id = _required_target_feature_nested_text(intent, parent="intent_json", field="intent_id")
    event_id = _required_target_feature_nested_text(intent, parent="intent_json", field="event_id")
    _required_target_feature_nested_text(resolution, parent="resolution_json", field="status")
    for field in ("reason_codes", "candidate_ids", "lookup_keys"):
        _required_target_feature_nested_list(resolution, parent="resolution_json", field=field)
    latest_event_received_at_ms = _required_target_feature_current_row_int(row, "latest_event_received_at_ms")
    last_scored_at_ms = _required_target_feature_current_row_int(row, "last_scored_at_ms")
    source_event_ids = _required_target_feature_current_row_string_list(row, "source_event_ids_json", allow_empty=False)
    source_intent_ids = _required_target_feature_current_row_string_list(
        row, "source_intent_ids_json", allow_empty=False
    )
    _required_target_feature_current_row_string_list(row, "source_resolution_ids_json", allow_empty=True)
    if event_id not in source_event_ids:
        raise RuntimeError("token_radar_target_feature_current_row_invalid:source_event_ids_json")
    if intent_id not in source_intent_ids:
        raise RuntimeError("token_radar_target_feature_current_row_invalid:source_intent_ids_json")
    target_type = row.get("target_type")
    target_id = row.get("target_id")
    subject = factor_snapshot.get("subject") if isinstance(factor_snapshot, dict) else {}
    data_health = factor_snapshot.get("data_health") if isinstance(factor_snapshot, dict) else {}
    subject_map = _dict(subject)
    market_target_fields = _market_target_fields_from_subject(target_type=target_type, subject=subject_map)
    return {
        "row_id": _stable_id(
            "token-radar-row",
            projection_version,
            window,
            str(venue),
            lane,
            target_type_key,
            identity_id,
        ),
        "source_max_received_at_ms": latest_event_received_at_ms,
        "lane": lane,
        "rank": 0,
        "venue": str(venue),
        "intent_id": intent_id,
        "event_id": event_id,
        "target_type_key": target_type_key,
        "identity_id": identity_id,
        "target_type": target_type,
        "target_id": target_id,
        "pricefeed_id": row.get("pricefeed_id"),
        **market_target_fields,
        "intent_json": intent,
        "factor_snapshot_json": factor_snapshot,
        "factor_version": factor_snapshot.get("schema_version") if isinstance(factor_snapshot, dict) else None,
        "resolution_json": resolution,
        "decision": (factor_snapshot.get("composite") or {}).get("recommended_decision")
        if isinstance(factor_snapshot, dict)
        else None,
        "data_health_json": {
            "factor_snapshot": "ready",
            "identity": (data_health or {}).get("identity") if isinstance(data_health, dict) else None,
            "market": (data_health or {}).get("market") if isinstance(data_health, dict) else None,
            "social": (data_health or {}).get("social") if isinstance(data_health, dict) else None,
            "alpha": (data_health or {}).get("alpha") if isinstance(data_health, dict) else None,
        },
        "source_event_ids_json": source_event_ids,
        "payload_hash": _rank_change_payload_hash(row),
        "created_at_ms": last_scored_at_ms,
    }


def _patch_ranked_current_row(row: dict[str, Any], ranked: dict[str, Any]) -> dict[str, Any]:
    patched = dict(row)
    factor_snapshot = _factor_snapshot_or_raise(patched)
    families = _dict(factor_snapshot.get("families"))
    factor_ranks = _ranked_factor_ranks(ranked)
    normalization_status = _ranked_row_status(ranked, "normalization_status", RANKED_NORMALIZATION_STATUSES)
    cohort_status = _ranked_row_status(ranked, "cohort_status", RANKED_COHORT_STATUSES)
    cohort_in_cohort = _ranked_row_bool(ranked, "cohort_in_cohort")
    cohort_size = _ranked_row_non_negative_int(ranked, "cohort_size")
    cohort_metadata = _ranked_row_mapping(ranked, "cohort_metadata")
    current_rank = _ranked_row_positive_int(ranked, "rank")
    latest_event_received_at_ms = _ranked_row_non_negative_int(ranked, "latest_event_received_at_ms")
    alpha_rank = _ranked_alpha_rank(ranked, normalization_status)
    _ranked_row_required(ranked, "rank_score")
    _ranked_row_required(ranked, "recommended_decision")
    rank_score = _rank_input_number(ranked, "rank_score")
    recommended_decision = _rank_input_decision(ranked, "recommended_decision")
    for family in TOKEN_RADAR_FACTOR_FAMILIES:
        rank = factor_ranks.get(family)
        if rank is not None and isinstance(families.get(family), dict):
            families[family]["score"] = round(rank * 100.0)
    family_scores = {family: _family_display_score(families.get(family)) for family in TOKEN_RADAR_FACTOR_FAMILIES}
    factor_snapshot["normalization"] = {
        "status": normalization_status,
        "cohort_status": cohort_status,
        "cohort": {
            "in_cohort": cohort_in_cohort,
            "size": cohort_size,
            "definition_version": COHORT_DEFINITION_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            **cohort_metadata,
        },
        "factor_ranks": factor_ranks,
        "alpha_rank": alpha_rank,
    }
    factor_snapshot["composite"]["family_scores"] = family_scores
    factor_snapshot["composite"]["rank_score"] = rank_score
    factor_snapshot["composite"]["recommended_decision"] = recommended_decision
    quality_status, degraded_reasons = _quality_from_factor_snapshot(factor_snapshot)
    patched["factor_snapshot_json"] = factor_snapshot
    patched["decision"] = recommended_decision
    patched["rank_score"] = rank_score
    patched["quality_status"] = quality_status
    patched["degraded_reasons_json"] = degraded_reasons
    patched["rank"] = current_rank
    patched["source_max_received_at_ms"] = latest_event_received_at_ms
    return patched


def _compact_target_id(row: dict[str, Any]) -> str:
    return str(row.get("target_id") or "")


def _rank_input_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _required_projection_row_text(row, "lane"),
        _required_projection_row_text(row, "target_type_key"),
        _required_projection_row_text(row, "identity_id"),
    )


def _required_hydrated_rank_input(
    rows: Mapping[tuple[str, str, str], dict[str, Any]],
    *,
    identity: tuple[str, str, str],
) -> dict[str, Any]:
    try:
        return rows[identity]
    except KeyError as exc:
        raise RuntimeError("token_radar_rank_input_hydration_missing") from exc


def _compact_family_raw_score(row: dict[str, Any], family: str) -> float | None:
    return _float_or_none(row.get(f"{family}_raw_score"))


def _compact_family_weight(row: dict[str, Any], family: str) -> float:
    return _float_or_none(row.get(f"{family}_weight")) or 0.0


def _factor_snapshot_or_raise(row: dict[str, Any]) -> dict[str, Any]:
    factor_snapshot = row.get("factor_snapshot_json")
    return require_token_factor_snapshot(factor_snapshot, field_name="factor_snapshot_json")


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _quality_from_factor_snapshot(snapshot: dict[str, Any]) -> tuple[str, list[str]]:
    data_health = _dict(snapshot.get("data_health"))
    market = _dict(snapshot.get("market"))
    readiness = _dict(market.get("readiness"))
    normalization = _dict(snapshot.get("normalization"))
    reasons: list[str] = []
    status = "ready"

    if data_health.get("identity") == "missing":
        reasons.append("identity_missing")
        status = "insufficient"
    if data_health.get("alpha") == "missing":
        reasons.append("alpha_missing")
        status = "insufficient"

    if readiness.get("anchor_status") != "ready":
        reasons.append("market_anchor_missing")
        if status == "ready":
            status = "degraded"

    latest_status = readiness.get("latest_status")
    if latest_status in {"missing", "stale"}:
        reasons.append(f"market_latest_{latest_status}")
        if status == "ready":
            status = "degraded"

    dex_floor_status = readiness.get("dex_floor_status")
    if dex_floor_status in {"missing_fields", "missing"}:
        reasons.append("dex_floor_missing")
        if status == "ready":
            status = "degraded"
    elif dex_floor_status == "below_floor":
        reasons.append("dex_floor_below")
        if status == "ready":
            status = "degraded"

    if normalization.get("cohort_status") in {"insufficient", "all_tied"}:
        reasons.append("cohort_not_rankable")
        if status == "ready":
            status = "degraded"

    return status, _dedupe_strings(reasons)


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _family_display_score(family: Any) -> int:
    if not isinstance(family, dict):
        return 0
    score = _float_or_none(family.get("score")) or 0.0
    return round(max(0.0, min(100.0, score)))


def _decision_from_score_and_gates(score: int, gates: dict[str, Any]) -> str:
    max_decision = _rank_input_decision(gates, "max_decision")
    if max_decision == "discard":
        return "discard"
    if score >= 70 and max_decision == "high_alert":
        return "high_alert"
    if score >= 35 and max_decision in {"watch", "high_alert"}:
        return "watch"
    return "discard"


def compute_token_radar_target_projection(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Pure spawn-safe reducer for one target-window source-edge closure."""

    window = str(payload["window"])
    now_ms = int(payload["now_ms"])
    target_type = str(payload["target_type"])
    target_id = str(payload["target_id"])
    source_rows = [dict(row) for row in payload["source_rows"]]
    window_ms = _window_ms(window)
    score_since_ms = now_ms - window_ms
    projected = _project_group(
        source_rows,
        now_ms=now_ms,
        window=window,
        score_since_ms=score_since_ms,
        window_ms=window_ms,
        total_window_events=len(
            {str(row["event_id"]) for row in source_rows if int(row.get("received_at_ms") or 0) >= score_since_ms}
        ),
    )
    if projected is None:
        return {
            "target_type": target_type,
            "target_id": target_id,
            "window": window,
            "feature": None,
            "projected": None,
            "target_venue": TOKEN_RADAR_DEFAULT_VENUE,
            "source_rows": len(source_rows),
        }
    feature = token_radar_target_feature_payload(
        projected,
        projection_version=PROJECTION_VERSION,
        window=window,
        computed_at_ms=now_ms,
    )
    return {
        "target_type": target_type,
        "target_id": target_id,
        "window": window,
        "feature": feature,
        "projected": projected,
        "target_venue": token_radar_venue_for_rank_input(feature),
        "source_rows": len(source_rows),
    }


def rank_token_radar_closure(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure cross-sectional ranking after replacing one target feature in memory."""

    target_type = str(payload["target_type"])
    target_id = str(payload["target_id"])
    window = str(payload["window"])
    now_ms = int(payload["now_ms"])
    feature = payload.get("feature")
    compact_inputs = [
        dict(row)
        for row in payload["compact_inputs"]
        if (
            str(row.get("target_type_key") or ""),
            str(row.get("identity_id") or ""),
        )
        != (target_type, target_id)
    ]
    if isinstance(feature, dict):
        compact_inputs.append(dict(feature))
    cutoff_ms = now_ms - _window_ms(window)
    current_inputs = [row for row in compact_inputs if _rank_input_latest_event_received_at_ms(row) >= cutoff_ms]
    selected_by_venue: dict[str, list[dict[str, Any]]] = {}
    source_rows_by_venue: dict[str, int] = {}
    for venue in dict.fromkeys(str(item) for item in payload["venues"]):
        venue_inputs = (
            current_inputs
            if venue == TOKEN_RADAR_DEFAULT_VENUE
            else [row for row in current_inputs if token_radar_venue_for_rank_input(row) == venue]
        )
        ranked = rank_compact_inputs(venue_inputs)
        selected_by_venue[venue] = _select_top_ranked_by_lane(
            ranked,
            limit=int(payload["rank_limit"]),
        )
        source_rows_by_venue[venue] = len(venue_inputs)
    selected_identities = list(
        dict.fromkeys(_rank_input_identity(row) for selected in selected_by_venue.values() for row in selected)
    )
    return {
        "selected_by_venue": selected_by_venue,
        "selected_identities": [list(identity) for identity in selected_identities],
        "source_rows_by_venue": source_rows_by_venue,
    }


def build_token_radar_current_closure(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Pure hydration of selected rank inputs into stable current rows."""

    hydrated = [dict(row) for row in payload["hydrated_inputs"]]
    feature = payload.get("feature")
    if isinstance(feature, dict):
        hydrated.append(dict(feature))
    hydrated_by_identity = {_rank_input_identity(row): row for row in hydrated}
    rows_by_venue: dict[str, list[dict[str, Any]]] = {}
    for venue, selected in dict(payload["selected_by_venue"]).items():
        rows_by_venue[str(venue)] = [
            _patch_ranked_current_row(
                _row_from_target_feature(
                    _required_hydrated_rank_input(
                        hydrated_by_identity,
                        identity=_rank_input_identity(dict(row)),
                    ),
                    venue=str(venue),
                ),
                dict(row),
            )
            for row in selected
        ]
    return {"rows_by_venue": rows_by_venue}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "PROJECTION_VERSION",
    "TokenRadarProjectionWindowError",
    "build_token_radar_current_closure",
    "compute_token_radar_target_projection",
    "rank_compact_inputs",
    "rank_token_radar_closure",
    "token_radar_venue_for_rank_input",
]
