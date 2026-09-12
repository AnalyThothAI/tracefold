"""The net-buy proposition: exact cohort, complete support and receipt cutoff."""

from dataclasses import replace
from decimal import Decimal

import pytest

from tests.news.net_buy_fixtures import NOW, movement, roster, snapshot
from tracefold.news.chain_tape.rules import WalletRules, effective_buy, trigger_age_reason


@pytest.mark.parametrize("count,matched", [(2, False), (3, True), (4, True)])
def test_fast_n_boundary(count, matched):
    result = snapshot([movement(i, usd="1000") for i in range(1, count + 1)])
    assert result.fast.matched is matched
    assert result.fast.qualified_n == count


def test_slow_window_only_and_two_windows_are_not_added():
    result = snapshot([movement(i, at=NOW - 400000) for i in range(1, 6)])
    assert not result.fast.matched and result.slow.matched
    assert result.slow.net_usd == Decimal(6000)
    both = snapshot()
    assert both.fast.net_usd == both.slow.net_usd == Decimal(6000)


def test_open_left_closed_right_and_chain_log_upper_bound():
    result = snapshot(
        [
            movement(1, at=NOW - 300000),
            movement(2, at=NOW - 300000 + 1),
            movement(3, at=NOW),
            movement(4, at=NOW + 1),
            replace(movement(5), block_number=101),
            replace(movement(6), log_index=101),
            movement(7, at=NOW - 1800000),
        ]
    )
    assert {m.wallet for m in result.fast.members} == {movement(2).wallet, movement(3).wallet}
    assert result.slow.qualified_n == 3


def test_address_case_alias_duplicates_hops_and_equal_symbols_do_not_create_wallets_or_tokens():
    one = movement(1)
    result = snapshot(
        [
            one,
            one,
            replace(movement(2, wallet=1), wallet=one.wallet.upper()),
            replace(movement(3), token="0x" + "bb" * 20),
            replace(movement(4), chain_id=1),
        ]
    )
    assert result.fast.qualified_n == 1
    assert result.fast.net_usd == Decimal(2400)


@pytest.mark.parametrize(
    "usd,raw,qualified", [("1000", 1, True), ("999.9999999999", 1, False), ("1000", 0, False), ("1000", -1, False)]
)
def test_both_precise_cash_threshold_and_positive_quantity_are_required(usd, raw, qualified):
    result = snapshot([movement(1, usd=usd, raw=raw)])
    assert result.fast.members[0].qualified is qualified


@pytest.mark.parametrize(
    "kind,usd,reason",
    [
        ("sell", None, "unpriced_trade"),
        ("buy", None, "unpriced_trade"),
        ("transfer_out", None, "transfer_out_incomplete"),
    ],
)
def test_unknown_is_not_zero(kind, usd, reason):
    result = snapshot([movement(1, usd="4000"), movement(2, wallet=1, kind=kind, usd=usd)])
    row = result.fast.members[0]
    assert row.net_usd is None and not row.qualified and reason in row.reasons


def test_profitable_cash_difference_after_selling_every_token_is_not_net_buy():
    result = snapshot([movement(1, usd="2000", raw=100), movement(2, wallet=1, kind="sell", usd="900", raw=100)])
    row = result.fast.members[0]
    assert row.net_usd == Decimal("1100") and row.net_token_raw == "0" and not row.qualified


def test_member_and_global_coverage_do_not_use_today_winners_for_past_windows():
    members = roster()
    members[0]["rank_quality"] = None
    members[1]["monitoring_from_ms"] = NOW - 299999
    members[2]["monitoring_from_ms"] = NOW - 300000
    result = snapshot(members=members)
    assert result.fast.qualified_n == 3 and result.slow.qualified_n == 2
    assert not snapshot(coverage_from_ms=None).matched
    assert not snapshot(coverage_gap_at_ms=NOW).matched


def test_negative_other_wallet_visible_and_cohort_totals_are_exact():
    result = snapshot([movement(i, usd=str(1000 + i)) for i in range(1, 5)] + [movement(5, kind="sell", usd="50000")])
    assert result.fast.qualified_n == 4
    assert result.fast.buy_usd - result.fast.sell_usd == result.fast.net_usd == Decimal(4010)
    assert result.fast.members[-1].net_usd == Decimal(-50000)


def test_effective_activity_requires_real_positive_receipt_net_for_qualified_buyer():
    result = snapshot()
    assert effective_buy([movement(1, usd="1", raw=1)], result)
    assert not effective_buy([movement(1), movement(2, wallet=1, kind="sell")], result)
    assert not effective_buy([movement(1, kind="sell")], result)
    assert not effective_buy([movement(9)], result)


@pytest.mark.parametrize(
    "event,received,now,expected",
    [
        (NOW, NOW, NOW + 60000, None),
        (NOW, NOW, NOW + 60001, "stale_trigger"),
        (NOW - 60001, NOW, NOW, "stale_trigger"),
        (NOW + 1, NOW, NOW, "future_chain_timestamp"),
    ],
)
def test_trigger_freshness(event, received, now, expected):
    assert trigger_age_reason(event_at_ms=event, received_at_ms=received, now_ms=now, max_age_s=60) == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"net_buy_fast_n": 1},
        {"net_buy_slow_n": 1},
        {"min_net_buy_usd": Decimal(0)},
        {"min_net_buy_usd": Decimal("NaN")},
        {"trigger_max_age_s": 0},
    ],
)
def test_bad_engineering_parameters_rejected(kwargs):
    with pytest.raises(ValueError):
        WalletRules(**kwargs)
