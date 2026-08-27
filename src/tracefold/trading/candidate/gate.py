"""The one place a source fact is admitted to the capital lane, and the one vocabulary it refuses in.

Every rule here used to be executed somewhere else as well — the rank ceiling and the liquidity floor
in News's SELECT, the floor again inside the strategy, the venue check in both `_plan` and `_freeze`.
That is what made `oi_rows = 0` unanswerable: a frame filtered out upstream and a frame that never
existed were the same absence, and the counters that could have told them apart lived in one JSONB
document reset every UTC midnight.

**Both trigger kinds are admitted here (#273).** Until then only the OI lane wrote rows: a News
trigger went straight to a frozen Case and was refused by `news_oi_alignment_v1` with
`oi_context_missing`, which put 64 of production's 76 Cases — every single one of them — in the case
table for the sole purpose of recording that no OI frame was beside them. That made the case table
noise, made the console's 成案 bar a number about nothing, and left the News lane as the last double
admission path in the system. A News trigger with no OI context now stops here, `DEFERRED` on
`oi_context_missing`, and the expiry sweep closes it when the frame is past the trigger budget. The
rules themselves stay OI-specific — `admit_context`, `admit_trigger` and `admit_route` all read an
OI frame's own numbers — because a News trigger's own eligibility is already decided by
`news_candidate` before it gets here.

**What this module owns** is whether a source fact may become a *trigger* now:

    source        the row is a usable, current-generation, live OI fact at all
    eligibility   rank, liquidity floor, blacklist, freshness, cooldown, idempotency, one-per-underlying
    routing       the frame's own venue resolves to a native perp this lane may execute
    market_context there is a candle at the cutoff to freeze a mark and a pre-move from
    freeze        the immutable case was written

**What it deliberately does not own** is anything that expresses an opinion about the trade. A frame
that is liquid, routable and priced but whose numbers the strategy dislikes must reach a Case and be
refused there by name, or the manifest never records what was rejected.

Two reasons from #264's taxonomy are deliberately absent:

* `whale_ratio_below_floor`. #265 §4 corrects the original design: the smart-money ratio is an *Alpha*
  threshold, and one strategy's Alpha gate must never delete another strategy's data. The projection
  publishes `whale_oi_ratio_bps`; which side of 50% or 80% matters belongs to a versioned strategy
  config, not to generic source admission.
* `instrument_stale`. There is no catalogue-freshness threshold in this system and inventing one here
  would be a new capital rule with nothing measured behind it. A listing that goes away is marked
  `delisted` by the universe snapshot and filtered by the instrument projection, which is where that
  fact already has an owner.
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from ..contracts import (
    LiveExchangeId,
    NewsTradeCandidate,
    OiTradeCandidate,
    TriggerKind,
    canonical_sha256,
    underlying_key,
)
from .blacklist import Blacklist
from .eligibility import EligibilityPolicy, Rejected
from .routing import signal_exchange_id

# Bumped when a rule is added, removed, or changes what it means. It is half of the durable row's key,
# so a new version re-decides every source rather than inheriting an answer from a rule that is gone.
CANDIDATE_GATE_VERSION: Final = "trading_candidate_gate_v1"

GateStatus = Literal["DEFERRED", "REJECTED", "CASE_CREATED", "EXPIRED"]
GateStage = Literal["source", "eligibility", "routing", "market_context", "freeze"]

# The closed vocabulary. A reason outside this set is a bug, not a new rule: the read model aggregates
# on it and an unbounded key set is exactly what the funnel's venue counter already fails at.
GATE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "source_contract_invalid",
        "source_generation_mismatch",
        "source_not_live",
        "trigger_stale",
        "rank_above_limit",
        "oi_value_below_floor",
        "blacklisted",
        "cooldown",
        "active_underlying",
        "case_in_flight",
        "venue_unresolved",
        "unsupported_venue",
        "no_native_perp",
        "market_data_unavailable",
        "market_data_invalid",
        "already_consumed",
        "superseded_by_newer_trigger",
        # The News lane's one admission rule (#273). `DEFERRED`, because the OI frame it needs may
        # still arrive inside the trigger budget — the sweep is what closes it when none does.
        "oi_context_missing",
        # Not in #264's list, and named rather than left silent. The two order caps and the per-turn
        # case budget refuse a source that passed every rule about the source itself; calling that
        # `active_underlying` would blame the frame's own issuer for the lane being full.
        "lane_capacity_exhausted",
        "case_created",
    }
)

# `oi_candidate` proves the source contract and names its own failures. This is the translation into
# the durable vocabulary — one place, so a new source rule cannot reach the ledger unnamed.
_SOURCE_REASONS: Final[Mapping[str, str]] = {
    "symbol_not_canonicalisable": "source_contract_invalid",
    "observed_at_missing": "source_contract_invalid",
    "verdict_time_missing": "source_contract_invalid",
    "oi_direction_unknown": "source_contract_invalid",
    "not_live_ingest": "source_not_live",
    "generation_invalid": "source_generation_mismatch",
}


@dataclass(frozen=True, slots=True)
class GateConfig:
    """The operator-owned numbers this gate executes, and nothing else.

    Its digest is half the durable row's key. Editing a threshold therefore does not rewrite the record
    of what the previous threshold decided — it starts a new record — which is the difference between a
    ledger and a mutable status field.
    """

    max_age_ms: int = 300_000
    max_rank_in_window: int = 2
    min_oi_value_usd: int = 20_000_000
    symbol_cooldown_ms: int = 1_800_000
    venue_priority: tuple[LiveExchangeId, ...] = ("binance", "hyperliquid")

    @classmethod
    def from_policy(
        cls,
        policy: EligibilityPolicy,
        *,
        venue_priority: tuple[LiveExchangeId, ...],
    ) -> GateConfig:
        return cls(
            max_age_ms=policy.max_age_ms,
            max_rank_in_window=policy.max_rank_in_window,
            min_oi_value_usd=policy.min_oi_value_usd,
            symbol_cooldown_ms=policy.symbol_cooldown_ms,
            venue_priority=venue_priority,
        )

    @property
    def snapshot(self) -> dict[str, Any]:
        return {
            "max_age_ms": self.max_age_ms,
            "max_rank_in_window": self.max_rank_in_window,
            "min_oi_value_usd": self.min_oi_value_usd,
            "symbol_cooldown_ms": self.symbol_cooldown_ms,
            "venue_priority": list(self.venue_priority),
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.snapshot)


@dataclass(frozen=True, slots=True)
class CandidateGateResult:
    """One durable answer about one source fact. Constructed only through the four helpers below."""

    source_key: str
    trigger_kind: TriggerKind
    underlying_key: str | None
    source_observed_at_ms: int
    status: GateStatus
    stage: GateStage
    reason: str
    retryable: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None

    def __post_init__(self) -> None:
        if self.reason not in GATE_REASONS:
            raise ValueError(f"trading_gate_reason_unknown:{self.reason}")
        if (self.status == "CASE_CREATED") != (self.case_id is not None):
            raise ValueError("trading_gate_case_link_invalid")

    @property
    def terminal(self) -> bool:
        return self.status != "DEFERRED"

    @property
    def funnel_key(self) -> str:
        """The in-memory counter's name for this answer, so the two reports cannot describe different rules."""

        lane = "oi_gate" if self.trigger_kind == "oi" else "gate"
        return f"{lane}:{self.stage}:{self.reason}"


