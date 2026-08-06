from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tracefold.integrations.okx.dex_client import OkxDexClient, _candidate_from_row
from tracefold.integrations.okx.providers import OkxDexDiscoveryProvider
from tracefold.market import (
    CollectorService,
    IngestedEvent,
    materialize_event,
    normalize_gmgn_payload,
    parse_gmgn_frame,
    parse_gmgn_token_payload,
)
from tracefold.platform.resource import ResourceAdmissionTimeout

FIXTURES = Path(__file__).resolve().parent / "provider_frames"


class _InlineRuntimeResources:
    def __init__(self) -> None:
        self.operations: list[tuple[str, float]] = []

    async def run_business(self, _operation_name, function, /, *args, **kwargs):
        timeout_seconds = float(kwargs.pop("operation_timeout_seconds"))
        self.operations.append((_operation_name, timeout_seconds))
        return function(*args, **kwargs)


class _IdleUpstream:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        return


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
        resources = _InlineRuntimeResources()
        upstream = _IdleUpstream()
        service = CollectorService(
            store=store,
            upstream_client=upstream,
            db=resources,
        )
        service.snapshot_timeout = 0.05
        stop_event = asyncio.Event()
        collector_task = asyncio.create_task(service.run(stop_event=stop_event))
        try:
            await upstream.started.wait()
            await service.handle_frame(raw_frames[0], received_at_ms=1_777_729_877_000)
            pending_task = next(iter(service._pending_snapshots.values()))
            await service.handle_frame(raw_frames[1], received_at_ms=1_777_729_877_010)

            assert pending_task.done()
            assert pending_task.cancelled()
            assert service._pending_snapshots == {}
            assert service.status.snapshot_gate_outcomes["debounced_complete"] == 1
            assert service.status.snapshot_gate_outcomes["debounced_timeout"] == 0
            assert len(store.raw_frames) == 2
            assert len(store.twitter_events) == 1
            assert store.twitter_events[0].content.text == "complete snapshot with final token text"
            assert store.twitter_events[0].token_snapshot is not None
            assert store.twitter_events[0].token_snapshot.icon_url == "https://example.test/token.png"
            assert resources.operations == [
                ("gmgn_raw_frame_publish", 3.0),
                ("gmgn_raw_frame_publish", 3.0),
                ("gmgn_event_publish", 5.0),
            ]
        finally:
            stop_event.set()
            await asyncio.gather(collector_task, return_exceptions=True)

    asyncio.run(scenario())


def test_gmgn_debounced_timeout_handles_database_admission_without_orphaned_task() -> None:
    class _AdmissionTimeoutResources(_InlineRuntimeResources):
        async def run_business(self, operation_name, function, /, *args, **kwargs):
            if operation_name == "gmgn_event_publish":
                timeout_seconds = float(kwargs.pop("operation_timeout_seconds"))
                self.operations.append((operation_name, timeout_seconds))
                raise ResourceAdmissionTimeout("worker_database_admission_timeout:gmgn_event_publish")
            return await super().run_business(operation_name, function, *args, **kwargs)

    async def scenario() -> None:
        raw_frame = _load_json("gmgn_public_tw_partial_then_complete.json")[0]
        store = MemoryStore()
        upstream = _IdleUpstream()
        service = CollectorService(
            store=store,
            upstream_client=upstream,
            db=_AdmissionTimeoutResources(),
        )
        service.snapshot_timeout = 0.001
        stop_event = asyncio.Event()
        collector_task = asyncio.create_task(service.run(stop_event=stop_event))
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            await upstream.started.wait()
            await service.handle_frame(raw_frame, received_at_ms=1_777_729_877_000)
            await asyncio.sleep(0.02)
            gc.collect()
            await asyncio.sleep(0)

            assert not collector_task.done()
            assert service._pending_snapshots == {}
            assert service.status.snapshot_gate_outcomes["debounced_timeout"] == 1
            assert len(store.raw_frames) == 1
            assert store.twitter_events == []
            assert unhandled == []
        finally:
            stop_event.set()
            await asyncio.gather(collector_task, return_exceptions=True)
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())


def test_gmgn_debounced_timeout_propagates_unknown_failure() -> None:
    class _FailingResources(_InlineRuntimeResources):
        async def run_business(self, operation_name, function, /, *args, **kwargs):
            if operation_name == "gmgn_event_publish":
                timeout_seconds = float(kwargs.pop("operation_timeout_seconds"))
                self.operations.append((operation_name, timeout_seconds))
                raise RuntimeError("gmgn_event_publish_boom")
            return await super().run_business(operation_name, function, *args, **kwargs)

    async def scenario() -> None:
        raw_frame = _load_json("gmgn_public_tw_partial_then_complete.json")[0]
        upstream = _IdleUpstream()
        service = CollectorService(
            store=MemoryStore(),
            upstream_client=upstream,
            db=_FailingResources(),
        )
        service.snapshot_timeout = 0.001
        stop_event = asyncio.Event()
        collector_task = asyncio.create_task(service.run(stop_event=stop_event))
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            await upstream.started.wait()
            await service.handle_frame(raw_frame, received_at_ms=1_777_729_877_000)
            await asyncio.sleep(0.02)
            gc.collect()
            await asyncio.sleep(0)

            assert collector_task.done()
            with pytest.raises(ExceptionGroup) as captured:
                await collector_task
            assert "gmgn_event_publish_boom" in repr(captured.value)
            assert unhandled == []
        finally:
            stop_event.set()
            await asyncio.gather(collector_task, return_exceptions=True)
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())


def test_gmgn_event_publications_are_serial() -> None:
    class _ConcurrentRuntimeResources:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def run_business(self, _operation_name, function, /, *args, **kwargs):
            kwargs.pop("operation_timeout_seconds")
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return function(*args, **kwargs)
            finally:
                self.active -= 1

    async def scenario() -> int:
        raw_frame = _load_json("gmgn_public_tw_complete.json")
        parsed = parse_gmgn_frame(raw_frame)
        assert parsed is not None
        item = parsed["data"][0]
        resources = _ConcurrentRuntimeResources()
        service = CollectorService(
            store=MemoryStore(),
            upstream_client=None,
            db=resources,
        )

        await asyncio.gather(
            service._process_item(parsed["channel"], item, 1_777_729_877_581),
            service._process_item(parsed["channel"], item, 1_777_729_877_582),
        )
        return resources.max_active

    assert asyncio.run(scenario()) == 1


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
