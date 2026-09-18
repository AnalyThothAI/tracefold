"""The send half of the smart-money alert, against real PostgreSQL (#649 §6, §7.1).

Four things are proved here, and each of them is a way a *correct* first report used to go nowhere:

* **§6.1** a continuous tail of new fills on the same token does not starve a report whose evidence
  is already complete below the collector's committed cutoff -- while a sell that landed *inside*
  that cutoff still invalidates it;
* **§6.2** a card the sender cannot decide yet is deferred rather than skipped, so an OI card behind
  it in the one shared queue is sent in the same turn and no attempt is spent;
* **§6.3** is next door in `test_news_chain_tape.py`, where the collector lives;
* **§7.1** an episode the notification stage rejected reads as its real terminal reason, never as
  "waiting to be sent".

The wallet fixtures are `test_wallet_net_buy`'s, and the OI ones are
`test_news_market_notifications`', because the point of these tests is that the *two families share
one queue* -- a second set of fakes would prove it about a second queue.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.integration.test_news_market_notifications import _oi_item
from tests.integration.test_wallet_net_buy import (
    NOW,
    Db,
    Sender,
    add_facts,
    events,
    fill,
    run,
    seed,
)
from tests.integration.test_wallet_net_buy import (
    conn as conn,  # noqa: PLC0414 -- the fixture, re-exported deliberately
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_notifications import DEFER_BACKOFF_MS, MarketNotificationLoop

pytestmark = pytest.mark.integration


def _loop(conn: Any, sender: Sender, *, at_ms: int = NOW, **kwargs: Any) -> MarketNotificationLoop:
    return MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: at_ms, **kwargs)


def _deliveries(conn: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT delivery_key, market_kind, state, attempts, next_attempt_at_ms, error"
            " FROM news_market_deliveries ORDER BY created_at_ms, delivery_key"
        ).fetchall()
    ]


def _wallet_delivery(conn: Any) -> dict[str, Any]:
    return next(row for row in _deliveries(conn) if row["market_kind"] == "wallet")


def _cut_off_at(conn: Any, *, block: int, log: int, at_ms: int) -> None:
    """Move the collector's committed cutoff, exactly as `chain_tape_record_coverage` does."""

    conn.execute(
        "UPDATE news_market_wallet_tape_state SET scanned_block = %s, scanned_log = %s, scanned_at_ms = %s"
        " WHERE state_id = 'chain_tape'",
        (block, log, at_ms),
    )
    conn.commit()


# --------------------------------------------------------------------- §6.1 the bounded cutoff
def test_a_continuous_tail_of_new_fills_does_not_starve_a_qualified_first_report(conn: Any) -> None:
    """§11 continuous input: collector keeps saving a tail while the sender decides.

    The old gate asked "does this token have *any* underived fill" with no upper bound. A token the
    roster keeps trading answers yes on every turn, for ever, and the qualified first report behind
    it was skipped every time -- no attempt, no suppression, no state change and nothing on the page
    to explain it. Here the same shape runs five turns: three qualifying buys are complete and
    derived below the cutoff, and every turn the collector commits two more fills *above* it.
    """

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    assert len(events(conn)) == 1
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    sender = Sender()
    loop = _loop(conn, sender)

    for turn in range(5):
        # The collector's next slice lands above the committed cutoff (block 100 + index) and stays
        # underived: this is the tail, and the sender has no business waiting for it.
        add_facts(
            conn,
            [fill(2_000 + turn * 2, wallet=7, at=NOW), fill(2_001 + turn * 2, wallet=8, at=NOW)],
            stamp=NOW,
        )
        _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
        asyncio.run(loop.advance())

    assert len(sender.cards) == 1
    assert _wallet_delivery(conn)["state"] == "sent"
    # Exactly one logical first report, however many turns ran.
    assert len(events(conn)) == 1


def test_evidence_not_yet_derived_inside_the_cutoff_defers_rather_than_sends(conn: Any) -> None:
    """A fill the detector has not consumed *inside* the cutoff is a real reason to wait.

    Deferral, not silence: the row moves its own due time and says why, so the page can show it and
    the queue behind it carries on.
    """

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    # A sixth fill committed below the cutoff and not yet derived.
    add_facts(conn, [fill(6, wallet=6, at=NOW)], stamp=NOW)
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    sender = Sender()

    turn = asyncio.run(_loop(conn, sender).advance())

    assert not sender.cards
    assert turn.deferred == 1
    row = _wallet_delivery(conn)
    assert (row["state"], row["attempts"], row["error"]) == ("pending", 0, "evidence_not_derived")
    assert row["next_attempt_at_ms"] == NOW + DEFER_BACKOFF_MS

    # Once the detector catches up, the same card is claimed and sent -- no attempt was burned.
    run(conn)
    asyncio.run(_loop(conn, sender, at_ms=NOW + DEFER_BACKOFF_MS).advance())
    assert len(sender.cards) == 1
    assert _wallet_delivery(conn)["attempts"] == 1


