"""Bounded entry prepare/commit handshake and coalesced lifecycle writes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import Literal

from tracefold.trading import TradePlan

_MAX_LIFECYCLE_WRITES = 256


@dataclass(frozen=True, slots=True)
class EntryQueryProof:
    entry_id: str
    status: Literal["absent", "terminal", "working"]
    filled_quantity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PlanPrepared:
    plan: TradePlan
    newly_committed: bool


class TradePlanChannel:
    """One pending admission; the database bridge alone acknowledges a committed plan.

    A retry after an uncertain commit returns an existing plan and never authorizes submission.
    Lifecycle writes coalesce by entry identity and cannot be dropped by audit backpressure.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._prepare: TradePlan | None = None
        self._receipt: PlanPrepared | None = None
        self._updates: dict[str, TradePlan] = {}

    def prepare(self, plan: TradePlan) -> bool:
        if plan.status != "prepared":
            raise ValueError("trade_plan_prepare_status_invalid")
        with self._lock:
            if self._prepare is not None or self._receipt is not None:
                return False
            self._prepare = plan
            return True

    def pending_prepare(self) -> TradePlan | None:
        with self._lock:
            return self._prepare

    def committed(self, plan: TradePlan, *, newly_committed: bool) -> None:
        with self._lock:
            if self._prepare is None or self._prepare.entry_id != plan.entry_id:
                raise RuntimeError("trade_plan_prepare_identity_lost")
            self._receipt = PlanPrepared(plan, newly_committed)
            self._prepare = None

    def take_receipt(self) -> PlanPrepared | None:
        with self._lock:
            receipt, self._receipt = self._receipt, None
            return receipt

    def offer_update(self, plan: TradePlan) -> None:
        with self._lock:
            previous = self._updates.get(plan.entry_id)
            if previous is not None and (
                previous.terminal_at_ns is not None or previous.updated_at_ns > plan.updated_at_ns
            ):
                return
            if previous is None and len(self._updates) >= _MAX_LIFECYCLE_WRITES:
                raise RuntimeError("trade_plan_lifecycle_capacity_exceeded")
            self._updates[plan.entry_id] = plan

    def pending_updates(self) -> tuple[TradePlan, ...]:
        with self._lock:
            return tuple(self._updates.values())

    def updated(self, plan: TradePlan) -> None:
        with self._lock:
            if self._updates.get(plan.entry_id) is plan:
                del self._updates[plan.entry_id]


__all__ = ["EntryQueryProof", "PlanPrepared", "TradePlanChannel"]
