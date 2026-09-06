"""The Robinhood Chain wallet tape: roster, chain logs, classified fills, and the two card rules.

One turn does both halves. `loop` reads the chain and writes `news_market_wallet_fills` and
`news_market_wallet_roster` (#572 PR-1); `derive` then reads those fills against `rules` and opens one
ordinary market Item per card through the same admission transaction every other market kind uses, and
fills in the +1h/+4h price receipts for cards already sent (#572 PR-2).

`derive` is deliberately not exported here. It reaches the admission path, which reaches the concrete
News repository, which is the module that imports this package -- so the composition root imports it
directly and the loop sees it as a `WalletCardPort`.
"""

from __future__ import annotations

from .contracts import (
    CHAIN_TAPE_NAME,
    CHAIN_TAPE_PROVIDER,
    FILL_KINDS,
    ROSTER_PROVIDER,
    ClassifiedFill,
    RosterMember,
    RosterSnapshot,
    TapeCursor,
)
from .loop import ChainTapeLoop

__all__ = [
    "CHAIN_TAPE_NAME",
    "CHAIN_TAPE_PROVIDER",
    "FILL_KINDS",
    "ROSTER_PROVIDER",
    "ChainTapeLoop",
    "ClassifiedFill",
    "RosterMember",
    "RosterSnapshot",
    "TapeCursor",
]
