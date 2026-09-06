"""When a followed wallet's movement is worth interrupting a reader (#572 PR-2, §5.3 medium tier).

Two rules, both arithmetic, both pure. Nothing here reads a clock, opens a connection or asks a model:
the caller hands in the fill, the denominator it managed to establish, the provider's own figures and
whatever the fills table already knows about the token, and these functions answer *card* or *no card*.
That is what makes the whole threshold table testable without a database or a network.

**Exit.** A roster wallet sold more than `exit_ratio_bps` of what it held, or sold the last of it. The
denominator is the chain's `balanceOf` at the block before the sell where the public node still holds
that state, and the provider's current bag plus the sold amount where it does not -- the card says which
(#572 決策更新, 2026-09-06). Size is the second half: a 100% exit of a $300 position is not news, so the
position must be worth `exit_min_position_usd`, *or* be worth `exit_cascade_min_usd` while at least one
other roster wallet bought the same token inside `exit_cascade_window_s`. The cascade arm is the one the
event study argued for -- after a followed wallet sells, other followed wallets stop buying and the price
drifts down (#572 §3.2) -- and it is the reason a smaller position can still earn a card.

**Crowding.** `crowding_n` roster wallets each put at least `crowding_min_usd` into the same token inside
`crowding_window_s`, each of them opening the position in that window. The card carries the lead, the
median follower's entry premium over the lead's price, and `late` when that premium reaches
`crowding_premium_late_bps` -- because the measured median follow-on entry was +54% over the leader and
the median outcome from there was negative (#572 §3.2).

Both rules are one card per subject and then silence: an exit segment ends when the balance reaches zero,
and a crowding window ends when the buying stops. A follow-up exists only where the number a reader was
already told has *materially* changed -- the exit ratio crossing 50% or 100%, the crowding count
doubling -- which is the same shape the OI branch's "twice the anchor" rule has (#553 §4.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..wallet_contracts import CheckBasis, WalletBalance

# Ten thousand basis points is the whole of a position. A ratio is clamped to it rather than allowed
# above it: a sell larger than the balance we established means the denominator was the wrong one (a
# same-block inbound transfer, a stale bag), and "sold more than everything" is not a number to print.
BPS_FULL: Final = 10_000
# Where an exit follow-up becomes worth a second card. The bands are halves of the position, not
# percentages of the previous ratio: after "sold 35%", a reader learns something from "sold 60%" and
# nothing from "sold 36%".
_RATIO_BANDS: Final[tuple[int, ...]] = (5_000, BPS_FULL)
# The widest entry premium a card will state, in both directions: 100,000%. Not a threshold -- nothing
# reads it but the renderer -- it is a bound on the *number*. These tokens trade in pools whose printed
# price spans thirty orders of magnitude (the recorded FSD document has pairs at 1.088e-4 and 2.94e-27
# on the same chain in the same second), so an unclamped ratio of two of them overflows the `integer`
# column it is stored in and takes the whole turn down with it.
PREMIUM_BPS_MAX: Final = 10_000_000


@dataclass(frozen=True, slots=True)
class WalletRules:
    """The medium tier of #572 §6.4, chosen on 2026-09-06 and carried as runtime configuration.

    Every value is a threshold an operator can move without a release. None of them is a contract: the
    numbers came from the provider's own seven-day close ledger, projected about 25-40 cards a day, and
    the point of shipping them is the receipt that says whether that projection held.
    """

    exit_ratio_bps: int = 3_000
    exit_min_position_usd: Decimal = Decimal("20000")
    exit_cascade_window_s: int = 7_200
    exit_cascade_min_usd: Decimal = Decimal("5000")
    crowding_n: int = 3
    crowding_window_s: int = 900
    crowding_min_usd: Decimal = Decimal("1000")
    crowding_premium_late_bps: int = 3_000
    # How old a fill may be, on the two clocks the tape already records, and still fire a rule. The 24 h
    # backfill this PR was dispatched against exists to give the cascade and crowding rules their
    # context; a card about what a wallet did yesterday would be a card about the backfill (#572
    # 決策更新).
    trigger_max_age_s: int = 600

    @property
    def trigger_max_age_ms(self) -> int:
        return max(0, int(self.trigger_max_age_s)) * 1_000

    @property
    def exit_cascade_window_ms(self) -> int:
        return max(0, int(self.exit_cascade_window_s)) * 1_000

    @property
    def crowding_window_ms(self) -> int:
        return max(0, int(self.crowding_window_s)) * 1_000


@dataclass(frozen=True, slots=True)
class PreviousExit:
    """The last exit card for one wallet and token: which segment it belonged to and what it said."""

    segment_key: str
    ratio_bps: int
    closed: bool


@dataclass(frozen=True, slots=True)
class PreviousCrowding:
    """The last crowding card for one token: the window it covered and how many wallets it counted."""

    window_from_ms: int
    window_to_ms: int
    buyers: int


@dataclass(frozen=True, slots=True)
class ExitCard:
    """What an exit card says, before anything decides how to show it."""

    ratio_bps: int
    basis: CheckBasis
    closed: bool
    balance_before_raw: int
    quantity_raw: int
    position_usd: Decimal | None
    cascade_wallets: int
    cascade_usd: Decimal
    segment_key: str
    followup: bool


@dataclass(frozen=True, slots=True)
class CrowdingBuyer:
    """One roster wallet's participation in one crowding window.

    `first_at_ms` is the wallet's first buy of this token in the whole retained tape, not its first buy
    inside the window: a wallet that has been holding since yesterday did not just crowd into anything,
    and counting it would turn every later purchase into a fresh signal.
    """

    wallet: str
    first_at_ms: int
    usd: Decimal
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class CrowdingCard:
    """What a crowding card says: who led, who followed, how much later and how much higher."""

    lead: CrowdingBuyer
    buyers: tuple[CrowdingBuyer, ...]
    total_usd: Decimal
    premium_bps: int | None
    window_from_ms: int
    window_to_ms: int
    late: bool
    followup: bool


def is_live(*, event_at_ms: int, received_at_ms: int, rules: WalletRules) -> bool:
    """Whether a fill is recent enough to be a signal rather than history.

    Both stamps are already on the fill: the block's own time and the moment this host read it. A
    backfilled movement has a wide gap between them by construction, so this one comparison is the whole
    of "a re-read of the past never sends a card" -- no marker, no flag, no separate ingest mode.
    """

    return int(received_at_ms) - int(event_at_ms) <= rules.trigger_max_age_ms


def ratio_bps(*, balance_before_raw: int, quantity_raw: int) -> int:
    """What share of the established balance this sell was, in basis points, from two exact integers.

    Floor division on purpose. Exactly 30% is `3000`, which the strictly-greater test below does not
    admit -- the boundary is a decision, not a rounding accident.
    """

    before, sold = int(balance_before_raw), int(quantity_raw)
    if before <= 0:
        # Nothing was established to divide by. The wallet sold what it had as far as anything can tell,
        # which is a full exit and is where the size test takes over.
        return BPS_FULL
    return min(BPS_FULL, (sold * BPS_FULL) // before)


def _band(bps: int) -> int:
    return sum(1 for edge in _RATIO_BANDS if int(bps) >= edge)


def decide_exit(
    *,
    balance: WalletBalance,
    quantity_raw: int,
    position_usd: Decimal | None,
    cascade_wallets: int,
    cascade_usd: Decimal,
    event_at_ms: int,
    previous: PreviousExit | None,
    rules: WalletRules,
) -> ExitCard | None:
    """One sell, read against the rule. `None` is the ordinary answer and costs nothing.

    The order matters and is the order a reader would ask in: did this wallet actually get out (ratio or
    zero), is the position big enough to care about on its own, and if it is not, was anybody else
    following it into this token recently enough for the exit to be about them too.
    """

    established = int(balance.q_before_raw)
    sold = int(quantity_raw)
    share = ratio_bps(balance_before_raw=established, quantity_raw=sold)
    closed = established <= 0 or sold >= established
    if not (share > int(rules.exit_ratio_bps) or closed):
        return None
    if position_usd is None:
        # Nothing priced the position. A card that could not say what was sold in dollars is not the
        # card #572 §5.3 describes, and the check row already recorded that the sell was seen.
        return None
    cascade = int(cascade_wallets) >= 1 and position_usd >= rules.exit_cascade_min_usd
    if not (position_usd >= rules.exit_min_position_usd or cascade):
        return None
    if previous is None or previous.closed:
        # A new position segment. The segment is keyed by the movement that opened the reporting of it,
        # so a wallet that gets out, buys back in and gets out again is two cards rather than a
        # follow-up to a position that no longer exists.
        return ExitCard(
            ratio_bps=share,
            basis=balance.basis,
            closed=closed,
            balance_before_raw=established,
            quantity_raw=sold,
            position_usd=position_usd,
            cascade_wallets=int(cascade_wallets),
            cascade_usd=cascade_usd,
            segment_key=str(int(event_at_ms)),
            followup=False,
        )
    if _band(share) <= _band(previous.ratio_bps):
        return None
    return ExitCard(
        ratio_bps=share,
        basis=balance.basis,
        closed=closed,
        balance_before_raw=established,
        quantity_raw=sold,
        position_usd=position_usd,
        cascade_wallets=int(cascade_wallets),
        cascade_usd=cascade_usd,
        segment_key=previous.segment_key,
        followup=True,
    )


def _median_premium_bps(lead: CrowdingBuyer, followers: tuple[CrowdingBuyer, ...]) -> int | None:
    """The median follower's entry premium over the lead's, or `None` when nothing can be compared."""

    if lead.price is None or lead.price <= 0:
        return None
    premiums = sorted(
        _clamped_premium(buyer.price, lead.price) for buyer in followers if buyer.price is not None and buyer.price > 0
    )
    if not premiums:
        return None
    middle = len(premiums) // 2
    return premiums[middle] if len(premiums) % 2 else (premiums[middle - 1] + premiums[middle]) // 2


def _clamped_premium(price: Decimal, lead_price: Decimal) -> int:
    """One follower's premium over the lead, bounded to a figure a card can print and a column hold."""

    return max(-PREMIUM_BPS_MAX, min(PREMIUM_BPS_MAX, int((price - lead_price) / lead_price * BPS_FULL)))


