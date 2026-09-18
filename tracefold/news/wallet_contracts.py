"""The single current wallet product: a token's net-buy episode (#641, one rule since #649 PR-3)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

WALLET_PROVIDER: Final = "robinhood_chain"
WALLET_SOURCE_ID: Final = "news-robinhood-chain"
OutcomeHorizon = Literal["15m", "1h", "4h"]
WALLET_OUTCOME_HORIZONS: Final[tuple[tuple[OutcomeHorizon, int], ...]] = (
    ("15m", 900_000),
    ("1h", 3_600_000),
    ("4h", 14_400_000),
)
OUTCOME_MAX_DELAY_MS: Final = 60_000
# How long after the trigger a first price still counts as *the trigger's* price. It is one fast
# window: past it the sample describes a later market, so the episode keeps no baseline at all rather
# than acquiring a misleading one, and an episode older than this is never backfilled (#649 §8).
REFERENCE_MAX_DELAY_MS: Final = 300_000
# The one trigger window. There used to be two -- a 5m quorum of three beside this one -- and a card
# had to say which of them had fired before it could say anything else. One window is one sentence:
# five roster addresses, thirty minutes, a thousand dollars each (#649 PR-3 §2).
NET_BUY_WINDOW_MS: Final = 1_800_000
# How far back a member's own participation is counted for the card's third fact. Fourteen days is
# long enough that a recurring buyer shows up as one and short enough that the count describes the
# roster as it is now; the replay that sized the rule covered the same fourteen days.
PARTICIPATION_WINDOW_MS: Final = 1_209_600_000
# Under this on-chain age at the trigger the token is labelled a new listing. It is a label on the
# card and never a filter: an older token with five concentrated buyers is the same alert.
NEW_LAUNCH_MAX_AGE_MS: Final = 3_600_000


class NetBuyMember(BaseModel):
    """One address, one window, one arithmetic set, including exclusions."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    wallet: str
    handle: str
    rank_quality: int | None
    roster_version: int | None
    roster_known_at_ms: int | None
    monitoring_from_ms: int | None
    source_closed_trades: int | None
    source_profit_factor: str | None
    # How many episodes this address was a qualified member of in the fourteen days before this
    # snapshot's cutoff -- the card's "have these addresses done this before" fact, counted from the
    # stored episodes themselves. `None` is "not counted", which only a snapshot written before the
    # count existed carries.
    recent_episodes: int | None
    buy_usd: Decimal
    sell_usd: Decimal
    net_usd: Decimal | None
    buy_token_raw: str
    sell_token_raw: str
    net_token_raw: str
    unpriced_count: int
    transfer_out_count: int
    qualified: bool
    reasons: tuple[str, ...]


class NetBuyWindow(BaseModel):
    """The one window, from its own two ends. There is no window name to choose between any more."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    from_ms: int
    to_ms: int
    required_n: int
    qualified_n: int
    buy_usd: Decimal
    sell_usd: Decimal
    net_usd: Decimal
    matched: bool
    members: tuple[NetBuyMember, ...]


class NetBuySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    chain_id: int
    token: str
    token_symbol: str | None
    token_decimals: int | None
    cutoff_at_ms: int
    cutoff_block: int
    cutoff_log: int
    roster_version: int | None
    min_net_buy_usd: Decimal
    coverage_from_ms: int | None
    coverage_gap_at_ms: int | None
    # The earliest movement in this token the tape holds, which is how old the token is as far as this
    # repository can honestly say: the collector only ever sees roster addresses, so it is an upper
    # bound on the token's age and never a claim about the chain's own first block. `None` is "the
    # tape holds nothing for this token", and the card says the age is unknown rather than guessing.
    token_first_seen_at_ms: int | None
    window: NetBuyWindow

    @property
    def matched(self) -> bool:
        return self.window.matched

    @property
    def token_age_ms(self) -> int | None:
        """How old the token was at this snapshot's cutoff, or None when nothing dates it."""

        if self.token_first_seen_at_ms is None:
            return None
        return max(0, self.cutoff_at_ms - self.token_first_seen_at_ms)

    @property
    def new_launch(self) -> bool:
        age = self.token_age_ms
        return age is not None and age < NEW_LAUNCH_MAX_AGE_MS


@dataclass(frozen=True, slots=True)
class WalletEvent:
    """First trigger is immutable. Only the detector writes the current snapshot."""

    item_id: str
    chain_id: int
    token: str
    token_symbol: str | None
    trigger_tx_hash: str
    event_at_ms: int
    received_at_ms: int
    detected_at_ms: int
    initial_snapshot: NetBuySnapshot
    trigger_max_age_s: int
    notification_eligible: bool
    notification_reason: str | None
    reference_price: Decimal | None = None
    reference_at_ms: int | None = None
    reference_source: str | None = None


@dataclass(frozen=True, slots=True)
class WalletReference:
    """The episode's t0 baseline: the first price that was really available after the trigger.

    `at_ms` is the moment the provider answered, not the trigger, so the delay the baseline carries is
    recorded rather than hidden -- `at_ms - trigger_at_ms` is the whole of it, and both halves are
    stored columns. Only the price sampler builds one, and only once per episode.
    """

    item_id: str
    price: Decimal
    at_ms: int
    source: str
    trigger_at_ms: int

    @property
    def delay_ms(self) -> int:
        return self.at_ms - self.trigger_at_ms


@dataclass(frozen=True, slots=True)
class WalletOutcome:
    item_id: str
    horizon: OutcomeHorizon
    price: Decimal | None
    at_ms: int
    source: str
    reference_price: Decimal | None
    reference_at_ms: int | None
    target_at_ms: int
    status: str
    delivery_key: str | None = None
