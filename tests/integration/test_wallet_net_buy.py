"""Real PostgreSQL receipt → episode → existing notification ledger regressions (#641, #649 PR-3).

The rule under test is one window: five published roster addresses inside thirty minutes, each with
at least $1000 of net buying, sells deducted from the same fill set.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.chain_tape.contracts import (
    BLOCK_COMPLETE_TX_INDEX,
    STABLE_CASH_TOKEN,
    ClassifiedFill,
    RosterMember,
    TapeCursor,
)
from tracefold.news.chain_tape.detect import NetBuyDetector
from tracefold.news.chain_tape.rules import WalletRules, calculate_window
from tracefold.news.market_notifications import MarketNotificationLoop

pytestmark = pytest.mark.integration
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


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


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
        conn.execute("UPDATE news_market_wallet_tape_state SET detection_cutover_at_ms = %s", (NOW - 3_600_000,))
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


def test_buys_600_600_sell_300_are_net_900(conn: Any) -> None:
    """Old SQL returned 1200; observed F2P failure on a37cb7abf before replacement."""
    repos = seed(
        conn, [fill(1, usd="600", raw=600), fill(2, usd="600", raw=600), fill(3, kind="sell", usd="300", raw=300)]
    )
    facts = repos.news.wallet_window_fills(
        chain_id=CHAIN_ID, token=TOKEN, from_ms=NOW - 1800000, to_ms=NOW, block=1000, log=100
    )
    snapshot = calculate_window(
        fills=facts,
        members=repos.news.chain_tape_members(1),
        chain_id=CHAIN_ID,
        token=TOKEN,
        cutoff_at_ms=NOW,
        cutoff_block=1000,
        cutoff_log=100,
        coverage_from_ms=NOW - 3600000,
        coverage_gap_at_ms=None,
        roster_version=1,
        rules=WalletRules(),
        token_first_seen_at_ms=repos.news.wallet_token_first_seen_ms(chain_id=CHAIN_ID, token=TOKEN),
        member_episodes=repos.news.wallet_member_episodes(
            wallets=[movement.wallet for movement in facts], from_ms=NOW - 1_209_600_000, to_ms=NOW
        ),
    )
    assert snapshot.window.members[0].net_usd == Decimal("900")
    assert snapshot.window.qualified_n == 0


@pytest.mark.parametrize(
    "kind,usd,raw",
    [
        ("sell", "1800", 1200),
        ("sell", None, 1),
        ("transfer_out", None, 1),
    ],
)
def test_sold_or_unknown_wallet_cannot_make_the_quorum(conn: Any, kind: str, usd: str | None, raw: int) -> None:
    seed(
        conn,
        [fill(1, usd="4000"), fill(2, kind=kind, usd=usd, raw=raw), *[fill(2 + i, wallet=1 + i) for i in range(1, 5)]],
    )
    run(conn)
    assert events(conn) == []


def test_complete_receipt_never_emits_transient_last_buyer(conn: Any) -> None:
    receipt_buy = fill(5, wallet=5, tx=30, log=3)
    receipt_sell = replace(receipt_buy, kind="sell", log_index=7, usd=Decimal("1100"))
    seed(conn, [*[fill(i, wallet=i) for i in range(1, 5)], receipt_buy, receipt_sell])
    run(conn)
    assert events(conn) == []
    assert (
        conn.execute("SELECT count(*) AS n FROM news_market_wallet_fills WHERE derived_at_ms IS NULL").fetchone()["n"]
        == 0
    )


def test_five_any_tier_buyers_are_one_episode_one_intent_and_one_card_with_the_three_facts(conn: Any) -> None:
    """The whole rule, end to end: five roster addresses in thirty minutes on a token under an hour
    old, and the card carries the age, the cohort's net total and how often these addresses have done
    this before (#649 PR-3 §3, §5). None of the five holds a quality rank."""

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)], quality=False)
    result = run(conn)
    assert result.opened == 1
    event = events(conn)[0]
    assert event["initial_snapshot"]["window"]["qualified_n"] == 5
    assert event["latest_snapshot"]["window"]["qualified_n"] == 5
    assert event["initial_snapshot"]["token_first_seen_at_ms"] == NOW
    sender = Sender()
    loop = MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW, console_base_url="https://example.com")
    asyncio.run(loop.advance())
    assert len(sender.cards) == 1
    card = sender.cards[0]
    assert card.title() == "链上钱包 · 集中净买入 · XYZ"
    body = "\n".join(card.body_lines())
    assert "30 分钟 · 5 个合格地址 · 净买入 $6,000.00" in body
    assert "代币链上年龄 0 分钟（采集首见） · 新盘" in body
    assert "这 5 个地址近 14 天共参与 0 次集中净买入" in body
    assert "/news/wallets?episode=" in card.link.url
    run(conn)
    asyncio.run(loop.advance())
    assert len(events(conn)) == 1
    assert conn.execute("SELECT count(*) AS n FROM news_market_deliveries").fetchone()["n"] == 1


