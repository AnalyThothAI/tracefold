"""The whole wallet loop against real PostgreSQL: fills in, a Feishu card out, a price receipt after.

The rules are proved next door with no database (`tests/news/test_news_chain_tape_rules.py`), and the
two provider adapters are proved against recorded responses at their own boundary. What is proved here
is everything only PostgreSQL can answer, end to end and in one pass:

* a live sell of a roster wallet becomes a `news_items` row with `market_kind = 'wallet'`, its
  `news_market_wallet_events` fact and its `news_market_wallet_checks` row, in one transaction and
  through the same `admit_market_item` the provider's four market kinds go through;
* the existing `MarketNotificationLoop` reads that Item with no branch of its own, groups it on the
  wallet family's key, and hands the send entry a Feishu card whose exact JSON is asserted;
* the +1h receipt lands in `news_market_wallet_outcomes` from a recorded DexScreener answer;
* the 24-hour backfill the tape was seeded with is context and never a card.

Every external answer is replayed: the chain's `balanceOf`, the provider's bags and marks, and
DexScreener's token document. No network.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.chain_tape.contracts import ClassifiedFill, RosterMember
from tracefold.news.chain_tape.derive import WalletCardDeriver
from tracefold.news.chain_tape.digest_writer import WalletDigestWriter
from tracefold.news.chain_tape.rules import WalletRules
from tracefold.news.market_notifications import MarketNotificationLoop
from tracefold.news.wallet_contracts import OUTCOME_GIVE_UP_MS, OUTCOME_PRICE_MIN

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "chain_tape"
# The byte-for-byte card a reader receives. Regenerate it deliberately with
# `TRACEFOLD_RECORD_WALLET_CARD=1`, and read the diff: this file is the wallet family's rendered
# contract, and a change to it is a change to what a reader sees.
EXIT_CARD = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "wallet_exit_card.json"
# The digest a reader receives, byte for byte. `TRACEFOLD_RECORD_WALLET_CARD=1` rewrites both.
DIGEST_CARD = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "wallet_digest_card.json"

CHAIN_ID = 4663
NOW = 1_788_642_800_000
SELL_TX = "0x5c10c3cf9b3a5ef265de9ea87e0b4c787583ef11823ea233fde27528ab9ac5f0"
SELL_WALLET = "0x69326e48f68500fb6cf3b3a7da640737b9cc347b"
FSD = "0x8de9018c1bb82884245f06dede9fe2bebabd1e18"
MADETEST = "0x5d191e73445cd5eb03cbaa56c263f1f9e9a4fcb3"
SELL_BLOCK = 55_432_994
# The recorded `balanceOf` at `SELL_BLOCK - 1`: the wallet held exactly what it sold (#572 §3.3).
FSD_HELD_RAW = 9_412_641_983_109_562_000_000_000
# The recorded sale settled for $3,608.60, which the medium tier's $20,000 position floor would not
# admit -- and that is the tier working, not a defect. The dollar figures below are scaled so the same
# recorded quantities clear the floor; the quantity, the balance and the identities stay the recorded
# ones, because those are what the ratio and the card's evidence are computed from.
SALE_USD = "23531.60"
FSD_MARK = 0.0025
UNIT = 10**18
CONSOLE = "https://tracefold-win.big9er.com"


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


class _Db:
    """The News database port over one real connection, in the two shapes both loops use."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.names: list[str] = []

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        return fn(repositories_for_connection(self.connection))

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        repos = repositories_for_connection(self.connection)
        with repos.transaction():
            return fn(repos)

    async def quotes_for_symbols(self, symbols: Sequence[str], *, now_ms: int) -> list[dict[str, Any]]:
        # A Robinhood Chain token is in no venue catalogue; the wallet card asks for no quote at all,
        # and this is here to prove it rather than to answer it.
        raise AssertionError(f"a wallet card asked for a quote: {list(symbols)}")


class _Chain:
    """The recorded chain, answering the one state read the exit rule makes."""

    chain_id = CHAIN_ID

    def __init__(self) -> None:
        self.balances: dict[tuple[str, str, int], int | None] = {
            (FSD, SELL_WALLET, SELL_BLOCK - 1): FSD_HELD_RAW,
        }
        self.calls: list[tuple[str, str, int]] = []

    async def balance_of(self, token: str, wallet: str, *, block_number: int) -> int | None:
        key = (token, wallet, int(block_number))
        self.calls.append(key)
        # Everything else is outside the public node's ~10-minute state window.
        return self.balances.get(key)

    async def balance_before_transfer(
        self, token: str, wallet: str, *, block_number: int, log_index: int
    ) -> int | None:
        return self.balances.get((token, wallet, block_number - 1))


@dataclass(frozen=True, slots=True)
class _Bag:
    token: str
    symbol: str
    amount: float
    avg_price: float
    cost_usd: float
    opened_at_ms: int


@dataclass(frozen=True, slots=True)
class _Mark:
    token: str
    symbol: str
    mark: float | None
    liquidity: float | None


class _Site:
    """The provider's own context endpoints. A failure raises, exactly as the real adapter's does.

    That is the distinction the exit rule's third tier turns on: `bags_by_handle` answering `()` is the
    site saying this wallet holds nothing, and `fail_with` is the site saying nothing at all.
    """

    def __init__(self) -> None:
        self.bags_by_handle: dict[str, tuple[_Bag, ...]] = {}
        self.token_marks: dict[str, _Mark] = {
            FSD: _Mark(FSD, "FSD", FSD_MARK, 412_000.0),
            MADETEST: _Mark(MADETEST, "MADETEST", 0.00041, 210_000.0),
        }
        self.fail_with: BaseException | None = None

    async def bags(self, handle: str) -> tuple[_Bag, ...]:
        if self.fail_with is not None:
            raise self.fail_with
        return self.bags_by_handle.get(handle, ())

    async def marks(self) -> Mapping[str, _Mark]:
        if self.fail_with is not None:
            raise self.fail_with
        return self.token_marks


class _Prices:
    """DexScreener, replayed. `None` is "not indexed", which is an answer and not a failure."""

    def __init__(self, prices: Mapping[str, Decimal | None] | None = None) -> None:
        self.prices = dict(prices or {})
        self.calls: list[str] = []

    async def token_price(self, address: str) -> Decimal | None:
        self.calls.append(address)
        return self.prices.get(address)


class _Sender:
    """The shared prepared-card send entry, recording exactly what a channel would have received."""

    available = True

    def __init__(self) -> None:
        self.cards: list[dict[str, Any]] = []

    async def send_prepared_card(self, card: Any, *, channel_payload: Mapping[str, Any], operation: str = "") -> Any:
        del card, operation
        self.cards.append(dict(channel_payload))
        return {"provider": "feishu", "message_id": len(self.cards)}


class _Clock:
    def __init__(self, at_ms: int = NOW) -> None:
        self.at_ms = at_ms

    def __call__(self) -> int:
        return self.at_ms

    def advance(self, ms: int) -> None:
        self.at_ms += ms