def test_a_sell_inside_the_cutoff_that_the_stored_snapshot_missed_still_stops_the_card(conn: Any) -> None:
    """§11: a sell inside the cutoff that the old snapshot does not know about must still count.

    The sell is written and marked derived *without* the detector, so `latest_matched` still says
    true and `latest_snapshot` still shows three qualified wallets. That is the state a detector
    lagging one turn behind the collector leaves behind, and the old send-time check read exactly
    that stored snapshot. The send-time re-evaluation runs `calculate_window` over the committed
    facts instead, so the sell is part of the answer and the card is suppressed with its real reason.
    """

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    sold = fill(6, wallet=1, kind="sell", usd="1200", at=NOW)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_record_fills([sold])
        repos.news.wallet_mark_receipt_derived(
            chain_id=sold.chain_id, tx_hash=sold.tx_hash, now_ms=NOW, reasons={sold.token: "conditions_not_met"}
        )
    conn.commit()
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    assert events(conn)[0]["latest_matched"] is True
    sender = Sender()

    turn = asyncio.run(_loop(conn, sender).advance())

    assert not sender.cards
    assert turn.suppressed == 1
    row = _wallet_delivery(conn)
    assert (row["state"], row["error"]) == ("failed", "invalidated_before_send")


def test_no_committed_cutoff_defers_instead_of_declaring_the_evidence_wrong(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    conn.execute(
        "UPDATE news_market_wallet_tape_state SET scanned_block = NULL, scanned_log = NULL, scanned_at_ms = NULL"
    )
    conn.commit()
    sender = Sender()

    turn = asyncio.run(_loop(conn, sender).advance())

    assert (not sender.cards, turn.deferred) == (True, 1)
    row = _wallet_delivery(conn)
    assert (row["state"], row["attempts"], row["error"]) == ("pending", 0, "collection_cutoff_unknown")


# --------------------------------------------------------------------- §6.2 one shared queue
def test_a_deferred_wallet_card_at_the_queue_head_does_not_block_an_oi_delivery(conn: Any) -> None:
    """§11: queue head A deferred, B/OI behind it -- A must not consume the turn's budget.

    Before the scheduling result type, the wallet card's third `None` changed nothing at all, so
    `market_due_delivery` answered with the same row on every one of the turn's twenty iterations
    and the OI card behind it waited a whole tick. Here the wallet card is older, so it *is* the
    head, and the OI card is sent in the same turn.
    """

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    add_facts(conn, [fill(6, wallet=6, at=NOW)], stamp=NOW)
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    # Admitted after the wallet observation, so the wallet card is the queue head.
    _oi_item(conn, "oi-behind-the-wallet-card", at_ms=NOW, change_bps=600)
    sender = Sender()

    turn = asyncio.run(_loop(conn, sender).advance())

    assert turn.deferred == 1
    assert turn.sent == 1
    assert len(sender.cards) == 1
    by_kind = {row["market_kind"]: row for row in _deliveries(conn)}
    assert by_kind["oi"]["state"] == "sent"
    assert (by_kind["wallet"]["state"], by_kind["wallet"]["attempts"]) == ("pending", 0)


def test_a_deferred_card_is_not_offered_again_inside_the_same_turn(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    add_facts(conn, [fill(6, wallet=6, at=NOW)], stamp=NOW)
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    sender = Sender()

    turn = asyncio.run(_loop(conn, sender).advance())

    # One deferral, not twenty: the row's own due time moved past this turn's clock.
    assert turn.deferred == 1


# --------------------------------------------------------------------- §7.1 one projection
def test_a_notification_stage_rejection_reads_as_its_real_reason_not_as_pending(conn: Any) -> None:
    """§11: detected at 59 s, decided at 61 s -- a terminal reason, never a permanent `pending`.

    This is the exact shape the route used to invent `pending` for: the episode is eligible, the
    notification stage refused it, and the refusal left no intent row -- only `stale_trigger` on the
    track. `episode_already_reported` and a discarded intent take the same path and are read from
    the same column.
    """

    seed(conn, [fill(i, wallet=i, at=NOW - 59_000) for i in range(1, 6)])
    run(conn)
    assert len(events(conn)) == 1
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    sender = Sender()

    asyncio.run(_loop(conn, sender, at_ms=NOW + 2_000).advance())

    assert not sender.cards
    assert conn.execute("SELECT count(*) AS n FROM news_market_deliveries").fetchone()["n"] == 0
    repos = repositories_for_connection(conn)
    row = repos.news.wallet_events(
        from_ms=NOW - 3_600_000, to_ms=NOW + 3_600_000, before_at_ms=None, before_id=None, limit=10
    )[0]
    assert row["notification_state"] == "not_alerted"
    assert row["notification_error"] == "stale_trigger"
    assert row["notification_next_due_at_ms"] is None


def test_pending_needs_an_unattempted_intent_and_a_next_due(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    sender = Sender()
    sender.available = False
    asyncio.run(_loop(conn, sender).advance())

    repos = repositories_for_connection(conn)
    row = repos.news.wallet_events(
        from_ms=NOW - 3_600_000, to_ms=NOW + 3_600_000, before_at_ms=None, before_id=None, limit=10
    )[0]
    # An intent exists, was never attempted, and the sender is simply absent.
    assert row["notification_state"] == "unavailable"
    assert row["notification_next_due_at_ms"] is not None

    sender.available = True
    asyncio.run(_loop(conn, sender).advance())
    sent = repos.news.wallet_events(
        from_ms=NOW - 3_600_000, to_ms=NOW + 3_600_000, before_at_ms=None, before_id=None, limit=10
    )[0]
    assert sent["notification_state"] == "sent"


def test_an_episode_the_detector_has_not_been_decided_on_reads_as_awaiting_decision(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)

    repos = repositories_for_connection(conn)
    row = repos.news.wallet_events(
        from_ms=NOW - 3_600_000, to_ms=NOW + 3_600_000, before_at_ms=None, before_id=None, limit=10
    )[0]
    assert row["notification_state"] == "awaiting_decision"
    assert row["notification_next_due_at_ms"] is None


def test_a_muted_lane_reads_as_not_alerted_with_the_detectors_own_reason(conn: Any) -> None:
    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn, enabled=False)

    repos = repositories_for_connection(conn)
    row = repos.news.wallet_events(
        from_ms=NOW - 3_600_000, to_ms=NOW + 3_600_000, before_at_ms=None, before_id=None, limit=10
    )[0]
    assert row["notification_state"] == "not_alerted"
    assert row["notification_error"] == "wallet_notifications_disabled"


# --------------------------------------------------------------------- §7.2 the diagnostic's SQL
def test_the_diagnostic_queries_run_against_the_real_schema_and_count_per_unit(conn: Any) -> None:
    """#649 §7.2: the read-only report is only useful if its five statements execute.

    Its arithmetic is proved without a database in `tests/news/test_news_wallet_diagnostic_report.py`.
    What needs PostgreSQL is that the SQL is valid against the deployed schema and that each count is
    of its own unit -- three fills across two transactions are three fills and two receipts, never
    five of anything.
    """

    seed(conn, [fill(i, wallet=i) for i in range(1, 6)])
    run(conn)
    _cut_off_at(conn, block=1000, log=100, at_ms=NOW)
    sender = Sender()
    sender.available = False
    asyncio.run(_loop(conn, sender).advance())
    news = repositories_for_connection(conn).news

    coverage = news.wallet_flow_coverage(from_ms=NOW - 3_600_000, to_ms=NOW + 1)
    reasons = news.wallet_derived_reasons(from_ms=NOW - 3_600_000, to_ms=NOW + 1)
    funnel = news.wallet_episode_funnel(from_ms=NOW - 3_600_000, to_ms=NOW + 1)
    unsent = news.wallet_episode_reasons(from_ms=NOW - 3_600_000, to_ms=NOW + 1)
    queue = news.wallet_send_queue(limit=10)

    assert (coverage["fills"], coverage["receipts"], coverage["wallets"], coverage["tokens"]) == (5, 5, 5, 1)
    assert (coverage["buys"], coverage["sells"], coverage["transfers_out"]) == (5, 0, 0)
    assert (coverage["priced"], coverage["unpriced"], coverage["underived"]) == (5, 0, 0)
    # Four of the five fills were read before the fifth made the window; only the one that opened the
    # episode carries `selected`. That split is the point of publishing the distribution at all.
    assert {row["reason"] for row in reasons} == {"selected", "conditions_not_met"}
    assert sum(int(row["fills"]) for row in reasons) == 5
    assert (funnel["episodes"], funnel["intents"], funnel["unavailable"]) == (1, 1, 1)
    assert funnel["sent"] == 0
    assert [row["reason"] for row in unsent] == ["merging_into_prepared_card"]
    assert [row["market_kind"] for row in queue] == ["wallet"]
    assert queue[0]["state"] == "unavailable"
