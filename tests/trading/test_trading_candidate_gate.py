"""The Candidate Gate's answers (#264): one owner per rule, one named reason per refusal.

What these pin is not "the thresholds are 2 and 20M" — those are configuration. It is that every way an
OI fact can fail to become a trigger produces a *named, durable* answer instead of an absence, and that
the four numbers the decision was taken on ride along with it. Before the gate, `oi_rows = 0` could
mean no data, a reader drop, a rank ceiling or a liquidity floor, and only an offline SQL replay could
say which.
"""

from __future__ import annotations

from typing import Any

import pytest

from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.candidate.eligibility import EligibilityPolicy, Rejected, oi_candidate
from tracefold.trading.candidate.gate import (
    GATE_REASONS,
    CandidateGateResult,
    GateConfig,
    admit_context,
    admit_route,
    admit_trigger,
    case_created,
    defer,
    reject,
    source_rejected,
)
from tracefold.trading.contracts import OiTradeCandidate

NOW = 1_787_000_000_000
OPEN_DENY = Blacklist.from_rows([{"base_symbol": "BTC", "reason": "benchmark_large_cap"}])
CONFIG = GateConfig.from_policy(EligibilityPolicy(), venue_priority=("binance", "hyperliquid"))


def _row(**kwargs: Any) -> dict[str, Any]:
    row = {
        "event_id": "e1",
        "final_decision": "push",
        "source_rule": "opening_move_with_whale_concentration",
        "ingest_mode": "live",
        "program_version": "news_oi_signal_v1",
        "metric_version": "oi_signal_v1",
        "symbol": "DOGE",
        "direction": "rise",
        "oi_change_bps": 320,
        "oi_value_usd": 73_010_000,
        "whale_long_profit_bps": 9_900,
        "whale_oi_ratio_bps": 21_097,
        "rank_in_window": 1,
        "observed_at_ms": NOW - 10_000,
        "verdict_created_at_ms": NOW - 9_000,
        "venue": "hyperliquid",
        "learning_epoch": "program_v8",
        "program_sha256": "a" * 64,
        "policy_version": "news_triage_policy_v10",
        "editorial_origin": "telemetry_deterministic",
        "editorial_sha256": "b" * 64,
        "scored_judgment_sha256": "c" * 64,
        "runtime_manifest_sha": "d" * 64,
    }
    row.update(kwargs)
    return row


def _fact(**kwargs: Any) -> OiTradeCandidate:
    candidate = oi_candidate(_row(**kwargs))
    assert isinstance(candidate, OiTradeCandidate)
    return candidate


def _verdict(candidate: OiTradeCandidate, *, blacklist: Blacklist = OPEN_DENY, **kwargs: Any) -> CandidateGateResult:
    result = admit_trigger(candidate, now_ms=NOW, config=CONFIG, blacklist=blacklist, **kwargs)
    assert result is not None
    return result


def test_a_fully_qualifying_frame_is_admitted_with_no_answer_at_all() -> None:
    """`None` is the admission. Every other return is a refusal that gets written down."""

    assert admit_trigger(_fact(), now_ms=NOW, config=CONFIG, blacklist=OPEN_DENY) is None
    assert admit_route(_fact(), config=CONFIG) is None


@pytest.mark.parametrize(
    ("kwargs", "status", "stage", "reason"),
    [
        ({"rank_in_window": 6}, "REJECTED", "eligibility", "rank_above_limit"),
        ({"oi_value_usd": 3_190_000}, "REJECTED", "eligibility", "oi_value_below_floor"),
        ({"symbol": "BTC"}, "DEFERRED", "eligibility", "blacklisted"),
        ({"observed_at_ms": NOW - 3_600_000}, "EXPIRED", "eligibility", "trigger_stale"),
        ({"venue": "okx"}, "REJECTED", "routing", "venue_unresolved"),
    ],
)
def test_every_refusal_is_named_rather_than_an_absence(
    kwargs: dict[str, Any], status: str, stage: str, reason: str
) -> None:
    """STORJ at $3.19M is the live shape of the second row: real, parsed, and simply too thin."""

    candidate = _fact(**kwargs)
    result = admit_trigger(candidate, now_ms=NOW, config=CONFIG, blacklist=OPEN_DENY) or admit_route(
        candidate, config=CONFIG
    )
    assert result is not None
    assert (result.status, result.stage, result.reason) == (status, stage, reason)
    assert result.source_key == candidate.source_key


