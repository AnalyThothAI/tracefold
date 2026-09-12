"""Concrete Signal/manual-entry admission, sizing, submit, and query-first owner."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientId

from tracefold.trading import TradePlan, TradeSignalV1

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
)
from .trade_plans import TradePlanChannel

_AMBIGUOUS_QUERY_AFTER_NS = 5_000_000_000
# Sizing pads the executable side of the book by this much before it divides the risk budget by a
# price, so a market order that fills through a thin top of book is still inside the frozen risk. It
# is not a drift *gate* and never was one — nothing here compares a fill to it (#537 PR-3).
_ENTRY_PRICE_PAD_BPS = Decimal(25)
_MAX_SPREAD_BPS = Decimal(30)


class EntryCoordinator:
    """Own the complete increase-exposure path for Signal and manual requests."""

    def __init__(
        self,
        *,
        engine: Any,
        profile: OiRuntimeProfile,
        plans: TradePlanChannel,
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
        self._plans = plans
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
            namespace=self._profile.namespace,
            entry_id=request.entry_id,
            leg="entry",
        )
        existing = self._engine.cache.order(client_order_id)
        if existing is not None:
            # This entry identity has already claimed its one deterministic client order id, so no
            # second economic order can come of this request. Rebuilding what became of that order --
            # the position it opened, the stop resting on it, the exit generation -- belongs to
            # `RecoveryCoordinator.reconcile`, which does it from the durable order and position
            # facts against a Binance report of the same instant. There was a second rebuild here
            # that worked from Cache alone and could reach a different answer about the same
            # execution, including flattening a position recovery had just reclaimed (#537 PR-4).
            self._engine.query_order(existing, client_id=ClientId("BINANCE"))
            self._observations.dispose_entry(request, "replayed_query_first")
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
        admission = self._admitted_quantity(
            request=request,
            route=route,
            instrument=instrument,
            quote=quote,
            now_ns=now_ns,
        )
        if isinstance(admission, str):
            self._observations.dispose_entry(request, admission)
            return
        quantity, allowed_risk = admission
        plan = TradePlan(
            entry_id=request.entry_id,
            source=request.source,
            case_id=None if request.signal is None else request.signal.case_id,
            account_slot=self._profile.account_slot,
            runtime_mode_at_creation=self._profile.mode,
            market_key=request.market_key,
            instrument_id=route.instrument_id.value,
            direction=request.direction,
            entry_client_order_id=client_order_id.value,
            created_at_ns=now_ns,
            entry_expires_at_ns=request.expires_at_ns,
            entry_quantity=quantity.as_decimal(),
            stop_distance_bps=route.stop_distance_bps,
            risk_budget_usd=allowed_risk,
            max_leverage_at_creation=self._profile.risk.max_leverage,
            exit_policy_id=self._profile.exit_policy.policy_id,
            take_profit_bps=self._profile.exit_policy.take_profit_bps,
            max_holding_ns=self._profile.exit_policy.max_holding_ns,
            updated_at_ns=now_ns,
        )
        if not self._plans.prepare(plan):
            self._observations.dispose_entry(request, "trade_plan_busy")
            return
        self._state.executions[request.entry_id] = ExecutionState(
            entry=request,
            plan=plan,
            route=route,
            entry_client_order_id=client_order_id,
            submitted_at_ns=now_ns,
            disposition_reason="prepare_pending",
        )

    def submit_committed(self) -> None:
        """Consume a commit receipt on the callback thread. No receipt means no entry order."""
        receipt = self._plans.take_receipt()
        if receipt is None:
            return
        plan = receipt.plan
        execution = self._state.executions.get(plan.entry_id)
        if execution is None or not receipt.newly_committed:
            if execution is not None:
                execution.plan = plan
                execution.active = plan.terminal_at_ns is None
                execution.native_pnl_complete = False
            self._request_reconciliation("unknown_outcome")
            return
        now_ns = int(self._engine.clock.timestamp_ns())
        if execution.plan.terminal_at_ns is not None:
            return
        execution.plan = plan
        instrument = self._engine.cache.instrument(execution.route.instrument_id)
        quote = self._engine.cache.quote_tick(execution.route.instrument_id)
        refusal = None
        if plan.entry_expires_at_ns <= now_ns:
            refusal = "expired"
        elif not self._verify_owned_exposure() or not self._readiness_snapshot().entries_armed:
            refusal = "entry_disarmed"
        elif instrument is None or quote is None:
            refusal = "instrument_or_market_missing"
        else:
            admission = self._admitted_quantity(
                request=execution.entry,
                route=execution.route,
                instrument=instrument,
                quote=quote,
                now_ns=now_ns,
                frozen_budget=plan.risk_budget_usd,
            )
            if isinstance(admission, str):
                refusal = admission
            elif admission[0].as_decimal() < plan.entry_quantity:
                refusal = "trade_plan_risk_changed"
        if refusal is not None:
            execution.plan = plan.model_copy(
                update={
                    "status": "closed",
                    "terminal_at_ns": now_ns,
                    "exit_reason": "not_submitted",
                    "updated_at_ns": max(now_ns, plan.updated_at_ns),
                }
            )
            execution.active = False
            self._plans.offer_update(execution.plan)
            self._observations.dispose_entry(execution.entry, refusal)
            self._quotes.release(execution.route.instrument_id)
            return
        order = self._engine.order_factory.market(
            instrument_id=execution.route.instrument_id,
            order_side=OrderSide.BUY if plan.direction == "long" else OrderSide.SELL,
            quantity=instrument.make_qty(plan.entry_quantity),
            reduce_only=False,
            client_order_id=execution.entry_client_order_id,
        )
        execution.entry_order = order
        execution.entry_query_pending = True
        execution.submitted_at_ns = now_ns
        execution.disposition_reason = "accepted"
        execution.plan = plan.model_copy(
            update={"status": "entry_working", "updated_at_ns": max(now_ns, plan.updated_at_ns)}
        )
        self._plans.offer_update(execution.plan)
        self._state.orders[execution.entry_client_order_id] = (plan.entry_id, "entry")
        try:
            self._engine.submit_order(order, client_id=ClientId("BINANCE"))
        except Exception:
            execution.disposition_reason = "unknown_query_first"
            self._request_reconciliation("unknown_outcome")
            self._engine.query_order(order, client_id=ClientId("BINANCE"))
            self._observations.order(execution, order, "entry", "unknown_query_first")
            self._observations.dispose_entry(execution.entry, "unknown_query_first")
            return
        self._observations.order(execution, order, "entry", "submitted")
        self._observations.dispose_entry(execution.entry, "accepted")

    def _admitted_quantity(
        self,
        *,
        request: RuntimeEntryRequest,
        route: OiInstrumentRoute,
        instrument: Any,
        quote: Any,
        now_ns: int,
        frozen_budget: Decimal | None = None,
    ) -> tuple[Any, Decimal] | str:
        account_clock, reconciliation_clock = self._readiness.facts_clock()
        try:
            facts = NautilusRiskFacts.collect(
                cache=self._engine.cache,
                portfolio=self._engine.portfolio,
                account_id=self._profile.account_id,
                strategy_id=self._engine.id,
                routes={
                    **self._stop_bps,
                    **{
                        value.route.instrument_id: value.plan.stop_distance_bps
                        for value in self._state.executions.values()
                        if value.active
                    },
                },
                candidate_instrument_id=route.instrument_id,
                owned_order_ids=frozenset(self._state.orders),
                owned_position_ids=frozenset(self._state.positions),
                account_observed_at_ns=account_clock,
                reconciliation_observed_at_ns=reconciliation_clock,
            )
        except RuntimeError as exc:
            return str(exc)
        if facts.unexpected_exposure:
            self._readiness.halt_for_unexpected_exposure()
        # A missing day-start baseline is recorded from this equity and the entry continues; it used
        # to redeliver the Signal until a background write landed (#520 PR-B). An equity that cannot
        # be a baseline at all - non-positive, or beyond the observation's precision - is a terminal
        # refusal here rather than an exception on the callback thread.
        try:
            day_start = self._day_start_baseline(equity_usd=facts.equity_usd, now_ns=now_ns)
        except ValueError as exc:
            return str(exc)
        requested_risk = min(
            facts.equity_usd * self._profile.risk.risk_fraction_per_trade,
            self._profile.risk.max_risk_per_trade_usd,
        )
        if frozen_budget is not None:
            requested_risk = min(requested_risk, frozen_budget)
        decision = self._policy.evaluate_entry(
            facts=facts,
            baseline=day_start,
            now_ns=now_ns,
            requested_risk_usd=requested_risk,
            candidate_is_new_position=True,
        )
        if decision.action == "refuse":
            return decision.reason
        quantity = self._sized_quantity(
            request=request,
            route=route,
            instrument=instrument,
            quote=quote,
            facts=facts,
            allowed_risk_usd=decision.allowed_risk_usd,
        )
        if isinstance(quantity, str):
            return quantity
        return quantity, decision.allowed_risk_usd

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
        price = executable_price * (Decimal(1) + _ENTRY_PRICE_PAD_BPS / Decimal(10_000))
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
        # `fixed_risk_quantity` divides the risk budget by the padded price, clamps the result to the
        # leverage headroom and floors it to `size_increment`, so re-checking either ceiling here could
        # only ever pass: rounding *down* cannot cross a ceiling. What remains are the venue's own
        # minimums, which sizing does not know (#537 PR-3).
        quantity = instrument.make_qty(raw_quantity)
        if quantity.as_decimal() <= 0:
            return "quantity_below_increment"
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
