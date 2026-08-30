"""Trading-owned state machine for one venue-neutral manual execution authority."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .manual import ManualTradeIntent, TradeSide
from .manual_execution import ManualExecutionPlan, ManualInstrumentRules, build_manual_execution_plan
from .manual_portfolio import ManualCloseRequest

ManualOrderLeg = Literal["entry", "take_profit", "stop_loss"]
ManualAttemptLeg = Literal["execution_setting", "entry", "take_profit", "stop_loss"]
ManualProtectionLeg = Literal["take_profit", "stop_loss"]
ManualExecutionState = Literal["PENDING", "SUBMITTING", "AMBIGUOUS", "OPEN", "EXPOSED", "TERMINAL"]
ManualReconciliationResult = Literal["confirmed", "ambiguous", "rejected", "exposed", "deferred"]

_PROTECTION_LEGS: tuple[ManualProtectionLeg, ...] = ("take_profit", "stop_loss")


class ManualVenueError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        ambiguous: bool = False,
        retryable: bool = False,
        provider_code: int | None = None,
    ) -> None:
        if ambiguous and retryable:
            raise ValueError("manual_venue_error_disposition_invalid")
        self.code = code
        self.ambiguous = ambiguous
        self.retryable = retryable
        self.provider_code = provider_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ManualVenueAccount:
    equity_usd: Decimal
    can_trade: bool
    provider_account_fingerprint: str


@dataclass(frozen=True, slots=True)
class ManualVenuePosition:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    leverage: int
    mark_price: Decimal | None = None
    unrealized_pnl_usd: Decimal | None = None
    liquidation_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ManualVenueInstrument:
    symbol: str
    tick_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal


@dataclass(frozen=True, slots=True)
class ManualVenueOrderReceipt:
    client_id: str
    provider_id: str
    status: str
    executed_quantity: Decimal | None = None
    average_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ManualProtectionExecutionRecord:
    client_order_id: str | None
    attempted: bool
    confirmed: bool


@dataclass(frozen=True, slots=True)
class ManualExecutionRecord:
    """Typed Trading view of the one durable intent eligible for reconciliation."""

    intent: ManualTradeIntent
    state: ManualExecutionState
    plan: ManualExecutionPlan | None
    execution_setting_attempted: bool
    execution_setting_applied: bool
    entry_attempted: bool
    entry_confirmed: bool
    take_profit: ManualProtectionExecutionRecord
    stop_loss: ManualProtectionExecutionRecord


@dataclass(frozen=True, slots=True)
class ManualManagedPositionRecord:
    intent: ManualTradeIntent
    plan: ManualExecutionPlan
    opened_at_ms: int
    close_request: ManualCloseRequest | None
    take_profit_cancel_attempted: bool
    take_profit_cancelled: bool
    stop_loss_cancel_attempted: bool
    stop_loss_cancelled: bool
    entry_price: Decimal | None = None
    close_receipts: tuple[ManualVenueOrderReceipt, ...] = ()


class ManualTradeOutcomeState(StrEnum):
    OPEN = "open"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"
    EXPOSED = "exposed"


class _FrozenExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManualTradeOutcome(_FrozenExecutionModel):
    outcome_version: Literal["manual_trade_outcome_v1"] = "manual_trade_outcome_v1"
    state: ManualTradeOutcomeState
    leg: ManualAttemptLeg | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=160)
    entry: ManualVenueOrderReceipt | None = None
    take_profit: ManualVenueOrderReceipt | None = None
    stop_loss: ManualVenueOrderReceipt | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ManualTradeOutcome:
        if self.state is ManualTradeOutcomeState.OPEN:
            if (
                self.leg is not None
                or self.error_code is not None
                or any(receipt is None for receipt in (self.entry, self.take_profit, self.stop_loss))
            ):
                raise ValueError("manual_trade_open_outcome_invalid")
        elif (
            self.leg is None
            or self.error_code is None
            or any(receipt is not None for receipt in (self.entry, self.take_profit, self.stop_loss))
        ):
            raise ValueError("manual_trade_failure_outcome_invalid")
        return self


class ManualExecutionStore(Protocol):
    def refresh_account(self, *, equity_usd: Decimal, observed_at_ms: int) -> None: ...

    def next_intent(self) -> ManualExecutionRecord | None: ...

    def next_open_position(self) -> ManualManagedPositionRecord | None: ...

    def has_active_symbol(self, *, base_symbol: str, exclude_intent_id: str) -> bool: ...

    def fence_entry(self, intent_id: str, *, plan: ManualExecutionPlan, now_ms: int) -> bool: ...

    def begin_attempt(self, intent_id: str, *, leg: ManualAttemptLeg, now_ms: int) -> bool: ...

    def record_execution_setting(self, intent_id: str, *, now_ms: int) -> bool: ...

    def record_entry(self, intent_id: str, *, receipt: dict[str, object], now_ms: int) -> bool: ...

    def fence_protection(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        client_id: str,
        now_ms: int,
    ) -> bool: ...

    def record_protection(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        receipt: dict[str, object],
        now_ms: int,
    ) -> bool: ...

    def mark_ambiguous(
        self,
        intent_id: str,
        *,
        leg: ManualAttemptLeg,
        error_code: str,
        now_ms: int,
    ) -> bool: ...

    def reject(
        self,
        intent_id: str,
        *,
        leg: ManualAttemptLeg,
        error_code: str,
        now_ms: int,
    ) -> bool: ...

    def mark_exposed(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        error_code: str,
        now_ms: int,
    ) -> bool: ...

    def observe_position(
        self,
        intent_id: str,
        *,
        position: ManualVenuePosition,
        plan: ManualExecutionPlan,
        opened_at_ms: int,
        now_ms: int,
    ) -> bool: ...

    def begin_close_attempt(
        self,
        close_id: str,
        *,
        quantity: Decimal,
        now_ms: int,
    ) -> bool: ...

    def record_close_fill(self, close_id: str, *, receipt: dict[str, object], now_ms: int) -> bool: ...

    def reconcile_close_fill(self, close_id: str, *, receipt: dict[str, object], now_ms: int) -> bool: ...

    def record_partial_close_reconciled(
        self,
        close_id: str,
        *,
        remaining_quantity: Decimal,
        mark_price: Decimal,
        now_ms: int,
    ) -> bool: ...

    def reject_close(self, close_id: str, *, error_code: str, now_ms: int) -> bool: ...

    def mark_close_ambiguous(self, close_id: str, *, error_code: str, now_ms: int) -> bool: ...

    def begin_protection_cancel(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        now_ms: int,
    ) -> bool: ...

    def record_protection_cancelled(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        now_ms: int,
    ) -> bool: ...

    def close_position(
        self,
        intent_id: str,
        *,
        exit_reason: str,
        exit_price: Decimal,
        realized_pnl_usd: Decimal,
        now_ms: int,
    ) -> bool: ...

    def mark_position_review(self, intent_id: str, *, error_code: str, now_ms: int) -> bool: ...


class ManualExecutionVenue(Protocol):
    """Venue-neutral effects; each adapter owns provider-specific endpoints and payloads."""

    def account(self) -> ManualVenueAccount: ...

    def execution_ready(self) -> bool: ...

    def instrument(self, symbol: str) -> ManualInstrumentRules: ...

    def position(self, symbol: str) -> ManualVenuePosition: ...

    def apply_execution_setting(self, plan: ManualExecutionPlan) -> None: ...

    def query_leg(self, plan: ManualExecutionPlan, leg: ManualOrderLeg) -> ManualVenueOrderReceipt | None: ...

    def submit_leg(self, plan: ManualExecutionPlan, leg: ManualOrderLeg) -> ManualVenueOrderReceipt: ...

    def query_close(self, *, symbol: str, client_order_id: str) -> ManualVenueOrderReceipt | None: ...

    def submit_close(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: Decimal,
        client_order_id: str,
    ) -> ManualVenueOrderReceipt: ...

    def cancel_leg(self, plan: ManualExecutionPlan, leg: ManualProtectionLeg) -> bool: ...


class ManualExecutionService:
    """Advance one durable intent without holding a transaction over venue I/O."""

    def __init__(
        self,
        *,
        store: ManualExecutionStore,
        venue: ManualExecutionVenue,
        clock_ms: Any | None = None,
    ) -> None:
        self._store = store
        self._venue = venue
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._execution_ready_verified = False

    def turn(self) -> str:
        now_ms = int(self._clock_ms())
        account = self._venue.account()
        if not account.can_trade:
            raise RuntimeError("manual_executor_account_trade_disabled")
        if account.equity_usd <= 0:
            raise RuntimeError("manual_executor_account_equity_unavailable")
        if not self._execution_ready_verified:
            if not self._venue.execution_ready():
                raise RuntimeError("manual_executor_execution_mode_unsupported")
            self._execution_ready_verified = True
        self._store.refresh_account(equity_usd=account.equity_usd, observed_at_ms=now_ms)
        record = self._store.next_intent()
        if record is None:
            managed = self._store.next_open_position()
            if managed is None:
                return "idle"
            return self._reconcile_open_position(managed, now_ms=now_ms)
        intent = record.intent
        plan = record.plan
        if record.state == "PENDING":
            plan, result = self._prepare_and_fence(intent, now_ms=now_ms)
            if result != "confirmed":
                return f"entry_{result}"
        if plan is None:
            raise RuntimeError("manual_executor_plan_missing")
        if not record.execution_setting_applied:
            result = self._reconcile_execution_setting(
                intent,
                plan,
                already_attempted=record.execution_setting_attempted,
                now_ms=now_ms,
            )
            if result != "confirmed":
                return f"execution_setting_{result}"
        if not record.entry_confirmed:
            result = self._reconcile_entry(
                intent,
                plan,
                already_attempted=record.entry_attempted,
                now_ms=now_ms,
            )
            if result != "confirmed":
                return f"entry_{result}"
        protections: tuple[tuple[Literal["take_profit", "stop_loss"], ManualProtectionExecutionRecord], ...] = (
            ("stop_loss", record.stop_loss),
            ("take_profit", record.take_profit),
        )
        for leg, protection in protections:
            if protection.confirmed:
                continue
            client_id = protection.client_order_id or getattr(plan, f"{leg}_client_order_id")
            if protection.client_order_id is None and not self._store.fence_protection(
                intent.intent_id,
                leg=leg,
                client_id=client_id,
                now_ms=now_ms,
            ):
                raise RuntimeError("manual_executor_protection_fence_conflict")
            result = self._reconcile_protection(
                intent,
                plan,
                leg=leg,
                already_attempted=protection.attempted,
                now_ms=now_ms,
            )
            if result != "confirmed":
                return f"{leg}_{result}"
        return "position_open"

    def _reconcile_open_position(self, record: ManualManagedPositionRecord, *, now_ms: int) -> str:
        intent = record.intent
        plan = record.plan
        try:
            position = self._venue.position(plan.symbol)
        except ManualVenueError as exc:
            if exc.retryable:
                return f"position_deferred:{exc.code}"
            self._store.mark_position_review(intent.intent_id, error_code=exc.code, now_ms=now_ms)
            return "position_manual_review"
        expected_positive = intent.source.side is TradeSide.LONG
        if position.quantity != 0 and (position.quantity > 0) != expected_positive:
            self._store.mark_position_review(
                intent.intent_id,
                error_code="manual_position_side_mismatch",
                now_ms=now_ms,
            )
            return "position_manual_review"
        if position.quantity == 0:
            if not self._store.observe_position(
                intent.intent_id,
                position=position,
                plan=plan,
                opened_at_ms=record.opened_at_ms,
                now_ms=now_ms,
            ):
                raise RuntimeError("manual_executor_position_observation_conflict")
            if record.close_request is not None and record.close_request.state.value == "SUBMITTING":
                return self._reconcile_flat_submitted_close(record, now_ms=now_ms)
            if (
                record.close_request is not None
                and record.close_request.state.value == "FILLED"
                and not _receipt_payload_complete(record.close_request.receipt)
            ):
                return self._repair_flat_close_receipt(record, now_ms=now_ms)
            return self._finish_flat_position(record, position=position, now_ms=now_ms)
        if record.close_request is not None and record.close_request.state.value == "FILLED":
            return self._reconcile_partial_close(record, position=position, now_ms=now_ms)
        if not self._store.observe_position(
            intent.intent_id,
            position=position,
            plan=plan,
            opened_at_ms=record.opened_at_ms,
            now_ms=now_ms,
        ):
            raise RuntimeError("manual_executor_position_observation_conflict")
        if record.close_request is None:
            return "position_monitored"
        if record.close_request.state.value == "AMBIGUOUS":
            return "position_manual_review"
        return self._reconcile_close(record, position=position, now_ms=now_ms)

    def _reconcile_partial_close(
        self,
        record: ManualManagedPositionRecord,
        *,
        position: ManualVenuePosition,
        now_ms: int,
    ) -> str:
        request = record.close_request
        if request is None:
            raise RuntimeError("manual_close_request_missing")
        for leg in ("take_profit", "stop_loss"):
            try:
                protection = self._venue.query_leg(record.plan, leg)
            except ManualVenueError as exc:
                if exc.retryable:
                    return f"close_reconcile_deferred:{exc.code}"
                self._store.mark_position_review(record.intent.intent_id, error_code=exc.code, now_ms=now_ms)
                return "position_manual_review"
            if protection is None or protection.status not in {"NEW", "ACCEPTED"}:
                self._store.mark_position_review(
                    record.intent.intent_id,
                    error_code=f"manual_{leg}_not_active_after_partial_close",
                    now_ms=now_ms,
                )
                return "position_manual_review"
        if not self._store.observe_position(
            record.intent.intent_id,
            position=position,
            plan=record.plan,
            opened_at_ms=record.opened_at_ms,
            now_ms=now_ms,
        ):
            raise RuntimeError("manual_executor_position_observation_conflict")
        mark_price = position.mark_price or position.entry_price
        if mark_price <= 0 or not self._store.record_partial_close_reconciled(
            request.close_id,
            remaining_quantity=abs(position.quantity),
            mark_price=mark_price,
            now_ms=now_ms,
        ):
            raise RuntimeError("manual_close_reconciliation_settlement_conflict")
        return "close_reconciled"

    def _reconcile_close(
        self,
        record: ManualManagedPositionRecord,
        *,
        position: ManualVenuePosition,
        now_ms: int,
    ) -> str:
        request = record.close_request
        if request is None:
            raise RuntimeError("manual_close_request_missing")
        try:
            rules = self._venue.instrument(record.plan.symbol)
            quantity = _close_quantity(
                abs(position.quantity),
                requested_bps=request.requested_bps,
                rules=rules,
                mark_price=position.mark_price or position.entry_price,
            )
        except (ManualVenueError, ValueError) as exc:
            code = exc.code if isinstance(exc, ManualVenueError) else str(exc)
            if isinstance(exc, ManualVenueError) and exc.retryable:
                return f"close_deferred:{code}"
            self._store.reject_close(request.close_id, error_code=code, now_ms=now_ms)
            return "close_rejected"
        try:
            receipt = self._venue.query_close(
                symbol=record.plan.symbol,
                client_order_id=request.client_order_id,
            )
            if receipt is None:
                if request.attempted_at_ms is not None:
                    self._store.mark_close_ambiguous(
                        request.close_id,
                        error_code="manual_close_attempt_unconfirmed",
                        now_ms=now_ms,
                    )
                    return "close_ambiguous"
                if not self._store.begin_close_attempt(request.close_id, quantity=quantity, now_ms=now_ms):
                    return "close_ambiguous"
                receipt = self._venue.submit_close(
                    symbol=record.plan.symbol,
                    side=record.plan.close_side,
                    quantity=quantity,
                    client_order_id=request.client_order_id,
                )
        except ManualVenueError as exc:
            if exc.retryable:
                return f"close_deferred:{exc.code}"
            if exc.ambiguous:
                self._store.mark_close_ambiguous(request.close_id, error_code=exc.code, now_ms=now_ms)
                return "close_ambiguous"
            self._store.reject_close(request.close_id, error_code=exc.code, now_ms=now_ms)
            return "close_rejected"
        if not _confirmed_close_receipt(receipt, client_order_id=request.client_order_id):
            if (
                receipt.client_id == request.client_order_id
                and receipt.status == "FILLED"
                and receipt.executed_quantity is not None
                and receipt.average_price is None
            ):
                return "close_fill_price_deferred"
            self._store.mark_close_ambiguous(
                request.close_id,
                error_code="manual_close_fill_unconfirmed",
                now_ms=now_ms,
            )
            return "close_ambiguous"
        if not self._store.record_close_fill(
            request.close_id,
            receipt=_receipt_payload(receipt),
            now_ms=now_ms,
        ):
            raise RuntimeError("manual_close_settlement_conflict")
        return "close_filled"

    def _reconcile_flat_submitted_close(
        self,
        record: ManualManagedPositionRecord,
        *,
        now_ms: int,
    ) -> str:
        request = record.close_request
        if request is None:
            raise RuntimeError("manual_close_request_missing")
        try:
            receipt = self._venue.query_close(
                symbol=record.plan.symbol,
                client_order_id=request.client_order_id,
            )
        except ManualVenueError as exc:
            return f"close_fill_price_deferred:{exc.code}"
        if receipt is None or not _confirmed_close_receipt(
            receipt,
            client_order_id=request.client_order_id,
        ):
            return "close_fill_price_deferred"
        if not self._store.record_close_fill(
            request.close_id,
            receipt=_receipt_payload(receipt),
            now_ms=now_ms,
        ):
            raise RuntimeError("manual_close_settlement_conflict")
        return "close_filled"

    def _repair_flat_close_receipt(
        self,
        record: ManualManagedPositionRecord,
        *,
        now_ms: int,
    ) -> str:
        request = record.close_request
        if request is None:
            raise RuntimeError("manual_close_request_missing")
        try:
            receipt = self._venue.query_close(
                symbol=record.plan.symbol,
                client_order_id=request.client_order_id,
            )
        except ManualVenueError as exc:
            return f"close_fill_price_deferred:{exc.code}"
        if receipt is None or not _confirmed_close_receipt(
            receipt,
            client_order_id=request.client_order_id,
        ):
            return "close_fill_price_deferred"
        if not self._store.reconcile_close_fill(
            request.close_id,
            receipt=_receipt_payload(receipt),
            now_ms=now_ms,
        ):
            raise RuntimeError("manual_close_receipt_reconciliation_conflict")
        return "close_receipt_reconciled"

    def _finish_flat_position(
        self,
        record: ManualManagedPositionRecord,
        *,
        position: ManualVenuePosition,
        now_ms: int,
    ) -> str:
        protection_cancellations: tuple[tuple[ManualProtectionLeg, bool, bool], ...] = (
            ("take_profit", record.take_profit_cancel_attempted, record.take_profit_cancelled),
            ("stop_loss", record.stop_loss_cancel_attempted, record.stop_loss_cancelled),
        )
        for leg, attempted, cancelled in protection_cancellations:
            if cancelled:
                continue
            if attempted:
                self._store.mark_position_review(
                    record.intent.intent_id,
                    error_code=f"manual_{leg}_cancel_unconfirmed",
                    now_ms=now_ms,
                )
                return "position_manual_review"
            if not self._store.begin_protection_cancel(record.intent.intent_id, leg=leg, now_ms=now_ms):
                return "position_manual_review"
            try:
                cancelled_now = self._venue.cancel_leg(record.plan, leg)
            except ManualVenueError as exc:
                self._store.mark_position_review(record.intent.intent_id, error_code=exc.code, now_ms=now_ms)
                return "position_manual_review"
            if not cancelled_now:
                self._store.mark_position_review(
                    record.intent.intent_id,
                    error_code=f"manual_{leg}_cancel_unconfirmed",
                    now_ms=now_ms,
                )
                return "position_manual_review"
            if not self._store.record_protection_cancelled(record.intent.intent_id, leg=leg, now_ms=now_ms):
                raise RuntimeError("manual_protection_cancel_settlement_conflict")
            return f"{leg}_cancelled"

        protection_fill: tuple[str, ManualVenueOrderReceipt] | None = None
        for leg in _PROTECTION_LEGS:
            try:
                receipt = self._venue.query_leg(record.plan, leg)
            except ManualVenueError as exc:
                if exc.retryable:
                    return f"position_close_reconcile_deferred:{exc.code}"
                self._store.mark_position_review(record.intent.intent_id, error_code=exc.code, now_ms=now_ms)
                return "position_manual_review"
            if (
                receipt is not None
                and receipt.status.upper() in {"FILLED", "FINISHED"}
                and receipt.executed_quantity is not None
                and receipt.average_price is not None
            ):
                if protection_fill is not None:
                    self._store.mark_position_review(
                        record.intent.intent_id,
                        error_code="manual_multiple_protection_fills_detected",
                        now_ms=now_ms,
                    )
                    return "position_manual_review"
                protection_fill = (leg, receipt)

        fills = list(record.close_receipts)
        if protection_fill is not None:
            fills.append(protection_fill[1])
        if any(receipt.executed_quantity is None or receipt.average_price is None for receipt in fills):
            self._store.mark_position_review(
                record.intent.intent_id,
                error_code="manual_close_fill_incomplete",
                now_ms=now_ms,
            )
            return "position_manual_review"
        entry_price = record.entry_price or record.intent.reference_entry
        direction = Decimal("1") if record.intent.source.side is TradeSide.LONG else Decimal("-1")
        known_quantity = sum(
            (receipt.executed_quantity or Decimal("0") for receipt in fills),
            start=Decimal("0"),
        )
        if known_quantity > record.plan.quantity:
            self._store.mark_position_review(
                record.intent.intent_id,
                error_code="manual_close_fill_quantity_exceeds_entry",
                now_ms=now_ms,
            )
            return "position_manual_review"
        fallback_price = position.mark_price or entry_price
        missing_quantity = record.plan.quantity - known_quantity
        gross_exit_value = (
            sum(
                (
                    (receipt.average_price or Decimal("0")) * (receipt.executed_quantity or Decimal("0"))
                    for receipt in fills
                ),
                start=Decimal("0"),
            )
            + fallback_price * missing_quantity
        )
        exit_price = gross_exit_value / record.plan.quantity
        realized = (exit_price - entry_price) * record.plan.quantity * direction
        if protection_fill is not None:
            exit_reason = protection_fill[0]
        elif record.close_receipts and missing_quantity == 0:
            exit_reason = "manual_close"
        else:
            exit_reason = "protection_or_external_close"
        if not self._store.close_position(
            record.intent.intent_id,
            exit_reason=exit_reason,
            exit_price=exit_price,
            realized_pnl_usd=realized,
            now_ms=now_ms,
        ):
            raise RuntimeError("manual_position_close_settlement_conflict")
        return "position_closed"

    def _prepare_and_fence(
        self,
        intent: ManualTradeIntent,
        *,
        now_ms: int,
    ) -> tuple[ManualExecutionPlan | None, ManualReconciliationResult]:
        symbol = f"{intent.source.base_symbol}USDT"
        if self._store.has_active_symbol(
            base_symbol=intent.source.base_symbol,
            exclude_intent_id=intent.intent_id,
        ):
            return None, self._reject(
                intent,
                leg="entry",
                error_code="manual_executor_existing_symbol_exposure",
                now_ms=now_ms,
            )
        try:
            position = self._venue.position(symbol)
            rules = self._venue.instrument(symbol)
        except ManualVenueError as exc:
            return None, self._settle_venue_error(intent, leg="entry", error=exc, now_ms=now_ms)
        if position.quantity != 0:
            return None, self._reject(
                intent,
                leg="entry",
                error_code="manual_executor_existing_symbol_exposure",
                now_ms=now_ms,
            )
        try:
            plan = build_manual_execution_plan(intent, rules)
        except ValueError as exc:
            return None, self._reject(intent, leg="entry", error_code=str(exc), now_ms=now_ms)
        if not self._store.fence_entry(intent.intent_id, plan=plan, now_ms=now_ms):
            if self._store.has_active_symbol(
                base_symbol=intent.source.base_symbol,
                exclude_intent_id=intent.intent_id,
            ):
                return None, self._reject(
                    intent,
                    leg="entry",
                    error_code="manual_executor_existing_symbol_exposure",
                    now_ms=now_ms,
                )
            raise RuntimeError("manual_executor_entry_fence_conflict")
        return plan, "confirmed"

    def _reconcile_execution_setting(
        self,
        intent: ManualTradeIntent,
        plan: ManualExecutionPlan,
        *,
        already_attempted: bool,
        now_ms: int,
    ) -> ManualReconciliationResult:
        try:
            if self._venue.position(plan.symbol).leverage != plan.leverage:
                if already_attempted:
                    self._store.mark_ambiguous(
                        intent.intent_id,
                        leg="execution_setting",
                        error_code="manual_execution_setting_unconfirmed",
                        now_ms=now_ms,
                    )
                    return "ambiguous"
                if not self._store.begin_attempt(
                    intent.intent_id,
                    leg="execution_setting",
                    now_ms=now_ms,
                ):
                    return "ambiguous"
                self._venue.apply_execution_setting(plan)
                if self._venue.position(plan.symbol).leverage != plan.leverage:
                    raise ManualVenueError("manual_execution_setting_unconfirmed", ambiguous=True)
        except ManualVenueError as exc:
            return self._settle_venue_error(intent, leg="execution_setting", error=exc, now_ms=now_ms)
        if not self._store.record_execution_setting(intent.intent_id, now_ms=now_ms):
            raise RuntimeError("manual_executor_execution_setting_settlement_conflict")
        return "confirmed"

    def _reconcile_entry(
        self,
        intent: ManualTradeIntent,
        plan: ManualExecutionPlan,
        *,
        already_attempted: bool,
        now_ms: int,
    ) -> ManualReconciliationResult:
        try:
            receipt = self._venue.query_leg(plan, "entry")
            if receipt is None:
                if already_attempted:
                    self._store.mark_ambiguous(
                        intent.intent_id,
                        leg="entry",
                        error_code="manual_entry_attempt_unconfirmed",
                        now_ms=now_ms,
                    )
                    return "ambiguous"
                if self._venue.position(plan.symbol).quantity != 0:
                    return self._reject(
                        intent,
                        leg="entry",
                        error_code="manual_existing_exposure_before_entry",
                        now_ms=now_ms,
                    )
                if not self._store.begin_attempt(intent.intent_id, leg="entry", now_ms=now_ms):
                    return "ambiguous"
                receipt = self._venue.submit_leg(plan, "entry")
        except ManualVenueError as exc:
            return self._settle_venue_error(intent, leg="entry", error=exc, now_ms=now_ms)
        if (
            receipt.client_id != plan.entry_client_order_id
            or receipt.status != "FILLED"
            or receipt.executed_quantity is None
            or receipt.average_price is None
        ):
            self._store.mark_ambiguous(
                intent.intent_id,
                leg="entry",
                error_code="manual_entry_fill_unconfirmed",
                now_ms=now_ms,
            )
            return "ambiguous"
        if not self._store.record_entry(intent.intent_id, receipt=_receipt_payload(receipt), now_ms=now_ms):
            raise RuntimeError("manual_executor_entry_settlement_conflict")
        return "confirmed"

    def _reconcile_protection(
        self,
        intent: ManualTradeIntent,
        plan: ManualExecutionPlan,
        *,
        leg: Literal["take_profit", "stop_loss"],
        already_attempted: bool,
        now_ms: int,
    ) -> ManualReconciliationResult:
        client_id = getattr(plan, f"{leg}_client_order_id")
        try:
            receipt = self._venue.query_leg(plan, leg)
            if receipt is None:
                if already_attempted:
                    self._store.mark_ambiguous(
                        intent.intent_id,
                        leg=leg,
                        error_code="manual_protection_attempt_unconfirmed",
                        now_ms=now_ms,
                    )
                    return "ambiguous"
                if not self._store.begin_attempt(intent.intent_id, leg=leg, now_ms=now_ms):
                    return "ambiguous"
                receipt = self._venue.submit_leg(plan, leg)
        except ManualVenueError as exc:
            if not exc.retryable and not exc.ambiguous:
                return self._expose(intent, leg=leg, error_code=exc.code, now_ms=now_ms)
            return self._settle_venue_error(intent, leg=leg, error=exc, now_ms=now_ms)
        if receipt.client_id != client_id or receipt.status not in {"NEW", "ACCEPTED"}:
            self._store.mark_ambiguous(
                intent.intent_id,
                leg=leg,
                error_code="manual_protection_acceptance_unconfirmed",
                now_ms=now_ms,
            )
            return "ambiguous"
        if not self._store.record_protection(
            intent.intent_id,
            leg=leg,
            receipt=_receipt_payload(receipt),
            now_ms=now_ms,
        ):
            raise RuntimeError("manual_executor_protection_settlement_conflict")
        return "confirmed"

    def _settle_venue_error(
        self,
        intent: ManualTradeIntent,
        *,
        leg: ManualAttemptLeg,
        error: ManualVenueError,
        now_ms: int,
    ) -> ManualReconciliationResult:
        if error.retryable:
            return "deferred"
        if error.ambiguous:
            self._store.mark_ambiguous(intent.intent_id, leg=leg, error_code=error.code, now_ms=now_ms)
            return "ambiguous"
        return self._reject(intent, leg=leg, error_code=error.code, now_ms=now_ms)

    def _reject(
        self,
        intent: ManualTradeIntent,
        *,
        leg: ManualAttemptLeg,
        error_code: str,
        now_ms: int,
    ) -> Literal["rejected"]:
        if not self._store.reject(intent.intent_id, leg=leg, error_code=error_code, now_ms=now_ms):
            raise RuntimeError("manual_executor_rejection_settlement_conflict")
        return "rejected"

    def _expose(
        self,
        intent: ManualTradeIntent,
        *,
        leg: Literal["take_profit", "stop_loss"],
        error_code: str,
        now_ms: int,
    ) -> Literal["exposed"]:
        if not self._store.mark_exposed(intent.intent_id, leg=leg, error_code=error_code, now_ms=now_ms):
            raise RuntimeError("manual_executor_exposure_settlement_conflict")
        return "exposed"


def _receipt_payload(receipt: ManualVenueOrderReceipt) -> dict[str, object]:
    return {
        "client_id": receipt.client_id,
        "provider_id": receipt.provider_id,
        "status": receipt.status,
        "executed_quantity": None if receipt.executed_quantity is None else str(receipt.executed_quantity),
        "average_price": None if receipt.average_price is None else str(receipt.average_price),
    }


def _confirmed_close_receipt(
    receipt: ManualVenueOrderReceipt,
    *,
    client_order_id: str,
) -> bool:
    return bool(
        receipt.client_id == client_order_id
        and receipt.status == "FILLED"
        and receipt.executed_quantity is not None
        and receipt.executed_quantity > 0
        and receipt.average_price is not None
        and receipt.average_price > 0
    )


def _receipt_payload_complete(receipt: dict[str, object] | None) -> bool:
    if receipt is None:
        return False
    for key in ("executed_quantity", "average_price"):
        try:
            value = Decimal(str(receipt.get(key)))
        except (ArithmeticError, ValueError):
            return False
        if not value.is_finite() or value <= 0:
            return False
    return True


def _close_quantity(
    position_quantity: Decimal,
    *,
    requested_bps: int,
    rules: ManualInstrumentRules,
    mark_price: Decimal,
) -> Decimal:
    if requested_bps not in {3000, 5000, 10000}:
        raise ValueError("manual_close_fraction_invalid")
    if position_quantity <= 0 or mark_price <= 0:
        raise ValueError("manual_close_position_invalid")
    raw = position_quantity if requested_bps == 10_000 else position_quantity * Decimal(requested_bps) / Decimal(10_000)
    quantity = (raw // rules.quantity_step) * rules.quantity_step
    if quantity < rules.min_quantity or quantity * mark_price < rules.min_notional:
        if requested_bps != 10_000:
            raise ValueError("manual_close_below_venue_minimum")
        quantity = position_quantity
    if quantity <= 0 or quantity > position_quantity:
        raise ValueError("manual_close_quantity_invalid")
    return quantity


__all__ = [
    "ManualAttemptLeg",
    "ManualExecutionRecord",
    "ManualExecutionService",
    "ManualExecutionState",
    "ManualExecutionStore",
    "ManualExecutionVenue",
    "ManualManagedPositionRecord",
    "ManualOrderLeg",
    "ManualProtectionExecutionRecord",
    "ManualReconciliationResult",
    "ManualTradeOutcome",
    "ManualTradeOutcomeState",
    "ManualVenueAccount",
    "ManualVenueError",
    "ManualVenueInstrument",
    "ManualVenueOrderReceipt",
    "ManualVenuePosition",
]
