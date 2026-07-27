from __future__ import annotations

import time
from dataclasses import replace

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import (
    Author,
    Content,
    IngestService,
    Source,
    TwitterEvent,
    parse_gmgn_token_payload,
)

TOKEN_ADDRESS = "0xd0667d0618dc9b6d2a0a55f428b47c64bcf00416"


def make_event(
    event_id: str = "event-1",
    *,
    author_handle: str = "toly",
    text: str = "$PEPE mainnet 0x6982508145454ce325ddbe47a25d4ec3d2311933",
    received_at_ms: int | None = None,
) -> TwitterEvent:
    received_at_ms = received_at_ms if received_at_ms is not None else int(time.time() * 1000)
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
        author=Author(handle=author_handle, name=author_handle, avatar=None, followers=100, tags=[]),
        content=Content(text=text, media=[]),
        reference=None,
        unfollow_target=None,
        avatar_change=None,
        bio_change=None,
        raw={"id": event_id},
    )


def make_token_event(
    event_id: str,
    *,
    symbol: str,
    address: str,
    received_at_ms: int,
    author_handle: str = "toly",
) -> TwitterEvent:
    snapshot = parse_gmgn_token_payload(
        {
            "tt": "ca",
            "t": {
                "a": address,
                "c": "eth",
                "mc": "60490.341996",
                "p": "1.0",
                "s": symbol,
                "liquidity": "250000",
                "holder_count": 10000,
                "pool": {"pool_address": f"pool-{symbol.lower()}"},
                "stat": {"volume_24h": "750000"},
            },
        }
    )
    return replace(
        make_event(
            event_id,
            author_handle=author_handle,
            text=f"${symbol} rotation",
            received_at_ms=received_at_ms,
        ),
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_token",
        ),
        token_snapshot=snapshot,
    )


def open_runtime(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    migrate(conn)
    repos = repositories_for_connection(conn)
    ingest = IngestService(
        evidence=repos.evidence,
        entities=repos.entities,
        registry=repos.registry,
        identity_evidence=repos.identity_evidence,
        token_intent_lookup=repos.token_intent_lookup,
        token_evidence=repos.token_evidence,
        token_intents=repos.token_intents,
        intent_resolutions=repos.intent_resolutions,
        discovery=repos.discovery,
        market_ticks=repos.market_ticks,
        market_tick_current=repos.market_tick_current,
        enriched_events=repos.enriched_events,
        event_anchor_jobs=repos.event_anchor_jobs,
        token_radar_dirty_targets=repos.token_radar_dirty_targets,
        transaction=repos.transaction,
        event_anchor_active_window_ms=300_000,
    )
    return conn, ingest, repos.registry


def token_event(
    event_id: str,
    *,
    received_at_ms: int,
    author_handle: str = "traderpow",
    text: str = "$DOG",
) -> TwitterEvent:
    snapshot = parse_gmgn_token_payload(
        {
            "tt": "ca",
            "t": {
                "a": TOKEN_ADDRESS,
                "c": "eth",
                "mc": "60490.341996",
                "p": "1.0",
                "p1": None,
                "s": "DOG",
                "liquidity": "250000",
                "holder_count": 10000,
                "pool": {"pool_address": "pool-dog"},
                "stat": {"volume_24h": "750000"},
            },
        }
    )
    return replace(
        make_event(event_id, author_handle=author_handle, text=text, received_at_ms=received_at_ms),
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_token",
        ),
        token_snapshot=snapshot,
    )
