"""Pure execution decisions. Venue and PostgreSQL effects belong to the runner."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_CLIENT_ID = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")
Side = Literal["long", "short"]
Leg = Literal["entry", "sl", "tp", "time_exit", "safety_flatten", "account_flatten"]


def client_order_id(*, namespace: str, entry_id: str, leg: Leg, attempt: int) -> str:
    """One Binance-compatible identity for ordinary and Algo orders."""

    if not namespace or not entry_id or attempt < 1:
        raise ValueError("order_identity_invalid")
    value = "tf" + hashlib.sha256(f"{namespace}|{entry_id}|{leg}|{attempt}".encode()).hexdigest()[:30]
    if len(value) != 32 or _CLIENT_ID.fullmatch(value) is None:
        raise AssertionError("order_identity_binance_invalid")
    return value


class EntryRequest(BaseModel):
    """Geometry and reference facts shared by automatic and operator entry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    account_slot: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")
    asset_id: str = Field(pattern=r"^crypto:[A-Z0-9._-]{1,32}$")
    native_symbol: str = Field(pattern=r"^[A-Z0-9]{2,32}$")
    side: Side
    reference_price: Decimal = Field(gt=0)
    reference_at_ns: int | None = Field(default=None, gt=0)
    max_drift_bps: int = Field(ge=1, le=2000)
    stop_bps: int = Field(ge=1, le=5000)
    tp_bps: int = Field(ge=1, le=20000)
    max_hold_s: int = Field(ge=1, le=86400)
    geometry_version: str = Field(min_length=1, max_length=64)
    decided_at_ns: int = Field(gt=0)
    expires_at_ns: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_clock(self) -> Self:
        if not self.decided_at_ns < self.expires_at_ns <= self.decided_at_ns + 300000000000:
            raise ValueError("entry_expiry_invalid")
        return self


class SignalV4(EntryRequest):
    signal_version: Literal["trade_signal_v4"] = "trade_signal_v4"
    seq: int = Field(ge=1)
    signal_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_id: str = Field(min_length=1, max_length=128)
    entry_scope_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping_semantics_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True, slots=True)
class EntryFacts:
    now_ns: int
    entries_paused: bool
    emergency_halted: bool
    symbol_position: Decimal
    symbol_regular_orders: int
    symbol_algo_orders: int
    active_plans: int
    max_plans: int
    equity_usdt: Decimal
    active_notional_usdt: Decimal
    max_leverage: int
    risk_fraction: Decimal
    available_margin_usdt: Decimal
    pending_margin_usdt: Decimal | None
    venue_leverage: int
    margin_mode: str
    mark_price: Decimal
    fee_bps: Decimal
    price_buffer_bps: Decimal
    max_spread_fraction_of_stop: Decimal
    live_mid_price: Decimal
    live_quote_at_ns: int
    account_at_ns: int
    account_max_age_ns: int
    cooldown_until_ns: int
    bid: Decimal
    ask: Decimal
    quote_at_ns: int
    quote_max_age_ns: int
    market_min_qty: Decimal
    market_max_qty: Decimal
    market_step: Decimal
    min_notional: Decimal
    venue_max_notional_usdt: Decimal | None = None
    multi_assets_mode: bool = True
    can_trade: bool = False
    hedge_mode: bool = False
    symbol_active_plans: int = 0


@dataclass(frozen=True, slots=True)
class EntryVerdict:
    accepted: bool
    reason: str
    quantity: Decimal | None = None
    reserved_margin_usdt: Decimal | None = None


