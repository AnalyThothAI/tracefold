"""The fail-closed guards in `quote_authority`, each defect on its own.

`tests/test_execution_quote.py` pins the bounds a *plausible* quote is measured against, and does it
well. What no test reached is the layer underneath: the guards that exist for states a caller should
never produce — a crossed book, a negative timestamp, a submission fence whose quantity is missing.
Those lines execute on every valid quote and are never the reason for an answer, so coverage counts
them and nothing constrains them. The mutation batch is what made that concrete: eleven surviving
mutants on the clock guard alone, and `SubmissionFenceV1.from_outcome` — the check standing between
a Case and a provider write — with no test at all.

Two rules shape what is here. Every clause of an `or` is broken *on its own*, because a guard whose
clauses are only ever tested together cannot tell you which one is load-bearing and reads the same
whether it is `or` or `and`. And the bounds are pinned as literals, because a test that derives its
fixture from the constant it is checking moves with the constant and can never notice it change.

Deliberately free of `nautilus_trader`: this module is in the mutation batch's command, where a
3-second import is paid once per mutant.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from tests.trading_v3_fixtures import trade_intent
from tracefold.trading import TradeIntent
from tracefold.trading.quote_authority import (
    MAX_EVENT_AGE_NS,
    MAX_FUTURE_SKEW_NS,
    MAX_RECEIVE_AGE_NS,
    MAX_SOURCE_LATENCY_NS,
    ExecutionQuote,
    ExecutionQuoteRejectionV1,
    ExecutionQuoteSnapshotV1,
    SubmissionFenceV1,
    validate_entry_quote,
)

NOW_NS = 1_900_000_000_000_000_000
INSTRUMENT = "SOLUSDT-PERP.BINANCE"


def _intent(reference_price: Decimal = Decimal("100")) -> TradeIntent:
    return trade_intent(
        case_id="case-quote-guards",
        case_manifest_sha256="1" * 64,
        created_at_ms=NOW_NS // 1_000_000,
        reference_price=reference_price,
        target_notional=Decimal("10"),
    )


def _quote(**values: object) -> ExecutionQuote:
    payload: dict[str, object] = {
        "instrument_id": INSTRUMENT,
        "bid": Decimal("99.80"),
        "ask": Decimal("100"),
        "ts_event_ns": NOW_NS,
        "ts_init_ns": NOW_NS,
        "stream_generation": 1,
    }
    payload.update(values)
    return ExecutionQuote(**payload)  # type: ignore[arg-type]


def _validate(quote: ExecutionQuote, *, now_ns: int = NOW_NS, last: int | None = None) -> Any:
    return validate_entry_quote(
        intent=_intent(),
        quote=quote,
        stage="Q1",
        now_ns=now_ns,
        last_accepted_ts_event_ns=last,
    )


def _reason(quote: ExecutionQuote, *, now_ns: int = NOW_NS, last: int | None = None) -> str | None:
    result = _validate(quote, now_ns=now_ns, last=last)
    return result.reason if isinstance(result, ExecutionQuoteRejectionV1) else None


def _snapshot() -> ExecutionQuoteSnapshotV1:
    result = _validate(_quote())
    assert isinstance(result, ExecutionQuoteSnapshotV1), result
    return result


@dataclass
class _Outcome:
    """The four attributes `SubmissionFenceOutcome` declares, and nothing else."""

    submission_fence_version: str | None
    entry_client_order_id: str | None
    submission_quantity: Decimal | None
    entry_quote_q1: Any


def _outcome(**values: Any) -> _Outcome:
    payload: dict[str, Any] = {
        "submission_fence_version": "submission_fence_v1",
        "entry_client_order_id": "order-1",
        "submission_quantity": Decimal("3"),
        "entry_quote_q1": _snapshot(),
    }
    payload.update(values)
    return _Outcome(**payload)


# --- the book guard: `bid <= 0 or ask <= 0 or bid > ask` ---------------------------------------


def test_a_well_formed_book_is_not_rejected() -> None:
    """The negative case for every guard below; without it `or` -> `and` is unobservable."""

    assert _reason(_quote()) is None


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"bid": Decimal("0")}, "bid at zero, ask healthy, book not crossed"),
        ({"bid": Decimal("-1")}, "bid below zero, ask healthy, book not crossed"),
        ({"bid": Decimal("101")}, "book crossed, both sides positive"),
    ],
)
def test_each_independently_reachable_book_defect_is_rejected_alone(overrides: dict[str, object], label: str) -> None:
    """Only `bid <= 0` and `bid > ask` can be the *sole* true clause; see the next test for why."""

    assert _reason(_quote(**overrides)) == "quote_book_invalid", label


def test_a_non_positive_ask_is_never_the_only_thing_wrong() -> None:
    """`ask <= 0` cannot be isolated, and saying so is more useful than a fixture that pretends.

    For `ask <= 0` to be the only true clause the bid would have to be positive and no greater than
    the ask — that is, `0 < bid <= ask <= 0`, which is empty. Every non-positive ask is either paired
    with a non-positive bid or is a crossed book. The clause is still worth keeping: it is what makes
    the rejection true for the right reason rather than by arithmetic accident.
    """

    assert _reason(_quote(ask=Decimal("0"))) == "quote_book_invalid"
    assert _reason(_quote(ask=Decimal("0"), bid=Decimal("-1"))) == "quote_book_invalid"


def test_a_book_is_valid_when_bid_exactly_equals_ask() -> None:
    """`bid > ask` and not `>=`: a zero spread is a real, tradeable book."""

    assert _reason(_quote(bid=Decimal("100"), ask=Decimal("100"))) is None


# --- the clock guard: `ts_event < 0 or ts_init < 0 or now < 0 or ts_init < ts_event` -----------


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"ts_event_ns": -1, "ts_init_ns": 0}, "event before the epoch, init after it and not earlier"),
        ({"ts_init_ns": NOW_NS - 1}, "init before event, both after the epoch"),
    ],
)
def test_each_independently_reachable_clock_defect_is_rejected_alone(overrides: dict[str, object], label: str) -> None:
    assert _reason(_quote(**overrides)) == "quote_clock_invalid", label


def test_a_negative_init_timestamp_is_always_also_out_of_order() -> None:
    """`ts_init_ns < 0` alone would need `0 <= ts_event_ns <= ts_init_ns < 0`, which is empty."""

    assert _reason(_quote(ts_init_ns=-1)) == "quote_clock_invalid"


def test_a_negative_clock_is_answered_by_the_intent_window_before_the_clock_guard() -> None:
    """`now_ns < 0` in the clock guard is unreachable, and the batch is how that surfaced.

    `created_at_ns` is a millisecond count scaled by 1_000_000 and is never negative, so any negative
    `now_ns` satisfies `now_ns < created_at_ns` and returns `quote_intent_not_active` two guards
    earlier. This test pins the ordering that makes that true rather than the dead clause: if the
    intent-window check ever moves below the clock check, this fails and the clause becomes live.
    """

    assert _reason(_quote(), now_ns=-1) == "quote_intent_not_active"


def test_a_clock_is_valid_when_init_exactly_equals_event() -> None:
    """`ts_init < ts_event` and not `<=`: zero source latency is the best case, not a fault."""

    assert _reason(_quote(ts_event_ns=NOW_NS, ts_init_ns=NOW_NS)) is None


# --- the skew guard: `ts_event > now + SKEW or ts_init > now + SKEW` ---------------------------


def test_a_receive_timestamp_alone_can_be_too_far_in_the_future() -> None:
    """`ts_init_ns` is the isolable half: it may run ahead of an event timestamp that is fine."""

    assert _reason(_quote(ts_init_ns=NOW_NS + MAX_FUTURE_SKEW_NS + 1)) == "quote_future_skew"


def test_an_event_timestamp_in_the_future_drags_the_receive_timestamp_with_it() -> None:
    """The event half cannot be isolated: `ts_init_ns < ts_event_ns` is rejected a guard earlier."""

    ahead = NOW_NS + MAX_FUTURE_SKEW_NS + 1
    assert _reason(_quote(ts_event_ns=ahead, ts_init_ns=ahead)) == "quote_future_skew"
    assert _reason(_quote(ts_event_ns=ahead)) == "quote_clock_invalid"


def test_a_timestamp_exactly_at_the_skew_bound_is_accepted() -> None:
    at_bound = NOW_NS + MAX_FUTURE_SKEW_NS
    assert _reason(_quote(ts_event_ns=at_bound, ts_init_ns=at_bound)) is None


# --- the bounds themselves --------------------------------------------------------------------


def test_the_quote_bounds_are_the_documented_nanosecond_values() -> None:
    """Pinned as literals on purpose.

    Every other test derives its fixture from these constants, so all of them keep passing if a
    constant moves — the fixture moves with it. Only a literal notices that the accepted event age
    silently became thirty seconds.
    """

    assert MAX_RECEIVE_AGE_NS == 2_000_000_000
    assert MAX_EVENT_AGE_NS == 3_000_000_000
    assert MAX_SOURCE_LATENCY_NS == 1_000_000_000
    assert MAX_FUTURE_SKEW_NS == 500_000_000


# --- the submission fence ---------------------------------------------------------------------


def test_a_complete_outcome_produces_the_fence_it_describes() -> None:
    fence = SubmissionFenceV1.from_outcome(_outcome())  # type: ignore[arg-type]
    assert fence.client_order_id == "order-1"
    assert fence.quantity == Decimal("3")
    assert fence.q1_evidence.stage == "Q1"


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"submission_fence_version": "submission_fence_v2"}, "a version this code does not implement"),
        ({"submission_fence_version": None}, "no version at all"),
        ({"entry_client_order_id": None}, "no client order id"),
        ({"submission_quantity": None}, "no quantity"),
        ({"submission_quantity": Decimal("0")}, "a quantity of zero"),
        ({"submission_quantity": Decimal("-1")}, "a negative quantity"),
        ({"entry_quote_q1": None}, "no Q1 evidence"),
        ({"entry_quote_q1": "execution_quote_snapshot_v1"}, "Q1 evidence that is not a snapshot"),
    ],
)
def test_each_defect_alone_refuses_the_fence(overrides: dict[str, Any], label: str) -> None:
    """Every clause independently, because this is the last check before a provider write."""

    with pytest.raises(ValueError, match="submission_fence_v1_invalid"):
        SubmissionFenceV1.from_outcome(_outcome(**overrides))  # type: ignore[arg-type]


def test_q1_evidence_from_the_wrong_stage_refuses_the_fence() -> None:
    """A Q2 snapshot is a well-formed snapshot; only the stage says it is the wrong evidence."""

    q2 = _snapshot().model_copy(update={"stage": "Q2"})
    with pytest.raises(ValueError, match="submission_fence_v1_invalid"):
        SubmissionFenceV1.from_outcome(_outcome(entry_quote_q1=q2))  # type: ignore[arg-type]


def test_a_quantity_of_exactly_one_smallest_unit_is_allowed() -> None:
    """`<= 0` and not `< 0`, and not `<= 1`: the floor is zero, and zero is excluded."""

    fence = SubmissionFenceV1.from_outcome(_outcome(submission_quantity=Decimal("0.00000001")))  # type: ignore[arg-type]
    assert fence.quantity == Decimal("0.00000001")


# --- identity, ordering and the recorded ages ---------------------------------------------------


@pytest.mark.parametrize("other", ["AAAUSDT-PERP.BINANCE", "ZZZUSDT-PERP.BINANCE"])
def test_an_instrument_mismatch_is_rejected_whichever_way_it_sorts(other: str) -> None:
    """`!=` and not an ordering.

    One mismatch on its own cannot tell the two apart: a wrong instrument that happens to sort
    before the intended one is rejected either way. Only the pair does, and the pair is what a
    provider actually hands you — the id is an identity, and identities do not have a direction.
    """

    assert _reason(_quote(instrument_id=other)) == "quote_instrument_mismatch"


def test_an_intent_is_expired_past_its_window_and_not_only_exactly_on_it() -> None:
    """`>=` and not `==`: a quote arriving a nanosecond after expiry is late, not valid again."""

    valid_until_ns = _intent().valid_until_ms * 1_000_000
    assert _reason(_quote(), now_ns=valid_until_ns) == "quote_intent_expired"
    assert _reason(_quote(), now_ns=valid_until_ns + 1) == "quote_intent_expired"


def test_a_current_quote_records_ages_of_exactly_zero() -> None:
    """The `max(0, ...)` floor is zero.

    Nothing else in the suite reads these numbers back, so a floor of one would ship an
    off-by-one into every recorded snapshot without failing anything.
    """

    snapshot = _snapshot()
    assert snapshot.receive_age_ns == 0
    assert snapshot.event_age_ns == 0
    assert snapshot.source_latency_ns == 0


def test_a_quote_at_or_after_the_last_accepted_event_timestamp_is_accepted() -> None:
    """`<` and not `!=`: a repeat of the last accepted timestamp is not out of order."""

    assert _reason(_quote(), last=NOW_NS) is None
    assert _reason(_quote(), last=NOW_NS - 1) is None


def test_a_quote_before_the_last_accepted_event_timestamp_is_refused() -> None:
    assert _reason(_quote(), last=NOW_NS + 1) == "quote_event_out_of_order"


# --- immutability of the evidence ---------------------------------------------------------------


def test_an_execution_quote_cannot_be_mutated_after_construction() -> None:
    with pytest.raises(FrozenInstanceError):
        _quote().bid = Decimal("1")  # type: ignore[misc]


def test_a_rejection_cannot_be_mutated_after_construction() -> None:
    """It is written to the admission ledger as the reason a Case did not trade."""

    rejection = _validate(_quote(bid=Decimal("0")))
    assert isinstance(rejection, ExecutionQuoteRejectionV1)
    with pytest.raises(ValidationError):
        rejection.reason = "accepted"  # type: ignore[assignment]


def test_a_submission_fence_cannot_be_mutated_after_construction() -> None:
    """The quantity is committed before the provider write; nothing may edit it afterwards."""

    fence = SubmissionFenceV1.from_outcome(_outcome())  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        fence.quantity = Decimal("999")  # type: ignore[misc]


# --- boundaries only a specific quote can reach -------------------------------------------------


def test_a_timestamp_of_exactly_zero_is_ancient_rather_than_malformed() -> None:
    """The epoch bound is `< 0`, so a timestamp of exactly zero passes it and is answered by age.

    This is the only quote that separates `< 0` from `< 1` or `<= 0` on either clock field. Both
    answers are a rejection, so a test that only asked "was this refused" could not tell them
    apart; the reason is what carries the distinction into the admission ledger.
    """

    assert _reason(_quote(ts_event_ns=0, ts_init_ns=0)) == "quote_receive_stale"


def test_a_quote_marginally_ahead_of_our_clock_records_a_zero_age_not_a_negative_one() -> None:
    """The `max(0, ...)` floor, at the only input that can reach it.

    A receive timestamp is allowed to run up to `MAX_FUTURE_SKEW_NS` ahead of us, so `now - ts_init`
    genuinely goes negative in normal operation. A floor of `-1` would persist a negative age into
    the snapshot rather than fail anything.
    """

    result = _validate(_quote(ts_event_ns=NOW_NS + 500, ts_init_ns=NOW_NS + 1_000))
    assert isinstance(result, ExecutionQuoteSnapshotV1), result
    assert result.receive_age_ns == 0
    assert result.event_age_ns == 0
    assert result.source_latency_ns == 500


def test_a_book_priced_below_one_is_a_valid_book() -> None:
    """The floor on both sides is zero, not one — most perpetual contracts trade under a dollar."""

    result = validate_entry_quote(
        intent=_intent(reference_price=Decimal("0.5")),
        quote=_quote(bid=Decimal("0.4995"), ask=Decimal("0.5000")),
        stage="Q1",
        now_ns=NOW_NS,
        last_accepted_ts_event_ns=None,
    )
    assert isinstance(result, ExecutionQuoteSnapshotV1), result


def test_reference_drift_is_a_difference_and_not_a_remainder() -> None:
    """`side_price - reference` and not `side_price % reference`, in both directions.

    Every other drift fixture sits inside `ref <= price < 2*ref`, where `a % b` and `a - b` are the
    same number — which is why a mutant swapping them survived the batch and was filed as
    equivalent. It is not. Below the reference the remainder is the whole price, so a fill 10 bps
    *better* than intended reads as 9990 bps of drift and is refused. At exactly twice the reference
    the remainder is zero, so a quote at double the intended price reports no drift at all and is
    admitted — against a bound of 25 bps.
    """

    assert _reason(_quote(bid=Decimal("99.85"), ask=Decimal("99.90"))) is None
    assert _reason(_quote(bid=Decimal("199.90"), ask=Decimal("200.00"))) == "quote_reference_drift_exceeded"


def test_reference_drift_is_reported_in_basis_points() -> None:
    """Pins the `_BPS` scale.

    Every other assertion about drift is a comparison against a bound that is itself in basis
    points, so the scale cancels and a wrong one is invisible. An exact value is the only thing
    that notices `Decimal(10_000)` becoming `Decimal(9_999)`.
    """

    result = _validate(_quote(bid=Decimal("99.90"), ask=Decimal("100.10")))
    assert isinstance(result, ExecutionQuoteSnapshotV1), result
    assert result.reference_drift_bps == Decimal(10)
    assert result.spread_bps == Decimal(20)
