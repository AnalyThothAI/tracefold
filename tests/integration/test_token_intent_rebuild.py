from __future__ import annotations

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import (
    Author,
    Content,
    EvidenceRepository,
    Reference,
    Source,
    TwitterEvent,
    event_to_row,
    rebuild_recent_token_intents,
)


def test_rebuild_recent_token_intents_uses_current_builder_policy():
    conn = connect_postgres_test(read_only=False)
    try:
        migrate(conn)
        event = _event(
            event_id="event-cross",
            text="You’ll own $NOTHING and be happy.",
            reference_text="Could be something, but you will own solana:F7pB3ZdfBnyFw2LRHydWEn9BmhEa5XihXLjhySFRpump",
        )
        with conn.transaction():
            EvidenceRepository(conn).insert_event_row(event_to_row(event, now_ms=event.received_at_ms))

        result = rebuild_recent_token_intents(
            repos=repositories_for_connection(
                conn,
            ),
            now_ms=event.received_at_ms + 1_000,
            window="5m",
            limit=10,
        )

        intents = repositories_for_connection(
            conn,
        ).token_intents.intents_for_event("event-cross")
        assert result["events_rebuilt"] == 1
        assert {intent["intent_key"] for intent in intents} == {
            "symbol:NOTHING",
            "ca:solana:F7pB3ZdfBnyFw2LRHydWEn9BmhEa5XihXLjhySFRpump".lower(),
        }
        assert {intent["created_at_ms"] for intent in intents} == {event.received_at_ms}
    finally:
        conn.close()


def _event(*, event_id: str, text: str, reference_text: str | None = None) -> TwitterEvent:
    received_at_ms = 1_778_100_000_000
    return TwitterEvent(
        event_id=event_id,
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_basic",
        ),
        action="tweet",
        original_action=None,
        tweet_id=event_id,
        internal_id=event_id,
        timestamp=received_at_ms // 1000,
        received_at_ms=received_at_ms,
        author=Author(handle="tester", name="tester", avatar=None, followers=1, tags=[]),
        content=Content(text=text, media=[]),
        reference=Reference(
            tweet_id=f"{event_id}-ref",
            author_handle="ref",
            author_name="ref",
            author_avatar=None,
            author_followers=1,
            text=reference_text,
            media=[],
            type="quote",
        )
        if reference_text is not None
        else None,
        unfollow_target=None,
        avatar_change=None,
        bio_change=None,
        raw=None,
    )
