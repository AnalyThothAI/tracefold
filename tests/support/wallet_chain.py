"""Recorded chain and ledger builders shared by wallet integration tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.chain_tape.contracts import STABLE_CASH_TOKEN, RosterMember
from tracefold.news.chain_tape.evm import TRANSFER_TOPIC, normalize_address
from tracefold.news.chain_tape.loop import ChainTapeLoop

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "chain_tape"

SELL_TX = "0x5c10c3cf9b3a5ef265de9ea87e0b4c787583ef11823ea233fde27528ab9ac5f0"

BUY_TX = "0x42f41c071eb8a6483995fe817b6ff8289f9b4a96ad2add4e6a9362dcfc23742b"

SELL_WALLET = "0x69326e48f68500fb6cf3b3a7da640737b9cc347b"

BUY_WALLET = "0x80f3b0b712a82172a67e454e313ba6e2b0e7ae64"

FSD = "0x8de9018c1bb82884245f06dede9fe2bebabd1e18"

MADETEST = "0x5d191e73445cd5eb03cbaa56c263f1f9e9a4fcb3"

SELL_BLOCK = 55_432_994

BUY_BLOCK = 55_446_520

DAY_MS = 24 * 3_600_000


class _Db:
    """The News database port over one real connection, in the two shapes the loop uses."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.names: list[str] = []
        # What the business lane refuses, in the News error vocabulary the composition root translates
        # an admission timeout or an overrun into.
        self.fail_on: dict[str, Exception] = {}

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        if name in self.fail_on:
            raise self.fail_on[name]
        return fn(repositories_for_connection(self.connection))

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        if name in self.fail_on:
            raise self.fail_on[name]
        repos = repositories_for_connection(self.connection)
        with repos.transaction():
            return fn(repos)


@dataclass(frozen=True, slots=True)
class _Log:
    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    block_hash: str
    transaction_hash: str
    transaction_index: int
    log_index: int
    removed: bool = False


@dataclass(frozen=True, slots=True)
class _Receipt:
    transaction_hash: str
    block_number: int
    block_hash: str
    transaction_index: int
    status: int
    logs: tuple[_Log, ...]


@dataclass(frozen=True, slots=True)
class _Token:
    address: str
    symbol: str | None
    decimals: int | None


def _receipt(document: Any, *, block_number: int | None = None) -> _Receipt:
    block = int(str(document["blockNumber"]), 16) if block_number is None else block_number
    block_hash = str(document["blockHash"]).lower()
    tx = str(document["transactionHash"]).lower()
    tx_index = int(str(document["transactionIndex"]), 16)
    return _Receipt(
        transaction_hash=tx,
        block_number=block,
        block_hash=block_hash,
        transaction_index=tx_index,
        status=int(str(document["status"]), 16),
        logs=tuple(
            _Log(
                address=str(log["address"]).lower(),
                topics=tuple(str(topic).lower() for topic in log["topics"]),
                data=str(log["data"]),
                block_number=block,
                block_hash=block_hash,
                transaction_hash=tx,
                transaction_index=tx_index,
                log_index=int(str(log["logIndex"]), 16),
            )
            for log in document["logs"]
        ),
    )


def _removed(log: _Log) -> _Log:
    return _Log(
        address=log.address,
        topics=log.topics,
        data=log.data,
        block_number=log.block_number,
        block_hash=log.block_hash,
        transaction_hash=log.transaction_hash,
        transaction_index=log.transaction_index,
        log_index=log.log_index,
        removed=True,
    )


def _recorded(name: str, *, block_number: int | None = None) -> _Receipt:
    document = json.loads((FIXTURES / name).read_text(encoding="utf-8"))["result"]
    return _receipt(document, block_number=block_number)


def _synthetic_receipt(
    name: str,
    *,
    block_number: int,
    transaction_index: int,
    transaction_hash: str | None = None,
) -> _Receipt:
    """One recorded synthetic receipt, re-anchored at a chosen position and, if asked, identity.

    The fixtures each carry one transaction hash. A test that needs two movements on the chain at once
    has to give the second its own, or the fake chain -- which is keyed by hash, as the real one is --
    keeps only the last of them and the test proves nothing about the two coexisting.
    """

    document = json.loads((FIXTURES / "synthetic_receipts.json").read_text(encoding="utf-8"))[name]["result"]
    decoded = _receipt(document, block_number=block_number)
    tx = (transaction_hash or decoded.transaction_hash).lower()
    return _Receipt(
        transaction_hash=tx,
        block_number=block_number,
        block_hash=decoded.block_hash,
        transaction_index=transaction_index,
        status=decoded.status,
        logs=tuple(
            _Log(
                address=log.address,
                topics=log.topics,
                data=log.data,
                block_number=block_number,
                block_hash=log.block_hash,
                transaction_hash=tx,
                transaction_index=transaction_index,
                log_index=log.log_index,
            )
            for log in decoded.logs
        ),
    )


