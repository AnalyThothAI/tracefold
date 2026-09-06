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
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.chain_tape.contracts import ClassifiedFill, RosterMember
from tracefold.news.chain_tape.derive import WalletCardDeriver
from tracefold.news.chain_tape.rules import WalletRules
from tracefold.news.market_notifications import MarketNotificationLoop
from tracefold.news.wallet_contracts import OUTCOME_GIVE_UP_MS, OUTCOME_PRICE_MIN

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "chain_tape"
# The byte-for-byte card a reader receives. Regenerate it deliberately with
# `TRACEFOLD_RECORD_WALLET_CARD=1`, and read the diff: this file is the wallet family's rendered
# contract, and a change to it is a change to what a reader sees.
EXIT_CARD = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "wallet_exit_card.json"

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


def _deriver(db: _Db, chain: _Chain, site: _Site, prices: _Prices, clock: _Clock) -> WalletCardDeriver:
    return WalletCardDeriver(db=db, chain=chain, site=site, prices=prices, rules=WalletRules(), clock=clock)


def _rows(conn: Any, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(statement, tuple(params)).fetchall()]


# ------------------------------------------------------------------------------------ the whole loop
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

    assert (receipts.outcomes, receipts.unavailable) == (1, 0)
    assert prices.calls == [FSD]
    outcome = _rows(conn, "SELECT * FROM news_market_wallet_outcomes")[0]
    assert outcome["delivery_key"] == delivery["delivery_key"]
    assert (outcome["horizon"], outcome["source"]) == ("1h", "dexscreener")
    assert Decimal(outcome["price"]) == Decimal("0.00019")

    # The four-hour horizon is not due yet, and "not due" is the absence of a row.
    assert len(_rows(conn, "SELECT * FROM news_market_wallet_outcomes")) == 1


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
    event = _rows(conn, "SELECT * FROM news_market_wallet_events")[0]
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

    assert turn.sent == 1
    body = sender.cards[0]["elements"][0]["content"]
    assert "3 个名单地址买入" in body
    assert "领头 trader1" in body
    # The card's span is the window the rules folded together, not the single instant its Item carries:
    # one derived row stands for three wallets' first buys ten minutes apart.
    assert sender.cards[0]["header"]["title"]["content"] == "链上钱包 · 拥挤 · 跟风偏晚 · MADETEST"
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

    clock.advance(3_600_000 + 1_000)
    assert asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors)).outcomes == 0
    assert _rows(conn, "SELECT * FROM news_market_wallet_outcomes") == []

    # Past the grace: the one-hour horizon is banked. The four-hour one is not due at all yet, which is
    # the absence of a row rather than an `unavailable`.
    clock.advance(OUTCOME_GIVE_UP_MS)
    first = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))
    assert (first.outcomes, first.unavailable) == (0, 1)
    assert [row["horizon"] for row in _rows(conn, "SELECT horizon FROM news_market_wallet_outcomes")] == ["1h"]

    clock.advance(4 * 3_600_000)
    receipts = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert receipts.unavailable == 1
    recorded = _rows(conn, "SELECT horizon, price, source FROM news_market_wallet_outcomes ORDER BY horizon")
    assert [(row["horizon"], row["price"], row["source"]) for row in recorded] == [
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
    clock.advance(3_600_000 + 1_000)
    receipts = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert (receipts.outcomes, receipts.unavailable) == (0, 0)
    assert _rows(conn, "SELECT * FROM news_market_wallet_outcomes") == []
    assert errors == []

    clock.advance(OUTCOME_GIVE_UP_MS)
    banked = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert banked.unavailable == 1
    row = _rows(conn, "SELECT horizon, price, source FROM news_market_wallet_outcomes")[0]
    assert (row["horizon"], row["price"], row["source"]) == ("1h", None, "unavailable")


def test_the_smallest_price_the_column_can_hold_is_still_a_receipt(conn) -> None:
    """The guard is a column bound, not a floor on what a token may be worth: 1e-18 is written."""

    clock = _Clock()
    db, chain, site = _Db(conn), _Chain(), _Site()
    site.token_marks = {}
    prices = _Prices({FSD: OUTCOME_PRICE_MIN})
    _card(conn, db, chain, site, prices, clock)

    errors: list[str] = []
    clock.advance(3_600_000 + 1_000)
    receipts = asyncio.run(_deriver(db, chain, site, prices, clock).take_outcomes(errors))

    assert (receipts.outcomes, receipts.unavailable) == (1, 0)
    row = _rows(conn, "SELECT price, source FROM news_market_wallet_outcomes")[0]
    assert Decimal(row["price"]) == OUTCOME_PRICE_MIN
    assert row["source"] == "dexscreener"


def test_a_row_postgresql_refuses_ends_the_pass_and_never_the_tape(conn) -> None:
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
    derived = asyncio.run(_deriver(db, chain, site, prices, clock).derive((sell,), roster=_roster(conn), errors=errors))

    assert (derived.checks, derived.exits) == (0, 0)
    assert errors == ["derive:news_chain_tape_wallet_cards:CheckViolation"]
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

    assert receipts.unavailable == 2
    banked = _rows(conn, "SELECT horizon FROM news_market_wallet_outcomes ORDER BY horizon")
    assert [row["horizon"] for row in banked] == ["1h", "4h"]


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


class CheckViolation(Exception):
    """Stands in for the driver's own constraint error, which the database port does not translate."""


class _Refusing(_Db):
    """A database whose derivation write raises the way a refused constraint does."""

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        if name == "news_chain_tape_wallet_cards":
            raise CheckViolation("news_market_wallet_events_exit_check")
        return await super().tx(name, fn, timeout_seconds=timeout_seconds)


def _roster(conn: Any) -> Any:
    return repositories_for_connection(conn).news.chain_tape_current_roster()
