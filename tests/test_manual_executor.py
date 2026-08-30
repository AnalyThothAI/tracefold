from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tracefold.trading import (
    ManualCloseRequest,
    ManualCloseState,
    ManualExecutionPlan,
    ManualExecutionRecord,
    ManualExecutionService,
    ManualManagedPositionRecord,
    ManualModificationGuard,
    ManualProtectionExecutionRecord,
    ManualTradeParameters,
    ManualTradeSource,
    ManualVenueAccount,
    ManualVenueError,
    ManualVenueInstrument,
    ManualVenueOrderReceipt,
    ManualVenuePosition,
    ModificationGuardState,
    StrategyPreset,
    TradeSide,
    create_manual_trade_intent,
)
from tracefold.trading.manual_execution import build_manual_execution_plan


def _intent():
    selected = ManualTradeParameters(
        notional_usd=Decimal("100"),
        leverage=5,
        stop_loss_bps=100,
        take_profit_bps=200,
    )
    return create_manual_trade_intent(
        session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea0",
        source=ManualTradeSource(
            news_event_id="event-42",
            delivery_target_sha256="a" * 64,
            delivery_message_id=42,
            headline_zh="BTC ETF 净流入创纪录",
            base_symbol="BTC",
            side=TradeSide.LONG,
            source_observed_at_ms=1_900_000_000_000,
        ),
        actor_user_id=123456789,
        account_ref="binance-manual-live-1",
        venue="binance_usdm_live",
        preset=StrategyPreset.TIGHT_STOP,
        recommended=selected,
        selected=selected,
        reference_entry=Decimal("100"),
        account_equity=Decimal("1000"),
        guard=ManualModificationGuard(
            state=ModificationGuardState.ACCEPTED,
            notional_deviation_bps=0,
            stop_loss_deviation_bps=0,
            take_profit_deviation_bps=0,
            original_max_loss_usd=Decimal("1"),
            modified_max_loss_usd=Decimal("1"),
            max_loss_change_bps=0,
            modified_account_risk_bps=10,
        ),
        confirmed_at_ms=1_900_000_000_100,
    )


