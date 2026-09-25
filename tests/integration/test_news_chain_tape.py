"""The wallet tape against real PostgreSQL (#572 PR-1).

The classification rules are proved next door without a database. What is proved here is everything only
PostgreSQL can answer: that the chain's own identity makes a re-delivered movement one row, that a
restart resumes from the durable position and its overlap writes no duplicate, that a wide catch-up is
split across turns instead of being one unbounded request, that retention deletes by block time and
nothing else, and that a roster version appears only when the list actually changed.

The chain and the roster site are replayed from the responses recorded under
`tests/fixtures/chain_tape/`, so the rows written here are the rows the real endpoints produce.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.bus import DeferError
from tracefold.news.chain_tape.contracts import (
    BLOCK_COMPLETE_TX_INDEX,
    STABLE_CASH_TOKEN,
    USD_SOURCE_STABLE_CASH_LEG,
    RosterMember,
)
from tracefold.news.chain_tape.evm import TRANSFER_TOPIC, normalize_address
from tracefold.news.chain_tape.loop import ChainTapeLoop
from tracefold.news.pipeline.maintenance import JanitorLoop

pytestmark = pytest.mark.integration

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


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


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
    conn.execute(
        """
        INSERT INTO news_market_wallet_tape_state
            (state_id, high_water_block, high_water_tx_index, roster_version, last_outcome, updated_at_ms)
        VALUES ('chain_tape', %s, %s, %s, '', 1)
        """,
        (block, tx_index, roster_version),
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
    row = conn.execute("SELECT * FROM news_market_wallet_tape_state").fetchone()
    return None if row is None else dict(row)


# --------------------------------------------------------------------------- ingest
def test_one_turn_writes_the_recorded_sell_with_its_dollar_figure(conn: Any) -> None:
    """The end-to-end F2P: a recorded receipt becomes one row whose `usd` is the provider's own number."""

    version = _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([_recorded("receipt_sell_fsd.json")], head=SELL_BLOCK + 5)
    loop = _loop(conn, chain)
    # Start one overlap behind the recorded block so the first turn covers it.
    _seed_cursor(conn, block=SELL_BLOCK, roster_version=version)

    result = asyncio.run(loop.advance())

    assert result["written"] == 1
    rows = _fills(conn)
    assert len(rows) == 1
    row = rows[0]
    assert (row["chain_id"], row["tx_hash"], row["log_index"]) == (4663, SELL_TX, 6)
    assert (row["wallet"], row["token"], row["kind"]) == (SELL_WALLET, FSD, "sell")
    assert row["amount_raw"] == Decimal("9412641983109561976191332")
    assert row["cash_token"] == STABLE_CASH_TOKEN
    assert row["cash_amount_raw"] == Decimal("3608596725")
    assert (row["cash_decimals"], row["usd_source"]) == (6, USD_SOURCE_STABLE_CASH_LEG)
    assert row["usd"] == Decimal("3608.5967250000")
    assert (row["token_symbol"], row["token_decimals"]) == ("FSD", 18)
    assert row["event_at_ms"] == 1_788_642_791_000
    assert (row["roster_version"], row["provider"]) == (version, "robinhood_chain")


def test_the_same_movement_delivered_twice_is_one_row(conn: Any) -> None:
    """The chain assigned the identity, so a re-read is not a duplicate and not an update."""

    version = _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([_recorded("receipt_sell_fsd.json")], head=SELL_BLOCK + 5)
    _seed_cursor(conn, block=SELL_BLOCK, roster_version=version)

    first = asyncio.run(_loop(conn, chain).advance())
    repeat = asyncio.run(_loop(conn, chain).advance())

    assert first["written"] == 1
    assert repeat["written"] == 0
    assert len(_fills(conn)) == 1


