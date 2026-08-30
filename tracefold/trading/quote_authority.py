"""Intent-scoped, provider-neutral execution Quote authority."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .contracts import canonical_sha256
from .execution_policy import entry_spread_bps

MAX_RECEIVE_AGE_NS: Final = 2_000_000_000
MAX_EVENT_AGE_NS: Final = 3_000_000_000
MAX_SOURCE_LATENCY_NS: Final = 1_000_000_000
MAX_FUTURE_SKEW_NS: Final = 500_000_000
_BPS: Final = Decimal(10_000)
QUOTE_CONTRACT_VERSION: Final = "execution_quote_contract_v1"
QUOTE_CONTRACT_SHA256: Final = canonical_sha256(
    {
        "version": QUOTE_CONTRACT_VERSION,
        "max_receive_age_ns": MAX_RECEIVE_AGE_NS,
        "max_event_age_ns": MAX_EVENT_AGE_NS,
        "max_source_latency_ns": MAX_SOURCE_LATENCY_NS,
        "max_future_skew_ns": MAX_FUTURE_SKEW_NS,
        "side_price": {"long": "ask", "short": "bid"},
        "q1": "freeze_quantity_and_quote_before_provider_write",
        "q2": "revalidate_same_fenced_quantity_immediately_before_provider_write",
    }
)
SUBMISSION_FENCE_VERSION: Final = "submission_fence_v1"
SUBMISSION_FENCE_SHA256: Final = canonical_sha256(
    {
        "version": SUBMISSION_FENCE_VERSION,
        "quote_contract_sha256": QUOTE_CONTRACT_SHA256,
        "identity": "intent_scoped_client_order_id_plus_exact_quantity_plus_q1_evidence",
        "provider_write_rule": "q2_accepts_same_fenced_quantity_then_one_write",
    }
)

QuoteStage = Literal["Q1", "Q2"]
ExecutionSide = Literal["buy", "sell"]
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
    intent_id: str
    instrument_id: str
    side: str
    created_at_ms: int
    valid_until_ms: int
    reference_price: Decimal
    max_spread_bps: int
    max_entry_drift_bps: int


class SubmissionFenceOutcome(Protocol):
    submission_fence_version: str | None
    entry_client_order_id: str | None
    submission_quantity: Decimal | None
    entry_quote_q1: ExecutionQuoteAuditV1 | None


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    """One provider-neutral top-of-book observation eligible for validation."""

    instrument_id: str
    bid: Decimal
    ask: Decimal
    ts_event_ns: int
    ts_init_ns: int
    stream_generation: int


class ExecutionQuoteSnapshotV1(BaseModel):
    """Frozen, versioned Q1/Q2 evidence for one accepted side price."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_version: Literal["execution_quote_snapshot_v1"] = "execution_quote_snapshot_v1"
    stage: QuoteStage
    reason: Literal["accepted"] = "accepted"
    intent_id: str
    instrument_id: str
    side: ExecutionSide
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


