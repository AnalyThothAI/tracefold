"""The net-buy proposition: one window, one quorum, exact cohort, complete support, receipt cutoff."""

from dataclasses import replace
from decimal import Decimal

import pytest

from tests.news.net_buy_fixtures import NOW, movement, roster, snapshot
from tracefold.news.chain_tape.rules import WalletRules, effective_buy, trigger_age_reason
from tracefold.news.wallet_contracts import NEW_LAUNCH_MAX_AGE_MS


@pytest.mark.parametrize("count,matched", [(4, False), (5, True), (6, True)])
def test_one_quorum_boundary(count, matched):
    """Five addresses in thirty minutes, and four is not "nearly": there is no shorter window left
    that a cohort of four could satisfy instead (#649 PR-3 §2)."""

    result = snapshot([movement(i, usd="1000") for i in range(1, count + 1)])
    assert result.window.matched is matched
    assert result.window.qualified_n == count
    assert result.matched is matched


def test_any_published_roster_member_counts_whatever_rank_put_it_there():
    """Historical rank information cannot change the rule or leak into a new snapshot."""

    members = roster()
    for member in members[:4]:
        member["rank_quality"] = None
    result = snapshot(members=members)
    assert result.window.qualified_n == 5 and result.window.matched
    assert all("rank_quality" not in member.model_dump() for member in result.window.members)
    assert not any("not_on_roster" in member.reasons for member in result.window.members)


def test_an_address_the_published_list_does_not_hold_is_not_a_buyer():
    members = roster()[:4]
    result = snapshot(members=members)
    assert result.window.qualified_n == 4 and not result.matched
    assert "not_on_roster" in result.window.members[-1].reasons


def test_thirty_minute_span_and_chain_log_upper_bound():
    result = snapshot(
        [
            movement(1, at=NOW - 1800000),
            movement(2, at=NOW - 1800000 + 1),
            movement(3, at=NOW - 400000),
            movement(4, at=NOW),
            movement(5, at=NOW + 1),
            replace(movement(6), block_number=101),
            replace(movement(7), log_index=101),
        ]
    )
    assert {m.wallet for m in result.window.members} == {movement(i).wallet for i in (2, 3, 4)}
    assert result.window.qualified_n == 3


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
    assert result.window.qualified_n == 1
    assert result.window.net_usd == Decimal(2400)


@pytest.mark.parametrize(
    "usd,raw,qualified", [("1000", 1, True), ("999.9999999999", 1, False), ("1000", 0, False), ("1000", -1, False)]
)
def test_both_precise_cash_threshold_and_positive_quantity_are_required(usd, raw, qualified):
    result = snapshot([movement(1, usd=usd, raw=raw)])
    assert result.window.members[0].qualified is qualified


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
    row = result.window.members[0]
    assert row.net_usd is None and not row.qualified and reason in row.reasons


def test_sells_are_deducted_from_the_same_fill_set():
    """Two buys and a sell by one address are one net position, not two buyers and a stranger."""

    result = snapshot(
        [
            movement(1, usd="600", raw=600),
            movement(2, wallet=1, usd="600", raw=600),
            movement(3, wallet=1, kind="sell", usd="300", raw=300),
            *[movement(i, wallet=i) for i in range(4, 9)],
        ]
    )
    first = result.window.members[0]
    assert first.buy_usd == Decimal(1200) and first.sell_usd == Decimal(300)
    assert first.net_usd == Decimal(900) and not first.qualified and "below_min_net_buy" in first.reasons
    assert result.window.qualified_n == 5


def test_profitable_cash_difference_after_selling_every_token_is_not_net_buy():
    result = snapshot([movement(1, usd="2000", raw=100), movement(2, wallet=1, kind="sell", usd="900", raw=100)])
    row = result.window.members[0]
    assert row.net_usd == Decimal("1100") and row.net_token_raw == "0" and not row.qualified


def test_member_and_global_coverage_do_not_use_today_winners_for_past_windows():
    members = roster()
    members[0]["monitoring_from_ms"] = NOW - 1799999
    members[1]["monitoring_from_ms"] = NOW - 1800000
    result = snapshot(members=members)
    assert result.window.qualified_n == 4
    assert "incomplete_monitoring_window" in result.window.members[0].reasons
    assert not snapshot(coverage_from_ms=None).matched
    assert not snapshot(coverage_gap_at_ms=NOW).matched


def test_negative_other_wallet_visible_and_cohort_totals_are_exact():
    result = snapshot([movement(i, usd=str(1000 + i)) for i in range(1, 6)] + [movement(6, kind="sell", usd="50000")])
    assert result.window.qualified_n == 5
    assert result.window.buy_usd - result.window.sell_usd == result.window.net_usd == Decimal(5015)
    assert result.window.members[-1].net_usd == Decimal(-50000)


def test_token_age_is_measured_at_the_cutoff_and_labels_a_launch_without_filtering_one():
    fresh = snapshot(token_first_seen_at_ms=NOW - NEW_LAUNCH_MAX_AGE_MS + 1)
    assert fresh.token_age_ms == NEW_LAUNCH_MAX_AGE_MS - 1 and fresh.new_launch
    old = snapshot(token_first_seen_at_ms=NOW - NEW_LAUNCH_MAX_AGE_MS)
    assert not old.new_launch and old.matched, "an older token with the same cohort is the same alert"
    unknown = snapshot(token_first_seen_at_ms=None)
    assert unknown.token_age_ms is None and not unknown.new_launch and unknown.matched


def test_participation_counts_are_carried_per_member_and_default_to_none_seen():
    result = snapshot(member_episodes={movement(1).wallet: 4})
    counts = {member.wallet: member.recent_episodes for member in result.window.members}
    assert counts[movement(1).wallet] == 4
    assert counts[movement(2).wallet] == 0, "an address with no earlier round has counted zero, not unknown"


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
        {"net_buy_slow_n": 1},
        {"min_net_buy_usd": Decimal(0)},
        {"min_net_buy_usd": Decimal("NaN")},
        {"trigger_max_age_s": 0},
    ],
)
def test_bad_engineering_parameters_rejected(kwargs):
    with pytest.raises(ValueError):
        WalletRules(**kwargs)


def test_the_second_window_is_gone_from_the_rule_object_entirely():
    with pytest.raises(TypeError):
        WalletRules(net_buy_fast_n=3)  # type: ignore[call-arg]
