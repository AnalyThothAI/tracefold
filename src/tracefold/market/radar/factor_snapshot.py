from __future__ import annotations

import math
from typing import Any

from tracefold.market.radar.constants import (
    TOKEN_FACTOR_SNAPSHOT_VERSION,
    TOKEN_RADAR_FACTOR_FAMILIES,
)
from tracefold.market.radar.scoring_common import (
    clamp_score,
    log_points,
    ratio_points,
    safe_float,
)

FACTOR_FAMILIES = TOKEN_RADAR_FACTOR_FAMILIES

DEX_HIGH_ALERT_FLOORS = {
    "holders": 100,
    "liquidity_usd": 25_000.0,
    "market_cap_usd": 50_000.0,
    "unique_authors": 3,
    "duplicate_text_share": 0.50,
    "top_author_share": 0.65,
}

_DEX_FLOOR_REASONS = {
    "holders": "holders_below_high_alert_floor",
    "liquidity_usd": "liquidity_below_high_alert_floor",
    "market_cap_usd": "market_cap_below_high_alert_floor",
}

_FAMILY_WEIGHTS = {
    "social_heat": 0.55,
    "social_propagation": 0.45,
    "timing_risk": 0.0,
}
_ALPHA_RANK_FAMILIES = ("social_heat", "social_propagation")

_PRE_SOCIAL_CHASE_RISK_PCT = 0.10
_POST_SOCIAL_LATE_RISK_PCT = 0.20


def build_token_factor_snapshot(
    *,
    target: dict[str, Any],
    attention: dict[str, Any],
    social_quality: dict[str, Any],
    market: dict[str, Any],
    timing: dict[str, Any],
    source_event_ids: list[str],
    computed_at_ms: int,
) -> dict[str, Any]:
    subject = _subject(target=target, market=market)
    identity_health = _identity_health(subject)
    market_health = _market_health(subject=subject, market=market)
    alpha_health = _alpha_health(
        attention=attention,
        social_quality=social_quality,
        timing=timing,
        market=market,
    )
    families = {
        "social_heat": _social_heat_family(attention=attention),
        "social_propagation": _social_propagation_family(social_quality=social_quality),
        "timing_risk": _timing_risk_family(timing=timing, market=market),
    }
    gates = _gates(
        subject=subject,
        attention=attention,
        social_quality=social_quality,
        families=families,
        market=market,
        alpha_health=alpha_health,
        raw_alpha_score=_raw_alpha_score(families),
    )
    return {
        "schema_version": TOKEN_FACTOR_SNAPSHOT_VERSION,
        "subject": subject,
        "market": market,
        "gates": gates,
        "data_health": {
            "identity": identity_health,
            "market": market_health,
            "social": _social_health(attention=attention, social_quality=social_quality),
            "alpha": alpha_health,
        },
        "families": families,
        "normalization": {
            "status": "pending_cross_section",
            "cohort_status": "pending_cross_section",
            "cohort": {},
            "factor_ranks": {family: None for family in FACTOR_FAMILIES},
            "alpha_rank": None,
        },
        "composite": _composite(families=families, gates=gates),
        "provenance": {
            "source_event_ids": _dedupe_strings(source_event_ids),
            "computed_at_ms": _computed_at_ms(computed_at_ms),
        },
    }


