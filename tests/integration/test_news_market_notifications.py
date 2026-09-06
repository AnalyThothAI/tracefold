"""The market notification loop against real PostgreSQL (#553 PR-2).

The rules are proved next door, without a database. What is proved here is everything that only
PostgreSQL can answer: that a notification failure never reaches an already-committed fact, that a
transaction which commits late is not skipped, that a confirmed card is never executed twice however
the process dies, that a frozen snapshot stays frozen, and that a send whose result could not be read
is `unknown` for ever rather than being retried into a duplicate.

The sender here is a stub, deliberately: what a *failed send* proved is decided inside the adapter and
is tested at that boundary in `tests/test_news_market_send_entry.py`. What is tested here is what the
loop durably does with each answer.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news import card_format as fmt
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.liquidations import parse_liquidation
from tracefold.news.market_notifications import (
    OI_QUIET_RESET_MS,
    REASON_ROUND_CLOSED,
    REASON_SENDER_UNAVAILABLE,
    REASON_SMART_MONEY_ROUND,
    REASON_UNSTRUCTURED,
    SEND_ATTEMPTS_MAX,
    SEND_RETRY_BACKOFF_MS,
    MarketNotificationLoop,
    group_identity,
)
from tracefold.news.market_review.instruments import Instrument
from tracefold.news.market_review.pricing import QUOTE_FRESH_MAX_AGE_MS, QUOTE_READ_TIMEOUT_SECONDS, Quote
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.oi_signals import measurement_definition, oi_source_contract
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame
from tracefold.news.pipeline.delivery import read_display_quotes, read_pushed_news
from tracefold.news.program.runtime import PROGRAM_VERSION as SEMANTIC_PROGRAM_VERSION
from tracefold.news.smart_money import parse_smart_money
from tracefold.news.source_contracts import MARKET_PROVIDER

pytestmark = pytest.mark.integration

NOW = 1_900_000_000_000


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


class _Db:
    """The News database port over one real connection, in the two shapes the loop uses.

    `tx` opens a real transaction, so a callable that raises leaves PostgreSQL exactly as it was --
    which is the mechanism half of "a notification failure rolls back no facts".

    Three injection points, and they prove different things. `fail_on` raises *before* the callable,
    which is a turn that never started. `fail_after` raises after it returned but before the commit,
    which is the one that matters: it is the only way to show that the marker, the track and the
    intent are one transaction rather than three that happened to succeed. `hold_on` keeps the
    transaction open so another connection meets it under a real lock.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.fail_on: set[str] = set()
        self.fail_after: set[str] = set()
        self.hold_on: str | None = None
        self.holding = threading.Event()
        self.release = threading.Event()
        self.names: list[str] = []
        # The quote plane's three failure shapes: a read that outlives its budget, and a port that
        # raises before the read happens at all.
        self.slow_reads: set[str] = set()
        self.slow_seconds = 0.0
        self.quote_failure: BaseException | None = None
        # A port that honours no budget of its own, which is what the loop's own deadline is for.
        self.quote_port_seconds = 0.0
        # The News plane's own two, for the second display read the OI card carries (#582 §3.3).
        self.news_failure: BaseException | None = None
        self.news_port_seconds = 0.0
        # Runs once, inside the window this PR opens: after the due card's transaction has committed
        # and released its row lock, and before the claim's compare-and-set.
        self.before_quotes: Callable[[], None] | None = None

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)

        async def run() -> Any:
            if name in self.slow_reads:
                await asyncio.sleep(self.slow_seconds)
            return fn(repositories_for_connection(self.connection))

        # The deadline the caller asked for is really applied, so a test that overruns it overruns
        # the same budget production overruns rather than a stand-in for one.
        return await asyncio.wait_for(run(), timeout=timeout_seconds)

    async def quotes_for_symbols(self, symbols: Any, *, now_ms: int) -> list[dict[str, Any]]:
        """`MarketNotificationDatabasePort.quotes_for_symbols`, composed as Workers composes it.

        `read_display_quotes` is the News first card's own session and 1.5 s budget, so what this
        suite proves about the market card's quote is proved about the code the News card is quoted
        with rather than about a second implementation of it.
        """

        if self.before_quotes is not None:
            self.before_quotes, run = None, self.before_quotes
            run()
        if self.quote_failure is not None:
            raise self.quote_failure
        if self.quote_port_seconds:
            await asyncio.sleep(self.quote_port_seconds)
        return await read_display_quotes(self, symbols, now_ms=now_ms, name="news_market_quotes")

    async def pushed_news_for_symbol(self, symbol: str, *, now_ms: int) -> dict[str, Any]:
        """`MarketNotificationDatabasePort.pushed_news_for_symbol`, composed as Workers composes it.

        One named read on the News lane, running the storage module's own statements, so what this
        suite proves about the OI card's news line is proved about the SQL production executes.
        """

        if self.news_failure is not None:
            raise self.news_failure
        if self.news_port_seconds:
            await asyncio.sleep(self.news_port_seconds)
        return await read_pushed_news(self, symbol, now_ms=now_ms, name="news_market_news")

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        repos = repositories_for_connection(self.connection)
        with repos.transaction():
            if name in self.fail_on:
                raise RuntimeError(f"injected_failure:{name}")
            result = fn(repos)
            if name in self.fail_after:
                raise RuntimeError(f"injected_failure_after:{name}")
            if self.hold_on == name:
                self.holding.set()
                assert self.release.wait(10), "the holding transaction was never released"
            return result

    def turns(self, name: str) -> int:
        return self.names.count(name)


class _Sender:
    """A prepared-card sender that answers whatever the test needs it to answer."""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.cards: list[dict[str, Any]] = []
        self.raise_with: BaseException | None = None

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, value: bool) -> None:
        self._available = value

    async def send_prepared_card(
        self,
        card: Any,
        *,
        channel_payload: Mapping[str, Any],
        operation: str = "",
    ) -> dict[str, Any]:
        del card
        self.cards.append(dict(channel_payload))
        if self.raise_with is not None:
            raise self.raise_with
        return {"provider": "test", "message_id": len(self.cards)}


