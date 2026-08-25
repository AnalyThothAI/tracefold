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
    ExecutionAdapter,
    ExecutionReceipt,
    OrderState,
    PreparedOrder,
    canonical_sha256,
    utc_day_key,
)
from .order import next_state_for

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
    claim_clock: Callable[[], int] | None = None,
    entry_approval_window: tuple[int, int] | None = None,
    entry_preflight_window: tuple[int, int] | None = None,
) -> tuple[ExecutionReceipt | None, str, int]:
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

    def _claim(repos: Any) -> tuple[str, int]:
        if entry_approval_window is not None:
            # Take the order-row lock before sampling the final live windows. Otherwise lock
            # contention can consume their remaining lifetime after a seemingly valid sample.
            repos.trading.lock_entry_attempt(order_id=order.order_id)
        claim_now = int(claim_clock()) if claim_clock is not None else now
        claim_kwargs: dict[str, Any] = {}
        if entry_approval_window is not None:
            claim_kwargs["entry_approval_window"] = entry_approval_window
        if entry_preflight_window is not None:
            claim_kwargs["entry_preflight_window"] = entry_preflight_window
        result = str(
            repos.trading.claim_attempt(
                order_id=order.order_id,
                kind=kind,
                now_ms=claim_now,
                **claim_kwargs,
            )
        )
        if result == "claimed" and kind == "entry":
            # This is an attempt/write ceiling, not a receipt counter. Charge it in the same commit as
            # SUBMITTING so process death after the provider call cannot make another entry admissible.
            repos.trading.bump_orders_today(day_key=utc_day_key(claim_now), now_ms=claim_now)
        return result, claim_now

    claim, claim_now = await db.tx(
        "trading_attempt_claim",
        _claim,
        timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
    )
    if claim != "claimed":
        log.info("trading %s attempt refused order=%s reason=%s", kind, order.order_id, claim)
        return None, claim, claim_now

    try:
        pending = call()
        return await (observe_call(pending) if observe_call is not None else pending), "claimed", claim_now
    except Exception as exc:
        reason = f"provider_exception:{type(exc).__name__}"
        log.warning("trading %s attempt did not answer: %s", kind, type(exc).__name__)
        await db.tx(
            "trading_attempt_ambiguous",
            lambda repos: repos.trading.update_order(
                order_id=order.order_id,
                state=OrderState.AMBIGUOUS.value,
                state_reason=f"{kind}_{reason}",
                next_reconcile_at_ms=claim_now + _RECONCILE_BACKOFF_MS,
                now_ms=claim_now,
            ),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )
        return None, "ambiguous", claim_now


async def commit_order(
    *,
    db: Any,
    adapter: ExecutionAdapter,
    order: PreparedOrder,
    count: Callable[[str], None] | None = None,
    now: int,
    observe_call: ExecutionCallObserver | None = None,
    claim_clock: Callable[[], int] | None = None,
    entry_approval_window: tuple[int, int] | None = None,
    entry_preflight_window: tuple[int, int] | None = None,
) -> bool:
    """Durable intent, then exactly one provider attempt, then whatever the answer turns out to be."""

    receipt, claim, claim_now = await attempt_once(
        db=db,
        order=order,
        kind="entry",
        call=lambda: adapter.submit(order),
        now=now,
        observe_call=observe_call,
        claim_clock=claim_clock,
        entry_approval_window=entry_approval_window,
        entry_preflight_window=entry_preflight_window,
    )
    if receipt is None:
        if claim != "ambiguous":
            # The attempt was already spent by an earlier caller, who already counted it. Counting it
            # again would charge the day twice for one order.
            if count is not None:
                count("commit_reject:already_attempted")
            return False
        if count is not None:
            count("order_ambiguous")
        return True

    state = next_state_for(receipt)

    def _apply(repos: Any) -> None:
        repos.trading.update_order(
            order_id=order.order_id,
            state=state.value,
            state_reason=receipt.reason,
            remote_order_id=receipt.remote_order_id,
            # A receipt may repeat exchange fill hints, but the canonical fill/position fields belong
            # only to the explicit read path. The complete receipt remains in the observation ledger.
            filled_quantity=None,
            average_price=None,
            position_opened_at_ms=None,
            must_close_at_ms=None,
            next_reconcile_at_ms=claim_now,
            closed_at_ms=claim_now if state is OrderState.REJECTED else None,
            now_ms=claim_now,
        )
        repos.trading.record_observation(
            order_id=order.order_id,
            observation_kind="submit",
            content_sha256=canonical_sha256(receipt.model_dump(mode="json")),
            content=receipt.model_dump(mode="json"),
            now_ms=claim_now,
        )
        if state is OrderState.REJECTED:
            # A definitive rejection proves no exposure. Releasing the conservative claim in the
            # receipt transaction preserves the nominal loss-envelope semantics. A crash before this
            # proof stays charged, which is the safe side of the uncertainty.
            repos.trading.release_order_day_charge(day_key=utc_day_key(claim_now), now_ms=claim_now)

    await db.tx("trading_order_receipt", _apply, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
    if count is not None:
        count(f"order_{state.value.lower()}")
    return state is not OrderState.REJECTED


__all__ = ["ExecutionCallObserver", "attempt_once", "commit_order"]
