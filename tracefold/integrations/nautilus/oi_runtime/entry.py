"""Concrete Signal/manual-entry admission, sizing, submit, and query-first owner."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import ClientId

from tracefold.trading import TradeSignalV1

from .config import OiInstrumentRoute, OiRuntimeProfile
from .observations import RuntimeObservationWriter
from .quotes import QuoteStreamCoordinator
from .risk import DayStartBaseline, NautilusRiskFacts, OiFuturesRiskPolicy, fixed_risk_quantity
from .state import (
    QUOTE_WARMUP_NS,
    ExecutionState,
    PrivateReconciliationReason,
    RuntimeEntryRequest,
    RuntimeExecutionState,
    RuntimeReadiness,
    RuntimeReadinessSnapshot,
    deterministic_client_order_id,
    entry_order_valid,
)

_AMBIGUOUS_QUERY_AFTER_NS = 5_000_000_000
_MAX_ENTRY_DRIFT_BPS = Decimal(25)
_MAX_SPREAD_BPS = Decimal(30)


class EntryCoordinator:
    """Own the complete increase-exposure path for Signal and manual requests."""

    def __init__(
        self,
        *,
        engine: Any,
        profile: OiRuntimeProfile,
        state: RuntimeExecutionState,
        readiness: RuntimeReadiness,
        observations: RuntimeObservationWriter,
        quotes: QuoteStreamCoordinator,
        day_start_baseline: Callable[..., DayStartBaseline],
        readiness_snapshot: Callable[[], RuntimeReadinessSnapshot],
        verify_owned_exposure: Callable[[], bool],
        request_reconciliation: Callable[[PrivateReconciliationReason], None],
    ) -> None:
        self._engine = engine
        self._profile = profile
        self._state = state
        self._readiness = readiness
        self._observations = observations
        self._quotes = quotes
        self._day_start_baseline = day_start_baseline
        self._readiness_snapshot = readiness_snapshot
        self._verify_owned_exposure = verify_owned_exposure
        self._request_reconciliation = request_reconciliation
        self._routes = {route.market_key: route for route in profile.routes}
        self._stop_bps = {route.instrument_id: route.stop_distance_bps for route in profile.routes}
        self._policy = OiFuturesRiskPolicy(profile.risk)

    def handle_signal(self, signal: TradeSignalV1) -> None:
        self.handle(RuntimeEntryRequest.from_signal(signal))

    def handle(self, request: RuntimeEntryRequest) -> None:
        now_ns = int(self._engine.clock.timestamp_ns())
        if request.signal is not None and request.entry_id in self._state.disposed_signal_ids:
            return
        existing_state = self._state.executions.get(request.entry_id)
        if existing_state is not None:
            self._observations.dispose_entry(request, existing_state.disposition_reason)
            return
        route = self._routes.get(request.market_key)
        if route is None:
            self._observations.dispose_entry(request, "instrument_unmapped")
            return
        if any(
            state.active and state.route.instrument_id == route.instrument_id
            for state in self._state.executions.values()
        ):
            self._observations.dispose_entry(request, "instrument_busy")
            return
        client_order_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            entry_id=request.entry_id,
            leg="entry",
        )
        existing = self._engine.cache.order(client_order_id)
        if existing is not None:
            self._replay_cached(request=request, route=route, order=existing, now_ns=now_ns)
            return
        if request.expires_at_ns <= now_ns:
            self._observations.dispose_entry(request, "expired")
            return
        exposure_ready = self._verify_owned_exposure()
        ready = self._readiness_snapshot()
        if not exposure_ready or not ready.entries_armed:
            self._observations.dispose_entry(
                request,
                (ready.entry_block_reason or "entry_blocked") if not ready.entries_armed else "protection_unproven",
            )
            return
        # Admission passed, so this instrument is now worth a market-data stream. The first tick can
        # be up to a round trip away; the wait is spent as redeliveries of an unresolved Signal, never
        # as a blocked event loop, and it is bounded well inside the Signal's TTL (#510 E).
        subscribed_at_ns = self._quotes.ensure(route.instrument_id, now_ns)
        instrument = self._engine.cache.instrument(route.instrument_id)
        quote = self._engine.cache.quote_tick(route.instrument_id)
        if instrument is None or quote is None:
            warming_up = quote is None and instrument is not None and now_ns - subscribed_at_ns <= QUOTE_WARMUP_NS
            self._observations.dispose_entry(
                request,
                "market_subscription_pending" if warming_up else "instrument_or_market_missing",
            )
            return
        account_clock, reconciliation_clock = self._readiness.facts_clock()
        try:
            facts = NautilusRiskFacts.collect(
                cache=self._engine.cache,
                portfolio=self._engine.portfolio,
                account_id=self._profile.account_id,
                strategy_id=self._engine.id,
                routes=self._stop_bps,
                candidate_instrument_id=route.instrument_id,
                owned_order_ids=frozenset(self._state.orders),
                owned_position_ids=frozenset(self._state.positions),
                account_observed_at_ns=account_clock,
                reconciliation_observed_at_ns=reconciliation_clock,
            )
        except RuntimeError as exc:
            self._observations.dispose_entry(request, str(exc))
            return
        if facts.unexpected_exposure:
            self._readiness.halt_for_unexpected_exposure()
        # A missing day-start baseline is recorded from this equity and the entry continues; it used
        # to redeliver the Signal until a background write landed (#520 PR-B). An equity that cannot
        # be a baseline at all - non-positive, or beyond the observation's precision - is a terminal
        # refusal here rather than an exception on the callback thread.
        try:
            day_start = self._day_start_baseline(equity_usd=facts.equity_usd, now_ns=now_ns)
        except ValueError as exc:
            self._observations.dispose_entry(request, str(exc))
            return
        requested_risk = min(
            facts.equity_usd * self._profile.risk.risk_fraction_per_trade,
            self._profile.risk.max_risk_per_trade_usd,
        )
        decision = self._policy.evaluate_entry(
            facts=facts,
            baseline=day_start,
            now_ns=now_ns,
            requested_risk_usd=requested_risk,
            requested_leverage=self._profile.risk.max_leverage,
            candidate_is_new_position=True,
        )
        if decision.action in {"deny", "halt"}:
            self._observations.dispose_entry(request, decision.reason)
            return
        quantity = self._sized_quantity(
            request=request,
            route=route,
            instrument=instrument,
            quote=quote,
            facts=facts,
            allowed_risk_usd=decision.allowed_risk_usd,
        )
        if isinstance(quantity, str):
            self._observations.dispose_entry(request, quantity)
            return
        side = OrderSide.BUY if request.direction == "long" else OrderSide.SELL
        order = self._engine.order_factory.market(
            instrument_id=route.instrument_id,
            order_side=side,
            quantity=quantity,
            reduce_only=False,
            client_order_id=client_order_id,
        )
        self._state.orders[client_order_id] = (request.entry_id, "entry")
        execution = ExecutionState(
            entry=request,
            route=route,
            entry_client_order_id=client_order_id,
            entry_order=order,
            submitted_at_ns=now_ns,
            disposition_reason="accepted",
            entry_query_pending=True,
        )
        self._state.executions[request.entry_id] = execution
        try:
            self._engine.submit_order(order, client_id=ClientId("BINANCE"))
        except Exception:
            execution.disposition_reason = "unknown_query_first"
            self._request_reconciliation("unknown_outcome")
            self._engine.query_order(order, client_id=ClientId("BINANCE"))
            self._observations.order(execution, order, "entry", "unknown_query_first")
            self._observations.dispose_entry(request, "unknown_query_first")
            return
        self._observations.order(execution, order, "entry", "submitted")
        self._observations.dispose_entry(request, "accepted")

    def _replay_cached(
        self,
        *,
        request: RuntimeEntryRequest,
        route: OiInstrumentRoute,
        order: Any,
        now_ns: int,
    ) -> None:
        if not entry_order_valid(
            profile=self._profile,
            strategy_id=self._engine.id,
            request=request,
            route=route,
            order=order,
        ):
            self._readiness.halt_for_unexpected_exposure()
            self._request_reconciliation("unexpected_exposure")
            self._observations.dispose_entry(request, "cached_entry_invalid")
            return
        position = self._engine.cache.position_for_order(order.client_order_id)
        expected_side = PositionSide.LONG if request.direction == "long" else PositionSide.SHORT
        if (
            position is not None
            and position.is_open
            and (
                position.account_id != self._profile.account_id
                or position.strategy_id != self._engine.id
                or position.instrument_id != route.instrument_id
                or position.side != expected_side
            )
        ):
            self._readiness.halt_for_unexpected_exposure()
            self._request_reconciliation("unexpected_exposure")
            self._observations.dispose_entry(request, "cached_position_invalid")
            return
        self._state.orders[order.client_order_id] = (request.entry_id, "entry")
        execution = ExecutionState(
            entry=request,
            route=route,
            entry_client_order_id=order.client_order_id,
            entry_order=order,
            submitted_at_ns=now_ns,
            disposition_reason="replayed_query_first",
            active=bool(position is not None and position.is_open) or not order.is_closed,
            entry_query_pending=bool(order.is_inflight or order.is_active_local),
        )
        self._state.executions[request.entry_id] = execution
        if position is not None and position.is_open:
            execution.position_id = position.id
            execution.position_quantity = abs(Decimal(str(position.quantity)))
            execution.avg_entry_price = Decimal(str(position.avg_px_open))
            self._state.positions[position.id] = request.entry_id
            self._readiness.halt_for_unexpected_exposure()
            self._request_reconciliation("unexpected_exposure")
            self._engine.flatten_position(position.id)
        self._engine.query_order(order, client_id=ClientId("BINANCE"))
        self._observations.dispose_entry(request, "replayed_query_first")

    def _sized_quantity(
        self,
        *,
        request: RuntimeEntryRequest,
        route: OiInstrumentRoute,
        instrument: Any,
        quote: Any,
        facts: NautilusRiskFacts,
        allowed_risk_usd: Decimal,
    ) -> Any | str:
        bid = Decimal(str(quote.bid_price))
        ask = Decimal(str(quote.ask_price))
        midpoint = (bid + ask) / Decimal(2)
        spread_bps = (ask - bid) * Decimal(10_000) / midpoint
        if spread_bps > _MAX_SPREAD_BPS:
            return "spread_limit"
        executable_price = ask if request.direction == "long" else bid
        price = executable_price * (Decimal(1) + _MAX_ENTRY_DRIFT_BPS / Decimal(10_000))
        existing_notional = (
            facts.gross_position_notional_usd + facts.open_order_notional_usd + facts.inflight_order_notional_usd
        )
        try:
            raw_quantity = fixed_risk_quantity(
                price=price,
                stop_distance_bps=route.stop_distance_bps,
                allowed_risk_usd=allowed_risk_usd,
                equity_usd=facts.equity_usd,
                max_leverage=self._profile.risk.max_leverage,
                existing_notional_usd=existing_notional,
                size_increment=instrument.size_increment.as_decimal(),
            )
        except ValueError as exc:
            return str(exc)
        quantity = instrument.make_qty(raw_quantity)
        if quantity.as_decimal() <= 0:
            return "quantity_below_increment"
        quantity_notional = quantity.as_decimal() * price
        stop_fraction = Decimal(route.stop_distance_bps) / Decimal(10_000)
        if quantity_notional * stop_fraction > allowed_risk_usd:
            return "quantity_exceeds_risk_after_rounding"
        if existing_notional + quantity_notional > facts.equity_usd * self._profile.risk.max_leverage:
            return "quantity_exceeds_leverage_after_rounding"
        if instrument.min_quantity is not None and quantity < instrument.min_quantity:
            return "quantity_below_minimum"
        if instrument.min_notional is not None and quantity.as_decimal() * price < instrument.min_notional.as_decimal():
            return "notional_below_minimum"
        return quantity

    def query_aged(self) -> None:
        now_ns = int(self._engine.clock.timestamp_ns())
        for state in self._state.executions.values():
            if not state.entry_query_pending or state.entry_order is None or state.entry_order.is_closed:
                state.entry_query_pending = False
                continue
            if now_ns - state.submitted_at_ns < _AMBIGUOUS_QUERY_AFTER_NS:
                continue
            self._engine.query_order(state.entry_order, client_id=ClientId("BINANCE"))
            state.submitted_at_ns = now_ns

    def known_terminal(self, state: ExecutionState) -> None:
        state.entry_query_pending = False
        if state.position_quantity <= 0:
            state.active = False
            self._quotes.release(state.route.instrument_id)

    @staticmethod
    def accepted(state: ExecutionState) -> None:
        state.entry_query_pending = False

    def mark_unknown(self, state: ExecutionState) -> None:
        state.entry_query_pending = True
        state.submitted_at_ns = int(self._engine.clock.timestamp_ns())


__all__ = ["EntryCoordinator"]
