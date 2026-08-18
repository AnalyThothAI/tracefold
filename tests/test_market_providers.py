from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import cast

import pytest

from tracefold.app.market_providers import wire_asset_market
from tracefold.app.provider_ownership import gmgn_stream_enabled
from tracefold.integrations.gmgn.openapi_client import (
    GmgnOpenApiClient,
    GmgnOpenApiError,
    GmgnTokenInfoLookup,
)
from tracefold.integrations.gmgn.openapi_gateway import GmgnOpenApiGateway
from tracefold.integrations.gmgn.providers import GmgnDexMarketProvider
from tracefold.market.provider_contracts import DexProviderTemporarilyUnavailable, DexTokenQuoteRequest
from tracefold.platform.config.settings import Settings


class _FailedGmgnGateway:
    def lookup_token_info(self, *, chain: str, address: str) -> None:
        del chain, address
        raise GmgnOpenApiError("provider response failed")


class _ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _RecordingGmgnClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def lookup_token_info(self, *, chain: str, address: str) -> GmgnTokenInfoLookup:
        self.calls.append((chain, address))
        return GmgnTokenInfoLookup(info=None, cache_status="miss")

    def close(self) -> None:
        self.closed = True


class _BlockingGmgnClient(_RecordingGmgnClient):
    def __init__(self) -> None:
        super().__init__()
        self.first_lookup_entered = Event()
        self.release_first_lookup = Event()
        self.second_lookup_entered = Event()
        self.close_entered = Event()
        self._state_lock = Lock()
        self._active_lookups = 0
        self.max_active_lookups = 0

    def lookup_token_info(self, *, chain: str, address: str) -> GmgnTokenInfoLookup:
        with self._state_lock:
            self.calls.append((chain, address))
            call_number = len(self.calls)
            self._active_lookups += 1
            self.max_active_lookups = max(self.max_active_lookups, self._active_lookups)
        try:
            if call_number == 1:
                self.first_lookup_entered.set()
                if not self.release_first_lookup.wait(timeout=2.0):
                    raise TimeoutError("test did not release the first GMGN lookup")
            else:
                self.second_lookup_entered.set()
            return GmgnTokenInfoLookup(info=None, cache_status="miss")
        finally:
            with self._state_lock:
                self._active_lookups -= 1

    def close(self) -> None:
        self.close_entered.set()
        super().close()


def _gmgn_provider(client: object, *, clock: _ManualClock | None = None) -> GmgnDexMarketProvider:
    return GmgnDexMarketProvider(
        GmgnOpenApiGateway(
            cast(GmgnOpenApiClient, client),
            token_info_cache_ttl_seconds=60,
            retry_attempts=1,
            clock=clock or _ManualClock(),
            sleep=lambda _: None,
        )
    )


def _quote(address: str) -> DexTokenQuoteRequest:
    return DexTokenQuoteRequest(chain_id="solana", address=address)


def test_gmgn_provider_failure_is_a_local_expected_failure() -> None:
    provider = GmgnDexMarketProvider(cast(GmgnOpenApiGateway, _FailedGmgnGateway()))

    with pytest.raises(DexProviderTemporarilyUnavailable, match="provider response failed"):
        provider.token_quotes([_quote("token")])


def test_gmgn_adapter_serializes_concurrent_quote_lookups() -> None:
    client = _BlockingGmgnClient()
    provider = _gmgn_provider(client)
    second_started = Event()

    def quote_lookup() -> list:
        second_started.set()
        return provider.token_quotes([_quote("quote-token")])

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider.token_quotes, [_quote("first-token")])
        assert client.first_lookup_entered.wait(timeout=1.0)
        second = executor.submit(quote_lookup)
        assert second_started.wait(timeout=1.0)
        try:
            assert not client.second_lookup_entered.wait(timeout=0.1)
        finally:
            client.release_first_lookup.set()
        assert first.result(timeout=1.0) == []
        assert second.result(timeout=1.0) == []

    assert client.max_active_lookups == 1


def test_gmgn_adapter_close_waits_for_an_active_lookup() -> None:
    client = _BlockingGmgnClient()
    provider = _gmgn_provider(client)
    close_started = Event()

    def close_provider() -> None:
        close_started.set()
        provider.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        lookup = executor.submit(provider.token_quotes, [_quote("first-token")])
        assert client.first_lookup_entered.wait(timeout=1.0)
        close = executor.submit(close_provider)
        assert close_started.wait(timeout=1.0)
        try:
            assert not client.close_entered.wait(timeout=0.1)
        finally:
            client.release_first_lookup.set()
        assert lookup.result(timeout=1.0) == []
        close.result(timeout=1.0)

    assert client.closed is True


def test_gmgn_adapter_token_info_cache_expires_through_public_interfaces() -> None:
    clock = _ManualClock()
    client = _RecordingGmgnClient()
    provider = _gmgn_provider(client, clock=clock)

    assert provider.token_quotes([_quote("shared-token")]) == []
    assert provider.token_quotes([_quote("shared-token")]) == []
    assert client.calls == [("solana", "shared-token")]

    clock.value = 61.0
    assert provider.token_quotes([_quote("shared-token")]) == []
    assert client.calls == [("solana", "shared-token"), ("solana", "shared-token")]


def test_gmgn_adapter_token_info_cache_is_bounded() -> None:
    client = _RecordingGmgnClient()
    provider = _gmgn_provider(client)

    for index in range(257):
        assert provider.token_quotes([_quote(f"token-{index}")]) == []
    assert len(client.calls) == 257

    assert provider.token_quotes([_quote("token-0")]) == []
    assert len(client.calls) == 258
    assert provider.token_quotes([_quote("token-256")]) == []
    assert len(client.calls) == 258


@pytest.mark.parametrize(
    ("gmgn_key", "binance_enabled", "expect_dex", "expect_cex"),
    (
        (None, False, False, False),
        ("gmgn-key", False, True, False),
        (None, True, False, True),
        ("gmgn-key", True, True, True),
    ),
)
def test_market_providers_are_gmgn_quotes_and_binance_cex_only(
    gmgn_key: str | None, binance_enabled: bool, expect_dex: bool, expect_cex: bool
) -> None:
    settings = Settings(gmgn={"api_key": gmgn_key}, providers={"binance": {"enabled": binance_enabled}})
    providers = wire_asset_market(settings)
    try:
        assert (providers.dex_quote_market is not None) is expect_dex
        assert (providers.cex_market is not None) is expect_cex
    finally:
        for provider in (providers.dex_quote_market, providers.cex_market):
            if provider is not None:
                provider.close()


@pytest.mark.parametrize(("channels", "expected"), (((), False), (("twitter_monitor_basic",), True)))
def test_gmgn_stream_enablement_has_one_policy(channels: tuple[str, ...], expected: bool) -> None:
    assert gmgn_stream_enabled(Settings(upstream={"channels": channels})) is expected