def admit(signal: EntryRequest, facts: EntryFacts) -> EntryVerdict:
    """A single admission decision from fresh venue facts and frozen Signal geometry."""

    def refuse(reason: str) -> EntryVerdict:
        return EntryVerdict(False, reason)

    if facts.now_ns >= signal.expires_at_ns:
        return refuse("expired")
    if facts.emergency_halted:
        return refuse("emergency_halt")
    if facts.entries_paused:
        return refuse("entries_paused")
    if facts.hedge_mode:
        return refuse("hedge_mode_unsupported")
    if facts.multi_assets_mode:
        return refuse("multi_assets_mode_unsupported")
    if not facts.can_trade:
        return refuse("account_trading_disabled")
    if facts.margin_mode != "CROSSED":
        return refuse("margin_mode_unsupported")
    if facts.now_ns < facts.cooldown_until_ns:
        return refuse("post_stop_cooldown")
    if facts.symbol_position or facts.symbol_regular_orders or facts.symbol_algo_orders or facts.symbol_active_plans:
        return refuse("symbol_exposure")
    if facts.active_plans >= facts.max_plans:
        return refuse("capacity")
    numbers = (
        facts.equity_usdt,
        facts.available_margin_usdt,
        facts.active_notional_usdt,
        facts.risk_fraction,
        facts.bid,
        facts.ask,
        facts.mark_price,
        facts.live_mid_price,
        facts.fee_bps,
        facts.price_buffer_bps,
        facts.market_step,
        facts.market_min_qty,
        facts.market_max_qty,
        facts.min_notional,
        facts.max_spread_fraction_of_stop,
    )
    if any(
        not value.is_finite()
        for value in (*numbers, *((facts.pending_margin_usdt,) if facts.pending_margin_usdt is not None else ()))
    ):
        return refuse("entry_facts_invalid")
    if facts.bid <= 0 or facts.ask <= facts.bid or not 0 <= facts.now_ns - facts.quote_at_ns <= facts.quote_max_age_ns:
        return refuse("quote_stale")
    if facts.live_mid_price <= 0 or not 0 <= facts.now_ns - facts.live_quote_at_ns <= facts.quote_max_age_ns:
        return refuse("live_quote_stale")
    mid = (facts.bid + facts.ask) / 2
    spread_bps = (facts.ask - facts.bid) / mid * 10_000
    if spread_bps > Decimal(signal.stop_bps) * facts.max_spread_fraction_of_stop:
        return refuse("spread")
    executable = facts.ask if signal.side == "long" else facts.bid
    drift_bps = abs(executable - signal.reference_price) / signal.reference_price * 10_000
    if drift_bps > signal.max_drift_bps:
        return refuse("price_drift")
    if facts.equity_usdt <= 0 or facts.max_leverage < 1 or not 0 < facts.risk_fraction <= 1:
        return refuse("equity_unavailable")
    if not 0 <= facts.now_ns - facts.account_at_ns <= facts.account_max_age_ns:
        return refuse("account_stale")
    if facts.pending_margin_usdt is None:
        return refuse("pending_margin_unknown")
    if (
        facts.venue_leverage < 1
        or facts.mark_price <= 0
        or facts.available_margin_usdt < 0
        or facts.pending_margin_usdt < 0
        or facts.fee_bps < 0
        or facts.price_buffer_bps < 0
        or not 0 < facts.max_spread_fraction_of_stop <= 1
    ):
        return refuse("margin_facts_invalid")
    if facts.market_step <= 0 or facts.market_min_qty <= 0 or facts.market_max_qty < facts.market_min_qty:
        return refuse("market_rules_invalid")
    risk_notional = facts.equity_usdt * facts.risk_fraction * Decimal(10_000) / signal.stop_bps
    if facts.venue_max_notional_usdt is not None:
        if not facts.venue_max_notional_usdt.is_finite() or facts.venue_max_notional_usdt <= 0:
            return refuse("margin_facts_invalid")
        risk_notional = min(risk_notional, facts.venue_max_notional_usdt)
    free_notional = facts.equity_usdt * facts.max_leverage - facts.active_notional_usdt
    if free_notional <= 0:
        return refuse("leverage_capacity")
    free_margin = facts.available_margin_usdt - facts.pending_margin_usdt
    if free_margin <= 0:
        return refuse("margin_capacity")
    # Binance initial margin uses mark/leverage. Market execution and fees need
    # a separate buffer; configured leverage is only a portfolio ceiling.
    buffered_price = max(executable, facts.mark_price) * (1 + facts.price_buffer_bps / 10_000)
    margin_per_unit = buffered_price * (Decimal(1) / facts.venue_leverage + facts.fee_bps / 10_000)
    quantity = (
        min(risk_notional / executable, free_notional / executable, free_margin / margin_per_unit, facts.market_max_qty)
        / facts.market_step
    ).to_integral_value(rounding=ROUND_DOWN) * facts.market_step
    if quantity < facts.market_min_qty or quantity * executable < facts.min_notional:
        return refuse("market_lot_or_notional")
    return EntryVerdict(True, "accepted", quantity, quantity * margin_per_unit)


