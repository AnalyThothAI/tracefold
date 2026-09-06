"""Which wallets the tape follows, from the provider rows it actually returned (#572 decision 5).

The ten traders here are real rows from `/api/traders?window=7d&stocks=false` recorded on 2026-09-06,
chosen to cover each side of every rule: enough closes and a passing profit factor, enough closes and a
failing one, large positions with too few closes to qualify, and one address that is on both lists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tracefold.integrations.robinhoodtrenches import RosterCandidate
from tracefold.news.chain_tape.roster import (
    MIN_CLOSED_TRADES,
    MIN_PROFIT_FACTOR,
    TOP_QUALITY,
    TOP_WHALE_BY_OPEN_COST,
    RosterRules,
    quality_candidates,
    select_roster,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "chain_tape"


def _candidates() -> tuple[RosterCandidate, ...]:
    rows = json.loads((FIXTURES / "traders_window_7d.json").read_text(encoding="utf-8"))
    return tuple(
        RosterCandidate(
            address=str(row["address"]),
            handle=str(row["handle"]),
            followers=int(row["followers"]),
            realized_pnl=float(row["realized_pnl"]),
            closed_trades=int(row["closed_trades"]),
            win_rate=float(row["win_rate"]),
            open_cost=float(row["open_cost"]),
        )
        for row in rows
    )


def _profit_factors() -> dict[str, float | None]:
    stats = json.loads((FIXTURES / "trader_stats.json").read_text(encoding="utf-8"))
    return {handle: document["stats"].get("profit_factor") for handle, document in stats.items()}


def _by_handle(members: Any, candidates: tuple[RosterCandidate, ...]) -> dict[str, Any]:
    handles = {row.address: row.handle for row in candidates}
    return {handles[member.wallet]: member for member in members}


def test_the_documented_defaults_are_the_decision_the_issue_recorded() -> None:
    assert (MIN_CLOSED_TRADES, MIN_PROFIT_FACTOR, TOP_QUALITY, TOP_WHALE_BY_OPEN_COST) == (10, 1.2, 20, 20)


def test_only_rows_past_the_closed_trade_floor_cost_a_per_trader_request() -> None:
    """The profit factor lives on one endpoint per handle, so the cheap filter runs first."""

    candidates = _candidates()
    asked = {row.handle for row in quality_candidates(candidates, rules=RosterRules())}

    assert asked == {
        "frankdegods",
        "rasmr",
        "smol_intern",
        "nosanityxbt",
        "397397",
        "bluntz_capital",
        "FartmanSacks",
        "Aurelius0121",
    }
    assert "0xleo" not in asked
    assert "MEADGod" not in asked


def test_the_quality_list_needs_both_the_closes_and_the_factor_and_ranks_by_realized_pnl() -> None:
    candidates = _candidates()
    members = select_roster(candidates, profit_factors=_profit_factors(), rules=RosterRules())
    by_handle = _by_handle(members, candidates)

    quality = sorted(
        ((member.rank_quality, handle) for handle, member in by_handle.items() if member.rank_quality),
    )
    assert quality == [(1, "frankdegods"), (2, "rasmr"), (3, "smol_intern"), (4, "nosanityxbt")]
    # Enough closes, factor below the floor: not on the quality list at any rank.
    assert by_handle["397397"].rank_quality is None
    assert by_handle["bluntz_capital"].rank_quality is None


def test_the_whale_list_ranks_by_open_cost_and_asks_nothing_about_performance() -> None:
    candidates = _candidates()
    members = select_roster(candidates, profit_factors=_profit_factors(), rules=RosterRules())
    by_handle = _by_handle(members, candidates)

    whales = sorted((member.rank_whale, handle) for handle, member in by_handle.items() if member.rank_whale)
    assert [handle for _rank, handle in whales[:3]] == ["FartmanSacks", "Aurelius0121", "bluntz_capital"]
    # A losing whale is still a whale: what it holds is the signal, not how it did.
    assert by_handle["FartmanSacks"].realized_pnl < 0
    assert by_handle["FartmanSacks"].rank_quality is None


def test_a_wallet_on_both_lists_carries_both_ranks() -> None:
    candidates = _candidates()
    members = select_roster(candidates, profit_factors=_profit_factors(), rules=RosterRules())
    frank = _by_handle(members, candidates)["frankdegods"]

    assert frank.rank_quality == 1
    assert frank.rank_whale is not None


def test_the_union_is_ordered_by_wallet_and_carries_the_lists_own_statistics() -> None:
    """Selection is total and deterministic: the same rows produce the same members in the same order."""

    candidates = _candidates()
    members = select_roster(candidates, profit_factors=_profit_factors(), rules=RosterRules())

    assert [member.wallet for member in members] == sorted(member.wallet for member in members)
    frank = _by_handle(members, candidates)["frankdegods"]
    assert (frank.closed_trades, round(frank.realized_pnl)) == (50, 510_047)
    assert frank.profit_factor is not None
    assert round(frank.profit_factor, 4) == 1.5653


def test_a_handle_the_site_would_not_answer_for_cannot_pass_the_quality_rule() -> None:
    """An unknown profit factor is not a passing one -- but it does not remove a whale."""

    candidates = _candidates()
    members = select_roster(candidates, profit_factors={}, rules=RosterRules())
    by_handle = _by_handle(members, candidates)

    assert all(member.rank_quality is None for member in members)
    assert by_handle["FartmanSacks"].rank_whale == 1


def test_tightening_the_rules_shrinks_the_lists_without_changing_their_shape() -> None:
    candidates = _candidates()
    rules = RosterRules(min_closed_trades=10, min_profit_factor=1.2, top_quality=2, top_whale_by_open_cost=1)
    members = select_roster(candidates, profit_factors=_profit_factors(), rules=rules)
    by_handle = _by_handle(members, candidates)

    assert sorted(by_handle) == ["FartmanSacks", "frankdegods", "rasmr"]
    assert by_handle["FartmanSacks"].rank_whale == 1
    assert by_handle["rasmr"].rank_quality == 2
