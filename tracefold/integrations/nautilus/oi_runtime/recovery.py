"""Concrete startup/private reconciliation and ownership reconstruction owner."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, InstrumentId, PositionId

from .config import OiInstrumentRoute, OiRuntimeProfile
from .exit import ExitCoordinator
from .protection import ProtectionCoordinator
from .quotes import QuoteStreamCoordinator
from .state import (
    ExecutionState,
    PrivateReconciliationReason,
    RuntimeEntryRequest,
    RuntimeExecutionState,
    RuntimeReadiness,
    RuntimeReconciliationSnapshot,
    deterministic_client_order_id,
    entry_order_valid,
    exit_leg,
    exit_order_valid,
    protection_leg,
    unowned_cache_exposure,
)
from .trade_plans import TradePlanChannel


class RecoveryCoordinator:
    """Own recovery validation, ambiguity handling, and exposure proof."""

    def __init__(
        self,
        *,
        engine: Any,
        profile: OiRuntimeProfile,
        plans: TradePlanChannel,
        state: RuntimeExecutionState,
        readiness: RuntimeReadiness,
        protection: ProtectionCoordinator,
        exits: ExitCoordinator,
        quotes: QuoteStreamCoordinator,
        request_reconciliation: Callable[[PrivateReconciliationReason], None],
    ) -> None:
        self._engine = engine
        self._profile = profile
        self._plans = plans
        self._state = state
        self._readiness = readiness
        self._protection = protection
        self._exits = exits
        self._quotes = quotes
        self._request_reconciliation = request_reconciliation

    def reconcile(self, snapshot: RuntimeReconciliationSnapshot) -> bool:
        """Rebuild runtime ownership only from durable identities and current Cache state."""

        if snapshot.account_slot != self._profile.account_slot:
            self._readiness.halt_for_unexpected_exposure()
            return False
        executions: dict[str, ExecutionState] = {}
        orders: dict[ClientOrderId, tuple[str, str]] = {}
        positions: dict[PositionId, str] = {}
        previous = self._state.executions
        for seed in snapshot.executions:
            request = seed.entry
            plan = seed.plan
            local = previous.get(plan.entry_id)
            if local is not None and local.plan.updated_at_ns >= plan.updated_at_ns:
                plan = local.plan
            route = OiInstrumentRoute(
                plan.market_key, InstrumentId.from_str(plan.instrument_id), plan.stop_distance_bps
            )
            expected_entry = deterministic_client_order_id(
                namespace=self._profile.namespace,
                entry_id=request.entry_id,
                leg="entry",
            )
            # The entry market order is absent from a cold Cache; the seed's position match is then
            # the only ownership proof, and there is nothing left to shape-check.
            entry_order = self._engine.cache.order(seed.entry_client_order_id)
            if (
                plan.account_slot != self._profile.account_slot
                or plan.runtime_mode_at_creation != self._profile.mode
                or plan.terminal_at_ns is not None
                or request.entry_id in executions
                or seed.entry_client_order_id != expected_entry
                or (entry_order is None and seed.position_id is None)
                or (entry_order is not None and entry_order.quantity.as_decimal() != plan.entry_quantity)
                or (
                    entry_order is not None
                    and not entry_order_valid(
                        profile=self._profile,
                        strategy_id=self._engine.id,
                        request=request,
                        route=route,
                        order=entry_order,
                    )
                )
            ):
                self._readiness.halt_for_unexpected_exposure()
                return False
            state = ExecutionState(
                entry=request,
                plan=plan,
                route=route,
                entry_client_order_id=seed.entry_client_order_id,
                entry_order=entry_order,
                submitted_at_ns=snapshot.reconciliation_observed_at_ns,
                disposition_reason="recovered",
                entry_query_pending=bool(
                    entry_order is not None and (entry_order.is_inflight or entry_order.is_active_local)
                ),
            )
            executions[request.entry_id] = state
            if entry_order is not None:
                orders[seed.entry_client_order_id] = (request.entry_id, "entry")
            if seed.position_id is None:
                if seed.protections or seed.exit_client_order_id is not None:
                    self._readiness.halt_for_unexpected_exposure()
                    return False
                state.active = entry_order is not None and not entry_order.is_closed
                state.native_pnl_complete = False
                if state.active and state.plan.status == "prepared":
                    state.plan = plan.model_copy(
                        update={
                            "status": "entry_working",
                            "updated_at_ns": max(plan.updated_at_ns, snapshot.reconciliation_observed_at_ns),
                        }
                    )
                    self._plans.offer_update(state.plan)
                continue
            position = self._engine.cache.position(seed.position_id)
            expected_side = PositionSide.LONG if request.direction == "long" else PositionSide.SHORT
            if (
                position is None
                or not position.is_open
                or position.account_id != self._profile.account_id
                or position.strategy_id != self._engine.id
                or position.instrument_id != route.instrument_id
                or position.side != expected_side
                or abs(Decimal(str(position.quantity))) > plan.entry_quantity
            ):
                self._readiness.halt_for_unexpected_exposure()
                return False
            state.native_pnl_complete = bool(
                local is not None
                and local.native_pnl_complete
                and local.position_id == seed.position_id
                and local.avg_entry_price is not None
            )
            state.exit_reason = plan.exit_reason
            state.position_id = seed.position_id
            state.position_quantity = abs(Decimal(str(position.quantity)))
            state.avg_entry_price = Decimal(str(position.avg_px_open))
            state.desired_stop = (state.position_quantity, state.avg_entry_price)
            positions[seed.position_id] = request.entry_id
            opened_at_ns = plan.opened_at_ns or plan.created_at_ns
            state.plan = plan.model_copy(
                update={
                    "status": "closing" if plan.exit_reason else "open",
                    "opened_at_ns": opened_at_ns,
                }
            )
            if not state.native_pnl_complete and state.plan.history_gap_reason is None:
                state.plan = state.plan.model_copy(
                    update={"history_gap_reason": "native_pnl_basis_incomplete_after_restart"}
                )
            if state.plan != plan:
                state.plan = state.plan.model_copy(
                    update={"updated_at_ns": max(plan.updated_at_ns, snapshot.reconciliation_observed_at_ns)}
                )
                self._plans.offer_update(state.plan)
            if not self._restore_protections(
                state=state,
                seed_protections=seed.protections,
                executions=executions,
                orders=orders,
                positions=positions,
            ):
                return False
            state.exit_generation = seed.exit_generation
            state.exit_retry_required = plan.exit_reason is not None
            if seed.exit_client_order_id is not None:
                expected_exit = deterministic_client_order_id(
                    namespace=self._profile.namespace,
                    entry_id=request.entry_id,
                    leg=exit_leg(seed.exit_generation),
                )
                exit_order = self._engine.cache.order(seed.exit_client_order_id)
                if (
                    seed.exit_generation < 0
                    or seed.exit_client_order_id != expected_exit
                    or not exit_order_valid(
                        profile=self._profile,
                        strategy_id=self._engine.id,
                        cache=self._engine.cache,
                        state=state,
                        exit_order=exit_order,
                        position=position,
                    )
                ):
                    self._readiness.halt_for_unexpected_exposure()
                    return False
                if exit_order.is_closed:
                    state.exit_generation += 1
                    state.exit_retry_budget -= 1
                    state.exit_retry_required = True
                else:
                    state.exit_order = exit_order
                    orders[seed.exit_client_order_id] = (request.entry_id, "exit")
        for identity, prior in previous.items():
            if identity not in executions and prior.plan.status == "prepared" and prior.entry_order is None:
                executions[identity] = prior
        retired = self._retire_empty_plans(snapshot, previous)
        for identity in retired:
            executions.pop(identity, None)
        for candidate in snapshot.unresolved_plans:
            local = previous.get(candidate.entry_id)
            plan = (
                local.plan if local is not None and local.plan.updated_at_ns >= candidate.updated_at_ns else candidate
            )
            if plan.terminal_at_ns is None and plan.entry_id not in executions and plan.entry_id not in retired:
                executions[plan.entry_id] = ExecutionState(
                    entry=RuntimeEntryRequest.from_plan(plan),
                    plan=plan,
                    route=OiInstrumentRoute(
                        plan.market_key, InstrumentId.from_str(plan.instrument_id), plan.stop_distance_bps
                    ),
                    entry_client_order_id=ClientOrderId(plan.entry_client_order_id),
                    submitted_at_ns=plan.created_at_ns,
                    disposition_reason="recovered_unresolved",
                    native_pnl_complete=False,
                )
        owned = not snapshot.ownership_ambiguous and not any(self._unowned_exposure(orders=orders, positions=positions))
        self._commit(
            executions=executions,
            orders=orders,
            positions=positions,
            observed_at_ns=snapshot.reconciliation_observed_at_ns,
        )
        self._resume_ambiguous_actions(executions)
        # Commit the rebuilt ownership even when exposure is left over: the operator has to see
        # which instrument, side and quantity is unclaimed before `/flatten account` can act, and
        # the reconciliation clock is exactly as fresh as the Binance proof that produced it.
        self._readiness.reconciled(
            account_observed_at_ns=snapshot.account_observed_at_ns,
            reconciliation_observed_at_ns=snapshot.reconciliation_observed_at_ns,
        )
        if not owned:
            self._readiness.halt_for_unexpected_exposure(
                "ownership_ambiguous" if snapshot.ownership_ambiguous else "unexpected_exposure"
            )
            return False
        self._state.unexpected_exposure_reconciliation_requested = False
        self._exits.complete_from_reconciliation(snapshot)
        return True

    def _retire_empty_plans(
        self,
        snapshot: RuntimeReconciliationSnapshot,
        previous: dict[str, ExecutionState],
    ) -> set[str]:
        retired: set[str] = set()
        if snapshot.ownership_ambiguous:
            return retired
        queries = {query.entry_id: query for query in snapshot.entry_queries}
        for candidate in snapshot.unresolved_plans:
            local = previous.get(candidate.entry_id)
            plan = (
                local.plan if local is not None and local.plan.updated_at_ns >= candidate.updated_at_ns else candidate
            )
            instrument_id = InstrumentId.from_str(plan.instrument_id)
            # Absence must cover the instrument's positions AND regular/Algo orders. An unrelated
            # exposure is never evidence that this plan closed, even if its original TTL elapsed.
            if any(
                position.instrument_id == instrument_id
                for position in self._engine.cache.positions_open(account_id=self._profile.account_id)
            ):
                continue
            working = tuple(
                order
                for order in (
                    *self._engine.cache.orders_open(account_id=self._profile.account_id),
                    *self._engine.cache.orders_inflight(account_id=self._profile.account_id),
                )
                if order.instrument_id == instrument_id
            )
            if working:
                if local is not None and local.position_quantity == 0:
                    for order in working:
                        if order.client_order_id in self._state.orders and order.is_reduce_only:
                            self._engine.cancel_order(order, client_id=ClientId("BINANCE"))
                continue
            proof_at = min(snapshot.account_observed_at_ns, snapshot.reconciliation_observed_at_ns)
            if proof_at < plan.updated_at_ns:
                continue
            if plan.opened_at_ns is None and proof_at <= plan.entry_expires_at_ns:
                continue
            query = queries.get(plan.entry_id)
            entry_order = self._engine.cache.order(ClientOrderId(plan.entry_client_order_id))
            cached_terminal = (
                entry_order is not None
                and entry_order.is_closed
                and entry_order_valid(
                    profile=self._profile,
                    strategy_id=self._engine.id,
                    request=RuntimeEntryRequest.from_plan(plan),
                    route=OiInstrumentRoute(plan.market_key, instrument_id, plan.stop_distance_bps),
                    order=entry_order,
                )
                and entry_order.quantity.as_decimal() == plan.entry_quantity
            )
            if not cached_terminal and (query is None or query.status == "working"):
                continue
            filled = entry_order.filled_qty.as_decimal() if cached_terminal else query.filled_quantity
            opened_at_ns = plan.opened_at_ns or (plan.created_at_ns if filled and filled > 0 else None)
            closed = plan.model_copy(
                update={
                    "status": "closed",
                    "terminal_at_ns": proof_at,
                    "opened_at_ns": opened_at_ns,
                    "exit_reason": plan.exit_reason or "venue_unknown",
                    "history_gap_reason": plan.history_gap_reason
                    or (
                        "close_observation_missing"
                        if opened_at_ns is not None and (local is None or local.active)
                        else ("entry_outcome_unknown" if filled is None else None)
                    ),
                    "updated_at_ns": proof_at,
                }
            )
            self._plans.offer_update(closed)
            retired.add(plan.entry_id)
            if local is not None:
                local.plan = closed
                local.active = False
        return retired

    def _restore_protections(
        self,
        *,
        state: ExecutionState,
        seed_protections: tuple[Any, ...],
        executions: dict[str, ExecutionState],
        orders: dict[ClientOrderId, tuple[str, str]],
        positions: dict[PositionId, str],
    ) -> bool:
        instrument = self._engine.cache.instrument(state.route.instrument_id)
        active = tuple(value for value in seed_protections if value.role == "active")
        pending = tuple(value for value in seed_protections if value.role == "pending")
        if instrument is None or len(active) != 1 or len(pending) > 1:
            return self._fail_and_flatten(state, executions, orders, positions)
        avg_entry_price = state.avg_entry_price
        if avg_entry_price is None:
            return self._fail_and_flatten(state, executions, orders, positions)
        desired_trigger = self._protection.desired_trigger_price(state, avg_entry_price)
        target = pending[0] if pending else active[0]
        if target.quantity != state.position_quantity or target.trigger_price != desired_trigger:
            return self._fail_and_flatten(state, executions, orders, positions)
        for protection_seed in seed_protections:
            protection = self._engine.cache.order(protection_seed.client_order_id)
            if not self._protection.stop_valid(
                state=state,
                protection=protection,
                quantity=protection_seed.quantity,
                expected_trigger=protection_seed.trigger_price,
                expected_leg=protection_leg(protection_seed.generation),
                require_open=protection_seed.role == "active",
            ):
                return self._fail_and_flatten(state, executions, orders, positions)
            orders[protection_seed.client_order_id] = (state.entry.entry_id, "protection")
            state.protection_generation = max(state.protection_generation, protection_seed.generation)
            if protection_seed.role == "active":
                state.stop_order = protection
                state.stop_quantity = protection_seed.quantity
                state.stop_avg_price = None if pending else state.avg_entry_price
            elif protection_seed.role == "pending":
                if protection.is_open:
                    return self._fail_and_flatten(state, executions, orders, positions)
                state.pending_stop_order = protection
                state.pending_stop_quantity = protection_seed.quantity
                state.pending_stop_avg_price = state.avg_entry_price
            else:
                state.retiring_stop_orders[protection_seed.client_order_id] = protection
        return True

    def _fail_and_flatten(
        self,
        state: ExecutionState,
        executions: dict[str, ExecutionState],
        orders: dict[ClientOrderId, tuple[str, str]],
        positions: dict[PositionId, str],
    ) -> bool:
        self._readiness.halt_for_unexpected_exposure()
        self._commit(
            executions=executions,
            orders=orders,
            positions=positions,
            observed_at_ns=int(self._engine.clock.timestamp_ns()),
        )
        if state.position_id is not None:
            self._exits.flatten(state.position_id, reason="recovery_safety_flatten")
        return False

    def _commit(
        self,
        *,
        executions: dict[str, ExecutionState],
        orders: dict[ClientOrderId, tuple[str, str]],
        positions: dict[PositionId, str],
        observed_at_ns: int,
    ) -> None:
        """Adopt the rebuilt ownership and open a quote stream for every position it reclaimed.

        A recovered position still has to be marked - for the operator projection, for the daily
        drawdown, and for the risk facts of any later entry - and `on_start` no longer subscribes
        anything (#510 E).
        """

        self._state.executions = executions
        self._state.orders = orders
        self._state.positions = positions
        for state in executions.values():
            if state.active or state.position_quantity > 0:
                self._quotes.ensure(state.route.instrument_id, observed_at_ns)

    def _unowned_exposure(
        self,
        *,
        orders: dict[ClientOrderId, tuple[str, str]],
        positions: dict[PositionId, str],
    ) -> tuple[frozenset[ClientOrderId], frozenset[PositionId]]:
        return unowned_cache_exposure(
            cache=self._engine.cache,
            account_id=self._profile.account_id,
            strategy_id=self._engine.id,
            owned_order_ids=frozenset(orders),
            owned_position_ids=frozenset(positions),
        )

    def _resume_ambiguous_actions(self, executions: dict[str, ExecutionState]) -> None:
        for state in executions.values():
            if state.entry_query_pending and state.entry_order is not None:
                self._engine.query_order(state.entry_order, client_id=ClientId("BINANCE"))
            if state.pending_stop_order is not None:
                self._engine.query_order(state.pending_stop_order, client_id=ClientId("BINANCE"))
            for retiring in state.retiring_stop_orders.values():
                self._engine.cancel_order(retiring, client_id=ClientId("BINANCE"))
            if state.exit_order is not None and state.exit_order.is_inflight:
                self._engine.query_order(state.exit_order, client_id=ClientId("BINANCE"))

    def _desired_trigger(self, state: ExecutionState, avg_price: Decimal | None) -> Decimal | None:
        return None if avg_price is None else self._protection.desired_trigger_price(state, avg_price)

    def verify_owned_exposure(self) -> bool:
        if any(self._unowned_exposure(orders=self._state.orders, positions=self._state.positions)):
            self._readiness.halt_for_unexpected_exposure()
            if not self._state.unexpected_exposure_reconciliation_requested:
                self._state.unexpected_exposure_reconciliation_requested = True
                self._request_reconciliation("unexpected_exposure")
            return False
        safe = True
        for state in self._state.executions.values():
            if state.position_id is None or state.position_quantity <= 0:
                continue
            active_valid = self._protection.stop_valid(
                state=state,
                protection=state.stop_order,
                quantity=state.stop_quantity,
                # `None` means "not yet priced", so there is no trigger to compare against yet.
                expected_trigger=self._desired_trigger(state, state.stop_avg_price),
                expected_leg=None,
                require_open=True,
            )
            fully_protected = (
                active_valid
                and state.stop_quantity == state.position_quantity
                and state.stop_avg_price == state.avg_entry_price
            )
            if fully_protected:
                continue
            pending_valid = self._protection.stop_valid(
                state=state,
                protection=state.pending_stop_order,
                quantity=state.pending_stop_quantity,
                expected_trigger=self._desired_trigger(state, state.pending_stop_avg_price),
                expected_leg=None,
                require_open=False,
            )
            if pending_valid and (state.stop_order is None or active_valid):
                safe = False
                continue
            self._readiness.halt_for_unexpected_exposure()
            if not state.private_reconciliation_requested:
                state.private_reconciliation_requested = True
                self._request_reconciliation("protection_ambiguity")
            self._exits.flatten(state.position_id, reason="protection_failure")
            safe = False
        return safe


__all__ = ["RecoveryCoordinator"]
