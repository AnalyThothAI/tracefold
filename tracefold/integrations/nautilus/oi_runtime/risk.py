"""Thin deterministic futures risk gap policy owned by the Nautilus Runtime."""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId, PositionId, StrategyId

from .config import OiRiskLimits
from .state import unowned_cache_exposure

# Two answers, because the caller only ever asked one question: may this entry be submitted? `halt`
# and `deny` were the same branch on the entry path, and `reduce` was `allow` with a smaller number
# the caller was already going to size from. The reason word carries the difference, and it is the
# word the disposition observation records (#537 PR-3).
RiskAction = Literal["allow", "refuse"]


def decimal_value(value: Any) -> Decimal:
    """One conversion from a Nautilus quantity, price, money or plain number to `Decimal`.

    Nautilus values carry their own precision through `asdecimal_value()`; `str()` on the wrapper would
    lose it. This was re-implemented in the risk facts and in the account projection (#510 E).
    """

    if isinstance(value, Decimal):
        return value
    method = getattr(value, "as_decimal", None)
    if method is not None:
        return Decimal(method())
    return Decimal(str(value))


def _mid_price(cache: Any, instrument_id: InstrumentId) -> tuple[Decimal, int]:
    quote = cache.quote_tick(instrument_id)
    if quote is None:
        raise RuntimeError("oi_runtime_market_missing")
    bid = decimal_value(quote.bid_price)
    ask = decimal_value(quote.ask_price)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError("oi_runtime_market_invalid")
    return (bid + ask) / Decimal(2), int(quote.ts_event)


def account_equity_usd(
    *,
    cache: Any,
    portfolio: Any,
    account_id: AccountId,
    routes: Container[InstrumentId],
) -> Decimal:
    """The one equity this Runtime means: USDT balance plus unrealized PnL at current marks.

    `evaluate_entry` subtracts `DayStartBaseline.equity_usd` from `NautilusRiskFacts.equity_usd`, so
    both have to be this function or `daily_loss_limit` compares two different definitions (#510 B).
    Positions on unrouted instruments are not priced here; they are already unexpected exposure and
    `NautilusRiskFacts.collect` is what names them.
    """

    account = cache.account(account_id)
    if account is None:
        raise RuntimeError("oi_runtime_account_missing")
    total = account.balance_total(USDT)
    if total is None:
        raise RuntimeError("oi_runtime_account_balance_missing")
    equity = decimal_value(total)
    for position in cache.positions_open(account_id=account_id):
        instrument_id = position.instrument_id
        if instrument_id not in routes:
            continue
        price, _ = _mid_price(cache, instrument_id)
        instrument = cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError("oi_runtime_instrument_missing")
        unrealized = portfolio.unrealized_pnl(
            instrument_id,
            price=instrument.make_price(price),
            account_id=account_id,
            target_currency=USDT,
        )
        if unrealized is not None:
            equity += decimal_value(unrealized)
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


