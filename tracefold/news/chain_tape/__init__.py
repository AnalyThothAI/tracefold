"""The Robinhood Chain wallet tape: roster, chain logs, classified fills (#572 PR-1).

Store-only in this PR. Nothing here opens a News Item, decides a card or sends anything: the loop
writes `news_market_wallet_fills` and `news_market_wallet_roster` and stops, so a week of real
on-chain counts can calibrate the thresholds before any rule is written (#572 §6.4, §7).
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
