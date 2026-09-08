"""Focused real-persistence regressions found during the #614 independent review."""

from __future__ import annotations

import asyncio
from contextlib import closing
from decimal import Decimal

import pytest

from tests.integration.test_news_chain_tape_cards import (
    CHAIN_ID,
    CONSOLE,
    MADETEST,
    NOW,
    SELL_BLOCK,
    SELL_WALLET,
    UNIT,
    _Chain,
    _Clock,
    _Db,
    _deriver,
    _fill,
    _Mark,
    _member,
    _Prices,
    _roster,
    _rows,
    _seed,
    _Sender,
    _Site,
)
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_notifications import MarketNotificationLoop
from tracefold.news.wallet_contracts import OUTCOME_GIVE_UP_MS

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    with closing(connect_postgres_test(read_only=False)) as connection:
        yield connection


def test_recovered_stale_buys_do_not_send_a_crowding_alert(conn) -> None:
    clock = _Clock(NOW + 3_600_000)
    db = _Db(conn)
    members = [_member("0x" + f"{index:040x}", handle=f"buyer{index}") for index in range(1, 4)]
    fills = [
        _fill(
            wallet=member.wallet,
            token=MADETEST,
            kind="buy",
            amount_raw=100 * UNIT,
            usd="1500",
            event_at_ms=NOW - 1_000,
            received_at_ms=NOW,
            tx_hash="0x" + f"{index:064x}",
            block_number=SELL_BLOCK + index,
            log_index=index,
        )
        for index, member in enumerate(members, 1)
    ]
    _seed(conn, members, fills)
    asyncio.run(_deriver(db, _Chain(), _Site(), _Prices(), clock).advance())
    buys = _rows(conn, "SELECT evidence FROM news_market_wallet_events WHERE kind='buy'")
    assert len(buys) == 3
    assert all(row["evidence"]["selection_reason"] == "stale" for row in buys)
    sender = _Sender()
    asyncio.run(MarketNotificationLoop(db=db, sender=sender, console_base_url=CONSOLE, clock=clock).advance())
    assert sender.cards == []


def test_same_second_later_block_uses_previous_selected_buy(conn) -> None:
    db, clock = _Db(conn), _Clock()
    fills = [
        _fill(
            wallet=SELL_WALLET,
            token=MADETEST,
            kind="buy",
            amount_raw=100 * UNIT,
            usd=usd,
            event_at_ms=NOW - 1_000,
            received_at_ms=NOW,
            tx_hash="0x" + f"{index:064x}",
            block_number=SELL_BLOCK + index,
            log_index=log_index,
        )
        for index, (usd, log_index) in enumerate((("1500", 9), ("100", 1)), 1)
    ]
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], fills)
    asyncio.run(_deriver(db, _Chain(), _Site(), _Prices(), clock).advance())
    rows = _rows(conn, "SELECT evidence FROM news_market_wallet_events WHERE kind='buy' ORDER BY block_number")
    assert [row["evidence"]["selection_reason"] for row in rows] == ["selected", "same_window"]


def test_quote_arriving_outside_grace_is_not_backdated_to_query_start(conn) -> None:
    db, clock = _Db(conn), _Clock()
    site = _Site()
    site.token_marks = {MADETEST: _Mark(token=MADETEST, symbol="MADETEST", mark=1.5, liquidity=10_000)}
    fill = _fill(
        wallet=SELL_WALLET,
        token=MADETEST,
        kind="buy",
        amount_raw=10 * UNIT,
        usd="10",
        event_at_ms=NOW - 1_000,
        received_at_ms=NOW,
        tx_hash="0x" + "77" * 32,
    )
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], [fill])
    asyncio.run(_deriver(db, _Chain(), site, _Prices(), clock).advance())
    clock.advance(900_000 + OUTCOME_GIVE_UP_MS - 1_000)

    class DelayedPrice(_Prices):
        async def token_price(self, address: str) -> Decimal:
            clock.advance(2_000)
            return Decimal("1.2")

    asyncio.run(_deriver(db, _Chain(), site, DelayedPrice(), clock).take_outcomes([]))
    row = _rows(conn, "SELECT * FROM news_market_wallet_outcomes WHERE horizon='15m'")[0]
    assert (row["price"], row["source"], row["at_ms"]) == (None, "unavailable", clock())


def test_crowding_entry_price_uses_chain_order_within_one_timestamp(conn) -> None:
    fills = [
        _fill(
            wallet=SELL_WALLET,
            token=MADETEST,
            kind="buy",
            amount_raw=100 * UNIT,
            usd=usd,
            event_at_ms=NOW - 1_000,
            received_at_ms=NOW,
            tx_hash="0x" + f"{index:064x}",
            log_index=index,
        )
        for index, usd in enumerate(("1000", "100"), 1)
    ]
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], fills)
    buyers = repositories_for_connection(conn).news.chain_tape_crowding_buyers(
        chain_id=CHAIN_ID, token=MADETEST, from_ms=NOW - 900_000, to_ms=NOW
    )
    assert len(buyers) == 1
    assert buyers[0].price == Decimal("10")


