from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tests.factories import make_event
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import (
    CaptureResult,
    DeterministicTokenResolver,
    EnrichedEventCapture,
    IngestService,
    MarketTick,
    MentionKeys,
    market_tick_id,
    parse_gmgn_token_payload,
)


def open_ingest(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    migrate(conn)
    repos = repositories_for_connection(
        conn,
    )
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
        persisted_live=repos.persisted_live,
        transaction=repos.transaction,
        event_anchor_active_window_ms=300_000,
    )
    return conn, repos, ingest


def test_ingest_mirror_writes_unresolved_token_intent(tmp_path):
    conn, _, ingest = open_ingest(tmp_path)
    event = make_event("event-1", text="$mirror is moving")
    try:
        result = ingest.ingest_event(event)
    finally:
        conn.close()

    assert result.inserted is True
    assert result.token_intents[0]["display_symbol"] == "MIRROR"
    assert result.token_intents[0]["created_at_ms"] == event.received_at_ms
    assert result.token_resolutions[0]["resolution_status"] == "NIL"
    assert result.token_resolutions[0]["target_id"] is None


def test_ingest_gmgn_payload_writes_identity_without_market_observation(tmp_path):
    conn, repos, ingest = open_ingest(tmp_path)
    address = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
    try:
        snapshot = parse_gmgn_token_payload(
            {
                "tt": "ca",
                "t": {
                    "a": address,
                    "c": "eth",
                    "s": "PEPE",
                    "mc": "1000000",
                    "p": "0.01",
                },
            }
        )
        event = replace(
            make_event("event-gmgn-payload-no-market", text="$PEPE payload identity"),
            token_snapshot=snapshot,
        )
        result = ingest.ingest_event(event)
        resolution = next(item for item in result.token_resolutions if item["resolution_status"] == "EXACT")
        asset = repos.registry.find_assets_by_address(chain_id="eth", address=address)[0]
        identity_evidence = repos.identity_evidence.list_identity_evidence(asset["asset_id"])
        enriched_events = repos.enriched_events.list_by_event_id(event.event_id)
        market_tick = repos.market_ticks.latest_at_or_before(
            target_type="chain_token",
            target_id=f"eip155:1:{address}",
            at_ms=event.received_at_ms,
            max_lag_ms=60_000,
        )
    finally:
        conn.close()

    assert resolution["resolution_status"] == "EXACT"
    assert resolution["target_type"] == "Asset"
    assert resolution["target_id"] == f"asset:eip155:1:erc20:{address}"
    assert any(item["evidence_kind"] == "gmgn_payload_exact" for item in identity_evidence)
    assert market_tick is None
    assert enriched_events[0]["target_type"] == "chain_token"
    assert enriched_events[0]["target_id"] == f"eip155:1:{address}"
    assert enriched_events[0]["capture_method"] == "unavailable"


def test_ingest_chain_ca_from_gmgn_url_writes_exact_registry_asset(tmp_path):
    conn, repos, ingest = open_ingest(tmp_path)
    address = "0x44b28991b167582f18ba0259e0173176ca125505"
    try:
        result = ingest.ingest_event(
            make_event("event-upic", text=f"https://gmgn.ai/eth/token/{address}"),
        )
        resolution = result.token_resolutions[0]
        asset = repos.registry.find_assets_by_address(chain_id="eth", address=address)[0]
        identity_evidence = repos.identity_evidence.list_identity_evidence(asset["asset_id"])
    finally:
        conn.close()

    assert resolution["resolution_status"] == "EXACT"
    assert resolution["target_type"] == "Asset"
    assert resolution["target_id"] == f"asset:eip155:1:erc20:{address}"
    assert asset["asset_id"] == resolution["target_id"]
    assert len(identity_evidence) == 1
    assert identity_evidence[0]["evidence_kind"] == "tweet_contract_mention"
    assert identity_evidence[0]["confidence"] == "mention_only"
    assert identity_evidence[0]["source_event_id"] == "event-upic"


def test_ingest_unknown_chain_ca_is_retained_as_unresolved_asset(tmp_path):
    conn, _, ingest = open_ingest(tmp_path)
    try:
        result = ingest.ingest_event(
            make_event("event-1", text="watch 0xd0667d0618dc9b6d2a0a55f428b47c64bcf00416"),
        )
    finally:
        conn.close()

    # address_hint is EIP-55 checksummed by entity_extractor.to_checksum_address (intentional canonicalisation).
    assert result.token_intents[0]["address_hint"] == "0xd0667d0618Dc9B6d2a0A55f428b47C64Bcf00416"
    assert result.token_resolutions[0]["resolution_status"] == "NIL"
    assert result.token_resolutions[0]["target_id"] is None