class _Refused(RuntimeError):
    def __init__(self, code: str, *, commit_phase: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.commit_phase = commit_phase
        self.retryable = retryable


def _loop(db: _Db, sender: _Sender, *, clock: Callable[[], int]) -> MarketNotificationLoop:
    return MarketNotificationLoop(db=db, sender=sender, clock=clock)


class _Clock:
    def __init__(self, at_ms: int = NOW) -> None:
        self.at_ms = at_ms

    def __call__(self) -> int:
        return self.at_ms

    def advance(self, ms: int) -> None:
        self.at_ms += ms


def _oi_item(conn: Any, item_id: str, *, at_ms: int, change_bps: int, ingest_mode: str = "live") -> None:
    """One admitted OI observation: the Item and its typed fact, exactly as admission writes them."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        _write_oi(repos.news, item_id, at_ms=at_ms, change_bps=change_bps, ingest_mode=ingest_mode)


def _write_oi(news: Any, item_id: str, *, at_ms: int, change_bps: int, ingest_mode: str = "live") -> None:
    """The Item and its typed fact, without owning the transaction they go in."""

    news.upsert_item(
        item_id=item_id,
        source_id="opennews",
        source_item_key=item_id,
        title=f"WIF OI Rise {change_bps / 100}%, OI Value 11.03M, Whale Long Profit 88.40%, Whale/OI Ratio 143.90%",
        raw_first_line=item_id,
        description="",
        canonical_url=None,
        reporting_origin="opennews",
        published_at_ms=at_ms,
        observed_at_ms=at_ms,
        provider_metadata_json="{}",
        strategy_ids_json="[]",
        ingest_mode=ingest_mode,
        trace_id="trace",
        now_ms=at_ms,
        market_kind="oi",
        market_source_strategy_id="1019",
        market_parse_status="parsed",
        market_parse_error=None,
        provider_params_json=json.dumps({"rule": "oi_rise"}),
    )
    source = oi_source_contract({"strategies": [{"id": "1019"}]})
    assert source is not None
    news.insert_oi_signal(
        event_id=f"event-{item_id}",
        metric_version="oi_signal_v1",
        symbol="WIF",
        raw_instrument="WIF",
        direction="rise",
        oi_change_bps=change_bps,
        oi_value_usd=11_030_000,
        whale_long_profit_bps=8_840,
        whale_oi_ratio_bps=14_390,
        observed_at_ms=at_ms,
        received_at_ms=at_ms,
        now_ms=at_ms,
        provider=MARKET_PROVIDER,
        source_strategy_id=source.strategy_id,
        source_contract_version=source.contract_version,
        measurement_window_ms=source.measurement_window_ms,
        measurement_definition=measurement_definition(source),
        source_item_id=item_id,
        source_venue="binance",
    )


def _rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _deliveries(conn: Any) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM news_market_deliveries ORDER BY created_at_ms, delivery_key")


def _notify_state(conn: Any, item_id: str) -> str | None:
    rows = _rows(conn, "SELECT market_notify_state FROM news_items WHERE item_id = %s", (item_id,))
    return None if not rows else rows[0]["market_notify_state"]


# --- facts are never rolled back by a notification --------------------------------------------


def test_a_notification_failure_leaves_the_committed_fact_and_the_backlog_untouched(conn: Any) -> None:
    """The coupling the whole design exists to break: rules run in their own transaction.

    A rule that raises must not be able to delete an observation the provider made and this process
    stored. It also must not be able to lose it: the Item stays `pending`, so the next turn tries
    again rather than the record silently never being alerted.
    """

    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.fail_on = {"news_market_notify_group"}
    loop = _loop(db, _Sender(), clock=_Clock())

    with pytest.raises(RuntimeError, match="injected_failure"):
        asyncio.run(loop.advance())

    assert _rows(conn, "SELECT item_id FROM news_items WHERE item_id = 'oi-1'")
    assert _rows(conn, "SELECT source_item_id FROM news_oi_signals WHERE source_item_id = 'oi-1'")
    assert _notify_state(conn, "oi-1") == "pending"
    assert _deliveries(conn) == []

    # And the next turn, with the failure removed, picks it up: nothing was skipped.
    db.fail_on = set()
    sender = _Sender()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    assert _notify_state(conn, "oi-1") == "processed"
    assert len(sender.cards) == 1


def test_the_group_turn_commits_its_marker_track_and_intent_together_or_not_at_all(conn: Any) -> None:
    """§4.1.3. Failing *after* the turn succeeded is the only way to prove it is one transaction.

    A failure before the callable proves nothing -- nothing ran. This one lets the marker, the track
    and the intent all be written and then rolls the transaction back, so a design that used three
    transactions would leave a track behind with no marker and no card.
    """

    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.fail_after = {"news_market_notify_group"}

    with pytest.raises(RuntimeError, match="injected_failure_after"):
        asyncio.run(_loop(db, _Sender(), clock=_Clock()).advance())

    assert _notify_state(conn, "oi-1") == "pending"
    assert _rows(conn, "SELECT group_key FROM news_market_tracks") == []
    assert _deliveries(conn) == []


def test_one_group_is_one_transaction_per_turn(conn: Any) -> None:
    """Two groups in one intake are two turns; one group reported twice is still one."""

    _oi_item(conn, "oi-a", at_ms=NOW - 60_000, change_bps=600)
    _oi_item(conn, "oi-b", at_ms=NOW - 50_000, change_bps=620)
    _liquidation_item(conn, "liq-a", at_ms=NOW - 40_000, side="long")
    db = _Db(conn)
    asyncio.run(_loop(db, _Sender(), clock=_Clock()).advance())

    assert db.turns("news_market_notify_group") == 2
    assert db.turns("news_market_notify_backlog") == 1


def test_an_observation_committed_late_is_not_skipped_by_the_take_query(conn: Any) -> None:
    """A marker, not a high-water mark: a stamp cursor would have passed this row for ever.

    The late writer's transaction is genuinely open across the first turn -- on its own connection,
    invisible, holding a stamp an hour older than anything the loop is about to process -- and commits
    only afterwards. A cursor over `observed_at_ms` or `created_at_ms` would have advanced past that
    stamp during the first turn and would never look below it again.
    """

    writer = connect_postgres_test(read_only=False)
    try:
        sender = _Sender()
        db = _Db(conn)
        _oi_item(conn, "oi-new", at_ms=NOW - 10_000, change_bps=600)

        started = threading.Event()
        commit = threading.Event()

        def late_writer() -> None:
            repos = repositories_for_connection(writer)
            with repos.transaction():
                _write_oi(repos.news, "oi-late", at_ms=NOW - 3_600_000, change_bps=610)
                started.set()
                assert commit.wait(10)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending_write = pool.submit(late_writer)
            assert started.wait(10)

            # Turn one cannot see the uncommitted row, and processes the newer one.
            asyncio.run(_loop(db, sender, clock=_Clock()).advance())
            assert _notify_state(conn, "oi-new") == "processed"
            assert _rows(conn, "SELECT item_id FROM news_items WHERE item_id = 'oi-late'") == []

            commit.set()
            pending_write.result(timeout=30)

        # Turn two finds it by its marker, an hour below where turn one stopped.
        asyncio.run(_loop(db, sender, clock=_Clock()).advance())
        assert _notify_state(conn, "oi-late") == "processed"
    finally:
        writer.close()


def test_a_recovery_observation_is_readable_and_never_alerted(conn: Any) -> None:
    _oi_item(conn, "oi-recovered", at_ms=NOW - 60_000, change_bps=600, ingest_mode="recovery")
    sender = _Sender()
    asyncio.run(_loop(_Db(conn), sender, clock=_Clock()).advance())
    assert _notify_state(conn, "oi-recovered") == "historical"
    assert sender.cards == []
    assert _deliveries(conn) == []


# --- one card, once ------------------------------------------------------------------------------


def test_a_confirmed_delivery_key_is_never_executed_twice_across_turns_or_processes(conn: Any) -> None:
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    sender = _Sender()
    db = _Db(conn)
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    assert len(sender.cards) == 1
    first = _deliveries(conn)
    assert len(first) == 1
    assert first[0]["state"] == "sent"

    # Another turn, and then a whole new process over the same tables. Neither re-sends.
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    restarted = _loop(_Db(conn), sender, clock=_Clock())
    asyncio.run(restarted.start())
    asyncio.run(restarted.advance())
    assert len(sender.cards) == 1
    assert [row["delivery_key"] for row in _deliveries(conn)] == [first[0]["delivery_key"]]


def test_a_second_process_over_the_same_committed_state_opens_no_second_card(conn: Any) -> None:
    """Sequential, deliberately: this is the *committed-state* half, not the contended one.

    A second process that reads the same tables after the first has finished must find the group
    already answered -- no second "first" card, and no second send. The contended half, where two
    claims meet inside one transaction, is next door.
    """

    other = connect_postgres_test(read_only=False)
    try:
        _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
        sender = _Sender()
        # Two loops, two connections, the same backlog. The second sees the first's committed work.
        asyncio.run(_loop(_Db(conn), sender, clock=_Clock()).advance())
        asyncio.run(_loop(_Db(other), sender, clock=_Clock()).advance())
        assert len(_deliveries(conn)) == 1
        assert len(sender.cards) == 1
    finally:
        other.close()


def test_two_processes_contending_one_due_card_produce_exactly_one_send(conn: Any) -> None:
    """The cross-process guard, met under a real lock rather than in sequence.

    Two mechanisms, and the test holds a transaction open to reach each. Inside the claim transaction
    the row is locked, and the second process's `FOR UPDATE SKIP LOCKED` steps over it rather than
    waiting. Once that transaction commits the row reads `sending`, which the due scan does not
    select -- and that is the one that matters in production, because the claim commits *before* the
    network call, so the whole send window is guarded by the state and not by a lock.
    """

    other = connect_postgres_test(read_only=False)
    try:
        _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
        sender = _Sender()
        first = _Db(conn)
        second = _Db(other)
        first.hold_on = "news_market_notify_claim"

        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(lambda: asyncio.run(_loop(first, sender, clock=_Clock()).advance()))
            assert first.holding.wait(10), "the first loop never reached its claim transaction"

            # The row is locked by an uncommitted claim. The second loop must step over it, not wait.
            asyncio.run(_loop(second, sender, clock=_Clock()).advance())
            assert len(sender.cards) == 0

            first.release.set()
            holder.result(timeout=30)

        assert len(sender.cards) == 1
        rows = _deliveries(conn)
        assert len(rows) == 1
        assert rows[0]["attempts"] == 1
        assert rows[0]["state"] == "sent"

        # And once more, now that it is settled: still one card, still one attempt.
        asyncio.run(_loop(second, sender, clock=_Clock()).advance())
        assert len(sender.cards) == 1
        assert _deliveries(conn)[0]["attempts"] == 1
    finally:
        other.close()


def test_a_card_being_sent_is_not_offered_to_another_process(conn: Any) -> None:
    """The claim commits before the network call, so `sending` is what guards the send window."""

    other = connect_postgres_test(read_only=False)
    try:
        _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
        sender = _Sender()
        first = _Db(conn)
        second = _Db(other)
        # Hold the *settlement*: by then the claim has committed and the row reads `sending`.
        first.hold_on = "news_market_notify_settle"

        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(lambda: asyncio.run(_loop(first, sender, clock=_Clock()).advance()))
            assert first.holding.wait(10)
            in_flight = _rows(other, "SELECT state, attempts FROM news_market_deliveries")
            assert in_flight == [{"state": "sending", "attempts": 1}]

            asyncio.run(_loop(second, sender, clock=_Clock()).advance())
            assert len(sender.cards) == 1

            first.release.set()
            holder.result(timeout=30)

        assert len(sender.cards) == 1
        assert _deliveries(conn)[0]["state"] == "sent"
    finally:
        other.close()


def test_new_observations_merge_into_an_un_started_card_and_a_frozen_one_refuses_them(conn: Any) -> None:
    """Merging is the Items themselves pointing at one card; freezing is that pointer stopping."""

    clock = _Clock()
    sender = _Sender(available=False)
    db = _Db(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    _oi_item(conn, "oi-2", at_ms=NOW - 50_000, change_bps=620)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    # No sender, so the card is held: both observations are merged into the one un-started card.
    held = _deliveries(conn)
    assert len(held) == 1
    assert held[0]["state"] == "unavailable"
    assert held[0]["attempts"] == 0
    covered = _rows(
        conn,
        "SELECT item_id FROM news_items WHERE market_notify_delivery_key = %s ORDER BY item_id",
        (held[0]["delivery_key"],),
    )
    assert [row["item_id"] for row in covered] == ["oi-1", "oi-2"]

    # A third observation joins the same card while it is still un-started.
    _oi_item(conn, "oi-3", at_ms=NOW - 40_000, change_bps=640)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(_deliveries(conn)) == 1

    # The sender returns. The card is sent once, covering all three, and the snapshot is frozen.
    sender.set_available(True)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    sent = _deliveries(conn)
    assert len(sent) == 1
    assert sent[0]["state"] == "sent"
    assert sent[0]["covered_count"] == 3
    frozen = sent[0]["card"]
    assert frozen["header"]["title"]["content"]

    # A later observation cannot join the frozen card; it opens its own state instead.
    _oi_item(conn, "oi-4", at_ms=NOW - 30_000, change_bps=1_400)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    after = _deliveries(conn)
    assert len(after) == 2
    frozen_row = next(row for row in after if row["delivery_key"] == sent[0]["delivery_key"])
    assert frozen_row["card"] == frozen
    assert frozen_row["covered_count"] == 3
    # The set the frozen card speaks for is unchanged, and the later observation is on the new card.
    still_covered = _rows(
        conn,
        "SELECT item_id FROM news_items WHERE market_notify_delivery_key = %s ORDER BY item_id",
        (sent[0]["delivery_key"],),
    )
    assert [row["item_id"] for row in still_covered] == ["oi-1", "oi-2", "oi-3"]
    successor = next(row for row in after if row["delivery_key"] != sent[0]["delivery_key"])
    joined = _rows(
        conn,
        "SELECT item_id FROM news_items WHERE market_notify_delivery_key = %s",
        (successor["delivery_key"],),
    )
    assert [row["item_id"] for row in joined] == ["oi-4"]


# --- what each send answer durably means ---------------------------------------------------------


def test_a_new_alert_round_covers_only_itself_and_the_held_observation_stays_on_the_page(conn: Any) -> None:
    """The production MARSCOIN card, through the real tables (#562 PR-F).

    A first card, an observation held below the follow-up threshold, four quiet hours, and the
    observation that opens the next round. That card covered both and printed a six-hour span; it now
    covers its own round only, and the held observation keeps saying on the page that no card ever
    spoke for it rather than claiming to be merging into one that is never coming.
    """

    start = NOW - OI_QUIET_RESET_MS - 3_600_000
    clock = _Clock(start)
    sender = _Sender()
    db = _Db(conn)
    repos = repositories_for_connection(conn)

    _oi_item(conn, "oi-round1", at_ms=start, change_bps=600)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    held_at = start + 60_000
    clock.at_ms = held_at
    _oi_item(conn, "oi-held", at_ms=held_at, change_bps=610)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    # 6.1 % is not twice 6 %: no second card, and the observation is left with no card of its own.
    assert len(_deliveries(conn)) == 1

    opened_at = held_at + OI_QUIET_RESET_MS
    clock.at_ms = opened_at
    _oi_item(conn, "oi-round2", at_ms=opened_at, change_bps=620)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    deliveries = _deliveries(conn)
    assert len(deliveries) == 2
    second = deliveries[-1]
    assert second["state"] == "sent"
    assert second["trigger_reason"] == "first"
    # Exactly the adopted set, and a span that is one moment rather than six hours.
    assert second["covered_count"] == 1
    assert second["covered_from_ms"] == opened_at
    assert second["covered_to_ms"] == opened_at
    printed = second["card"]["elements"][0]["content"]
    assert fmt.clock(opened_at) in printed
    assert fmt.clock(held_at) not in printed
    covered = _rows(
        conn,
        "SELECT item_id FROM news_items WHERE market_notify_delivery_key = %s",
        (second["delivery_key"],),
    )
    assert [row["item_id"] for row in covered] == ["oi-round2"]

    held = repos.news.market_item(item_id="oi-held")
    assert held is not None
    assert held["delivery_key"] is None
    assert (held["notification_status"], held["notification_reason"]) == ("uncovered", REASON_ROUND_CLOSED)

    # The round that is open still merges everything it holds: a follow-up inside it speaks for the
    # observation it held as well as the one that triggered it.
    inside_at = opened_at + 60_000
    clock.at_ms = inside_at
    _oi_item(conn, "oi-inside", at_ms=inside_at, change_bps=630)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    triggered_at = inside_at + 60_000
    clock.at_ms = triggered_at
    _oi_item(conn, "oi-doubled", at_ms=triggered_at, change_bps=1_300)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    followup = _deliveries(conn)[-1]
    assert followup["trigger_reason"] == "followup"
    assert followup["covered_count"] == 2
    assert followup["covered_from_ms"] == inside_at
    # ... and the observation from the round that ended is still nobody's, two cards later.
    still_held = repos.news.market_item(item_id="oi-held")
    assert still_held is not None
    assert still_held["delivery_key"] is None
    assert still_held["notification_status"] == "uncovered"


def test_a_retryable_provably_not_sent_failure_retries_the_same_card_at_most_three_times(conn: Any) -> None:
    clock = _Clock()
    sender = _Sender()
    sender.raise_with = _Refused("news_delivery_feishu_http_failed", commit_phase="not_sent", retryable=True)
    db = _Db(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)

    asyncio.run(_loop(db, sender, clock=clock).advance())
    first = _deliveries(conn)[0]
    assert first["state"] == "pending"
    assert first["attempts"] == 1
    # The wait is a due time in PostgreSQL, not a sleeping task: nothing is retried before it.
    assert first["next_attempt_at_ms"] == clock.at_ms + 5_000
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert _deliveries(conn)[0]["attempts"] == 1

    clock.advance(5_000)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    second = _deliveries(conn)[0]
    assert second["attempts"] == 2
    assert second["next_attempt_at_ms"] == clock.at_ms + 30_000
    # And the longer wait is a due time too: nothing is retried before it either.
    clock.advance(29_000)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert _deliveries(conn)[0]["attempts"] == 2
    clock.advance(1_000)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    spent = _deliveries(conn)[0]
    assert spent["attempts"] == SEND_ATTEMPTS_MAX
    assert spent["state"] == "failed"
    assert spent["error"] == "news_delivery_feishu_http_failed"
    assert spent["receipt"] is None
    assert len(sender.cards) == SEND_ATTEMPTS_MAX

    # A failure is not permanent silence: the next observation opens a card of its own.
    sender.raise_with = None
    clock.advance(60_000)
    _oi_item(conn, "oi-2", at_ms=NOW - 20_000, change_bps=700)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(_deliveries(conn)) == 2


def test_an_explicit_rejection_is_never_retried(conn: Any) -> None:
    sender = _Sender()
    sender.raise_with = _Refused("news_delivery_feishu_business_rejected", commit_phase="not_sent", retryable=False)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    asyncio.run(_loop(_Db(conn), sender, clock=_Clock()).advance())
    row = _deliveries(conn)[0]
    assert row["state"] == "failed"
    assert row["attempts"] == 1


def test_an_unknown_result_is_never_auto_resent_and_never_locks_the_group(conn: Any) -> None:
    """A provider may have the card. Re-sending the same snapshot would double-notify a reader."""

    clock = _Clock()
    sender = _Sender()
    sender.raise_with = _Refused("news_delivery_feishu_transport_failed", commit_phase="unknown", retryable=True)
    db = _Db(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    unknown = _deliveries(conn)[0]
    assert unknown["state"] == "unknown"
    assert unknown["receipt"] is None
    assert unknown["settled_at_ms"] is not None

    # Not resent, however many turns pass.
    sender.raise_with = None
    for _ in range(3):
        clock.advance(60_000)
        asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(sender.cards) == 1

    # And the group is not frozen: the unknown snapshot is the anchor, so a genuine escalation still
    # reaches the reader while a repeat of the same magnitude does not.
    _oi_item(conn, "oi-small", at_ms=NOW - 30_000, change_bps=610)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(sender.cards) == 1
    _oi_item(conn, "oi-big", at_ms=NOW - 20_000, change_bps=1_400)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(sender.cards) == 2


def test_a_process_that_died_between_the_send_and_the_receipt_leaves_unknown_not_sent(conn: Any) -> None:
    """The startup sweep, which is why `sending` is not a state any process can inherit."""

    clock = _Clock()
    sender = _Sender()
    db = _Db(conn)
    db.fail_on = {"news_market_notify_settle"}
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)

    with pytest.raises(RuntimeError, match="injected_failure"):
        asyncio.run(_loop(db, sender, clock=clock).advance())
    interrupted = _deliveries(conn)[0]
    assert interrupted["state"] == "sending"
    assert len(sender.cards) == 1

    # The next process takes ownership and adopts what it found. It never re-sends that snapshot.
    db.fail_on = set()
    restarted = _loop(db, sender, clock=clock)
    swept = asyncio.run(restarted.start())
    assert swept == 1
    adopted = _deliveries(conn)[0]
    assert adopted["state"] == "unknown"
    assert adopted["receipt"] is None
    asyncio.run(restarted.advance())
    assert len(sender.cards) == 1


# --- an outage, and what recovery owes a reader ---------------------------------------------------


def test_an_outage_merges_per_group_and_recovery_sends_one_summary_for_each(conn: Any) -> None:
    """No per-window replay of what happened while nobody was listening, and no 30-minute drop."""

    clock = _Clock()
    sender = _Sender(available=False)
    db = _Db(conn)
    for index in range(6):
        _oi_item(conn, f"oi-{index}", at_ms=NOW - 3_600_000 + index * 600_000, change_bps=600 + index * 200)
    _liquidation_item(conn, "liq-1", at_ms=NOW - 3_000_000, side="long")
    _liquidation_item(conn, "liq-2", at_ms=NOW - 2_400_000, side="long")
    _liquidation_item(conn, "liq-3", at_ms=NOW - 1_800_000, side="short")

    # A long outage: several turns with no sender, well past any 30-minute threshold.
    for _ in range(4):
        clock.advance(45 * 60_000)
        asyncio.run(_loop(db, sender, clock=clock).advance())
    held = _deliveries(conn)
    assert {row["state"] for row in held} == {"unavailable"}
    # One card per group, not one per observation and not one per window.
    assert len(held) == 3
    assert sender.cards == []

    sender.set_available(True)
    clock.advance(2_000)
    asyncio.run(_loop(db, sender, clock=clock).advance())
    recovered = _deliveries(conn)
    assert len(recovered) == 3
    assert {row["state"] for row in recovered} == {"sent"}
    assert len(sender.cards) == 3
    # Each summary is labelled with the span it speaks for, which is what makes it honest.
    covered = sorted(row["covered_count"] for row in recovered)
    assert covered == [1, 2, 6]
    assert {row["attempts"] for row in recovered} == {1}
    summary = next(row for row in recovered if row["covered_count"] == 6)
    # A summary of six observations spans real time, and the card says so in its own text.
    assert summary["covered_from_ms"] < summary["covered_to_ms"]
    # ... and the card prints that span rather than a single moment, which is what makes a summary
    # of an outage readable: an en-dashed pair of clock times, and the two differ.
    printed = summary["card"]["elements"][0]["content"]
    span = re.search(r"(\d{2}:\d{2})–(\d{2}:\d{2})", printed)
    assert span is not None, printed
    assert span.group(1) != span.group(2)

    # Nothing was dropped: every observation is accounted for by exactly one card.
    orphans = _rows(
        conn,
        "SELECT item_id FROM news_items"
        " WHERE market_kind IS NOT NULL AND market_notify_state = 'processed'"
        "   AND market_notify_delivery_key IS NULL",
    )
    assert orphans == []


def _liquidation_item(conn: Any, item_id: str, *, at_ms: int, side: str) -> None:
    """One admitted liquidation, parsed by the real parser and written by the real repository."""

    title = f"DOGE Large {side.title()} Liquidation 412.53K at $0.2181"
    fact = parse_liquidation(
        title,
        item_id=item_id,
        fact_id=item_id,
        source_strategy_id="2083",
        provider_source="binance",
        event_at_ms=at_ms,
        received_at_ms=at_ms,
    )
    assert fact is not None
    repos = repositories_for_connection(conn)
    with repos.transaction():
        news = repos.news
        news.upsert_item(
            item_id=item_id,
            source_id="opennews",
            source_item_key=item_id,
            title=title,
            raw_first_line=title,
            description="",
            canonical_url=None,
            reporting_origin="opennews",
            published_at_ms=at_ms,
            observed_at_ms=at_ms,
            provider_metadata_json="{}",
            strategy_ids_json="[]",
            ingest_mode="live",
            trace_id="trace",
            now_ms=at_ms,
            market_kind="liquidation",
            market_source_strategy_id="2083",
            market_parse_status="parsed",
            market_parse_error=None,
            provider_params_json="{}",
        )
        news.insert_market_liquidation(fact=fact, ingest_mode="live", now_ms=at_ms)


def test_a_smart_money_round_sends_a_first_card_a_closing_card_and_no_third(conn: Any) -> None:
    """#582 §3.1 through the real ledger: two cards a day for one account in one instrument.

    Every observation here is inside one 24 h round, and the sequence is the one production reports
    -- opens, then closes, then more closes. The first card speaks for the round's opening, the
    closing card is the first `open -> close` in it, and the closes after that update the page. Under
    the 60 s window this replaces, these five records were five cards.
    """

    clock = _Clock()
    sender = _Sender()
    db = _Db(conn)
    _wallet_item(conn, "sm-open-1", at_ms=NOW - 7_200_000, action="open", side="long")
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(sender.cards) == 1

    track = _rows(conn, "SELECT anchor_action, anchor_position_side, round_started_at_ms FROM news_market_tracks")
    assert track == [{"anchor_action": "open", "anchor_position_side": "long", "round_started_at_ms": NOW - 7_200_000}]

    # A second open, hours later and still inside the round: the page moves, the reader is not
    # interrupted a second time.
    clock.advance(3_600_000)
    _wallet_item(conn, "sm-open-2", at_ms=NOW - 3_600_000, action="open", side="long")
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(sender.cards) == 1
    held = _rows(conn, "SELECT pending_reason FROM news_market_tracks")
    assert held == [{"pending_reason": REASON_SMART_MONEY_ROUND}]

    # The account starts closing. That is the round's one further card.
    clock.advance(1_800_000)
    _wallet_item(conn, "sm-close-1", at_ms=NOW - 1_800_000, action="close", side="long")
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(sender.cards) == 2

    change = next(row for row in _deliveries(conn) if row["trigger_reason"] == "action_change")
    assert change["covered_count"] == 2
    printed = change["card"]["elements"][0]["content"]
    assert "动作变化 1 次" in printed
    assert "首 开多" in printed
    assert "末 平多" in printed
    assert change["card"]["header"]["title"]["content"].startswith("聪明钱 · 平仓")

    # And the closes that follow it are page updates, whatever they say.
    for offset, action in ((900_000, "close"), (600_000, "open")):
        clock.advance(300_000)
        _wallet_item(conn, f"sm-after-{offset}", at_ms=NOW - offset, action=action, side="long")
        asyncio.run(_loop(db, sender, clock=clock).advance())
    assert len(sender.cards) == 2
    assert [row["trigger_reason"] for row in _deliveries(conn)] == ["first", "action_change"]
    assert _rows(conn, "SELECT pending_reason FROM news_market_tracks") == [
        {"pending_reason": REASON_SMART_MONEY_ROUND}
    ]


def test_a_close_that_arrives_before_the_first_card_is_sent_merges_into_it(conn: Any) -> None:
    """One un-started card per group, and no second intent beside it (#582 §3.1).

    The sender is unavailable while both records arrive, so the first card is still un-started when
    the close reaches the loop. It joins that card rather than opening a second one, and the round
    has then had its close: nothing more is prepared afterwards.
    """

    clock = _Clock()
    sender = _Sender(available=False)
    db = _Db(conn)
    _wallet_item(conn, "sm-open", at_ms=NOW - 120_000, action="open", side="long")
    asyncio.run(_loop(db, sender, clock=clock).advance())
    _wallet_item(conn, "sm-close", at_ms=NOW - 60_000, action="close", side="long")
    asyncio.run(_loop(db, sender, clock=clock).advance())

    assert [row["trigger_reason"] for row in _deliveries(conn)] == ["first"]

    sender.set_available(True)
    clock.advance(1_000)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    deliveries = _deliveries(conn)
    assert [row["trigger_reason"] for row in deliveries] == ["first"]
    assert deliveries[0]["covered_count"] == 2
    assert deliveries[0]["state"] == "sent"
    # The card printed both actions, and the anchor now holds the close.
    printed = deliveries[0]["card"]["elements"][0]["content"]
    assert "开多" in printed and "平多" in printed
    assert _rows(conn, "SELECT anchor_action FROM news_market_tracks") == [{"anchor_action": "close"}]

    # A close after it says nothing new, and no card is issued to repeat it.
    clock.advance(1_000)
    _wallet_item(conn, "sm-close-2", at_ms=NOW - 30_000, action="close", side="long")
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert [row["trigger_reason"] for row in _deliveries(conn)] == ["first"]


def test_an_unstructured_record_is_processed_and_never_alerted(conn: Any) -> None:
    """#582 §3.2 on the real tables: grouped, readable, no track, no delivery, no card.

    The page's answer is `not_alerted` / `unstructured_record_not_alerted`, which the read model can
    only give because the LEFT JOIN finds no track row at all. `merging` would have promised a card
    that is never coming, and the four such cards production did send are why the branch is gone.
    """

    clock = _Clock()
    sender = _Sender()
    db = _Db(conn)
    _raw_item(conn, "raw-withdraw", at_ms=NOW - 60_000, kind="smart_money", title="js-2 Withdraw $160,000 USDC")
    _raw_item(conn, "raw-oi", at_ms=NOW - 50_000, kind="oi", title="a line no OI template matched")

    turn = asyncio.run(_loop(db, sender, clock=clock).advance())

    assert turn.observations == 2
    assert turn.groups == 2
    assert turn.intents == 0
    assert sender.cards == []
    assert _deliveries(conn) == []
    assert _rows(conn, "SELECT group_key FROM news_market_tracks") == []

    marked = _rows(
        conn,
        "SELECT item_id, market_notify_state, market_notify_group_key AS group_key,"
        "       market_notify_delivery_key AS delivery_key"
        "  FROM news_items ORDER BY item_id",
    )
    assert marked == [
        {
            "item_id": "raw-oi",
            "market_notify_state": "processed",
            "group_key": "raw|oi|raw-oi",
            "delivery_key": None,
        },
        {
            "item_id": "raw-withdraw",
            "market_notify_state": "processed",
            "group_key": "raw|smart_money|raw-withdraw",
            "delivery_key": None,
        },
    ]

    repos = repositories_for_connection(conn)
    for item_id in ("raw-oi", "raw-withdraw"):
        detail = repos.news.market_item(item_id=item_id)
        assert detail is not None
        assert (detail["notification_status"], detail["notification_reason"]) == (
            "not_alerted",
            REASON_UNSTRUCTURED,
        )
        assert detail["notification_delivery"] is None

    # And a second turn changes nothing: the record is off the to-do list for good.
    assert asyncio.run(_loop(db, sender, clock=clock).advance()).observations == 0


def _raw_item(conn: Any, item_id: str, *, at_ms: int, kind: str, title: str) -> None:
    """One admitted market record whose template no parser could prove."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.upsert_item(
            item_id=item_id,
            source_id="opennews",
            source_item_key=item_id,
            title=title,
            raw_first_line=title,
            description="",
            canonical_url=None,
            reporting_origin="opennews",
            published_at_ms=at_ms,
            observed_at_ms=at_ms,
            provider_metadata_json="{}",
            strategy_ids_json="[]",
            ingest_mode="live",
            trace_id="trace",
            now_ms=at_ms,
            market_kind=kind,
            market_source_strategy_id="2026",
            market_parse_status="raw",
            market_parse_error="market_template_unmatched",
            provider_params_json="{}",
        )


def _wallet_item(conn: Any, item_id: str, *, at_ms: int, action: str, side: str) -> None:
    title = f"Machi Big Brother {action.title()} {side.title()} ETH $1,250,000.00, Price $3,120.50"
    fact = parse_smart_money(
        title,
        item_id=item_id,
        fact_id=item_id,
        source_strategy_id="2026",
        provider_source="hyperliquid",
        related_address="0x4d3a",
        event_at_ms=at_ms,
        received_at_ms=at_ms,
    )
    assert fact is not None, title
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.upsert_item(
            item_id=item_id,
            source_id="opennews",
            source_item_key=item_id,
            title=title,
            raw_first_line=title,
            description="",
            canonical_url=None,
            reporting_origin="opennews",
            published_at_ms=at_ms,
            observed_at_ms=at_ms,
            provider_metadata_json="{}",
            strategy_ids_json="[]",
            ingest_mode="live",
            trace_id="trace",
            now_ms=at_ms,
            market_kind="smart_money",
            market_source_strategy_id="2026",
            market_parse_status="parsed",
            market_parse_error=None,
            provider_params_json='{"relatedAddress": "0x4d3a"}',
        )
        repos.news.insert_market_smart_money(fact=fact, ingest_mode="live", now_ms=at_ms)


# --- the card's quote, on the same read model News is quoted from (#562 §2) ------------------------


_PRICED = (("WIF", "WIFUSDT", "0.5432", 7.91), ("DOGE", "DOGEUSDT", "0.19980", -3.2))


def _quotable(
    conn: Any,
    *,
    received_at_ms: int = NOW,
    reference_at_ms: int | None = NOW - 60_000,
) -> None:
    """The corpus's symbols priced in `news_quote_snapshots`, through the real writers.

    One snapshot for the whole venue, because that is what a venue answers with: a second snapshot
    naming only the other symbol would delist the first, which is `apply_snapshot`'s own rule.
    """

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [
                Instrument(
                    venue="binance.perp",
                    venue_symbol=venue_symbol,
                    base_symbol=symbol,
                    instrument_class="crypto",
                    quote_asset="USDT",
                )
                for symbol, venue_symbol, _, _ in _PRICED
            ],
            now_ms=NOW,
        )
        repos.price.replace_source_snapshot(
            source_key="binance.perp",
            quotes=[
                Quote(
                    venue="binance.perp",
                    venue_symbol=venue_symbol,
                    base_symbol=symbol,
                    price=Decimal(price),
                    price_kind="last",
                    instrument_class="crypto",
                    quote_asset="USDT",
                    change_pct=change_pct,
                    change_basis="rolling_24h",
                    source_at_ms=received_at_ms,
                    reference_at_ms=reference_at_ms,
                )
                for symbol, venue_symbol, price, change_pct in _PRICED
            ],
            target_count=len(_PRICED),
            source_at_ms=received_at_ms,
            received_at_ms=received_at_ms,
            now_ms=received_at_ms,
        )


def _card_body(conn: Any) -> str:
    rows = _deliveries(conn)
    assert len(rows) == 1
    return next(element["content"] for element in rows[0]["card"]["elements"] if element["tag"] == "markdown")


def test_a_fresh_quote_reaches_the_market_card_as_the_line_the_news_card_carries(conn: Any) -> None:
    """The frozen snapshot itself, not a rendering of it: this is what the reader was sent."""

    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    sender = _Sender()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())

    assert "行情 WIF $0.5432 24h +7.91%" in _card_body(conn)
    # And the frame's own two whale columns, which were selected by SQL and dropped before #562.
    assert "鲸鱼多头盈利 88.4% · 鲸鱼持仓/OI 143.9%" in _card_body(conn)
    assert db.turns("news_market_quotes") == 1
    assert _deliveries(conn)[0]["state"] == "sent"