def test_four_addresses_are_not_a_quorum_and_nothing_shorter_can_rescue_them(conn: Any) -> None:
    """The boundary the whole rule rests on. There is no second window left for four to satisfy."""

    seed(conn, [fill(i, wallet=i) for i in range(1, 5)])
    run(conn)
    assert events(conn) == []
    reasons = {
        row["derived_reason"]
        for row in conn.execute("SELECT DISTINCT derived_reason FROM news_market_wallet_fills").fetchall()
    }
    assert reasons == {"conditions_not_met"}


def test_the_replayed_episode_shape_five_buyers_across_thirty_minutes_on_a_token_under_an_hour_old(
    conn: Any,
) -> None:
    """One episode of the twenty a fourteen-day replay of production-shaped fills produced.

    The shape, not the volume: a token the tape first saw fifty minutes ago, five roster addresses
    buying over the half hour that follows, each past $1000 net, and a sixth address whose $200 is
    real and simply below the floor. It is the fixture behind the card's three facts (#649 PR-3 §5).
    """

    launch = NOW - 3_000_000
    seed(conn, [replace(fill(20, wallet=9, usd="200", raw=200, at=launch), received_at_ms=launch)])
    run(conn)
    assert events(conn) == [], "one small buy on a new token is not an episode"

    for index, offset in enumerate([1_700_000, 1_200_000, 700_000, 200_000, 0], start=1):
        stamp = NOW - offset
        add_facts(conn, [replace(fill(index, wallet=index, at=stamp), received_at_ms=stamp)], stamp=stamp)
        run(conn, stamp=stamp)

    episode = events(conn)[0]
    snapshot = episode["initial_snapshot"]
    assert snapshot["window"]["qualified_n"] == 5 and snapshot["window"]["required_n"] == 5
    assert snapshot["token_first_seen_at_ms"] == launch
    assert snapshot["cutoff_at_ms"] - snapshot["token_first_seen_at_ms"] == 3_000_000
    sender = Sender()
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW).advance())
    body = "\n".join(sender.cards[0].body_lines())
    assert "30 分钟 · 5 个合格地址" in body
    assert "代币链上年龄 50 分钟（采集首见） · 新盘" in body


def test_a_second_round_carries_what_these_addresses_did_in_the_last_fourteen_days(conn: Any) -> None:
    """The card's third fact, counted from the stored episodes rather than from a separate ledger."""

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    sender = Sender()
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW).advance())
    assert "这 5 个地址近 14 天共参与 0 次集中净买入" in "\n".join(sender.cards[0].body_lines())

    other = "0x" + "bb" * 20
    stamp = NOW + 2_000_000
    add_facts(
        conn,
        [replace(fill(30 + i, wallet=i, at=stamp), token=other, received_at_ms=stamp) for i in range(1, 6)],
        stamp=stamp,
    )
    run(conn, stamp=stamp)
    second = next(row for row in events(conn) if row["token"] == other)
    assert [member["recent_episodes"] for member in second["initial_snapshot"]["window"]["members"]] == [1] * 5
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: stamp).advance())
    assert "这 5 个地址近 14 天共参与 5 次集中净买入" in "\n".join(sender.cards[-1].body_lines())


