"""What a wallet observation is: the derived event, its verification, and its price receipt (#572 PR-2).

A pure value module, and it lives at the top of `news` rather than inside `chain_tape` for one concrete
reason: the tape's rules produce these rows and `pipeline/admission` writes them, so both sides need the
shapes. Putting them under `chain_tape` would make the admission path import the package whose loop
imports the admission path.

Nothing here is a provider frame. A `wallet` market observation is derived by this process from chain
logs it already stored, which is why it has no Strategy id, no source-contract family and no parser --
and why every quantity is a raw integer in the token's own units, exactly as the fills it came from are.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal

# The provider label a wallet observation is stored under. It is the chain, read over public JSON-RPC.
WALLET_PROVIDER: Final = "robinhood_chain"
# The source a wallet Item belongs to. `news_items.item_id` is `sha256(source_id, source_item_key)`, so
# this is what keeps the chain's identity space and OpenNews's from overlapping.
WALLET_SOURCE_ID: Final = "news-robinhood-chain"

# What a derived wallet observation can be. Two rules produce a card about one movement -- "a followed
# wallet started getting out of this" or "several of them just got in at once" -- and the third kind is
# the periodic digest (#572 §5.4), which is about the window rather than about any one movement: it names
# no wallet and no token, and its subject is what the whole roster did in four hours.
WalletEventKind = Literal["buy", "exit", "crowding", "digest"]
BUY_STAGE_ZH: Final = {
    "first_observed": "首次观察买入",
    "new_position": "新建仓",
    "add": "加仓",
    "reentry": "重新买入",
    "unknown": "仓位历史不足",
}
# The digest's own kind value, as a constant rather than a literal repeated in five SQL statements and
# three modules. It is a stored value, so the one place it is written down is the one place it can move.
DIGEST_KIND: Final = "digest"
# How many lines a digest may carry, and how few it may be reduced to and still be worth sending rather
# than the deterministic template. They are here because the Program that produces the lines and the
# tape that checks them both need the same two numbers, and neither may import the other.
DIGEST_LINES_MAX: Final = 8
DIGEST_LINES_MIN: Final = 3

# Where the denominator of an exit ratio came from. `chain_balance` is `balanceOf` at the block before
# the sell -- the chain's own answer, which the public node holds for about ten minutes. `site_reported`
# is the provider's current bag for that wallet and token plus the amount just sold: an arithmetic
# reconstruction, labelled as one on the card. The relaxed rule is deliberate -- #572's 2026-09-06
# decision chose a card with a stated basis over the previous "unverified means no card".
CheckBasis = Literal["chain_balance", "site_reported"]

# The two horizons a card's price receipt is taken at, and how far after the send each one falls.
OutcomeHorizon = Literal["15m", "1h", "4h"]
WALLET_OUTCOME_HORIZONS: Final[tuple[tuple[OutcomeHorizon, int], ...]] = (
    ("15m", 900_000),
    ("1h", 3_600_000),
    ("4h", 14_400_000),
)
# What an outcome row says when the horizon passed and nothing could price the token for a whole day. A
# row rather than a silence: "we looked and could not price it" is a different fact from "not due yet",
# and the absence of a row is what "not due yet" means.
OUTCOME_UNAVAILABLE: Final = "unavailable"
# A new source identity attests the adapter checked chain and base-token address.
VERIFIED_WALLET_PRICE_SOURCE: Final = "dexscreener_base_token_v1"
# The smallest figure the receipt column can hold. `price` is `numeric(38,18)`, so anything under half
# of this rounds to zero on the way in and the column's own `price > 0` refuses the row -- which is the
# same defect as a price of zero, one representation further down. It is reachable: the recorded
# DexScreener answer for FSD carries pools printing 2.94e-27, and a token whose every pool reports no
# liquidity is priced off whichever of them the ranking lands on. A figure this small is not a price
# anyway; it is the pool saying it has nothing in it.
OUTCOME_PRICE_MIN: Final = Decimal("1e-18")
# How late a receipt may be taken and still be *this horizon's* receipt. A price read three hours after
# the one-hour mark is not the one-hour number, so a horizon nothing could price within this grace is
# banked as `unavailable` rather than retried into a figure that answers a different question -- and
# banking it is also what stops an unpriceable token from occupying the turn's receipt budget for a day.
OUTCOME_GIVE_UP_MS: Final = 15 * 60_000


@dataclass(frozen=True, slots=True)
class DigestLine:
    """A program-rendered fact line with immutable citations; the model only selects fact IDs."""

    text: str
    cites: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WalletBalance:
    """The denominator of one exit, and the honest statement of where it came from.

    `q_before_raw` is a raw integer in the token's own units, like every other quantity on this path, so
    the ratio is computed from two exact integers rather than from two floats.
    """

    q_before_raw: int
    basis: CheckBasis
    block_hash: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WalletEvent:
    """One derived observation: the row a card is built from, beside its `news_items` parent.

    Every number here was computed by this process from stored fills, the chain and the provider's own
    figures -- no model, and nothing a card can move. `evidence` carries the fill identities the rule
    read, so "which movements is this card about" is answerable from the row itself rather than from a
    reconstruction of the rule.
    """

    item_id: str
    kind: WalletEventKind
    chain_id: int
    wallet: str
    handle: str
    followers: int
    token: str
    token_symbol: str | None
    token_decimals: int | None
    roster_version: int
    window_from_ms: int
    window_to_ms: int
    segment_key: str
    event_at_ms: int
    received_at_ms: int
    title: str = ""
    tone: str = ""
    ratio_bps: int | None = None
    basis: CheckBasis | None = None
    quantity_raw: int | None = None
    balance_before_raw: int | None = None
    usd: Decimal | None = None
    position_usd: Decimal | None = None
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    peer_wallets: int = 0
    peer_usd: Decimal | None = None
    premium_bps: int | None = None
    liquidity_usd: Decimal | None = None
    tx_hash: str | None = None
    block_number: int | None = None
    closed: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)
    provider: str = WALLET_PROVIDER


@dataclass(frozen=True, slots=True)
class WalletCheck:
    """One verification attempt against one sell fill, recorded whatever it proved.

    Written for every sell the rule looked at, including the ones that produced no card: "how often does
    the public node still hold the state" is a question only the failures can answer, and it is the
    evidence behind the `site_reported` label a card prints when it does not.
    """

    chain_id: int
    tx_hash: str
    log_index: int
    basis: CheckBasis
    q_before_raw: int | None
    q_sell_raw: int
    ratio_bps: int | None
    block_hash: str
    checked_at_ms: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WalletOutcome:
    """An observation's price receipt, including candidates that were never notified."""

    item_id: str
    horizon: OutcomeHorizon
    price: Decimal | None
    at_ms: int
    source: str
    reference_price: Decimal | None
    reference_at_ms: int
    target_at_ms: int
    reference_kind: str = "observed"
    delivery_key: str | None = None


__all__ = [
    "DIGEST_KIND",
    "DIGEST_LINES_MAX",
    "DIGEST_LINES_MIN",
    "OUTCOME_GIVE_UP_MS",
    "OUTCOME_PRICE_MIN",
    "OUTCOME_UNAVAILABLE",
    "WALLET_OUTCOME_HORIZONS",
    "WALLET_PROVIDER",
    "WALLET_SOURCE_ID",
    "CheckBasis",
    "DigestLine",
    "OutcomeHorizon",
    "WalletBalance",
    "WalletCheck",
    "WalletEvent",
    "WalletEventKind",
    "WalletOutcome",
]
