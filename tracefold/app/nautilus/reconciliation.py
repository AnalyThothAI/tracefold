"""Pinned Binance account proof and OI Runtime Cache reclamation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.model.identifiers import ClientOrderId

from tracefold.integrations.nautilus.oi_runtime.config import OiRuntimeProfile
from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import CompleteBinanceAccountReports
from tracefold.integrations.nautilus.oi_runtime.state import (
    RecoveredExecutionSeed,
    RecoveredProtectionSeed,
    RuntimeEntryRequest,
    RuntimeReconciliationSnapshot,
    deterministic_client_order_id,
)
from tracefold.trading import OperatorIntentV1, TradeSignalV1

_MAX_RECOVERY_GENERATIONS = 128


def reconcile_reports_into_cache(*, engine: Any, reports: CompleteBinanceAccountReports) -> None:
    """Project every authoritative report through Nautilus ExecutionEngine."""

    if not all(engine.reconcile_execution_report(report) for report in (*reports.positions, *reports.orders)):
        raise RuntimeError("oi_runtime_execution_report_reconciliation_failed")


def account_reports_are_flat(reports: CompleteBinanceAccountReports) -> bool:
    return reports.account_flat


def build_runtime_reconciliation_snapshot(
    *,
    profile: OiRuntimeProfile,
    signals: tuple[TradeSignalV1, ...],
    manual_entries: tuple[OperatorIntentV1, ...] = (),
    cache: Any,
    account_observed_at_ns: int,
    reconciliation_observed_at_ns: int,
) -> RuntimeReconciliationSnapshot:
    """Match durable Signal identities to current Nautilus orders and positions."""

    orders = tuple(cache.orders(account_id=profile.account_id))
    subjects = [RuntimeEntryRequest.from_signal(signal) for signal in signals]
    subjects.extend(RuntimeEntryRequest.from_manual_command(command) for command in manual_entries)
    identities = tuple(request.entry_id for request in subjects)
    if len(identities) != len(set(identities)):
        raise RuntimeError("oi_runtime_recovery_identity_ambiguous")
    seeds: list[RecoveredExecutionSeed] = []
    for request in subjects:
        entry_id = deterministic_client_order_id(
            namespace=profile.client_order_namespace,
            profile_id=profile.profile_id,
            entry_id=request.entry_id,
            leg="entry",
        )
        entry = cache.order(entry_id)
        if entry is None:
            continue
        position = cache.position_for_order(entry_id)
        position_id = None if position is None or not position.is_open else position.id
        protections = _recovered_protections(profile=profile, request=request, orders=orders)
        exit_id, exit_generation = _recovered_exit(profile=profile, request=request, orders=orders)
        if entry.is_closed and position_id is None and not protections and exit_id is None:
            continue
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
    return RuntimeReconciliationSnapshot(
        runtime_profile_id=profile.profile_id,
        account_observed_at_ns=account_observed_at_ns,
        reconciliation_observed_at_ns=reconciliation_observed_at_ns,
        executions=tuple(seeds),
    )


def _recovered_protections(
    *,
    profile: OiRuntimeProfile,
    request: RuntimeEntryRequest,
    orders: tuple[Any, ...],
) -> tuple[RecoveredProtectionSeed, ...]:
    matched: list[tuple[int, Any]] = []
    for order in orders:
        quantity = Decimal(str(order.quantity))
        for generation in range(1, _MAX_RECOVERY_GENERATIONS + 1):
            expected = deterministic_client_order_id(
                namespace=profile.client_order_namespace,
                profile_id=profile.profile_id,
                entry_id=request.entry_id,
                leg=f"protection:{generation}:{format(quantity.normalize(), 'f')}",
            )
            if order.client_order_id == expected:
                matched.append((generation, order))
                break
    if not matched:
        return ()
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


def _recovered_exit(
    *,
    profile: OiRuntimeProfile,
    request: RuntimeEntryRequest,
    orders: tuple[Any, ...],
) -> tuple[ClientOrderId | None, int]:
    by_id = {order.client_order_id: order for order in orders}
    for generation in range(_MAX_RECOVERY_GENERATIONS, -1, -1):
        leg = "exit" if generation == 0 else f"exit:{generation}"
        expected = deterministic_client_order_id(
            namespace=profile.client_order_namespace,
            profile_id=profile.profile_id,
            entry_id=request.entry_id,
            leg=leg,
        )
        if expected in by_id:
            return expected, generation
    return None, 0


__all__ = [
    "account_reports_are_flat",
    "build_runtime_reconciliation_snapshot",
    "reconcile_reports_into_cache",
]