def _member(wallet: str, *, handle: str, followers: int = 123_456, rank: int = 1) -> RosterMember:
    return RosterMember(
        wallet=wallet,
        handle=handle,
        followers=followers,
        realized_pnl=510_000.0,
        closed_trades=46,
        win_rate=0.44,
        profit_factor=1.6,
        open_cost=220_000.0,
        rank_quality=rank,
        rank_whale=None,
    )


def _fill(
    *,
    wallet: str,
    token: str,
    kind: str,
    amount_raw: int,
    usd: str | None,
    event_at_ms: int,
    received_at_ms: int,
    tx_hash: str,
    log_index: int = 6,
    block_number: int = SELL_BLOCK,
    symbol: str = "FSD",
    roster_version: int = 1,
) -> ClassifiedFill:
    return ClassifiedFill(
        chain_id=CHAIN_ID,
        tx_hash=tx_hash,
        log_index=log_index,
        block_number=block_number,
        block_hash="0x" + "cd" * 32,
        wallet=wallet,
        token=token,
        kind=kind,  # type: ignore[arg-type]
        amount_raw=amount_raw,
        event_at_ms=event_at_ms,
        received_at_ms=received_at_ms,
        classified_at_ms=received_at_ms,
        roster_version=roster_version,
        token_symbol=symbol,
        token_decimals=18,
        cash_token=None if usd is None else "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
        cash_amount_raw=None if usd is None else int(Decimal(usd) * 10**6),
        cash_decimals=None if usd is None else 6,
        usd=None if usd is None else Decimal(usd),
        usd_source=None if usd is None else "usdg_cash_leg",
    )


def _seed(conn: Any, members: Sequence[RosterMember], fills: Sequence[ClassifiedFill]) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_store_roster(list(members), now_ms=NOW - 3_600_000)
        repos.news.chain_tape_record_fills(list(fills))