def test_a_restart_resumes_from_the_durable_position_and_the_overlap_adds_nothing(conn: Any) -> None:
    """Two processes, one position: the second reads the same overlap and writes no second row."""

    version = _seed_roster(conn, [SELL_WALLET, BUY_WALLET])
    chain = _Chain(
        [_recorded("receipt_sell_fsd.json"), _recorded("receipt_buy_madetest.json", block_number=BUY_BLOCK)],
        head=BUY_BLOCK,
    )
    _seed_cursor(conn, block=SELL_BLOCK, roster_version=version)

    asyncio.run(_loop(conn, chain, catch_up_blocks_max=100_000).advance())
    after_first = _state(conn)
    assert after_first is not None
    assert after_first["last_outcome"] == "success"
    assert {row["kind"] for row in _fills(conn)} == {"sell", "buy"}

    # The durable position deliberately stops one overlap short of the block it was read to, so the
    # tip stays inside the next turn's candidate set instead of being declared complete on sight.
    assert after_first["high_water_block"] == BUY_BLOCK - 30
    assert after_first["high_water_tx_index"] == BLOCK_COMPLETE_TX_INDEX

    # A new process, a new loop object, nothing carried over but the row in PostgreSQL.
    restarted = _loop(conn, chain, catch_up_blocks_max=100_000)
    result = asyncio.run(restarted.advance())

    # The re-read is genuine: the buy is offered again, its receipt is fetched again, and the chain's
    # own identity collapses the write. An empty candidate set here would mean the overlap was being
    # fetched and discarded rather than re-read.
    assert result["candidates"] >= 1
    assert result["receipts"] >= 1
    assert BUY_TX in chain.receipt_calls[len(chain.receipt_calls) - result["receipts"] :]
    assert result["written"] == 0
    assert len(_fills(conn)) == 2
    assert chain.log_calls[-1][0] == BUY_BLOCK - 60


def test_a_tip_that_answered_short_is_stored_on_the_next_turn(conn: Any) -> None:
    """The whole point of the 30-block overlap, as a failing-to-passing case.

    A load-balanced public RPC can answer a range from a node that has not seen the newest block yet.
    Turn 1 gets nothing back and must not conclude the range is finished: if the durable mark went to
    the head it was read to, every later re-read of that block would be filtered out before a receipt
    was requested and the fill would be lost for ever.
    """

    version = _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([_recorded("receipt_sell_fsd.json")], head=SELL_BLOCK + 2)
    chain.hide_logs_until_call = 2  # both of turn 1's topic calls answer short
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    loop = _loop(conn, chain)

    first = asyncio.run(loop.advance())
    assert (first["logs"], first["candidates"], first["written"]) == (0, 0, 0)
    assert _fills(conn) == []

    second = asyncio.run(loop.advance())

    assert second["written"] == 1
    assert [row["tx_hash"] for row in _fills(conn)] == [SELL_TX]


def test_a_log_the_node_has_withdrawn_is_not_classified(conn: Any) -> None:
    """`removed` is the node saying this log is no longer on the chain it is serving (#572 §10)."""

    version = _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([_recorded("receipt_sell_fsd.json")], head=SELL_BLOCK + 2)
    chain.mark_logs_removed = True
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)

    result = asyncio.run(_loop(conn, chain).advance())

    assert result["logs"] == 1
    assert (result["candidates"], result["written"]) == (0, 0)
    assert chain.receipt_calls == []


def test_missing_receipt_never_advances_cursor_and_recovers_when_complete(conn: Any) -> None:
    version = _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([_recorded("receipt_sell_fsd.json")], head=SELL_BLOCK + 2)
    chain.withhold_receipts = {SELL_TX}
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    clock = [1_900_000_000_000]
    loop = _loop(conn, chain, clock=lambda: clock[0])
    for _turn in range(3):
        asyncio.run(loop.advance())
        state = _state(conn)
        assert state["high_water_block"] < SELL_BLOCK
        assert state["blocked_tx_hash"] == SELL_TX
        assert state["last_outcome"] == "partial"
        before = len(chain.receipt_calls)
        assert asyncio.run(_loop(conn, chain, clock=lambda: clock[0]).advance())["deferred"]
        assert len(chain.receipt_calls) == before
        clock[0] = state["next_attempt_at_ms"]
    assert len(chain.receipt_calls) == 3 and _state(conn)["unknown_total"] == 0
    chain.withhold_receipts.clear()
    assert asyncio.run(loop.advance())["written"] == 1
    assert len(_fills(conn)) == 1 and _state(conn)["blocked_tx_hash"] is None