class _Chain:
    """The recorded chain, answering the five calls the loop makes."""

    chain_id = 4663

    def __init__(self, receipts: Sequence[_Receipt], *, head: int) -> None:
        self.receipts = {receipt.transaction_hash: receipt for receipt in receipts}
        self.head = int(head)
        self.last_response_bytes = 0
        self.log_calls: list[tuple[int, int]] = []
        self.receipt_calls: list[str] = []
        self.fail_logs_with: Exception | None = None
        # A node that answers short on the first call and completely afterwards -- the exact shape the
        # 30-block overlap exists for.
        self.hide_logs_until_call = 0
        # Transactions the node will not produce a receipt for, however many times it is asked.
        self.withhold_receipts: set[str] = set()
        self.mark_logs_removed = False
        # Token addresses whose metadata call raises, however often it is asked.
        self.fail_token_with: dict[str, Exception] = {}
        self.tokens = {
            STABLE_CASH_TOKEN: _Token(STABLE_CASH_TOKEN, "USDG", 6),
            FSD: _Token(FSD, "FSD", 18),
            MADETEST: _Token(MADETEST, "MADETEST", 18),
        }

    async def block_number(self) -> int:
        return self.head

    async def logs(
        self,
        *,
        from_block: int,
        to_block: int,
        topics: Sequence[Any],
    ) -> tuple[_Log, ...]:
        self.log_calls.append((int(from_block), int(to_block)))
        if self.fail_logs_with is not None:
            raise self.fail_logs_with
        if len(self.log_calls) <= self.hide_logs_until_call:
            return ()
        position = 1 if len(topics) == 2 else 2
        wanted = {str(topic).lower() for topic in topics[position]}
        out: list[_Log] = []
        for receipt in self.receipts.values():
            if not from_block <= receipt.block_number <= to_block:
                continue
            for log in receipt.logs:
                if len(log.topics) < 3 or log.topics[0] != TRANSFER_TOPIC:
                    continue
                if log.topics[position] in wanted:
                    out.append(_removed(log) if self.mark_logs_removed else log)
        return tuple(out)

    async def receipt(self, transaction_hash: str) -> _Receipt | None:
        self.receipt_calls.append(transaction_hash)
        if transaction_hash in self.withhold_receipts:
            return None
        return self.receipts.get(transaction_hash)

    async def block_timestamp_ms(self, block_number: int) -> int:
        # 0.1 s blocks, anchored on the recorded sell's own header.
        return 1_788_642_791_000 + (int(block_number) - SELL_BLOCK) * 100

    async def token_decimals(self, address: str) -> int | None:
        return (await self.token(address)).decimals

    async def token(self, address: str) -> _Token:
        normalized = normalize_address(address)
        if normalized in self.fail_token_with:
            raise self.fail_token_with[normalized]
        return self.tokens.get(normalized, _Token(normalized, None, None))


def _member(wallet: str, *, quality: int | None = 1, whale: int | None = None) -> RosterMember:
    return RosterMember(
        wallet=wallet,
        handle=f"handle-{wallet[-4:]}",
    )


def _seed_roster(conn: Any, wallets: Sequence[str], *, now_ms: int = 1_788_600_000_000) -> int:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        snapshot = repos.news.chain_tape_store_roster(
            [_member(wallet, quality=index + 1) for index, wallet in enumerate(wallets)],
            now_ms=now_ms,
        )
    return snapshot.roster_version


def _loop(conn: Any, chain: _Chain, **kwargs: Any) -> ChainTapeLoop:
    """The collector, which since #649 §5.1 never talks to the roster site at all.

    The published list is seeded directly, exactly as production's collector reads it: one PostgreSQL
    row written by the `news-wallet-roster` task, whose own regressions live in
    `test_wallet_roster_refresh.py`.
    """

    return ChainTapeLoop(db=_Db(conn), chain=chain, **kwargs)


def _seed_cursor(conn: Any, *, block: int, roster_version: int, tx_index: int = -1) -> None:
    from tracefold.news.chain_tape.contracts import TapeCursor

    with conn.transaction():
        repositories_for_connection(conn).news.chain_tape_save_state(
            cursor=TapeCursor(block, tx_index),
            roster_version=roster_version,
            outcome="",
            error=None,
            now_ms=1,
            succeeded=False,
        )
    conn.commit()


def _fills(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT chain_id, tx_hash, log_index, block_number, wallet, token, kind, amount_raw,
               cash_token, cash_amount_raw, cash_decimals, usd, usd_source, token_symbol,
               token_decimals, event_at_ms, roster_version, provider
          FROM news_market_wallet_fills
         ORDER BY block_number, log_index
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _state(conn: Any) -> dict[str, Any] | None:
    return repositories_for_connection(conn).news.chain_tape_state()