def decide_crowding(
    *,
    buyers: tuple[CrowdingBuyer, ...],
    window_from_ms: int,
    previous: PreviousCrowding | None,
    rules: WalletRules,
) -> CrowdingCard | None:
    """One token's window, read against the rule. The window is the caller's; the count is this one's.

    Only wallets that both *opened* their position inside the window and put at least
    `crowding_min_usd` into it are counted. Both halves matter: a wallet already holding is not crowding
    in, and a $40 test buy is not a position.
    """

    qualified = tuple(
        buyer for buyer in buyers if buyer.first_at_ms >= int(window_from_ms) and buyer.usd >= rules.crowding_min_usd
    )
    if len(qualified) < max(1, int(rules.crowding_n)):
        return None
    ordered = tuple(sorted(qualified, key=lambda buyer: (buyer.first_at_ms, buyer.wallet)))
    lead, followers = ordered[0], ordered[1:]
    premium = _median_premium_bps(lead, followers)
    late = premium is not None and premium >= int(rules.crowding_premium_late_bps)
    total = sum((buyer.usd for buyer in ordered), Decimal(0))
    window_to = ordered[-1].first_at_ms
    if previous is None or lead.first_at_ms > previous.window_to_ms:
        return CrowdingCard(
            lead=lead,
            buyers=ordered,
            total_usd=total,
            premium_bps=premium,
            window_from_ms=lead.first_at_ms,
            window_to_ms=window_to,
            late=late,
            followup=False,
        )
    if len(ordered) < 2 * max(1, previous.buyers):
        return None
    return CrowdingCard(
        lead=lead,
        buyers=ordered,
        total_usd=total,
        premium_bps=premium,
        # A follow-up belongs to the window it follows, so the card the reader already has and this one
        # are one subject rather than two.
        window_from_ms=previous.window_from_ms,
        window_to_ms=window_to,
        late=late,
        followup=True,
    )


__all__ = [
    "BPS_FULL",
    "PREMIUM_BPS_MAX",
    "CrowdingBuyer",
    "CrowdingCard",
    "ExitCard",
    "PreviousCrowding",
    "PreviousExit",
    "WalletRules",
    "decide_crowding",
    "decide_exit",
    "is_live",
    "ratio_bps",
]