def test_a_wide_backlog_is_walked_in_bounded_ranges_rather_than_one_request(conn: Any) -> None:
    """A 300,000-block outage is three turns of 100,000, not one unbounded `eth_getLogs`."""

    version = _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([], head=SELL_BLOCK + 300_000)
    _seed_cursor(conn, block=SELL_BLOCK, roster_version=version)
    loop = _loop(conn, chain, catch_up_blocks_max=100_000)

    for _turn in range(4):
        asyncio.run(loop.advance())

    # No turn ever asks for more than one catch-up window plus its overlap.
    assert max(to_block - from_block for from_block, to_block in chain.log_calls) == 100_030
    assert len(chain.log_calls) == 8  # two topic calls per turn, four turns
    state = _state(conn)
    assert state is not None
    # Caught up to the head, still lagging by the overlap so the tip is re-read next turn.
    assert state["high_water_block"] == SELL_BLOCK + 300_000 - 30
    assert state["high_water_tx_index"] == BLOCK_COMPLETE_TX_INDEX


def test_a_turn_beyond_the_receipt_bound_leaves_the_rest_pending_for_the_next_one(conn: Any) -> None:
    """The bound is on receipts per turn, never on what is stored: nothing is dropped."""

    version = _seed_roster(conn, [SELL_WALLET, BUY_WALLET])
    chain = _Chain(
        [_recorded("receipt_sell_fsd.json"), _recorded("receipt_buy_madetest.json", block_number=BUY_BLOCK)],
        head=BUY_BLOCK,
    )
    _seed_cursor(conn, block=SELL_BLOCK, roster_version=version)
    loop = _loop(conn, chain, receipts_per_turn_max=1)

    first = asyncio.run(loop.advance())
    assert (first["receipts"], first["pending"], first["written"]) == (1, 1, 1)
    assert [row["kind"] for row in _fills(conn)] == ["sell"]

    second = asyncio.run(loop.advance())
    assert second["written"] == 1
    assert {row["kind"] for row in _fills(conn)} == {"sell", "buy"}


@pytest.mark.parametrize("broken_token", [FSD, MADETEST])
def test_optional_metadata_never_blocks_receipts_or_complete_cutoff(conn: Any, broken_token: str) -> None:
    version = _seed_roster(conn, [SELL_WALLET, BUY_WALLET])
    chain = _Chain(
        [_recorded("receipt_sell_fsd.json"), _recorded("receipt_buy_madetest.json", block_number=BUY_BLOCK)],
        head=BUY_BLOCK + 40,
    )
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    chain.fail_token_with = {broken_token: RuntimeError("chain_rpc_rate_limited")}
    result = asyncio.run(_loop(conn, chain).advance())
    assert result["receipts"] == 2
    assert {row["kind"] for row in _fills(conn)} == {"sell", "buy"}
    state = _state(conn)
    assert state["high_water_block"] == BUY_BLOCK + 10
    assert state["last_outcome"] == "success" and state["last_error"] is None
    assert state["enrichment_error"] is not None
    broken = next(row for row in _fills(conn) if row["token"] == broken_token)
    assert broken["token_symbol"] is None and broken["usd"] is not None


def test_a_chain_failure_ends_the_turn_with_the_previous_position_intact(conn: Any) -> None:
    """An RPC that will not answer is a recorded outcome, never a lost position and never a raise."""

    version = _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([_recorded("receipt_sell_fsd.json")], head=SELL_BLOCK + 5)
    _seed_cursor(conn, block=SELL_BLOCK, roster_version=version)
    chain.fail_logs_with = RuntimeError("chain_rpc_rate_limited")
    clock = [1_900_000_000_000]
    loop = _loop(conn, chain, clock=lambda: clock[0])

    result = asyncio.run(loop.advance())

    assert result["written"] == 0
    assert _fills(conn) == []
    state = _state(conn)
    assert state is not None
    assert state["high_water_block"] == SELL_BLOCK
    # And the row says so. An operator reading `last_outcome` is asking "did the last turn work"; a
    # row still reading `success` because the turn returned before the write answers a different
    # question than the one they asked, and OPERATIONS.md tells them to read exactly this.
    assert state["last_outcome"] == "error"
    assert str(state["last_error"]).startswith("robinhood_rpc:")
    assert state["last_success_at_ms"] is None
    assert loop.last_error is not None

    clock[0] = _state(conn)["next_attempt_at_ms"]
    chain.fail_logs_with = None
    assert asyncio.run(_loop(conn, chain, clock=lambda: clock[0]).advance())["written"] == 1
    recovered = _state(conn)
    assert recovered is not None
    assert (recovered["last_outcome"], recovered["last_error"]) == ("success", None)
    assert recovered["last_success_at_ms"] is not None


