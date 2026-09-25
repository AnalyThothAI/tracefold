"""Regression for #697: complete coverage must not jump over a missing receipt."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from tests.integration.test_news_chain_tape import (
    SELL_BLOCK,
    SELL_WALLET,
    _Chain,
    _loop,
    _seed_cursor,
    _seed_roster,
    _state,
    _synthetic_receipt,
)
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


pytestmark = pytest.mark.integration


def test_same_block_missing_receipt_never_releases_successful_suffix(conn: Any) -> None:
    version = _seed_roster(conn, [SELL_WALLET])
    receipts = []
    for index in (1, 2, 3):
        receipt = _synthetic_receipt(
            "transfer_out_plain",
            block_number=SELL_BLOCK,
            transaction_index=index,
            transaction_hash=f"0x{index:064x}",
        )
        receipts.append(
            replace(receipt, logs=tuple(replace(log, log_index=index * 10 + log.log_index) for log in receipt.logs))
        )
    chain = _Chain(receipts, head=SELL_BLOCK + 40)
    chain.withhold_receipts.add(receipts[1].transaction_hash)
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    asyncio.run(_loop(conn, chain).advance())

    state = _state(conn)
    assert state is not None
    assert state["high_water_tx_index"] == 1
    assert state["scanned_log"] == max(log.log_index for log in receipts[0].logs)
    pending = repositories_for_connection(conn).news.wallet_pending_receipts()
    assert {fills[0].tx_hash for fills in pending} == {receipts[0].transaction_hash}


def test_gap_backoff_does_not_reprocess_suffix_and_all_23_receipts_drain_after_recovery(conn: Any) -> None:
    version = _seed_roster(conn, [SELL_WALLET])
    receipts = []
    for index in range(1, 24):
        receipt = _synthetic_receipt(
            "transfer_out_plain", block_number=SELL_BLOCK, transaction_index=index, transaction_hash=f"0x{index:064x}"
        )
        receipts.append(
            replace(receipt, logs=tuple(replace(log, log_index=index * 10 + log.log_index) for log in receipt.logs))
        )
    chain = _Chain(receipts, head=SELL_BLOCK + 40)
    chain.withhold_receipts.add(receipts[0].transaction_hash)
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    clock = [1_900_000_000_000]
    task = _loop(conn, chain, clock=lambda: clock[0])
    assert asyncio.run(task.advance())["written"] == 0
    assert chain.receipt_calls == [receipts[0].transaction_hash]
    assert asyncio.run(task.advance())["deferred"]
    clock[0] = _state(conn)["next_attempt_at_ms"]
    chain.withhold_receipts.clear()
    assert asyncio.run(task.advance())["written"] == 20
    assert asyncio.run(task.advance())["written"] == 3
    assert conn.execute("SELECT count(*) AS n FROM news_market_wallet_fills").fetchone()["n"] == 23
    assert _state(conn)["high_water_block"] == SELL_BLOCK + 10


def test_zero_business_fill_receipt_still_has_a_real_log_cutoff(conn: Any) -> None:
    version = _seed_roster(conn, [SELL_WALLET])
    first = _synthetic_receipt("airdrop_in", block_number=SELL_BLOCK, transaction_index=1)
    second = _synthetic_receipt("transfer_out_plain", block_number=SELL_BLOCK + 1, transaction_index=1)
    chain = _Chain([first, second], head=SELL_BLOCK + 40)
    chain.withhold_receipts.add(second.transaction_hash)
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    result = asyncio.run(_loop(conn, chain).advance())
    assert result["written"] == 0 and result["ignored_inbound"] == 1
    assert _state(conn)["scanned_log"] == max(log.log_index for log in first.logs)
    assert _state(conn)["high_water_tx_index"] == first.transaction_index


def test_plan_is_consistent_inside_the_real_workers_preconfigured_transaction(conn: Any) -> None:
    version = _seed_roster(conn, [SELL_WALLET])
    _seed_cursor(conn, block=SELL_BLOCK - 1, roster_version=version)
    # worker_session configures timeouts with SELECT set_config before the callback.
    # Starting a nested REPEATABLE READ transaction here would fail after that SELECT.
    with conn.transaction():
        conn.execute("SELECT set_config('statement_timeout', '5s', true)")
        state, roster, wallets = repositories_for_connection(conn).news.chain_tape_collection_plan()
        assert state["roster_version"] == roster.roster_version == version
        assert wallets == roster.wallets == (SELL_WALLET,)


def test_missing_sell_never_creates_false_quorum_and_normal_receipts_reach_one_send(conn: Any) -> None:
    from tests.integration.test_news_chain_tape import FSD, _Log, _Receipt
    from tests.integration.test_wallet_net_buy import Db, Sender
    from tracefold.news.chain_tape.contracts import STABLE_CASH_TOKEN, UNISWAP_V3_SWAP_TOPIC
    from tracefold.news.chain_tape.detect import NetBuyDetector
    from tracefold.news.chain_tape.evm import TRANSFER_TOPIC, address_topic
    from tracefold.news.market_notifications import MarketNotificationLoop

    wallets = tuple(f"0x{i:040x}" for i in range(1, 7))
    executor, pool = "0x" + "e" * 40, "0x" + "d" * 40

    def receipt(number, wallet, kind="buy", usd=1200, block=SELL_BLOCK):
        tx, bh = f"0x{number:064x}", "0x" + "c" * 64

        def transfer(offset, token, sender, recipient, raw):
            return _Log(
                token,
                (TRANSFER_TOPIC, address_topic(sender), address_topic(recipient)),
                hex(raw),
                block,
                bh,
                tx,
                number,
                number * 10 + offset,
            )

        swap = _Log(pool, (UNISWAP_V3_SWAP_TOPIC,), "0x", block, bh, tx, number, number * 10 + 2)
        if kind == "buy":
            logs = (
                transfer(0, STABLE_CASH_TOKEN, wallet, executor, usd * 10**6),
                transfer(1, FSD, pool, executor, usd * 10**18),
                swap,
                transfer(3, FSD, executor, wallet, usd * 10**18),
            )
        else:
            logs = (
                transfer(0, FSD, wallet, executor, usd * 10**18),
                transfer(1, FSD, executor, pool, usd * 10**18),
                swap,
                transfer(3, STABLE_CASH_TOKEN, pool, executor, usd * 10**6),
            )
        return _Receipt(tx, block, bh, number, 1, logs)

    prior = [receipt(i, wallets[i - 1], block=SELL_BLOCK - 1000) for i in range(1, 5)]
    before_gap = receipt(5, wallets[1], usd=10)
    missing_sell = receipt(6, wallets[0], kind="sell", usd=600)
    fifth = receipt(7, wallets[4])
    chain = _Chain([*prior, before_gap, missing_sell, fifth], head=SELL_BLOCK + 40)
    chain.withhold_receipts.add(missing_sell.transaction_hash)
    version = _seed_roster(conn, wallets)
    _seed_cursor(conn, block=SELL_BLOCK - 20_000, roster_version=version)
    conn.execute("UPDATE news_market_wallet_tape_state SET detection_cutover_at_ms=0")
    conn.commit()
    clock = [asyncio.run(chain.block_timestamp_ms(SELL_BLOCK)) + 1000]
    collector = _loop(conn, chain, clock=lambda: clock[0])
    detector = NetBuyDetector(db=Db(conn), clock=lambda: clock[0])
    sender = Sender()
    notifier = MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: clock[0])
    asyncio.run(collector.advance())
    assert asyncio.run(detector.advance()).opened == 0
    assert conn.execute("SELECT count(*) AS n FROM news_market_wallet_events").fetchone()["n"] == 0
    clock[0] = _state(conn)["next_attempt_at_ms"]
    chain.withhold_receipts.clear()
    asyncio.run(collector.advance())
    assert asyncio.run(detector.advance()).opened == 0
    asyncio.run(notifier.advance())
    assert sender.cards == []
    # A sixth source wallet now makes five genuinely qualified buyers (the seller
    # remains below $1000). This checks the real positive collector→detector→sender seam.
    sixth = receipt(8, wallets[5], block=SELL_BLOCK + 20)
    chain.receipts[sixth.transaction_hash] = sixth
    chain.head = SELL_BLOCK + 60
    asyncio.run(collector.advance())
    assert asyncio.run(detector.advance()).opened == 1
    asyncio.run(notifier.advance())
    asyncio.run(notifier.advance())
    assert len(sender.cards) == 1
    assert conn.execute("SELECT count(*) AS n FROM news_market_deliveries WHERE state='sent'").fetchone()["n"] == 1


@pytest.mark.parametrize("count", [147, 201, 256])
def test_actual_rpc_requests_are_bounded_for_full_topic_sets(conn: Any, count: int) -> None:
    import json

    import httpx

    from tests.integration.test_news_chain_tape import _Db
    from tracefold.integrations.robinhood_chain import RobinhoodChainClient
    from tracefold.news.chain_tape.loop import ChainTapeLoop

    wallets = tuple(f"0x{i:040x}" for i in range(1, count + 1))
    version = _seed_roster(conn, wallets)
    _seed_cursor(conn, block=SELL_BLOCK - 1000, roster_version=version)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        match body["method"]:
            case "eth_blockNumber":
                result = hex(SELL_BLOCK + 40)
            case "eth_getLogs":
                result = []
            case "eth_getBlockByNumber":
                result = {"timestamp": hex(1_788_642_791)}
            case _:
                raise AssertionError(body["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    chain = RobinhoodChainClient(transport=httpx.MockTransport(handler))
    task = ChainTapeLoop(db=_Db(conn), chain=chain)

    async def run():
        try:
            return await task.advance()
        finally:
            await task.aclose()

    result = asyncio.run(run())
    logs = [r for r in requests if r["method"] == "eth_getLogs"]
    assert result["wallets"] == count and len(logs) == 2
    assert len(logs[0]["params"][0]["topics"][2]) == count
    assert len(logs[1]["params"][0]["topics"][1]) == count
    assert result["rpc_requests"] == len(requests) == 5
    assert result["rpc_bytes"] == chain.response_bytes_total > 0
    state = _state(conn)
    assert state["scanned_block"] == state["high_water_block"] == SELL_BLOCK + 10


def test_rpc_retry_after_blocks_requests_until_due_and_then_recovers(conn: Any) -> None:
    import json

    import httpx

    from tests.integration.test_news_chain_tape import _Db
    from tracefold.integrations.robinhood_chain import RobinhoodChainClient
    from tracefold.news.chain_tape.loop import ChainTapeLoop

    version = _seed_roster(conn, [SELL_WALLET])
    _seed_cursor(conn, block=SELL_BLOCK - 100, roster_version=version)
    clock, blocked, calls = [1_900_000_000_000], [True], []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "eth_getLogs" and blocked[0]:
            return httpx.Response(429, headers={"Retry-After": "900"})
        result = (
            hex(SELL_BLOCK + 40)
            if body["method"] == "eth_blockNumber"
            else ([] if body["method"] == "eth_getLogs" else {"timestamp": hex(1_788_642_791)})
        )
        return httpx.Response(200, json={"result": result})

    chain = RobinhoodChainClient(transport=httpx.MockTransport(handler))
    task = ChainTapeLoop(db=_Db(conn), chain=chain, clock=lambda: clock[0])

    async def run():
        try:
            await task.advance()
            state = _state(conn)
            assert state["next_attempt_at_ms"] == clock[0] + 900_000
            assert state["high_water_block"] == SELL_BLOCK - 100
            count = len(calls)
            assert (await task.advance())["deferred"] and len(calls) == count
            clock[0] += 900_000
            blocked[0] = False
            await task.advance()
            assert _state(conn)["scanned_block"] == SELL_BLOCK + 10
        finally:
            await task.aclose()

    asyncio.run(run())