class _Store:
    def __init__(self) -> None:
        self.intent = _intent()
        self.record: dict[str, object] = {
            "intent_id": self.intent.intent_id,
            "payload": self.intent,
            "state": "PENDING",
            "execution_plan": None,
            "execution_setting_attempted_at_ms": None,
            "execution_setting_applied_at_ms": None,
            "entry_attempted_at_ms": None,
            "entry_receipt": None,
            "take_profit_client_order_id": None,
            "take_profit_attempted_at_ms": None,
            "take_profit_receipt": None,
            "stop_loss_client_order_id": None,
            "stop_loss_attempted_at_ms": None,
            "stop_loss_receipt": None,
        }
        self.events: list[str] = []
        self.snapshot: tuple[Decimal, int] | None = None

    def refresh_account(self, *, equity_usd: Decimal, observed_at_ms: int) -> None:
        self.snapshot = (equity_usd, observed_at_ms)

    def next_intent(self):
        if self.record["state"] in {"OPEN", "EXPOSED", "TERMINAL"}:
            return None
        return ManualExecutionRecord(
            intent=self.intent,
            state=self.record["state"],
            plan=self.record["execution_plan"],
            execution_setting_attempted=self.record["execution_setting_attempted_at_ms"] is not None,
            execution_setting_applied=self.record["execution_setting_applied_at_ms"] is not None,
            entry_attempted=self.record["entry_attempted_at_ms"] is not None,
            entry_confirmed=self.record["entry_receipt"] is not None,
            take_profit=ManualProtectionExecutionRecord(
                client_order_id=self.record["take_profit_client_order_id"],
                attempted=self.record["take_profit_attempted_at_ms"] is not None,
                confirmed=self.record["take_profit_receipt"] is not None,
            ),
            stop_loss=ManualProtectionExecutionRecord(
                client_order_id=self.record["stop_loss_client_order_id"],
                attempted=self.record["stop_loss_attempted_at_ms"] is not None,
                confirmed=self.record["stop_loss_receipt"] is not None,
            ),
        )

    def next_open_position(self):
        return None

    def fence_entry(self, intent_id: str, *, plan: object, now_ms: int) -> bool:
        assert intent_id == self.intent.intent_id and now_ms > 0
        self.record.update(state="SUBMITTING", execution_plan=plan)
        self.events.append("entry_fenced")
        return True

    def record_entry(self, intent_id: str, *, receipt: dict[str, object], now_ms: int) -> bool:
        self.record["entry_receipt"] = receipt
        self.events.append("entry_recorded")
        return True

    def record_execution_setting(self, intent_id: str, *, now_ms: int) -> bool:
        self.record["execution_setting_applied_at_ms"] = now_ms
        self.events.append("execution_setting_recorded")
        return True

    def begin_attempt(self, intent_id: str, *, leg: str, now_ms: int) -> bool:
        key = f"{leg}_attempted_at_ms"
        if self.record[key] is not None:
            return False
        self.record[key] = now_ms
        self.events.append(f"{leg}_attempted")
        return True

    def fence_protection(self, intent_id: str, *, leg: str, client_id: str, now_ms: int) -> bool:
        self.record[f"{leg}_client_order_id"] = client_id
        self.events.append(f"{leg}_fenced")
        return True

    def record_protection(self, intent_id: str, *, leg: str, receipt: dict[str, object], now_ms: int) -> bool:
        self.record[f"{leg}_receipt"] = receipt
        self.events.append(f"{leg}_recorded")
        return True

    def mark_ambiguous(self, intent_id: str, *, leg: str, error_code: str, now_ms: int) -> bool:
        self.record["state"] = "AMBIGUOUS"
        self.events.append(f"{leg}_ambiguous:{error_code}")
        return True

    def reject(self, intent_id: str, *, leg: str, error_code: str, now_ms: int) -> bool:
        self.record["state"] = "TERMINAL"
        self.events.append(f"{leg}_rejected:{error_code}")
        return True

    def mark_exposed(self, intent_id: str, *, leg: str, error_code: str, now_ms: int) -> bool:
        self.record["state"] = "EXPOSED"
        self.events.append(f"{leg}_exposed:{error_code}")
        return True