def test_mute_then_restore_does_not_adopt_existing_episode(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn, enabled=False)
    sender = Sender()
    loop = MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW)
    asyncio.run(loop.advance())
    assert not sender.cards
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_record_fills([fill(6, wallet=6)])
    run(conn, enabled=True)
    asyncio.run(loop.advance())
    assert not sender.cards
    assert len(events(conn)) == 1
    assert events(conn)[0]["notification_reason"] == "wallet_notifications_disabled"


def test_event_and_receipt_progress_roll_back_together(conn: Any) -> None:
    from psycopg.errors import DivisionByZero

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    db = Db(conn)
    db.rollback_commit = True
    with pytest.raises(DivisionByZero):
        asyncio.run(NetBuyDetector(db=db, clock=lambda: NOW).advance())
    assert not events(conn)
    assert (
        conn.execute("SELECT count(*) AS n FROM news_market_wallet_fills WHERE derived_at_ms IS NULL").fetchone()["n"]
        > 0
    )
    run(conn)
    assert len(events(conn)) == 1


def test_pending_card_invalidated_by_sale(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    sender = Sender()
    sender.available = False
    loop = MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW)
    asyncio.run(loop.advance())
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_record_fills([fill(6, wallet=1, kind="sell", usd="1200")])
    run(conn)
    sender.available = True
    asyncio.run(loop.advance())
    assert not sender.cards
    row = conn.execute("SELECT state,error FROM news_market_deliveries").fetchone()
    assert row == {"state": "failed", "error": "invalidated_before_send"}


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


def test_dropping_below_the_quorum_and_recovering_never_creates_a_followup(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    first = events(conn)[0]
    sender = Sender()
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW).advance())
    add_facts(conn, [fill(6, wallet=1, kind="sell", at=NOW + 1000)], stamp=NOW + 1000)
    run(conn, stamp=NOW + 1000)
    assert events(conn)[0]["latest_snapshot"]["window"]["qualified_n"] == 4
    add_facts(
        conn,
        [fill(7, wallet=1, at=NOW + 2000), fill(8, wallet=6, at=NOW + 2000)],
        stamp=NOW + 2000,
    )
    run(conn, stamp=NOW + 2000)
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW + 2000).advance())
    assert len(events(conn)) == len(sender.cards) == 1
    assert events(conn)[0]["initial_snapshot"] == first["initial_snapshot"]
    assert events(conn)[0]["latest_snapshot"]["window"]["qualified_n"] == 6
    assert events(conn)[0]["send_snapshot"]["window"]["qualified_n"] == 5