def test_a_liquidation_card_carries_the_reported_price_and_the_quote_under_different_labels(conn: Any) -> None:
    """Both numbers, from two different planes, never written as one (#562 §3)."""

    _quotable(conn)
    _liquidation_item(conn, "liq-1", at_ms=NOW - 60_000, side="long")
    asyncio.run(_loop(_Db(conn), _Sender(), clock=_Clock()).advance())

    body = _card_body(conn)
    assert "来源报告价 $0.2181" in body  # the provider's own report, exact and unrounded
    assert "行情 DOGE $0.1998 24h -3.20%" in body


def test_a_stale_quote_costs_its_line_and_the_card_is_sent_anyway(conn: Any) -> None:
    """The rule is the read model's own, so this proves the age, not a re-implementation of it."""

    _quotable(conn, received_at_ms=NOW - QUOTE_FRESH_MAX_AGE_MS - 1_000)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    sender = _Sender()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())

    assert db.turns("news_market_quotes") == 1  # it was asked, and the answer was not fresh
    assert "行情" not in _card_body(conn)
    assert len(sender.cards) == 1
    assert _deliveries(conn)[0]["state"] == "sent"


def test_a_quote_port_that_raises_never_holds_back_a_card_or_spends_an_attempt(conn: Any) -> None:
    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.quote_failure = RuntimeError("quote_plane_down")
    sender = _Sender()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())

    assert "行情" not in _card_body(conn)
    assert len(sender.cards) == 1
    row = _deliveries(conn)[0]
    assert row["state"] == "sent"
    assert row["attempts"] == 1  # the send was the first attempt, not a retry of a failed quote
    assert row["settled_at_ms"] is not None