class _Venue:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.leverage = 2

    def account(self) -> ManualVenueAccount:
        return ManualVenueAccount(
            equity_usd=Decimal("1000"),
            can_trade=True,
            provider_account_fingerprint="a" * 64,
        )

    def execution_ready(self) -> bool:
        self.calls.append("position_mode")
        return True

    def instrument(self, symbol: str) -> ManualVenueInstrument:
        assert symbol == "BTCUSDT"
        return ManualVenueInstrument(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

    def position(self, symbol: str) -> ManualVenuePosition:
        return ManualVenuePosition(
            symbol=symbol,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            leverage=self.leverage,
        )

    def apply_execution_setting(self, plan: ManualExecutionPlan) -> None:
        self.calls.append(f"leverage:{plan.symbol}:{plan.leverage}")
        self.leverage = plan.leverage

    def query_leg(self, plan: ManualExecutionPlan, leg: str):
        client_id = getattr(plan, f"{leg}_client_order_id")
        self.calls.append(f"query_{leg}:{client_id}")

    def submit_leg(self, plan: ManualExecutionPlan, leg: str):
        client_id = getattr(plan, f"{leg}_client_order_id")
        self.calls.append(f"submit_{leg}:{client_id}")
        if leg == "entry":
            return ManualVenueOrderReceipt(
                client_id=client_id,
                provider_id="1",
                status="FILLED",
                executed_quantity=plan.quantity,
                average_price=Decimal("100"),
            )
        return ManualVenueOrderReceipt(client_id=client_id, provider_id="2", status="NEW")


def test_service_fences_and_reconciles_each_economic_leg_before_marking_open() -> None:
    store, venue = _Store(), _Venue()
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    assert service.turn() == "position_open"

    assert store.snapshot == (Decimal("1000"), 1_900_000_000_200)
    assert store.events == [
        "entry_fenced",
        "execution_setting_attempted",
        "execution_setting_recorded",
        "entry_attempted",
        "entry_recorded",
        "stop_loss_fenced",
        "stop_loss_attempted",
        "stop_loss_recorded",
        "take_profit_fenced",
        "take_profit_attempted",
        "take_profit_recorded",
    ]
    assert venue.calls[0:2] == ["position_mode", "leverage:BTCUSDT:5"]
    assert sum(call.startswith("submit_entry:") for call in venue.calls) == 1
    protection_submits = [
        call for call in venue.calls if call.startswith("submit_take_profit:") or call.startswith("submit_stop_loss:")
    ]
    assert len(protection_submits) == 2


class _AmbiguousEntryVenue(_Venue):
    def submit_leg(self, plan: ManualExecutionPlan, leg: str):
        if leg == "entry":
            self.calls.append(f"submit_entry:{plan.entry_client_order_id}")
            raise ManualVenueError("binance_manual_write_ambiguous", ambiguous=True)
        return super().submit_leg(plan, leg)


def test_unknown_entry_write_is_never_resubmitted_after_the_durable_attempt_marker() -> None:
    store, venue = _Store(), _AmbiguousEntryVenue()
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    assert service.turn() == "entry_ambiguous"
    assert service.turn() == "entry_ambiguous"

    assert sum(call.startswith("submit_entry:") for call in venue.calls) == 1
    assert sum(call.startswith("query_entry:") for call in venue.calls) == 2
    assert store.events.count("entry_attempted") == 1
    assert any(event == "entry_ambiguous:manual_entry_attempt_unconfirmed" for event in store.events)


class _RejectedEntryVenue(_Venue):
    def submit_leg(self, plan: ManualExecutionPlan, leg: str):
        if leg == "entry":
            self.calls.append(f"submit_entry:{plan.entry_client_order_id}")
            raise ManualVenueError("binance_manual_provider_rejected", provider_code=-2010)
        return super().submit_leg(plan, leg)


def test_explicit_provider_rejection_is_terminal_and_is_never_resubmitted() -> None:
    store, venue = _Store(), _RejectedEntryVenue()
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    assert service.turn() == "entry_rejected"
    assert service.turn() == "idle"

    assert sum(call.startswith("submit_entry:") for call in venue.calls) == 1
    assert store.record["state"] == "TERMINAL"
    assert "entry_rejected:binance_manual_provider_rejected" in store.events


class _RejectedStopVenue(_Venue):
    def submit_leg(self, plan: ManualExecutionPlan, leg: str):
        if leg == "stop_loss":
            self.calls.append(f"submit_stop_loss:{plan.stop_loss_client_order_id}")
            raise ManualVenueError("binance_manual_provider_rejected", provider_code=-2021)
        return super().submit_leg(plan, leg)


def test_protection_rejection_records_unresolved_exposure_and_stops_automation() -> None:
    store, venue = _Store(), _RejectedStopVenue()
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    assert service.turn() == "stop_loss_exposed"
    assert service.turn() == "idle"

    assert store.record["entry_receipt"] is not None
    assert store.record["state"] == "EXPOSED"
    assert "stop_loss_exposed:binance_manual_provider_rejected" in store.events
    assert not any(call.startswith("submit_take_profit:") for call in venue.calls)


class _RetryableInstrumentVenue(_Venue):
    def instrument(self, symbol: str) -> ManualVenueInstrument:
        raise ManualVenueError("binance_manual_read_retryable", retryable=True)


def test_retryable_preflight_read_failure_defers_without_terminalizing_the_intent() -> None:
    store, venue = _Store(), _RetryableInstrumentVenue()
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    assert service.turn() == "entry_deferred"
    assert store.record["state"] == "PENDING"
    assert not any("rejected" in event or "ambiguous" in event for event in store.events)


def test_crash_after_protection_attempt_freezes_when_provider_cannot_find_the_order() -> None:
    store, venue = _Store(), _Venue()
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    assert service.turn() == "position_open"
    store.record.update(
        state="SUBMITTING",
        take_profit_receipt=None,
        take_profit_attempted_at_ms=1_900_000_000_199,
    )

    assert service.turn() == "take_profit_ambiguous"
    assert "take_profit_ambiguous:manual_protection_attempt_unconfirmed" in store.events


class _AmbiguousSettingVenue(_Venue):
    def apply_execution_setting(self, plan: ManualExecutionPlan) -> None:
        self.calls.append(f"leverage:{plan.symbol}:{plan.leverage}")
        raise ManualVenueError("binance_manual_write_ambiguous", ambiguous=True)


def test_unknown_execution_setting_write_is_never_retried_after_attempt_marker() -> None:
    store, venue = _Store(), _AmbiguousSettingVenue()
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    assert service.turn() == "execution_setting_ambiguous"
    assert service.turn() == "execution_setting_ambiguous"

    assert venue.calls.count("leverage:BTCUSDT:5") == 1
    assert store.events.count("execution_setting_attempted") == 1


def test_hedge_mode_never_publishes_an_account_snapshot_for_preview() -> None:
    store, venue = _Store(), _Venue()
    venue.execution_ready = lambda: False  # type: ignore[method-assign]
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    with pytest.raises(RuntimeError, match="manual_executor_execution_mode_unsupported"):
        service.turn()

    assert store.snapshot is None


def test_zero_equity_never_publishes_a_snapshot_or_reaches_an_execution_write() -> None:
    store, venue = _Store(), _Venue()
    venue.account = lambda: ManualVenueAccount(  # type: ignore[method-assign]
        equity_usd=Decimal("0"),
        can_trade=True,
        provider_account_fingerprint="a" * 64,
    )
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: 1_900_000_000_200)

    with pytest.raises(RuntimeError, match="manual_executor_account_equity_unavailable"):
        service.turn()

    assert store.snapshot is None
    assert store.events == []
    assert venue.calls == []


