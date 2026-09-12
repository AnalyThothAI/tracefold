"""Two fixed sliding windows over the same complete receipt facts (#641)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Final, Literal

from ..wallet_contracts import NetBuyMember, NetBuySnapshot, NetBuyWindow
from .contracts import ClassifiedFill

FAST_WINDOW_MS: Final = 300_000
SLOW_WINDOW_MS: Final = 1_800_000


@dataclass(frozen=True, slots=True)
class WalletRules:
    net_buy_fast_n: int = 3
    net_buy_slow_n: int = 5
    min_net_buy_usd: Decimal = Decimal("1000")
    trigger_max_age_s: int = 60

    def __post_init__(self) -> None:
        if min(self.net_buy_fast_n, self.net_buy_slow_n) < 2:
            raise ValueError("net_buy_n_must_be_at_least_two")
        if not self.min_net_buy_usd.is_finite() or self.min_net_buy_usd <= 0:
            raise ValueError("min_net_buy_usd_must_be_positive")
        if self.trigger_max_age_s <= 0:
            raise ValueError("trigger_max_age_s_must_be_positive")


def trigger_age_reason(*, event_at_ms: int, received_at_ms: int, now_ms: int, max_age_s: int) -> str | None:
    ages = (received_at_ms - event_at_ms, now_ms - event_at_ms)
    if min(ages) < 0:
        return "future_chain_timestamp"
    if max(ages) > max_age_s * 1000:
        return "stale_trigger"
    return None


def calculate_windows(
    *,
    fills: Sequence[ClassifiedFill],
    members: Sequence[Mapping[str, Any]],
    chain_id: int,
    token: str,
    cutoff_at_ms: int,
    cutoff_block: int,
    cutoff_log: int,
    coverage_from_ms: int | None,
    coverage_gap_at_ms: int | None,
    roster_version: int | None,
    rules: WalletRules,
) -> NetBuySnapshot:
    """No query, price, balance, model or wall clock is needed to evaluate a receipt."""

    token = token.lower()
    relevant = tuple(
        fill
        for fill in fills
        if fill.chain_id == chain_id
        and fill.token.lower() == token
        and cutoff_at_ms - SLOW_WINDOW_MS < fill.event_at_ms <= cutoff_at_ms
        and (fill.block_number, fill.log_index) <= (cutoff_block, cutoff_log)
    )
    relevant = tuple({(f.chain_id, f.tx_hash, f.log_index): f for f in relevant}.values())
    roster = {str(member["wallet"]).lower(): member for member in members}
    with localcontext() as context:
        context.prec = 100
        fast = _window(
            relevant,
            roster,
            window="5m",
            duration=FAST_WINDOW_MS,
            required_n=rules.net_buy_fast_n,
            cutoff_at_ms=cutoff_at_ms,
            coverage_from_ms=coverage_from_ms,
            coverage_gap_at_ms=coverage_gap_at_ms,
            rules=rules,
        )
        slow = _window(
            relevant,
            roster,
            window="30m",
            duration=SLOW_WINDOW_MS,
            required_n=rules.net_buy_slow_n,
            cutoff_at_ms=cutoff_at_ms,
            coverage_from_ms=coverage_from_ms,
            coverage_gap_at_ms=coverage_gap_at_ms,
            rules=rules,
        )
    newest = max(relevant, key=lambda f: (f.block_number, f.log_index), default=None)
    return NetBuySnapshot(
        chain_id=chain_id,
        token=token,
        token_symbol=None if newest is None else newest.token_symbol,
        token_decimals=None if newest is None else newest.token_decimals,
        cutoff_at_ms=cutoff_at_ms,
        cutoff_block=cutoff_block,
        cutoff_log=cutoff_log,
        roster_version=roster_version,
        min_net_buy_usd=rules.min_net_buy_usd,
        coverage_from_ms=coverage_from_ms,
        coverage_gap_at_ms=coverage_gap_at_ms,
        fast=fast,
        slow=slow,
    )


def _window(
    fills: Sequence[ClassifiedFill],
    roster: Mapping[str, Mapping[str, Any]],
    *,
    window: Literal["5m", "30m"],
    duration: int,
    required_n: int,
    cutoff_at_ms: int,
    coverage_from_ms: int | None,
    coverage_gap_at_ms: int | None,
    rules: WalletRules,
) -> NetBuyWindow:
    start = cutoff_at_ms - duration
    by_wallet: dict[str, list[ClassifiedFill]] = defaultdict(list)
    for fill in fills:
        if fill.event_at_ms > start:
            by_wallet[fill.wallet.lower()].append(fill)
    rows = []
    for wallet, movements in sorted(by_wallet.items()):
        member = roster.get(wallet)
        buys = [fill for fill in movements if fill.kind == "buy"]
        sells = [fill for fill in movements if fill.kind == "sell"]
        buy_usd = sum((fill.usd for fill in buys if fill.usd is not None), Decimal(0))
        sell_usd = sum((fill.usd for fill in sells if fill.usd is not None), Decimal(0))
        buy_raw = sum(fill.amount_raw for fill in buys)
        sell_raw = sum(fill.amount_raw for fill in sells)
        unpriced = sum(fill.usd is None for fill in (*buys, *sells))
        transfers = sum(fill.kind == "transfer_out" for fill in movements)
        reasons = []
        if member is None or member["rank_quality"] is None:
            reasons.append("not_quality_roster")
        monitoring = None if member is None else member["monitoring_from_ms"]
        if coverage_from_ms is None or coverage_from_ms > start or monitoring is None or monitoring > start:
            reasons.append("incomplete_monitoring_window")
        if coverage_gap_at_ms is not None and coverage_gap_at_ms > start:
            reasons.append("collection_gap")
        if unpriced:
            reasons.append("unpriced_trade")
        if transfers:
            reasons.append("transfer_out_incomplete")
        net = None if unpriced or transfers else buy_usd - sell_usd
        if net is not None and net < rules.min_net_buy_usd:
            reasons.append("below_min_net_buy")
        if buy_raw - sell_raw <= 0:
            reasons.append("nonpositive_net_quantity")
        rows.append(
            NetBuyMember(
                wallet=wallet,
                handle="" if member is None else str(member["handle"]),
                rank_quality=None if member is None else member["rank_quality"],
                roster_version=None if member is None else int(member["roster_version"]),
                roster_known_at_ms=None if member is None else int(member["known_at_ms"]),
                monitoring_from_ms=monitoring,
                source_closed_trades=None if member is None else int(member["closed_trades"]),
                source_profit_factor=None
                if member is None or member["profit_factor"] is None
                else str(member["profit_factor"]),
                buy_usd=buy_usd,
                sell_usd=sell_usd,
                net_usd=net,
                buy_token_raw=str(buy_raw),
                sell_token_raw=str(sell_raw),
                net_token_raw=str(buy_raw - sell_raw),
                unpriced_count=unpriced,
                transfer_out_count=transfers,
                qualified=not reasons,
                reasons=tuple(reasons),
            )
        )
    qualified = [row for row in rows if row.qualified]
    buy_total = sum((row.buy_usd for row in qualified), Decimal(0))
    sell_total = sum((row.sell_usd for row in qualified), Decimal(0))
    return NetBuyWindow(
        window=window,
        from_ms=start,
        to_ms=cutoff_at_ms,
        required_n=required_n,
        qualified_n=len(qualified),
        matched=len(qualified) >= required_n,
        buy_usd=buy_total,
        sell_usd=sell_total,
        net_usd=buy_total - sell_total,
        members=tuple(rows),
    )


def effective_buy(fills: Sequence[ClassifiedFill], snapshot: NetBuySnapshot) -> bool:
    """A new priced buy must increase a currently qualified member's net investment."""

    with localcontext() as context:
        context.prec = 100
        for window in (snapshot.fast, snapshot.slow):
            for member in window.members:
                if not member.qualified:
                    continue
                changes = [fill for fill in fills if fill.wallet.lower() == member.wallet]
                net_change = Decimal(0)
                has_buy = False
                for fill in changes:
                    if fill.usd is not None:
                        if fill.kind == "buy":
                            has_buy = True
                            net_change += fill.usd
                        elif fill.kind == "sell":
                            net_change -= fill.usd
                if has_buy and net_change > 0:
                    return True
    return False