def _result(
    *,
    candidate: OiTradeCandidate | NewsTradeCandidate,
    status: GateStatus,
    stage: GateStage,
    reason: str,
    retryable: bool,
    evidence: Mapping[str, Any] | None = None,
    case_id: str | None = None,
) -> CandidateGateResult:
    """One admitted-source answer, carrying the frame's own measurements.

    The four numbers ride on every OI result past the source stage so a threshold argument can be
    settled from this row alone. Re-deriving them means joining `news_oi_signals` back through the
    verdict, and the whole point of the ledger is that the answer survives without that join.

    A News trigger carries the verdict's own identifying facts instead, and its clock is
    `verdict_created_at_ms` — the same field `is_fresh_trigger` reads, so the expiry sweep closes a
    News row on exactly the budget that made it stale.
    """

    if isinstance(candidate, NewsTradeCandidate):
        return CandidateGateResult(
            source_key=candidate.source_key,
            trigger_kind="news",
            underlying_key=underlying_key(candidate.base_symbol),
            source_observed_at_ms=candidate.verdict_created_at_ms,
            status=status,
            stage=stage,
            reason=reason,
            retryable=retryable,
            evidence={
                "event_id": candidate.event_id,
                "event_type": candidate.event_type,
                "magnitude": candidate.magnitude,
                "source_decision": candidate.final_decision,
                **dict(evidence or {}),
            },
            case_id=case_id,
        )
    return CandidateGateResult(
        source_key=candidate.source_key,
        trigger_kind="oi",
        underlying_key=underlying_key(candidate.base_symbol),
        source_observed_at_ms=candidate.observed_at_ms,
        status=status,
        stage=stage,
        reason=reason,
        retryable=retryable,
        evidence={
            "venue": candidate.venue,
            "oi_change_bps": candidate.oi_change_bps,
            "oi_value_usd": candidate.oi_value_usd,
            "whale_oi_ratio_bps": candidate.whale_oi_ratio_bps,
            "whale_long_profit_bps": candidate.whale_long_profit_bps,
            "rank_in_window": candidate.rank_in_window,
            "source_decision": candidate.final_decision,
            "source_rule": candidate.source_rule,
            **dict(evidence or {}),
        },
        case_id=case_id,
    )


