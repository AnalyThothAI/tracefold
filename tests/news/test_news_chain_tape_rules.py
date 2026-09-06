"""The two card rules, as a table of cases with no database and no network (#572 PR-2 §5.3).

`rules.py` is pure arithmetic on purpose, and this is the return on that: every threshold in #572's
medium tier is asserted here directly, including the boundary the whole exit rule turns on -- exactly
30% does not trigger and 35% does.

Nothing here mocks a mechanism. The inputs are the numbers the tape actually hands the rules: a
denominator with the basis it was established on, a sold quantity, a position value, and what the fills
table says about other roster wallets in the window.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.news.chain_tape.rules import (
    PREMIUM_BPS_MAX,
    CrowdingBuyer,
    PreviousCrowding,
    PreviousExit,
    WalletRules,
    decide_crowding,
    decide_exit,
    is_live,
    ratio_bps,
)
from tracefold.news.wallet_contracts import WalletBalance

RULES = WalletRules()
MINUTE_MS = 60_000

# One token, 18 decimals. Every quantity below is that token's raw integer.
UNIT = 10**18


def _balance(held: int, *, basis: str = "chain_balance") -> WalletBalance:
    return WalletBalance(q_before_raw=held * UNIT, basis=basis, block_hash="0x" + "ab" * 32)  # type: ignore[arg-type]


def _exit(
    *,
    held: int,
    sold: int,
    position_usd: str | None = "50000",
    cascade_wallets: int = 0,
    cascade_usd: str = "0",
    previous: PreviousExit | None = None,
    basis: str = "chain_balance",
    rules: WalletRules = RULES,
):
    return decide_exit(
        balance=_balance(held, basis=basis),
        quantity_raw=sold * UNIT,
        position_usd=None if position_usd is None else Decimal(position_usd),
        cascade_wallets=cascade_wallets,
        cascade_usd=Decimal(cascade_usd),
        event_at_ms=1_788_642_791_000,
        previous=previous,
        rules=rules,
    )


def test_the_ratio_is_two_exact_integers_and_thirty_percent_is_not_above_thirty_percent() -> None:
    """The boundary is a decision, not a rounding accident (#572 §5.3)."""

    assert ratio_bps(balance_before_raw=100 * UNIT, quantity_raw=30 * UNIT) == 3000
    assert ratio_bps(balance_before_raw=100 * UNIT, quantity_raw=35 * UNIT) == 3500
    # A denominator nothing established is a full exit as far as anything can see, and the size test is
    # what decides whether that is worth a card.
    assert ratio_bps(balance_before_raw=0, quantity_raw=1) == 10_000
    # More sold than established means the denominator was wrong, not that 140% of a position left.
    assert ratio_bps(balance_before_raw=10 * UNIT, quantity_raw=14 * UNIT) == 10_000


@pytest.mark.parametrize(
    ("name", "kwargs", "expected"),
    [
        ("exactly_thirty_percent_is_not_a_card", {"held": 100, "sold": 30}, None),
        ("thirty_five_percent_is", {"held": 100, "sold": 35}, 3500),
        ("a_full_exit_is", {"held": 100, "sold": 100}, 10_000),
        # The position floor, and the cascade arm that lets a smaller one through.
        ("below_the_position_floor_is_not", {"held": 100, "sold": 100, "position_usd": "15000"}, None),
        (
            "a_smaller_position_with_a_roster_buyer_in_the_window_is",
            {"held": 100, "sold": 100, "position_usd": "8000", "cascade_wallets": 1, "cascade_usd": "3000"},
            10_000,
        ),
        (
            "a_smaller_position_below_the_cascade_floor_is_not",
            {"held": 100, "sold": 100, "position_usd": "4000", "cascade_wallets": 2, "cascade_usd": "3000"},
            None,
        ),
        # No price anywhere is no card. The check row still records that the sell was looked at.
        ("an_unpriced_position_is_not", {"held": 100, "sold": 100, "position_usd": None}, None),
    ],
)
def test_the_exit_rule_answers_each_case_the_way_the_medium_tier_says(name, kwargs, expected) -> None:
    card = _exit(**kwargs)

    assert (None if card is None else card.ratio_bps) == expected, name


def test_a_second_sell_in_the_same_segment_follows_up_only_when_it_crosses_a_half() -> None:
    """After "sold 35%", 40% teaches a reader nothing and 60% does (#572 §5.3)."""

    previous = PreviousExit(segment_key="1788642000000", ratio_bps=3500, closed=False)

    assert _exit(held=100, sold=40, previous=previous) is None
    followup = _exit(held=100, sold=60, previous=previous)
    assert followup is not None
    assert followup.followup is True
    # The follow-up belongs to the segment it follows, which is what puts it in the same notification
    # group as the card the reader already has.
    assert followup.segment_key == "1788642000000"


def test_a_closed_segment_starts_a_new_one_rather_than_following_up() -> None:
    """A wallet that got out, bought back in and got out again is two cards, not a follow-up."""

    closed = PreviousExit(segment_key="1788600000000", ratio_bps=10_000, closed=True)

    card = _exit(held=100, sold=100, previous=closed)

    assert card is not None
    assert card.followup is False
    assert card.segment_key == "1788642791000"


def test_the_basis_the_denominator_came_from_reaches_the_card_unchanged() -> None:
    """A reconstructed denominator produces a card, and the card says it was reconstructed."""

    card = _exit(held=100, sold=100, basis="site_reported")

    assert card is not None
    assert card.basis == "site_reported"


def test_a_backfilled_fill_is_not_live_however_recent_its_block_is() -> None:
    """The 24 h backfill's whole purpose is context, and context never sends a card (#572 決策更新)."""

    event_at = 1_788_642_791_000

    assert is_live(event_at_ms=event_at, received_at_ms=event_at + 30_000, rules=RULES) is True
    assert is_live(event_at_ms=event_at, received_at_ms=event_at + 601_000, rules=RULES) is False
    assert is_live(event_at_ms=event_at, received_at_ms=event_at + 24 * 3_600_000, rules=RULES) is False


# --- crowding ---------------------------------------------------------------------------------------

WINDOW_FROM = 1_788_642_000_000


def _buyer(index: int, *, minutes: float, usd: str, price: str | None) -> CrowdingBuyer:
    return CrowdingBuyer(
        wallet=f"0x{index:040x}",
        first_at_ms=WINDOW_FROM + int(minutes * MINUTE_MS),
        usd=Decimal(usd),
        price=None if price is None else Decimal(price),
    )


def test_three_qualifying_wallets_inside_the_window_are_a_card_and_two_are_not() -> None:
    two = (_buyer(1, minutes=0, usd="4000", price="0.001"), _buyer(2, minutes=3, usd="2000", price="0.0012"))
    three = (*two, _buyer(3, minutes=11, usd="1500", price="0.0014"))

    assert decide_crowding(buyers=two, window_from_ms=WINDOW_FROM, previous=None, rules=RULES) is None
    card = decide_crowding(buyers=three, window_from_ms=WINDOW_FROM, previous=None, rules=RULES)
    assert card is not None
    assert card.lead.wallet == three[0].wallet
    assert card.total_usd == Decimal("7500")
    assert card.window_from_ms == three[0].first_at_ms
    assert card.window_to_ms == three[2].first_at_ms


def test_a_wallet_below_the_size_floor_or_already_holding_does_not_count() -> None:
    """Both halves of "opened a position of this size, in this window" have to hold (#572 §5.3)."""

    small = _buyer(3, minutes=11, usd="400", price="0.0014")
    holder = CrowdingBuyer(wallet=f"0x{4:040x}", first_at_ms=WINDOW_FROM - MINUTE_MS, usd=Decimal("9000"), price=None)
    buyers = (
        _buyer(1, minutes=0, usd="4000", price="0.001"),
        _buyer(2, minutes=3, usd="2000", price="0.0012"),
        small,
        holder,
    )

    assert decide_crowding(buyers=buyers, window_from_ms=WINDOW_FROM, previous=None, rules=RULES) is None


def test_the_late_tone_is_the_median_follower_premium_over_the_lead() -> None:
    """+54% was the measured median follow-on entry; 30% is where the card starts saying so (#572 §3.2)."""

    buyers = (
        _buyer(1, minutes=0, usd="4000", price="0.0010"),
        _buyer(2, minutes=3, usd="2000", price="0.0014"),
        _buyer(3, minutes=11, usd="1500", price="0.0016"),
    )

    card = decide_crowding(buyers=buyers, window_from_ms=WINDOW_FROM, previous=None, rules=RULES)

    assert card is not None
    # 40% and 60% over the lead; the median of the two is 50%.
    assert card.premium_bps == 5000
    assert card.late is True


def test_an_earlier_follower_median_below_the_threshold_is_not_late() -> None:
    buyers = (
        _buyer(1, minutes=0, usd="4000", price="0.0010"),
        _buyer(2, minutes=3, usd="2000", price="0.00105"),
        _buyer(3, minutes=11, usd="1500", price="0.00110"),
    )

    card = decide_crowding(buyers=buyers, window_from_ms=WINDOW_FROM, previous=None, rules=RULES)

    assert card is not None
    assert card.premium_bps == 750
    assert card.late is False


def test_a_dust_priced_lead_cannot_produce_a_premium_the_column_will_not_hold() -> None:
    """The recorded DexScreener answer for one token spans 1.088e-4 to 2.94e-27 in the same second.

    Two prices from that range divide into a ratio far past `integer`. The column the premium is stored
    in is `integer`, so an unclamped figure would not merely print badly -- the INSERT would be refused,
    the turn would raise, and the tape would stop. It is bounded to a number a card can state and a
    column can hold, in both directions.
    """

    buyers = (
        _buyer(1, minutes=0, usd="4000", price="0.00000000000000000000000000294"),
        _buyer(2, minutes=3, usd="2000", price="0.0001088"),
        _buyer(3, minutes=6, usd="2000", price="0.0001088"),
    )

    card = decide_crowding(buyers=buyers, window_from_ms=WINDOW_FROM, previous=None, rules=RULES)

    assert card is not None
    assert card.premium_bps == PREMIUM_BPS_MAX
    assert -(2**31) < card.premium_bps < 2**31
    assert card.late is True


def test_a_follower_far_below_the_lead_is_bounded_by_arithmetic_at_minus_one_position() -> None:
    """The other direction needs no clamp, and saying so is the point: a price is never negative.

    A follower can pay at most 100% less than the lead, so the ratio floors just above -10,000 bps
    whatever the two prices are. The clamp is still applied in both directions -- an unbounded number
    reaching an `integer` column is the class of defect, not this particular sign of it.
    """

    buyers = (
        _buyer(1, minutes=0, usd="4000", price="0.0001088"),
        _buyer(2, minutes=3, usd="2000", price="0.00000000000000000000000000294"),
        _buyer(3, minutes=6, usd="2000", price="0.00000000000000000000000000294"),
    )

    card = decide_crowding(buyers=buyers, window_from_ms=WINDOW_FROM, previous=None, rules=RULES)

    assert card is not None
    assert card.premium_bps == -9_999
    assert card.premium_bps >= -PREMIUM_BPS_MAX
    assert card.late is False


def test_a_live_window_follows_up_only_when_the_count_doubles() -> None:
    """Four wallets after a card about three is noise; six is the same event twice the size."""

    previous = PreviousCrowding(window_from_ms=WINDOW_FROM, window_to_ms=WINDOW_FROM + 11 * MINUTE_MS, buyers=3)
    four = tuple(_buyer(index, minutes=index, usd="2000", price="0.001") for index in range(1, 5))
    six = tuple(_buyer(index, minutes=index, usd="2000", price="0.001") for index in range(1, 7))

    assert decide_crowding(buyers=four, window_from_ms=WINDOW_FROM, previous=previous, rules=RULES) is None
    card = decide_crowding(buyers=six, window_from_ms=WINDOW_FROM, previous=previous, rules=RULES)
    assert card is not None
    assert card.followup is True
    # A follow-up belongs to the window it follows, so the reader's two cards are one subject.
    assert card.window_from_ms == WINDOW_FROM


def test_a_window_that_starts_after_the_last_card_is_a_new_subject() -> None:
    previous = PreviousCrowding(window_from_ms=WINDOW_FROM, window_to_ms=WINDOW_FROM + 11 * MINUTE_MS, buyers=3)
    later = tuple(_buyer(index, minutes=40 + index, usd="2000", price="0.001") for index in range(1, 4))

    card = decide_crowding(buyers=later, window_from_ms=WINDOW_FROM + 30 * MINUTE_MS, previous=previous, rules=RULES)

    assert card is not None
    assert card.followup is False
    assert card.window_from_ms == later[0].first_at_ms
