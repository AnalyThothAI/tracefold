"""Execution progress derived from durable entry facts for the read-only monitor.

Nothing here reads a database, a clock it was not handed, or a live venue.
"""

from __future__ import annotations

from typing import Literal

ExecutionStage = Literal["pending", "rejected", "expired", "ordered", "filled", "protected", "closed"]

# The one entry disposition that means "the venue took this Signal's or manual Command's order". A
# Runtime writes it only after the venue answered (#680); every other word is a refusal, including
# `venue_rejected`, which is the venue refusing the order itself.
ACCEPTED_ENTRY_DISPOSITIONS: frozenset[str] = frozenset({"accepted"})


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
    exit_reason: str | None = None,
) -> ExecutionStage:
    """Plans own lifecycle. Entries without a plan use their recorded observations.

    A missing audit row cannot erase an active plan or turn it into an expired Signal. The Signal TTL
    applies only before an entry plan exists. A plan that ended because its entry was refused is a
    rejection, not a closed trade.
    """

    if plan_status == "closed":
        return "rejected" if exit_reason == "not_submitted" else "closed"
    if plan_status == "open":
        return "protected" if stop_trigger_price is not None else "filled"
    if plan_status == "prepared":
        return "ordered" if order_status is not None or fill_quantity is not None else "pending"
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