def test_address_only_resolution_prioritizes_canonical_robinhood_over_unknown_chains(tmp_path):
    conn, repos, _ingest = open_ingest(tmp_path)
    address = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
    try:
        with repos.transaction():
            repos.registry.upsert_chain_asset(chain_id="aaa", address=address, observed_at_ms=1)
            robinhood = repos.registry.upsert_chain_asset(
                chain_id="robinhood",
                address=address,
                observed_at_ms=1,
            )
        decision = DeterministicTokenResolver(registry=repos.registry).resolve(
            intent_id="intent-robinhood-address",
            event_id="event-robinhood-address",
            keys=MentionKeys(address=address),
            decision_time_ms=2,
        )
    finally:
        conn.close()

    assert decision.target_id == robinhood["asset_id"]
    assert decision.reason_codes == ["RESOLVED_BY_CHAIN_PRIORITY"]


def test_ingest_capture_tick_updates_current_and_persisted_live_event(tmp_path):
    conn, repos, ingest = open_ingest(tmp_path)
    event = make_event(
        "event-capture-dirty",
        text="https://gmgn.ai/eth/token/0x6982508145454ce325ddbe47a25d4ec3d2311933 captured",
        received_at_ms=1_800_000_000_000,
    )
    try:
        with repos.transaction():
            prepared, resolutions, capture_result = _prepared_capture(ingest, event)
            result = ingest.commit_prepared_event(prepared, resolutions=resolutions, captures=[capture_result])
        current_row = repos.market_tick_current.get(
            target_type=capture_result.tick.target_type,
            target_id=capture_result.tick.target_id,
        )
        live_row = conn.execute(
            """
            SELECT event_kind, payload_json
            FROM persisted_live_events
            WHERE source_key = %s
            """,
            (f"event:{event.event_id}",),
        ).fetchone()
    finally:
        conn.close()

    assert result.inserted is True
    assert current_row is not None
    assert current_row["tick_id"] == capture_result.tick.tick_id
    assert live_row is not None
    assert live_row["event_kind"] == "event"
    assert live_row["payload_json"]["event"]["event_id"] == event.event_id


def test_ingest_capture_tick_current_rolls_back_with_event_transaction(tmp_path):
    conn, repos, ingest = open_ingest(tmp_path)
    event = make_event(
        "event-capture-rollback",
        text="https://gmgn.ai/eth/token/0x6982508145454ce325ddbe47a25d4ec3d2311933 rollback",
        received_at_ms=1_800_000_010_000,
    )
    try:
        ingest.event_anchor_jobs = _FailingEventAnchorJobs()
        with pytest.raises(RuntimeError, match="event_anchor_enqueue_failed_for_test"), repos.transaction():
            prepared, resolutions, capture_result = _prepared_capture(ingest, event)
            ingest.commit_prepared_event(prepared, resolutions=resolutions, captures=[capture_result])

        event_row = conn.execute("SELECT * FROM events WHERE event_id = %s", (event.event_id,)).fetchone()
        tick_row = repos.market_ticks.latest_at_or_before(
            target_type=capture_result.tick.target_type,
            target_id=capture_result.tick.target_id,
            at_ms=capture_result.tick.observed_at_ms,
            max_lag_ms=1,
        )
        current_row = repos.market_tick_current.get(
            target_type=capture_result.tick.target_type,
            target_id=capture_result.tick.target_id,
        )
    finally:
        conn.close()

    assert event_row is None
    assert tick_row is None
    assert current_row is None


def test_ingest_event_and_persisted_live_journal_roll_back_together(
    tmp_path,
):
    conn, repos, ingest = open_ingest(tmp_path)
    event = make_event(
        "event-live-journal-rollback",
        text="$ROLLBACK event and live journal",
        received_at_ms=1_800_000_020_000,
    )
    try:
        with pytest.raises(RuntimeError, match="rollback_after_live_append"), repos.transaction():
            prepared = ingest.prepare_event(event)
            ingest.prepare_registry_for_resolution(prepared)
            resolutions = ingest.resolve_prepared(prepared, persist=False)
            result = ingest.commit_prepared_event(
                prepared,
                resolutions=resolutions,
                captures=[],
            )
            assert result.inserted is True
            raise RuntimeError("rollback_after_live_append")

        event_row = conn.execute(
            "SELECT event_id FROM events WHERE event_id = %s",
            (event.event_id,),
        ).fetchone()
        live_row = conn.execute(
            "SELECT cursor FROM persisted_live_events WHERE source_key = %s",
            (f"event:{event.event_id}",),
        ).fetchone()
    finally:
        conn.close()

    assert event_row is None
    assert live_row is None


