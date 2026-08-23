"""The paper execution adapter and the deterministic paper exit.

Paper exists to exercise the order kernel, not to produce a P&L claim. It therefore behaves like a
provider that can misbehave: the fault script can make any attempt reject or go ambiguous, because the
states that matter — `AMBIGUOUS`, the restart that must not resend, the account that stays blocked
until a read resolves it — are exactly the ones a always-succeeds fake never reaches.

Two things paper deliberately does *not* fake:

* an order book, so `spread_bps` stays unknown rather than zero;
* the venue's lot size, so sizing uses a declared conservative step and live refuses to guess.

Costs are charged on both legs at the taker rate. A paper receipt that ignored fees would be the one
number most likely to be quoted later as if it were an edge.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .models import Bar, ExecutionReceipt, OrderSide, PreparedOrder


@dataclass(slots=True)
class PaperFaults:
    """A finite script of provider misbehaviour, consumed in order. Empty means every attempt is acked."""

    script: list[str] = field(default_factory=list)

    def next(self) -> str:
        return self.script.pop(0) if self.script else "ack"


@dataclass(slots=True)
class PaperAdapter:
    """Fills at the frozen entry reference. One attempt, one answer, no retry, no second venue.

    `remote` is the simulated venue's own book, and it is deliberately separate from what `submit`
    returned. That separation is the whole point of the ambiguous fault: `ambiguous` means the write
    landed but the answer did not, `ambiguous_lost` means neither did. Reconciliation must be able to
    tell those apart by *reading*, exactly as it will against a real venue, and a fake that always
    agrees with its own return value can never exercise that.
    """

    name: str = "paper"
    faults: PaperFaults = field(default_factory=PaperFaults)
    attempts: int = 0
    remote: dict[str, ExecutionReceipt] = field(default_factory=dict)

    async def submit(self, order: PreparedOrder) -> ExecutionReceipt:
        self.attempts += 1
        fault = self.faults.next()
        if fault == "reject":
            return ExecutionReceipt(state="REJECTED", reason="paper_rejected")
        accepted = ExecutionReceipt(
            state="ACKNOWLEDGED",
            remote_order_id=f"paper-{order.order_id}",
            filled_quantity=order.quantity,
            average_price=order.entry_reference,
            reason="paper_ack",
        )
        if fault == "ambiguous":
            self.remote[order.order_id] = accepted
            return ExecutionReceipt(state="AMBIGUOUS", reason="paper_timeout")
        if fault == "ambiguous_lost":
            return ExecutionReceipt(state="AMBIGUOUS", reason="paper_timeout")
        self.remote[order.order_id] = accepted
        return accepted

    async def observe(self, order: PreparedOrder) -> ExecutionReceipt | None:
        """The read-only reconcile primitive: what the venue says it holds for this intent, or nothing."""

        return self.remote.get(order.order_id)

    async def close(self, order: PreparedOrder, *, quantity: Decimal) -> ExecutionReceipt:
        self.attempts += 1
        fault = self.faults.next()
        if fault == "ambiguous":
            return ExecutionReceipt(state="AMBIGUOUS", reason="paper_close_timeout")
        if fault == "reject":
            return ExecutionReceipt(state="REJECTED", reason="paper_close_rejected")
        self.remote.pop(order.order_id, None)
        return ExecutionReceipt(
            state="ACKNOWLEDGED",
            remote_order_id=f"paper-close-{order.order_id}",
            filled_quantity=quantity,
            reason="paper_close_ack",
        )


@dataclass(frozen=True, slots=True)
class PaperExit:
    exit_price: Decimal
    exit_at_ms: int
    reason: str


def evaluate_paper_exit(
    *,
    side: OrderSide,
    entry: Decimal,
    stop_price: Decimal,
    take_profit_price: Decimal | None,
    opened_at_ms: int,
    must_close_at_ms: int,
    bars: Sequence[Bar],
    now_ms: int,
) -> PaperExit | None:
    """Walk closed bars in order; the first trigger wins, and the stop wins a tie.

    Only closed bars are consulted and only the close is used. A high/low fill would be a claim about
    an intrabar path the 1-minute feed cannot support, and being optimistic about the exit is the easiest
    way to manufacture an edge that does not exist.
    """

    ordered = sorted((bar for bar in bars if bar.open_at_ms >= opened_at_ms), key=lambda bar: bar.close_at_ms)
    for bar in ordered:
        stopped = bar.close <= stop_price if side == "buy" else bar.close >= stop_price
        if stopped:
            return PaperExit(exit_price=bar.close, exit_at_ms=bar.close_at_ms, reason="stop_loss")
        if take_profit_price is not None:
            hit = bar.close >= take_profit_price if side == "buy" else bar.close <= take_profit_price
            if hit:
                return PaperExit(exit_price=bar.close, exit_at_ms=bar.close_at_ms, reason="take_profit")
        if bar.close_at_ms >= must_close_at_ms:
            return PaperExit(exit_price=bar.close, exit_at_ms=bar.close_at_ms, reason="max_holding")
    if now_ms >= must_close_at_ms and ordered:
        # The deadline passed but no bar closed after it — close on the last price that exists rather
        # than holding a paper position open forever because the feed was thin.
        last = ordered[-1]
        return PaperExit(
            exit_price=last.close,
            exit_at_ms=max(last.close_at_ms, must_close_at_ms),
            reason="max_holding",
        )
    return None


__all__ = ["PaperAdapter", "PaperExit", "PaperFaults", "evaluate_paper_exit"]