def test_a_thin_frame_carries_the_number_it_failed_on_and_the_floor_it_failed_against() -> None:
    """An operator arguing about a threshold reads one row, not three tables."""

    result = _verdict(_fact(oi_value_usd=3_190_000, symbol="STORJ"))
    assert result.evidence["oi_value_usd"] == 3_190_000
    assert result.evidence["floor"] == CONFIG.min_oi_value_usd
    # And the reader's own verdict rides along, so "News dropped it" and "Trading refused it" are two
    # separately readable facts about the same frame rather than one conflated absence.
    assert result.evidence["source_decision"] == "push"
    assert result.evidence["source_rule"] == "opening_move_with_whale_concentration"


def test_the_reversible_refusals_defer_and_the_frozen_ones_do_not() -> None:
    """`retryable` is a promise. A rank or a liquidity number is frozen in the frame itself."""

    for reversible in (
        _verdict(_fact(), underlyings_in_flight={"crypto:DOGE"}),
        # The deny list is mutable while the frame is still actionable, so it belongs on this side.
        _verdict(_fact(symbol="BTC")),
    ):
        assert (reversible.status, reversible.retryable, reversible.terminal) == ("DEFERRED", True, False)

    for frozen in (_verdict(_fact(oi_value_usd=1)), _verdict(_fact(rank_in_window=99))):
        assert (frozen.status, frozen.retryable, frozen.terminal) == ("REJECTED", False, True)


def test_idempotency_is_answered_before_any_rule_about_the_frame() -> None:
    """A source that already produced a case has a terminal answer; the rest would describe done work."""

    candidate = _fact(oi_value_usd=1, rank_in_window=99)
    result = _verdict(candidate, cased_source_keys={candidate.source_key})
    assert result.reason == "already_consumed"


def test_a_venue_the_operator_has_not_enabled_is_not_the_same_as_an_unknown_one() -> None:
    single = GateConfig.from_policy(EligibilityPolicy(), venue_priority=("binance",))
    unsupported = admit_route(_fact(venue="hyperliquid"), config=single)
    assert unsupported is not None
    assert unsupported.reason == "unsupported_venue"
    assert unsupported.evidence["enabled"] == ["binance"]

    unknown = admit_route(_fact(venue="okx"), config=single)
    assert unknown is not None
    assert unknown.reason == "venue_unresolved"


def test_a_source_contract_failure_is_filed_under_the_key_the_case_would_have_used() -> None:
    """The row never becomes a candidate, and its answer still has to be findable by source key."""

    rejection = oi_candidate(_row(direction="sideways"))
    assert isinstance(rejection, Rejected)
    result = source_rejected(rejection, source_key="oi:e1:oi_signal_v1", observed_at_ms=NOW)
    assert (result.status, result.stage, result.reason) == ("REJECTED", "source", "source_contract_invalid")
    assert result.source_key == "oi:e1:oi_signal_v1"
    assert result.underlying_key == "crypto:DOGE"


def test_a_retired_generation_is_a_named_source_failure_not_an_exception() -> None:
    """The Program and policy are `Literal`s on the candidate; a stale row used to raise out of the turn."""

    rejection = oi_candidate(_row(policy_version="news_triage_policy_v9"))
    assert isinstance(rejection, Rejected)
    assert source_rejected(rejection, source_key="oi:e1:oi_signal_v1", observed_at_ms=NOW).reason == (
        "source_generation_mismatch"
    )


