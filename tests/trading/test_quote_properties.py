"""What `validate_entry_quote` must be true of for *every* quote, not just the tabled ones.

`tests/test_execution_quote.py` pins each bound at its exact `==` / `<` / `>` point, which is what
catches an operator flipped from `>` to `>=`. What a table cannot say is that the boundary holds when
the other five dimensions are somewhere else — a bound checked after an earlier `return` is still
exactly right at its own boundary and unreachable in practice, and a table built one dimension at a
time cannot see that.

So these are properties over generated quotes: every result is a typed answer, every accepted
snapshot satisfies every bound it claims to have checked, and each bound still rejects one unit past
itself no matter where the other dimensions sit. `deadline=None` because the whole module is pure
arithmetic with no I/O, and a shared runner's scheduling is not a signal about it.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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
    validate_entry_quote,
)

pytestmark = pytest.mark.property

NOW_NS = 1_900_000_000_000_000_000
INSTRUMENT = "SOLUSDT-PERP.BINANCE"
REFERENCE = Decimal("100")
MAX_SPREAD_BPS = 30
MAX_DRIFT_BPS = 25


def _intent(**overrides: Any) -> TradeIntent:
    intent = trade_intent(
        case_id="case-quote-property",
        case_manifest_sha256="1" * 64,
        created_at_ms=NOW_NS // 1_000_000,
        reference_price=REFERENCE,
    )
    return replace(intent, **overrides) if overrides else intent


INTENT = _intent()
assert INTENT.max_spread_bps == MAX_SPREAD_BPS, "the module's boundary arithmetic follows the Intent's own caps"
assert INTENT.max_entry_drift_bps == MAX_DRIFT_BPS


def _quote(**values: Any) -> ExecutionQuote:
    payload: dict[str, Any] = {
        "instrument_id": INSTRUMENT,
        "bid": Decimal("99.90"),
        "ask": Decimal("100.00"),
        "ts_event_ns": NOW_NS,
        "ts_init_ns": NOW_NS,
        "stream_generation": 1,
    }
    payload.update(values)
    return ExecutionQuote(**payload)


def _validate(
    quote: ExecutionQuote | None,
    *,
    now_ns: int = NOW_NS,
    last: int | None = None,
) -> ExecutionQuoteSnapshotV1 | ExecutionQuoteRejectionV1:
    return validate_entry_quote(
        intent=INTENT,
        quote=quote,
        stage="Q1",
        now_ns=now_ns,
        last_accepted_ts_event_ns=last,
    )


def _reason(result: Any) -> str | None:
    return result.reason if isinstance(result, ExecutionQuoteRejectionV1) else None


# Generated quotes stay inside the shapes the venue can actually produce — a positive, non-crossed
# book, and clocks within a day of the evaluation instant. A generator that also produced negative
# prices would spend its budget re-proving `quote_book_invalid`, which the table already owns.
_DAY_NS = 86_400_000_000_000
_prices = st.decimals(min_value=Decimal("1"), max_value=Decimal("200"), places=2)
_offsets = st.integers(min_value=-_DAY_NS, max_value=_DAY_NS)


@st.composite
def _quotes(draw: Any) -> ExecutionQuote:
    bid = draw(_prices)
    spread = draw(st.decimals(min_value=Decimal(0), max_value=Decimal("2"), places=2))
    ts_event = NOW_NS + draw(_offsets)
    latency = draw(st.integers(min_value=0, max_value=_DAY_NS))
    return _quote(bid=bid, ask=bid + spread, ts_event_ns=ts_event, ts_init_ns=ts_event + latency)


@settings(deadline=None)
@given(quote=_quotes(), last=st.one_of(st.none(), st.integers(min_value=NOW_NS - _DAY_NS, max_value=NOW_NS)))
def test_every_quote_reaches_exactly_one_typed_answer(quote: ExecutionQuote, last: int | None) -> None:
    """No input escapes the contract: the answer is a snapshot or a rejection naming a known reason."""

    result = _validate(quote, last=last)

    assert isinstance(result, ExecutionQuoteSnapshotV1 | ExecutionQuoteRejectionV1)
    if isinstance(result, ExecutionQuoteRejectionV1):
        assert result.reason
        assert result.intent_id == INTENT.intent_id
        assert result.evaluated_at_ns == NOW_NS
    else:
        assert result.intent_id == INTENT.intent_id
        assert result.stage == "Q1"


@settings(deadline=None)
@given(quote=_quotes(), last=st.one_of(st.none(), st.integers(min_value=NOW_NS - _DAY_NS, max_value=NOW_NS)))
def test_an_accepted_snapshot_satisfies_every_bound_it_claims_to_have_checked(
    quote: ExecutionQuote,
    last: int | None,
) -> None:
    """Acceptance is not a mood. Each published field has to be inside the bound that admitted it."""

    result = _validate(quote, last=last)
    if not isinstance(result, ExecutionQuoteSnapshotV1):
        return

    assert result.receive_age_ns <= MAX_RECEIVE_AGE_NS
    assert result.event_age_ns <= MAX_EVENT_AGE_NS
    assert result.source_latency_ns <= MAX_SOURCE_LATENCY_NS
    assert quote.ts_event_ns <= NOW_NS + MAX_FUTURE_SKEW_NS
    assert quote.ts_init_ns <= NOW_NS + MAX_FUTURE_SKEW_NS
    assert result.spread_bps <= Decimal(MAX_SPREAD_BPS)
    assert result.reference_drift_bps <= Decimal(MAX_DRIFT_BPS)
    assert last is None or quote.ts_event_ns >= last
    # The side price is the ask for a buy, and it is what the drift was measured against.
    assert result.side == "buy"
    assert result.side_price == quote.ask
    assert result.bid == quote.bid and result.ask == quote.ask


@settings(deadline=None)
@given(quote=_quotes())
def test_the_same_inputs_always_produce_the_same_answer(quote: ExecutionQuote) -> None:
    """Pure and side-effect free: the authority may not depend on anything it was not handed."""

    assert _validate(quote) == _validate(quote)


@settings(deadline=None)
@given(
    event_offset=st.integers(min_value=0, max_value=MAX_EVENT_AGE_NS),
    latency=st.integers(min_value=0, max_value=MAX_SOURCE_LATENCY_NS),
)
def test_each_clock_bound_still_rejects_one_nanosecond_past_itself(event_offset: int, latency: int) -> None:
    """The `>` in each clock guard, generalised over where the other clocks happen to sit.

    A table fixes every dimension but one. That cannot distinguish a bound that is exactly right from
    a bound that is exactly right and unreachable because an earlier `return` shadows it, so the
    boundary is re-proved here from an arbitrary admissible starting point.
    """

    receive_age = min(event_offset, MAX_RECEIVE_AGE_NS)
    ts_init = NOW_NS - receive_age
    ts_event = min(ts_init, NOW_NS - event_offset)
    if ts_init - ts_event > latency:
        ts_event = ts_init - latency
    accepted = _quote(ts_event_ns=ts_event, ts_init_ns=ts_init)
    if _reason(_validate(accepted)) is not None:
        return  # this draw was already outside another bound; the shadowing case is the next one

    assert _reason(_validate(replace(accepted, ts_init_ns=NOW_NS - MAX_RECEIVE_AGE_NS - 1))) is not None
    assert _reason(_validate(replace(accepted, ts_event_ns=NOW_NS - MAX_EVENT_AGE_NS - 1))) in {
        "quote_event_stale",
        "quote_source_latency_exceeded",
    }
    assert (
        _reason(_validate(replace(accepted, ts_event_ns=NOW_NS + MAX_FUTURE_SKEW_NS + 1, ts_init_ns=NOW_NS)))
        == "quote_clock_invalid"
    ), "a future event with a present receive is an impossible clock before it is a skew"
    skewed = replace(
        accepted,
        ts_event_ns=NOW_NS + MAX_FUTURE_SKEW_NS + 1,
        ts_init_ns=NOW_NS + MAX_FUTURE_SKEW_NS + 1,
    )
    assert _reason(_validate(skewed)) == "quote_future_skew"
    assert _reason(_validate(accepted, last=accepted.ts_event_ns + 1)) == "quote_event_out_of_order"
    assert _reason(_validate(accepted, last=accepted.ts_event_ns)) is None, "equal is in order, not out of it"


@settings(deadline=None)
@given(bid=st.decimals(min_value=Decimal("50"), max_value=Decimal("150"), places=2))
def test_the_intent_window_is_half_open_wherever_the_book_sits(bid: Decimal) -> None:
    """`created_at <= now < valid_until`: active at its first instant, expired at its last."""

    quote = _quote(bid=bid, ask=bid)
    created_ns = INTENT.created_at_ms * 1_000_000
    expiry_ns = INTENT.valid_until_ms * 1_000_000

    assert _reason(_validate(quote, now_ns=created_ns - 1)) == "quote_intent_not_active"
    assert _reason(_validate(quote, now_ns=created_ns)) != "quote_intent_not_active"
    assert _reason(_validate(quote, now_ns=expiry_ns)) == "quote_intent_expired"
    assert _reason(_validate(quote, now_ns=expiry_ns - 1)) != "quote_intent_expired"


@settings(deadline=None)
@given(spread=st.integers(min_value=0, max_value=MAX_SPREAD_BPS))
def test_spread_and_drift_reject_the_first_basis_point_past_their_caps(spread: int) -> None:
    """Both caps are `>`, and both are measured in exact `Decimal` basis points, never in float."""

    half = REFERENCE * Decimal(spread) / Decimal(20_000)
    inside = _quote(bid=REFERENCE - half, ask=REFERENCE + half)
    over_spread = _quote(
        bid=REFERENCE - REFERENCE * Decimal(MAX_SPREAD_BPS + 1) / Decimal(20_000),
        ask=REFERENCE + REFERENCE * Decimal(MAX_SPREAD_BPS + 1) / Decimal(20_000),
    )
    drifted = _quote(
        bid=REFERENCE,
        ask=REFERENCE + REFERENCE * Decimal(MAX_DRIFT_BPS + 1) / Decimal(10_000),
    )

    assert _reason(_validate(inside)) is None
    assert _reason(_validate(over_spread)) == "quote_spread_exceeded"
    assert _reason(_validate(drifted)) in {"quote_spread_exceeded", "quote_reference_drift_exceeded"}