class ExecutionQuoteRejectionV1(BaseModel):
    """Frozen, versioned no-submit evidence from the Quote authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_version: Literal["execution_quote_rejection_v1"] = "execution_quote_rejection_v1"
    stage: QuoteStage
    reason: QuoteRejectionReason
    intent_id: str
    instrument_id: str
    side: ExecutionSide | None
    evaluated_at_ns: int
    observed_instrument_id: str | None = None
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


ExecutionQuoteAuditV1 = Annotated[
    ExecutionQuoteSnapshotV1 | ExecutionQuoteRejectionV1,
    Field(discriminator="snapshot_version"),
]


@dataclass(frozen=True, slots=True)
class SubmissionFenceV1:
    """The exact quantity and Q1 evidence committed before Q2 or a provider write."""

    client_order_id: str
    quantity: Decimal
    q1_evidence: ExecutionQuoteSnapshotV1

    @classmethod
    def from_outcome(cls, outcome: SubmissionFenceOutcome) -> SubmissionFenceV1:
        q1 = outcome.entry_quote_q1
        if (
            outcome.submission_fence_version != "submission_fence_v1"
            or outcome.entry_client_order_id is None
            or outcome.submission_quantity is None
            or outcome.submission_quantity <= 0
            or not isinstance(q1, ExecutionQuoteSnapshotV1)
            or q1.stage != "Q1"
        ):
            raise ValueError("submission_fence_v1_invalid")
        return cls(
            client_order_id=outcome.entry_client_order_id,
            quantity=outcome.submission_quantity,
            q1_evidence=q1,
        )


def validate_entry_quote(
    *,
    intent: EntryQuoteIntent,
    quote: ExecutionQuote | None,
    stage: QuoteStage,
    now_ns: int,
    last_accepted_ts_event_ns: int | None,
) -> ExecutionQuoteSnapshotV1 | ExecutionQuoteRejectionV1:
    """Validate one exact, side-aware quote without I/O or mutable state."""

    side = _execution_side(intent.side)

    def rejected(
        reason: QuoteRejectionReason,
        *,
        observed: ExecutionQuote | None = None,
        **evidence: Decimal | int,
    ) -> ExecutionQuoteRejectionV1:
        return ExecutionQuoteRejectionV1(
            stage=stage,
            reason=reason,
            intent_id=intent.intent_id,
            instrument_id=intent.instrument_id,
            side=side,
            evaluated_at_ns=now_ns,
            observed_instrument_id=None if observed is None else observed.instrument_id,
            bid=None if observed is None else observed.bid,
            ask=None if observed is None else observed.ask,
            ts_event_ns=None if observed is None else observed.ts_event_ns,
            ts_init_ns=None if observed is None else observed.ts_init_ns,
            stream_generation=None if observed is None else observed.stream_generation,
            **evidence,
        )

    if quote is None:
        return rejected("quote_missing")
    if not isinstance(quote, ExecutionQuote):
        return rejected("quote_type_invalid")
    if quote.instrument_id != intent.instrument_id:
        return rejected("quote_instrument_mismatch", observed=quote)
    if quote.bid <= 0 or quote.ask <= 0 or quote.bid > quote.ask:
        return rejected("quote_book_invalid", observed=quote)
    if side is None:
        return rejected("quote_side_unsupported", observed=quote)
    side_price = quote.ask if side == "buy" else quote.bid

    created_at_ns = intent.created_at_ms * 1_000_000
    valid_until_ns = intent.valid_until_ms * 1_000_000
    if now_ns < created_at_ns:
        return rejected("quote_intent_not_active", observed=quote)
    if now_ns >= valid_until_ns:
        return rejected("quote_intent_expired", observed=quote)
    if quote.ts_event_ns < 0 or quote.ts_init_ns < 0 or now_ns < 0 or quote.ts_init_ns < quote.ts_event_ns:
        return rejected("quote_clock_invalid", observed=quote)
    if quote.ts_event_ns > now_ns + MAX_FUTURE_SKEW_NS or quote.ts_init_ns > now_ns + MAX_FUTURE_SKEW_NS:
        return rejected("quote_future_skew", observed=quote)

    receive_age_ns = max(0, now_ns - quote.ts_init_ns)
    event_age_ns = max(0, now_ns - quote.ts_event_ns)
    source_latency_ns = quote.ts_init_ns - quote.ts_event_ns
    clocks = {
        "receive_age_ns": receive_age_ns,
        "event_age_ns": event_age_ns,
        "source_latency_ns": source_latency_ns,
    }
    if receive_age_ns > MAX_RECEIVE_AGE_NS:
        return rejected("quote_receive_stale", observed=quote, **clocks)
    if event_age_ns > MAX_EVENT_AGE_NS:
        return rejected("quote_event_stale", observed=quote, **clocks)
    if source_latency_ns > MAX_SOURCE_LATENCY_NS:
        return rejected("quote_source_latency_exceeded", observed=quote, **clocks)
    if last_accepted_ts_event_ns is not None and quote.ts_event_ns < last_accepted_ts_event_ns:
        return rejected("quote_event_out_of_order", observed=quote, **clocks)

    spread_bps = entry_spread_bps(bid=quote.bid, ask=quote.ask)
    reference_drift_bps = abs(side_price - intent.reference_price) * _BPS / intent.reference_price
    if spread_bps > Decimal(intent.max_spread_bps):
        return rejected(
            "quote_spread_exceeded",
            observed=quote,
            **clocks,
            spread_bps=spread_bps,
            reference_drift_bps=reference_drift_bps,
        )
    if reference_drift_bps > Decimal(intent.max_entry_drift_bps):
        return rejected(
            "quote_reference_drift_exceeded",
            observed=quote,
            **clocks,
            spread_bps=spread_bps,
            reference_drift_bps=reference_drift_bps,
        )
    return ExecutionQuoteSnapshotV1(
        stage=stage,
        intent_id=intent.intent_id,
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


def _execution_side(side: str) -> ExecutionSide | None:
    if side in {"long", "buy"}:
        return "buy"
    if side in {"short", "sell"}:
        return "sell"
    return None


__all__ = [
    "MAX_EVENT_AGE_NS",
    "MAX_FUTURE_SKEW_NS",
    "MAX_RECEIVE_AGE_NS",
    "MAX_SOURCE_LATENCY_NS",
    "QUOTE_CONTRACT_SHA256",
    "QUOTE_CONTRACT_VERSION",
    "SUBMISSION_FENCE_SHA256",
    "SUBMISSION_FENCE_VERSION",
    "EntryQuoteIntent",
    "ExecutionQuote",
    "ExecutionQuoteAuditV1",
    "ExecutionQuoteRejectionV1",
    "ExecutionQuoteSnapshotV1",
    "ExecutionSide",
    "QuoteRejectionReason",
    "QuoteStage",
    "SubmissionFenceV1",
    "validate_entry_quote",
]
