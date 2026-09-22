"""Equity, the daily baseline and fixed-risk sizing, read from the Nautilus Cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.identifiers import AccountId


def decimal_value(value: Any) -> Decimal:
    """One conversion from a Nautilus quantity, price, money or plain number to `Decimal`.

    Nautilus values carry their own precision through `as_decimal()`; `str()` on the wrapper would
    lose it.
    """

    if isinstance(value, Decimal):
        return value
    method = getattr(value, "as_decimal", None)
    if method is not None:
        return Decimal(method())
    return Decimal(str(value))


def quote_mid(cache: Any, instrument_id: Any) -> Decimal | None:
    quote = cache.quote_tick(instrument_id)
    if quote is None:
        return None
    bid = decimal_value(quote.bid_price)
    ask = decimal_value(quote.ask_price)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / Decimal(2)


def account_equity_usd(*, cache: Any, account_id: AccountId) -> Decimal | None:
    """The one equity this Runtime means: USDT balance plus unrealized PnL of every open position.

    `None` when the balance is not known yet or a position cannot be marked: an entry then waits for
    the next quote rather than sizing against half an account. Positions are read without an account
    filter -- Nautilus 1.231 reconciles orders without their account id, and this slot is the only
    account the node has.
    """

    account = cache.account(account_id)
    if account is None:
        return None
    total = account.balance_total(USDT)
    if total is None:
        return None
    equity = decimal_value(total)
    for position in cache.positions_open():
        mark = quote_mid(cache, position.instrument_id)
        instrument = cache.instrument(position.instrument_id)
        if mark is None or instrument is None:
            return None
        pnl = position.unrealized_pnl(instrument.make_price(mark))
        if pnl is None:
            return None
        equity += decimal_value(pnl)
    return equity


@dataclass(frozen=True, slots=True)
class DayStartBaseline:
    """One durable UTC-day equity point recovered from an Observation."""

    utc_day: str
    equity_usd: Decimal
    recorded_at_ns: int
    event_id: str

    def __post_init__(self) -> None:
        try:
            parsed_day = date.fromisoformat(self.utc_day)
        except ValueError:
            parsed_day = None
        if (
            parsed_day is None
            or parsed_day.isoformat() != self.utc_day
            or self.equity_usd <= 0
            or self.recorded_at_ns <= 0
        ):
            raise ValueError("oi_runtime_day_start_baseline_invalid")
        if len(self.event_id) != 64:
            raise ValueError("oi_runtime_day_start_baseline_invalid")


def fixed_risk_quantity(
    *,
    price: Decimal,
    stop_distance_bps: int,
    allowed_risk_usd: Decimal,
    equity_usd: Decimal,
    max_leverage: int,
    existing_notional_usd: Decimal,
    size_increment: Decimal,
) -> Decimal:
    """Size from fixed loss risk, clamp to leverage, then round down."""

    if price <= 0 or allowed_risk_usd <= 0 or stop_distance_bps <= 0 or size_increment <= 0:
        raise ValueError("oi_runtime_sizing_input_invalid")
    stop_fraction = Decimal(stop_distance_bps) / Decimal(10_000)
    risk_notional = allowed_risk_usd / stop_fraction
    leverage_notional = max(Decimal(0), equity_usd * max_leverage - existing_notional_usd)
    notional = min(risk_notional, leverage_notional)
    if notional <= 0:
        raise ValueError("oi_runtime_sizing_capacity_exhausted")
    raw_quantity = notional / price
    return (raw_quantity / size_increment).to_integral_value(rounding=ROUND_FLOOR) * size_increment


__all__ = [
    "DayStartBaseline",
    "account_equity_usd",
    "decimal_value",
    "fixed_risk_quantity",
    "quote_mid",
]