def test_chain_time_expiry_and_unchanged_snapshot_zero_writes(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    original = events(conn)[0]
    before = conn.execute("SELECT xmin::text AS version FROM news_market_wallet_events").fetchone()
    # Wall time alone is not completed chain coverage.
    assert run(conn, stamp=NOW + 1800001).updated == 0
    assert conn.execute("SELECT xmin::text AS version FROM news_market_wallet_events").fetchone() == before
    add_facts(conn, [], stamp=NOW + 1800000)
    assert run(conn, stamp=NOW + 1800000).updated == 1
    current = events(conn)[0]
    assert current["initial_snapshot"] == original["initial_snapshot"]
    assert current["latest_snapshot"]["window"]["qualified_n"] == 0
    assert current["change_reason"] == "window_expiry"
    assert current["ended_at_ms"] == NOW + 1800000
    before = conn.execute("SELECT xmin::text AS version FROM news_market_wallet_events").fetchone()
    add_facts(conn, [], stamp=NOW + 1800100)
    assert run(conn, stamp=NOW + 1800100).updated == 0
    assert conn.execute("SELECT xmin::text AS version FROM news_market_wallet_events").fetchone() == before


def test_effective_buy_extends_past_thirty_minutes_then_closes_and_new_receipt_reopens(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    first_id = events(conn)[0]["item_id"]
    for index, stamp in [(6, NOW + 1200000), (7, NOW + 2400000)]:
        add_facts(
            conn,
            [replace(fill(index, wallet=1, at=stamp, usd="1" if index == 6 else "1200", raw=1), received_at_ms=stamp)],
            stamp=stamp,
        )
        run(conn, stamp=stamp)
    assert events(conn)[0]["ended_at_ms"] is None
    assert events(conn)[0]["last_effective_buy_at_ms"] == NOW + 2400000
    close = NOW + 4200000
    add_facts(conn, [], stamp=close)
    run(conn, stamp=close)
    assert events(conn)[0]["ended_at_ms"] == close
    fresh = [replace(fill(10 + i, wallet=i, at=close + 1), received_at_ms=close + 1) for i in range(1, 6)]
    add_facts(conn, fresh, stamp=close + 1)
    run(conn, stamp=close + 1)
    rows = events(conn)
    assert len(rows) == 2 and rows[0]["item_id"] == first_id
    assert rows[1]["trigger_tx_hash"] == fresh[-1].tx_hash


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("stale", "stale_trigger"),
        ("future", "future_chain_timestamp"),
        ("cutover", "before_cutover"),
    ],
)
def test_history_future_and_cutover_are_durable_non_triggers(conn: Any, mode: str, reason: str) -> None:
    facts = [fill(i, wallet=i, at=NOW + 1000 if mode == "future" else NOW) for i in range(1, 6)]
    seed(conn, facts)
    if mode == "cutover":
        conn.execute("UPDATE news_market_wallet_tape_state SET detection_cutover_at_ms=%s", (NOW,))
    run(conn, stamp=NOW + 60001 if mode == "stale" else NOW)
    assert events(conn) == []
    last = conn.execute(
        "SELECT derived_reason FROM news_market_wallet_fills ORDER BY block_number DESC LIMIT 1"
    ).fetchone()
    assert last["derived_reason"] == reason


def test_stale_first_attempt_ends_original_intent_and_does_not_retry_same_episode(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    sender = Sender()
    sender.available = False
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW).advance())
    sender.available = True
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW + 60001).advance())
    assert sender.cards == []
    assert conn.execute("SELECT state,error FROM news_market_deliveries").fetchone() == {
        "state": "failed",
        "error": "stale_before_send",
    }
    add_facts(conn, [replace(fill(6, wallet=6, at=NOW + 60002), received_at_ms=NOW + 60002)], stamp=NOW + 60002)
    run(conn, stamp=NOW + 60002)
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW + 60002).advance())
    assert sender.cards == []
    assert conn.execute("SELECT count(*) AS n FROM news_market_deliveries").fetchone()["n"] == 1


@pytest.mark.parametrize("batch_size", [1, 2, 20])
def test_committed_receipt_batching_preserves_first_trigger_identity(conn: Any, batch_size: int) -> None:
    seed(conn, [])
    facts = [fill(i, wallet=i) for i in range(1, 6)]
    for start in range(0, len(facts), batch_size):
        add_facts(conn, facts[start : start + batch_size], stamp=NOW)
        run(conn)
    assert len(events(conn)) == 1
    assert events(conn)[0]["trigger_tx_hash"] == facts[4].tx_hash
    assert events(conn)[0]["initial_snapshot"]["window"]["qualified_n"] == 5


