"""Thin Nautilus BAR adapter: one fresh engine for one replay intent."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, PositionId, Symbol, TraderId
from nautilus_trader.model.instruments.crypto_perpetual import CryptoPerpetual
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from tracefold.trading import (
    ReplayBarV1,
    ReplayExecutionIntentV1,
    ReplayScenarioCapabilityV1,
    deterministic_client_order_id,
)
from tracefold.trading.execution_policy import (
    ENTRY_TTL_MS,
    MAX_ENTRY_DRIFT_BPS,
    MAX_HOLDING_MS,
    MAX_SPREAD_BPS,
    STOP_LOSS_BPS,
    TARGET_NOTIONAL_CEILING_USD,
    evaluate_entry,
    max_holding_due,
    stop_price,
)
from tracefold.trading.replay import BarEpisodeResult


class _EpisodeStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    intent_id: str
    quantity: Decimal
    entry_after_ms: int
    stop_loss_bps: int
    max_holding_ms: int
    price_increment: Decimal


class _EpisodeStrategy(Strategy):
    def __init__(self, config: _EpisodeStrategyConfig) -> None:
        super().__init__(config)
        self.instrument: Any = None
        self.entry_submitted = False
        self.exit_submitted = False
        self.entry_price: Decimal | None = None
        self.exit_price: Decimal | None = None
        self.opened_at_ms: int | None = None
        self.closed_at_ms: int | None = None
        self.position_id: PositionId | None = None
        self.stop_trigger_price: Decimal | None = None
        self.exit_reason: str | None = None
        self.fees = Decimal(0)

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            raise RuntimeError("replay_instrument_unavailable")
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        at_ms = int(bar.ts_event) // 1_000_000
        if not self.entry_submitted and at_ms >= self.config.entry_after_ms:
            entry = self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY,
                quantity=self.instrument.make_qty(self.config.quantity),
                reduce_only=False,
                client_order_id=ClientOrderId(deterministic_client_order_id(self.config.intent_id, "entry")),
            )
            self.entry_submitted = True
            self.submit_order(entry)
            return
        if self.opened_at_ms is None or self.exit_submitted:
            return
        if self.stop_trigger_price is not None and bar.low.as_decimal() <= self.stop_trigger_price:
            self._close("stop")
        elif max_holding_due(
            opened_at_ms=self.opened_at_ms,
            max_holding_ms=self.config.max_holding_ms,
            now_ms=at_ms,
        ):
            self._close("max_holding")

    def on_order_filled(self, event: Any) -> None:
        self.fees += event.commission.as_decimal()
        if event.order_side == OrderSide.BUY and self.entry_price is None:
            self.entry_price = event.last_px.as_decimal()
            self.opened_at_ms = int(event.ts_event) // 1_000_000
            self.position_id = event.position_id
            self.stop_trigger_price = stop_price(
                entry_price=self.entry_price,
                stop_loss_bps=self.config.stop_loss_bps,
                price_increment=self.config.price_increment,
            )
        elif event.order_side == OrderSide.SELL:
            self.exit_price = event.last_px.as_decimal()
            self.closed_at_ms = int(event.ts_event) // 1_000_000

    def on_stop(self) -> None:
        self.unsubscribe_bars(self.config.bar_type)

    def _close(self, reason: str) -> None:
        if self.position_id is None or self.instrument is None:
            return
        close = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.SELL,
            quantity=self.instrument.make_qty(self.config.quantity),
            reduce_only=True,
            client_order_id=ClientOrderId(deterministic_client_order_id(self.config.intent_id, "close")),
        )
        self.exit_submitted = True
        self.exit_reason = reason
        self.submit_order(close, position_id=self.position_id)


def run_bar_episode(
    *,
    intent: ReplayExecutionIntentV1,
    capability: ReplayScenarioCapabilityV1,
    bars: list[ReplayBarV1],
    reference_price: Decimal,
    target_notional: Decimal = TARGET_NOTIONAL_CEILING_USD,
) -> BarEpisodeResult:
    """Run the intent through a fresh v1.231.0 engine and return one terminal answer."""

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(f"REPLAY-{intent.replay_intent_id[:12]}"),
            logging=LoggingConfig(bypass_logging=True),
        )
    )
    try:
        ordered = sorted(bars, key=lambda bar: (bar.close_at_ms, bar.open_at_ms))
        policy_bar = next(
            (bar for bar in ordered if bar.open_at_ms <= intent.ts_init < bar.close_at_ms),
            None,
        )
        if policy_bar is None:
            return BarEpisodeResult("MISSING_MARKET_DATA", "outside_bar_coverage")
        entry_policy = evaluate_entry(
            now_ms=intent.ts_init,
            created_at_ms=intent.ts_init,
            valid_until_ms=intent.ts_init + ENTRY_TTL_MS,
            quote_at_ms=intent.ts_init,
            bid=policy_bar.open,
            ask=policy_bar.open,
            reference_price=reference_price,
            target_notional=target_notional,
            size_increment=capability.size_increment,
            min_quantity=capability.min_quantity,
            min_notional=capability.min_notional,
            max_spread_bps=MAX_SPREAD_BPS,
            max_drift_bps=MAX_ENTRY_DRIFT_BPS,
        )
        if entry_policy.quantity is None:
            return BarEpisodeResult("REJECTED", entry_policy.reason)

        instrument_id = InstrumentId.from_str(capability.instrument_id)
        instrument = _instrument(capability, instrument_id)
        bar_type = BarType.from_str(f"{instrument_id.value}-5-MINUTE-LAST-EXTERNAL")
        strategy = _EpisodeStrategy(
            _EpisodeStrategyConfig(
                strategy_id=f"REPLAY-{intent.replay_intent_id[:12]}",
                instrument_id=instrument_id,
                bar_type=bar_type,
                intent_id=intent.replay_intent_id,
                quantity=entry_policy.quantity,
                entry_after_ms=intent.ts_init,
                stop_loss_bps=STOP_LOSS_BPS,
                max_holding_ms=MAX_HOLDING_MS,
                price_increment=capability.price_increment,
            )
        )
        engine.add_venue(
            venue=instrument_id.venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(1_000_000, instrument.quote_currency)],
            base_currency=None,
            default_leverage=Decimal(1),
        )
        engine.add_instrument(instrument)
        engine.add_data([_bar(bar_type, bar, capability) for bar in ordered])
        engine.add_strategy(strategy)
        engine.run()
        if strategy.entry_price is None:
            return BarEpisodeResult("MISSING_MARKET_DATA", "outside_bar_coverage")
        if strategy.exit_price is None:
            return BarEpisodeResult(
                "MISSING_MARKET_DATA",
                "outside_bar_coverage",
                entry_price=strategy.entry_price,
                quantity=entry_policy.quantity,
                fees=strategy.fees,
            )
        gross = (strategy.exit_price - strategy.entry_price) * entry_policy.quantity
        mfe_bps, mae_bps = _excursions(
            ordered,
            entry_price=strategy.entry_price,
            opened_at_ms=strategy.opened_at_ms,
            closed_at_ms=strategy.closed_at_ms,
        )
        return BarEpisodeResult(
            "CLOSED",
            strategy.exit_reason or "closed",
            entry_price=strategy.entry_price,
            exit_price=strategy.exit_price,
            quantity=entry_policy.quantity,
            gross_result=gross,
            fees=strategy.fees,
            net_excluding_funding=gross - strategy.fees,
            mfe_bps=mfe_bps,
            mae_bps=mae_bps,
        )
    finally:
        engine.dispose()


def _instrument(capability: ReplayScenarioCapabilityV1, instrument_id: InstrumentId) -> CryptoPerpetual:
    base = Currency.from_str(capability.base_currency)
    quote = Currency.from_str(capability.quote_currency)
    return CryptoPerpetual(
        instrument_id=instrument_id,
        raw_symbol=Symbol(capability.native_symbol),
        base_currency=base,
        quote_currency=quote,
        settlement_currency=quote,
        is_inverse=False,
        price_precision=capability.price_precision,
        size_precision=capability.size_precision,
        price_increment=Price.from_str(str(capability.price_increment)),
        size_increment=Quantity.from_str(str(capability.size_increment)),
        min_quantity=(None if capability.min_quantity is None else Quantity.from_str(str(capability.min_quantity))),
        min_notional=None if capability.min_notional is None else Money(capability.min_notional, quote),
        margin_init=Decimal(1),
        margin_maint=Decimal(1),
        maker_fee=Decimal("0.0005"),
        taker_fee=Decimal("0.0005"),
        ts_event=0,
        ts_init=0,
    )


def _bar(bar_type: BarType, value: ReplayBarV1, capability: ReplayScenarioCapabilityV1) -> Bar:
    def price(number: Decimal) -> Price:
        return Price.from_str(f"{number:.{capability.price_precision}f}")

    return Bar(
        bar_type=bar_type,
        open=price(value.open),
        high=price(value.high),
        low=price(value.low),
        close=price(value.close),
        volume=Quantity.from_str(f"{value.volume:.{capability.size_precision}f}"),
        ts_event=value.close_at_ms * 1_000_000,
        ts_init=value.close_at_ms * 1_000_000,
    )


def _excursions(
    bars: list[ReplayBarV1],
    *,
    entry_price: Decimal,
    opened_at_ms: int | None,
    closed_at_ms: int | None,
) -> tuple[int | None, int | None]:
    if opened_at_ms is None or closed_at_ms is None:
        return None, None
    covered = [bar for bar in bars if opened_at_ms <= bar.close_at_ms <= closed_at_ms]
    if not covered:
        return None, None
    mfe = (max(bar.high for bar in covered) / entry_price - 1) * 10_000
    mae = (min(bar.low for bar in covered) / entry_price - 1) * 10_000
    return (
        int(mfe.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)),
        int(mae.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)),
    )


__all__ = ["BarEpisodeResult", "run_bar_episode"]
