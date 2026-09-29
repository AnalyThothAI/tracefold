"""Execution progress derived from durable entry facts for the read-only monitor.

Nothing here reads a database, a clock it was not handed, or a live venue.
"""

from __future__ import annotations

from typing import Literal

ExecutionStage = Literal[
    "pending", "accepted", "submission_unknown", "rejected", "expired", "ordered", "filled", "protected", "closed"
]

# Admission is durable before external order I/O. It does not prove venue acceptance.
ACCEPTED_ENTRY_DISPOSITIONS: frozenset[str] = frozenset({"accepted"})


def execution_stage(
    *,
    disposition_reason: str | None,
    order_status: str | None,
    fill_quantity: str | None,
    stop_trigger_price: str | None,
    take_profit_trigger_price: str | None,
    position_status: str | None,
    expires_at_ns: int | None,
    now_ns: int,
    plan_status: str | None = None,
    exit_reason: str | None = None,
) -> ExecutionStage:
    """Plans own lifecycle. Entries without a plan use their recorded observations.

    A missing audit row cannot erase an active plan or turn it into an expired Signal. The Signal TTL
    applies only before an entry plan exists. A plan that ended because its entry was refused is a
    rejection, not a closed trade.
    """

    if plan_status == "closed":
        return "rejected" if exit_reason == "entry_rejected" else "closed"
    if plan_status == "open":
        return "protected" if stop_trigger_price is not None and take_profit_trigger_price is not None else "filled"
    if plan_status == "prepared":
        if fill_quantity is not None:
            return "filled"
        if order_status in ("UNKNOWN", "RESERVED"):
            return "submission_unknown" if order_status == "UNKNOWN" else "accepted"
        if order_status in ("REJECTED", "NOT_SUBMITTED"):
            return "rejected"
        return "ordered" if order_status is not None else "accepted"
    if position_status == "closed":
        return "closed"
    if stop_trigger_price is not None and take_profit_trigger_price is not None:
        return "protected"
    if fill_quantity is not None:
        return "filled"
    if order_status in ("UNKNOWN", "RESERVED"):
        return "submission_unknown" if order_status == "UNKNOWN" else "accepted"
    if order_status in ("REJECTED", "NOT_SUBMITTED", "rejected"):
        return "rejected"
    if order_status is not None:
        return "ordered"
    if disposition_reason in ACCEPTED_ENTRY_DISPOSITIONS:
        return "accepted"
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
