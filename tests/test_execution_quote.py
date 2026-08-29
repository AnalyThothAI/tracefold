"""Pure boundary table for Intent-level execution quote authority (#303)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model.data import Bar, MarkPriceUpdate
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from pydantic import ValidationError

from tracefold.integrations.nautilus.quote_authority import (
    execution_quote_from_nautilus,
)
from tracefold.trading import BlacklistSnapshotV1, TradeIntent
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

NOW_NS = 1_900_000_000_000_000_000
INSTRUMENT = InstrumentId.from_str("SOLUSDT-PERP.BINANCE")


def _intent() -> TradeIntent:
    return TradeIntent.create(
        case_id="case-quote",
        case_manifest_sha256="1" * 64,
        execution_capability_snapshot_sha256="2" * 64,
        blacklist_snapshot=BlacklistSnapshotV1(revision=0, active_rows=()),
        instrument_id=INSTRUMENT.value,
        underlying_key="crypto:SOL",
        created_at_ms=NOW_NS // 1_000_000,
        reference_price=Decimal("100"),
        target_notional_usd=Decimal("10"),
    )


def _quote(**values: object) -> ExecutionQuote:
    payload: dict[str, object] = {
        "instrument_id": INSTRUMENT.value,
        "bid": Decimal("99.80"),
        "ask": Decimal("100"),
        "ts_event_ns": NOW_NS,
        "ts_init_ns": NOW_NS,
        "stream_generation": 1,
    }
    payload.update(values)
    return ExecutionQuote(**payload)  # type: ignore[arg-type]


def _reason(quote: ExecutionQuote | None, *, now_ns: int = NOW_NS, last: int | None = None) -> str | None:
    result = validate_entry_quote(
        intent=_intent(),
        quote=quote,
        stage="Q1",
        now_ns=now_ns,
        last_accepted_ts_event_ns=last,
    )
    return result.reason if isinstance(result, ExecutionQuoteRejectionV1) else None


@pytest.mark.parametrize(
    ("quote", "reason"),
    [
        (None, "quote_missing"),
        (_quote(instrument_id="BTCUSDT-PERP.BINANCE"), "quote_instrument_mismatch"),
        (_quote(bid=Decimal(0)), "quote_book_invalid"),
        (_quote(ask=Decimal(0)), "quote_book_invalid"),
        (_quote(bid=Decimal("100.01")), "quote_book_invalid"),
    ],
)
def test_quote_identity_and_book_rejections_are_typed(quote: ExecutionQuote | None, reason: str) -> None:
    assert _reason(quote) == reason


@pytest.mark.parametrize(
    ("quote", "now_ns", "last", "reason"),
    [
        (
            _quote(
                ts_event_ns=NOW_NS - MAX_RECEIVE_AGE_NS,
                ts_init_ns=NOW_NS - MAX_RECEIVE_AGE_NS,
            ),
            NOW_NS,
            None,
            None,
        ),
        (
            _quote(
                ts_event_ns=NOW_NS - MAX_RECEIVE_AGE_NS - 1,
                ts_init_ns=NOW_NS - MAX_RECEIVE_AGE_NS - 1,
            ),
            NOW_NS,
            None,
            "quote_receive_stale",
        ),
        (
            _quote(ts_event_ns=NOW_NS - MAX_EVENT_AGE_NS, ts_init_ns=NOW_NS - MAX_RECEIVE_AGE_NS),
            NOW_NS,
            None,
            None,
        ),
        (
            _quote(ts_event_ns=NOW_NS - MAX_EVENT_AGE_NS - 1, ts_init_ns=NOW_NS - MAX_RECEIVE_AGE_NS),
            NOW_NS,
            None,
            "quote_event_stale",
        ),
        (
            _quote(ts_event_ns=NOW_NS - MAX_SOURCE_LATENCY_NS, ts_init_ns=NOW_NS),
            NOW_NS,
            None,
            None,
        ),
        (
            _quote(ts_event_ns=NOW_NS - MAX_SOURCE_LATENCY_NS - 1, ts_init_ns=NOW_NS),
            NOW_NS,
            None,
            "quote_source_latency_exceeded",
        ),
        (_quote(ts_event_ns=NOW_NS + MAX_FUTURE_SKEW_NS, ts_init_ns=NOW_NS + MAX_FUTURE_SKEW_NS), NOW_NS, None, None),
        (
            _quote(
                ts_event_ns=NOW_NS + MAX_FUTURE_SKEW_NS + 1,
                ts_init_ns=NOW_NS + MAX_FUTURE_SKEW_NS + 1,
            ),
            NOW_NS,
            None,
            "quote_future_skew",
        ),
        (_quote(), NOW_NS, NOW_NS, None),
        (_quote(ts_event_ns=NOW_NS - 1, ts_init_ns=NOW_NS), NOW_NS, NOW_NS, "quote_event_out_of_order"),
    ],
)
def test_quote_clock_boundaries(
    quote: ExecutionQuote,
    now_ns: int,
    last: int | None,
    reason: str | None,
) -> None:
    assert _reason(quote, now_ns=now_ns, last=last) == reason


def test_intent_clock_boundaries_are_closed() -> None:
    assert _reason(_quote(), now_ns=NOW_NS - 1) == "quote_intent_not_active"
    assert _reason(_quote(), now_ns=NOW_NS + 60_000_000_000) == "quote_intent_expired"
    assert _reason(_quote(ts_init_ns=NOW_NS - 1)) == "quote_clock_invalid"


def test_spread_and_reference_drift_use_decimal_boundary_math() -> None:
    intent = _intent()
    spread_boundary = _quote(bid=Decimal("99.85"), ask=Decimal("100.15"))
    drift_boundary = _quote(bid=Decimal("99.95"), ask=Decimal("100.25"))

    assert isinstance(
        validate_entry_quote(
            intent=intent,
            quote=spread_boundary,
            stage="Q1",
            now_ns=NOW_NS,
            last_accepted_ts_event_ns=None,
        ),
        ExecutionQuoteSnapshotV1,
    )
    assert isinstance(
        validate_entry_quote(
            intent=intent,
            quote=drift_boundary,
            stage="Q1",
            now_ns=NOW_NS,
            last_accepted_ts_event_ns=None,
        ),
        ExecutionQuoteSnapshotV1,
    )
    assert _reason(_quote(bid=Decimal("99.70"), ask=Decimal("100"))) == "quote_spread_exceeded"
    assert _reason(replace(spread_boundary, bid=Decimal("99.8499"))) == "quote_spread_exceeded"
    assert _reason(replace(drift_boundary, ask=Decimal("100.2501"))) == "quote_reference_drift_exceeded"


def test_sell_execution_uses_the_bid_side() -> None:
    long_intent = _intent()
    sell_intent = SimpleNamespace(
        intent_id=long_intent.intent_id,
        instrument_id=long_intent.instrument_id,
        side="sell",
        created_at_ms=long_intent.created_at_ms,
        valid_until_ms=long_intent.valid_until_ms,
        reference_price=Decimal("99.80"),
        max_spread_bps=long_intent.max_spread_bps,
        max_entry_drift_bps=long_intent.max_entry_drift_bps,
    )

    result = validate_entry_quote(
        intent=sell_intent,
        quote=_quote(),
        stage="Q1",
        now_ns=NOW_NS,
        last_accepted_ts_event_ns=None,
    )

    assert isinstance(result, ExecutionQuoteSnapshotV1)
    assert (result.side, result.side_price) == ("sell", Decimal("99.80"))


def test_only_a_nautilus_quote_tick_can_construct_the_execution_input() -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    tick = TestDataStubs.quote_tick(instrument=instrument, ts_event=NOW_NS, ts_init=NOW_NS)
    execution_quote = execution_quote_from_nautilus(tick, stream_generation=4)

    assert execution_quote.instrument_id == instrument.id.value
    assert execution_quote.stream_generation == 4
    for wrong_semantics in (Bar, MarkPriceUpdate, Decimal("100")):
        with pytest.raises(TypeError, match="execution_quote_requires_nautilus_quote_tick"):
            execution_quote_from_nautilus(wrong_semantics, stream_generation=4)  # type: ignore[arg-type]


def test_non_execution_price_objects_cannot_validate_as_entry_quotes() -> None:
    result = validate_entry_quote(
        intent=_intent(),
        quote=Decimal("100"),  # type: ignore[arg-type]
        stage="Q1",
        now_ns=NOW_NS,
        last_accepted_ts_event_ns=None,
    )
    assert isinstance(result, ExecutionQuoteRejectionV1)
    assert (result.reason, result.intent_id, result.instrument_id, result.side, result.stage) == (
        "quote_type_invalid",
        _intent().intent_id,
        INSTRUMENT.value,
        "buy",
        "Q1",
    )


def test_quote_audit_is_frozen_and_carries_its_durable_version_and_identity() -> None:
    result = validate_entry_quote(
        intent=_intent(),
        quote=_quote(),
        stage="Q2",
        now_ns=NOW_NS,
        last_accepted_ts_event_ns=None,
    )

    assert isinstance(result, ExecutionQuoteSnapshotV1)
    assert (result.snapshot_version, result.stage, result.intent_id) == (
        "execution_quote_snapshot_v1",
        "Q2",
        _intent().intent_id,
    )
    with pytest.raises(ValidationError, match="frozen"):
        result.stage = "Q1"  # type: ignore[misc]