class _PartialCloseStore(_Store):
    def __init__(self, venue: _Venue) -> None:
        super().__init__()
        plan = build_manual_execution_plan(self.intent, venue.instrument("BTCUSDT"))
        close = ManualCloseRequest(
            close_id="c" * 64,
            intent_id=self.intent.intent_id,
            session_id=self.intent.session_id,
            requested_bps=5000,
            client_order_id="tfm-c-partial",
            state=ManualCloseState.PENDING,
            requested_at_ms=1_900_000_000_100,
            updated_at_ms=1_900_000_000_100,
        )
        self.managed = ManualManagedPositionRecord(
            intent=self.intent,
            plan=plan,
            opened_at_ms=1_900_000_000_100,
            close_request=close,
            take_profit_cancel_attempted=False,
            take_profit_cancelled=False,
            stop_loss_cancel_attempted=False,
            stop_loss_cancelled=False,
            entry_price=Decimal("100"),
        )

    def next_intent(self):
        return None

    def next_open_position(self):
        return self.managed

    def observe_position(self, intent_id: str, **values: object) -> bool:
        assert intent_id == self.intent.intent_id
        self.events.append(f"position_observed:{values['position'].quantity}")  # type: ignore[union-attr]
        return True

    def begin_close_attempt(self, close_id: str, *, quantity: Decimal, now_ms: int) -> bool:
        assert self.managed.close_request is not None and close_id == self.managed.close_request.close_id
        self.managed = replace(
            self.managed,
            close_request=self.managed.close_request.model_copy(
                update={
                    "state": ManualCloseState.SUBMITTING,
                    "target_quantity": quantity,
                    "attempted_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            ),
        )
        return True

    def record_close_fill(self, close_id: str, *, receipt: dict[str, object], now_ms: int) -> bool:
        assert self.managed.close_request is not None and close_id == self.managed.close_request.close_id
        parsed_receipt = ManualVenueOrderReceipt(
            client_id=str(receipt["client_id"]),
            provider_id=str(receipt["provider_id"]),
            status=str(receipt["status"]),
            executed_quantity=Decimal(str(receipt["executed_quantity"])),
            average_price=Decimal(str(receipt["average_price"])),
        )
        self.managed = replace(
            self.managed,
            close_request=self.managed.close_request.model_copy(
                update={"state": ManualCloseState.FILLED, "receipt": receipt, "updated_at_ms": now_ms}
            ),
            close_receipts=(*self.managed.close_receipts, parsed_receipt),
        )
        self.events.append("close_filled")
        return True

    def record_partial_close_reconciled(
        self,
        close_id: str,
        *,
        remaining_quantity: Decimal,
        mark_price: Decimal,
        now_ms: int,
    ) -> bool:
        assert self.managed.close_request is not None and close_id == self.managed.close_request.close_id
        self.managed = replace(
            self.managed,
            close_request=self.managed.close_request.model_copy(update={"reconciled_at_ms": now_ms}),
        )
        self.events.append(f"close_reconciled:{remaining_quantity}:{mark_price}")
        return True

    def reconcile_close_fill(self, close_id: str, *, receipt: dict[str, object], now_ms: int) -> bool:
        assert self.managed.close_request is not None and close_id == self.managed.close_request.close_id
        parsed_receipt = ManualVenueOrderReceipt(
            client_id=str(receipt["client_id"]),
            provider_id=str(receipt["provider_id"]),
            status=str(receipt["status"]),
            executed_quantity=Decimal(str(receipt["executed_quantity"])),
            average_price=Decimal(str(receipt["average_price"])),
        )
        self.managed = replace(
            self.managed,
            close_request=self.managed.close_request.model_copy(update={"receipt": receipt, "updated_at_ms": now_ms}),
            close_receipts=(parsed_receipt,),
        )
        self.events.append("close_receipt_reconciled")
        return True


class _PartialCloseVenue(_Venue):
    def __init__(self) -> None:
        super().__init__()
        self.quantity = Decimal("1")

    def position(self, symbol: str) -> ManualVenuePosition:
        return ManualVenuePosition(
            symbol=symbol,
            quantity=self.quantity,
            entry_price=Decimal("100"),
            leverage=5,
            mark_price=Decimal("101"),
            unrealized_pnl_usd=self.quantity,
            liquidation_price=Decimal("80"),
        )

    def query_close(self, *, symbol: str, client_order_id: str):
        self.calls.append(f"query_close:{symbol}:{client_order_id}")

    def submit_close(self, *, symbol: str, side: str, quantity: Decimal, client_order_id: str):
        assert (symbol, side, quantity, client_order_id) == ("BTCUSDT", "SELL", Decimal("0.5"), "tfm-c-partial")
        self.quantity = Decimal("0.5")
        self.calls.append("submit_close")
        return ManualVenueOrderReceipt(
            client_id=client_order_id,
            provider_id="88",
            status="FILLED",
            executed_quantity=quantity,
            average_price=Decimal("101"),
        )

    def query_leg(self, plan: ManualExecutionPlan, leg: str):
        client_id = getattr(plan, f"{leg}_client_order_id")
        self.calls.append(f"query_{leg}:{client_id}")
        return ManualVenueOrderReceipt(client_id=client_id, provider_id="99", status="NEW")


def test_partial_close_reconciles_exchange_quantity_and_rechecks_close_all_protection() -> None:
    venue = _PartialCloseVenue()
    store = _PartialCloseStore(venue)
    ticks = iter((1_900_000_000_200, 1_900_000_000_300))
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: next(ticks))

    assert service.turn() == "close_filled"
    assert service.turn() == "close_reconciled"

    assert "close_filled" in store.events
    assert "position_observed:0.5" in store.events
    assert "close_reconciled:0.5:101" in store.events
    assert any(call.startswith("query_take_profit:") for call in venue.calls)
    assert any(call.startswith("query_stop_loss:") for call in venue.calls)


class _FullCloseStore(_PartialCloseStore):
    def __init__(self, venue: _Venue) -> None:
        super().__init__(venue)
        assert self.managed.close_request is not None
        self.managed = replace(
            self.managed,
            close_request=self.managed.close_request.model_copy(update={"requested_bps": 10000}),
        )
        self.closed: tuple[str, Decimal, Decimal] | None = None

    def begin_protection_cancel(self, intent_id: str, *, leg: str, now_ms: int) -> bool:
        self.managed = replace(self.managed, **{f"{leg}_cancel_attempted": True})
        return True

    def record_protection_cancelled(self, intent_id: str, *, leg: str, now_ms: int) -> bool:
        self.managed = replace(self.managed, **{f"{leg}_cancelled": True})
        return True

    def close_position(
        self,
        intent_id: str,
        *,
        exit_reason: str,
        exit_price: Decimal,
        realized_pnl_usd: Decimal,
        now_ms: int,
    ) -> bool:
        self.closed = (exit_reason, exit_price, realized_pnl_usd)
        return True


class _FullCloseVenue(_PartialCloseVenue):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled: set[str] = set()

    def submit_close(self, *, symbol: str, side: str, quantity: Decimal, client_order_id: str):
        assert (symbol, side, quantity, client_order_id) == ("BTCUSDT", "SELL", Decimal("1"), "tfm-c-partial")
        self.quantity = Decimal("0")
        return ManualVenueOrderReceipt(
            client_id=client_order_id,
            provider_id="88",
            status="FILLED",
            executed_quantity=quantity,
            average_price=Decimal("101"),
        )

    def query_leg(self, plan: ManualExecutionPlan, leg: str):
        client_id = getattr(plan, f"{leg}_client_order_id")
        return ManualVenueOrderReceipt(
            client_id=client_id,
            provider_id="99",
            status="CANCELED" if leg in self.cancelled else "NEW",
        )

    def cancel_leg(self, plan: ManualExecutionPlan, leg: str) -> bool:
        self.cancelled.add(leg)
        return True


def test_full_manual_close_cancels_both_protections_and_uses_actual_fill_for_final_pnl() -> None:
    venue = _FullCloseVenue()
    store = _FullCloseStore(venue)
    ticks = iter(
        (
            1_900_000_000_200,
            1_900_000_000_300,
            1_900_000_000_400,
            1_900_000_000_500,
        )
    )
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: next(ticks))

    assert service.turn() == "close_filled"
    assert service.turn() == "take_profit_cancelled"
    assert service.turn() == "stop_loss_cancelled"
    assert service.turn() == "position_closed"

    assert store.closed == ("manual_close", Decimal("101"), Decimal("1"))
    assert venue.cancelled == {"take_profit", "stop_loss"}