def test_roster_and_monitoring_evidence_are_frozen_and_removed_wallet_keeps_being_collected(conn: Any) -> None:
    repos = seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    first = events(conn)[0]["initial_snapshot"]
    stamp = NOW + 1000
    with repos.transaction():
        roster = repos.news.chain_tape_store_roster([member(i) for i in range(2, 7)], now_ms=stamp)
        conn.execute(
            "UPDATE news_market_wallet_tape_state SET roster_version=%s,scanned_at_ms=%s",
            (roster.roster_version, stamp),
        )
    assert member(1).wallet in repos.news.chain_tape_collection_wallets(through_at_ms=stamp + 1799999)
    assert member(1).wallet not in repos.news.chain_tape_collection_wallets(through_at_ms=stamp + 1800000)
    run(conn, stamp=stamp)
    assert events(conn)[0]["initial_snapshot"] == first
    assert events(conn)[0]["latest_snapshot"]["window"]["qualified_n"] == 4
    assert events(conn)[0]["change_reason"] == "roster_changed"
    add_facts(
        conn, [replace(fill(6, wallet=1, kind="sell", at=stamp), roster_version=roster.roster_version)], stamp=stamp
    )
    run(conn, stamp=stamp)
    excluded = events(conn)[0]["latest_snapshot"]["window"]["members"][0]
    assert Decimal(excluded["sell_usd"]) == Decimal("1200") and not excluded["qualified"]


def test_a_newly_monitored_address_cannot_complete_the_quorum_but_an_unranked_one_can(conn: Any) -> None:
    """Warm-up still gates an address; the provider's ranks no longer do (#649 PR-3 §1)."""

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    conn.execute(
        "UPDATE news_market_wallet_roster SET monitoring_from_ms=%s WHERE wallet=%s", (NOW - 1799999, member(1).wallet)
    )
    run(conn)
    assert events(conn) == []
    excluded = conn.execute("SELECT derived_reason FROM news_market_wallet_fills WHERE wallet=%s", (member(1).wallet,))
    assert excluded.fetchone()["derived_reason"] == "conditions_not_met"

    # The same five addresses, with the warm-up satisfied and one of them still unranked, do trigger.
    conn.execute("UPDATE news_market_wallet_roster SET monitoring_from_ms=%s", (NOW - 3_600_000,))
    stamp = NOW + 1000
    add_facts(conn, [replace(fill(6, wallet=1, at=stamp), received_at_ms=stamp)], stamp=stamp)
    run(conn, stamp=stamp)
    assert len(events(conn)) == 1
    assert events(conn)[0]["initial_snapshot"]["window"]["qualified_n"] == 5


def test_muted_episode_prices_are_sampled_once_and_missing_baseline_stays_unknown(conn: Any) -> None:
    from tracefold.news.chain_tape.prices import WalletPriceSampler

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn, enabled=False)

    class Prices:
        calls = 0

        async def token_price(self, address):
            self.calls += 1
            return Decimal("0.000000000000000000000000000123")

        async def aclose(self):
            pass

    prices = Prices()
    sampler = WalletPriceSampler(db=Db(conn), prices=prices, clock=lambda: NOW + 900000)
    assert asyncio.run(sampler.advance()) == 1
    assert asyncio.run(sampler.advance()) == 0
    assert prices.calls == 1
    receipt = repositories_for_connection(conn).news.wallet_outcomes(events(conn)[0]["item_id"])[0]
    assert receipt["status"] == "missing_reference" and receipt["change_percent"] is None
    assert Decimal(receipt["price"]) == Decimal("1.23e-28")
    assert receipt["target_at_ms"] == receipt["at_ms"] == NOW + 900000


class RecordedPrices:
    """A price provider with a scripted answer per token, counting what it was actually asked."""

    def __init__(self, answers: dict[str, Any] | None = None, default: Any = Decimal("2")) -> None:
        self.answers = answers or {}
        self.default = default
        self.asked: list[str] = []

    async def token_price(self, address: str) -> Any:
        self.asked.append(address)
        answer = self.answers.get(address, self.default)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def aclose(self) -> None:
        pass


def sampler(conn: Any, prices: Any, *, at_ms: int | None = None, clock: Callable[[], int] | None = None) -> Any:
    from tracefold.news.chain_tape.prices import WalletPriceSampler

    return WalletPriceSampler(db=Db(conn), prices=prices, clock=clock or (lambda: int(at_ms or 0)))