@dataclass(frozen=True, slots=True)
class PlanFacts:
    """A durable plan joined to the latest REST snapshot; no WebSocket truth is needed."""

    now_ns: int
    entered_at_ns: int | None
    max_hold_s: int
    position_amount: Decimal
    entry_order_status: str | None
    entry_submission_unknown: bool
    sl_status: str | None
    tp_status: str | None
    sl_attempts: int
    tp_attempts: int
    sl_submission_unknown: bool
    tp_submission_unknown: bool
    flatten_status: str | None
    exit_fill_client_id: str | None
    sl_client_ids: frozenset[str]
    tp_client_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class PlanStep:
    action: Literal[
        "await_entry",
        "query_entry",
        "submit_sl",
        "submit_tp",
        "query_sl",
        "query_tp",
        "flatten",
        "query_flatten",
        "cancel_protection",
        "cancel_entry",
        "terminal",
        "await_venue",
    ]
    reason: str


def step(facts: PlanFacts) -> PlanStep:
    """Choose one recoverable action; the runner persists intent before venue I/O."""

    if facts.entry_submission_unknown and facts.position_amount == 0:
        return PlanStep("query_entry", "entry_submission_unknown")
    entry_pending = facts.entry_submission_unknown or facts.entry_order_status not in (
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "REJECTED",
        "NOT_SUBMITTED",
    )
    if entry_pending and facts.position_amount == 0:
        return PlanStep("await_entry", "entry_not_terminal")
    if facts.position_amount == 0:
        if facts.sl_submission_unknown:
            return PlanStep("query_sl", "flat_protection_submission_unknown")
        if facts.tp_submission_unknown:
            return PlanStep("query_tp", "flat_protection_submission_unknown")
        if facts.sl_status in ("NEW", "PARTIALLY_FILLED") or facts.tp_status in ("NEW", "PARTIALLY_FILLED"):
            return PlanStep("cancel_protection", "venue_flat")
        if facts.exit_fill_client_id in facts.sl_client_ids:
            return PlanStep("terminal", "stop_filled")
        if facts.exit_fill_client_id in facts.tp_client_ids:
            return PlanStep("terminal", "take_profit")
        return PlanStep(
            "terminal", "external" if facts.entered_at_ns or facts.entry_order_status == "FILLED" else "not_submitted"
        )
    if facts.entered_at_ns is None:
        return PlanStep("await_venue", "entry_position_without_terminal_clock")
    if entry_pending:
        if facts.sl_submission_unknown:
            return PlanStep("query_sl", "protection_submission_unknown")
        if facts.sl_status not in ("NEW", "PARTIALLY_FILLED", "FILLED"):
            if facts.sl_attempts >= 3:
                return PlanStep("cancel_entry", "unprotected_entry_pending")
            return PlanStep("submit_sl", "partial_entry_unprotected")
        return PlanStep("cancel_entry", "partial_entry")
    if facts.flatten_status in ("NEW", "PARTIALLY_FILLED", "unknown"):
        return PlanStep("query_flatten", "flatten_pending")
    if facts.flatten_status == "FILLED":
        return PlanStep("flatten", "flatten_fill_left_exposure")
    if facts.sl_status == "FILLED" or facts.tp_status == "FILLED":
        return PlanStep("flatten", "partial_protection_exit")
    if facts.now_ns >= facts.entered_at_ns + facts.max_hold_s * 1_000_000_000:
        return PlanStep("flatten", "time_exit")
    if facts.sl_submission_unknown:
        return PlanStep("query_sl", "protection_submission_unknown")
    if facts.sl_status not in ("NEW", "PARTIALLY_FILLED", "FILLED"):
        if facts.sl_attempts >= 3:
            return PlanStep("flatten", "protection_failed")
        return PlanStep("submit_sl", "unprotected")
    if facts.tp_submission_unknown:
        return PlanStep("query_tp", "protection_submission_unknown")
    if facts.tp_status not in ("NEW", "PARTIALLY_FILLED", "FILLED"):
        if facts.tp_attempts >= 3:
            return PlanStep("flatten", "protection_failed")
        return PlanStep("submit_tp", "unprotected")
    return PlanStep("await_venue", "protected")


__all__ = [
    "EntryFacts",
    "EntryRequest",
    "EntryVerdict",
    "PlanFacts",
    "PlanStep",
    "SignalV4",
    "admit",
    "client_order_id",
    "step",
]