def source_rejected(
    rejection: Rejected,
    *,
    source_key: str,
    observed_at_ms: int,
) -> CandidateGateResult:
    """A row that is not a usable OI fact. Terminal: re-reading the same row cannot change it."""

    symbol = str(rejection.symbol or "")
    return CandidateGateResult(
        source_key=source_key,
        trigger_kind="oi",
        underlying_key=underlying_key(symbol) if symbol else None,
        source_observed_at_ms=int(observed_at_ms),
        status="REJECTED",
        stage="source",
        reason=_SOURCE_REASONS.get(rejection.rule, "source_contract_invalid"),
        retryable=False,
        evidence={"rule": rejection.rule},
    )


def admit_context(candidate: OiTradeCandidate, *, config: GateConfig) -> CandidateGateResult | None:
    """The rules that read only the frame's own frozen numbers, and therefore bind it everywhere.

    Rank and the absolute liquidity floor are properties of the frame, not of the moment: they can
    never change, and they say whether this fact may ground a capital decision *at all* — as a trigger
    or as the OI context another lane's trigger attaches. Splitting them out is what keeps that true in
    one place. Leaving them inside trigger admission let a News verdict freeze a case grounded on a
    $1M, rank-50 frame that the floor exists to exclude, because the context set was never gated.

    Terminal on purpose: a `DEFERRED` here would promise a retry that can only ever reach the same
    conclusion, since the number it failed on is frozen in the frame.
    """

    if candidate.rank_in_window > config.max_rank_in_window:
        return _result(
            candidate=candidate,
            status="REJECTED",
            stage="eligibility",
            reason="rank_above_limit",
            retryable=False,
            evidence={"limit": config.max_rank_in_window},
        )
    if candidate.oi_value_usd < config.min_oi_value_usd:
        return _result(
            candidate=candidate,
            status="REJECTED",
            stage="eligibility",
            reason="oi_value_below_floor",
            retryable=False,
            evidence={"floor": config.min_oi_value_usd},
        )
    return None