def test_a_quote_read_that_overruns_its_budget_leaves_the_card_unquoted_and_on_time(conn: Any) -> None:
    """The 1.5 s budget is the News budget, applied to the real read and not waited past."""

    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.slow_reads = {"news_market_quotes"}
    db.slow_seconds = QUOTE_READ_TIMEOUT_SECONDS + 0.5
    sender = _Sender()

    started = time.monotonic()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    elapsed = time.monotonic() - started

    assert elapsed < QUOTE_READ_TIMEOUT_SECONDS + 0.5
    assert "行情" not in _card_body(conn)
    assert len(sender.cards) == 1
    assert _deliveries(conn)[0]["attempts"] == 1


def test_a_quote_port_that_honours_no_budget_of_its_own_is_still_cut_off_by_the_loop(conn: Any) -> None:
    """The 1.5 s is the loop's promise to the reader, not the composition site's promise to the loop.

    The port here sleeps for far longer than the budget and applies none of its own -- a plausible
    future composition, and exactly what an unbounded external read would look like. The card must
    still go out, unquoted, without waiting for it.
    """

    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.quote_port_seconds = 30.0
    sender = _Sender()

    started = time.monotonic()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    elapsed = time.monotonic() - started

    assert elapsed < QUOTE_READ_TIMEOUT_SECONDS + 1.0
    assert "行情" not in _card_body(conn)
    assert len(sender.cards) == 1
    assert _deliveries(conn)[0]["attempts"] == 1


