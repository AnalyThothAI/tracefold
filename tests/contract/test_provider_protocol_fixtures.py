from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from tracefold.integrations.okx.dex_client import OkxDexClient, _candidate_from_row
from tracefold.integrations.okx.dex_ws_client import _price_info_update_from_row, _rows_from_message
from tracefold.integrations.okx.providers import (
    OkxDexDiscoveryProvider,
    _domain_dex_market_fact_update,
)
from tracefold.market import (
    CollectorService,
    IngestedEvent,
    materialize_event,
    normalize_gmgn_payload,
    parse_gmgn_frame,
    parse_gmgn_token_payload,
)

FIXTURES = Path(__file__).resolve().parent / "provider_frames"


class _InlineRuntimeResources:
    async def run_realtime_db(self, function, /, *args, **kwargs):
        return function(*args, **kwargs)


def test_gmgn_complete_public_tw_fixture_parses_persists_and_extracts_token_identity() -> None:
    raw_frame = _load_json("gmgn_public_tw_complete.json")

    parsed = parse_gmgn_frame(raw_frame)

    assert parsed is not None
    assert parsed["channel"] == "twitter_monitor_token"
    assert len(parsed["data"]) == 1
    raw_persistence_input = {
        "source": "gmgn",
        "channel": parsed["channel"],
        "received_at_ms": 1_777_729_877_581,
        "raw_payload_json": raw_frame,
    }
    assert raw_persistence_input == {
        "source": "gmgn",
        "channel": "twitter_monitor_token",
        "received_at_ms": 1_777_729_877_581,
        "raw_payload_json": raw_frame,
    }

    events = normalize_gmgn_payload(parsed, received_at_ms=1_777_729_877_581)
    token_snapshot = parse_gmgn_token_payload(parsed["data"][0])

    assert len(events) == 1
    assert events[0].event_id == "gmgn:twitter_monitor_token:fixture-internal-001"
    assert events[0].token_snapshot is not None
    assert token_snapshot is not None
    assert token_snapshot.chain == "bsc"
    assert token_snapshot.address == "0x8F32420F2E3728C49399b00DD0A796602d984444"
    assert token_snapshot.symbol == "MIRROR"
    assert events[0].raw["providerOptionalNote"] == "retained-in-event-raw-only"
    assert "providerOptionalNote" not in events[0].token_snapshot.raw


def test_gmgn_partial_then_complete_fixture_debounces_and_ingests_only_complete_event() -> None:
    async def scenario() -> None:
        raw_frames = _load_json("gmgn_public_tw_partial_then_complete.json")
        store = MemoryStore()
        service = CollectorService(
            name="collector",
            settings=SimpleNamespace(
                enabled=True,
                interval_seconds=3.0,
                timeout_seconds=0.0,
                snapshot_timeout_seconds=0.05,
            ),
            db=object(),
            telemetry=object(),
            store=store,
            upstream_client=None,
        )
        service.bind_runtime_resources(_InlineRuntimeResources())

        await service.handle_frame(raw_frames[0], received_at_ms=1_777_729_877_000)
        await service.handle_frame(raw_frames[1], received_at_ms=1_777_729_877_010)
        await asyncio.sleep(0.06)

        assert service.status.snapshot_gate_outcomes["debounced_complete"] == 1
        assert service.status.snapshot_gate_outcomes["debounced_timeout"] == 0
        assert len(store.raw_frames) == 2
        assert len(store.twitter_events) == 1
        assert store.twitter_events[0].content.text == "complete snapshot with final token text"
        assert store.twitter_events[0].token_snapshot is not None
        assert store.twitter_events[0].token_snapshot.icon_url == "https://example.test/token.png"

    asyncio.run(scenario())