def _seed_fills(conn: Any, fills: Sequence[ClassifiedFill]) -> None:
    """More fills, later, with the roster left exactly as it is."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_record_fills(list(fills))


def _deriver(db: _Db, chain: _Chain, site: _Site, prices: _Prices, clock: _Clock) -> WalletCardDeriver:
    return WalletCardDeriver(
        db=db,
        chain=chain,
        site=site,
        prices=prices,
        rules=WalletRules(exit_notifications_enabled=True),
        clock=clock,
    )


def _rows(conn: Any, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(statement, tuple(params)).fetchall()]


# ------------------------------------------------------------------------------------ the whole loop
def test_wallet_mute_keeps_buy_research_digest_and_price_receipts(conn) -> None:
    clock = _Clock()
    db, chain, site = _Db(conn), _Chain(), _Site()
    prices = _Prices({MADETEST: Decimal("0.0005")})
    buy = _fill(
        wallet=SELL_WALLET,
        token=MADETEST,
        kind="buy",
        amount_raw=1_000 * UNIT,
        usd="1500",
        event_at_ms=NOW - 1000,
        received_at_ms=NOW,
        tx_hash="0x" + "61" * 32,
        symbol="MADETEST",
    )
    buyers = [SELL_WALLET, "0x" + "22" * 20, "0x" + "33" * 20]
    fills = [replace(buy, wallet=wallet, tx_hash="0x" + f"{index + 1:064x}") for index, wallet in enumerate(buyers)]
    fills.append(
        _fill(
            wallet=SELL_WALLET,
            token=FSD,
            kind="sell",
            amount_raw=FSD_HELD_RAW,
            usd=SALE_USD,
            event_at_ms=NOW - 500,
            received_at_ms=NOW,
            tx_hash=SELL_TX,
        )
    )
    _seed(conn, [_member(wallet, handle=f"buyer-{index}") for index, wallet in enumerate(buyers)], fills)
    research = _deriver(db, chain, site, prices, clock)
    asyncio.run(research.advance())
    assert asyncio.run(_digest_writer(db, clock, site=site).take_digest(roster=_roster(conn), errors=[])).digests == 1
    facts = _rows(conn, "SELECT * FROM news_market_wallet_events ORDER BY item_id")
    assert {row["kind"] for row in facts} == {"buy", "exit", "crowding", "digest"}
    sender = _Sender()
    loop = MarketNotificationLoop(db=db, sender=sender, clock=clock)
    loop.wallet_notifications_enabled = False
    asyncio.run(loop.start())
    turn = asyncio.run(loop.advance())
    assert (turn.sent, turn.intents, sender.cards) == (0, 0, [])
    assert _rows(conn, "SELECT * FROM news_market_wallet_events ORDER BY item_id") == facts
    assert repositories_for_connection(conn).news.chain_tape_pending_fills() == []
    assert {
        repositories_for_connection(conn).news.market_item(item_id=fact["item_id"])["notification_status"]
        for fact in facts
    } == {"not_alerted"}
    clock.at_ms = NOW + 900_000
    asyncio.run(research.take_outcomes([]))
    assert _rows(conn, "SELECT * FROM news_market_wallet_outcomes")
    # A new process with notifications enabled has no saved cards to catch up on.
    resumed = MarketNotificationLoop(db=db, sender=sender, clock=clock)
    asyncio.run(resumed.start())
    assert asyncio.run(resumed.advance()).sent == 0
    assert not sender.cards


@pytest.mark.parametrize("prior", ["queued", "retry", "sent", "unknown"])
def test_wallet_mute_stops_pending_cards_and_preserves_attempt_evidence(conn, prior) -> None:
    from tests.integration.test_news_market_notifications import _Refused
    from tests.integration.test_news_market_notifications import _Sender as OutcomeSender

    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    buy = _fill(
        wallet=SELL_WALLET,
        token=MADETEST,
        kind="buy",
        amount_raw=1_000 * UNIT,
        usd="1500",
        event_at_ms=NOW - 1000,
        received_at_ms=NOW,
        tx_hash="0x" + "61" * 32,
        symbol="MADETEST",
    )
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], [buy])
    asyncio.run(_deriver(db, chain, site, prices, clock).advance())
    before_sender = OutcomeSender(available=prior != "queued")
    if prior in {"retry", "unknown"}:
        before_sender.raise_with = _Refused(
            "provider_failure", commit_phase="not_sent" if prior == "retry" else "unknown", retryable=True
        )
    asyncio.run(MarketNotificationLoop(db=db, sender=before_sender, clock=clock).advance())
    before = _rows(conn, "SELECT * FROM news_market_deliveries")[0]
    assert before["state"] == {"queued": "unavailable", "retry": "pending"}.get(prior, prior)
    sender = _Sender()
    muted = MarketNotificationLoop(db=db, sender=sender, clock=clock, wallet_notifications_enabled=False)
    asyncio.run(muted.start())
    asyncio.run(muted.advance())
    after = _rows(conn, "SELECT * FROM news_market_deliveries")[0]
    if prior in {"queued", "retry"}:
        assert (after["state"], after["error"]) == ("failed", "wallet_notifications_disabled")
        for field in ("attempts", "card", "receipt", "first_attempt_at_ms", "last_attempt_at_ms"):
            assert after[field] == before[field]
    else:
        assert after == before
    clock.at_ms += 60_000
    assert asyncio.run(MarketNotificationLoop(db=db, sender=sender, clock=clock).advance()).sent == 0
    assert not sender.cards


def test_wallet_mute_does_not_adopt_silenced_observations_when_reenabled(conn) -> None:
    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    first = _fill(
        wallet=SELL_WALLET,
        token=MADETEST,
        kind="buy",
        amount_raw=1_000 * UNIT,
        usd="1500",
        event_at_ms=NOW - 1000,
        received_at_ms=NOW,
        tx_hash="0x" + "61" * 32,
        symbol="MADETEST",
    )
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], [first])
    research = _deriver(db, chain, site, prices, clock)
    asyncio.run(research.advance())
    sender = _Sender()
    muted = MarketNotificationLoop(db=db, sender=sender, clock=clock, wallet_notifications_enabled=False)
    asyncio.run(muted.advance())
    first_item = _rows(conn, "SELECT item_id FROM news_market_wallet_events")[0]["item_id"]
    # Same receive millisecond and group: only the new candidate joins the new card.
    second = replace(first, tx_hash="0x" + "62" * 32, log_index=first.log_index + 1, usd=Decimal("2000"))
    _seed_fills(conn, [second])
    asyncio.run(research.advance())
    resumed = MarketNotificationLoop(db=db, sender=sender, clock=clock)
    assert asyncio.run(resumed.advance()).sent == 1
    assert _rows(conn, "SELECT market_notify_delivery_key FROM news_items WHERE item_id=%s", (first_item,)) == [
        {"market_notify_delivery_key": None}
    ]
    assert _rows(conn, "SELECT covered_count FROM news_market_deliveries") == [{"covered_count": 1}]


def test_single_wallet_buy_is_a_research_candidate_even_when_not_notified(conn) -> None:
    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    member = _member(SELL_WALLET, handle="buyer")
    buy = _fill(
        wallet=SELL_WALLET,
        token=MADETEST,
        kind="buy",
        amount_raw=1_000 * UNIT,
        usd="10",
        event_at_ms=NOW - 1_000,
        received_at_ms=NOW,
        tx_hash="0x" + "61" * 32,
        symbol="MADETEST",
    )
    _seed(conn, [member], [buy])
    errors: list[str] = []
    asyncio.run(_deriver(db, chain, site, prices, clock).derive((buy,), roster=_roster(conn), errors=errors))
    candidates = _rows(conn, "SELECT * FROM news_market_wallet_events WHERE kind = 'buy'")
    assert len(candidates) == 1
    assert candidates[0]["evidence"]["selection_reason"] == "below_minimum"
    assert candidates[0]["evidence"]["stage"] == "first_observed"
    assert candidates[0]["evidence"]["observed_at_ms"] == NOW
    sender = _Sender()
    asyncio.run(MarketNotificationLoop(db=db, sender=sender, console_base_url=CONSOLE, clock=clock).advance())
    assert not sender.cards


@pytest.mark.parametrize("split", [False, True])
def test_buy_threshold_repeats_and_restart_preserve_exact_candidates(conn, split) -> None:
    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
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
            symbol="MADETEST",
        )
        for index, usd in enumerate(("600", "600", "100", "1300"), 1)
    ]
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], [] if split else fills)
    if split:
        for fill in fills:
            _seed_fills(conn, [fill])
            asyncio.run(_deriver(db, chain, site, prices, clock).advance())
    else:
        asyncio.run(_deriver(db, chain, site, prices, clock).advance())
    # Recreate the process and advance twice; persisted checkpoints carry progress.
    asyncio.run(_deriver(db, chain, site, prices, clock).advance())
    events = _rows(
        conn, "SELECT * FROM news_market_wallet_events WHERE kind='buy' ORDER BY (evidence->>'log_index')::int"
    )
    assert [e["evidence"]["selection_reason"] for e in events] == [
        "below_minimum",
        "selected",
        "same_window",
        "selected",
    ]
    assert [int(e["usd"]) for e in events] == [600, 1200, 1300, 2600]
    assert [e["evidence"]["buy_count"] for e in events] == [1, 2, 3, 4]
    assert repositories_for_connection(conn).news.chain_tape_pending_fills() == []
    assert len({e["item_id"] for e in events}) == 4


@pytest.mark.parametrize(
    ("balance", "prior", "expected"),
    [
        (None, False, "first_observed"),
        (None, True, "unknown"),
        (0, False, "new_position"),
        (0, True, "reentry"),
        (10 * UNIT, True, "add"),
    ],
)
def test_buy_stage_requires_pre_transfer_balance_evidence(conn, balance, prior, expected) -> None:
    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    chain.balances[(MADETEST, SELL_WALLET, SELL_BLOCK - 1)] = balance
    buy = _fill(
        wallet=SELL_WALLET,
        token=MADETEST,
        kind="buy",
        amount_raw=100 * UNIT,
        usd="1500",
        event_at_ms=NOW - 1000,
        received_at_ms=NOW,
        tx_hash="0x" + "61" * 32,
    )
    old = replace(
        buy,
        block_number=SELL_BLOCK - 1,
        tx_hash="0x" + "60" * 32,
        event_at_ms=NOW - 3_600_000,
        received_at_ms=NOW - 3_600_000,
    )
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], [old, buy] if prior else [buy])
    errors: list[str] = []
    asyncio.run(_deriver(db, chain, site, prices, clock).derive([buy], roster=_roster(conn), errors=errors))
    assert errors == []
    evidence = _rows(conn, "SELECT evidence FROM news_market_wallet_events WHERE kind='buy'")[0]["evidence"]
    assert evidence["stage"] == expected
    assert evidence["history_complete"] is False


def test_unsent_candidate_receipts_use_observation_price_and_expired_horizons_stay_missing(conn) -> None:
    clock = _Clock()
    db, chain, site = _Db(conn), _Chain(), _Site()
    site.token_marks = {MADETEST: _Mark(token=MADETEST, symbol="MADETEST", mark=1.5, liquidity=10_000)}
    prices = _Prices({MADETEST: Decimal("1.5")})
    buy = _fill(
        wallet=SELL_WALLET,
        token=MADETEST,
        kind="buy",
        amount_raw=10 * UNIT,
        usd="10",
        event_at_ms=NOW - 1000,
        received_at_ms=NOW,
        tx_hash="0x" + "62" * 32,
    )
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], [buy])
    asyncio.run(_deriver(db, chain, site, prices, clock).advance())
    clock.advance(900_000)
    prices.prices[MADETEST] = Decimal("1.2")
    result = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes([]))
    assert (result.outcomes, result.unavailable) == (1, 0)
    row = _rows(conn, "SELECT * FROM news_market_wallet_outcomes")[0]
    assert row["delivery_key"] is None
    assert row["reference_price"] == Decimal("1.5")
    assert (row["reference_at_ms"], row["target_at_ms"], row["at_ms"]) == (NOW, NOW + 900_000, NOW + 900_000)
    cards = repositories_for_connection(conn).news.chain_tape_cards(
        from_ms=NOW - 10_000, to_ms=clock() + 1, limit=10, kind="buy"
    )
    assert cards[0]["outcomes"][0]["return_bps"] == -2000
    clock.advance(3_600_000 + OUTCOME_GIVE_UP_MS)
    result = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes([]))
    assert (result.outcomes, result.unavailable) == (0, 1)
    missed = _rows(conn, "SELECT * FROM news_market_wallet_outcomes WHERE horizon='1h'")[0]
    assert missed["price"] is None and missed["source"] == "unavailable"


def test_a_live_exit_becomes_an_item_a_card_and_a_price_receipt(conn) -> None:
    """#572 PR-2 end to end, on the recorded FSD sale that #572 §3.3 measured as a 100% exit."""

    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices({FSD: Decimal("0.00019")})
    seller = _member(SELL_WALLET, handle="0xVantaa")
    site.bags_by_handle["0xVantaa"] = (
        _Bag(token=FSD, symbol="FSD", amount=0.0, avg_price=0.0018, cost_usd=16_900.0, opened_at_ms=NOW - 7_200_000),
    )
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=FSD_HELD_RAW,
        usd=SALE_USD,
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [seller], [sell])

    errors: list[str] = []
    derived = asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))

    assert errors == []
    assert (derived.checks, derived.exits) == (1, 1)
    # The denominator came from the chain, and the check row says so whatever the card printed.
    check = _rows(conn, "SELECT * FROM news_market_wallet_checks")[0]
    assert (check["basis"], int(check["ratio_bps"])) == ("chain_balance", 10_000)
    assert int(check["q_before_raw"]) == FSD_HELD_RAW
    assert chain.calls == [(FSD, SELL_WALLET, SELL_BLOCK - 1)]

    # One ordinary market Item, pending for the loop that already exists.
    item = _rows(conn, "SELECT item_id, market_kind, market_parse_status, market_notify_state FROM news_items")[0]
    assert (item["market_kind"], item["market_parse_status"], item["market_notify_state"]) == (
        "wallet",
        "parsed",
        "pending",
    )
    event = _rows(conn, "SELECT * FROM news_market_wallet_events")[0]
    assert event["kind"] == "exit"
    assert event["provider"] == "robinhood_chain"
    assert bool(event["closed"]) is True
    assert Decimal(event["position_usd"]) > Decimal("20000")
    # The evidence names the movement the rule read, so "which fill is this card about" is answerable
    # from the row rather than from a reconstruction.
    assert event["evidence"]["fill"] == {"chain_id": CHAIN_ID, "tx_hash": SELL_TX, "log_index": 6}

    # --- the existing notification loop, with no branch of its own for this family -----------------
    sender = _Sender()
    turn = asyncio.run(MarketNotificationLoop(db=db, sender=sender, console_base_url=CONSOLE, clock=clock).advance())

    assert (turn.observations, turn.groups, turn.intents, turn.sent) == (1, 1, 1, 1)
    card = sender.cards[0]
    if os.environ.get("TRACEFOLD_RECORD_WALLET_CARD"):  # pragma: no cover - recording aid
        EXIT_CARD.write_text(json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert card == json.loads(EXIT_CARD.read_text(encoding="utf-8"))

    delivery = _rows(conn, "SELECT delivery_key, market_kind, state FROM news_market_deliveries")[0]
    assert (delivery["market_kind"], delivery["state"]) == ("wallet", "sent")

    # --- the price receipt ------------------------------------------------------------------------
    clock.advance(3_600_000 + 1_000)
    receipts = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert (receipts.outcomes, receipts.unavailable) == (1, 1)
    assert prices.calls == [FSD]
    outcome = _rows(conn, "SELECT * FROM news_market_wallet_outcomes WHERE horizon='1h'")[0]
    assert outcome["delivery_key"] == delivery["delivery_key"]
    assert (outcome["horizon"], outcome["source"]) == ("1h", "dexscreener_base_token_v1")
    assert Decimal(outcome["price"]) == Decimal("0.00019")

    # The four-hour horizon is not due yet, and "not due" is the absence of a row.
    assert len(_rows(conn, "SELECT * FROM news_market_wallet_outcomes")) == 2


def test_exit_is_stored_but_does_not_notify_with_default_rules(conn) -> None:
    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=FSD_HELD_RAW,
        usd=SALE_USD,
        event_at_ms=NOW - 1000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [_member(SELL_WALLET, handle="seller")], [sell])
    deriver = WalletCardDeriver(db=db, chain=chain, site=site, prices=prices, clock=clock)
    result = asyncio.run(deriver.advance())
    assert result.exits == 1
    assert (
        _rows(conn, "SELECT evidence FROM news_market_wallet_events")[0]["evidence"]["selection_reason"]
        == "exit_disabled"
    )
    sender = _Sender()
    asyncio.run(MarketNotificationLoop(db=db, sender=sender, clock=clock).advance())
    assert sender.cards == []
    assert len(_rows(conn, "SELECT * FROM news_market_wallet_fills")) == 1


