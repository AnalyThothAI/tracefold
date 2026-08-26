"""Projections in, eligible candidates out. Pure functions over rows — no database, no clock, no network.

The News package owns the two reads; this module owns what may become a case. Keeping the rules here
rather than in SQL is deliberate: the funnel report and the eligibility check must be the same code, or
the report eventually describes a filter the scanner no longer applies.

Eligibility fails closed on everything it cannot prove. An unknown asset class, a symbol that
canonicalises to nothing, a verdict with two primaries, a missing rank — all of them are a named
rejection, never a default.

**Age is not one of those rules (#211).** `oi_candidate` and `news_candidate` answer "may this row be
used at all", which is the *context* question: an hour-old verdict is perfectly good point-in-time
context to freeze into a manifest. Whether a row is fresh enough to *create* a case on its own is a
separate question with a separate budget, and `is_fresh_trigger` is the whole of it. Conflating the
two is what made the configured 60 m / 30 m counterpart lookbacks unreachable: both sides were
re-checked against the 5 m trigger budget, so nothing older than 5 m could ever be attached.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from ..contracts import (
    TRADING_MANIFEST_VERSION,
    LiquidationCandidateRow,
    LiquidationSourceContract,
    LiquidationTradeCandidate,
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
    """Two independent time budgets, and they are not interchangeable.

    `max_age_ms` gates a **trigger**: how new a row must be to create a case now. `news_lookback_ms`
    and `oi_lookback_ms` gate a **counterpart**: how far back of the trigger's own cutoff the other
    lane may be read for context. A counterpart is never required to be fresh — it is required to be
    older than the trigger and inside its own lookback.
    """

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
    """One counter per named rejection, plus the survivors. This is the report and the rule at once.

    Counted per **scan**, not per distinct row. The scanner keeps no cursor, so one verdict inside the
    context horizon is re-read every turn and contributes to `news_rows` — and to `news_context_only`
    if it is too old to trigger — on each of them. The key set is what is bounded and what an operator
    reads; the magnitudes are a function of the poll interval and the horizon, and #211 widened the
    horizon from `max_age x 3` to `max_age + max(lookback)`, so they stepped up with it.
    """

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


def is_fresh_trigger(at_ms: object, *, now_ms: int, policy: EligibilityPolicy = DEFAULT_ELIGIBILITY) -> bool:
    """Whether one eligible row is new enough to create a case on its own.

    The single implementation of trigger freshness. The scanner splits its rows with it and nothing
    else applies an age rule, so the funnel's `*_context_only` counters and the trigger set cannot
    describe different windows. A row stamped slightly ahead of this process's clock is fresh, exactly
    as it was before the split: clock skew between the writer and the runner must not silently
    demote a brand-new trigger to context.
    """

    parsed = _int(at_ms)
    return parsed is not None and now_ms - parsed <= policy.max_age_ms


# ---------------------------------------------------------------------------- OI
def oi_candidate(row: OiCandidateRow, *, funnel: Funnel | None = None) -> OiTradeCandidate | Rejected:
    """One projected telemetry fact, or a named source-contract failure. No clock, no policy (#264).

    This is the **source** stage and nothing else: is the row a usable, live, current-generation OI
    fact at all? Rank, the liquidity floor, the deny list, freshness, cooldown and idempotency are the
    Candidate Gate's, and they used to be here as well as in News's SELECT — which is how the same
    threshold came to be executed in three places and a rejection came to be indistinguishable from a
    row that never existed.

    Everything it cannot prove is still a named rejection, never a default. `rank_in_window` is read
    here because the candidate carries it, not because a ceiling is applied to it.
    """

    def _no(rule: str, symbol: str = "") -> Rejected:
        if funnel is not None:
            funnel.count(f"oi_reject:{rule}")
        return Rejected(rule=rule, symbol=symbol)

    symbol = canonical_base_symbol(row.get("symbol"))
    if not symbol:
        return _no("symbol_not_canonicalisable")
    # The reader's push/drop is carried onto the candidate as audit and is not an admission (#264): its
    # rule is `whale_oi_ratio > 80%`, and gating capital on it meant a reader policy edit opened or
    # closed the trading lane without anyone deciding that it should.
    if str(row.get("ingest_mode") or "") != "live":
        return _no("not_live_ingest", symbol)

    observed = _int(row.get("observed_at_ms"))
    if observed is None:
        return _no("observed_at_missing", symbol)

    verdict_at = _int(row.get("verdict_created_at_ms"))
    if verdict_at is None:
        return _no("verdict_time_missing", symbol)

    rank = _int(row.get("rank_in_window"))
    if rank is None:
        return _no("rank_missing", symbol)

    direction = str(row.get("direction") or "").strip().lower()
    if direction not in ("rise", "fall"):
        return _no("oi_direction_unknown", symbol)

    # The frame's `source` is provider text. It is carried on the candidate as-is because the research
    # keys on it, but the funnel is one bounded JSONB document in one row — an arbitrary provider
    # string there would make its key set unbounded, so the counter uses a closed vocabulary.
    venue = str(row.get("venue") or "").strip().lower()
    try:
        candidate = OiTradeCandidate(
            event_id=str(row.get("event_id") or ""),
            observed_at_ms=observed,
            verdict_created_at_ms=verdict_at,
            base_symbol=symbol,
            venue=venue,
            oi_direction=direction,
            oi_change_bps=_int(row.get("oi_change_bps"), 0) or 0,
            oi_value_usd=_int(row.get("oi_value_usd"), 0) or 0,
            whale_long_profit_bps=_int(row.get("whale_long_profit_bps"), 0) or 0,
            whale_oi_ratio_bps=_int(row.get("whale_oi_ratio_bps"), 0) or 0,
            rank_in_window=rank,
            final_decision=str(row.get("final_decision") or ""),
            source_rule=str(row.get("source_rule") or ""),
            metric_version=str(row.get("metric_version") or ""),
            # Carried, never defaulted. A frame whose measurement window the provider contract could
            # not prove reaches the strategy as `None`, and the strategy refuses it by name (#265).
            source_strategy_id=(str(row["source_strategy_id"]) if row.get("source_strategy_id") else None),
            source_contract_version=(
                str(row["source_contract_version"]) if row.get("source_contract_version") else None
            ),
            measurement_window_ms=_int(row.get("measurement_window_ms")),
            learning_epoch=str(row.get("learning_epoch") or ""),
            program_version=str(row.get("program_version") or ""),
            program_sha256=str(row.get("program_sha256") or ""),
            policy_version=str(row.get("policy_version") or ""),
            editorial_origin=str(row.get("editorial_origin") or ""),
            editorial_sha256=str(row.get("editorial_sha256") or ""),
            scored_judgment_sha256=str(row.get("scored_judgment_sha256") or ""),
            runtime_manifest_sha=str(row.get("runtime_manifest_sha") or ""),
        )
    except ValidationError:
        # The Program, policy, epoch and editorial origin are `Literal`s on the candidate, so a row from
        # a retired generation raises here rather than being frozen into a manifest that claims to be
        # current. It used to raise out of the scan turn entirely; now it is one named source failure.
        return _no("generation_invalid", symbol)

    if funnel is not None:
        funnel.count("oi_eligible")
        funnel.count(f"oi_eligible_venue:{venue if venue in _KNOWN_VENUES else 'other'}")
    return candidate


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
    """One projected model verdict, or a named rejection. Every field is knowable at the verdict cutoff.

    Eligible as *context*. Whether it is new enough to trigger a case of its own is `is_fresh_trigger`.
    """

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


def liquidation_candidate(
    row: LiquidationCandidateRow,
    *,
    now_ms: int,
    blacklist: Blacklist,
    funnel: Funnel | None = None,
) -> LiquidationTradeCandidate | Rejected:
    """One typed forced-flow fact. Its forced side is preserved, never promoted to a forecast."""

    def _no(rule: str, symbol: str = "") -> Rejected:
        if funnel is not None:
            funnel.count(f"liquidation_reject:{rule}")
        return Rejected(rule=rule, symbol=symbol)

    symbol = canonical_base_symbol(row.get("symbol"))
    if not symbol:
        return _no("symbol_not_canonicalisable")
    if str(row.get("ingest_mode") or "") != "live":
        return _no("not_live_ingest", symbol)
    venue = str(row.get("venue") or "").strip().lower()
    position_side = str(row.get("liquidated_position_side") or "").strip().lower()
    forced_side = str(row.get("forced_order_side") or "").strip().lower()
    if venue not in _KNOWN_VENUES:
        return _no("venue_unknown", symbol)
    if (position_side, forced_side) not in {("short", "buy"), ("long", "sell")}:
        return _no("side_semantics_invalid", symbol)
    blocked = blacklist.blocked(symbol, now_ms=now_ms)
    if blocked is not None:
        return _no(blacklist_rule(blocked.reason), symbol)
    try:
        candidate = LiquidationTradeCandidate(
            source_key=str(row.get("source_key") or ""),
            item_id=str(row.get("item_id") or ""),
            fact_id=str(row.get("fact_id") or ""),
            base_symbol=symbol,
            venue=venue,
            liquidated_position_side=position_side,
            forced_order_side=forced_side,
            notional_usd=Decimal(str(row.get("notional_usd"))),
            quantity=(Decimal(str(row["quantity"])) if row.get("quantity") is not None else None),
            price=Decimal(str(row.get("price"))),
            event_at_ms=int(row.get("event_at_ms") or 0),
            received_at_ms=int(row.get("received_at_ms") or 0),
            parser_version=str(row.get("parser_version") or ""),
            source_contract=LiquidationSourceContract(
                provider_record_identity=str(row.get("provider_record_identity") or ""),
                symbol_contract_identity=str(row.get("symbol_contract_identity") or ""),
                position_side_semantics=str(row.get("position_side_semantics") or ""),
                quantity_semantics=str(row.get("quantity_semantics") or ""),
                notional_semantics=str(row.get("notional_semantics") or ""),
                price_semantics=str(row.get("price_semantics") or ""),
                completeness_assumption=str(row.get("completeness_assumption") or ""),
                throttle_assumption=str(row.get("throttle_assumption") or ""),
                source_contract_version=str(row.get("source_contract_version") or ""),
                complete=bool(row.get("source_contract_complete")),
            ),
        )
    except (InvalidOperation, TypeError, ValueError, ValidationError):
        return _no("typed_fact_invalid", symbol)
    if candidate.received_at_ms < candidate.event_at_ms:
        return _no("timestamp_order_invalid", symbol)
    if funnel is not None:
        funnel.count("liquidation_eligible")
        funnel.count(f"liquidation_eligible_venue:{candidate.venue}")
    return candidate


def _uses_current_news_generation(raw: object) -> bool:
    """Whether an untrusted persisted manifest names the one executable News generation."""

    if not isinstance(raw, Mapping) or raw.get("manifest_version") != TRADING_MANIFEST_VERSION:
        return False
    contexts = raw.get("contexts")
    if not isinstance(contexts, Mapping):
        return False
    sources = (("oi", "news_oi_signal_v1"), ("news", "news_semantic_program_v5"))
    found = False
    for source_field, expected_program in sources:
        source = contexts.get(source_field)
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
    "is_fresh_trigger",
    "liquidation_candidate",
    "news_candidate",
    "oi_candidate",
]