def test_a_first_start_begins_at_the_head_rather_than_backfilling_history(conn: Any) -> None:
    _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([], head=SELL_BLOCK)
    loop = _loop(conn, chain)

    asyncio.run(loop.advance())

    assert chain.log_calls[0] == (SELL_BLOCK - 30, SELL_BLOCK)
    state = _state(conn)
    assert state is not None
    # The window's own start: a first turn reads one overlap and keeps all of it re-readable.
    assert state["high_water_block"] == SELL_BLOCK - 30


def test_no_roster_is_no_work_and_writes_nothing(conn: Any) -> None:
    chain = _Chain([_recorded("receipt_sell_fsd.json")], head=SELL_BLOCK)
    loop = _loop(conn, chain)

    result = asyncio.run(loop.advance())

    assert (result["wallets"], result["written"]) == (0, 0)
    assert chain.log_calls == []
    assert _fills(conn) == []


def test_an_airdrop_is_counted_on_the_state_row_rather_than_stored(conn: Any) -> None:
    """ "How much of this stream is noise" is a question the fills table cannot answer (#572 §6)."""

    version = _seed_roster(conn, [SELL_WALLET])
    airdrop = _synthetic_receipt("airdrop_in", block_number=SELL_BLOCK, transaction_index=2)
    chain = _Chain([airdrop], head=SELL_BLOCK + 2)
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)

    result = asyncio.run(_loop(conn, chain).advance())

    assert (result["ignored_inbound"], result["written"]) == (1, 0)
    assert _fills(conn) == []
    state = _state(conn)
    assert state is not None
    assert (state["ignored_inbound_total"], state["unknown_total"]) == (1, 0)


def test_one_movement_re_read_across_turns_is_counted_once(conn: Any) -> None:
    """The counters count movements, not passes over them.

    The classified position lags the head by the log overlap so the tip is re-read, which means one
    airdrop is re-classified for several turns running. A fill collapses on its primary key; a count has
    no key to collapse on, so `noise_through_*` is the key -- it only ever advances, and a movement is
    counted only when it is strictly above it. Without that, a stream that is half noise reads as three
    quarters noise and the week-one calibration in #572 §6 is wrong in the direction that matters.
    """

    version = _seed_roster(conn, [SELL_WALLET])
    airdrop = _synthetic_receipt("airdrop_in", block_number=SELL_BLOCK, transaction_index=2)
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    loop = _loop(conn, chain := _Chain([airdrop], head=SELL_BLOCK + 2))

    classified = 0
    for turn in range(3):
        chain.head = SELL_BLOCK + 2 + turn * 20  # the chain moves on; the overlap keeps re-offering it
        classified += asyncio.run(loop.advance())["receipts"]

    # It really was read on every turn -- this is not a test of the movement falling out of the window.
    assert classified == 3
    assert chain.receipt_calls.count(airdrop.transaction_hash) == 3
    state = _state(conn)
    assert state is not None
    assert state["ignored_inbound_total"] == 1
    assert state["noise_through_block"] == SELL_BLOCK
    assert state["noise_through_tx_index"] == 2


def test_a_second_movement_above_the_marker_is_still_counted(conn: Any) -> None:
    """The marker suppresses re-counts, not counting."""

    version = _seed_roster(conn, [SELL_WALLET])
    first = _synthetic_receipt("airdrop_in", block_number=SELL_BLOCK, transaction_index=2)
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    asyncio.run(_loop(conn, _Chain([first], head=SELL_BLOCK + 2)).advance())

    # Its own identity, so both movements are on the chain at once and the second turn re-reads the
    # first rather than replacing it.
    later = _synthetic_receipt(
        "airdrop_in",
        block_number=SELL_BLOCK + 5,
        transaction_index=0,
        transaction_hash="0x" + "7" * 64,
    )
    chain = _Chain([first, later], head=SELL_BLOCK + 7)
    result = asyncio.run(_loop(conn, chain).advance())

    assert result["receipts"] == 2  # both were classified this turn
    assert result["ignored_inbound"] == 1  # and only the one above the marker was counted
    state = _state(conn)
    assert state is not None
    assert state["ignored_inbound_total"] == 2
    assert state["noise_through_block"] == SELL_BLOCK + 5


