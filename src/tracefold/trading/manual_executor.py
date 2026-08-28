"""Trading-owned state machine for one venue-neutral manual execution authority."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .manual import ManualTradeIntent
from .manual_execution import ManualExecutionPlan, ManualInstrumentRules, build_manual_execution_plan

ManualOrderLeg = Literal["entry", "take_profit", "stop_loss"]
ManualAttemptLeg = Literal["execution_setting", "entry", "take_profit", "stop_loss"]
ManualExecutionState = Literal["PENDING", "SUBMITTING", "AMBIGUOUS", "OPEN", "EXPOSED", "TERMINAL"]
ManualReconciliationResult = Literal["confirmed", "ambiguous", "rejected", "exposed", "deferred"]


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


class ManualExecutionVenue(Protocol):
    """Venue-neutral effects; each adapter owns provider-specific endpoints and payloads."""

    def account(self) -> ManualVenueAccount: ...

    def execution_ready(self) -> bool: ...

    def instrument(self, symbol: str) -> ManualInstrumentRules: ...

    def position(self, symbol: str) -> ManualVenuePosition: ...

    def apply_execution_setting(self, plan: ManualExecutionPlan) -> None: ...

    def query_leg(self, plan: ManualExecutionPlan, leg: ManualOrderLeg) -> ManualVenueOrderReceipt | None: ...

    def submit_leg(self, plan: ManualExecutionPlan, leg: ManualOrderLeg) -> ManualVenueOrderReceipt: ...


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
        if not self._execution_ready_verified:
            if not self._venue.execution_ready():
                raise RuntimeError("manual_executor_execution_mode_unsupported")
            self._execution_ready_verified = True
        self._store.refresh_account(equity_usd=account.equity_usd, observed_at_ms=now_ms)
        record = self._store.next_intent()
        if record is None:
            return "idle"
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

    def _prepare_and_fence(
        self,
        intent: ManualTradeIntent,
        *,
        now_ms: int,
    ) -> tuple[ManualExecutionPlan | None, ManualReconciliationResult]:
        symbol = f"{intent.source.base_symbol}USDT"
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


__all__ = [
    "ManualAttemptLeg",
    "ManualExecutionRecord",
    "ManualExecutionService",
    "ManualExecutionState",
    "ManualExecutionStore",
    "ManualExecutionVenue",
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
