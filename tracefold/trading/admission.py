"""The one place a Source is admitted to the live Signal lane, and the one vocabulary it refuses in.

Every rule here used to be executed somewhere else as well — the rank ceiling and the liquidity floor
in News's SELECT, the floor again inside the strategy, the venue check in two places. That is what made
`oi_rows = 0` unanswerable: a frame filtered out upstream and a frame that never existed were the same
absence.

**One trigger kind, two supported source venues.** Binance and Hyperliquid OI frames keep their own
venue identity for source-native public market context. Unknown venues fail before a Case exists.
This provenance never chooses an execution route. Editorial News frames are not admitted at all:
they are not a Source of this lane and no code path offers one.

**What this module owns** is whether a Source may become a *trigger* now:

    source          the row is a usable, current-generation, live OI fact at all
    venue           the frame's own venue is supported for source-native public context
    eligibility     liquidity floor, blacklist, freshness, idempotency, one undecided Case per underlying
    market_context  there is a candle at the cutoff to freeze a mark and a pre-move from
    freeze          the immutable Case was written

**What it deliberately does not own** is anything that expresses an opinion about the trade. A frame
that is liquid, routable and priced but whose numbers the policy dislikes must reach a Case and be
refused there by name, or the manifest never records what was rejected.

Two reasons from #264's taxonomy are deliberately absent:

* `whale_ratio_below_floor`. The smart-money ratio is an *Alpha* threshold, and one policy's Alpha gate
  must never delete another reader's data.
* `instrument_stale`. There is no catalogue-freshness threshold in this system and inventing one here
  would be a new capital rule with nothing measured behind it.
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, TypedDict

from .contracts import OiTradeCandidate, TriggerKind, canonical_sha256, underlying_key
from .sources import SourceRejected, normalize_source_venue

# Bumped when a rule is added, removed, or changes what it means. It is half of the durable row's key,
# so a new version re-decides every source rather than inheriting an answer from a rule that is gone.
# v5 is #433-C: source venue is evidence provenance, not an execution binding or catalogue lookup.
# v6 keeps Hyperliquid's `xyz` builder DEX distinct so source-native candles cannot silently fall
# back to the main perpetual book.
ADMISSION_VERSION: Final = "trading_admission_v6"

AdmissionStatus = Literal["DEFERRED", "REJECTED", "CASE_CREATED", "EXPIRED"]
AdmissionStage = Literal["source", "venue", "eligibility", "catalog", "market_context", "freeze"]

# The closed vocabulary a *writer* may reach. Reading history does not pass through here, which is why
# `blacklisted` and `catalog_absent` could leave in #460 while their `tradingLabels.ts` entries stayed:
# the one stored `eligibility:blacklisted` row is served as a plain string and still renders. A reason
# outside this set is a bug, not a new rule: an unbounded key set is exactly what the retired funnel's
# venue counter already failed at.
ADMISSION_REASONS: Final[frozenset[str]] = frozenset(
    {
        "source_contract_invalid",
        "source_generation_mismatch",
        "source_not_live",
        # An unknown source venue has no supported public context adapter. Terminal and explicit;
        # borrowing another venue would silently change the fact being evaluated.
        "venue_unresolved",
        "trigger_stale",
        "oi_value_below_floor",
        # One name for "this issuer already has an undecided Case". `cooldown` remains retired; Signal
        # TTL and execution risk belong to later boundaries.
        "underlying_busy",
        "market_data_unavailable",
        "market_data_invalid",
        "already_consumed",
        "superseded_by_newer_trigger",
        # The per-turn Case budget refuses a Source that passed every rule about itself; calling that
        # `underlying_busy` would blame the frame's own issuer for a different name being ahead of it.
        "lane_capacity_exhausted",
        "case_created",
    }
)

# `normalize_oi_source` proves the source contract and names its own failures. This is the translation
# into the durable vocabulary — one place, so a new source rule cannot reach the ledger unnamed.
_SOURCE_REASONS: Final[Mapping[str, str]] = {
    "symbol_not_canonicalisable": "source_contract_invalid",
    "market_key_invalid": "source_contract_invalid",
    "observed_at_missing": "source_contract_invalid",
    "verdict_time_missing": "source_contract_invalid",
    "rank_missing": "source_contract_invalid",
    "oi_direction_unknown": "source_contract_invalid",
    "not_live_ingest": "source_not_live",
    "generation_invalid": "source_generation_mismatch",
}


@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    """The operator-owned numbers admission executes, and nothing else.

    Its digest is half the durable row's key. Editing a threshold therefore does not rewrite the record
    of what the previous threshold decided — it starts a new record — which is the difference between a
    ledger and a mutable status field.

    No `venue_priority`: source venue names evidence provenance only. Execution route selection does
    not belong to Admission.
    """

    max_age_ms: int = 300_000
    min_oi_value_usd: int = 20_000_000

    @property
    def snapshot(self) -> dict[str, Any]:
        return {
            "max_age_ms": self.max_age_ms,
            "min_oi_value_usd": self.min_oi_value_usd,
            "source_venues": ["binance.usdm", "hyperliquid.perp", "hyperliquid.xyz"],
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.snapshot)


class AdmissionRow(TypedDict):
    """The durable admission row, named field by field so the writer and the ledger cannot drift."""

    source_key: str
    gate_version: str
    gate_config_digest: str
    trigger_kind: TriggerKind
    underlying_key: str | None
    source_observed_at_ms: int
    status: AdmissionStatus
    stage: AdmissionStage
    reason: str
    retryable: bool
    evidence: dict[str, Any]
    case_id: str | None


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """One durable answer about one Source. Constructed only through the helpers below."""

    source_key: str
    trigger_kind: TriggerKind
    underlying_key: str | None
    source_observed_at_ms: int
    status: AdmissionStatus
    stage: AdmissionStage
    reason: str
    retryable: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None

    def __post_init__(self) -> None:
        if self.reason not in ADMISSION_REASONS:
            raise ValueError(f"trading_admission_reason_unknown:{self.reason}")
        if (self.status == "CASE_CREATED") != (self.case_id is not None):
            raise ValueError("trading_admission_case_link_invalid")

    @property
    def terminal(self) -> bool:
        return self.status != "DEFERRED"

    def row(self, *, gate_config_digest: str) -> AdmissionRow:
        """This answer as the ledger stores it. One construction, so the two cannot describe different rules."""

        return AdmissionRow(
            source_key=self.source_key,
            gate_version=ADMISSION_VERSION,
            gate_config_digest=gate_config_digest,
            trigger_kind=self.trigger_kind,
            underlying_key=self.underlying_key,
            source_observed_at_ms=self.source_observed_at_ms,
            status=self.status,
            stage=self.stage,
            reason=self.reason,
            retryable=self.retryable,
            evidence=dict(self.evidence),
            case_id=self.case_id,
        )


def _result(
    *,
    candidate: OiTradeCandidate,
    status: AdmissionStatus,
    stage: AdmissionStage,
    reason: str,
    retryable: bool,
    evidence: Mapping[str, Any] | None = None,
    case_id: str | None = None,
) -> AdmissionResult:
    """One admitted-source answer, carrying the frame's own measurements.

    The four numbers ride on every result past the source stage so a threshold argument can be settled
    from this row alone. Re-deriving them means joining `news_oi_signals` back through the verdict, and
    the whole point of the ledger is that the answer survives without that join.
    """

    return AdmissionResult(
        source_key=candidate.source_key,
        trigger_kind="oi",
        underlying_key=candidate.underlying_key,
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
            "source_decision": candidate.final_decision,
            "source_rule": candidate.source_rule,
            **dict(evidence or {}),
        },
        case_id=case_id,
    )


def source_rejected(
    rejection: SourceRejected,
    *,
    source_key: str,
    observed_at_ms: int,
) -> AdmissionResult:
    """A row that is not a usable OI fact. Terminal: re-reading the same row cannot change it."""

    symbol = str(rejection.symbol or "")
    return AdmissionResult(
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


def admit_venue(candidate: OiTradeCandidate) -> AdmissionResult | None:
    """Whether the frame's venue names a supported source-native public context adapter.

    An OI frame is a claim about *one venue's* open interest, so no fallback may answer it with another
    venue's bars. This check says nothing about where a future Runtime executes.
    """

    if normalize_source_venue(candidate.venue) is None:
        return _result(
            candidate=candidate,
            status="REJECTED",
            stage="venue",
            reason="venue_unresolved",
            retryable=False,
        )
    return None


def admit_frame(candidate: OiTradeCandidate, *, config: AdmissionConfig) -> AdmissionResult | None:
    """The one rule that reads only the frame's own frozen numbers, and therefore binds it everywhere.

    The absolute liquidity floor is a property of the frame, not of the moment: it can never change,
    and it says whether this fact may ground a capital decision *at all*. It is a venue prior — how
    much book stands behind the name — which is why it lives here and not in the policy.

    `rank_above_limit` used to sit beside it and is gone (#348). A "only the top N in the window"
    ceiling is a *selectivity* rule, and selectivity belongs to the policy, which already owns four
    thresholds of it. Splitting the strategy's rulebook across two files bought two refusals in seven
    days and cost a reader having to know both places to answer what the strategy requires.

    Terminal on purpose: a `DEFERRED` here would promise a retry that can only ever reach the same
    conclusion, since the number it failed on is frozen in the frame.
    """

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
    config: AdmissionConfig,
    underlyings_in_flight: Container[str] = (),
    cased_source_keys: Container[str] = (),
) -> AdmissionResult | None:
    """Whether this fact may start a Case *now*, or the one named reason it may not.

    `None` means "carry on to Case freeze". The order is deliberate. Idempotency first,
    because a Source that already produced a Case has a terminal answer and every rule below it would
    be describing work that is already done. Then the frame's own frozen properties. The reversible
    conditions come last.
    """

    key = candidate.underlying_key
    if candidate.source_key in cased_source_keys:
        return _result(
            candidate=candidate,
            status="REJECTED",
            stage="eligibility",
            reason="already_consumed",
            retryable=False,
        )
    frame = admit_frame(candidate, config=config)
    if frame is not None:
        return frame
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
    # One reason for one current fact: this issuer already has an undecided Case.
    if key in underlyings_in_flight:
        return _result(
            candidate=candidate,
            status="DEFERRED",
            stage="eligibility",
            reason="underlying_busy",
            retryable=True,
            evidence={"holds": "case"},
        )
    return None


def defer(
    candidate: OiTradeCandidate,
    *,
    stage: AdmissionStage,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> AdmissionResult:
    """A refusal a later scan could genuinely answer differently, and the expiry sweep will close."""

    return _result(
        candidate=candidate, status="DEFERRED", stage=stage, reason=reason, retryable=True, evidence=evidence
    )


def reject(
    candidate: OiTradeCandidate,
    *,
    stage: AdmissionStage,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> AdmissionResult:
    """A refusal frozen by the frame's own properties: no later scan can reach a different answer."""

    return _result(
        candidate=candidate, status="REJECTED", stage=stage, reason=reason, retryable=False, evidence=evidence
    )


def case_created(candidate: OiTradeCandidate, *, case_id: str) -> AdmissionResult:
    """The admission succeeded. Written in the same transaction as the Case row it names."""

    return _result(
        candidate=candidate,
        status="CASE_CREATED",
        stage="freeze",
        reason="case_created",
        retryable=False,
        case_id=case_id,
    )


__all__ = [
    "ADMISSION_REASONS",
    "ADMISSION_VERSION",
    "AdmissionConfig",
    "AdmissionResult",
    "AdmissionRow",
    "AdmissionStage",
    "AdmissionStatus",
    "admit_frame",
    "admit_trigger",
    "admit_venue",
    "case_created",
    "defer",
    "reject",
    "source_rejected",
]