def test_ingest_registry_asset_rolls_back_with_failed_event_transaction(tmp_path):
    conn, repos, ingest = open_ingest(tmp_path)
    address = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
    event = make_event(
        "event-registry-rollback",
        text=f"https://gmgn.ai/eth/token/{address} registry rollback",
        received_at_ms=1_800_000_015_000,
    )
    ingest.event_anchor_jobs = _FailingEventAnchorJobs()

    try:
        with pytest.raises(RuntimeError, match="event_anchor_enqueue_failed_for_test"):
            ingest.ingest_event(event)

        event_row = conn.execute("SELECT event_id FROM events WHERE event_id = %s", (event.event_id,)).fetchone()
        assets = repos.registry.find_assets_by_address(chain_id="eth", address=address)
        intent_rows = conn.execute(
            "SELECT intent_id FROM token_intents WHERE event_id = %s",
            (event.event_id,),
        ).fetchall()
    finally:
        conn.close()

    assert event_row is None
    assert assets == []
    assert intent_rows == []


def test_ingest_rejects_loose_capture_result_contract(tmp_path):
    conn, repos, ingest = open_ingest(tmp_path)
    event = make_event(
        "event-loose-capture-result",
        text="https://gmgn.ai/eth/token/0x6982508145454ce325ddbe47a25d4ec3d2311933 loose",
        received_at_ms=1_800_000_020_000,
    )
    try:

        class LooseCaptureResult:
            pass

        with pytest.raises(RuntimeError, match="ingest_capture_result_contract_required"), repos.transaction():
            prepared, resolutions, capture_result = _prepared_capture(ingest, event)
            LooseCaptureResult.tick = capture_result.tick
            LooseCaptureResult.capture = capture_result.capture
            ingest.commit_prepared_event(prepared, resolutions=resolutions, captures=[LooseCaptureResult()])
    finally:
        conn.close()


def _prepared_capture(ingest: IngestService, event):
    prepared = ingest.prepare_event(event)
    ingest.prepare_registry_for_resolution(prepared)
    resolutions = ingest.resolve_prepared(prepared, persist=False)
    market_resolution = next(
        item for decision in resolutions if (item := ingest.market_resolution_for_decision(decision)) is not None
    )
    tick = _capture_tick(market_resolution, observed_at_ms=event.received_at_ms)
    capture = EnrichedEventCapture(
        event_id=event.event_id,
        intent_id=str(market_resolution["intent_id"]),
        resolution_id=str(market_resolution["resolution_id"]),
        target_type=tick.target_type,
        target_id=tick.target_id,
        t_event_ms=event.received_at_ms,
        tick_observed_at_ms=tick.observed_at_ms,
        tick_id=tick.tick_id,
        tick_lag_ms=0,
        capture_method="tier3_inline",
        capture_reason="inline_quote",
        created_at_ms=event.received_at_ms,
    )
    return prepared, resolutions, CaptureResult(tick=tick, capture=capture)


def _capture_tick(market_resolution: dict[str, object], *, observed_at_ms: int) -> MarketTick:
    target_type = str(market_resolution["target_type"])
    target_id = str(market_resolution["target_id"])
    chain, _, token_address = target_id.rpartition(":")
    source_provider = "gmgn_dex_quote"
    return MarketTick(
        tick_id=market_tick_id(
            target_type=target_type,
            target_id=target_id,
            source_provider=source_provider,
            observed_at_ms=observed_at_ms,
        ),
        target_type=target_type,  # type: ignore[arg-type]
        target_id=target_id,
        chain=chain,
        token_address=token_address,
        exchange=None,
        instrument=None,
        pricefeed_id=None,
        source_tier="tier3_inline",
        source_provider=source_provider,
        observed_at_ms=observed_at_ms,
        received_at_ms=observed_at_ms,
        price_usd=Decimal("1.23"),
        liquidity_usd=Decimal("1000"),
        volume_24h_usd=Decimal("5000"),
        open_interest_usd=None,
        market_cap_usd=Decimal("1000000"),
        holders=None,
        created_at_ms=observed_at_ms,
        raw_payload_json={"source": "ingest-test"},
    )


class _FailingEventAnchorJobs:
    def enqueue_for_capture(self, *args, **kwargs) -> None:
        raise RuntimeError("event_anchor_enqueue_failed_for_test")