def test_database_refusal_rolls_back_candidate_and_checkpoint_then_replays(conn) -> None:
    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    buy = _fill(
        wallet=SELL_WALLET,
        token=MADETEST,
        kind="buy",
        amount_raw=1000 * UNIT,
        usd="2000",
        event_at_ms=NOW - 1000,
        received_at_ms=NOW,
        tx_hash="0x" + "63" * 32,
    )
    _seed(conn, [_member(SELL_WALLET, handle="buyer")], [buy])
    # Refuse the actual PostgreSQL write after admission has opened the Item.
    conn.execute("ALTER TABLE news_market_wallet_events ADD CONSTRAINT test_refuse_buy CHECK (kind <> 'buy')")
    conn.commit()
    errors: list[str] = []
    with pytest.raises(CheckViolation):
        asyncio.run(_deriver(db, chain, site, prices, clock).derive([buy], roster=_roster(conn), errors=errors))
    assert errors == []
    assert _rows(conn, "SELECT * FROM news_items") == []
    assert len(repositories_for_connection(conn).news.chain_tape_pending_fills()) == 1
    conn.execute("ALTER TABLE news_market_wallet_events DROP CONSTRAINT test_refuse_buy")
    conn.commit()
    result = asyncio.run(_deriver(db, chain, site, prices, clock).advance())
    assert result.buys == 1
    assert repositories_for_connection(conn).news.chain_tape_pending_fills() == []
    assert len(_rows(conn, "SELECT * FROM news_items")) == 1


def test_a_backfilled_fill_is_context_and_never_a_card(conn) -> None:
    """The 24-hour backfill exists to give the rules their window; it must not speak to a reader."""

    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    seller = _member(SELL_WALLET, handle="0xVantaa")
    backfilled = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=FSD_HELD_RAW,
        usd=SALE_USD,
        # A block from yesterday, read now: the two stamps are what tell them apart.
        event_at_ms=NOW - 20 * 3_600_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [seller], [backfilled])

    errors: list[str] = []
    derived = asyncio.run(
        _deriver(db, chain, site, prices, clock).derive((backfilled,), roster=_roster(conn), errors=errors)
    )

    assert (derived.checks, derived.exits, derived.crowding) == (0, 0, 0)
    assert _rows(conn, "SELECT item_id FROM news_items") == []
    assert chain.calls == []


