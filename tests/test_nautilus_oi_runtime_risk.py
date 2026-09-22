"""Sizing, equity and the protective trigger arithmetic, read from the Nautilus Cache."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.nautilus_oi_runtime_fixtures import INSTRUMENT, NOW_NS, cached_position, quote, unit_runtime
from tracefold.integrations.nautilus.oi_runtime.entry import entry_quantity, protective_trigger, spread_bps
from tracefold.integrations.nautilus.oi_runtime.risk import account_equity_usd, fixed_risk_quantity


def test_fixed_risk_quantity_floors_to_the_increment_without_exceeding_the_risk_or_the_leverage() -> None:
    quantity = fixed_risk_quantity(
        price=Decimal(10_000),
        stop_distance_bps=200,
        allowed_risk_usd=Decimal(10),
        equity_usd=Decimal(1_000),
        max_leverage=2,
        existing_notional_usd=Decimal(0),
        size_increment=Decimal("0.001"),
    )
    # 10 USD at a 2 % stop is 500 USD of notional, 0.05 BTC, inside 2 x 1000 of leverage headroom.
    assert quantity == Decimal("0.050")
    clamped = fixed_risk_quantity(
        price=Decimal(10_000),
        stop_distance_bps=200,
        allowed_risk_usd=Decimal(10),
        equity_usd=Decimal(100),
        max_leverage=1,
        existing_notional_usd=Decimal(0),
        size_increment=Decimal("0.001"),
    )
    assert clamped == Decimal("0.010")
    with pytest.raises(ValueError, match="oi_runtime_sizing_capacity_exhausted"):
        fixed_risk_quantity(
            price=Decimal(10_000),
            stop_distance_bps=200,
            allowed_risk_usd=Decimal(10),
            equity_usd=Decimal(100),
            max_leverage=1,
            existing_notional_usd=Decimal(100),
            size_increment=Decimal("0.001"),
        )


def test_an_entry_is_sized_once_against_the_padded_executable_side_and_the_venue_minimums() -> None:
    sized = entry_quantity(
        direction="long",
        quote=quote(9_999, 10_000, NOW_NS),
        instrument=INSTRUMENT,
        stop_distance_bps=200,
        allowed_risk_usd=Decimal(10),
        equity_usd=Decimal(1_000),
        max_leverage=2,
        existing_notional_usd=Decimal(0),
    )
    assert not isinstance(sized, str) and sized.as_decimal() == Decimal("0.049")
    tiny = entry_quantity(
        direction="long",
        quote=quote(9_999, 10_000, NOW_NS),
        instrument=INSTRUMENT,
        stop_distance_bps=200,
        allowed_risk_usd=Decimal("0.001"),
        equity_usd=Decimal(1_000),
        max_leverage=2,
        existing_notional_usd=Decimal(0),
    )
    assert tiny == "quantity_below_increment"


@pytest.mark.parametrize(
    ("direction", "leg", "expected"),
    [
        ("long", "stop", 9_800),
        ("long", "take_profit", 10_200),
        ("short", "stop", 10_200),
        ("short", "take_profit", 9_800),
    ],
)
def test_the_stop_sits_against_the_position_and_the_take_profit_with_it(
    direction: str, leg: str, expected: int
) -> None:
    trigger = protective_trigger(
        direction=direction,  # type: ignore[arg-type]
        average_entry_price=Decimal(10_000),
        distance_bps=200,
        leg=leg,  # type: ignore[arg-type]
    )
    assert trigger == Decimal(expected)


def test_the_spread_is_measured_over_its_own_midpoint_and_a_crossed_book_is_not_a_book() -> None:
    assert spread_bps(quote(9_950, 10_050, NOW_NS)) == Decimal(100)
    assert spread_bps(quote(10_000, 10_000, NOW_NS)) == Decimal(0)


def test_equity_is_the_balance_plus_every_marked_position_and_unknown_until_every_position_is_marked() -> None:
    runtime = unit_runtime(with_quote=False)
    assert account_equity_usd(cache=runtime.cache, account_id=runtime.profile.account_id) == Decimal(1_000)
    cached_position(runtime, quantity=Decimal("0.05"), price=Decimal(10_000))
    assert account_equity_usd(cache=runtime.cache, account_id=runtime.profile.account_id) is None
    runtime.add_quote(10_099, 10_101)
    assert account_equity_usd(cache=runtime.cache, account_id=runtime.profile.account_id) == Decimal("1005")