def admit_trigger(
    candidate: OiTradeCandidate,
    *,
    now_ms: int,
    config: GateConfig,
    blacklist: Blacklist,
    active_underlyings: Container[str] = (),
    underlyings_in_flight: Container[str] = (),
    cased_source_keys: Container[str] = (),
) -> CandidateGateResult | None:
    """Whether this fact may start a case *now*, or the one named reason it may not.

    `None` means "carry on to routing". A candidate refused by one of the *situational* rules below is
    still legal point-in-time context for another lane's trigger; one refused by `admit_context` is
    not, and the caller keeps the two apart.

    The order is deliberate. Idempotency first, because a source that already produced a case has a
    terminal answer and every rule below it would be describing work that is already done. Then the
    frame's own frozen properties. The reversible conditions come last.
    """

    key = underlying_key(candidate.base_symbol)
    if candidate.source_key in cased_source_keys:
        return _result(
            candidate=candidate,
            status="REJECTED",
            stage="eligibility",
            reason="already_consumed",
            retryable=False,
        )
    frame = admit_context(candidate, config=config)
    if frame is not None:
        return frame
    blocked = blacklist.blocked(candidate.base_symbol, now_ms=now_ms)
    if blocked is not None:
        # `DEFERRED`, always. The deny list is the one input here that is *mutable while the frame is
        # still actionable*: an operator can remove an entry, and a timed entry can reach its
        # `expires_at_ms`, both well inside the five-minute trigger budget. A terminal `REJECTED` froze
        # the row — the ledger only advances a row out of `DEFERRED` — so the next scan would create a
        # case while the ledger went on claiming `blacklisted` with no case link, which is exactly the
        # "one and only one answer per frame" this table exists to guarantee. A failed *read* of the
        # list blocks every symbol and lands here too, and it is infrastructure state rather than a
        # property of the frame, so it wants the same answer for the same reason.
        #
        # The expiry sweep is what stops these accumulating: a frame nobody un-blocked goes `EXPIRED`
        # the moment it is past the trigger budget, keeping its reason.
        return _result(
            candidate=candidate,
            status="DEFERRED",
            stage="eligibility",
            reason="blacklisted",
            retryable=True,
            evidence={"blacklist_reason": str(blocked.reason)},
        )
    if now_ms - candidate.observed_at_ms > config.max_age_ms:
        # The clock only moves one way, so this is terminal on arrival. It is `EXPIRED` rather than
        # `REJECTED` because nothing about the fact was wrong — the lane simply was not looking when it
        # was actionable, which is the answer an operator needs after a restart or a paused runner.
        return _result(
            candidate=candidate,
            status="EXPIRED",
            stage="eligibility",
            reason="trigger_stale",
            retryable=False,
            evidence={"age_ms": now_ms - candidate.observed_at_ms, "max_age_ms": config.max_age_ms},
        )
    if key in active_underlyings:
        return _result(
            candidate=candidate,
            status="DEFERRED",
            stage="eligibility",
            reason="active_underlying",
            retryable=True,
        )
    if key in underlyings_in_flight:
        return _result(
            candidate=candidate,
            status="DEFERRED",
            stage="eligibility",
            reason="case_in_flight",
            retryable=True,
        )
    return None


def admit_route(candidate: OiTradeCandidate, *, config: GateConfig) -> CandidateGateResult | None:
    """Whether the frame's own venue tag names a book this lane may execute against.

    Source-aligned (#211): an OI frame is a claim about *one venue's* open interest, so the static
    operator priority may not answer it. A Hyperliquid frame resolved to a Binance perp produced an
    order against a book whose open interest did nothing of the kind.
    """

    exchange = signal_exchange_id(candidate.venue)
    if exchange is None:
        return _result(
            candidate=candidate,
            status="REJECTED",
            stage="routing",
            reason="venue_unresolved",
            retryable=False,
        )
    if exchange not in config.venue_priority:
        return _result(
            candidate=candidate,
            status="REJECTED",
            stage="routing",
            reason="unsupported_venue",
            retryable=False,
            evidence={"enabled": list(config.venue_priority)},
        )
    return None


def defer(candidate: OiTradeCandidate | NewsTradeCandidate, *, stage: GateStage, reason: str) -> CandidateGateResult:
    """A refusal a later scan could genuinely answer differently, and the expiry sweep will close."""

    return _result(candidate=candidate, status="DEFERRED", stage=stage, reason=reason, retryable=True)


def reject(
    candidate: OiTradeCandidate | NewsTradeCandidate,
    *,
    stage: GateStage,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> CandidateGateResult:
    """A refusal frozen by the frame's own properties: no later scan can reach a different answer."""

    return _result(
        candidate=candidate, status="REJECTED", stage=stage, reason=reason, retryable=False, evidence=evidence
    )


def case_created(candidate: OiTradeCandidate | NewsTradeCandidate, *, case_id: str) -> CandidateGateResult:
    """The admission succeeded. Written in the same transaction as the case row it names."""

    return _result(
        candidate=candidate,
        status="CASE_CREATED",
        stage="freeze",
        reason="case_created",
        retryable=False,
        case_id=case_id,
    )


__all__ = [
    "CANDIDATE_GATE_VERSION",
    "GATE_REASONS",
    "CandidateGateResult",
    "GateConfig",
    "GateStage",
    "GateStatus",
    "admit_context",
    "admit_route",
    "admit_trigger",
    "case_created",
    "defer",
    "reject",
    "source_rejected",
]
