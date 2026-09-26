"""Recorded INJ evidence, decoded with the installed SDK; no venue orders are sent."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import msgspec
import pytest
from nautilus_trader.adapters.binance.common.schemas.account import BinanceOrder, BinanceUserTrade
from nautilus_trader.adapters.binance.futures.schemas.account import BinanceFuturesAlgoOrder

from tracefold.integrations.nautilus.oi_runtime.order_evidence import (
    OrderEvidenceRequest,
    read_order_evidence,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/binance/inj_20260925_execution.json").read_text())
OBSERVED_NS = 1790380800_000000000
TP = OrderEvidenceRequest(
    symbol="INJUSDT",
    client_order_id="tff9790ec0ab0d903641baaa5549a9f9",
    parent_algo_id=1000000218190388,
    conditional_type="TAKE_PROFIT_MARKET",
)


def _account(*, entry: bool = False, parent_changes=None, order_changes=None, trades=None):
    index = 0 if entry else 1
    order = FIXTURE["orders"][index] | (order_changes or {})
    parent = FIXTURE["algo"] | (parent_changes or {})
    trade_rows = [FIXTURE["trades"][index]] if trades is None else trades
    return SimpleNamespace(
        query_algo_order=AsyncMock(
            return_value=msgspec.json.decode(msgspec.json.encode(parent), type=BinanceFuturesAlgoOrder)
        ),
        query_order=AsyncMock(return_value=msgspec.json.decode(msgspec.json.encode(order), type=BinanceOrder)),
        query_user_trades=AsyncMock(
            return_value=msgspec.json.decode(msgspec.json.encode(trade_rows), type=list[BinanceUserTrade])
        ),
    )


def test_recorded_inj_child_stays_market_with_real_trade_time_and_fee():
    account = _account()
    evidence = asyncio.run(read_order_evidence(account, request=TP, observed_at_ns=OBSERVED_NS))
    assert evidence.complete
    assert evidence.order.type.value == "MARKET"
    assert evidence.parent.orderType == "TAKE_PROFIT_MARKET"
    [trade] = evidence.history.trades
    assert (trade.id, trade.orderId, trade.time) == (63772472, 308654865, 1790338365075)
    assert (Decimal(trade.qty), Decimal(trade.commission), trade.commissionAsset) == (
        Decimal("121.3"),
        Decimal("0.40669464"),
        "USDT",
    )
    account.query_algo_order.assert_awaited_once_with(algo_id=1000000218190388, client_algo_id=None)
    assert account.query_order.await_args.kwargs["order_id"] == 308654865
    assert account.query_user_trades.await_args.kwargs["order_id"] == 308654865
    with pytest.raises(FrozenInstanceError):
        evidence.observed_at_ns = 1
    with pytest.raises(AttributeError):
        trade.qty = "999"


def test_recorded_entry_has_its_own_native_trade_and_actual_cost():
    account = _account(entry=True)
    request = OrderEvidenceRequest(
        symbol="INJUSDT", client_order_id=FIXTURE["orders"][0]["clientOrderId"], venue_order_id=308643511
    )
    evidence = asyncio.run(read_order_evidence(account, request=request, observed_at_ns=OBSERVED_NS))
    assert evidence.complete and evidence.parent is None
    assert evidence.history.trades[0].id == 63767654
    assert Decimal(evidence.history.trades[0].commission) == Decimal("0.39873736")
    account.query_algo_order.assert_not_awaited()


def test_cumulative_filled_with_no_trade_is_explicitly_incomplete():
    evidence = asyncio.run(read_order_evidence(_account(trades=[]), request=TP, observed_at_ns=OBSERVED_NS))
    assert evidence.order.status.value == "FILLED"
    assert not evidence.complete
    assert evidence.history.trades == ()


def test_partial_native_evidence_is_retained_without_claiming_complete():
    trade = FIXTURE["trades"][1] | {"qty": "60"}
    evidence = asyncio.run(read_order_evidence(_account(trades=[trade]), request=TP, observed_at_ns=OBSERVED_NS))
    assert not evidence.complete
    assert evidence.history.trades[0].qty == "60"


@pytest.mark.parametrize(
    "changes",
    [
        {"actualOrderId": "308654866"},
        {"clientAlgoId": "another-plan"},
        {"symbol": "APTUSDT"},
        {"orderType": "STOP_MARKET"},
        {"reduceOnly": False},
        {"side": "BUY"},
    ],
)
def test_conflicting_parent_is_rejected(changes):
    with pytest.raises(ValueError, match=r"binance_algo_.*conflict"):
        asyncio.run(read_order_evidence(_account(parent_changes=changes), request=TP, observed_at_ns=OBSERVED_NS))


def test_regular_order_scope_cannot_silently_follow_a_different_order():
    with pytest.raises(ValueError, match="binance_order_identity_conflict"):
        asyncio.run(
            read_order_evidence(_account(), request=replace(TP, venue_order_id=123), observed_at_ns=OBSERVED_NS)
        )


def test_native_trades_cannot_exceed_the_signed_order_quantity():
    with pytest.raises(ValueError, match="binance_order_trade_quantity_conflict"):
        asyncio.run(
            read_order_evidence(
                _account(trades=[FIXTURE["trades"][1] | {"qty": "122"}]), request=TP, observed_at_ns=OBSERVED_NS
            )
        )


def test_failed_read_never_becomes_an_empty_success():
    account = _account()
    account.query_user_trades.side_effect = TimeoutError("bounded venue timeout")
    with pytest.raises(TimeoutError):
        asyncio.run(read_order_evidence(account, request=TP, observed_at_ns=OBSERVED_NS))
