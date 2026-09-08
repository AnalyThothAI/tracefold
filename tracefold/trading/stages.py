"""Execution progress derived from durable entry facts for the read-only monitor.

Nothing here reads a database, a clock it was not handed, or a live venue.
"""

from __future__ import annotations

from typing import Literal

ExecutionStage = Literal["pending", "rejected", "expired", "ordered", "filled", "protected", "closed"]

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
) -> ExecutionStage:
    """How far one entry got, read off the facts its own observations carry and its own TTL.

    The newest fact wins: a closed position is closed however it got there, and a protected position
    is the one an operator wants told apart from a bare fill. An entry the Runtime accepted always has
    an entry order observation, so `ordered` accepts either witness rather than trusting one. A manual
    entry reaches this the same way a Signal does; only where its facts are correlated differs.

    A Signal with no disposition at all is `pending` only while it can still get one. The bridge that
    hands Signals to the Runtime anti-joins on `expires_at_ns > now`, so a Signal that was refused for
    a retryable reason -- and therefore has no durable disposition row -- stops being offered the
    moment it expires and never receives one. `pending` for the rest of the window was the desk
    reading that hole as work still in flight (#604 T3, audit A4). The TTL is the Signal's own
    published clock, so the answer is derived from durable facts rather than from a new writer or a
    new gate. `expires_at_ns` is `None` for a manual entry, whose Command carries its own TTL and
    whose refusals are always written down.
    """

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