def test_okx_dex_ws_price_info_fixture_maps_provider_fields_to_domain_fact() -> None:
    message = _load_json("okx_dex_price_info.json")

    rows = _rows_from_message(message)
    integration_update = _price_info_update_from_row(rows[0])

    assert len(rows) == 1
    assert rows[0]["chainIndex"] == "56"
    assert rows[0]["tokenContractAddress"] == "0x8F32420F2E3728C49399b00DD0A796602d984444"
    assert rows[0]["providerExtraMemo"] == "retained-in-raw-only"
    assert integration_update is not None
    assert integration_update.chain_id == "56"
    assert integration_update.address == "0x8f32420f2e3728c49399b00dd0a796602d984444"
    assert integration_update.observed_at_ms == 1_778_085_000_000
    assert integration_update.price_usd == 0.1205
    assert integration_update.market_cap_usd == 123_456
    assert integration_update.liquidity_usd == 45_678
    assert integration_update.volume_24h_usd == 7_890
    assert integration_update.holders == 321
    assert integration_update.raw == rows[0]
    assert not hasattr(integration_update, "providerExtraMemo")

    domain_update = _domain_dex_market_fact_update(integration_update)

    assert domain_update.chain_id == "eip155:56"
    assert domain_update.address == "0x8f32420f2e3728c49399b00dd0a796602d984444"
    assert domain_update.observed_at_ms == 1_778_085_000_000
    assert domain_update.price_usd == 0.1205
    assert domain_update.market_cap_usd == 123_456
    assert domain_update.liquidity_usd == 45_678
    assert domain_update.holders == 321
    assert domain_update.raw is integration_update.raw
    assert domain_update.raw["providerExtraMemo"] == "retained-in-raw-only"
    assert not hasattr(domain_update, "providerExtraMemo")


def test_okx_dex_search_fixture_maps_rest_candidate_and_domain_candidate() -> None:
    fixture = _load_json("okx_dex_search_result.json")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v6/dex/market/token/search"
        assert request.url.params["search"] == "MIRROR"
        assert request.url.params["chains"] == "501"
        return httpx.Response(200, json=fixture)

    client = OkxDexClient(base_url="https://web3.okx.test", transport=httpx.MockTransport(handler))
    try:
        candidates = client.search_tokens(query="mirror", chain_indexes=["501"])
        direct_candidate = _candidate_from_row(fixture["data"][0])
        domain_candidates = OkxDexDiscoveryProvider(client).search_tokens(query="mirror", chain_ids=("solana",))
    finally:
        client.close()

    assert len(requests) == 2
    assert len(candidates) == 1
    assert direct_candidate is not None
    assert candidates[0] == direct_candidate
    assert candidates[0].chain_index == "501"
    assert candidates[0].chain == "solana"
    assert candidates[0].address == "Mirror111111111111111111111111111111111111"
    assert candidates[0].symbol == "MIRROR"
    assert candidates[0].name == "Mirror Fixture"
    assert candidates[0].price_usd == 0.12
    assert candidates[0].market_cap_usd == 123_456
    assert candidates[0].liquidity_usd == 45_678
    assert candidates[0].holders == 321
    assert candidates[0].community_recognized is True
    assert candidates[0].raw["providerExtraProfile"] == {"sourceRank": "fixture-only"}
    assert not hasattr(candidates[0], "providerExtraProfile")

    assert len(domain_candidates) == 1
    assert domain_candidates[0].chain_id == "solana"
    assert domain_candidates[0].address == "Mirror111111111111111111111111111111111111"
    assert domain_candidates[0].symbol == "MIRROR"
    assert domain_candidates[0].name == "Mirror Fixture"
    assert domain_candidates[0].price_usd == 0.12
    assert domain_candidates[0].raw is not candidates[0].raw
    assert domain_candidates[0].raw == candidates[0].raw
    assert domain_candidates[0].raw["providerExtraProfile"] == {"sourceRank": "fixture-only"}
    assert not hasattr(domain_candidates[0], "providerExtraProfile")


def _load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class MemoryStore:
    def __init__(self) -> None:
        self.twitter_events = []
        self.raw_frames = []

    def insert_raw_frame(self, **kwargs: Any) -> bool:
        self.raw_frames.append(kwargs)
        return True

    def ingest_event(self, event: Any) -> IngestedEvent:
        self.twitter_events.append(event)
        _row, event_read = materialize_event(event, now_ms=event.received_at_ms)
        return IngestedEvent(
            event=event_read,
            entities=[],
            token_intents=[],
            token_resolutions=[{"event_id": event.event_id, "target_id": "fixture:mirror"}],
            inserted=True,
        )
