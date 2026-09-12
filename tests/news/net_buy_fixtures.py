"""Synthetic chain facts shared by rule and channel contract tests."""

from dataclasses import replace
from decimal import Decimal
from typing import Any

from tracefold.news.chain_tape.contracts import STABLE_CASH_TOKEN, ClassifiedFill
from tracefold.news.chain_tape.rules import WalletRules, calculate_windows
from tracefold.news.wallet_contracts import NetBuySnapshot

NOW = 1_789_200_000_000
TOKEN = "0x" + "aa" * 20


def movement(
    index: int,
    *,
    wallet: int | None = None,
    usd: str | None = "1200",
    kind: str = "buy",
    raw: int = 1200,
    at: int = NOW,
) -> ClassifiedFill:
    transfer = kind == "transfer_out"
    return ClassifiedFill(
        chain_id=4663,
        tx_hash="0x" + f"{index:064x}",
        log_index=index,
        block_number=100,
        block_hash="0x" + "cc" * 32,
        wallet="0x" + f"{wallet or index:040x}",
        token=TOKEN,
        token_symbol="XYZ",
        token_decimals=18,
        kind=kind,
        amount_raw=raw,
        event_at_ms=at,
        received_at_ms=NOW,
        classified_at_ms=NOW,
        roster_version=1,
        cash_token=None if transfer else STABLE_CASH_TOKEN,
        cash_amount_raw=None if transfer else 1200000000,
        cash_decimals=None if transfer else 6,
        usd=None if usd is None else Decimal(usd),
        usd_source=None if usd is None else "usdg_cash_leg",
    )


def roster() -> list[dict[str, Any]]:
    return [
        dict(
            wallet="0x" + f"{i:040x}",
            handle=f"wallet{i}",
            rank_quality=i,
            roster_version=1,
            known_at_ms=NOW - 3600000,
            monitoring_from_ms=NOW - 3600000,
            closed_trades=20,
            profit_factor="2.0",
        )
        for i in range(1, 11)
    ]


def snapshot(fills: Any = None, **kwargs: Any) -> NetBuySnapshot:
    args = dict(
        fills=[movement(i) for i in range(1, 6)] if fills is None else fills,
        members=roster(),
        chain_id=4663,
        token=TOKEN,
        cutoff_at_ms=NOW,
        cutoff_block=100,
        cutoff_log=100,
        coverage_from_ms=NOW - 3600000,
        coverage_gap_at_ms=None,
        roster_version=1,
        rules=WalletRules(),
    )
    args.update(kwargs)
    return calculate_windows(**args)


def unsafe_snapshot(handle: str, symbol: str) -> NetBuySnapshot:
    members = roster()
    for member in members:
        member["handle"] = handle
    return snapshot([replace(movement(i), token_symbol=symbol) for i in range(1, 4)], members=members)