class _DelayedClosePriceVenue(_FullCloseVenue):
    def __init__(self) -> None:
        super().__init__()
        self.submitted = False

    def query_close(self, *, symbol: str, client_order_id: str):
        if not self.submitted:
            return None
        return ManualVenueOrderReceipt(
            client_id=client_order_id,
            provider_id="88",
            status="FILLED",
            executed_quantity=Decimal("1"),
            average_price=Decimal("101"),
        )

    def submit_close(self, *, symbol: str, side: str, quantity: Decimal, client_order_id: str):
        self.submitted = True
        self.quantity = Decimal("0")
        return ManualVenueOrderReceipt(
            client_id=client_order_id,
            provider_id="88",
            status="FILLED",
            executed_quantity=quantity,
            average_price=None,
        )


def test_full_close_defers_until_order_query_returns_the_actual_average_price() -> None:
    venue = _DelayedClosePriceVenue()
    store = _FullCloseStore(venue)
    ticks = iter(
        (
            1_900_000_000_200,
            1_900_000_000_300,
            1_900_000_000_400,
            1_900_000_000_500,
            1_900_000_000_600,
        )
    )
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: next(ticks))

    assert service.turn() == "close_fill_price_deferred"
    assert service.turn() == "close_filled"
    assert service.turn() == "take_profit_cancelled"
    assert service.turn() == "stop_loss_cancelled"
    assert service.turn() == "position_closed"

    assert store.closed == ("manual_close", Decimal("101"), Decimal("1"))