def test_a_pruned_state_window_falls_back_to_the_reported_bag_and_labels_the_card(conn) -> None:
    """The relaxed rule, on the seam it exists for: the node has moved on, the card still goes out."""

    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    chain.balances.clear()
    # Nothing prices FSD from the provider either, so the position value is the price this very trade
    # printed: its dollars over its quantity.
    site.token_marks = {}
    seller = _member(SELL_WALLET, handle="0xVantaa")
    # The provider says a quarter of the position is still there, so the reconstructed denominator is
    # that plus what just left: a 75% exit, not a 100% one.
    site.bags_by_handle["0xVantaa"] = (
        _Bag(token=FSD, symbol="FSD", amount=3.0, avg_price=8000.0, cost_usd=24_000.0, opened_at_ms=NOW - 7_200_000),
    )
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=9 * UNIT,
        usd="72000",
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [seller], [sell])

    errors: list[str] = []
    derived = asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))

    assert (derived.checks, derived.exits) == (1, 1)
    check = _rows(conn, "SELECT * FROM news_market_wallet_checks")[0]
    assert check["basis"] == "site_reported"
    assert int(check["q_before_raw"]) == 12 * UNIT
    assert int(check["ratio_bps"]) == 7500
    # The failure that sent it here is recorded rather than swallowed.
    assert check["error"] == "rpc_state_unavailable"
    event = _rows(conn, "SELECT basis, ratio_bps, closed FROM news_market_wallet_events")[0]
    assert (event["basis"], int(event["ratio_bps"]), bool(event["closed"])) == ("site_reported", 7500, False)


def test_three_roster_wallets_in_one_window_open_a_crowding_card(conn) -> None:
    """The crowding rule against the real fills table, including the wallet that was already holding."""

    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    members = [
        _member(f"0x{index:040x}", handle=f"trader{index}", followers=50_000 * index, rank=index)
        for index in range(1, 5)
    ]
    buys = [
        _fill(
            wallet=members[0].wallet,
            token=MADETEST,
            kind="buy",
            amount_raw=4_000_000 * UNIT,
            usd="4000",
            event_at_ms=NOW - 600_000,
            received_at_ms=NOW - 590_000,
            tx_hash="0x" + "11" * 32,
            symbol="MADETEST",
        ),
        _fill(
            wallet=members[1].wallet,
            token=MADETEST,
            kind="buy",
            amount_raw=1_400_000 * UNIT,
            usd="2000",
            event_at_ms=NOW - 300_000,
            received_at_ms=NOW - 290_000,
            tx_hash="0x" + "22" * 32,
            symbol="MADETEST",
        ),
        # Already holding since well before the window: context, never a crowd.
        _fill(
            wallet=members[3].wallet,
            token=MADETEST,
            kind="buy",
            amount_raw=9_000_000 * UNIT,
            usd="9000",
            event_at_ms=NOW - 6 * 3_600_000,
            received_at_ms=NOW - 6 * 3_600_000,
            tx_hash="0x" + "44" * 32,
            symbol="MADETEST",
        ),
    ]
    trigger = _fill(
        wallet=members[2].wallet,
        token=MADETEST,
        kind="buy",
        amount_raw=1_000_000 * UNIT,
        usd="1500",
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash="0x" + "33" * 32,
        symbol="MADETEST",
    )
    _seed(conn, members, [*buys, trigger])

    errors: list[str] = []
    derived = asyncio.run(
        _deriver(db, chain, site, prices, clock).derive((trigger,), roster=_roster(conn), errors=errors)
    )

    assert (derived.crowding, derived.exits) == (1, 0)
    event = _rows(conn, "SELECT * FROM news_market_wallet_events WHERE kind='crowding'")[0]
    assert event["kind"] == "crowding"
    assert int(event["peer_wallets"]) == 3
    assert Decimal(event["peer_usd"]) == Decimal("7500")
    assert event["wallet"] == members[0].wallet
    assert event["handle"] == "trader1"
    # The lead's followers plus the two who followed; the holder is not on this card at all.
    assert int(event["followers"]) == 50_000 + 100_000 + 150_000
    assert Decimal(event["liquidity_usd"]) == Decimal("210000")
    assert [entry["wallet"] for entry in event["evidence"]["buyers"]] == [member.wallet for member in members[:3]]

    sender = _Sender()
    turn = asyncio.run(MarketNotificationLoop(db=db, sender=sender, console_base_url=CONSOLE, clock=clock).advance())

    assert turn.sent == 2
    card = next(card for card in sender.cards if "拥挤" in card["header"]["title"]["content"])
    body = card["elements"][0]["text"]["content"]
    assert "3 个名单地址买入" in body
    assert "领头 trader1" in body
    # The card's span is the window the rules folded together, not the single instant its Item carries:
    # one derived row stands for three wallets' first buys ten minutes apart.
    assert card["header"]["title"]["content"] == "链上钱包 · 拥挤 · 跟风偏晚 · MADETEST"
    assert body.splitlines()[0].endswith("05:03–05:12")


def test_a_wallet_item_reads_back_through_the_market_read_model(conn) -> None:
    """The detail route's own read: a `wallet` Item resolves with its facts and its own group key."""

    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    seller = _member(SELL_WALLET, handle="0xVantaa")
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=FSD_HELD_RAW,
        usd=SALE_USD,
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    site.bags_by_handle["0xVantaa"] = (
        _Bag(token=FSD, symbol="FSD", amount=0.0, avg_price=0.0018, cost_usd=16_900.0, opened_at_ms=NOW - 7_200_000),
    )
    _seed(conn, [seller], [sell])
    errors: list[str] = []
    asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))

    item_id = _rows(conn, "SELECT item_id FROM news_items")[0]["item_id"]
    detail = repositories_for_connection(conn).news.market_item(item_id=item_id)

    assert detail is not None
    assert detail["market_kind"] == "wallet"
    assert detail["provider"] == "robinhood_chain"
    assert detail["symbol"] == "FSD"
    assert detail["raw_instrument"] == FSD
    assert detail["wallet_kind"] == "exit"
    assert detail["wallet_basis"] == "chain_balance"
    assert int(detail["wallet_ratio_bps"]) == 10_000
    assert Decimal(detail["wallet_quantity"]) == Decimal(FSD_HELD_RAW) / Decimal(10**18)
    # The read model's key and the loop's key are one string: a page and a card must never disagree
    # about which card a card follows.
    assert detail["group_key"].startswith(f"wallet|exit|robinhood_chain|{SELL_WALLET}|{FSD}|")
    timeline = repositories_for_connection(conn).news.market_group_timeline(group_key=detail["group_key"])
    assert [row["item_id"] for row in timeline] == [item_id]


class _Digest:
    """A model that selects buy fact IDs and counts how often it was asked."""

    def __init__(self, fact_ids: Sequence[str] | None = None) -> None:
        self.fact_ids = tuple(fact_ids or ())
        self.calls = 0
        self.packs: list[str] = []

    async def summarize(self, *, facts_json: str) -> Sequence[str]:
        self.calls += 1
        self.packs.append(facts_json)
        return self.fact_ids


def _digest_writer(db: _Db, clock: _Clock, program: Any = None, *, site: Any = None) -> WalletDigestWriter:
    return WalletDigestWriter(db=db, program=program, bags=site, interval_s=14_400, clock=clock)