def _social_heat_family(*, attention: dict[str, Any]) -> dict[str, Any]:
    mentions_5m = _optional_int(attention.get("mentions_5m"))
    mentions_1h = _optional_int(attention.get("mentions_1h"))
    mentions_4h = _optional_int(attention.get("mentions_4h"))
    mentions_24h = _optional_int(attention.get("mentions_24h"))
    unique_authors = _optional_int(attention.get("unique_authors"))
    weighted_mentions = _optional_float(attention.get("weighted_mentions"))
    attention_acceleration = _optional_float(attention.get("attention_acceleration"))
    z_score = _optional_float(attention.get("z_score"))
    z_ewma = _optional_float(attention.get("z_ewma"))
    robust_z = _optional_float(attention.get("robust_z"))
    new_burst_score = _optional_float(attention.get("new_burst_score"))
    attention_surprise = _first_float(attention, ("robust_z", "z_ewma", "z_score"))
    facts = {
        "mentions_5m": mentions_5m,
        "mentions_1h": _count_int(mentions_1h),
        "mentions_4h": _count_int(mentions_4h),
        "mentions_24h": _count_int(mentions_24h),
        "mentions_window": _optional_int(attention.get("mentions_window")),
        "previous_mentions": _optional_int(attention.get("previous_mentions")),
        "mention_delta": _optional_int(attention.get("mention_delta")),
        "mention_delta_pct": _optional_float(attention.get("mention_delta_pct")),
        "stream_share": _optional_float(attention.get("stream_share")),
        "weighted_mentions": weighted_mentions,
        "unique_authors": _count_int(unique_authors),
        "attention_surprise": attention_surprise,
        "attention_acceleration": attention_acceleration,
        "z_score": z_score,
        "z_ewma": z_ewma,
        "robust_z": robust_z,
        "new_burst_score": new_burst_score,
        "baseline_status": _optional_str(attention.get("baseline_status")),
        "baseline_sample_count": _optional_int(attention.get("baseline_sample_count")),
        "baseline_nonzero_sample_count": _optional_int(attention.get("baseline_nonzero_sample_count")),
        "zero_slot_count": _optional_int(attention.get("zero_slot_count")),
        "latest_seen_ms": _optional_int(attention.get("latest_seen_ms")),
    }
    return _family(
        "social_heat",
        facts=facts,
        factors=[
            _z_or_new_burst_factor(z_value=attention_surprise, new_burst_score=new_burst_score),
            _count_factor("social_heat", "source_weighted_mentions", weighted_mentions, scale=3),
            _acceleration_factor(attention_acceleration),
        ],
    )


def _social_propagation_family(*, social_quality: dict[str, Any]) -> dict[str, Any]:
    duplicate_text_share = _optional_float(social_quality.get("duplicate_text_share"))
    top_author_share = _optional_float(social_quality.get("top_author_share"))
    mentions = _optional_int(social_quality.get("mentions"))
    independent_authors = _optional_int(social_quality.get("independent_authors"))
    source_weighted_effective_authors = _optional_float(
        social_quality.get("source_weighted_effective_authors")
        if social_quality.get("source_weighted_effective_authors") is not None
        else social_quality.get("effective_authors")
    )
    time_to_second_author_ms = _optional_int(social_quality.get("time_to_second_author_ms"))
    time_to_third_author_ms = _optional_int(social_quality.get("time_to_third_author_ms"))
    followup_author_count = _optional_int(social_quality.get("followup_author_count"))
    informative_post_count = _optional_int(social_quality.get("informative_post_count"))
    new_authors = _optional_int(social_quality.get("new_authors"))
    reproduction_rate = _optional_float(social_quality.get("reproduction_rate"))
    facts = {
        "duplicate_text_share": duplicate_text_share,
        "top_author_share": top_author_share,
        "mentions": _count_int(mentions),
        "independent_authors": _count_int(independent_authors),
        "informative_post_count": _count_int(informative_post_count),
        "effective_authors": _optional_float(social_quality.get("effective_authors")),
        "new_authors": _count_int(new_authors),
        "reproduction_rate": reproduction_rate,
        "source_weighted_effective_authors": source_weighted_effective_authors,
        "time_to_second_author_ms": time_to_second_author_ms,
        "time_to_third_author_ms": time_to_third_author_ms,
        "followup_author_count": _count_int(followup_author_count),
        "author_entropy": _optional_float(social_quality.get("author_entropy")),
    }
    return _family(
        "social_propagation",
        facts=facts,
        factors=[
            _count_factor("social_propagation", "independent_authors", independent_authors, scale=4),
            _ratio_factor(
                "social_propagation",
                "source_weighted_effective_authors",
                source_weighted_effective_authors,
                max_ratio=5.0,
            ),
            _propagation_speed_factor(time_to_second_author_ms, time_to_third_author_ms),
            _count_factor(
                "social_propagation",
                "followup_authors",
                followup_author_count,
                scale=2,
            ),
            _penalty_factor(
                "social_propagation",
                "duplicate_text_share_penalty",
                raw_value=duplicate_text_share,
                threshold=DEX_HIGH_ALERT_FLOORS["duplicate_text_share"],
                risk_flag="duplicate_text_share_high",
            ),
            _penalty_factor(
                "social_propagation",
                "top_author_concentration_penalty",
                raw_value=top_author_share,
                threshold=DEX_HIGH_ALERT_FLOORS["top_author_share"],
                risk_flag="author_concentration_high",
            ),
        ],
    )


