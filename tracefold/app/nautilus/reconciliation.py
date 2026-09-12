"""Pinned Binance account proof and OI Runtime Cache reclamation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from tracefold.integrations.nautilus.oi_runtime.config import OiRuntimeProfile
from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import CompleteBinanceAccountReports
from tracefold.integrations.nautilus.oi_runtime.state import (
    RecoveredExecutionSeed,
    RecoveredProtectionSeed,
    RuntimeEntryRequest,
    RuntimeReconciliationSnapshot,
    deterministic_client_order_id,
    exit_leg,
    protection_leg,
)
from tracefold.integrations.nautilus.oi_runtime.trade_plans import EntryQueryProof
from tracefold.trading import TradePlan

# How far a single execution's stop and exit replacement chains are followed when ownership is
# rebuilt. It bounds one candidate map, derived per durable entry identity before any cached order is
# read; matching the account's N cached orders against it is then N dict lookups. It used to bound a
# loop *inside* the scan over those orders, so every reconciliation derived 128 client order ids per
# cached order to discover which generation that order was -- a discovery the map now makes once,
# because a protection leg is its generation and nothing else (#537 PR-4).
_MAX_RECOVERY_GENERATIONS = 128

type _RecoveryLeg = tuple[Literal["protection", "exit"], int]


def reconcile_reports_into_cache(*, engine: Any, reports: CompleteBinanceAccountReports) -> None:
    """Project every authoritative report through Nautilus ExecutionEngine."""

    if not all(engine.reconcile_execution_report(report) for report in (*reports.positions, *reports.orders)):
        raise RuntimeError("oi_runtime_execution_report_reconciliation_failed")


def build_runtime_reconciliation_snapshot(
    *,
    profile: OiRuntimeProfile,
    plans: tuple[TradePlan, ...],
    cache: Any,
    account_observed_at_ns: int,
    reconciliation_observed_at_ns: int,
    entry_queries: tuple[EntryQueryProof, ...] = (),
) -> RuntimeReconciliationSnapshot:
    """Match only active plans in this account/mode. Never choose the newest candidate.

    A cold Cache may lack the filled entry. Only a unique active identity on the frozen
    instrument and side can then claim that position; native shape checks still follow.
    """
    candidates = tuple(
        plan
        for plan in plans
        if plan.account_slot == profile.account_slot
        and plan.runtime_mode_at_creation == profile.mode
        and plan.terminal_at_ns is None
    )
    orders = tuple(cache.orders(account_id=profile.account_id))
    positions = tuple(cache.positions_open(account_id=profile.account_id))
    by_instrument: dict[str, list[TradePlan]] = {}
    for plan in candidates:
        by_instrument.setdefault(plan.instrument_id, []).append(plan)
    ambiguous = any(len(group) > 1 for group in by_instrument.values())
    seeds: list[RecoveredExecutionSeed] = []
    unresolved: list[TradePlan] = []
    for plan in candidates:
        if len(by_instrument[plan.instrument_id]) != 1:
            unresolved.append(plan)
            continue
        request = RuntimeEntryRequest.from_plan(plan)
        entry_id = ClientOrderId(plan.entry_client_order_id)
        entry = cache.order(entry_id)
        instrument_id = InstrumentId.from_str(plan.instrument_id)
        side = PositionSide.LONG if plan.direction == "long" else PositionSide.SHORT
        matching = tuple(
            position for position in positions if position.instrument_id == instrument_id and position.side == side
        )
        if len(matching) > 1:
            ambiguous = True
            unresolved.append(plan)
            continue
        linked_position = cache.position_for_order(entry_id)
        if linked_position is not None:
            if linked_position.is_open and (len(matching) != 1 or matching[0].id != linked_position.id):
                ambiguous = True
                unresolved.append(plan)
                continue
            # A closed explicitly linked position cannot be reassigned to a different new position.
            if not linked_position.is_open and matching:
                ambiguous = True
                unresolved.append(plan)
                continue
        position_id = matching[0].id if matching else None
        if position_id is None and (entry is None or entry.is_closed):
            unresolved.append(plan)
            continue
        protections, exit_id, exit_generation = _recovered_legs(profile=profile, request=request, orders=orders)
        seeds.append(
            RecoveredExecutionSeed(
                entry=request,
                plan=plan,
                entry_client_order_id=entry_id,
                position_id=position_id,
                protections=protections,
                exit_client_order_id=exit_id,
                exit_generation=exit_generation,
            )
        )
    return RuntimeReconciliationSnapshot(
        account_slot=profile.account_slot,
        account_observed_at_ns=account_observed_at_ns,
        reconciliation_observed_at_ns=reconciliation_observed_at_ns,
        executions=tuple(seeds),
        unresolved_plans=tuple(unresolved),
        ownership_ambiguous=ambiguous,
        entry_queries=entry_queries,
    )


def _recovery_legs(*, profile: OiRuntimeProfile, entry_id: str) -> dict[ClientOrderId, _RecoveryLeg]:
    """Every stop and exit id this entry identity could have claimed, keyed by client order id."""

    legs: dict[ClientOrderId, _RecoveryLeg] = {}
    for generation in range(_MAX_RECOVERY_GENERATIONS + 1):
        legs[
            deterministic_client_order_id(
                namespace=profile.namespace,
                entry_id=entry_id,
                leg=exit_leg(generation),
            )
        ] = ("exit", generation)
        if generation == 0:
            continue
        legs[
            deterministic_client_order_id(
                namespace=profile.namespace,
                entry_id=entry_id,
                leg=protection_leg(generation),
            )
        ] = ("protection", generation)
    return legs


def _recovered_legs(
    *,
    profile: OiRuntimeProfile,
    request: RuntimeEntryRequest,
    orders: tuple[Any, ...],
) -> tuple[tuple[RecoveredProtectionSeed, ...], ClientOrderId | None, int]:
    """One pass over the account's cached orders, claiming this identity's stops and its exit."""

    legs = _recovery_legs(profile=profile, entry_id=request.entry_id)
    protections: list[tuple[int, Any]] = []
    exit_id: ClientOrderId | None = None
    exit_generation = 0
    for order in orders:
        claim = legs.get(order.client_order_id)
        if claim is None:
            continue
        kind, generation = claim
        if kind == "protection":
            protections.append((generation, order))
        elif exit_id is None or generation > exit_generation:
            exit_id, exit_generation = order.client_order_id, generation
    return _protection_seeds(protections), exit_id, exit_generation


def _protection_seeds(matched: list[tuple[int, Any]]) -> tuple[RecoveredProtectionSeed, ...]:
    """The live stop is the newest open generation; every other open one is on its way out."""

    highest_open_generation = max(
        (generation for generation, order in matched if order.is_open),
        default=-1,
    )
    return tuple(
        RecoveredProtectionSeed(
            role="active" if generation == highest_open_generation else "retiring",
            client_order_id=order.client_order_id,
            quantity=Decimal(str(order.quantity)),
            trigger_price=Decimal(str(order.trigger_price)),
            generation=generation,
        )
        for generation, order in sorted(matched, key=lambda item: item[0])
        if not order.is_closed
    )


__all__ = [
    "build_runtime_reconciliation_snapshot",
    "reconcile_reports_into_cache",
]
