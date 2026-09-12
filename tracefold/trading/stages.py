"""Execution progress derived from durable entry facts for the read-only monitor.

Nothing here reads a database, a clock it was not handed, or a live venue.
"""

from __future__ import annotations

from typing import Literal

ExecutionStage = Literal[
    "pending", "rejected", "expired", "ordered", "filled", "protected", "closing", "closed", "unresolved"
]

# Every terminal entry disposition that means "this Signal or manual Command became an order".
# Everything else the Runtime writes is a refusal; the retryable clock refusals never reach a durable
# row at all (`RETRYABLE_ENTRY_REASONS`). It also fed a published `accepted` / `rejected` split beside
# `stage`, which already says `ordered` or `rejected` about the same row -- one word, derived once
# here, is what `/api/trading/executions` publishes (#537 PR-5).
ACCEPTED_ENTRY_DISPOSITIONS: frozenset[str] = frozenset(
    {"accepted", "recovered", "replayed_query_first", "unknown_query_first"}
)


def execution_stage(
    *,
    disposition_reason: str | None,
    order_status: str | None,
    fill_quantity: str | None,
    stop_trigger_price: str | None,
    position_status: str | None,
    expires_at_ns: int | None,
    now_ns: int,
    plan_status: str | None = None,
) -> ExecutionStage:
    """Plans own lifecycle. Historical entries without a plan use their recorded observations.

    A missing audit row cannot erase an active plan or turn it into an expired Signal.
    The Signal TTL applies only before an entry plan exists.
    """

    if plan_status == "closed":
        return "closed"
    if plan_status == "closing":
        return "closing"
    if plan_status == "unresolved":
        return "unresolved"
    if plan_status == "open":
        return "protected" if stop_trigger_price is not None else "filled"
    if plan_status == "entry_working":
        return "ordered"
    if plan_status == "prepared":
        return "pending"
    if position_status == "closed":
        return "closed"
    if stop_trigger_price is not None:
        return "protected"
    if fill_quantity is not None:
        return "filled"
    if order_status is not None or disposition_reason in ACCEPTED_ENTRY_DISPOSITIONS:
        return "ordered"
    if disposition_reason is None:
        return "expired" if expires_at_ns is not None and now_ns > expires_at_ns else "pending"
    if disposition_reason == "expired":
        return "expired"
    return "rejected"


__all__ = [
    "ACCEPTED_ENTRY_DISPOSITIONS",
    "ExecutionStage",
    "execution_stage",
]