def test_reference_price_is_written_once_by_the_sampler_after_a_failure_and_never_rewritten(conn: Any) -> None:
    """t0 is a sampler stage, not a detector one, and it retries until the first price is really there."""

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    episode = events(conn)[0]
    assert episode["reference_price"] is None, "the detector must not write a baseline (#649 §3 row 7)"

    broken = RecordedPrices(default=OSError("provider unavailable"))
    assert asyncio.run(sampler(conn, broken, at_ms=NOW + 1000).advance()) == 0
    assert events(conn)[0]["reference_price"] is None and broken.asked == [TOKEN]

    recovered = RecordedPrices({TOKEN: Decimal("3.5")})
    assert asyncio.run(sampler(conn, recovered, at_ms=NOW + 30_000).advance()) == 1
    stored = events(conn)[0]
    assert Decimal(stored["reference_price"]) == Decimal("3.5")
    assert stored["reference_at_ms"] == NOW + 30_000
    assert stored["reference_source"] == "dexscreener_robinhood_chain_base_token"
    # The delay from the trigger is the two stored stamps, not a second recorded number.
    assert stored["reference_at_ms"] - stored["event_at_ms"] == 30_000

    # A later turn, and a restarted process, both leave the first baseline exactly as it is.
    moved = RecordedPrices({TOKEN: Decimal("99")})
    assert asyncio.run(sampler(conn, moved, at_ms=NOW + 60_000).advance()) == 0
    assert moved.asked == [], "an episode that already has a baseline is not due for one"
    assert Decimal(events(conn)[0]["reference_price"]) == Decimal("3.5")


def test_episode_older_than_the_sampling_budget_is_never_given_a_backfilled_baseline(conn: Any) -> None:
    from tracefold.news.wallet_contracts import REFERENCE_MAX_DELAY_MS

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    prices = RecordedPrices({TOKEN: Decimal("7")})
    assert asyncio.run(sampler(conn, prices, at_ms=NOW + REFERENCE_MAX_DELAY_MS + 1).advance()) == 0
    assert prices.asked == []
    assert events(conn)[0]["reference_price"] is None
    # And the horizons it goes on to record say so rather than reporting a change of zero.
    late = RecordedPrices({TOKEN: Decimal("7")})
    assert asyncio.run(sampler(conn, late, at_ms=NOW + 900_000).advance()) == 1
    receipt = repositories_for_connection(conn).news.wallet_outcomes(events(conn)[0]["item_id"])[0]
    assert receipt["status"] == "missing_reference" and receipt["change_percent"] is None


def test_a_call_that_overruns_the_budget_leaves_no_baseline_and_the_next_turn_is_too_late(conn: Any) -> None:
    from tracefold.news.wallet_contracts import REFERENCE_MAX_DELAY_MS

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    clock = [NOW + 1000]

    class SlowPrices(RecordedPrices):
        async def token_price(self, address: str) -> Any:
            clock[0] += REFERENCE_MAX_DELAY_MS
            return await super().token_price(address)

    slow = SlowPrices({TOKEN: Decimal("4")})
    assert asyncio.run(sampler(conn, slow, clock=lambda: clock[0]).advance()) == 0
    assert slow.asked == [TOKEN] and events(conn)[0]["reference_price"] is None


def test_unpriced_tokens_do_not_starve_the_other_fresh_episodes_of_a_baseline(conn: Any) -> None:
    """Two unpriceable tokens fill the per-turn reference budget; rotation reaches the rest anyway."""

    from tracefold.news.wallet_contracts import REFERENCE_MAX_DELAY_MS

    tokens = ["0x" + f"{i:040x}" for i in range(1, 5)]
    seed(conn, [])
    for index, token in enumerate(tokens):
        add_facts(conn, [replace(fill(index * 5 + i, wallet=i), token=token) for i in range(1, 6)], stamp=NOW)
        run(conn, enabled=False)
    assert len(events(conn)) == 4
    repos = repositories_for_connection(conn)
    queued = repos.news.chain_tape_due_references(now_ms=NOW + 1000, max_delay_ms=REFERENCE_MAX_DELAY_MS, limit=2)
    dead = {row["token"] for row in queued}
    assert len(dead) == 2, "the first turn's whole budget goes to the two tokens that cannot be priced"
    prices = RecordedPrices({token: None for token in dead})
    live = sampler(conn, prices, at_ms=NOW + 1000)
    written = [asyncio.run(live.advance()) for _ in range(3)]
    assert written == [0, 2, 0], f"two references a turn, rotating past the dead tokens: {written}"
    priced = {row["token"]: row["reference_price"] is None for row in events(conn)}
    assert priced == {token: token in dead for token in tokens}
    assert all(prices.asked.count(token) >= 2 for token in dead), "the dead tokens were retried, not abandoned"