def test_crowding_lead_uses_chain_order_before_wallet_alphabetical_order(conn) -> None:
    members = [_member("0x" + f"{index:040x}", handle=f"buyer{index}") for index in (3, 2, 1)]
    fills = [
        _fill(
            wallet=member.wallet,
            token=MADETEST,
            kind="buy",
            amount_raw=100 * UNIT,
            usd=str(index * 1000),
            event_at_ms=NOW - 1_000,
            received_at_ms=NOW,
            tx_hash="0x" + f"{index:064x}",
            log_index=index,
        )
        for index, member in enumerate(members, 1)
    ]
    _seed(conn, members, fills)
    asyncio.run(_deriver(_Db(conn), _Chain(), _Site(), _Prices(), _Clock()).advance())
    row = _rows(conn, "SELECT wallet, premium_bps FROM news_market_wallet_events WHERE kind='crowding'")[0]
    assert (row["wallet"], row["premium_bps"]) == (members[0].wallet, 15000)


def test_unpriceable_oldest_candidates_do_not_starve_a_priceable_same_horizon(conn) -> None:
    clock, db, site = _Clock(), _Db(conn), _Site()
    members = [_member("0x" + f"{index:040x}", handle=f"buyer{index}") for index in range(1, 4)]
    missing_token = "0x" + "ab" * 20
    fills = [
        _fill(
            wallet=member.wallet,
            token=MADETEST if index == 3 else missing_token,
            kind="buy",
            amount_raw=100 * UNIT,
            usd="100",
            event_at_ms=NOW - 1_000,
            received_at_ms=NOW,
            tx_hash="0x" + f"{index:064x}",
            block_number=SELL_BLOCK + index,
            log_index=index,
        )
        for index, member in enumerate(members, 1)
    ]
    _seed(conn, members, fills)
    prices = _Prices({MADETEST: Decimal("1.2")})
    deriver = _deriver(db, _Chain(), site, prices, clock)
    for fill in fills:
        asyncio.run(deriver.derive([fill], roster=_roster(conn), errors=[]))
        clock.advance(1)
    clock.at_ms = NOW + 900_000 + 10
    for _ in range(30):
        asyncio.run(deriver.take_outcomes([]))
        clock.advance(30_000)
    # Bank the expired oldest rows, then examine the next candidate.
    asyncio.run(deriver.take_outcomes([]))
    asyncio.run(deriver.take_outcomes([]))
    row = _rows(
        conn,
        """SELECT o.price FROM news_market_wallet_outcomes o
             JOIN news_market_wallet_events e ON e.item_id=o.item_id
            WHERE e.token=%s AND o.horizon='15m'""",
        (MADETEST,),
    )[0]
    assert row["price"] == Decimal("1.2")


def test_cached_fallback_marks_keep_their_fetch_time_after_a_slow_second_quote(conn) -> None:
    clock, db = _Clock(), _Db(conn)
    second_token = "0x" + "ab" * 20

    class TimedSite(_Site):
        def __init__(self) -> None:
            super().__init__()
            self.fetched_at: list[int] = []
            self.token_marks = {
                MADETEST: _Mark(MADETEST, "MADETEST", 1.5, 10_000),
                second_token: _Mark(second_token, "SECOND", 2.5, 10_000),
            }

        async def marks(self):
            clock.advance(25)
            self.fetched_at.append(clock())
            return await super().marks()

    class SlowSecondQuote(_Prices):
        async def token_price(self, address: str) -> None:
            self.calls.append(address)
            if len(self.calls) == 2:
                clock.advance(5_000)

    fills = [
        _fill(
            wallet=SELL_WALLET,
            token=token,
            kind="buy",
            amount_raw=10 * UNIT,
            usd="10",
            event_at_ms=NOW - 1_000,
            received_at_ms=NOW,
            tx_hash="0x" + f"{index:064x}",
            log_index=index,
        )
        for index, token in enumerate((MADETEST, second_token), 1)
    ]
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], fills)
    site = TimedSite()
    asyncio.run(_deriver(db, _Chain(), site, _Prices(), clock).advance())
    site.fetched_at.clear()
    clock.advance(900_000)
    prices = SlowSecondQuote()

    result = asyncio.run(_deriver(db, _Chain(), site, prices, clock).take_outcomes([]))

    assert result.outcomes == 2
    assert len(prices.calls) == 2 and set(prices.calls) == {MADETEST, second_token}
    assert len(site.fetched_at) == 1
    fetched_at = site.fetched_at[0]
    assert clock() == fetched_at + 5_000
    rows = _rows(
        conn,
        """SELECT e.token, o.price, o.source, o.at_ms FROM news_market_wallet_outcomes o
             JOIN news_market_wallet_events e ON e.item_id=o.item_id WHERE o.horizon='15m'""",
    )
    assert {row["token"]: row["price"] for row in rows} == {
        MADETEST: Decimal("1.5"),
        second_token: Decimal("2.5"),
    }
    assert all(row["source"] == "robinhoodtrenches_mark" and row["at_ms"] == fetched_at for row in rows)