@dataclass(frozen=True, slots=True)
class NautilusRiskFacts:
    """Read-only facts collected from Nautilus Cache and Portfolio."""

    equity_usd: Decimal
    gross_position_notional_usd: Decimal
    open_order_notional_usd: Decimal
    inflight_order_notional_usd: Decimal
    aggregate_risk_usd: Decimal
    current_positions: int
    market_observed_at_ns: int
    account_observed_at_ns: int
    reconciliation_observed_at_ns: int
    unexpected_exposure: bool

    @classmethod
    def collect(
        cls,
        *,
        cache: Any,
        portfolio: Any,
        account_id: AccountId,
        strategy_id: StrategyId,
        routes: dict[InstrumentId, int],
        candidate_instrument_id: InstrumentId,
        owned_order_ids: frozenset[ClientOrderId],
        owned_position_ids: frozenset[PositionId],
        account_observed_at_ns: int,
        reconciliation_observed_at_ns: int,
    ) -> NautilusRiskFacts:
        equity = account_equity_usd(cache=cache, portfolio=portfolio, account_id=account_id, routes=routes)
        positions = tuple(cache.positions_open(account_id=account_id))
        open_orders = tuple(cache.orders_open(account_id=account_id))
        inflight_orders = tuple(cache.orders_inflight(account_id=account_id))
        open_ids = {order.client_order_id for order in open_orders}
        inflight_orders = tuple(order for order in inflight_orders if order.client_order_id not in open_ids)
        unowned_orders, unowned_positions = unowned_cache_exposure(
            cache=cache,
            account_id=account_id,
            strategy_id=strategy_id,
            owned_order_ids=owned_order_ids,
            owned_position_ids=owned_position_ids,
        )

        priced_instruments = {
            candidate_instrument_id,
            *(position.instrument_id for position in positions),
            *(order.instrument_id for order in (*open_orders, *inflight_orders)),
        }
        market_clocks: list[int] = []
        prices: dict[InstrumentId, Decimal] = {}
        for instrument_id in priced_instruments:
            if instrument_id not in routes:
                continue
            price, observed_at_ns = _mid_price(cache, instrument_id)
            prices[instrument_id] = price
            market_clocks.append(observed_at_ns)
        if candidate_instrument_id not in prices:
            raise RuntimeError("oi_runtime_candidate_market_missing")
        market_observed_at_ns = min(market_clocks)

        gross_position_notional = Decimal(0)
        aggregate_risk = Decimal(0)
        # Ownership is one scan with one definition; what stays local is the pricing gap, which is a
        # different fact: an instrument this Runtime has no route for cannot be marked at all.
        unexpected = bool(unowned_orders or unowned_positions)
        for position in positions:
            instrument_id = position.instrument_id
            price = prices.get(instrument_id)
            stop_bps = routes.get(instrument_id)
            if price is None or stop_bps is None:
                unexpected = True
                continue
            notional = abs(decimal_value(position.quantity)) * price
            gross_position_notional += notional
            aggregate_risk += notional * Decimal(stop_bps) / Decimal(10_000)

        def order_values(order: Any) -> tuple[Decimal, Decimal]:
            nonlocal unexpected
            price = prices.get(order.instrument_id)
            stop_bps = routes.get(order.instrument_id)
            if price is None or stop_bps is None:
                unexpected = True
                return Decimal(0), Decimal(0)
            if bool(order.is_reduce_only):
                return Decimal(0), Decimal(0)
            quantity = decimal_value(getattr(order, "leaves_qty", order.quantity))
            notional = abs(quantity) * price
            return notional, notional * Decimal(stop_bps) / Decimal(10_000)

        open_values = tuple(order_values(order) for order in open_orders)
        inflight_values = tuple(order_values(order) for order in inflight_orders)
        open_notional = sum((value[0] for value in open_values), Decimal(0))
        inflight_notional = sum((value[0] for value in inflight_values), Decimal(0))
        aggregate_risk += sum((value[1] for value in (*open_values, *inflight_values)), Decimal(0))

        return cls(
            equity_usd=equity,
            gross_position_notional_usd=gross_position_notional,
            open_order_notional_usd=open_notional,
            inflight_order_notional_usd=inflight_notional,
            aggregate_risk_usd=aggregate_risk,
            current_positions=len(
                {position.instrument_id for position in positions}
                | {order.instrument_id for order in (*open_orders, *inflight_orders) if not bool(order.is_reduce_only)}
            ),
            market_observed_at_ns=market_observed_at_ns,
            account_observed_at_ns=account_observed_at_ns,
            reconciliation_observed_at_ns=reconciliation_observed_at_ns,
            unexpected_exposure=unexpected,
        )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    action: RiskAction
    reason: str
    allowed_risk_usd: Decimal = Decimal(0)


class OiFuturesRiskPolicy:
    """Return a decision only; Nautilus remains the only order and position owner."""

    def __init__(self, limits: OiRiskLimits) -> None:
        self.limits = limits

    def evaluate_entry(
        self,
        *,
        facts: NautilusRiskFacts,
        baseline: DayStartBaseline,
        now_ns: int,
        requested_risk_usd: Decimal,
        candidate_is_new_position: bool,
    ) -> RiskDecision:
        """The first named refusal, or the risk budget this entry may spend.

        There is no leverage refusal here. The one caller passed `max_leverage` itself as the request,
        so `requested > max` compared a number with itself; leverage is a *sizing* clamp and
        `fixed_risk_quantity` is where it is applied (#537 PR-3).
        """

        if facts.unexpected_exposure:
            return RiskDecision("refuse", "unexpected_exposure")
        if now_ns - facts.market_observed_at_ns > self.limits.market_stale_after_ns:
            return RiskDecision("refuse", "market_stale")
        if now_ns - facts.account_observed_at_ns > self.limits.account_stale_after_ns:
            return RiskDecision("refuse", "account_stale")
        if now_ns - facts.reconciliation_observed_at_ns > self.limits.reconciliation_stale_after_ns:
            return RiskDecision("refuse", "reconciliation_stale")
        if baseline.equity_usd - facts.equity_usd >= self.limits.max_daily_loss_usd:
            return RiskDecision("refuse", "daily_loss_limit")
        if candidate_is_new_position and facts.current_positions >= self.limits.max_positions:
            return RiskDecision("refuse", "position_limit")
        if requested_risk_usd <= 0:
            return RiskDecision("refuse", "risk_non_positive")

        per_trade = min(
            requested_risk_usd,
            facts.equity_usd * self.limits.risk_fraction_per_trade,
            self.limits.max_risk_per_trade_usd,
        )
        remaining = self.limits.max_total_risk_usd - facts.aggregate_risk_usd
        # A budget smaller than the request is the answer, not a second verdict: the entry is sized
        # from `allowed_risk_usd` either way, and the caller treated `reduce` and `allow` as one path.
        # Only an exhausted budget refuses, and under `max_positions = 1` that is a configuration with
        # no room for a first trade rather than a portfolio that filled up.
        allowed = min(per_trade, remaining)
        if allowed <= 0:
            return RiskDecision("refuse", "risk_non_positive")
        return RiskDecision("allow", "risk_allowed", allowed)


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
    "NautilusRiskFacts",
    "OiFuturesRiskPolicy",
    "RiskAction",
    "RiskDecision",
    "account_equity_usd",
    "decimal_value",
    "fixed_risk_quantity",
]
