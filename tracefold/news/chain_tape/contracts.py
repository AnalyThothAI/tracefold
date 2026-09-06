"""What the wallet tape stores, and the on-chain identities it recognises (#572 §5.2).

Every amount here is a raw integer in the token's own units. No float ever touches a stored quantity:
an 18-decimal balance does not survive a `float`, and the whole point of the sell rule this feeds in
PR-2 is comparing two such quantities exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

# Telemetry name and the two provider labels this flow reports under.
CHAIN_TAPE_NAME: Final = "chain_tape"
CHAIN_TAPE_PROVIDER: Final = "robinhood_chain"
ROSTER_PROVIDER: Final = "robinhoodtrenches"

FillKind = Literal["buy", "sell", "transfer_out"]
FILL_KINDS: Final[tuple[FillKind, ...]] = ("buy", "sell", "transfer_out")

# Uniswap V3's `Swap(address,address,int256,int256,uint160,uint128,int24)` and V4's
# `Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)`. Their presence in the same
# receipt is what separates a trade from a plain token movement; the amounts are read off the ERC-20
# `Transfer` logs, not off these, because a multi-hop route emits one `Swap` per pool and one `Transfer`
# per leg while the wallet appears in exactly one leg (#572 §3.3).
UNISWAP_V3_SWAP_TOPIC: Final = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
UNISWAP_V4_SWAP_TOPIC: Final = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
SWAP_TOPICS: Final[frozenset[str]] = frozenset({UNISWAP_V3_SWAP_TOPIC, UNISWAP_V4_SWAP_TOPIC})

# The stablecoin the routed trades settle in, pinned by full address after reading its own `symbol`,
# `name` and `decimals` from chain on 2026-09-06: `USDG`, "Global Dollar", 6 decimals. #572 §3.3 called
# this leg USDC from its 6-decimal shape and its address tail; the contract says otherwise, and the
# recorded `eth_call` fixture beside the classifier tests is the evidence. It is still a 1:1 US-dollar
# stablecoin, so its face value is a dollar figure -- which is exactly what `usd_source` records, rather
# than a claim that some price feed was consulted.
STABLE_CASH_TOKEN: Final = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
USD_SOURCE_STABLE_CASH_LEG: Final = "usdg_cash_leg"


@dataclass(frozen=True, slots=True)
class ClassifiedFill:
    """One roster wallet's `Transfer` in one receipt, read as what it was.

    Identity is `(chain_id, tx_hash, log_index)`: the chain already assigns exactly one of those per
    movement, so re-reading a block range can only write the same row again.

    `token_decimals` and `cash_decimals` are recorded beside the raw amounts rather than applied to
    them. A token that answers no `decimals()` still gets a faithful row; a reader that wants a human
    number divides, and a reader that wants to compare quantities does not.

    `block_hash` is recorded as evidence, not as reorg handling. PR-1 does not detect a reorg: it drops
    logs the node itself marks `removed`, and stores the hash the fill was read under so a PR-2
    correction path can tell a row on the canonical chain from one that is not.
    """

    chain_id: int
    tx_hash: str
    log_index: int
    block_number: int
    block_hash: str
    wallet: str
    token: str
    kind: FillKind
    amount_raw: int
    event_at_ms: int
    received_at_ms: int
    classified_at_ms: int
    roster_version: int
    token_symbol: str | None = None
    token_decimals: int | None = None
    cash_token: str | None = None
    cash_amount_raw: int | None = None
    cash_decimals: int | None = None
    usd: Decimal | None = None
    usd_source: str | None = None
    provider: str = CHAIN_TAPE_PROVIDER


@dataclass(frozen=True, slots=True)
class RosterMember:
    """One wallet in one roster version, with the ranks that put it there.

    A member can hold both ranks: the two lists overlapped by five addresses on the day the rules were
    chosen. `None` in a rank means "this list did not select this wallet", which is not the same as
    rank 0.
    """

    wallet: str
    handle: str
    followers: int
    realized_pnl: float
    closed_trades: int
    win_rate: float
    profit_factor: float | None
    open_cost: float
    rank_quality: int | None
    rank_whale: int | None


@dataclass(frozen=True, slots=True)
class RosterSnapshot:
    """One version of the roster: the union of the quality and whale lists, and when it was taken."""

    roster_version: int
    taken_at_ms: int
    members: tuple[RosterMember, ...]
    provider: str = ROSTER_PROVIDER

    @property
    def wallets(self) -> tuple[str, ...]:
        return tuple(member.wallet for member in self.members)

    def membership_key(self) -> tuple[tuple[str, int | None, int | None], ...]:
        """What "the roster changed" means: the members and their ranks, and nothing else.

        Follower counts and P&L move every hour and are recorded, not versioned on: a new version every
        hour would make `roster_version` on a fill meaningless as evidence of which list produced it.
        """

        return tuple(sorted((member.wallet, member.rank_quality, member.rank_whale) for member in self.members))


@dataclass(frozen=True, slots=True)
class TapeCursor:
    """How far the tape has been classified, as an exclusive position in `(block, transaction index)`.

    A block number alone cannot express "this block is half done", and one block can hold more roster
    transactions than a turn may fetch receipts for. The pair can, so a bounded turn always makes
    progress and a restart never re-fetches what it already classified.
    """

    block_number: int
    transaction_index: int

    def precedes(self, block_number: int, transaction_index: int) -> bool:
        return (self.block_number, self.transaction_index) < (block_number, transaction_index)


# "The whole of this block is classified". Above any real transaction index, and small enough to stay an
# ordinary `integer` column.
BLOCK_COMPLETE_TX_INDEX: Final = 2_147_483_647


__all__ = [
    "BLOCK_COMPLETE_TX_INDEX",
    "CHAIN_TAPE_NAME",
    "CHAIN_TAPE_PROVIDER",
    "FILL_KINDS",
    "ROSTER_PROVIDER",
    "STABLE_CASH_TOKEN",
    "SWAP_TOPICS",
    "UNISWAP_V3_SWAP_TOPIC",
    "UNISWAP_V4_SWAP_TOPIC",
    "USD_SOURCE_STABLE_CASH_LEG",
    "ClassifiedFill",
    "FillKind",
    "RosterMember",
    "RosterSnapshot",
    "TapeCursor",
]
