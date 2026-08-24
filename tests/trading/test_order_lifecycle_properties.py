"""Property coverage for the public Trading order and sizing boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from tracefold.trading.contracts import (
    ACTIVE_ORDER_STATES,
    TERMINAL_ORDER_STATES,
    ExecutionReceipt,
    InstrumentRef,
    MarketContext,
    OrderState,
    PreparedOrder,
    RiskRejection,
)
from tracefold.trading.execution.order import DEFAULT_ORDER_POLICY, SizedOrder, size_order
from tracefold.trading.execution.submission import commit_order

pytestmark = pytest.mark.property


def _order() -> PreparedOrder:
    instrument = InstrumentRef(
        exchange_id="binance",
        venue="binance",
        provider_symbol="DOGEUSDT",
        base_symbol="DOGE",
        instrument_class="perpetual",
        quote_asset="USDT",
        observed_at_ms=1,
    )
    return PreparedOrder(
        order_id="property-order",
        case_id="property-case",
        underlying_key="crypto:DOGE",
        account_ref="default",
        instrument=instrument,
        mode="paper",
        side="buy",
        notional_usd=Decimal("50"),
        quantity=Decimal("5"),
        entry_reference=Decimal("10"),
        stop_price=Decimal("9.8"),
        take_profit_price=None,
        must_close_after_ms=1_800_000,
        payload={"symbol": "DOGEUSDT", "quantity": "5"},
    )


@dataclass
class _Ledger:
    state: str = OrderState.PREPARED.value
    provider_attempt_count: int = 0
    orders_today: int = 0
    transitions: list[str] = field(default_factory=lambda: [OrderState.PREPARED.value])
    observations: int = 0
    last_update: dict[str, Any] = field(default_factory=dict)

    def apply_provider_event(self, event: str) -> None:
        """Reference the provider read-path contract without inventing a second submit path."""

        if self.provider_attempt_count == 0 or self.state in TERMINAL_ORDER_STATES:
            return
        transitions = {
            "timeout": OrderState.AMBIGUOUS.value,
            "partial_fill": OrderState.PARTIAL.value,
            "fill": OrderState.OPEN.value,
            "cancel": OrderState.CLOSED.value,
            "close": OrderState.CLOSED.value,
        }
        if event == "reject":
            if self.state in {OrderState.SUBMITTING.value, OrderState.AMBIGUOUS.value, OrderState.RECONCILING.value}:
                self.state = OrderState.REJECTED.value
                self.transitions.append(self.state)
            return
        next_state = transitions[event]
        if next_state == OrderState.CLOSED.value and self.state not in {
            OrderState.ACKNOWLEDGED.value,
            OrderState.PARTIAL.value,
            OrderState.OPEN.value,
            OrderState.UNPROTECTED.value,
            OrderState.SAFETY_CLOSING.value,
        }:
            return
        self.state = next_state
        self.transitions.append(self.state)

    def claim_attempt(self, *, order_id: str, kind: str, now_ms: int) -> str:
        del order_id, now_ms
        assert kind == "entry"
        if self.provider_attempt_count:
            return "already_spent"
        if self.state not in {OrderState.PREPARED.value, OrderState.APPROVED.value}:
            return "wrong_state"
        self.provider_attempt_count = 1
        self.state = OrderState.SUBMITTING.value
        self.transitions.append(self.state)
        return "claimed"

    def update_order(self, **values: Any) -> None:
        self.last_update = dict(values)
        self.state = str(values["state"])
        self.transitions.append(self.state)

    def record_observation(self, **values: Any) -> None:
        del values
        self.observations += 1

    def bump_orders_today(self, **values: Any) -> None:
        del values
        self.orders_today += 1

    def release_order_day_charge(self, **values: Any) -> None:
        del values
        self.orders_today = max(self.orders_today - 1, 0)


@dataclass
class _Db:
    ledger: _Ledger

    async def tx(self, name: str, operation: Any, *, timeout_seconds: float) -> Any:
        del name, timeout_seconds
        return operation(SimpleNamespace(trading=self.ledger))


@dataclass
class _Adapter:
    outcome: str = "ack"
    attempts: int = 0

    async def submit(self, order: PreparedOrder) -> ExecutionReceipt:
        self.attempts += 1
        if self.outcome == "exception":
            raise TimeoutError("scripted provider timeout")
        if self.outcome == "reject":
            return ExecutionReceipt(state="REJECTED", reason="scripted_reject")
        if self.outcome == "ambiguous":
            return ExecutionReceipt(state="AMBIGUOUS", reason="scripted_timeout")
        return ExecutionReceipt(
            state="ACKNOWLEDGED",
            remote_order_id=f"remote-{order.order_id}",
            reason="scripted_ack",
        )


class OrderSubmissionStateMachine(RuleBasedStateMachine):
    """Submit/reconcile interleavings preserve the one-attempt and monotonic-terminal contracts."""

    def __init__(self) -> None:
        super().__init__()
        self.ledger = _Ledger()
        self.adapter = _Adapter()
        self.db = _Db(self.ledger)
        self.order = _order()
        self.mode = "paper"
        self.market = MarketContext(
            instrument=self.order.instrument,
            mark_price=Decimal("10"),
            observed_at_ms=1,
            pre_move_bps=None,
            pre_move_lookback_ms=300_000,
            spread_bps=1,
            spread_available=True,
            quantity_step=Decimal("0.001"),
            price_tick=Decimal("0.001"),
        )

    @rule(outcome=st.sampled_from(["ack", "reject", "ambiguous", "exception"]))
    def choose_provider_outcome(self, outcome: str) -> None:
        if self.ledger.provider_attempt_count == 0:
            self.adapter.outcome = outcome

    @rule(
        mode=st.sampled_from(["paper", "live_reviewed", "live_bounded"]),
        mark=st.integers(min_value=-1, max_value=100),
        spread_available=st.booleans(),
        known_step=st.booleans(),
        spread_bps=st.integers(min_value=0, max_value=100),
    )
    def vary_risk_facts(
        self,
        mode: str,
        mark: int,
        spread_available: bool,
        known_step: bool,
        spread_bps: int,
    ) -> None:
        self.mode = mode
        self.market = MarketContext(
            instrument=self.order.instrument,
            mark_price=Decimal(mark),
            observed_at_ms=1,
            pre_move_bps=None,
            pre_move_lookback_ms=300_000,
            spread_bps=spread_bps,
            spread_available=spread_available,
            quantity_step=Decimal("0.001") if known_step else None,
            price_tick=Decimal("0.001") if known_step else None,
        )

    @rule()
    def submit_or_replay_same_intent(self) -> None:
        sized = size_order(side=self.order.side, market=self.market, mode=self.mode)  # type: ignore[arg-type]
        if isinstance(sized, RiskRejection):
            return
        self.order = self.order.model_copy(
            update={
                "mode": self.mode,
                "notional_usd": sized.notional_usd,
                "quantity": sized.quantity,
                "entry_reference": sized.entry_reference,
                "stop_price": sized.stop_price,
                "take_profit_price": sized.take_profit_price,
                "payload": {"symbol": "DOGEUSDT", "quantity": str(sized.quantity)},
            }
        )
        state_before = self.ledger.state
        attempt_was_spent = self.ledger.provider_attempt_count == 1
        asyncio.run(
            commit_order(
                db=self.db,
                adapter=self.adapter,
                order=self.order,
                now=1_000,
            )
        )
        if attempt_was_spent:
            assert self.ledger.state == state_before

    @rule(event=st.sampled_from(["timeout", "partial_fill", "fill", "cancel", "reject", "close"]))
    def reconcile_arbitrary_provider_observation(self, event: str) -> None:
        self.ledger.apply_provider_event(event)

    @invariant()
    def one_intent_spends_at_most_one_provider_call(self) -> None:
        assert self.ledger.provider_attempt_count <= 1
        assert self.adapter.attempts <= 1

    @invariant()
    def ambiguous_results_are_never_resent(self) -> None:
        if self.ledger.state == OrderState.AMBIGUOUS.value:
            assert self.ledger.provider_attempt_count == 1
            assert self.adapter.attempts == 1

    @invariant()
    def terminal_state_is_monotonic_and_recorded_once(self) -> None:
        terminal_positions = [
            index for index, state in enumerate(self.ledger.transitions) if state in TERMINAL_ORDER_STATES
        ]
        assert len(terminal_positions) <= 1
        if terminal_positions:
            assert terminal_positions[0] == len(self.ledger.transitions) - 1
            assert self.ledger.state in TERMINAL_ORDER_STATES

    @invariant()
    def every_state_uses_the_public_lifecycle_vocabulary(self) -> None:
        assert set(self.ledger.transitions) <= {*ACTIVE_ORDER_STATES, *TERMINAL_ORDER_STATES}
        assert set(ACTIVE_ORDER_STATES).isdisjoint(TERMINAL_ORDER_STATES)

    @invariant()
    def replayed_state_matches_the_durable_transition_log(self) -> None:
        assert self.ledger.state == self.ledger.transitions[-1]


TestOrderSubmissionStateMachine = OrderSubmissionStateMachine.TestCase


def test_acknowledgement_never_stamps_a_position_open_or_holding_deadline() -> None:
    ledger = _Ledger()
    asyncio.run(
        commit_order(
            db=_Db(ledger),
            adapter=_Adapter(),
            order=_order(),
            now=1_000,
        )
    )
    assert ledger.state == OrderState.ACKNOWLEDGED.value
    assert ledger.last_update["position_opened_at_ms"] is None
    assert ledger.last_update["must_close_at_ms"] is None
    assert ledger.last_update["filled_quantity"] is None
    assert ledger.last_update["average_price"] is None


_positive_decimal = st.decimals(
    min_value=Decimal("0.0001"),
    max_value=Decimal("1000000"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)
_market_contexts = st.builds(
    MarketContext,
    instrument=st.just(_order().instrument),
    mark_price=_positive_decimal,
    observed_at_ms=st.just(1),
    pre_move_bps=st.none(),
    pre_move_lookback_ms=st.just(300_000),
    spread_bps=st.one_of(st.none(), st.integers(min_value=0, max_value=100)),
    spread_available=st.booleans(),
    quantity_step=st.one_of(st.none(), _positive_decimal),
    price_tick=st.one_of(st.none(), _positive_decimal),
    min_quantity=st.one_of(st.none(), _positive_decimal),
    contract_size=_positive_decimal,
)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("mode", ["paper", "live_reviewed", "live_bounded"])
@given(market=_market_contexts)
def test_sizing_never_exceeds_the_cap_and_fails_closed_on_missing_live_facts(
    side: str,
    mode: str,
    market: MarketContext,
) -> None:
    result = size_order(side=side, market=market, mode=mode)  # type: ignore[arg-type]
    if mode != "paper" and market.quantity_step is None:
        assert isinstance(result, RiskRejection)
        assert result.rule == "quantity_step_unknown"
        return
    if mode != "paper" and not market.spread_available:
        assert isinstance(result, RiskRejection)
        assert result.rule == "spread_unknown_fail_closed"
        return
    if market.spread_bps is not None and market.spread_bps > DEFAULT_ORDER_POLICY.max_spread_bps:
        assert isinstance(result, RiskRejection)
        assert result.rule == "spread_above_max"
        return
    if mode != "paper" and market.price_tick is None:
        assert isinstance(result, RiskRejection)
        assert result.rule == "price_tick_unknown"
        return
    if isinstance(result, SizedOrder):
        assert Decimal("0") < result.notional_usd <= DEFAULT_ORDER_POLICY.fixed_notional_usd
        assert result.quantity > 0
        if side == "buy":
            assert result.stop_price < result.entry_reference
        else:
            assert result.stop_price > result.entry_reference


@pytest.mark.parametrize("mark_price", [Decimal("0"), Decimal("-0.0001")])
def test_sizing_rejects_nonpositive_market_prices(mark_price: Decimal) -> None:
    market = MarketContext(
        instrument=_order().instrument,
        mark_price=mark_price,
        observed_at_ms=1,
        pre_move_bps=None,
        pre_move_lookback_ms=300_000,
        spread_bps=1,
        spread_available=True,
        quantity_step=Decimal("0.001"),
        price_tick=Decimal("0.001"),
    )
    result = size_order(side="buy", market=market, mode="live_bounded")
    assert isinstance(result, RiskRejection)
    assert result.rule == "mark_price_invalid"