class _Telemetry:
    """Only the three measurements the loop takes."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.skipped: list[str] = []
        self.provider_calls: list[tuple[str, str]] = []

    def record_external_data_turn(self, name: str, outcome: str, seconds: float, **_kwargs: Any) -> None:
        del seconds
        self.turns.append((name, outcome))

    def record_external_data_provider_call(
        self, name: str, source: str, outcome: str, seconds: float, **_kwargs: Any
    ) -> None:
        del seconds
        self.provider_calls.append((source, outcome))

    def record_external_data_skipped(self, name: str, reason: str) -> None:
        del name
        self.skipped.append(reason)


def test_the_skip_counters_wait_for_a_committed_write(conn: Any) -> None:
    """Two answers to one question must not disagree.

    The state row's totals move only when the write commits. A refused write rolls the whole turn back
    and the next one re-reads the same movements, so emitting the Prometheus counters anyway would leave
    them ahead of the row by exactly the refused turns.
    """

    version = _seed_roster(conn, [SELL_WALLET])
    airdrop = _synthetic_receipt("airdrop_in", block_number=SELL_BLOCK, transaction_index=2)
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    telemetry = _Telemetry()
    db = _Db(conn)
    db.fail_on = {"news_chain_tape_store": DeferError("db_admission_timeout")}
    loop = ChainTapeLoop(
        db=db,
        chain=_Chain([airdrop], head=SELL_BLOCK + 2),
        telemetry=telemetry,  # type: ignore[arg-type]
    )

    refused = asyncio.run(loop.advance())

    assert refused["ignored_inbound"] == 1
    assert telemetry.skipped == []
    refused_state = _state(conn)
    assert refused_state is not None
    assert (refused_state["ignored_inbound_total"], refused_state["noise_through_block"]) == (0, 0)

    # The same turn, committed: the row and the counter move together.
    db.fail_on = {}
    asyncio.run(loop.advance())

    assert telemetry.skipped == ["airdrop_ignored"]
    state = _state(conn)
    assert state is not None
    assert state["ignored_inbound_total"] == 1


# --------------------------------------------------------------------------- database failures
def test_a_refused_read_ends_the_turn_and_leaves_the_capability_running(conn: Any) -> None:
    """An ordinary admission timeout is not a program error, and must not fault `chain_tape`.

    `_store` already treated a refused write this way. The opening read and the roster write did not,
    so one busy moment on the business lane raised out of `advance()` and the Workers root confined the
    whole capability for the rest of the process's life.
    """

    _seed_roster(conn, [SELL_WALLET])
    db = _Db(conn)
    db.fail_on = {"news_chain_tape_plan": DeferError("db_admission_timeout")}
    loop = ChainTapeLoop(db=db, chain=_Chain([], head=SELL_BLOCK))

    result = asyncio.run(loop.advance())

    assert result["written"] == 0
    assert loop.last_error == "db:DeferError"


# --------------------------------------------------------------------------- roster versions
def test_a_roster_version_appears_only_when_the_membership_changes(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        first = repos.news.chain_tape_store_roster([_member(SELL_WALLET)], now_ms=1_000)
    with repos.transaction():
        again = repos.news.chain_tape_store_roster([_member(SELL_WALLET)], now_ms=2_000)

    assert first.roster_version == 1
    assert again.roster_version == 1
    assert again.taken_at_ms == 2_000
    current = repos.news.chain_tape_current_roster()
    assert current is not None
    assert current.taken_at_ms == 2_000

    with repos.transaction():
        moved = repos.news.chain_tape_store_roster([_member(SELL_WALLET, quality=2)], now_ms=3_000)
    assert moved.roster_version == 1

    with repos.transaction():
        joined = repos.news.chain_tape_store_roster(
            [_member(SELL_WALLET, quality=2), _member(BUY_WALLET, quality=1)], now_ms=4_000
        )
    assert joined.roster_version == 2
    assert conn.execute("SELECT count(*) AS n FROM news_market_wallet_roster").fetchone()["n"] == 3


# --------------------------------------------------------------------------- retention
def _insert_fill(conn: Any, *, tx_suffix: int, event_at_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO news_market_wallet_fills (
            chain_id, tx_hash, log_index, block_number, block_hash, wallet, token, kind, amount_raw,
            event_at_ms, received_at_ms, classified_at_ms, roster_version
        ) VALUES (4663, %s, 1, 1, '0xabc', %s, %s, 'transfer_out', 1, %s, %s, %s, 1)
        """,
        (
            "0x" + format(tx_suffix, "064x"),
            SELL_WALLET,
            FSD,
            event_at_ms,
            event_at_ms,
            event_at_ms,
        ),
    )


