"""Pinned Binance account proof and OI Runtime Cache reclamation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import ClientOrderId, PositionId

from tracefold.integrations.nautilus.oi_runtime.config import OiInstrumentRoute, OiRuntimeProfile
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
from tracefold.trading import OperatorIntentV1, TradeSignalV1

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
    signals: tuple[TradeSignalV1, ...],
    manual_entries: tuple[OperatorIntentV1, ...] = (),
    cache: Any,
    account_observed_at_ns: int,
    reconciliation_observed_at_ns: int,
) -> RuntimeReconciliationSnapshot:
    """Rebuild ownership from durable entry identities and the reconciled Binance reports.

    Cache is process memory and the Binance proof carries only open orders and position risk, so a
    restart in a position has no filled entry order to key off. Ownership is proven by the durable
    entry identity instead: its deterministic client order ids claim the resting stop and exit, and an
    open position on that identity's routed instrument and direction is the one it opened. Anything
    left over stays unowned (#510 C).
    """

    orders = tuple(cache.orders(account_id=profile.account_id))
    routes = {route.market_key: route for route in profile.routes}
    subjects = [RuntimeEntryRequest.from_signal(signal) for signal in signals]
    subjects.extend(RuntimeEntryRequest.from_manual_command(command) for command in manual_entries)
    identities = tuple(request.entry_id for request in subjects)
    if len(identities) != len(set(identities)):
        raise RuntimeError("oi_runtime_recovery_identity_ambiguous")
    open_positions = tuple(cache.positions_open(account_id=profile.account_id))
    claimed: set[PositionId] = set()
    seeds: list[RecoveredExecutionSeed] = []
    # Newest identity first: one instrument carries at most one active execution, so when two
    # durable entries could claim the same position the most recent one is the one that opened it.
    for request in reversed(subjects):
        entry_id = deterministic_client_order_id(
            namespace=profile.namespace,
            entry_id=request.entry_id,
            leg="entry",
        )
        entry = cache.order(entry_id)
        route = routes.get(request.market_key)
        position_id = _matched_position(
            positions=open_positions,
            route=route,
            direction=request.direction,
            claimed=claimed,
        )
        if position_id is None:
            if entry is None or entry.is_closed:
                continue
            protections: tuple[RecoveredProtectionSeed, ...] = ()
            exit_id, exit_generation = None, 0
        else:
            claimed.add(position_id)
            protections, exit_id, exit_generation = _recovered_legs(
                profile=profile,
                request=request,
                orders=orders,
            )
        seeds.append(
            RecoveredExecutionSeed(
                entry=request,
                entry_client_order_id=entry_id,
                position_id=position_id,
                protections=protections,
                exit_client_order_id=exit_id,
                exit_generation=exit_generation,
            )
        )
    seeds.reverse()
    return RuntimeReconciliationSnapshot(
        account_slot=profile.account_slot,
        account_observed_at_ns=account_observed_at_ns,
        reconciliation_observed_at_ns=reconciliation_observed_at_ns,
        executions=tuple(seeds),
    )


def _matched_position(
    *,
    positions: tuple[Any, ...],
    route: OiInstrumentRoute | None,
    direction: str,
    claimed: set[PositionId],
) -> PositionId | None:
    if route is None:
        return None
    side = PositionSide.LONG if direction == "long" else PositionSide.SHORT
    for position in positions:
        if position.id in claimed or position.instrument_id != route.instrument_id or position.side != side:
            continue
        return position.id
    return None


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
