from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import (
    CONFIDENCE_PROVIDER_EXACT,
    EVIDENCE_GMGN_PAYLOAD_EXACT,
    Author,
    Content,
    IdentityEvidenceRepository,
    IngestService,
    RegistryRepository,
    Source,
    TwitterEvent,
    parse_gmgn_token_payload,
)

VERSA_BASE_CA = "0x2cc0db4f8977accadb5b7da59c5923e14328eba3"


def make_token_event(
    event_id: str = "event-versa",
    *,
    text: str,
    received_at_ms: int = 1_777_800_000_000,
    author_handle: str = "toly",
) -> TwitterEvent:
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
        author=Author(handle=author_handle, name=author_handle, avatar=None, followers=1000, tags=[]),
        content=Content(text=text, media=[]),
        reference=None,
        unfollow_target=None,
        avatar_change=None,
        bio_change=None,
        raw={"id": event_id},
    )


def make_gmgn_payload_event(
    event_id: str = "event-payload",
    *,
    symbol: str = "PEPE",
    chain: str = "eth",
    address: str = "0x6982508145454ce325ddbe47a25d4ec3d2311933",
    received_at_ms: int = 1_777_800_000_000,
) -> TwitterEvent:
    snapshot = parse_gmgn_token_payload(
        {
            "tt": "ca",
            "t": {
                "a": address,
                "c": chain,
                "mc": "1000000",
                "p": "0.01",
                "s": symbol,
                "liquidity": "250000",
                "holder_count": 1000,
            },
        }
    )
    return replace(
        make_token_event(event_id, text=f"${symbol} payload", received_at_ms=received_at_ms),
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_token",
        ),
        token_snapshot=snapshot,
    )


def open_token_radar_runtime(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    migrate(conn)
    repos = repositories_for_connection(conn)
    ingest = IngestService(
        evidence=repos.evidence,
        entities=repos.entities,
        registry=repos.registry,
        identity_evidence=repos.identity_evidence,
        token_evidence=repos.token_evidence,
        token_intents=repos.token_intents,
        intent_resolutions=repos.intent_resolutions,
        discovery=repos.discovery,
        market_ticks=repos.market_ticks,
        market_tick_current=repos.market_tick_current,
        enriched_events=repos.enriched_events,
        event_anchor_jobs=repos.event_anchor_jobs,
        token_intent_lookup=repos.token_intent_lookup,
        radar_source_edges=repos.radar_source_edges,
        persisted_live=repos.persisted_live,
        transaction=repos.transaction,
        event_anchor_active_window_ms=300_000,
    )
    return conn, repos, ingest


def insert_base_versa_asset(conn: Any, *, observed_at_ms: int | None = None) -> None:
    now_ms = observed_at_ms if observed_at_ms is not None else int(time.time() * 1000)
    registry = RegistryRepository(conn)
    identity = IdentityEvidenceRepository(conn)
    asset = registry.upsert_chain_asset(
        chain_id="base",
        address=VERSA_BASE_CA,
        observed_at_ms=now_ms,
        status="canonical",
    )
    identity.upsert_identity_evidence(
        asset_id=str(asset["asset_id"]),
        evidence_kind=EVIDENCE_GMGN_PAYLOAD_EXACT,
        provider="fixture",
        lookup_mode="exact_address",
        chain_id=str(asset["chain_id"]),
        address=str(asset["address"]),
        symbol="VERSA",
        confidence=CONFIDENCE_PROVIDER_EXACT,
        observed_at_ms=now_ms,
    )
    identity.recompute_current_identity(
        str(asset["asset_id"]),
        now_ms=now_ms,
    )
    conn.commit()
