"""Projections in, eligible candidates out. Pure functions over rows — no database, no clock, no network.

The News package owns the two reads; this module owns what may become a case. Keeping the rules here
rather than in SQL is deliberate: the funnel report and the eligibility check must be the same code, or
the report eventually describes a filter the scanner no longer applies.

Eligibility fails closed on everything it cannot prove. An unknown asset class, a symbol that
canonicalises to nothing, a verdict with two primaries, a missing rank — all of them are a named
rejection, never a default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..contracts import (
    TRADING_MANIFEST_VERSION,
    NewsCandidateRow,
    NewsTradeCandidate,
    OiCandidateRow,
    OiTradeCandidate,
    canonical_base_symbol,
)
from .blacklist import Blacklist

_LIVE_DECISIONS = frozenset({"push", "escalate"})
_KNOWN_VENUES = frozenset({"binance", "hyperliquid"})
# The reasons the code itself writes. An operator's `--reason` is free text and every distinct string
# would become another key in the single funnel document, which is exactly the unbounded key set the
# closed venue vocabulary exists to prevent three lines below.
_KNOWN_BLACKLIST_REASONS = frozenset({"benchmark_large_cap", "commodity_not_target", "blacklist_unavailable"})


def blacklist_rule(reason: str) -> str:
    normalized = str(reason or "").strip().lower()
    return f"blacklisted:{normalized if normalized in _KNOWN_BLACKLIST_REASONS else 'operator'}"


@dataclass(frozen=True, slots=True)
class EligibilityPolicy:
    max_age_ms: int = 300_000
    min_magnitude: int = 2
    max_rank_in_window: int = 2
    min_oi_value_usd: int = 20_000_000
    news_lookback_ms: int = 3_600_000
    oi_lookback_ms: int = 1_800_000
    symbol_cooldown_ms: int = 1_800_000


DEFAULT_ELIGIBILITY = EligibilityPolicy()


@dataclass(slots=True)
class Funnel:
    """One counter per named rejection, plus the survivors. This is the report and the rule at once."""

    stages: dict[str, int] = field(default_factory=dict)

    def count(self, stage: str, amount: int = 1) -> None:
        self.stages[stage] = self.stages.get(stage, 0) + int(amount)

    def as_dict(self) -> dict[str, int]:
        return dict(sorted(self.stages.items()))


@dataclass(frozen=True, slots=True)
class Rejected:
    rule: str
    symbol: str = ""


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------- OI
def oi_candidate(
    row: OiCandidateRow,
    *,
    now_ms: int,
    blacklist: Blacklist,
    policy: EligibilityPolicy = DEFAULT_ELIGIBILITY,
    funnel: Funnel | None = None,
) -> OiTradeCandidate | Rejected:
    """One projected telemetry verdict, or a named rejection."""

    def _no(rule: str, symbol: str = "") -> Rejected:
        if funnel is not None:
            funnel.count(f"oi_reject:{rule}")
        return Rejected(rule=rule, symbol=symbol)

    symbol = canonical_base_symbol(row.get("symbol"))
    if not symbol:
        return _no("symbol_not_canonicalisable")
    if str(row.get("final_decision") or "") not in _LIVE_DECISIONS:
        return _no("not_pushed", symbol)
    if str(row.get("ingest_mode") or "") != "live":
        return _no("not_live_ingest", symbol)

    observed = _int(row.get("observed_at_ms"))
    if observed is None:
        return _no("observed_at_missing", symbol)
    if now_ms - observed > policy.max_age_ms:
        return _no("stale", symbol)

    rank = _int(row.get("rank_in_window"))
    if rank is None or rank > policy.max_rank_in_window:
        return _no("rank_exhausted", symbol)

    oi_value = _int(row.get("oi_value_usd"), 0) or 0
    if oi_value < policy.min_oi_value_usd:
        return _no("oi_value_below_floor", symbol)

    direction = str(row.get("direction") or "").strip().lower()
    if direction not in ("rise", "fall"):
        return _no("oi_direction_unknown", symbol)

    blocked = blacklist.blocked(symbol, now_ms=now_ms)
    if blocked is not None:
        return _no(f"blacklisted:{blocked.reason}", symbol)

    # The frame's `source` is provider text. It is carried on the candidate as-is because the research
    # keys on it, but the funnel is one bounded JSONB document in one row — an arbitrary provider
    # string there would make its key set unbounded, so the counter uses a closed vocabulary.
    venue = str(row.get("venue") or "").strip().lower()
    if funnel is not None:
        funnel.count("oi_eligible")
        funnel.count(f"oi_eligible_venue:{venue if venue in _KNOWN_VENUES else 'other'}")
    return OiTradeCandidate(
        event_id=str(row.get("event_id") or ""),
        observed_at_ms=observed,
        base_symbol=symbol,
        venue=venue,
        oi_direction=direction,
        oi_change_bps=_int(row.get("oi_change_bps"), 0) or 0,
        oi_value_usd=oi_value,
        whale_long_profit_bps=_int(row.get("whale_long_profit_bps"), 0) or 0,
        whale_oi_ratio_bps=_int(row.get("whale_oi_ratio_bps"), 0) or 0,
        rank_in_window=rank,
        metric_version=str(row.get("metric_version") or ""),
        learning_epoch=str(row.get("learning_epoch") or ""),
        program_version=str(row.get("program_version") or ""),
        program_sha256=str(row.get("program_sha256") or ""),
        policy_version=str(row.get("policy_version") or ""),
        editorial_origin=str(row.get("editorial_origin") or ""),
        editorial_sha256=str(row.get("editorial_sha256") or ""),
        scored_judgment_sha256=str(row.get("scored_judgment_sha256") or ""),
        runtime_manifest_sha=str(row.get("runtime_manifest_sha") or ""),
    )


# ---------------------------------------------------------------------------- News
def _primary_symbol(verdict: Mapping[str, Any]) -> tuple[str, int]:
    assets = verdict.get("assets")
    if not isinstance(assets, list | tuple):
        return "", 0
    primaries = [a for a in assets if isinstance(a, Mapping) and str(a.get("role") or "") == "primary"]
    if len(primaries) != 1:
        return "", len(primaries)
    return canonical_base_symbol(primaries[0].get("symbol")), 1


def news_candidate(
    row: NewsCandidateRow,
    *,
    now_ms: int,
    blacklist: Blacklist,
    policy: EligibilityPolicy = DEFAULT_ELIGIBILITY,
    funnel: Funnel | None = None,
) -> NewsTradeCandidate | Rejected:
    """One projected model verdict, or a named rejection. Every field is knowable at the verdict cutoff."""

    def _no(rule: str, symbol: str = "") -> Rejected:
        if funnel is not None:
            funnel.count(f"news_reject:{rule}")
        return Rejected(rule=rule, symbol=symbol)

    verdict = row.get("verdict")
    if not isinstance(verdict, Mapping):
        return _no("verdict_unreadable")

    symbol, primary_count = _primary_symbol(verdict)
    if primary_count != 1:
        return _no("not_exactly_one_primary")
    if not symbol:
        return _no("symbol_not_canonicalisable")

    grounded = row.get("grounded_assets")
    grounded_set = {canonical_base_symbol(item) for item in grounded} if isinstance(grounded, list | tuple) else set()
    if symbol not in grounded_set:
        return _no("primary_not_grounded", symbol)

    if str(row.get("asset_class") or "") != "crypto":
        return _no("asset_class_not_crypto", symbol)

    novelty = str(verdict.get("novelty") or "")
    if novelty == "restatement":
        return _no("restatement", symbol)

    magnitude = _int(verdict.get("magnitude"), 0) or 0
    if magnitude < policy.min_magnitude:
        return _no("magnitude_below_floor", symbol)

    verdict_at = _int(row.get("verdict_created_at_ms"))
    if verdict_at is None:
        return _no("verdict_time_missing", symbol)
    if now_ms - verdict_at > policy.max_age_ms:
        return _no("stale", symbol)

    blocked = blacklist.blocked(symbol, now_ms=now_ms)
    if blocked is not None:
        return _no(blacklist_rule(blocked.reason), symbol)

    decision = str(row.get("final_decision") or "")
    if decision not in _LIVE_DECISIONS:
        return _no("not_pushed", symbol)

    if funnel is not None:
        funnel.count("news_eligible")
    return NewsTradeCandidate(
        event_id=str(row.get("event_id") or ""),
        verdict_created_at_ms=verdict_at,
        opened_at_ms=_int(row.get("opened_at_ms"), verdict_at) or verdict_at,
        base_symbol=symbol,
        evidence_version=_int(row.get("evidence_version"), 0) or 0,
        evidence_sha256=str(row.get("evidence_sha256") or ""),
        focus_fact_id=str(row.get("focus_fact_id") or ""),
        comparison_fingerprint=str(row.get("comparison_fingerprint") or ""),
        source_artifact_id=(str(row["source_artifact_id"]) if row.get("source_artifact_id") else None),
        source_published_at_ms=_int(row.get("source_published_at_ms")),
        final_decision=decision,
        event_type=str(verdict.get("event_type") or ""),
        risk_direction=str(verdict.get("direction") or ""),
        scope=str(verdict.get("scope") or ""),
        magnitude=magnitude,
        novelty=novelty,
        headline_zh=str(verdict.get("headline_zh") or ""),
        why_zh=str(verdict.get("why_zh") or ""),
        learning_epoch=str(row.get("learning_epoch") or ""),
        program_version=str(row.get("program_version") or ""),
        program_sha256=str(row.get("program_sha256") or ""),
        policy_version=str(row.get("policy_version") or ""),
        editorial_origin=str(row.get("editorial_origin") or ""),
        editorial_sha256=str(row.get("editorial_sha256") or ""),
        scored_judgment_sha256=str(row.get("scored_judgment_sha256") or ""),
        runtime_manifest_sha=str(row.get("runtime_manifest_sha") or ""),
    )


def _uses_current_news_generation(raw: object) -> bool:
    """Whether an untrusted persisted manifest names the one executable News generation."""

    if not isinstance(raw, Mapping) or raw.get("manifest_version") != TRADING_MANIFEST_VERSION:
        return False
    sources = (("oi", "news_oi_signal_v1"), ("news", "news_semantic_program_v5"))
    found = False
    for source_field, expected_program in sources:
        source = raw.get(source_field)
        if source is None:
            continue
        found = True
        if (
            not isinstance(source, Mapping)
            or source.get("learning_epoch") != "program_v7"
            or source.get("policy_version") != "news_triage_policy_v10"
            or source.get("program_version") != expected_program
        ):
            return False
    return found


__all__ = [
    "DEFAULT_ELIGIBILITY",
    "EligibilityPolicy",
    "Funnel",
    "Rejected",
    "blacklist_rule",
    "news_candidate",
    "oi_candidate",
]
