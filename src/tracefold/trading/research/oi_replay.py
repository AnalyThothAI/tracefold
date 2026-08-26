"""Replay every parsed OI fact through the exact rules the runner applies (#265 PR-C).

The report and the scanner must be the same code, or the report eventually describes a funnel the lane
no longer has. So this module owns no rule at all: it drives `oi_candidate`, the Candidate Gate and the
strategy, in the order `CandidateRunner` drives them, and counts what each one answered.

**What it deliberately cannot answer.** Two stages need market data — the pre-move band and any
outcome — and this is a read-only report, not a second runner. Every fact that survives the
deterministic stages is reported as `pending_market_context`, with its measurements attached, so an
operator can see the population the price rules would have judged rather than a number this module
invented for them.

**It never proposes a threshold.** Survivor counts per rule are the point: they say which condition is
binding. Reading a better number off the same seven days that produced the counts is how a lane ends up
tuned to its own history, and #265 §8 forbids it in as many words.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..candidate.blacklist import Blacklist
from ..candidate.eligibility import Rejected, oi_candidate
from ..candidate.gate import GateConfig, admit_route, admit_trigger
from ..contracts import OiCandidateRow, OiTradeCandidate, oi_source_key
from ..strategy.oi_smart_money_momentum import OiSmartMoneyMomentumConfig, OiSmartMoneyMomentumStrategy

# The stage a surviving fact stops at. Not a gate reason: nothing refused it, and calling it one would
# put a refusal in the ledger's vocabulary that no rule produced.
PENDING_MARKET_CONTEXT = "pending_market_context"


@dataclass(frozen=True, slots=True)
class OiReplayOutcome:
    """One fact, and the first rule that had an opinion about it."""

    source_key: str
    symbol: str
    venue: str
    observed_at_ms: int
    stage: str
    reason: str
    oi_change_bps: int
    oi_value_usd: int
    whale_oi_ratio_bps: int
    whale_long_profit_bps: int
    rank_in_window: int
    measurement_window_ms: int | None
    source_decision: str
    routable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "symbol": self.symbol,
            "venue": self.venue,
            "observed_at_ms": self.observed_at_ms,
            "stage": self.stage,
            "reason": self.reason,
            "oi_change_bps": self.oi_change_bps,
            "oi_value_usd": self.oi_value_usd,
            "whale_oi_ratio_bps": self.whale_oi_ratio_bps,
            "whale_long_profit_bps": self.whale_long_profit_bps,
            "rank_in_window": self.rank_in_window,
            "measurement_window_ms": self.measurement_window_ms,
            "source_decision": self.source_decision,
            "routable": self.routable,
        }


@dataclass(slots=True)
class OiReplayReport:
    """Counts by stage and by rule, plus the cohort the target template describes.

    `target_cohort` is listed row by row rather than counted, because at the volume this lane runs — 405
    facts in the seven days the ledger has existed, of which seven meet the template — a count would
    hide whether the survivors are seven distinct issuers or one issuer seven times.
    """

    facts: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    reader_decisions: dict[str, int] = field(default_factory=dict)
    # The issuers whose frames resolved to a venue this lane may execute. The caller owns the catalogue
    # read and fills `instrument_coverage` from exactly this set — a count written here for a symbol
    # nobody looked up would ship as "no native perp listed", which is a false negative in the one
    # report whose job is establishing which rule is binding.
    routable_symbols: set[str] = field(default_factory=set)
    instrument_coverage: dict[str, int] = field(default_factory=dict)
    surviving: list[OiReplayOutcome] = field(default_factory=list)
    target_cohort: list[OiReplayOutcome] = field(default_factory=list)

    def _count(self, bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def record(self, outcome: OiReplayOutcome) -> None:
        self.facts += 1
        self._count(self.by_stage, outcome.stage)
        self._count(self.by_reason, f"{outcome.stage}:{outcome.reason}")
        self._count(self.reader_decisions, outcome.source_decision or "unknown")
        if outcome.stage == PENDING_MARKET_CONTEXT:
            self.surviving.append(outcome)

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts": self.facts,
            "by_stage": dict(sorted(self.by_stage.items())),
            "by_reason": dict(sorted(self.by_reason.items())),
            "reader_decisions": dict(sorted(self.reader_decisions.items())),
            "routable_symbols": sorted(self.routable_symbols),
            "instrument_coverage": dict(sorted(self.instrument_coverage.items())),
            "surviving": [row.as_dict() for row in self.surviving],
            "target_cohort": [row.as_dict() for row in self.target_cohort],
        }


def _outcome(
    candidate: OiTradeCandidate,
    *,
    stage: str,
    reason: str,
    routable: bool,
) -> OiReplayOutcome:
    return OiReplayOutcome(
        source_key=candidate.source_key,
        symbol=candidate.base_symbol,
        venue=candidate.venue,
        observed_at_ms=candidate.observed_at_ms,
        stage=stage,
        reason=reason,
        oi_change_bps=candidate.oi_change_bps,
        oi_value_usd=candidate.oi_value_usd,
        whale_oi_ratio_bps=candidate.whale_oi_ratio_bps,
        whale_long_profit_bps=candidate.whale_long_profit_bps,
        rank_in_window=candidate.rank_in_window,
        measurement_window_ms=candidate.measurement_window_ms,
        source_decision=candidate.final_decision,
        routable=routable,
    )


def meets_target_template(
    candidate: OiTradeCandidate,
    *,
    config: OiSmartMoneyMomentumConfig,
) -> bool:
    """The three conditions the NewsLiquid template names, and only those.

    Reported separately from the funnel because they are the *Alpha* question — "how often does the
    shape this lane was built for actually occur" — and the funnel answers a different one: "of the
    frames that occurred, where did each stop". Liquidity, rank and routing are not in this test.

    The measurement window is, though. The template is "**5 minute** OI rise >= 10%", so a frame whose
    interval the provider contract could not establish is not an instance of it — it is three numbers
    over an unknown period, and counting it would make the cohort a claim nobody checked.
    """

    return (
        candidate.measurement_window_ms == config.measurement_window_ms
        and candidate.oi_direction == "rise"
        and candidate.oi_change_bps >= config.min_oi_change_bps
        and candidate.whale_oi_ratio_bps > config.min_whale_oi_ratio_bps
        and candidate.whale_long_profit_bps > config.min_whale_long_profit_bps
    )


def replay_oi_facts(
    rows: Sequence[OiCandidateRow],
    *,
    gate: GateConfig,
    strategy: OiSmartMoneyMomentumStrategy,
    blacklist: Blacklist,
    now_ms: int,
) -> OiReplayReport:
    """Every fact in, one named stop each out. Pure; the caller owns every read.

    Freshness is deliberately excluded. Replaying a seven-day window against `now` would stop every
    fact at `trigger_stale` and answer nothing — the question here is which *rule* is binding, and
    freshness is a property of when the lane happened to be looking.
    """

    report = OiReplayReport()
    for row in rows:
        parsed = oi_candidate(row)
        if isinstance(parsed, Rejected):
            report.record(
                OiReplayOutcome(
                    source_key=oi_source_key(row.get("event_id"), row.get("metric_version")),
                    symbol=str(row.get("symbol") or ""),
                    venue=str(row.get("venue") or ""),
                    observed_at_ms=int(row.get("observed_at_ms") or 0),
                    stage="source",
                    reason=parsed.rule,
                    oi_change_bps=0,
                    oi_value_usd=0,
                    whale_oi_ratio_bps=0,
                    whale_long_profit_bps=0,
                    rank_in_window=0,
                    measurement_window_ms=None,
                    source_decision=str(row.get("final_decision") or ""),
                    routable=False,
                )
            )
            continue

        routing = admit_route(parsed, config=gate)
        routable = routing is None
        if meets_target_template(parsed, config=strategy.config):
            report.target_cohort.append(_outcome(parsed, stage="target", reason="template", routable=routable))
        if routable:
            report.routable_symbols.add(parsed.base_symbol)

        # Same order as the runner: eligibility, then routing. `now_ms` is the fact's own observation
        # time so the freshness rule is satisfied by construction and cannot mask the rules under test.
        refused = admit_trigger(parsed, now_ms=parsed.observed_at_ms, config=gate, blacklist=blacklist) or routing
        if refused is not None:
            report.record(_outcome(parsed, stage=refused.stage, reason=refused.reason, routable=routable))
            continue

        # The strategy, minus the two rules that need a price. A surviving fact is one the deterministic
        # half admits; whether it would have entered is the market context's answer, not this report's.
        outcome = strategy.evaluate(_alpha_only_context(parsed))
        if outcome.decision == "no_trade" and outcome.rule not in _PRICE_RULES:
            report.record(_outcome(parsed, stage="strategy", reason=outcome.rule, routable=routable))
            continue
        report.record(_outcome(parsed, stage=PENDING_MARKET_CONTEXT, reason=outcome.rule, routable=routable))
    return report


# The two strategy rules a replay cannot reach, because both read a price this module does not fetch.
_PRICE_RULES = frozenset({"price_direction_not_confirmed", "move_above_band_chasing"})


def _alpha_only_context(candidate: OiTradeCandidate) -> Any:
    """A frozen context carrying the fact and a pre-move that satisfies the band by construction.

    The strategy is a pure function and this module wants its *Alpha* answer, so the price input is set
    to a value inside the band rather than left absent — an absent one would return
    `price_direction_not_confirmed` for every row and report the price rule as universally binding when
    it was never evaluated. The two price rules are excluded from the funnel above for the same reason.
    """

    from decimal import Decimal

    from ..contracts import FrozenMarketContext, FrozenStrategyContext, OiRegime, RegimeAssessment

    return FrozenStrategyContext(
        mode="paper",
        oi=candidate,
        regime=RegimeAssessment(
            regime=OiRegime.UNCLEAR,
            reason="replay_no_price",
            pre_move_bps=0,
            oi_direction=candidate.oi_direction,
        ),
        market=FrozenMarketContext(
            mark_price=Decimal("1"),
            observed_at_ms=candidate.observed_at_ms,
            pre_move_bps=0,
            pre_move_lookback_ms=3_600_000,
        ),
    )


__all__ = [
    "PENDING_MARKET_CONTEXT",
    "OiReplayOutcome",
    "OiReplayReport",
    "meets_target_template",
    "replay_oi_facts",
]