def test_every_deny_list_refusal_stays_retryable_because_the_list_is_mutable() -> None:
    """A terminal `blacklisted` row is a promise the ledger cannot keep.

    The deny list is the one input here that can change while the frame is still actionable: an
    operator can remove an entry, and a timed one reaches its `expires_at_ms`, both well inside the
    five-minute trigger budget. Since the ledger only advances a row out of `DEFERRED`, a terminal
    `REJECTED` meant the next scan could create a case while the ledger went on claiming `blacklisted`
    with no case link — the exact "one and only one answer per frame" this table exists to guarantee.

    A failed *read* of the list blocks every symbol and lands here too. That is infrastructure state
    rather than a property of the frame, and it wants the same answer for the same reason: otherwise
    one database hiccup records a whole scan window as permanently denied.
    """

    for blacklist, expected_reason in (
        (OPEN_DENY, "benchmark_large_cap"),
        (Blacklist.unavailable(), "blacklist_unavailable"),
    ):
        symbol = "BTC" if expected_reason == "benchmark_large_cap" else "DOGE"
        result = _verdict(_fact(symbol=symbol), blacklist=blacklist)
        assert (result.status, result.reason, result.retryable) == ("DEFERRED", "blacklisted", True)
        assert result.evidence["blacklist_reason"] == expected_reason

    # A timed entry that has already expired is not a refusal at all.
    expired = Blacklist.from_rows([{"base_symbol": "DOGE", "reason": "operator", "expires_at_ms": NOW - 1}])
    assert admit_trigger(_fact(), now_ms=NOW, config=CONFIG, blacklist=expired) is None


def test_the_frames_own_frozen_numbers_bind_it_as_context_too() -> None:
    """#264 gave the liquidity floor one owner; `admit_context` is what keeps that true everywhere.

    Rank and the absolute floor say whether a fact may ground a capital decision *at all* — as a
    trigger, or as the OI context a News trigger attaches. Checking them only on the trigger path let a
    News verdict freeze a case on a $1M, rank-50 frame, which is the population the floor was measured
    to exclude (the 10-50M bucket is the worst at +4h).
    """

    assert admit_context(_fact(), config=CONFIG) is None
    thin = admit_context(_fact(oi_value_usd=1_000_000), config=CONFIG)
    assert thin is not None and thin.reason == "oi_value_below_floor"
    deep = admit_context(_fact(rank_in_window=50), config=CONFIG)
    assert deep is not None and deep.reason == "rank_above_limit"

    # The situational rules are *not* here: an underlying already in flight is still legal context.
    assert admit_context(_fact(symbol="BTC"), config=CONFIG) is None


def test_the_reason_vocabulary_is_closed() -> None:
    """The read model aggregates on `reason`; an open key set is an unbounded label, not a report."""

    with pytest.raises(ValueError, match="trading_gate_reason_unknown"):
        defer(_fact(), stage="routing", reason="because_i_said_so")


def test_a_case_link_exists_exactly_when_a_case_was_created() -> None:
    linked = case_created(_fact(), case_id="c1")
    assert (linked.status, linked.reason, linked.case_id) == ("CASE_CREATED", "case_created", "c1")
    for refusal in (
        defer(_fact(), stage="market_context", reason="market_data_unavailable"),
        reject(_fact(), stage="market_context", reason="market_data_invalid"),
    ):
        assert refusal.case_id is None


def test_editing_a_threshold_starts_a_new_record_rather_than_rewriting_the_old_one() -> None:
    """The digest is half the durable key, which is what makes the ledger a ledger."""

    twenty = GateConfig.from_policy(EligibilityPolicy(min_oi_value_usd=20_000_000), venue_priority=("binance",))
    five = GateConfig.from_policy(EligibilityPolicy(min_oi_value_usd=5_000_000), venue_priority=("binance",))
    assert twenty.digest != five.digest
    # And the same numbers reached by a different route are the same record.
    assert twenty.digest == GateConfig(min_oi_value_usd=20_000_000, venue_priority=("binance",)).digest


def test_the_smart_money_ratio_is_not_a_gate_rule() -> None:
    """#265 §4 corrects #264: one strategy's Alpha threshold must not delete another strategy's data.

    The reader's `> 80%` rule dropped five of the seven frames meeting the target strategy's `> 50%`
    conditions in the last seven days. Re-imposing any ratio here would reproduce that defect one layer
    down, so the gate admits a 54.24% frame — TUT's — and leaves the threshold to a versioned strategy.
    """

    assert "whale_ratio_below_floor" not in GATE_REASONS
    assert admit_trigger(_fact(whale_oi_ratio_bps=5_424), now_ms=NOW, config=CONFIG, blacklist=OPEN_DENY) is None
    assert "whale_oi_ratio_bps" not in CONFIG.snapshot
