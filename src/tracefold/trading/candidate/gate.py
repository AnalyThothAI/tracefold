"""The one place an OI fact is admitted to the capital lane, and the one vocabulary it refuses in.

Every rule here used to be executed somewhere else as well — the rank ceiling and the liquidity floor
in News's SELECT, the floor again inside the strategy, the venue check in both `_plan` and `_freeze`.
That is what made `oi_rows = 0` unanswerable: a frame filtered out upstream and a frame that never
existed were the same absence, and the counters that could have told them apart lived in one JSONB
document reset every UTC midnight.

**What this module owns** is whether an OI fact may become a *trigger* now:

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

from ..contracts import LiveExchangeId, OiTradeCandidate, TriggerKind, canonical_sha256, underlying_key
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
    candidate: OiTradeCandidate,
    status: GateStatus,
    stage: GateStage,
    reason: str,
    retryable: bool,
    evidence: Mapping[str, Any] | None = None,
    case_id: str | None = None,
) -> CandidateGateResult:
    """One admitted-source answer, carrying the frame's own measurements.

    The four numbers ride on every result past the source stage so a threshold argument can be settled
    from this row alone. Re-deriving them means joining `news_oi_signals` back through the verdict, and
    the whole point of the ledger is that the answer survives without that join.
    """

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

    `None` means "carry on to routing". Returning early is not the same as discarding the fact: a
    candidate refused here is still legal point-in-time context for another lane's trigger, which is
    why the caller keeps it in the context set regardless of what this answers.

    The order is deliberate. Idempotency first, because a source that already produced a case has a
    terminal answer and every rule below it would be describing work that is already done. Then the two
    frozen properties of the frame itself — rank and liquidity — because they can never change, so a
    `DEFERRED` on them would promise a retry that can only ever reach the same conclusion. The
    reversible conditions come last.
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
    blocked = blacklist.blocked(candidate.base_symbol, now_ms=now_ms)
    if blocked is not None:
        # Terminal, not deferred. An entry can expire or be lifted, but the frame goes stale long
        # before either happens, and promising a retry that the clock guarantees will never be taken
        # is the kind of open row the expiry sweep exists to stop accumulating.
        return _result(
            candidate=candidate,
            status="REJECTED",
            stage="eligibility",
            reason="blacklisted",
            retryable=False,
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


def defer(candidate: OiTradeCandidate, *, stage: GateStage, reason: str) -> CandidateGateResult:
    """A refusal a later scan could genuinely answer differently, and the expiry sweep will close."""

    return _result(candidate=candidate, status="DEFERRED", stage=stage, reason=reason, retryable=True)


def reject(
    candidate: OiTradeCandidate,
    *,
    stage: GateStage,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> CandidateGateResult:
    """A refusal frozen by the frame's own properties: no later scan can reach a different answer."""

    return _result(
        candidate=candidate, status="REJECTED", stage=stage, reason=reason, retryable=False, evidence=evidence
    )


def case_created(candidate: OiTradeCandidate, *, case_id: str) -> CandidateGateResult:
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
    "admit_route",
    "admit_trigger",
    "case_created",
    "defer",
    "reject",
    "source_rejected",
]