def test_a_due_window_becomes_a_digest_item_and_a_feishu_card(conn) -> None:
    """#572 PR-3 end to end on real PostgreSQL: window rows in, one `wallet` Item out, one card sent.

    The model selects an existing buy ID; the program renders the reader's exact factual text and
    preserves the selection audit beside the pack.
    """

    clock = _Clock()
    db, site = _Db(conn), _Site()
    seller = _member(SELL_WALLET, handle="0xVantaa")
    site.bags_by_handle["0xVantaa"] = (
        _Bag(token=FSD, symbol="FSD", amount=4.0, avg_price=0.0018, cost_usd=16_900.0, opened_at_ms=NOW - 7_200_000),
    )
    fills = [
        _fill(
            wallet=SELL_WALLET,
            token=FSD,
            kind="buy",
            amount_raw=8 * UNIT,
            usd="12340.50",
            event_at_ms=NOW - 3 * 3_600_000,
            received_at_ms=NOW - 3 * 3_600_000,
            tx_hash="0x" + "ab" * 32,
        ),
        _fill(
            wallet=SELL_WALLET,
            token=FSD,
            kind="sell",
            amount_raw=4 * UNIT,
            usd=SALE_USD,
            event_at_ms=NOW - 2 * 3_600_000,
            received_at_ms=NOW - 2 * 3_600_000,
            tx_hash=SELL_TX,
        ),
    ]
    _seed(conn, [seller], fills)
    program = _Digest(("b1",))
    errors: list[str] = []

    result = asyncio.run(_digest_writer(db, clock, program, site=site).take_digest(roster=_roster(conn), errors=errors))

    assert errors == []
    assert (result.digests, result.model_called, result.model_used) == (1, True, True)
    assert (result.lines_kept, result.lines_dropped) == (1, 0)
    assert program.calls == 1
    assert "已计价部分均价" in program.packs[0] and "本窗口首笔买入后卖出" in program.packs[0]
    assert "净现金回收线未知" in program.packs[0]

    event = _rows(conn, "SELECT * FROM news_market_wallet_events WHERE kind = 'digest'")[0]
    # A digest names no wallet and no token: the schema admits the empty pair for this kind alone.
    assert (event["wallet"], event["token"]) == ("", "")
    assert event["evidence"]["model_used"] is True
    assert (event["evidence"]["lines_kept"], event["evidence"]["lines_dropped"]) == (1, 0)
    assert len(event["evidence"]["pack_sha256"]) == 64
    assert event["evidence"]["model_authority"] == "buy_fact_selection"
    facts = {fact["id"]: fact["text"] for fact in event["evidence"]["facts"]}
    assert all(line["text"] == facts[line["cites"][0]] for line in event["evidence"]["lines"])
    item = _rows(conn, "SELECT market_kind, market_notify_state FROM news_items")[0]
    assert (item["market_kind"], item["market_notify_state"]) == ("wallet", "pending")

    sender = _Sender()
    turn = asyncio.run(MarketNotificationLoop(db=db, sender=sender, console_base_url=CONSOLE, clock=clock).advance())

    assert (turn.groups, turn.intents, turn.sent) == (1, 1, 1)
    card = sender.cards[0]
    if os.environ.get("TRACEFOLD_RECORD_WALLET_CARD"):  # pragma: no cover - recording aid
        DIGEST_CARD.write_text(json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert card == json.loads(DIGEST_CARD.read_text(encoding="utf-8"))
    delivery = _rows(conn, "SELECT market_kind, state FROM news_market_deliveries")[0]
    assert (delivery["market_kind"], delivery["state"]) == ("wallet", "sent")


def test_an_ungrounded_answer_sends_the_template_instead(conn) -> None:
    """An invented fact ID is rejected and the computed buy template is sent instead."""

    clock = _Clock()
    db, site = _Db(conn), _Site()
    seller = _member(SELL_WALLET, handle="0xVantaa")
    _seed(
        conn,
        [seller],
        [
            _fill(
                wallet=SELL_WALLET,
                token=FSD,
                kind="buy",
                amount_raw=8 * UNIT,
                usd="12340.50",
                event_at_ms=NOW - 3 * 3_600_000,
                received_at_ms=NOW - 3 * 3_600_000,
                tx_hash="0x" + "ab" * 32,
            )
        ],
    )
    program = _Digest(("b999",))
    errors: list[str] = []

    result = asyncio.run(_digest_writer(db, clock, program, site=site).take_digest(roster=_roster(conn), errors=errors))

    assert (result.digests, result.model_called, result.model_used) == (1, True, False)
    event = _rows(conn, "SELECT evidence FROM news_market_wallet_events WHERE kind = 'digest'")[0]
    assert event["evidence"]["model_used"] is False
    lines = [line["text"] for line in event["evidence"]["lines"]]
    assert "b999" not in " ".join(lines)
    assert any("买入合计 1 笔，已计价 $12,340.50" in line for line in lines)


def test_a_second_turn_inside_the_interval_writes_no_second_digest(conn) -> None:
    """Due-time is the whole schedule: the loop offers every turn and the writer answers "not yet"."""

    clock = _Clock()
    db, site = _Db(conn), _Site()
    seller = _member(SELL_WALLET, handle="0xVantaa")
    _seed(
        conn,
        [seller],
        [
            _fill(
                wallet=SELL_WALLET,
                token=FSD,
                kind="buy",
                amount_raw=8 * UNIT,
                usd="12340.50",
                event_at_ms=NOW - 3 * 3_600_000,
                received_at_ms=NOW - 3 * 3_600_000,
                tx_hash="0x" + "ab" * 32,
            )
        ],
    )
    program = _Digest()
    writer = _digest_writer(db, clock, program, site=site)
    errors: list[str] = []

    assert asyncio.run(writer.take_digest(roster=_roster(conn), errors=errors)).digests == 1
    clock.advance(3_600_000)
    assert asyncio.run(writer.take_digest(roster=_roster(conn), errors=errors)).digests == 0

    # Four more hours, and a movement inside them: the next window is due *and* has something to say.
    clock.advance(3 * 3_600_000 + 1_000)
    _seed_fills(
        conn,
        [
            _fill(
                wallet=SELL_WALLET,
                token=FSD,
                kind="buy",
                amount_raw=2 * UNIT,
                usd="3100",
                event_at_ms=NOW + 3_600_000,
                received_at_ms=NOW + 3_600_000,
                tx_hash="0x" + "cd" * 32,
            )
        ],
    )
    assert asyncio.run(writer.take_digest(roster=_roster(conn), errors=errors)).digests == 1
    assert len(_rows(conn, "SELECT item_id FROM news_market_wallet_events WHERE kind = 'digest'")) == 2


def test_the_digest_attempt_marker_is_durable_monotonic_and_readable_without_a_digest_row(conn) -> None:
    """The bound on the write-failure loop, against the real table it lives on.

    It has to survive a database whose tape has never saved state (the marker upserts the row), it may
    only advance (an out-of-order turn cannot reopen the interval), and `chain_tape_last_digest` has to
    return it with no digest rows at all -- which is the only case it exists for.
    """

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_mark_digest_attempt(now_ms=NOW)
    with repos.transaction():
        repos.news.chain_tape_mark_digest_attempt(now_ms=NOW - 3_600_000)

    state = repos.news.chain_tape_last_digest(since_ms=NOW - 86_400_000)

    assert state is not None
    assert (state.attempted_at_ms, state.window_to_ms) == (NOW, 0)
    assert _rows(conn, "SELECT item_id FROM news_market_wallet_events") == []


def test_a_quiet_window_writes_no_digest_at_all(conn) -> None:
    """#572 §5.3's 空窗跳过, against the real tables rather than a stub."""

    clock = _Clock()
    db = _Db(conn)
    _seed(conn, [_member(SELL_WALLET, handle="0xVantaa")], [])
    program = _Digest()
    errors: list[str] = []

    result = asyncio.run(_digest_writer(db, clock, program).take_digest(roster=_roster(conn), errors=errors))

    assert (result.digests, program.calls) == (0, 0)
    assert _rows(conn, "SELECT item_id FROM news_items") == []


def test_a_day_at_the_call_cap_still_writes_the_digest_from_the_template(conn) -> None:
    """The cap is on the endpoint, not on the summary: the facts were computed before a call was weighed."""

    clock = _Clock()
    db, site = _Db(conn), _Site()
    _seed(
        conn,
        [_member(SELL_WALLET, handle="0xVantaa")],
        [
            _fill(
                wallet=SELL_WALLET,
                token=FSD,
                kind="buy",
                amount_raw=8 * UNIT,
                usd="12340.50",
                event_at_ms=NOW - 3 * 3_600_000,
                received_at_ms=NOW - 3 * 3_600_000,
                tx_hash="0x" + "ab" * 32,
            )
        ],
    )
    program = _Digest(("b1",))
    writer = WalletDigestWriter(
        db=db,
        program=program,
        bags=site,
        interval_s=14_400,
        max_calls_per_day=1,
        clock=clock,
    )
    errors: list[str] = []

    assert asyncio.run(writer.take_digest(roster=_roster(conn), errors=errors)).model_called is True
    clock.advance(4 * 3_600_000 + 1_000)
    _seed_fills(
        conn,
        [
            _fill(
                wallet=SELL_WALLET,
                token=FSD,
                kind="buy",
                amount_raw=2 * UNIT,
                usd="3100",
                event_at_ms=NOW + 3_600_000,
                received_at_ms=NOW + 3_600_000,
                tx_hash="0x" + "cd" * 32,
            )
        ],
    )
    second = asyncio.run(writer.take_digest(roster=_roster(conn), errors=errors))

    # The second window is due and is written, and the model is never asked: one call is what the day
    # had left.
    assert (second.digests, second.model_called, program.calls) == (1, False, 1)


def test_a_digest_reads_back_through_the_wallets_console_read_model(conn) -> None:
    """The page reads the same program-rendered facts and model-selection audit."""

    clock = _Clock()
    db, site = _Db(conn), _Site()
    _seed(
        conn,
        [_member(SELL_WALLET, handle="0xVantaa")],
        [
            _fill(
                wallet=SELL_WALLET,
                token=FSD,
                kind="buy",
                amount_raw=8 * UNIT,
                usd="12340.50",
                event_at_ms=NOW - 3 * 3_600_000,
                received_at_ms=NOW - 3 * 3_600_000,
                tx_hash="0x" + "ab" * 32,
            )
        ],
    )
    program = _Digest(("b1",))
    asyncio.run(_digest_writer(db, clock, program, site=site).take_digest(roster=_roster(conn), errors=[]))

    repos = repositories_for_connection(conn)
    cards = repos.news.chain_tape_cards(from_ms=NOW - 86_400_000, to_ms=NOW + 60_000, limit=50)
    fills = repos.news.chain_tape_fill_totals(from_ms=NOW - 86_400_000)
    roster = repos.news.chain_tape_roster_rows()

    assert [card["kind"] for card in cards] == ["digest"]
    assert any("买入合计 1 笔，已计价 $12,340.50" in line for line in cards[0]["digest_lines"])
    assert any(SELL_WALLET in line and FSD in line and "买入 1 笔" in line for line in cards[0]["digest_lines"])
    assert any("观察前余额与历史连续性未知" in line for line in cards[0]["digest_lines"])
    assert cards[0]["digest_model_used"] is True
    assert [(row["kind"], row["fills"]) for row in fills] == [("buy", 1)]
    assert [row["handle"] for row in roster] == ["0xVantaa"]


def test_a_horizon_nothing_can_price_stays_due_briefly_and_is_then_recorded_unavailable(conn) -> None:
    """A miss is retried inside the grace and banked after it, because a late read is a different number.

    The grace is minutes rather than a day on purpose: a price taken three hours after the one-hour mark
    does not answer the one-hour question, and a row that is never banked keeps occupying the turn's
    receipt budget for as long as it stays unpriceable.
    """

    clock = _Clock()
    db, chain, site = _Db(conn), _Chain(), _Site()
    site.token_marks = {}
    prices = _Prices()
    seller = _member(SELL_WALLET, handle="0xVantaa")
    site.bags_by_handle["0xVantaa"] = (
        _Bag(token=FSD, symbol="FSD", amount=0.0, avg_price=0.0018, cost_usd=16_900.0, opened_at_ms=NOW - 7_200_000),
    )
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=FSD_HELD_RAW,
        usd=SALE_USD,
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [seller], [sell])
    errors: list[str] = []
    asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))
    asyncio.run(MarketNotificationLoop(db=db, sender=_Sender(), console_base_url=CONSOLE, clock=clock).advance())

    clock.advance(900_000 + 1_000)
    assert asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors)).outcomes == 0
    assert _rows(conn, "SELECT * FROM news_market_wallet_outcomes") == []

    # Past the grace: the one-hour horizon is banked. The four-hour one is not due at all yet, which is
    # the absence of a row rather than an `unavailable`.
    clock.advance(OUTCOME_GIVE_UP_MS)
    first = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))
    assert (first.outcomes, first.unavailable) == (0, 1)
    assert [row["horizon"] for row in _rows(conn, "SELECT horizon FROM news_market_wallet_outcomes")] == ["15m"]

    clock.advance(4 * 3_600_000)
    receipts = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert receipts.unavailable == 2
    recorded = _rows(conn, "SELECT horizon, price, source FROM news_market_wallet_outcomes ORDER BY horizon")
    assert [(row["horizon"], row["price"], row["source"]) for row in recorded] == [
        ("15m", None, "unavailable"),
        ("1h", None, "unavailable"),
        ("4h", None, "unavailable"),
    ]