def test_legacy_filled_close_without_price_is_reconciled_before_final_settlement() -> None:
    venue = _DelayedClosePriceVenue()
    venue.submitted = True
    venue.quantity = Decimal("0")
    store = _FullCloseStore(venue)
    assert store.managed.close_request is not None
    incomplete = ManualVenueOrderReceipt(
        client_id=store.managed.close_request.client_order_id,
        provider_id="88",
        status="FILLED",
        executed_quantity=Decimal("1"),
        average_price=None,
    )
    store.managed = replace(
        store.managed,
        close_request=store.managed.close_request.model_copy(
            update={
                "state": ManualCloseState.FILLED,
                "target_quantity": Decimal("1"),
                "attempted_at_ms": 1_900_000_000_150,
                "receipt": {
                    "client_id": incomplete.client_id,
                    "provider_id": incomplete.provider_id,
                    "status": incomplete.status,
                    "executed_quantity": "1",
                    "average_price": None,
                },
            }
        ),
        close_receipts=(incomplete,),
        take_profit_cancel_attempted=True,
        take_profit_cancelled=True,
        stop_loss_cancel_attempted=True,
        stop_loss_cancelled=True,
    )
    ticks = iter((1_900_000_000_300, 1_900_000_000_400))
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: next(ticks))

    assert service.turn() == "close_receipt_reconciled"
    assert service.turn() == "position_closed"

    assert "close_receipt_reconciled" in store.events
    assert store.closed == ("manual_close", Decimal("101"), Decimal("1"))


