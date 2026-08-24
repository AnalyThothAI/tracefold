"""Durable one-attempt submission protocol for Trading capital writes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ..contracts import (
    TRADING_COLD_WRITE_TIMEOUT_SECONDS as _COLD_WRITE_TIMEOUT_SECONDS,
)
from ..contracts import (
    TRADING_RECONCILE_BACKOFF_MS as _RECONCILE_BACKOFF_MS,
)
from ..contracts import (
    ExecutionReceipt,
    OrderState,
    PreparedOrder,
    canonical_sha256,
    utc_day_key,
)
from .order import OrderPolicy, must_close_at, next_state_for

log = logging.getLogger("tracefold.trading")

ExecutionCallObserver = Callable[[Awaitable[ExecutionReceipt]], Awaitable[ExecutionReceipt]]


async def attempt_once(
    *,
    db: Any,
    order: PreparedOrder,
    kind: Literal["entry", "exit"],
    call: Callable[[], Awaitable[ExecutionReceipt]],
    now: int,
    observe_call: ExecutionCallObserver | None = None,
) -> tuple[ExecutionReceipt | None, str]:
    """The durable one-attempt contract, shared by the entry and the exit.

    OpenTrade publishes no client idempotency key, so nothing downstream can deduplicate a resend.
    The protection is entirely local and has three parts, and **both** capital writes need all three:

    1. the ledger records the attempt and the transaction **commits before** the network call, so a
       crash between them leaves evidence that an attempt may exist;
    2. the claim is conditional, so a second caller finds zero rows updated and must not call;
    3. any exception terminalises as `AMBIGUOUS`, whose only legal successor is a read.

    The exit used to have none of them: `adapter.close()` was called bare, so an exception left the row
    untouched and the same close was re-issued every thirty seconds forever.
    """

    claim = await db.tx(
        "trading_attempt_claim",
        lambda repos: repos.trading.claim_attempt(order_id=order.order_id, kind=kind, now_ms=now),
        timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
    )
    if claim != "claimed":
        log.info("trading %s attempt refused order=%s reason=%s", kind, order.order_id, claim)
        return None, claim

    try:
        pending = call()
        return await (observe_call(pending) if observe_call is not None else pending), "claimed"
    except Exception as exc:
        reason = f"provider_exception:{type(exc).__name__}"
        log.warning("trading %s attempt did not answer: %s", kind, type(exc).__name__)
        await db.tx(
            "trading_attempt_ambiguous",
            lambda repos: repos.trading.update_order(
                order_id=order.order_id,
                state=OrderState.AMBIGUOUS.value,
                state_reason=f"{kind}_{reason}",
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            ),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )
        return None, "ambiguous"


async def commit_order(
    *,
    db: Any,
    adapter: Any,
    order: PreparedOrder,
    policy: OrderPolicy,
    count: Callable[[str], None] | None = None,
    now: int,
    observe_call: ExecutionCallObserver | None = None,
) -> bool:
    """Durable intent, then exactly one provider attempt, then whatever the answer turns out to be."""

    receipt, claim = await attempt_once(
        db=db,
        order=order,
        kind="entry",
        call=lambda: adapter.submit(order),
        now=now,
        observe_call=observe_call,
    )
    if receipt is None:
        if claim != "ambiguous":
            # The attempt was already spent by an earlier caller, who already counted it. Counting it
            # again would charge the day twice for one order.
            if count is not None:
                count("commit_reject:already_attempted")
            return False
        # It raised and is now recorded AMBIGUOUS. That does count: an order that may exist at the
        # venue is exposure.
        await db.tx(
            "trading_order_day_count",
            lambda repos: repos.trading.bump_orders_today(day_key=utc_day_key(now), now_ms=now),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )
        if count is not None:
            count("order_ambiguous")
        return True

    state = next_state_for(receipt)
    opened = now if state is OrderState.ACKNOWLEDGED else None
    deadline = must_close_at(opened_at_ms=now, policy=policy) if opened is not None else None

    def _apply(repos: Any) -> None:
        repos.trading.update_order(
            order_id=order.order_id,
            state=state.value,
            state_reason=receipt.reason,
            remote_order_id=receipt.remote_order_id,
            filled_quantity=None if receipt.filled_quantity is None else str(receipt.filled_quantity),
            average_price=None if receipt.average_price is None else str(receipt.average_price),
            position_opened_at_ms=opened,
            must_close_at_ms=deadline,
            next_reconcile_at_ms=now,
            closed_at_ms=now if state is OrderState.REJECTED else None,
            now_ms=now,
        )
        repos.trading.record_observation(
            order_id=order.order_id,
            observation_kind="submit",
            content_sha256=canonical_sha256(receipt.model_dump(mode="json")),
            content=receipt.model_dump(mode="json"),
            now_ms=now,
        )
        if state is not OrderState.REJECTED:
            repos.trading.bump_orders_today(day_key=utc_day_key(now), now_ms=now)

    await db.tx("trading_order_receipt", _apply, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
    if count is not None:
        count(f"order_{state.value.lower()}")
    return state is not OrderState.REJECTED


__all__ = ["ExecutionCallObserver", "attempt_once", "commit_order"]
