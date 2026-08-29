"""Bounded in-process messages between the DB and TradingNode threads."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from queue import Queue
from typing import Literal

from tracefold.trading import (
    ExecutionQuoteAuditV1,
    ExecutionQuoteRejectionV1,
    ExecutionQuoteSnapshotV1,
    IntentOutcome,
    IntentReasonCode,
    RejectedReason,
    TradeIntent,
)

OrderLeg = Literal["entry", "stop", "close"]


@dataclass(frozen=True, slots=True)
class StartupAccountReconciliationConfirmed:
    """Confirm the startup process reconciled one complete provider account report."""

    verified_at_ms: int
    bootstrap_account_zero: bool


@dataclass(frozen=True, slots=True)
class StartupAccountReconciliationUnproven:
    """Keep startup closed after provider failure or observed account exposure."""

    observed_at_ms: int
    unexpected_exposure: bool


@dataclass(frozen=True, slots=True)
class AdoptIntent:
    """Ask the strategy to preflight or recover one durable Intent."""

    intent: TradeIntent
    outcome: IntentOutcome


@dataclass(frozen=True, slots=True)
class EntryFenceGranted:
    """Confirm the entry fence was committed before the provider write."""

    outcome: IntentOutcome


@dataclass(frozen=True, slots=True)
class EntrySubmissionGranted:
    """Confirm Q2 evidence was committed before the provider write."""

    outcome: IntentOutcome


@dataclass(frozen=True, slots=True)
class EntryNoSubmitFinalized:
    """Confirm the fenced Intent durably terminalized without a provider write."""

    outcome: IntentOutcome


@dataclass(frozen=True, slots=True)
class QuoteStreamChanged:
    """Invalidate cached execution quotes across a data-client disconnect/reconnect."""

    connected: bool
    generation: int


@dataclass(frozen=True, slots=True)
class IntentReleased:
    """Forget a pending Intent that PostgreSQL has terminalized or refused to fence."""

    intent_id: str


@dataclass(frozen=True, slots=True)
class VenueFlatConfirmed:
    """Deliver a fresh account-wide position/order reconciliation result."""

    intent_id: str
    instrument_id: str
    position_id: str
    authoritative_quantity: Decimal
    verified_at_ms: int
    account_wide_zero: bool = True


@dataclass(frozen=True, slots=True)
class VenueFlatUnproven:
    """Report that the account-wide venue query failed or did not prove zero."""

    intent_id: str
    position_id: str
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class VenueFlatProofRequested:
    """Ask the root to prove zero positions and account for every open order."""

    intent_id: str
    instrument_id: str
    account_id: str
    position_id: str
    closing_client_order_id: str
    observed_at_ms: int
    owned_open_order_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadinessChanged:
    ready: bool
    reason: str
    unexpected_exposure: bool


@dataclass(frozen=True, slots=True)
class BootstrapAccountZeroChanged:
    """Project the bootstrap-only zero proof without claiming engine readiness."""

    verified_at_ms: int | None
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class EntryFenceRequested:
    intent_id: str
    engine_identity: str
    quantity: Decimal
    q1_evidence: ExecutionQuoteSnapshotV1
    requested_at_ms: int


@dataclass(frozen=True, slots=True)
class EntryPreflightRejected:
    intent_id: str
    reason_code: IntentReasonCode
    q1_evidence: ExecutionQuoteAuditV1


@dataclass(frozen=True, slots=True)
class EntrySubmissionRequested:
    intent_id: str
    client_order_id: str
    q2_evidence: ExecutionQuoteSnapshotV1


@dataclass(frozen=True, slots=True)
class EntryNoSubmitRequested:
    intent_id: str
    client_order_id: str
    reason_code: RejectedReason
    q2_evidence: ExecutionQuoteRejectionV1


@dataclass(frozen=True, slots=True)
class EntrySubmitted:
    intent_id: str
    client_order_id: str
    submitted_at_ms: int


@dataclass(frozen=True, slots=True)
class EntryAccepted:
    intent_id: str
    client_order_id: str
    accepted_at_ms: int


@dataclass(frozen=True, slots=True)
class IntentRefused:
    intent_id: str
    reason_code: IntentReasonCode


@dataclass(frozen=True, slots=True)
class EntryFilled:
    intent_id: str
    actual_quantity: Decimal
    avg_entry_price: Decimal
    position_id: str
    opened_at_ms: int


@dataclass(frozen=True, slots=True)
class EntryRejected:
    intent_id: str
    client_order_id: str
    reason_code: Literal["risk_denied"]
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class StopSubmitted:
    intent_id: str
    client_order_id: str
    generation: int
    previous_client_order_id: str | None
    quantity: Decimal
    submitted_at_ms: int


@dataclass(frozen=True, slots=True)
class StopAccepted:
    intent_id: str
    client_order_id: str
    venue_order_id: str
    quantity: Decimal
    trigger_price: Decimal
    accepted_at_ms: int


@dataclass(frozen=True, slots=True)
class PositionQuantityChanged:
    intent_id: str
    position_id: str
    actual_quantity: Decimal
    avg_entry_price: Decimal
    changed_at_ms: int


@dataclass(frozen=True, slots=True)
class CloseSubmitted:
    intent_id: str
    client_order_id: str
    position_id: str
    quantity: Decimal
    submitted_at_ms: int


@dataclass(frozen=True, slots=True)
class PositionClosedObserved:
    """Local OMS callback fact; this is not a fresh venue-flat proof."""

    intent_id: str
    instrument_id: str
    account_id: str
    position_id: str
    closing_client_order_id: str
    local_quantity: Decimal
    avg_exit_price: Decimal
    realized_pnl_amount: Decimal | None
    realized_pnl_currency: str | None
    commissions_by_currency: dict[str, str] | None
    closed_at_ms: int


@dataclass(frozen=True, slots=True)
class PositionFlatConfirmed:
    """Final fact after venue zero and exact close/stop terminality are proven."""

    intent_id: str
    position_id: str
    authoritative_quantity: Decimal
    avg_exit_price: Decimal
    realized_pnl_amount: Decimal | None
    realized_pnl_currency: str | None
    commissions_by_currency: dict[str, str] | None
    closed_at_ms: int
    flat_verified_at_ms: int


@dataclass(frozen=True, slots=True)
class OrderOutcomeUnknown:
    intent_id: str | None
    leg: OrderLeg | None
    observed_at_ms: int


StrategyCommand = (
    AdoptIntent
    | StartupAccountReconciliationConfirmed
    | StartupAccountReconciliationUnproven
    | EntryFenceGranted
    | EntrySubmissionGranted
    | EntryNoSubmitFinalized
    | IntentReleased
    | QuoteStreamChanged
    | VenueFlatConfirmed
    | VenueFlatUnproven
)
StrategyEvent = (
    BootstrapAccountZeroChanged
    | ReadinessChanged
    | EntryFenceRequested
    | EntryPreflightRejected
    | EntrySubmissionRequested
    | EntryNoSubmitRequested
    | EntrySubmitted
    | EntryAccepted
    | IntentRefused
    | EntryFilled
    | EntryRejected
    | StopSubmitted
    | StopAccepted
    | PositionQuantityChanged
    | CloseSubmitted
    | PositionClosedObserved
    | PositionFlatConfirmed
    | OrderOutcomeUnknown
)


@dataclass(frozen=True, slots=True)
class StrategyQueues:
    commands: Queue[StrategyCommand]
    events: Queue[StrategyEvent]


def strategy_queues(*, maxsize: int = 64) -> StrategyQueues:
    """Create the two non-blocking thread boundaries with one explicit bound."""

    if maxsize <= 0:
        raise ValueError("nautilus_queue_maxsize_invalid")
    return StrategyQueues(commands=Queue(maxsize=maxsize), events=Queue(maxsize=maxsize))


__all__ = [
    "AdoptIntent",
    "BootstrapAccountZeroChanged",
    "CloseSubmitted",
    "EntryAccepted",
    "EntryFenceGranted",
    "EntryFenceRequested",
    "EntryFilled",
    "EntryNoSubmitFinalized",
    "EntryNoSubmitRequested",
    "EntryPreflightRejected",
    "EntryRejected",
    "EntrySubmissionGranted",
    "EntrySubmissionRequested",
    "EntrySubmitted",
    "IntentRefused",
    "IntentReleased",
    "OrderLeg",
    "OrderOutcomeUnknown",
    "PositionClosedObserved",
    "PositionFlatConfirmed",
    "PositionQuantityChanged",
    "QuoteStreamChanged",
    "ReadinessChanged",
    "StartupAccountReconciliationConfirmed",
    "StartupAccountReconciliationUnproven",
    "StopAccepted",
    "StopSubmitted",
    "StrategyCommand",
    "StrategyEvent",
    "StrategyQueues",
    "VenueFlatConfirmed",
    "VenueFlatProofRequested",
    "VenueFlatUnproven",
    "strategy_queues",
]