class _ProtectedCloseStore(_FullCloseStore):
    def __init__(self, venue: _Venue) -> None:
        super().__init__(venue)
        self.managed = replace(self.managed, close_request=None, close_receipts=())


class _ProtectedCloseVenue(_FullCloseVenue):
    def __init__(self) -> None:
        super().__init__()
        self.quantity = Decimal("0")

    def query_leg(self, plan: ManualExecutionPlan, leg: str):
        client_id = getattr(plan, f"{leg}_client_order_id")
        if leg == "take_profit":
            return ManualVenueOrderReceipt(
                client_id=client_id,
                provider_id="100",
                status="FINISHED",
                executed_quantity=Decimal("1"),
                average_price=Decimal("102"),
            )
        return ManualVenueOrderReceipt(
            client_id=client_id,
            provider_id="101",
            status="CANCELED" if leg in self.cancelled else "NEW",
        )


def test_triggered_protection_uses_provider_fill_for_exit_reason_price_and_pnl() -> None:
    venue = _ProtectedCloseVenue()
    store = _ProtectedCloseStore(venue)
    ticks = iter((1_900_000_000_200, 1_900_000_000_300, 1_900_000_000_400))
    service = ManualExecutionService(store=store, venue=venue, clock_ms=lambda: next(ticks))

    assert service.turn() == "take_profit_cancelled"
    assert service.turn() == "stop_loss_cancelled"
    assert service.turn() == "position_closed"

    assert store.closed == ("take_profit", Decimal("102"), Decimal("2"))
