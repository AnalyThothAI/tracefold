"""The single current wallet product: a token's net-buy episode (#641, one rule since #649 PR-3)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict

WALLET_PROVIDER: Final = "robinhood_chain"
WALLET_SOURCE_ID: Final = "news-robinhood-chain"
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
    roster_version: int | None
    roster_known_at_ms: int | None
    monitoring_from_ms: int | None
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
