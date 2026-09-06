"""Which wallets the tape follows, and why those (#572 decision 5, §3.2).

Two lists, one union:

* **quality** -- at least `min_closed_trades` closed trades and a profit factor of at least
  `min_profit_factor`, then the top `top_quality` by realized profit and loss.
* **whale** -- the top `top_whale` by open position cost, with no performance filter at all.

Win rate is deliberately not a criterion. Over the 87 addresses with five or more closes, the rank
correlation between win rate and realized P&L was 0.31, and four of the nine addresses with a win rate
above 0.6 were losing money -- one of them by 221,000 dollars on a 0.69 win rate. A list built on win
rate would have been a list of people who sell winners early (#572 §3.2).

Selection is pure and total: given the same provider rows it produces the same members in the same
order, which is what makes "did the roster change" a comparison rather than a judgement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .contracts import RosterMember

MIN_CLOSED_TRADES: Final = 10
MIN_PROFIT_FACTOR: Final = 1.2
TOP_QUALITY: Final = 20
TOP_WHALE_BY_OPEN_COST: Final = 20


@dataclass(frozen=True, slots=True)
class RosterRules:
    """The four numbers that decide the list. Operator-tunable, and recorded with the version they made."""

    min_closed_trades: int = MIN_CLOSED_TRADES
    min_profit_factor: float = MIN_PROFIT_FACTOR
    top_quality: int = TOP_QUALITY
    top_whale_by_open_cost: int = TOP_WHALE_BY_OPEN_COST


def quality_candidates(candidates: Sequence[Any], *, rules: RosterRules) -> tuple[Any, ...]:
    """The rows worth spending a per-trader request on: the closed-trade floor, from the list alone.

    The profit factor lives only on `/api/trader/{handle}`, so the floor is applied first and the site
    is asked about a bounded subset rather than about all 147 addresses.
    """

    return tuple(row for row in candidates if int(getattr(row, "closed_trades", 0)) >= rules.min_closed_trades)


def select_roster(
    candidates: Sequence[Any],
    *,
    profit_factors: Mapping[str, float | None],
    rules: RosterRules,
) -> tuple[RosterMember, ...]:
    """The union of the two lists, each member carrying the ranks that selected it.

    `profit_factors` is keyed by handle because that is the key the per-trader endpoint answers to. A
    wallet with no answer cannot pass the quality rule -- an unknown profit factor is not a passing one
    -- but it can still be a whale, which is exactly what the whale list is for.
    """

    quality_pool = [
        row
        for row in quality_candidates(candidates, rules=rules)
        if _passes_profit_factor(profit_factors.get(str(getattr(row, "handle", ""))), rules.min_profit_factor)
    ]
    quality_pool.sort(key=lambda row: (-float(getattr(row, "realized_pnl", 0.0)), str(getattr(row, "address", ""))))
    quality = {str(row.address): index + 1 for index, row in enumerate(quality_pool[: max(0, int(rules.top_quality))])}

    whale_pool = sorted(
        candidates,
        key=lambda row: (-float(getattr(row, "open_cost", 0.0)), str(getattr(row, "address", ""))),
    )
    whale = {
        str(row.address): index + 1 for index, row in enumerate(whale_pool[: max(0, int(rules.top_whale_by_open_cost))])
    }

    selected = quality.keys() | whale.keys()
    by_address = {str(row.address): row for row in candidates}
    return tuple(
        RosterMember(
            wallet=address,
            handle=str(getattr(by_address[address], "handle", "") or ""),
            followers=int(getattr(by_address[address], "followers", 0)),
            realized_pnl=float(getattr(by_address[address], "realized_pnl", 0.0)),
            closed_trades=int(getattr(by_address[address], "closed_trades", 0)),
            win_rate=float(getattr(by_address[address], "win_rate", 0.0)),
            profit_factor=profit_factors.get(str(getattr(by_address[address], "handle", ""))),
            open_cost=float(getattr(by_address[address], "open_cost", 0.0)),
            rank_quality=quality.get(address),
            rank_whale=whale.get(address),
        )
        for address in sorted(selected)
    )


def _passes_profit_factor(value: float | None, floor: float) -> bool:
    return value is not None and float(value) >= float(floor)


__all__ = [
    "MIN_CLOSED_TRADES",
    "MIN_PROFIT_FACTOR",
    "TOP_QUALITY",
    "TOP_WHALE_BY_OPEN_COST",
    "RosterRules",
    "quality_candidates",
    "select_roster",
]