def test_retention_deletes_by_block_time_and_leaves_everything_inside_the_window(conn: Any) -> None:
    now = 1_800_000_000_000
    _insert_fill(conn, tx_suffix=1, event_at_ms=now - 91 * DAY_MS)
    _insert_fill(conn, tx_suffix=2, event_at_ms=now - 89 * DAY_MS)
    _insert_fill(conn, tx_suffix=3, event_at_ms=now)
    conn.commit()
    janitor = JanitorLoop(
        db=_Db(conn),
        cold_db=_Db(conn),
        bus=None,
        retention_chain_tape_days=90,
        chain_tape_enabled=True,
    )

    asyncio.run(janitor._purge_chain_tape_retention(now))

    remaining = {row["event_at_ms"] for row in _fills(conn)}
    assert remaining == {now - 89 * DAY_MS, now}


def test_a_disabled_tape_is_not_swept_every_minute(conn: Any) -> None:
    """No turn is writing fills, so a `DELETE` every sixty seconds is work with a known answer."""

    now = 1_800_000_000_000
    _insert_fill(conn, tx_suffix=9, event_at_ms=now - 200 * DAY_MS)
    conn.commit()
    db = _Db(conn)
    janitor = JanitorLoop(db=db, cold_db=db, bus=None, retention_chain_tape_days=90, chain_tape_enabled=False)

    asyncio.run(janitor._purge_chain_tape_retention(now))

    assert "news_chain_tape_retention" not in db.names
    assert len(_fills(conn)) == 1


def test_retention_is_one_bounded_batch_per_pass(conn: Any) -> None:
    """A sweep that could delete a month in one statement is not bounded; this one is."""

    now = 1_800_000_000_000
    for suffix in range(5):
        _insert_fill(conn, tx_suffix=100 + suffix, event_at_ms=now - 200 * DAY_MS)
    conn.commit()
    repos = repositories_for_connection(conn)

    with repos.transaction():
        deleted = repos.news.chain_tape_purge_fills(cutoff_ms=now - 90 * DAY_MS, limit=2)

    assert deleted == 2
    assert len(_fills(conn)) == 3


# --------------------------------------------------------------------------- the schema itself
def test_the_kind_vocabulary_and_the_cash_pairing_are_enforced_by_postgres(conn: Any) -> None:
    """The rules that must not depend on one writer being correct."""

    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_kind(conn, "transfer_in")
    conn.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            INSERT INTO news_market_wallet_fills (
                chain_id, tx_hash, log_index, block_number, block_hash, wallet, token, kind, amount_raw,
                cash_token, event_at_ms, received_at_ms, classified_at_ms, roster_version
            ) VALUES (4663, %s, 1, 1, '0xabc', %s, %s, 'sell', 1, %s, 1, 1, 1, 1)
            """,
            ("0x" + "9" * 64, SELL_WALLET, FSD, STABLE_CASH_TOKEN),
        )
    conn.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            INSERT INTO news_market_wallet_tape_state (state_id, updated_at_ms)
            VALUES ('somebody_elses_tape', 1)
            """
        )
    conn.rollback()


def _insert_kind(conn: Any, kind: str) -> None:
    conn.execute(
        """
        INSERT INTO news_market_wallet_fills (
            chain_id, tx_hash, log_index, block_number, block_hash, wallet, token, kind, amount_raw,
            event_at_ms, received_at_ms, classified_at_ms, roster_version
        ) VALUES (4663, %s, 1, 1, '0xabc', %s, %s, %s, 1, 1, 1, 1, 1)
        """,
        ("0x" + "8" * 64, SELL_WALLET, FSD, kind),
    )


def test_missing_previously_committed_overlap_log_records_gap_without_erasing_facts(conn: Any) -> None:
    version = _seed_roster(conn, [SELL_WALLET])
    chain = _Chain([_recorded("receipt_sell_fsd.json")], head=SELL_BLOCK + 2)
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    loop = _loop(conn, chain)
    assert asyncio.run(loop.advance())["written"] == 1
    before = _fills(conn)
    chain.receipts = {}
    asyncio.run(loop.advance())
    assert "reorg_unresolved_overlap" in _state(conn)["last_error"]
    assert _state(conn)["gap_at_ms"] is not None
    assert _fills(conn) == before