@pytest.mark.parametrize(
    ("name", "price"),
    [
        ("zero", Decimal("0")),
        # The dust print the recorded DexScreener answer for FSD actually carries. A token whose every
        # pool reports no liquidity is priced off whichever of them the depth ranking lands on, so this
        # is not hypothetical.
        ("a_recorded_dust_pool", Decimal("2.94e-27")),
        # Positive, and still under half the column's last representable digit.
        ("just_under_the_columns_scale", Decimal("4e-19")),
    ],
)
def test_a_price_the_receipt_column_cannot_hold_is_no_price_at_all(conn, name, price) -> None:
    """B1. `price numeric(38,18)` rounds these to zero and its own `price > 0` then refuses the row.

    A refused INSERT is caught now rather than faulting the tape, but a row that keeps being refused is
    still due for ever, still occupies one of the horizon's slots every turn, and still logs a
    traceback each time. A figure this small is not a price -- it is a pool saying it holds nothing --
    so it is treated as none and banked `unavailable` after the grace, like any other unpriced row.
    """

    assert price < OUTCOME_PRICE_MIN, name
    clock = _Clock()
    db, chain, site = _Db(conn), _Chain(), _Site()
    site.token_marks = {}
    prices = _Prices({FSD: price})
    _card(conn, db, chain, site, prices, clock)

    errors: list[str] = []
    clock.advance(900_000 + 1_000)
    receipts = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert (receipts.outcomes, receipts.unavailable) == (0, 0)
    assert _rows(conn, "SELECT * FROM news_market_wallet_outcomes") == []
    assert errors == []

    clock.advance(OUTCOME_GIVE_UP_MS)
    banked = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert banked.unavailable == 1
    row = _rows(conn, "SELECT horizon, price, source FROM news_market_wallet_outcomes")[0]
    assert (row["horizon"], row["price"], row["source"]) == ("15m", None, "unavailable")


