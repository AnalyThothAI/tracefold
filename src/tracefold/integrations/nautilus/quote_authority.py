"""Intent-scoped entry authority derived only from Nautilus ``QuoteTick`` values."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, Protocol

from nautilus_trader.model.data import QuoteTick

from tracefold.trading import IntentOutcome

MAX_RECEIVE_AGE_NS: Final = 2_000_000_000
MAX_EVENT_AGE_NS: Final = 3_000_000_000
MAX_SOURCE_LATENCY_NS: Final = 1_000_000_000
MAX_FUTURE_SKEW_NS: Final = 500_000_000
_BPS: Final = Decimal(10_000)

QuoteRejectionReason = Literal[
    "quote_missing",
    "quote_type_invalid",
    "quote_instrument_mismatch",
    "quote_book_invalid",
    "quote_side_unsupported",
    "quote_intent_not_active",
    "quote_intent_expired",
    "quote_clock_invalid",
    "quote_receive_stale",
    "quote_event_stale",
    "quote_source_latency_exceeded",
    "quote_future_skew",
    "quote_event_out_of_order",
    "quote_spread_exceeded",
    "quote_reference_drift_exceeded",
]


class EntryQuoteIntent(Protocol):
    instrument_id: str
    side: str
    created_at_ms: int
    valid_until_ms: int
    reference_price: Decimal
    max_spread_bps: int
    max_entry_drift_bps: int


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    """The only input type that can authorize entry; constructed from a Nautilus quote tick."""

    instrument_id: str
    bid: Decimal
    ask: Decimal
    ts_event_ns: int
    ts_init_ns: int
    stream_generation: int

    @classmethod
    def from_nautilus(cls, quote: QuoteTick, *, stream_generation: int) -> ExecutionQuote:
        if not isinstance(quote, QuoteTick):
            raise TypeError("execution_quote_requires_nautilus_quote_tick")
        if stream_generation < 0:
            raise ValueError("execution_quote_generation_invalid")
        return cls(
            instrument_id=quote.instrument_id.value,
            bid=quote.bid_price.as_decimal(),
            ask=quote.ask_price.as_decimal(),
            ts_event_ns=int(quote.ts_event),
            ts_init_ns=int(quote.ts_init),
            stream_generation=stream_generation,
        )


@dataclass(frozen=True, slots=True)
class ExecutionQuoteSnapshot:
    """Bounded Q1/Q2 evidence for one accepted executable side price."""

    instrument_id: str
    side: Literal["buy", "sell"]
    side_price: Decimal
    bid: Decimal
    ask: Decimal
    ts_event_ns: int
    ts_init_ns: int
    evaluated_at_ns: int
    stream_generation: int
    receive_age_ns: int
    event_age_ns: int
    source_latency_ns: int
    spread_bps: Decimal
    reference_drift_bps: Decimal

    def as_payload(self, *, stage: Literal["Q1", "Q2"]) -> dict[str, str | int]:
        return {
            "snapshot_version": "execution_quote_snapshot_v1",
            "stage": stage,
            "reason": "accepted",
            "instrument_id": self.instrument_id,
            "side": self.side,
            "side_price": str(self.side_price),
            "bid": str(self.bid),
            "ask": str(self.ask),
            "ts_event_ns": self.ts_event_ns,
            "ts_init_ns": self.ts_init_ns,
            "evaluated_at_ns": self.evaluated_at_ns,
            "stream_generation": self.stream_generation,
            "receive_age_ns": self.receive_age_ns,
            "event_age_ns": self.event_age_ns,
            "source_latency_ns": self.source_latency_ns,
            "spread_bps": str(self.spread_bps),
            "reference_drift_bps": str(self.reference_drift_bps),
        }


@dataclass(frozen=True, slots=True)
class SubmissionFenceV1:
    """The exact quantity and Q1 evidence committed before Q2 or any provider write."""

    client_order_id: str
    quantity: Decimal
    q1_evidence: dict[str, str | int]

    @classmethod
    def from_outcome(cls, outcome: IntentOutcome) -> SubmissionFenceV1:
        if (
            outcome.submission_fence_version != "submission_fence_v1"
            or outcome.entry_client_order_id is None
            or outcome.submission_quantity is None
            or outcome.submission_quantity <= 0
            or outcome.entry_quote_q1 is None
            or outcome.entry_quote_q1.get("snapshot_version") != "execution_quote_snapshot_v1"
            or outcome.entry_quote_q1.get("stage") != "Q1"
            or outcome.entry_quote_q1.get("reason") != "accepted"
        ):
            raise ValueError("submission_fence_v1_invalid")
        return cls(
            client_order_id=outcome.entry_client_order_id,
            quantity=outcome.submission_quantity,
            q1_evidence=outcome.entry_quote_q1,
        )


@dataclass(frozen=True, slots=True)
class QuoteRejection:
    """A closed, typed no-submit result from the quote authority."""

    reason: QuoteRejectionReason
    instrument_id: str | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    ts_event_ns: int | None = None
    ts_init_ns: int | None = None
    stream_generation: int | None = None
    receive_age_ns: int | None = None
    event_age_ns: int | None = None
    source_latency_ns: int | None = None
    spread_bps: Decimal | None = None
    reference_drift_bps: Decimal | None = None

    def as_payload(self, *, stage: Literal["Q1", "Q2"], evaluated_at_ns: int) -> dict[str, str | int]:
        payload: dict[str, str | int] = {
            "snapshot_version": "execution_quote_rejection_v1",
            "stage": stage,
            "reason": self.reason,
            "evaluated_at_ns": evaluated_at_ns,
        }
        for name in (
            "instrument_id",
            "bid",
            "ask",
            "ts_event_ns",
            "ts_init_ns",
            "stream_generation",
            "receive_age_ns",
            "event_age_ns",
            "source_latency_ns",
            "spread_bps",
            "reference_drift_bps",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = str(value) if isinstance(value, Decimal) else value
        return payload


def validate_entry_quote(
    *,
    intent: EntryQuoteIntent,
    quote: ExecutionQuote | None,
    now_ns: int,
    last_accepted_ts_event_ns: int | None,
) -> ExecutionQuoteSnapshot | QuoteRejection:
    """Validate one exact, side-aware quote without I/O or mutable state."""

    if quote is None:
        return QuoteRejection("quote_missing")
    if not isinstance(quote, ExecutionQuote):
        return QuoteRejection("quote_type_invalid")

    def rejected(reason: QuoteRejectionReason, **evidence: Decimal | int) -> QuoteRejection:
        return QuoteRejection(
            reason=reason,
            instrument_id=quote.instrument_id,
            bid=quote.bid,
            ask=quote.ask,
            ts_event_ns=quote.ts_event_ns,
            ts_init_ns=quote.ts_init_ns,
            stream_generation=quote.stream_generation,
            **evidence,
        )

    if quote.instrument_id != intent.instrument_id:
        return rejected("quote_instrument_mismatch")
    if quote.bid <= 0 or quote.ask <= 0 or quote.bid > quote.ask:
        return rejected("quote_book_invalid")

    if intent.side in {"long", "buy"}:
        side: Literal["buy", "sell"] = "buy"
        side_price = quote.ask
    elif intent.side in {"short", "sell"}:
        side = "sell"
        side_price = quote.bid
    else:
        return rejected("quote_side_unsupported")

    created_at_ns = intent.created_at_ms * 1_000_000
    valid_until_ns = intent.valid_until_ms * 1_000_000
    if now_ns < created_at_ns:
        return rejected("quote_intent_not_active")
    if now_ns >= valid_until_ns:
        return rejected("quote_intent_expired")
    if quote.ts_event_ns < 0 or quote.ts_init_ns < 0 or now_ns < 0 or quote.ts_init_ns < quote.ts_event_ns:
        return rejected("quote_clock_invalid")
    if quote.ts_event_ns > now_ns + MAX_FUTURE_SKEW_NS or quote.ts_init_ns > now_ns + MAX_FUTURE_SKEW_NS:
        return rejected("quote_future_skew")

    receive_age_ns = max(0, now_ns - quote.ts_init_ns)
    event_age_ns = max(0, now_ns - quote.ts_event_ns)
    source_latency_ns = quote.ts_init_ns - quote.ts_event_ns
    clocks = {
        "receive_age_ns": receive_age_ns,
        "event_age_ns": event_age_ns,
        "source_latency_ns": source_latency_ns,
    }
    if receive_age_ns > MAX_RECEIVE_AGE_NS:
        return rejected("quote_receive_stale", **clocks)
    if event_age_ns > MAX_EVENT_AGE_NS:
        return rejected("quote_event_stale", **clocks)
    if source_latency_ns > MAX_SOURCE_LATENCY_NS:
        return rejected("quote_source_latency_exceeded", **clocks)
    if last_accepted_ts_event_ns is not None and quote.ts_event_ns < last_accepted_ts_event_ns:
        return rejected("quote_event_out_of_order", **clocks)

    spread_bps = (quote.ask - quote.bid) * _BPS / quote.ask
    reference_drift_bps = abs(side_price - intent.reference_price) * _BPS / intent.reference_price
    if spread_bps > Decimal(intent.max_spread_bps):
        return rejected(
            "quote_spread_exceeded",
            **clocks,
            spread_bps=spread_bps,
            reference_drift_bps=reference_drift_bps,
        )
    if reference_drift_bps > Decimal(intent.max_entry_drift_bps):
        return rejected(
            "quote_reference_drift_exceeded",
            **clocks,
            spread_bps=spread_bps,
            reference_drift_bps=reference_drift_bps,
        )
    return ExecutionQuoteSnapshot(
        instrument_id=quote.instrument_id,
        side=side,
        side_price=side_price,
        bid=quote.bid,
        ask=quote.ask,
        ts_event_ns=quote.ts_event_ns,
        ts_init_ns=quote.ts_init_ns,
        evaluated_at_ns=now_ns,
        stream_generation=quote.stream_generation,
        receive_age_ns=receive_age_ns,
        event_age_ns=event_age_ns,
        source_latency_ns=source_latency_ns,
        spread_bps=spread_bps,
        reference_drift_bps=reference_drift_bps,
    )


__all__ = [
    "MAX_EVENT_AGE_NS",
    "MAX_FUTURE_SKEW_NS",
    "MAX_RECEIVE_AGE_NS",
    "MAX_SOURCE_LATENCY_NS",
    "EntryQuoteIntent",
    "ExecutionQuote",
    "ExecutionQuoteSnapshot",
    "QuoteRejection",
    "QuoteRejectionReason",
    "SubmissionFenceV1",
    "validate_entry_quote",
]