def test_a_baseline_taken_before_the_target_makes_the_horizon_comparable(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    assert asyncio.run(sampler(conn, RecordedPrices({TOKEN: Decimal("2")}), at_ms=NOW + 1000).advance()) == 1
    assert asyncio.run(sampler(conn, RecordedPrices({TOKEN: Decimal("3")}), at_ms=NOW + 900_000).advance()) == 1
    receipt = repositories_for_connection(conn).news.wallet_outcomes(events(conn)[0]["item_id"])[0]
    assert receipt["status"] == "comparable"
    assert Decimal(receipt["reference_price"]) == Decimal("2")
    assert receipt["reference_at_ms"] == NOW + 1000 < receipt["target_at_ms"]
    assert Decimal(receipt["change_percent"]) == Decimal("50")


def test_quote_that_finishes_late_is_not_backdated_or_used_for_target_return(conn: Any) -> None:
    from tracefold.news.chain_tape.prices import WalletPriceSampler
    from tracefold.news.wallet_contracts import OUTCOME_MAX_DELAY_MS

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    clock = [NOW + 900000]

    class SlowPrices:
        async def token_price(self, address):
            clock[0] += OUTCOME_MAX_DELAY_MS + 1
            return Decimal("2")

        async def aclose(self):
            pass

    sampler = WalletPriceSampler(db=Db(conn), prices=SlowPrices(), clock=lambda: clock[0])
    assert asyncio.run(sampler.advance()) == 1
    receipt = repositories_for_connection(conn).news.wallet_outcomes(events(conn)[0]["item_id"])[0]
    assert receipt["price"] is None and receipt["change_percent"] is None
    assert receipt["status"] == "late"
    assert receipt["at_ms"] > receipt["target_at_ms"] + OUTCOME_MAX_DELAY_MS


def test_price_failure_keeps_horizon_pending_without_blocking_next_receipt(conn: Any) -> None:
    from tracefold.news.chain_tape.prices import WalletPriceSampler

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)

    class BrokenPrices:
        async def token_price(self, address):
            raise OSError("provider unavailable")

        async def aclose(self):
            pass

    assert (
        asyncio.run(WalletPriceSampler(db=Db(conn), prices=BrokenPrices(), clock=lambda: NOW + 900000).advance()) == 0
    )
    assert conn.execute("SELECT count(*) AS n FROM news_market_wallet_outcomes").fetchone()["n"] == 0
    add_facts(conn, [replace(fill(6, wallet=6, at=NOW + 1000), received_at_ms=NOW + 1000)], stamp=NOW + 1000)
    run(conn, stamp=NOW + 1000)
    assert events(conn)[0]["latest_snapshot"]["window"]["qualified_n"] == 6


def test_crash_after_external_send_before_receipt_is_unknown_after_restart(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)

    class InterruptedDb(Db):
        async def tx(self, name, fn, **kwargs):
            if name == "news_market_notify_settle":
                raise RuntimeError("process stopped before settlement")
            return await super().tx(name, fn, **kwargs)

    sender = Sender()
    loop = MarketNotificationLoop(db=InterruptedDb(conn), sender=sender, clock=lambda: NOW)
    with pytest.raises(RuntimeError, match="process stopped"):
        asyncio.run(loop.advance())
    assert len(sender.cards) == 1
    restarted = MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW + 1000)
    asyncio.run(restarted.start())
    asyncio.run(restarted.advance())
    assert len(sender.cards) == 1
    assert conn.execute("SELECT state,attempts FROM news_market_deliveries").fetchone() == {
        "state": "unknown",
        "attempts": 1,
    }


