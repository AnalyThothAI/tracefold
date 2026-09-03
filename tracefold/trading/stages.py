"""How far one Signal and one operator Command got, derived from durable facts alone.

The console renders a stage word, and the words are a closed vocabulary; deriving them here rather
than in the browser is what makes them the same words in `/api/trading/executions`, the CLI and any
later reader. Nothing here reads a database, a clock it was not handed, or a live venue.
"""

from __future__ import annotations

from typing import Literal

ExecutionStage = Literal["pending", "rejected", "expired", "ordered", "filled", "protected", "closed"]
CommandStage = Literal["recorded", "accepted", "rejected", "completed", "expired"]

# Every terminal entry disposition that means "this Signal or manual Command became an order".
# Everything else the Runtime writes is a refusal; the retryable clock refusals never reach a durable
# row at all (`RETRYABLE_ENTRY_REASONS`). One durable word is stored where an operator reads two --
# what happened, and why -- so the split is derived rather than duplicated into the observation.
ACCEPTED_ENTRY_DISPOSITIONS: frozenset[str] = frozenset(
    {"accepted", "recovered", "replayed_query_first", "unknown_query_first"}
)


def signal_disposition(reason: str | None) -> Literal["accepted", "rejected"] | None:
    """`accepted` or `rejected` for one durable entry disposition reason; `None` while undisposed."""

    if reason is None:
        return None
    return "accepted" if reason in ACCEPTED_ENTRY_DISPOSITIONS else "rejected"


def execution_stage(
    *,
    disposition_reason: str | None,
    order_status: str | None,
    fill_quantity: str | None,
    stop_trigger_price: str | None,
    position_status: str | None,
) -> ExecutionStage:
    """How far one Signal got, read off the facts its own observations carry and nothing else.

    The newest fact wins: a closed position is closed however it got there, and a protected position
    is the one an operator wants told apart from a bare fill. A Signal the Runtime accepted always has
    an entry order observation, so `ordered` accepts either witness rather than trusting one.
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
        return "pending"
    if disposition_reason == "expired":
        return "expired"
    return "rejected"


def command_stage(
    *,
    disposition: str | None,
    disposition_reason: str | None,
    expires_at_ns: int,
    now_ns: int,
) -> CommandStage:
    """How far one operator Command got, from its `control_disposition` alone.

    Never from a venue observation: a flatten converges the whole account slot, so the orders it
    produces belong to whatever exposure was there rather than to the Command. The Runtime's own
    `binance_account_flat` completion is the only fact that says the slot actually went flat.
    """

    if disposition is None:
        return "expired" if expires_at_ns <= now_ns else "recorded"
    if disposition == "completed" and disposition_reason == "binance_account_flat":
        return "completed"
    if disposition == "rejected":
        return "expired" if disposition_reason == "expired" else "rejected"
    return "accepted"


__all__ = [
    "ACCEPTED_ENTRY_DISPOSITIONS",
    "CommandStage",
    "ExecutionStage",
    "command_stage",
    "execution_stage",
    "signal_disposition",
]