def _timing_risk_family(*, timing: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    event_anchor = _market_event_anchor(market)
    decision_latest = _market_decision_latest(market)
    readiness = _market_readiness(market)
    social_signal_start_ms = (
        timing.get("social_signal_start_ms") or event_anchor.get("received_at_ms") or event_anchor.get("observed_at_ms")
    )
    price_change_before_social_pct = _optional_float(timing.get("price_change_before_social_pct"))
    price_change_since_social_pct = _optional_float(
        timing.get("price_change_since_social_pct")
        if timing.get("price_change_since_social_pct") is not None
        else _market_price_change_since_anchor(decision_latest, event_anchor)
    )
    price_change_status = _optional_str(readiness.get("latest_status") or readiness.get("anchor_status"))
    facts = {
        "price_change_before_social_pct": price_change_before_social_pct,
        "price_change_since_social_pct": price_change_since_social_pct,
        "social_signal_start_ms": _optional_int(social_signal_start_ms),
        "price_change_status": price_change_status,
    }
    if price_change_status == "live_not_persisted":
        return {
            "raw_score": 0,
            "score": 0,
            "weight": _FAMILY_WEIGHTS["timing_risk"],
            "data_health": "anchor_only",
            "facts": facts,
            "factors": {},
        }
    return _family(
        "timing_risk",
        facts=facts,
        factors=[
            _timing_risk_factor(
                "pre_social_chase_risk",
                price_change_before_social_pct,
                threshold=_PRE_SOCIAL_CHASE_RISK_PCT,
                risk_flag="timing_chase_risk",
            ),
            _timing_risk_factor(
                "post_social_late_risk",
                price_change_since_social_pct,
                threshold=_POST_SOCIAL_LATE_RISK_PCT,
                risk_flag="timing_late_risk",
            ),
        ],
    )


def _gates(
    *,
    subject: dict[str, Any],
    attention: dict[str, Any],
    social_quality: dict[str, Any],
    families: dict[str, dict[str, Any]],
    market: dict[str, Any],
    alpha_health: str,
    raw_alpha_score: int,
) -> dict[str, Any]:
    blocked_reasons: list[str] = []
    risk_reasons: list[str] = []
    discard_cap_reasons: list[str] = []
    if _identity_unresolved(subject):
        blocked_reasons.append("identity_unresolved")
        discard_cap_reasons.append("identity_unresolved")
    if alpha_health == "missing":
        blocked_reasons.append("alpha_data_missing")
        discard_cap_reasons.append("alpha_data_missing")
    readiness = _market_readiness(market)
    anchor_status = str(readiness.get("anchor_status") or "").lower()
    latest_status = str(readiness.get("latest_status") or "").lower()
    if anchor_status != "ready":
        blocked_reasons.append("market_anchor_missing")
    if latest_status in {"", "missing"}:
        blocked_reasons.append("market_latest_missing")
    elif latest_status == "stale":
        blocked_reasons.append("market_latest_stale")
    if subject["target_market_type"] == "dex":
        metadata_missing = False
        floor_below = False
        decision_latest = _market_decision_latest(market)
        for key, reason in _DEX_FLOOR_REASONS.items():
            value = _optional_float(decision_latest.get(key))
            if value is None:
                metadata_missing = True
                blocked_reasons.append(f"{key}_unverified")
                continue
            if _is_below(value, key):
                floor_below = True
                blocked_reasons.append(reason)
        if metadata_missing:
            blocked_reasons.append("dex_floor_missing")
            risk_reasons.append("market_metadata_missing")
        if floor_below:
            blocked_reasons.append("dex_floor_below")
    independent_sources = max(
        _count_int(attention.get("unique_authors")),
        _count_int(social_quality.get("independent_authors")),
    )
    credible_sources = _optional_float(social_quality.get("source_weighted_effective_authors"))
    if credible_sources is None:
        credible_sources = _optional_float(social_quality.get("effective_authors"))
    if independent_sources < 2:
        blocked_reasons.append("insufficient_independent_social_sources")
        risk_reasons.append("thin_author_set")
    if credible_sources is not None and credible_sources < 1.5:
        blocked_reasons.append("insufficient_credible_social_sources")
        risk_reasons.append("thin_credible_author_set")
    if _is_at_or_above(_optional_float(social_quality.get("duplicate_text_share")), "duplicate_text_share"):
        blocked_reasons.append("duplicate_text_share_high")
        risk_reasons.append("duplicate_text_share_high")
    if _is_at_or_above(_optional_float(social_quality.get("top_author_share")), "top_author_share"):
        risk_reasons.append("author_concentration_high")
    timing_family = families.get("timing_risk") or {}
    timing_factors = timing_family.get("factors") or {}
    if isinstance(timing_factors, dict):
        for factor in timing_factors.values():
            if isinstance(factor, dict):
                risk_reasons.extend(factor.get("risk_flags") or [])

    blocked_reasons = _dedupe_strings(blocked_reasons)
    if discard_cap_reasons:
        max_decision = "discard"
    else:
        max_decision = ("watch" if raw_alpha_score >= 35 else "discard") if blocked_reasons else "high_alert"
    return {
        "eligible_for_high_alert": not blocked_reasons,
        "max_decision": max_decision,
        "blocked_reasons": blocked_reasons,
        "risk_reasons": _dedupe_strings(risk_reasons),
    }


def _composite(*, families: dict[str, dict[str, Any]], gates: dict[str, Any]) -> dict[str, Any]:
    family_scores = {family: _count_int(families[family]["score"]) for family in FACTOR_FAMILIES}
    raw_alpha_score = _raw_alpha_score(families)
    if raw_alpha_score >= 70:
        recommended_decision = "high_alert"
    elif raw_alpha_score >= 35:
        recommended_decision = "watch"
    else:
        recommended_decision = "discard"
    recommended_decision = _cap_decision(recommended_decision, str(gates["max_decision"]))
    return {
        "raw_alpha_score": raw_alpha_score,
        "rank_score": raw_alpha_score,
        "family_scores": family_scores,
        "recommended_decision": recommended_decision,
    }


def _raw_alpha_score(families: dict[str, dict[str, Any]]) -> int:
    total_weight = sum(safe_float(families[family].get("weight")) for family in _ALPHA_RANK_FAMILIES)
    if total_weight <= 0:
        return 0
    return clamp_score(
        sum(
            safe_float(families[family].get("score")) * safe_float(families[family].get("weight"))
            for family in _ALPHA_RANK_FAMILIES
        )
        / total_weight
    )


def _cap_decision(decision: str, max_decision: str) -> str:
    priority = {"discard": 0, "watch": 1, "high_alert": 2}
    if priority.get(decision, 0) <= priority.get(max_decision, 2):
        return decision
    return max_decision if max_decision in priority else "discard"


def _family(
    family: str,
    *,
    facts: dict[str, Any],
    factors: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_score = _factor_sum(factors)
    return {
        "raw_score": raw_score,
        "score": clamp_score(raw_score),
        "weight": _FAMILY_WEIGHTS[family],
        "data_health": _family_data_health(factors),
        "facts": facts,
        "factors": {str(factor["key"]): factor for factor in factors},
    }


def _factor_sum(factors: list[dict[str, Any]]) -> int:
    scores = [_finite_score(factor.get("score")) for factor in factors]
    positive_scores = [score for score in scores if score > 0]
    penalty = sum(score for score in scores if score < 0)
    positive_score = sum(positive_scores) / len(positive_scores) if positive_scores else 0.0
    return clamp_score(positive_score + penalty)


def _factor_point(
    family: str,
    key: str,
    *,
    raw_value: Any,
    score: float,
    confidence: float = 0.95,
    data_health: str | None = None,
    risk_flags: list[str] | None = None,
) -> dict[str, Any]:
    health = data_health or ("missing" if raw_value is None else "ready")
    return {
        "family": family,
        "key": key,
        "raw_value": raw_value,
        "score": round(_finite_score(score), 4),
        "confidence": round(max(0.0, min(1.0, _finite_score(confidence))), 4),
        "data_health": health,
        "risk_flags": _dedupe_strings(risk_flags or []),
    }


def _count_factor(family: str, key: str, value: Any, *, scale: float) -> dict[str, Any]:
    return _factor_point(
        family,
        key,
        raw_value=value,
        score=log_points(safe_float(value), scale=scale, max_points=100.0),
        confidence=0.95 if value is not None else 0.0,
    )


def _ratio_factor(
    family: str,
    key: str,
    value: float | None,
    *,
    max_ratio: float = 1.0,
) -> dict[str, Any]:
    return _factor_point(
        family,
        key,
        raw_value=value,
        score=ratio_points(safe_float(value), max_ratio=max_ratio, max_points=100.0),
        confidence=0.9 if value is not None else 0.0,
    )


def _penalty_factor(
    family: str,
    key: str,
    *,
    raw_value: float | None,
    threshold: float,
    risk_flag: str,
) -> dict[str, Any]:
    risk_flags = [risk_flag] if raw_value is not None and raw_value >= threshold else []
    penalty = (
        0.0 if raw_value is None else -min(100.0, max(0.0, raw_value - threshold) / max(0.01, 1.0 - threshold) * 100.0)
    )
    return _factor_point(
        family,
        key,
        raw_value=raw_value,
        score=penalty,
        confidence=0.9 if raw_value is not None else 0.0,
        risk_flags=risk_flags,
    )


def _z_or_new_burst_factor(*, z_value: float | None, new_burst_score: float | None) -> dict[str, Any]:
    if z_value is not None:
        raw_value: Any = z_value
        score = max(0.0, min(100.0, 25.0 + z_value * 22.5))
    elif new_burst_score is not None:
        raw_value = new_burst_score
        score = log_points(new_burst_score, scale=2.0, max_points=80.0)
    else:
        raw_value = None
        score = 0.0
    return _factor_point(
        "social_heat",
        "attention_surprise",
        raw_value=raw_value,
        score=score,
        confidence=0.95 if raw_value is not None else 0.0,
    )


def _acceleration_factor(value: float | None) -> dict[str, Any]:
    return _factor_point(
        "social_heat",
        "attention_acceleration",
        raw_value=value,
        score=log_points(safe_float(value), scale=2, max_points=100.0),
        confidence=0.9 if value is not None else 0.0,
    )


def _propagation_speed_factor(second_ms: int | None, third_ms: int | None) -> dict[str, Any]:
    if second_ms is None:
        raw_value = None
        score = 0.0
        confidence = 0.0
    else:
        raw_value = {"time_to_second_author_ms": second_ms, "time_to_third_author_ms": third_ms}
        second_minutes = second_ms / 60_000
        second_score = max(0.0, 100.0 - min(60.0, second_minutes) / 60.0 * 60.0)
        if third_ms is None:
            third_score = 0.0
        else:
            third_minutes = third_ms / 60_000
            third_score = max(0.0, 100.0 - min(60.0, third_minutes) / 60.0 * 40.0)
        score = second_score * 0.65 + third_score * 0.35
        confidence = 0.9
    return _factor_point(
        "social_propagation",
        "propagation_speed",
        raw_value=raw_value,
        score=score,
        confidence=confidence,
    )


def _timing_risk_factor(key: str, value: float | None, *, threshold: float, risk_flag: str) -> dict[str, Any]:
    risk_flags = [risk_flag] if value is not None and value >= threshold else []
    score = 0.0 if value is None else -min(100.0, max(0.0, value - threshold) / max(0.01, threshold) * 50.0)
    return _factor_point(
        "timing_risk",
        key,
        raw_value=value,
        score=score,
        confidence=0.8 if value is not None else 0.0,
        risk_flags=risk_flags,
    )


def _first_float(source: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _optional_float(source.get(key))
        if value is not None:
            return value
    return None


def _market_event_anchor(market: dict[str, Any]) -> dict[str, Any]:
    value = market.get("event_anchor")
    return value if isinstance(value, dict) else {}


def _market_decision_latest(market: dict[str, Any]) -> dict[str, Any]:
    value = market.get("decision_latest")
    return value if isinstance(value, dict) else {}


def _market_readiness(market: dict[str, Any]) -> dict[str, Any]:
    value = market.get("readiness")
    return value if isinstance(value, dict) else {}


def _market_price_change_since_anchor(decision_latest: dict[str, Any], event_anchor: dict[str, Any]) -> float | None:
    latest_usd = _optional_float(decision_latest.get("price_usd"))
    anchor_usd = _optional_float(event_anchor.get("price_usd"))
    if latest_usd is not None and anchor_usd is not None and anchor_usd != 0:
        return latest_usd / anchor_usd - 1.0
    if decision_latest.get("quote_symbol") and decision_latest.get("quote_symbol") == event_anchor.get("quote_symbol"):
        latest_quote = _optional_float(decision_latest.get("price_quote"))
        anchor_quote = _optional_float(event_anchor.get("price_quote"))
        if latest_quote is not None and anchor_quote is not None and anchor_quote != 0:
            return latest_quote / anchor_quote - 1.0
    return None


def _subject(*, target: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    event_anchor = _market_event_anchor(market)
    decision_latest = _market_decision_latest(market)
    return {
        "target_type": _optional_str(target.get("target_type")),
        "target_id": _optional_str(target.get("target_id")),
        "symbol": _optional_str(target.get("symbol")),
        "target_market_type": _target_market_type(target),
        "chain": _optional_str(target.get("chain")),
        "address": _optional_str(target.get("address")),
        "pricefeed_id": _optional_str(
            target.get("pricefeed_id") or decision_latest.get("pricefeed_id") or event_anchor.get("pricefeed_id")
        ),
    }


def _identity_health(subject: dict[str, Any]) -> str:
    return "missing" if _identity_unresolved(subject) else "ready"


def _market_health(*, subject: dict[str, Any], market: dict[str, Any]) -> str:
    readiness = _market_readiness(market)
    anchor_status = str(readiness.get("anchor_status") or "").lower()
    latest_status = str(readiness.get("latest_status") or "").lower()
    if anchor_status != "ready":
        return "missing"
    if latest_status not in {"live", "fresh"}:
        return "partial"
    if subject["target_market_type"] != "dex":
        return "ready"
    dex_floor_status = str(readiness.get("dex_floor_status") or "").lower()
    if dex_floor_status == "ready":
        return "ready"
    if dex_floor_status in {"missing_fields", "below_floor"}:
        return "partial"
    return "missing"


def _social_health(*, attention: dict[str, Any], social_quality: dict[str, Any]) -> str:
    attention_health = _count_fields_health(
        attention,
        keys=("mentions_5m", "mentions_1h", "mentions_4h", "mentions_24h", "unique_authors"),
    )
    diffusion_health = _count_fields_health(
        social_quality,
        keys=("informative_post_count", "mentions", "independent_authors"),
    )
    if attention_health == "ready" and diffusion_health == "ready":
        return "ready"
    if attention_health == "missing" and diffusion_health == "missing":
        return "missing"
    return "partial"


def _alpha_health(
    *,
    attention: dict[str, Any],
    social_quality: dict[str, Any],
    timing: dict[str, Any],
    market: dict[str, Any],
) -> str:
    if any(
        _count_int(attention.get(key)) > 0
        for key in ("mentions_5m", "mentions_1h", "mentions_4h", "mentions_24h", "unique_authors")
    ):
        return "ready"
    if any(
        _count_int(social_quality.get(key)) > 0 for key in ("informative_post_count", "mentions", "independent_authors")
    ):
        return "ready"
    if _optional_float(social_quality.get("effective_authors")) is not None:
        return "ready"
    if any(
        _optional_float(source.get(key)) is not None
        for source in (timing, market)
        for key in ("price_change_before_social_pct", "price_change_since_social_pct")
    ):
        return "ready"
    return "missing"


def _count_fields_health(source: dict[str, Any], *, keys: tuple[str, ...]) -> str:
    statuses = [_optional_float(source.get(key)) is not None for key in keys]
    if all(statuses):
        return "ready"
    if any(statuses):
        return "partial"
    return "missing"


def _family_data_health(factors: list[dict[str, Any]]) -> str:
    health_values = {str(factor.get("data_health") or "missing") for factor in factors}
    if not health_values or health_values == {"missing"}:
        return "missing"
    if health_values == {"ready"}:
        return "ready"
    return "partial"


def _target_market_type(target: dict[str, Any]) -> str:
    target_type = str(target.get("target_type") or "").lower()
    target_id = str(target.get("target_id") or "").lower()
    if target_type in {"cextoken", "cex_token"} or target_id.startswith("cex_token:"):
        return "cex"
    return "dex"


def _identity_unresolved(subject: dict[str, Any]) -> bool:
    target_type = str(subject.get("target_type") or "").lower()
    target_id = str(subject.get("target_id") or "")
    return not target_type or not target_id or target_type in {"source_seed", "sourceseed", "unresolved"}


def _is_below(value: Any, floor_key: str) -> bool:
    parsed = _optional_float(value)
    if parsed is None:
        return True
    return parsed < safe_float(DEX_HIGH_ALERT_FLOORS[floor_key])


def _is_at_or_above(value: Any, floor_key: str) -> bool:
    if value is None:
        return False
    return safe_float(value) >= safe_float(DEX_HIGH_ALERT_FLOORS[floor_key])


def _finite_score(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return parsed


def _optional_float(value: Any) -> float | None:
    parsed = _finite_number(value)
    return None if parsed is None else float(parsed)


def _optional_int(value: Any) -> int | None:
    parsed = _finite_number(value)
    if parsed is None:
        return None
    return int(parsed)


def _count_int(value: Any, default: int = 0) -> int:
    parsed = _finite_number(value)
    if parsed is None:
        return default
    return int(parsed)


def _computed_at_ms(value: Any) -> int:
    parsed = _finite_number(value)
    if parsed is None:
        return 0
    return int(parsed)


def _finite_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _dedupe_strings(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value is not None and str(value)))