def test_gap_discovered_after_intent_before_detector_cannot_send_old_affirmative_snapshot(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    sender = Sender()
    sender.available = False
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW).advance())
    # The collector commits the gap before the independent detector next gets its turn.
    conn.execute("UPDATE news_market_wallet_tape_state SET gap_at_ms=%s", (NOW + 1,))
    sender.available = True
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW + 2).advance())
    assert sender.cards == []
    assert conn.execute("SELECT state,error,attempts FROM news_market_deliveries").fetchone() == {
        "state": "failed",
        "error": "invalidated_before_send",
        "attempts": 0,
    }


def test_unavailable_prices_rotate_past_oldest_episodes_and_each_horizon_gets_budget(conn: Any) -> None:
    from collections import Counter

    from tracefold.news.chain_tape.prices import WalletPriceSampler

    tokens = ["0x" + f"{i:040x}" for i in range(1, 7)]
    seed(conn, [])
    for index, token in enumerate(tokens):
        add_facts(conn, [replace(fill(index * 5 + i, wallet=i), token=token) for i in range(1, 6)], stamp=NOW)
        run(conn, enabled=False)
    rows = events(conn)
    assert len(rows) == 6
    repos = repositories_for_connection(conn)
    due = repos.news.chain_tape_due_outcomes(now_ms=NOW + 14400000, limit=6)
    assert Counter(row["horizon"] for row in due) == {"15m": 2, "1h": 2, "4h": 2}
    unavailable = {row["token"] for row in repos.news.chain_tape_due_outcomes(now_ms=NOW + 900000, limit=6)}

    class Prices:
        async def token_price(self, address):
            return None if address in unavailable else Decimal("2")

        async def aclose(self):
            pass

    sampler = WalletPriceSampler(db=Db(conn), prices=Prices(), clock=lambda: NOW + 900000)
    assert [asyncio.run(sampler.advance()) for _ in range(3)] == [0, 2, 2]
    assert conn.execute("SELECT count(*) AS n FROM news_market_wallet_outcomes").fetchone()["n"] == 4


def test_explicit_not_sent_retry_preserves_frozen_snapshot_and_both_channel_serializers(conn: Any) -> None:
    from tests.test_telegram_push import _sent_text
    from tracefold.news.feishu_card import feishu_card

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)

    class Refused(RuntimeError):
        code = "rate_limited"
        commit_phase = "not_sent"
        retryable = True

    class RetrySender(Sender):
        async def send_prepared_card(self, card, **kwargs):
            self.cards.append(card)
            if len(self.cards) == 1:
                raise Refused()
            return {"message_id": len(self.cards)}

    sender = RetrySender()
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW).advance())
    original = events(conn)[0]["send_snapshot"]
    add_facts(conn, [fill(6, wallet=6, at=NOW + 1000)], stamp=NOW + 1000)
    run(conn, stamp=NOW + 1000)
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW + 6000).advance())
    assert len(sender.cards) == 2
    assert events(conn)[0]["latest_snapshot"]["window"]["qualified_n"] == 6
    assert events(conn)[0]["send_snapshot"] == original
    assert feishu_card(sender.cards[0]) == feishu_card(sender.cards[1])
    assert _sent_text(sender.cards[0]) == _sent_text(sender.cards[1])
    assert conn.execute("SELECT count(*) AS n FROM news_market_deliveries").fetchone()["n"] == 1


def test_cutover_rosters_without_monitoring_support_do_not_expand_the_collection_pool(conn: Any) -> None:
    repos = seed(conn, [])
    # The migration leaves predecessor versions with no provable monitoring support.
    conn.execute("UPDATE news_market_wallet_roster SET monitoring_from_ms=NULL")
    with repos.transaction():
        repos.news.chain_tape_store_roster([member(20)], now_ms=NOW)
    assert repos.news.chain_tape_collection_wallets(through_at_ms=NOW) == (member(20).wallet,)
