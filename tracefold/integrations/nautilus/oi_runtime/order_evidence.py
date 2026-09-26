"""One immutable signed order/Algo/trade result for native replay and the ledger.

The endpoint namespace is explicit: conditional identities go to algoOrder first;
only its actualOrderId goes to the regular order and userTrades endpoints. A
cumulative quantity is a completeness check, never a source of invented fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from nautilus_trader.adapters.binance.common.schemas.account import BinanceOrder
from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
from nautilus_trader.adapters.binance.futures.schemas.account import BinanceFuturesAlgoOrder

from .trade_history import BinanceTradeHistory, TradeHistoryCursor, read_trade_history


@dataclass(frozen=True, slots=True)
class OrderEvidenceRequest:
    symbol: str
    client_order_id: str
    # A child ID is never supplied in parent_algo_id (or vice versa).
    venue_order_id: int | None = None
    parent_algo_id: int | None = None
    conditional_type: Literal["STOP_MARKET", "TAKE_PROFIT_MARKET", "CONDITIONAL"] | None = None

    def __post_init__(self) -> None:
        if (
            not self.symbol
            or not self.symbol.isalnum()
            or self.symbol != self.symbol.upper()
            or not self.client_order_id
            or any(value is not None and value <= 0 for value in (self.venue_order_id, self.parent_algo_id))
            or (self.parent_algo_id is not None and self.conditional_type is None)
        ):
            raise ValueError("binance_order_evidence_scope_invalid")


@dataclass(frozen=True, slots=True)
class BinanceOrderEvidence:
    request: OrderEvidenceRequest
    parent: BinanceFuturesAlgoOrder | None
    order: BinanceOrder | None
    history: BinanceTradeHistory | None
    observed_at_ns: int

    @property
    def complete(self) -> bool:
        if self.order is None:
            return self.parent is not None and not self.parent.actualOrderId
        return (
            self.history is not None
            and self.history.complete
            and sum((Decimal(trade.qty) for trade in self.history.trades), Decimal()) == Decimal(self.order.executedQty)
        )


def _positive(value: str | None) -> Decimal:
    result = Decimal(value) if value is not None else Decimal("NaN")
    if not result.is_finite() or result <= 0:
        raise ValueError("binance_order_evidence_quantity_invalid")
    return result


def validate_order_evidence(evidence: BinanceOrderEvidence) -> BinanceOrderEvidence:
    request, parent, order, history = evidence.request, evidence.parent, evidence.order, evidence.history
    if evidence.observed_at_ns <= 0:
        raise ValueError("binance_order_evidence_clock_invalid")
    if request.conditional_type is not None:
        if (
            parent is None
            or parent.algoId <= 0
            or (request.parent_algo_id is not None and parent.algoId != request.parent_algo_id)
            or parent.clientAlgoId != request.client_order_id
            or parent.symbol != request.symbol
            or parent.algoType != "CONDITIONAL"
            or parent.orderType not in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
            or request.conditional_type not in {"CONDITIONAL", parent.orderType}
            or parent.positionSide != "BOTH"
            or parent.reduceOnly is not True
            or parent.workingType != "MARK_PRICE"
        ):
            raise ValueError("binance_algo_identity_conflict")
        _positive(parent.quantity)
        _positive(parent.triggerPrice)
        if not parent.actualOrderId:
            if order is not None or history is not None or request.venue_order_id is not None:
                raise ValueError("binance_algo_child_unconfirmed")
            # FINISHED without a child cannot prove cancellation or a zero fill.
            if parent.algoStatus not in ("NEW", "CANCELED", "EXPIRED", "REJECTED"):
                raise ValueError("binance_algo_child_unconfirmed")
            return evidence
        if (
            not parent.actualOrderId.isdecimal()
            or int(parent.actualOrderId) <= 0
            or order is None
            or parent.actualOrderId != str(order.orderId)
            or parent.algoStatus not in ("TRIGGERED", "FINISHED")
            or order.type.value != "MARKET"
            or order.reduceOnly is not True
            or order.side.value != parent.side
            or Decimal(order.origQty) != Decimal(parent.quantity)
        ):
            raise ValueError("binance_algo_child_identity_conflict")
    elif parent is not None:
        raise ValueError("binance_unexpected_algo_parent")
    if (
        order is None
        or order.orderId <= 0
        or order.symbol != request.symbol
        or (request.venue_order_id is not None and order.orderId != request.venue_order_id)
        or (parent is None and order.clientOrderId != request.client_order_id)
        or order.positionSide != "BOTH"
        or order.time is None
        or order.updateTime is None
        or order.time <= 0
        or order.updateTime < order.time
    ):
        raise ValueError("binance_order_identity_conflict")
    quantity = _positive(order.origQty)
    executed = Decimal(order.executedQty)
    if not executed.is_finite() or not 0 <= executed <= quantity:
        raise ValueError("binance_order_executed_quantity_invalid")
    if order.status.value == "FILLED" and executed != quantity:
        raise ValueError("binance_order_executed_quantity_invalid")
    if history is None or history.symbol != request.symbol or history.order_id != order.orderId:
        raise ValueError("binance_order_trade_scope_conflict")
    if any(trade.side != order.side or trade.time < order.time for trade in history.trades):
        raise ValueError("binance_order_trade_identity_conflict")
    if sum((Decimal(trade.qty) for trade in history.trades), Decimal()) > executed:
        # The order can race a later trade read. Re-read on the next bounded
        # attempt instead of guessing that either quantity is authoritative.
        raise ValueError("binance_order_trade_quantity_conflict")
    return evidence


async def read_order_evidence(
    account: BinanceFuturesAccountHttpAPI,
    *,
    request: OrderEvidenceRequest,
    observed_at_ns: int,
    max_trade_requests: int = 8,
) -> BinanceOrderEvidence:
    """Read one exact chain, with at most two identity reads plus bounded trades.

    Partial native trades remain in the immutable result. Consumers may retain
    them but may not treat an incomplete cumulative match as a terminal result.
    HTTP failures propagate; no failed request means an absent order or flatness.
    """
    parent = None
    order_id = request.venue_order_id
    if request.conditional_type is not None:
        parent = await account.query_algo_order(
            algo_id=request.parent_algo_id,
            client_algo_id=request.client_order_id if request.parent_algo_id is None else None,
        )
        if not parent.actualOrderId:
            return validate_order_evidence(BinanceOrderEvidence(request, parent, None, None, observed_at_ns))
        if not parent.actualOrderId.isdecimal():
            raise ValueError("binance_algo_child_identity_conflict")
        order_id = int(parent.actualOrderId)
    order = await account.query_order(
        symbol=request.symbol,
        order_id=order_id,
        orig_client_order_id=request.client_order_id if order_id is None else None,
        recv_window="60000",
    )
    if order.updateTime is None:
        raise ValueError("binance_order_evidence_clock_invalid")
    history = await read_trade_history(
        account,
        symbol=request.symbol,
        order_id=order.orderId,
        cursors=(TradeHistoryCursor(0, max(observed_at_ns // 1_000_000, order.updateTime)),),
        max_requests=max_trade_requests,
    )
    return validate_order_evidence(BinanceOrderEvidence(request, parent, order, history, observed_at_ns))