def test_the_smallest_price_the_column_can_hold_is_still_a_receipt(conn) -> None:
    """The guard is a column bound, not a floor on what a token may be worth: 1e-18 is written."""

    clock = _Clock()
    db, chain, site = _Db(conn), _Chain(), _Site()
    site.token_marks = {}
    prices = _Prices({FSD: OUTCOME_PRICE_MIN})
    _card(conn, db, chain, site, prices, clock)

    errors: list[str] = []
    clock.advance(900_000 + 1_000)
    receipts = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert (receipts.outcomes, receipts.unavailable) == (1, 0)
    row = _rows(conn, "SELECT price, source FROM news_market_wallet_outcomes")[0]
    assert Decimal(row["price"]) == OUTCOME_PRICE_MIN
    assert row["source"] == "dexscreener_base_token_v1"


def test_a_row_postgresql_refuses_faults_its_research_stage_and_leaves_the_fill_pending(conn) -> None:
    """B1. The rules half must not be able to fault the ingestion half, whatever a write does.

    The database port translates an admission refusal and an overrun; anything else -- a constraint the
    driver would not accept -- arrives raw. A single derived row PostgreSQL will not take would
    otherwise stop the chain tape on every restart for ever, because the fill that produced it is still
    there to produce it again.
    """

    clock = _Clock()
    db, chain, site, prices = _Refusing(conn), _Chain(), _Site(), _Prices()
    seller = _member(SELL_WALLET, handle="0xVantaa")
    site.bags_by_handle["0xVantaa"] = (
        _Bag(token=FSD, symbol="FSD", amount=0.0, avg_price=0.0018, cost_usd=16_900.0, opened_at_ms=NOW - 7_200_000),
    )
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=FSD_HELD_RAW,
        usd=SALE_USD,
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [seller], [sell])

    errors: list[str] = []
    with pytest.raises(CheckViolation):
        asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))
    assert errors == []
    assert _rows(conn, "SELECT item_id FROM news_items") == []


def test_a_sell_nothing_could_establish_a_denominator_for_produces_no_card(conn) -> None:
    """B2. An RPC that would not answer and a site that would not either is not a full exit.

    A rate-limited turn during a 20% sell used to become a `清仓` card on no evidence at all, because
    "the site says this wallet holds none of it" and "the site did not answer" were the same value.
    """

    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    chain.balances.clear()
    site.fail_with = RuntimeError("roster_rate_limited")
    seller = _member(SELL_WALLET, handle="0xVantaa")
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=2 * UNIT,
        usd="16000",
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [seller], [sell])

    errors: list[str] = []
    derived = asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))

    assert (derived.checks, derived.exits) == (0, 0)
    # No card, and no check row either: a check names the basis it was taken on, and there was none.
    assert _rows(conn, "SELECT * FROM news_market_wallet_checks") == []
    assert _rows(conn, "SELECT item_id FROM news_items") == []


def test_a_site_that_answers_with_no_position_is_still_a_full_exit(conn) -> None:
    """B2's other half. The third tier is kept -- it is evidence, and only the silence was not."""

    clock = _Clock()
    db, chain, site, prices = _Db(conn), _Chain(), _Site(), _Prices()
    chain.balances.clear()
    site.token_marks = {}
    # The site answers, and says this wallet holds nothing of this token: the sale was the position.
    site.bags_by_handle["0xVantaa"] = ()
    seller = _member(SELL_WALLET, handle="0xVantaa")
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=2 * UNIT,
        usd="46000",
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [seller], [sell])

    errors: list[str] = []
    derived = asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))

    assert (derived.checks, derived.exits) == (1, 1)
    check = _rows(conn, "SELECT basis, error, ratio_bps FROM news_market_wallet_checks")[0]
    assert check["basis"] == "site_reported"
    assert check["error"] == "rpc_state_unavailable:no_reported_bag"
    assert int(check["ratio_bps"]) == 10_000


def test_a_backlog_on_one_horizon_does_not_starve_the_other(conn) -> None:
    """B3. The turn's receipt budget is split per horizon, so a stuck 1h queue cannot hide the 4h one."""

    clock = _Clock()
    db, chain, site = _Db(conn), _Chain(), _Site()
    site.token_marks = {}
    prices = _Prices()
    _card(conn, db, chain, site, prices, clock)

    errors: list[str] = []
    # Both horizons are due and neither can be priced; each is banked, and the four-hour one is reached
    # in the same turn rather than queueing behind the one-hour one.
    clock.advance(4 * 3_600_000 + OUTCOME_GIVE_UP_MS)
    receipts = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert receipts.unavailable == 3
    banked = _rows(conn, "SELECT horizon FROM news_market_wallet_outcomes ORDER BY horizon")
    assert [row["horizon"] for row in banked] == ["15m", "1h", "4h"]


def _card(conn: Any, db: Any, chain: Any, site: Any, prices: Any, clock: _Clock) -> None:
    """One sent exit card, which is what a receipt is a receipt for."""

    seller = _member(SELL_WALLET, handle="0xVantaa")
    site.bags_by_handle["0xVantaa"] = (
        _Bag(token=FSD, symbol="FSD", amount=0.0, avg_price=0.0018, cost_usd=16_900.0, opened_at_ms=NOW - 7_200_000),
    )
    sell = _fill(
        wallet=SELL_WALLET,
        token=FSD,
        kind="sell",
        amount_raw=FSD_HELD_RAW,
        usd=SALE_USD,
        event_at_ms=NOW - 30_000,
        received_at_ms=NOW,
        tx_hash=SELL_TX,
    )
    _seed(conn, [seller], [sell])
    errors: list[str] = []
    asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))
    asyncio.run(MarketNotificationLoop(db=db, sender=_Sender(), console_base_url=CONSOLE, clock=clock).advance())


class _Refusing(_Db):
    """A database whose derivation write raises the way a refused constraint does."""

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        if name == "news_chain_tape_wallet_cards":
            raise CheckViolation("news_market_wallet_events_exit_check")
        return await super().tx(name, fn, timeout_seconds=timeout_seconds)


def _roster(conn: Any) -> Any:
    return repositories_for_connection(conn).news.chain_tape_current_roster()
