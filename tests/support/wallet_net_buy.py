"""Net-buy fact, detector and sender fixtures shared by wallet integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.chain_tape.contracts import (
    BLOCK_COMPLETE_TX_INDEX,
    STABLE_CASH_TOKEN,
    ClassifiedFill,
    RosterMember,
    TapeCursor,
)
from tracefold.news.chain_tape.detect import NetBuyDetector
from tracefold.news.chain_tape.rules import WalletRules

NOW = 1_789_200_000_000

CHAIN_ID = 4663

TOKEN = "0x" + "aa" * 20


def fill(
    index: int,
    *,
    wallet: int = 1,
    kind: str = "buy",
    usd: str | None = "1200",
    raw: int = 1200,
    at: int = NOW,
    tx: int | None = None,
    log: int = 1,
) -> ClassifiedFill:
    transfer = kind == "transfer_out"
    return ClassifiedFill(
        chain_id=CHAIN_ID,
        tx_hash="0x" + f"{index if tx is None else tx:064x}",
        log_index=log,
        block_number=100 + index,
        block_hash="0x" + "cc" * 32,
        wallet="0x" + f"{wallet:040x}",
        token=TOKEN,
        kind=kind,
        amount_raw=raw,
        event_at_ms=at,
        received_at_ms=NOW,
        classified_at_ms=NOW,
        roster_version=1,
        token_symbol="XYZ",
        token_decimals=0,
        cash_token=None if transfer else (STABLE_CASH_TOKEN if usd is not None else "0x" + "ee" * 20),
        cash_amount_raw=None if transfer else int(Decimal(usd or "1") * 10**6),
        cash_decimals=None if transfer else 6,
        usd=None if usd is None else Decimal(usd),
        usd_source=None if usd is None else "usdg_cash_leg",
    )


def member(wallet: int, *, quality: bool = True) -> RosterMember:
    return RosterMember(
        wallet="0x" + f"{wallet:040x}",
        handle=f"wallet{wallet}",
    )


class Db:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.rollback_commit = False

    async def read(self, name: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
        return fn(repositories_for_connection(self.connection))

    async def tx(self, name: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
        repos = repositories_for_connection(self.connection)
        with repos.transaction():
            result = fn(repos)
            if self.rollback_commit and name == "news_wallet_net_buy_commit":
                self.connection.execute("SELECT 1 / 0")
            return result

    async def quotes_for_symbols(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("net-buy first report must not fetch quotes")


class Sender:
    available = True

    def __init__(self) -> None:
        self.cards: list[Any] = []

    async def send_prepared_card(self, card: Any, **kwargs: Any) -> dict[str, Any]:
        self.cards.append(card)
        return {"provider": "feishu", "message_id": len(self.cards)}


def seed(conn: Any, fills: Sequence[ClassifiedFill], *, quality: bool = True) -> Any:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        roster = repos.news.chain_tape_store_roster(
            [member(i, quality=quality) for i in range(1, 11)], now_ms=NOW - 3_600_000
        )
        repos.news.chain_tape_save_state(
            cursor=TapeCursor(1000, BLOCK_COMPLETE_TX_INDEX),
            roster_version=roster.roster_version,
            outcome="success",
            error=None,
            now_ms=NOW,
            succeeded=True,
        )
        conn.execute(
            (
                "UPDATE news_collectors SET state=state || "
                "jsonb_build_object('detection_cutover_at_ms',%s::bigint) WHERE "
                "collector_id='chain_tape'"
            ),
            (NOW - 3_600_000,),
        )
        repos.news.chain_tape_record_coverage(
            from_ms=NOW - 3_600_000,
            through_ms=NOW,
            through_block=1000,
            through_log=BLOCK_COMPLETE_TX_INDEX,
            gap_at_ms=None,
            wallets=roster.wallets,
        )
        repos.news.chain_tape_record_fills(fills)
    return repos


def events(conn: Any) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM news_market_wallet_events ORDER BY event_at_ms, item_id").fetchall())


def run(conn: Any, *, stamp: int = NOW, enabled: bool = True, rules: WalletRules | None = None) -> Any:
    return asyncio.run(
        NetBuyDetector(db=Db(conn), clock=lambda: stamp, notifications_enabled=enabled, rules=rules).advance()
    )


def add_facts(conn: Any, facts: Sequence[ClassifiedFill], *, stamp: int) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_record_fills(facts)
        repos.news.chain_tape_record_coverage(
            from_ms=NOW - 3600000,
            through_ms=stamp,
            through_block=max([1000, *(f.block_number for f in facts)]),
            through_log=BLOCK_COMPLETE_TX_INDEX,
            gap_at_ms=None,
            wallets=tuple(member(i).wallet for i in range(1, 11)),
        )
