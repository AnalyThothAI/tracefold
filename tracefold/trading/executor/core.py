"""Pure execution decisions. Venue and PostgreSQL effects belong to the runner."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..operator_control import control_entry_block

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


class SignalV4(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    signal_version: Literal["trade_signal_v4"] = "trade_signal_v4"
    signal_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_id: str = Field(min_length=1, max_length=128)
    account_slot: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")
    entry_scope_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset_id: str = Field(pattern=r"^crypto:[A-Z0-9._-]{1,32}$")
    native_symbol: str = Field(pattern=r"^[A-Z0-9]{2,32}$")
    mapping_semantics_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    side: Side
    reference_price: Decimal = Field(gt=0)
    max_drift_bps: int = Field(ge=1, le=2_000)
    stop_bps: int = Field(ge=1, le=5_000)
    tp_bps: int = Field(ge=1, le=20_000)
    max_hold_s: int = Field(ge=1, le=86_400)
    policy_id: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    geometry_version: str = Field(min_length=1, max_length=64)
    decided_at_ns: int = Field(gt=0)
    expires_at_ns: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_clock(self) -> SignalV4:
        if not self.decided_at_ns < self.expires_at_ns <= self.decided_at_ns + 300_000_000_000:
            raise ValueError("signal_expiry_invalid")
        return self


@dataclass(frozen=True, slots=True)
class EntryFacts:
    now_ns: int
    entries_paused: bool
    emergency_halted: bool
    symbol_position: Decimal
    symbol_regular_orders: int
    symbol_algo_orders: int
    active_entries: int
    max_entries: int
    equity_usdt: Decimal
    active_notional_usdt: Decimal
    max_leverage: int
    risk_fraction: Decimal
    bid: Decimal
    ask: Decimal
    quote_at_ns: int
    quote_max_age_ns: int
    market_min_qty: Decimal
    market_max_qty: Decimal
    market_step: Decimal
    min_notional: Decimal
    hedge_mode: bool = False
    flatten_in_progress: bool = False
    unexpected_exposure: bool = False
    execution_fault: bool = False
    available_margin_usdt: Decimal | None = None
    unreflected_pending_margin_usdt: Decimal = Decimal(0)
    actual_leverage: int = 0
    margin_type: str = ""
    multi_assets_mode: bool = False
    mark_price: Decimal = Decimal(0)
    taker_fee_rate: Decimal = Decimal(0)
    symbol_notional_cap: Decimal = Decimal(0)
    snapshot_started_at_ns: int = 0
    initial_margin_usdt: Decimal = Decimal(0)
    active_entry_versions: tuple[tuple[str, int], ...] = ()
    venue_quote_at_ns: int | None = None
    venue_mark_at_ns: int | None = None


@dataclass(frozen=True, slots=True)
class EntryVerdict:
    accepted: bool
    reason: str
    quantity: Decimal | None = None
    reserved_margin_usdt: Decimal | None = None
    bounded_price: Decimal | None = None


def admit(signal: SignalV4, facts: EntryFacts) -> EntryVerdict:
    """A single admission decision from fresh venue facts and frozen Signal geometry."""

    def refuse(reason: str) -> EntryVerdict:
        return EntryVerdict(False, reason)

    if facts.now_ns >= signal.expires_at_ns:
        return refuse("expired")
    blocked = control_entry_block(
        {
            "emergency_halted": facts.emergency_halted,
            "entries_paused": facts.entries_paused,
            "flatten_command_id": True if facts.flatten_in_progress else None,
            "execution_faults": facts.execution_fault,
        },
        unexpected_exposure=facts.unexpected_exposure,
    )
    if blocked is not None:
        return refuse(blocked)
    if facts.hedge_mode:
        return refuse("hedge_mode_unsupported")
    if facts.multi_assets_mode:
        return refuse("multi_assets_unsupported")
    if facts.margin_type not in ("crossed", "isolated"):
        return refuse("margin_mode_unsupported")
    if facts.symbol_position or facts.symbol_regular_orders or facts.symbol_algo_orders:
        return refuse("symbol_exposure")
    if facts.active_entries >= facts.max_entries:
        return refuse("capacity")
    if facts.bid <= 0 or facts.ask <= facts.bid or facts.now_ns - facts.quote_at_ns > facts.quote_max_age_ns:
        return refuse("quote_stale")
    mid = (facts.bid + facts.ask) / 2
    spread_bps = (facts.ask - facts.bid) / mid * 10_000
    if spread_bps > Decimal(signal.stop_bps) * Decimal("0.3"):
        return refuse("spread")
    executable = facts.ask if signal.side == "long" else facts.bid
    drift_bps = abs(executable - signal.reference_price) / signal.reference_price * 10_000
    if drift_bps > signal.max_drift_bps:
        return refuse("price_drift")
    if facts.equity_usdt <= 0 or facts.max_leverage < 1 or not 0 < facts.risk_fraction <= 1:
        return refuse("equity_unavailable")
    if facts.market_step <= 0 or facts.market_min_qty <= 0 or facts.market_max_qty < facts.market_min_qty:
        return refuse("market_rules_invalid")
    risk_notional = facts.equity_usdt * facts.risk_fraction * Decimal(10_000) / signal.stop_bps
    free_notional = facts.equity_usdt * facts.max_leverage - facts.active_notional_usdt
    if free_notional <= 0:
        return refuse("leverage_capacity")
    if (
        facts.available_margin_usdt is None
        or facts.available_margin_usdt < 0
        or facts.actual_leverage < 1
        or facts.mark_price <= 0
        or not 0 <= facts.taker_fee_rate < 1
        or facts.symbol_notional_cap <= 0
    ):
        return refuse("funding_facts_unavailable")
    # A bounded adverse fill consumes initial margin, taker fees and opening loss
    # relative to mark. Isolated openings allocate margin from the same USDT
    # available balance; cross openings also cannot reuse position initial margin.
    buffer = executable * Decimal(signal.max_drift_bps) / 10_000
    bounded_price = executable + buffer
    opening_loss = (
        max(Decimal(0), executable - facts.mark_price if signal.side == "long" else facts.mark_price - executable)
        + buffer
    )
    unit_margin = max(bounded_price, facts.mark_price) / facts.actual_leverage
    unit_cost = unit_margin + bounded_price * facts.taker_fee_rate + opening_loss
    available = max(Decimal(0), facts.available_margin_usdt - facts.unreflected_pending_margin_usdt)
    if available <= 0:
        return refuse("available_margin")
    raw = min(
        risk_notional / bounded_price,
        free_notional / bounded_price,
        available / unit_cost,
        facts.symbol_notional_cap / bounded_price,
        facts.market_max_qty,
    )
    quantity = (raw / facts.market_step).to_integral_value(rounding=ROUND_DOWN) * facts.market_step
    if (
        quantity < facts.market_min_qty
        or quantity > facts.market_max_qty
        or quantity * executable < facts.min_notional
        or quantity % facts.market_step
    ):
        return refuse("market_lot_or_notional")
    return EntryVerdict(True, "accepted", quantity, quantity * unit_cost, bounded_price)


@dataclass(frozen=True, slots=True)
class EntryLifecycleFacts:
    """A durable entry joined to the latest REST snapshot; no WebSocket truth is needed."""

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
class EntryStep:
    action: Literal[
        "await_entry",
        "query_entry",
        "cancel_entry",
        "submit_sl",
        "submit_tp",
        "query_sl",
        "query_tp",
        "flatten",
        "query_flatten",
        "cancel_protection",
        "terminal",
        "await_venue",
    ]
    reason: str


def step(facts: EntryLifecycleFacts) -> EntryStep:
    """Choose one recoverable action; the runner persists intent before venue I/O."""

    if facts.position_amount == 0:
        if facts.entry_submission_unknown:
            return EntryStep("query_entry", "entry_submission_unknown")
        if facts.entry_order_status not in ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "NOT_SUBMITTED"):
            return EntryStep("cancel_entry", "entry_may_increase_exposure")
        if facts.flatten_status in ("NEW", "PARTIALLY_FILLED", "unknown"):
            return EntryStep("query_flatten", "flatten_pending")
        if facts.sl_submission_unknown:
            return EntryStep("query_sl", "protection_submission_unknown")
        if facts.tp_submission_unknown:
            return EntryStep("query_tp", "protection_submission_unknown")
        if facts.sl_status in ("NEW", "PARTIALLY_FILLED") or facts.tp_status in ("NEW", "PARTIALLY_FILLED"):
            return EntryStep("cancel_protection", "venue_flat")
        if facts.exit_fill_client_id in facts.sl_client_ids:
            return EntryStep("terminal", "stop_filled")
        if facts.exit_fill_client_id in facts.tp_client_ids:
            return EntryStep("terminal", "take_profit")
        return EntryStep(
            "terminal", "external" if facts.entered_at_ns or facts.entry_order_status == "FILLED" else "not_submitted"
        )
    if facts.entered_at_ns is None:
        return EntryStep("await_venue", "entry_position_without_terminal_clock")
    if facts.flatten_status in ("NEW", "PARTIALLY_FILLED", "unknown"):
        return EntryStep("query_flatten", "flatten_pending")
    if facts.flatten_status == "FILLED":
        return EntryStep("flatten", "flatten_fill_left_exposure")
    if facts.sl_status == "FILLED" or facts.tp_status == "FILLED":
        return EntryStep("flatten", "partial_protection_exit")
    if facts.now_ns >= facts.entered_at_ns + facts.max_hold_s * 1_000_000_000:
        return EntryStep("flatten", "time_exit")
    if facts.sl_submission_unknown:
        return EntryStep("query_sl", "protection_submission_unknown")
    if facts.sl_status not in ("NEW", "PARTIALLY_FILLED", "FILLED"):
        if facts.sl_attempts >= 3:
            return EntryStep("flatten", "protection_failed")
        return EntryStep("submit_sl", "unprotected")
    if facts.entry_submission_unknown:
        return EntryStep("query_entry", "entry_submission_unknown")
    if facts.entry_order_status not in ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "NOT_SUBMITTED"):
        return EntryStep("cancel_entry", "partial_entry_remainder")
    if facts.tp_submission_unknown:
        return EntryStep("query_tp", "protection_submission_unknown")
    if facts.tp_status not in ("NEW", "PARTIALLY_FILLED", "FILLED"):
        if facts.tp_attempts >= 3:
            return EntryStep("flatten", "protection_failed")
        return EntryStep("submit_tp", "unprotected")
    return EntryStep("await_venue", "protected")


__all__ = [
    "EntryFacts",
    "EntryLifecycleFacts",
    "EntryStep",
    "EntryVerdict",
    "SignalV4",
    "admit",
    "client_order_id",
    "step",
]
