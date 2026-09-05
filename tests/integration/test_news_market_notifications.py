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
from collections.abc import Callable
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.liquidations import parse_liquidation
from tracefold.news.market_notifications import (
    REASON_SENDER_UNAVAILABLE,
    SEND_ATTEMPTS_MAX,
    MarketNotificationLoop,
    group_identity,
)
from tracefold.news.oi_signals import measurement_definition, oi_source_contract
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
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.fail_on: set[str] = set()
        self.names: list[str] = []

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        return fn(repositories_for_connection(self.connection))

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        repos = repositories_for_connection(self.connection)
        with repos.transaction():
            if name in self.fail_on:
                raise RuntimeError(f"injected_failure:{name}")
            return fn(repos)


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

    async def send_prepared_card(self, card: Any, *, operation: str = "") -> dict[str, Any]:
        self.cards.append(dict(card))
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
        news = repos.news
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


def test_an_observation_committed_late_is_not_skipped_by_the_take_query(conn: Any) -> None:
    """A marker, not a high-water mark: a stamp cursor would have passed this row for ever."""

    _oi_item(conn, "oi-new", at_ms=NOW - 10_000, change_bps=600)
    sender = _Sender()
    db = _Db(conn)
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    assert _notify_state(conn, "oi-new") == "processed"

    # Now a transaction that began earlier finally commits, with an *older* stamp than the one just
    # processed. Its observation is in the next turn's answer because the marker says so.
    _oi_item(conn, "oi-late", at_ms=NOW - 3_600_000, change_bps=610)
    asyncio.run(_loop(db, sender, clock=_Clock()).advance())
    assert _notify_state(conn, "oi-late") == "processed"


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


def test_two_processes_racing_on_one_group_open_one_card_between_them(conn: Any) -> None:
    """The unique partial index is the referee, not a read-then-write in either process."""

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
    assert next(row for row in after if row["delivery_key"] == sent[0]["delivery_key"])["card"] == frozen


# --- what each send answer durably means ---------------------------------------------------------


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
    assert _deliveries(conn)[0]["attempts"] == 2
    clock.advance(30_000)
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
    for row in recovered:
        assert row["covered_from_ms"] <= row["covered_to_ms"]

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