def test_a_retry_re_sends_the_frozen_card_and_asks_for_no_quote_at_all(conn: Any) -> None:
    """A retry is the same card again. Re-quoting it would spend a read to change nothing."""

    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    sender = _Sender()
    sender.raise_with = _Refused("rate_limited", commit_phase="not_sent", retryable=True)
    clock = _Clock()
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert db.turns("news_market_quotes") == 1

    sender.raise_with = None
    clock.advance(SEND_RETRY_BACKOFF_MS[0] + 1_000)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    assert db.turns("news_market_quotes") == 1  # the frozen card was re-sent, not re-quoted
    assert sender.cards[0] == sender.cards[1]
    assert _deliveries(conn)[0]["attempts"] == 2


def test_two_loops_racing_the_quote_read_send_one_card_and_spend_one_attempt(conn: Any) -> None:
    """The claim is a compare-and-set, because the row lock no longer spans the read (#562 PR-B).

    The quote read happens between the transaction that reads the due card and the one that freezes
    it, so the `FOR UPDATE SKIP LOCKED` the reader took is released before the claim -- which is the
    only reason a second process can be in this window at all. The test drives exactly that: the
    first loop pauses where its quote read is, having committed its read of a card at attempt 0, and
    a second loop then runs a whole turn on another connection, claims that card, has its send
    refused and settles it back to `pending` with attempt 1 and a retry due 5 s later.

    The first loop then resumes with a `DueCard` that describes a row that no longer exists: its
    claim must lose. One card sent, one attempt spent, and the second attempt still waiting for its
    backoff rather than being burned early against the first attempt's snapshot.
    """

    other = connect_postgres_test(read_only=False)
    try:
        _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
        _quotable(conn)
        first, second = _Db(conn), _Db(other)
        held_sender, racing_sender = _Sender(), _Sender()
        # The winner's send is refused with an explicit rate limit, so it settles back to `pending`
        # with attempts = 1: the exact row a stale reader would otherwise send a second time.
        racing_sender.raise_with = _Refused("rate_limited", commit_phase="not_sent", retryable=True)

        def race() -> None:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: asyncio.run(_loop(second, racing_sender, clock=_Clock()).advance())).result(
                    timeout=30
                )

        first.before_quotes = race
        asyncio.run(_loop(first, held_sender, clock=_Clock()).advance())

        assert len(racing_sender.cards) == 1
        # The loop that read the card first quoted it, lost the compare-and-set and sent nothing.
        assert held_sender.cards == []
        row = _deliveries(conn)[0]
        assert row["state"] == "pending"
        assert row["attempts"] == 1
        assert row["next_attempt_at_ms"] == NOW + SEND_RETRY_BACKOFF_MS[0]
        # And the frozen snapshot is still the one the winner rendered and sent.
        assert row["card"] == racing_sender.cards[0]
    finally:
        other.close()


def test_a_lost_claim_moves_to_the_next_due_card_rather_than_ending_the_turn(conn: Any) -> None:
    """A turn that returned on a lost race would idle a whole tick behind one contended card."""

    other = connect_postgres_test(read_only=False)
    try:
        _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
        _liquidation_item(conn, "liq-1", at_ms=NOW - 50_000, side="long")
        db = _Db(conn)
        sender = _Sender()
        loop = _loop(db, sender, clock=_Clock())
        lost: list[str] = []

        def claim_the_first_card_elsewhere() -> None:
            """A real claim by another process, in the window before this turn's own claim."""

            due = _rows(other, "SELECT delivery_key FROM news_market_deliveries ORDER BY created_at_ms LIMIT 1")
            lost.append(str(due[0]["delivery_key"]))
            repos = repositories_for_connection(other)
            with repos.transaction():
                assert repos.news.market_begin_send(
                    delivery_key=lost[0],
                    card={"claimed": "elsewhere"},
                    covered_count=1,
                    covered_from_ms=NOW,
                    covered_to_ms=NOW,
                    attempts=0,
                    due_at_ms=NOW,
                    now_ms=NOW,
                )

        db.before_quotes = claim_the_first_card_elsewhere
        asyncio.run(loop.advance())

        assert len(lost) == 1
        # One card lost its race and one card was still this turn's work; a turn that returned on the
        # loss would have sent nothing at all and idled a whole tick behind one contended card.
        assert len(sender.cards) == 1
        states = {str(row["delivery_key"]): row["state"] for row in _deliveries(conn)}
        assert states.pop(lost[0]) == "sending"  # held by the other process, never sent from here
        assert list(states.values()) == ["sent"]
    finally:
        other.close()


def test_the_quote_read_changes_no_notification_decision(conn: Any) -> None:
    """§4 stays a function of the observations: what was decided is identical either way.

    The same corpus is replayed twice into two databases -- once with every quote answered, once with
    the quote port raising on every card -- and every durable decision is compared. A quote that could
    reach `decide_group` would show up here as a different track, reason, count or due time.
    """

    def replay(*, quotes: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        connection = connect_postgres_test(read_only=False)
        try:
            connection.execute("TRUNCATE news_items, news_market_tracks, news_market_deliveries CASCADE")
            if quotes:
                _quotable(connection)
            db = _Db(connection)
            if not quotes:
                db.quote_failure = RuntimeError("quote_plane_down")
            clock = _Clock()
            sender = _Sender()
            loop = _loop(db, sender, clock=clock)
            _oi_item(connection, "oi-1", at_ms=NOW - 60_000, change_bps=600)
            _liquidation_item(connection, "liq-1", at_ms=NOW - 50_000, side="long")
            asyncio.run(loop.advance())
            _oi_item(connection, "oi-2", at_ms=NOW - 10_000, change_bps=1_400)
            clock.advance(30_000)
            asyncio.run(loop.advance())
            decisions = _rows(
                connection,
                "SELECT delivery_key, group_key, trigger_reason, trigger_item_id, state, attempts,"
                " covered_count, next_attempt_at_ms FROM news_market_deliveries ORDER BY delivery_key",
            )
            tracks = _rows(
                connection,
                "SELECT group_key, family, anchor_state, anchor_oi_change_bps, anchor_direction,"
                " next_due_at_ms, pending_reason FROM news_market_tracks ORDER BY group_key",
            )
            bodies = sorted(
                next(element["content"] for element in row["card"]["elements"] if element["tag"] == "markdown")
                for row in _deliveries(connection)
            )
            return decisions, tracks, bodies
        finally:
            connection.close()

    with_quotes = replay(quotes=True)
    without_quotes = replay(quotes=False)
    assert with_quotes[:2] == without_quotes[:2]
    assert len(with_quotes[0]) == 3  # not vacuous: three cards, two families, one follow-up
    # And not vacuous in the other direction either: the quoted run really did reach the reader with
    # a price, so what the comparison above holds equal is a decision and not an unquoted card.
    assert sum("行情" in body for body in with_quotes[2]) == 3
    assert not any("行情" in body for body in without_quotes[2])


# --- what the page then reads --------------------------------------------------------------------


def test_the_read_model_reports_exactly_what_the_loop_wrote(conn: Any) -> None:
    clock = _Clock()
    sender = _Sender()
    db = _Db(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    repos = repositories_for_connection(conn)

    before = repos.news.market_item(item_id="oi-1")
    assert before is not None
    assert before["notification_status"] == "unprocessed"
    assert before["notification_reason"] == "awaiting_market_loop"
    assert before["notification_delivery"] is None

    asyncio.run(_loop(db, sender, clock=clock).advance())

    after = repos.news.market_item(item_id="oi-1")
    assert after is not None
    assert after["notification_status"] == "sent"
    assert after["notification_covered_item_ids"] == ["oi-1"]
    delivery = after["notification_delivery"]
    assert delivery["state"] == "sent"
    assert delivery["trigger_reason"] == "first"
    assert delivery["receipt"]["provider"] == "test"

    groups, _truncated = repos.news.market_groups(
        kinds=("oi",),
        from_ms=NOW - 3_600_000,
        to_ms=NOW + 3_600_000,
        cursor_received_at_ms=1 << 62,
        cursor_item_id="",
        limit=10,
    )
    assert [group["notification_status"] for group in groups] == ["sent"]

    # And the per-kind status block counts the receipt beside the intake.
    summary = {
        row["market_kind"]: row for row in repos.news.market_sources(from_ms=NOW - 3_600_000, to_ms=NOW + 3_600_000)
    }
    assert summary["oi"]["received"] == 1
    assert summary["oi"]["sent"] == 1
    assert summary["oi"]["merged"] == 0
    assert summary["oi"]["last_sent_at_ms"] is not None


def test_a_held_card_says_the_sender_is_the_reason(conn: Any) -> None:
    sender = _Sender(available=False)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    asyncio.run(_loop(_Db(conn), sender, clock=_Clock()).advance())
    detail = repositories_for_connection(conn).news.market_item(item_id="oi-1")
    assert detail is not None
    assert detail["notification_status"] == "unavailable"
    assert detail["notification_delivery"]["error"] == REASON_SENDER_UNAVAILABLE
    assert detail["notification_delivery"]["attempts"] == 0


def test_the_notification_group_is_the_loops_own_record_of_its_decision(conn: Any) -> None:
    """It is written by the loop, and it is not the read model's display key."""

    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    asyncio.run(_loop(_Db(conn), _Sender(), clock=_Clock()).advance())
    detail = repositories_for_connection(conn).news.market_item(item_id="oi-1")
    assert detail is not None
    assert detail["notify_group_key"] == group_identity(_observation_of(detail)).group_key
    tracks = _rows(conn, "SELECT group_key, family, anchor_state FROM news_market_tracks")
    assert len(tracks) == 1
    assert tracks[0]["family"] == "oi"
    assert tracks[0]["anchor_state"] == "sent"


def _observation_of(detail: dict[str, Any]) -> Any:
    from tracefold.news.market_notifications import MarketObservation

    return MarketObservation.from_row(detail)


# --- the OI card's News line, on the delivered-card ledger it reads (#582 §3.3) --------------------
#
# The rows here are written by the production writers -- admission, the verdict insert, the delivery
# ledger -- because the statement under test joins all four of them. A hand-built row would prove the
# join it was built to satisfy and nothing about the one production produces.


def _news_event(
    conn: Any,
    *,
    hit_id: int,
    symbol: str,
    text: str,
    opened_at_ms: int,
    # The verdict's own headline, which is what the card's COALESCE falls back to when the frozen
    # snapshot carries no title. Distinct from every `delivered_title` below on purpose.
    headline_zh: str = "判定给出的标题",
    settled_at_ms: int | None = None,
    delivered_title: str | None = None,
    state: str = "sent",
) -> str:
    """One editorial Event about `symbol`, optionally with the card a reader was sent for it."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        stamp = f"{datetime.fromtimestamp(opened_at_ms / 1000, tz=UTC).isoformat()}"
        message = parse_opennews_message(
            {
                "method": "strategy.triggered",
                "params": {
                    "id": hit_id,
                    "text": text,
                    "link": f"https://example.test/{hit_id}",
                    "source": f"wire-{hit_id}",
                    "newsType": "news",
                    "engineType": "news",
                    "ts": stamp,
                    "aiRating": {"score": 90, "signal": "short", "status": "done"},
                    "coins": [{"expired": False, "grade": "A", "market_type": "cex", "score": 90, "symbol": symbol}],
                    "strategy": {"id": 1018, "name": "News Score > 70", "engine_type": "news", "source_type": "news"},
                },
            }
        )
        assert message is not None
        batch = admit_frame(
            repos,
            event=message,
            ingest_mode="live",
            observed_at_ms=opened_at_ms,
            trace_id=f"trace-{hit_id}",
            watchlist_symbols=frozenset(),
            now_ms=opened_at_ms,
        )
        assert len(batch.results) == 1 and batch.results[0].event_created
        event_id = batch.results[0].event_id
        if settled_at_ms is None:
            return event_id
        _persist_verdict(repos, event_id=event_id, symbol=symbol, headline_zh=headline_zh, at_ms=settled_at_ms - 1)
        card = {"header": {"title": {"content": delivered_title}}} if delivered_title is not None else {}
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card=card, now_ms=settled_at_ms - 1) == "new"
        assert repos.news.settle_delivery(
            event_id=event_id,
            kind="first",
            state=state,
            receipt={"ok": True} if state == "sent" else None,
            error_code=None if state == "sent" else "gave_up",
            now_ms=settled_at_ms,
        )
    return event_id


def _persist_verdict(repos: Any, *, event_id: str, symbol: str, headline_zh: str, at_ms: int) -> None:
    evidence = repos.news.latest_evidence_snapshot(event_id)
    assert evidence is not None
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[{"symbol": symbol, "role": "primary"}],
        direction="bearish",
        scope="single_name",
        magnitude=2,
        confidence=0.9,
        headline_zh=headline_zh,
        why_zh="",
    )
    judgment = scored_judgment(verdict)
    manifest_sha = "b" * 64
    assert repos.news.insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=TRIAGE_POLICY_VERSION,
        judgment_contract_version=judgment.judgment_contract_version,
        judgment_origin="model",
        rule_baseline_decision="push",
        final_decision="push",
        override_rule="trade_relevance_realtime",
        throttled_by=None,
        verdict=verdict.model_dump(mode="json"),
        model_editorial=judgment.editorial.model_dump(mode="json"),
        judgment_sha256=judgment.scored_judgment_sha256,
        runtime_manifest_sha=manifest_sha,
        model="test",
        program_version=SEMANTIC_PROGRAM_VERSION,
        program_sha256="a" * 64,
        degraded=False,
        error_code=None,
        trace={
            "judgment_contract_version": judgment.judgment_contract_version,
            "judgment_origin": "model",
            "judgment_sha256": judgment.scored_judgment_sha256,
            "verdict_sha256": canonical_sha(verdict.model_dump(mode="json")),
            "editorial_sha256": judgment.editorial.editorial_sha256,
            "runtime_manifest_sha": manifest_sha,
            "program_version": SEMANTIC_PROGRAM_VERSION,
            "program_sha256": "a" * 64,
            "evidence_version": int(evidence["evidence_version"]),
            "evidence_sha256": str(evidence["evidence_sha256"]),
            "focus_fact_id": str(evidence["focus_fact_id"]),
            "told": [],
            "told_count": 0,
        },
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=at_ms,
    )


def test_an_oi_card_carries_the_news_its_own_instrument_already_has(conn: Any) -> None:
    """The two numbers and the pushed titles, on the card the reader was actually sent."""

    _news_event(
        conn,
        hit_id=582_001,
        symbol="WIF",
        text="Dogwifhat treasury moves a large tranche to an exchange",
        opened_at_ms=NOW - 6 * 3_600_000,
        settled_at_ms=NOW - 6 * 3_600_000 + 30_000,
        delivered_title="WIF 国库向交易所转入大额代币",
    )
    _news_event(
        conn,
        hit_id=582_002,
        symbol="WIF",
        text="A major venue lists a new perpetual contract on dogwifhat",
        opened_at_ms=NOW - 2 * 3_600_000,
        settled_at_ms=NOW - 2 * 3_600_000 + 30_000,
        delivered_title="某大型交易所上线 WIF 永续合约",
    )
    # A third Event nobody was told about: it is the difference between the two numbers.
    _news_event(
        conn,
        hit_id=582_003,
        symbol="WIF",
        text="An analyst publishes a routine weekly note mentioning dogwifhat",
        opened_at_ms=NOW - 3 * 3_600_000,
    )
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    asyncio.run(_loop(db, _Sender(), clock=_Clock()).advance())

    body = _card_body(conn).split("\n")
    # After the whale line the frame carried, before the facts line the card ends on, newest first.
    assert body[2].startswith("鲸鱼多头盈利 ")
    assert body[3:6] == [
        "相关新闻 48h · 已推 2 · 共 3",
        "· 某大型交易所上线 WIF 永续合约 " + fmt.clock(NOW - 2 * 3_600_000 + 30_000),
        "· WIF 国库向交易所转入大额代币 " + fmt.clock(NOW - 6 * 3_600_000 + 30_000),
    ]
    assert body[6].startswith("WIF · opennews oi")
    assert db.turns("news_market_news") == 1
    assert _deliveries(conn)[0]["state"] == "sent"


def test_an_oi_card_whose_instrument_has_no_news_carries_no_news_line(conn: Any) -> None:
    """The ordinary case for most of the instruments a day's OI cards name (#582 §1)."""

    _news_event(
        conn,
        hit_id=582_010,
        symbol="DOGE",
        text="An unrelated token announces a governance vote",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="另一个代币宣布治理投票",
    )
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    asyncio.run(_loop(db, _Sender(), clock=_Clock()).advance())

    assert db.turns("news_market_news") == 1  # it was asked, and the answer was nothing
    assert "相关新闻" not in _card_body(conn)
    assert _deliveries(conn)[0]["state"] == "sent"


def test_only_an_oi_card_asks_what_news_its_instrument_has(conn: Any) -> None:
    """§3.3 leaves liquidation and smart money out deliberately: they spend no read at all."""

    _liquidation_item(conn, "liq-1", at_ms=NOW - 60_000, side="long")
    db = _Db(conn)
    asyncio.run(_loop(db, _Sender(), clock=_Clock()).advance())

    assert db.turns("news_market_news") == 0
    assert db.turns("news_market_quotes") == 1  # and the quote it does carry was still read
    assert _deliveries(conn)[0]["state"] == "sent"


def test_a_news_port_that_raises_never_holds_back_a_card_or_spends_an_attempt(conn: Any) -> None:
    _news_event(
        conn,
        hit_id=582_020,
        symbol="WIF",
        text="Dogwifhat treasury moves a large tranche to an exchange",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="WIF 国库向交易所转入大额代币",
    )
    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.news_failure = RuntimeError("news_plane_down")
    sender = _Sender()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())

    body = _card_body(conn)
    assert "相关新闻" not in body
    # The other display plane is untouched: one read failing costs its own line and no other.
    assert "行情 WIF $0.5432 24h +7.91%" in body
    assert len(sender.cards) == 1
    row = _deliveries(conn)[0]
    assert row["state"] == "sent"
    assert row["attempts"] == 1
    assert row["settled_at_ms"] is not None


def test_a_news_read_that_overruns_its_budget_leaves_the_card_newsless_and_on_time(conn: Any) -> None:
    """The 1.5 s budget is the quote's own, applied to the second display read and not waited past."""

    _news_event(
        conn,
        hit_id=582_030,
        symbol="WIF",
        text="Dogwifhat treasury moves a large tranche to an exchange",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="WIF 国库向交易所转入大额代币",
    )
    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.slow_reads = {"news_market_news"}
    db.slow_seconds = QUOTE_READ_TIMEOUT_SECONDS + 0.5
    sender = _Sender()

    started = time.monotonic()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    elapsed = time.monotonic() - started

    assert elapsed < QUOTE_READ_TIMEOUT_SECONDS + 0.5
    assert "相关新闻" not in _card_body(conn)
    assert len(sender.cards) == 1
    assert _deliveries(conn)[0]["attempts"] == 1


def test_a_news_port_that_hangs_costs_its_own_lines_and_leaves_the_quote_on_the_card(conn: Any) -> None:
    """One clock over both display reads, but two answers (#582 §3.3).

    `QUOTE_READ_TIMEOUT_SECONDS` is the loop's promise that no card waits longer than this before
    being sent, and two serial budgets would quietly make that promise twice as long -- so the two
    reads share one deadline. They do not share their degradation: the port here applies no budget of
    its own and hangs far past the deadline, and the price that did arrive is still on the card. Only
    the read that was still running is cancelled.
    """

    _news_event(
        conn,
        hit_id=582_040,
        symbol="WIF",
        text="Dogwifhat treasury moves a large tranche to an exchange",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="WIF 国库向交易所转入大额代币",
    )
    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.news_port_seconds = 30.0
    sender = _Sender()

    started = time.monotonic()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    elapsed = time.monotonic() - started

    assert elapsed < QUOTE_READ_TIMEOUT_SECONDS + 1.0
    body = _card_body(conn)
    assert "相关新闻" not in body
    assert "行情 WIF $0.5432 24h +7.91%" in body
    assert len(sender.cards) == 1
    assert _deliveries(conn)[0]["attempts"] == 1


def test_a_quote_port_that_hangs_costs_its_own_line_and_leaves_the_news_on_the_card(conn: Any) -> None:
    """The same claim from the other side: neither read can take the other's line down with it."""

    _news_event(
        conn,
        hit_id=582_045,
        symbol="WIF",
        text="Dogwifhat treasury moves a large tranche to an exchange",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="WIF 国库向交易所转入大额代币",
    )
    _quotable(conn)
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    db.quote_port_seconds = 30.0
    sender = _Sender()

    started = time.monotonic()
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    elapsed = time.monotonic() - started

    assert elapsed < QUOTE_READ_TIMEOUT_SECONDS + 1.0
    body = _card_body(conn)
    assert "行情" not in body
    assert "相关新闻 48h · 已推 1 · 共 1" in body
    assert len(sender.cards) == 1
    assert _deliveries(conn)[0]["attempts"] == 1


def test_a_retry_re_sends_the_frozen_card_and_asks_for_no_news_at_all(conn: Any) -> None:
    """A retry is the same card again; re-reading the News would spend a read to change nothing."""

    _news_event(
        conn,
        hit_id=582_050,
        symbol="WIF",
        text="Dogwifhat treasury moves a large tranche to an exchange",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="WIF 国库向交易所转入大额代币",
    )
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    db = _Db(conn)
    sender = _Sender()
    sender.raise_with = _Refused("rate_limited", commit_phase="not_sent", retryable=True)
    clock = _Clock()
    asyncio.run(_loop(db, sender, clock=clock).advance())
    assert db.turns("news_market_news") == 1

    sender.raise_with = None
    clock.advance(SEND_RETRY_BACKOFF_MS[0] + 1_000)
    asyncio.run(_loop(db, sender, clock=clock).advance())

    assert db.turns("news_market_news") == 1  # the frozen card was re-sent, not re-read
    assert sender.cards[0] == sender.cards[1]
    assert "相关新闻 48h · 已推 1 · 共 1" in _card_body(conn)


def test_the_news_read_changes_no_notification_decision(conn: Any) -> None:
    """§4 stays a function of the observations: what was decided is identical either way.

    The twin of `test_the_quote_read_changes_no_notification_decision`, for the second display read.
    """

    def replay(*, news: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        connection = connect_postgres_test(read_only=False)
        try:
            connection.execute("TRUNCATE news_items, news_market_tracks, news_market_deliveries CASCADE")
            connection.execute("TRUNCATE news_events, news_deliveries, news_verdicts CASCADE")
            _news_event(
                connection,
                hit_id=582_060,
                symbol="WIF",
                text="Dogwifhat treasury moves a large tranche to an exchange",
                opened_at_ms=NOW - 3_600_000,
                settled_at_ms=NOW - 3_600_000 + 30_000,
                delivered_title="WIF 国库向交易所转入大额代币",
            )
            db = _Db(connection)
            if not news:
                db.news_failure = RuntimeError("news_plane_down")
            clock = _Clock()
            sender = _Sender()
            loop = _loop(db, sender, clock=clock)
            _oi_item(connection, "oi-1", at_ms=NOW - 60_000, change_bps=600)
            _liquidation_item(connection, "liq-1", at_ms=NOW - 50_000, side="long")
            asyncio.run(loop.advance())
            _oi_item(connection, "oi-2", at_ms=NOW - 10_000, change_bps=1_400)
            clock.advance(30_000)
            asyncio.run(loop.advance())
            decisions = _rows(
                connection,
                "SELECT delivery_key, group_key, trigger_reason, trigger_item_id, state, attempts,"
                " covered_count, next_attempt_at_ms FROM news_market_deliveries ORDER BY delivery_key",
            )
            tracks = _rows(
                connection,
                "SELECT group_key, family, anchor_state, anchor_oi_change_bps, anchor_direction,"
                " next_due_at_ms, pending_reason FROM news_market_tracks ORDER BY group_key",
            )
            bodies = sorted(
                next(element["content"] for element in row["card"]["elements"] if element["tag"] == "markdown")
                for row in _deliveries(connection)
            )
            return decisions, tracks, bodies
        finally:
            connection.close()

    with_news = replay(news=True)
    without_news = replay(news=False)
    assert with_news[:2] == without_news[:2]
    assert len(with_news[0]) == 3  # not vacuous: three cards, two families, one follow-up
    # And not vacuous in the other direction: the answered run really did reach the reader with the
    # line, on the two OI cards and on neither liquidation card.
    assert sum("相关新闻" in body for body in with_news[2]) == 2
    assert not any("相关新闻" in body for body in without_news[2])


def test_the_pushed_news_read_answers_from_the_delivered_card_ledger(conn: Any) -> None:
    """The statement itself, against real rows: the window, the bound, the order and the title.

    Four pushed cards inside the window and one Event nobody was told about. `pushed` is the three
    newest by the time the *reader* was interrupted; `total` counts every editorial Event the
    instrument was named in, told or not, which is the second number the card prints.
    """

    # Four unrelated stories rather than four numbered copies of one: admission would fold near
    # duplicates into a single Event, and this read is about four separate interruptions.
    stories = (
        "Dogwifhat treasury moves a large tranche to an exchange wallet",
        "A major venue lists a perpetual contract on dogwifhat",
        "An auditor publishes findings on the dogwifhat bridge contract",
        "A payments company adds dogwifhat to its merchant checkout",
    )
    for index, story in enumerate(stories):
        _news_event(
            conn,
            hit_id=582_100 + index,
            symbol="WIF",
            text=story,
            opened_at_ms=NOW - (index + 1) * 3_600_000,
            settled_at_ms=NOW - (index + 1) * 3_600_000 + 30_000,
            delivered_title=f"WIF 消息 {index}",
        )
    _news_event(
        conn,
        hit_id=582_110,
        symbol="WIF",
        text="An analyst publishes a routine weekly note mentioning dogwifhat",
        opened_at_ms=NOW - 5 * 3_600_000,
    )

    answer = repositories_for_connection(conn).news.pushed_news_for_symbol("WIF", now_ms=NOW)

    assert [row["headline_zh"] for row in answer["pushed"]] == ["WIF 消息 0", "WIF 消息 1", "WIF 消息 2"]
    assert [row["at_ms"] for row in answer["pushed"]] == sorted(
        (row["at_ms"] for row in answer["pushed"]), reverse=True
    )
    assert answer["total"] == 5
    # And nothing at all for a symbol nobody wrote about, without a read that has to be guarded.
    assert repositories_for_connection(conn).news.pushed_news_for_symbol("", now_ms=NOW) == {"pushed": [], "total": 0}
    assert repositories_for_connection(conn).news.pushed_news_for_symbol("DOGE", now_ms=NOW) == {
        "pushed": [],
        "total": 0,
    }


def test_the_pushed_news_read_answers_for_an_equivalent_symbol(conn: Any) -> None:
    """`9988` and `BABA` are the same instrument to a reader, as they are to reader history."""

    conn.execute(
        "INSERT INTO news_symbol_aliases(alias, base_symbol, source, updated_at_ms) VALUES ('9988','BABA','seed',1)"
    )
    conn.commit()
    _news_event(
        conn,
        hit_id=582_200,
        symbol="9988",
        text="Alibaba prices a Hong Kong share placement",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="阿里巴巴配售新股",
    )
    repos = repositories_for_connection(conn)

    for asked in ("BABA", "9988"):
        answer = repos.news.pushed_news_for_symbol(asked, now_ms=NOW)
        assert [row["headline_zh"] for row in answer["pushed"]] == ["阿里巴巴配售新股"], asked
        assert answer["total"] == 1, asked


def test_the_pushed_news_read_counts_only_cards_a_reader_actually_received(conn: Any) -> None:
    """Delivered, editorial and inside the window -- each of the three is its own exclusion."""

    told = _news_event(
        conn,
        hit_id=582_300,
        symbol="WIF",
        text="Dogwifhat treasury moves a large tranche to an exchange",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="WIF 国库向交易所转入大额代币",
    )
    # A card that was never delivered, one deleted after it was, one settled before the window opened,
    # and one whose Event is a retired market kind rather than editorial News.
    _news_event(
        conn,
        hit_id=582_301,
        symbol="WIF",
        text="A venue publishes a scheduled maintenance notice touching dogwifhat",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="维护公告",
        state="terminal",
    )
    deleted = _news_event(
        conn,
        hit_id=582_302,
        symbol="WIF",
        text="A newsroom retracts an earlier report about dogwifhat holders",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="已撤回的报道",
    )
    _news_event(
        conn,
        hit_id=582_303,
        symbol="WIF",
        text="Dogwifhat reported an unusual funding rate two days ago entirely",
        opened_at_ms=NOW - 50 * 3_600_000,
        settled_at_ms=NOW - 50 * 3_600_000 + 30_000,
        delivered_title="窗口之外的旧卡",
    )
    retired = _news_event(
        conn,
        hit_id=582_304,
        symbol="WIF",
        text="Dogwifhat open interest rose sharply on a single venue overnight",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        delivered_title="退役的市场 Event",
    )
    conn.execute(
        "UPDATE news_deliveries SET delete_state = 'deleted', delete_evidence = '{}'::jsonb,"
        " delete_reason = 'test', delete_attempted_at_ms = %s, delete_settled_at_ms = %s WHERE event_id = %s",
        (NOW, NOW, deleted),
    )
    conn.execute("UPDATE news_events SET event_kind = 'oi' WHERE event_id = %s", (retired,))
    conn.commit()

    answer = repositories_for_connection(conn).news.pushed_news_for_symbol("WIF", now_ms=NOW)

    assert [row["event_id"] for row in answer["pushed"]] == [told]
    # The three excluded-from-`pushed` Events that are still inside the Event window are still counted;
    # the one that opened 50 h ago and the retired market kind are outside the total as well.
    assert answer["total"] == 3


def test_a_delivered_card_without_a_frozen_title_falls_back_to_the_verdict_headline(conn: Any) -> None:
    """The same COALESCE reader history reads, so both surfaces name a card the same way."""

    _news_event(
        conn,
        hit_id=582_400,
        symbol="WIF",
        text="Dogwifhat treasury moves a large tranche to an exchange",
        opened_at_ms=NOW - 3_600_000,
        settled_at_ms=NOW - 3_600_000 + 30_000,
        headline_zh="判定给出的标题",
    )

    answer = repositories_for_connection(conn).news.pushed_news_for_symbol("WIF", now_ms=NOW)

    assert [row["headline_zh"] for row in answer["pushed"]] == ["判定给出的标题"]


def test_a_card_pushed_inside_the_window_for_an_older_event_is_neither_quoted_nor_counted(conn: Any) -> None:
    """The two windows are one window (#582, Issue-owner decision after review).

    `已推` bounds by when the reader was interrupted and `共` by when the Event opened, so an Event
    that opened 50 h ago and was pushed 10 h ago used to be quoted by the first and missed by the
    second: `{pushed: 1, total: 0}`, and a total of zero prints nothing at all -- the headline was
    silently dropped rather than shown. Both statements now carry the Event window, so `pushed` is a
    subset of `total` and the card either shows a story or does not have one.
    """

    _news_event(
        conn,
        hit_id=582_500,
        symbol="WIF",
        text="Dogwifhat treasury moved a large tranche to an exchange two days ago",
        opened_at_ms=NOW - 50 * 3_600_000,
        settled_at_ms=NOW - 10 * 3_600_000,
        delivered_title="窗口之外开的 Event",
    )
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    asyncio.run(_loop(_Db(conn), _Sender(), clock=_Clock()).advance())

    assert repositories_for_connection(conn).news.pushed_news_for_symbol("WIF", now_ms=NOW) == {
        "pushed": [],
        "total": 0,
    }
    assert "相关新闻" not in _card_body(conn)
    assert "窗口之外开的 Event" not in _card_body(conn)


def test_a_card_pushed_inside_the_window_for_an_event_inside_it_is_quoted_and_counted(conn: Any) -> None:
    """The other half of the same boundary: 40 h opened, 10 h pushed, both numbers see it."""

    _news_event(
        conn,
        hit_id=582_501,
        symbol="WIF",
        text="A major venue listed a perpetual contract on dogwifhat yesterday",
        opened_at_ms=NOW - 40 * 3_600_000,
        settled_at_ms=NOW - 10 * 3_600_000,
        delivered_title="窗口之内开的 Event",
    )
    _oi_item(conn, "oi-1", at_ms=NOW - 60_000, change_bps=600)
    asyncio.run(_loop(_Db(conn), _Sender(), clock=_Clock()).advance())

    body = _card_body(conn)
    assert "相关新闻 48h · 已推 1 · 共 1" in body
    assert "· 窗口之内开的 Event " + fmt.clock(NOW - 10 * 3_600_000) in body
