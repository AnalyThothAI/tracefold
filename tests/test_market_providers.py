from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import cast

import pytest

from tracefold.app.market_providers import (
    FallbackDexQuoteProvider,
    wire_asset_market,
)
from tracefold.app.provider_ownership import configured_profile_provider_ids, gmgn_stream_enabled
from tracefold.integrations.gmgn.openapi_client import (
    GmgnOpenApiClient,
    GmgnOpenApiError,
    GmgnTokenInfoLookup,
)
from tracefold.integrations.gmgn.openapi_gateway import GmgnOpenApiGateway
from tracefold.integrations.gmgn.providers import GmgnDexMarketProvider
from tracefold.integrations.okx import providers as okx_providers
from tracefold.market.provider_contracts import (
    DexProviderTemporarilyUnavailable,
    DexTokenQuote,
    DexTokenQuoteRequest,
)
from tracefold.platform.config.settings import BinanceProviderConfig, OkxProviderConfig, ProvidersConfig, Settings


class _QuoteProvider:
    def __init__(self, result: list[DexTokenQuote]) -> None:
        self.result = result
        self.calls: list[list[DexTokenQuoteRequest]] = []

    def token_quotes(self, tokens: list[DexTokenQuoteRequest]) -> list[DexTokenQuote]:
        self.calls.append(list(tokens))
        return self.result

    def close(self) -> None:
        return None


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


def test_gmgn_provider_failure_is_a_local_expected_failure() -> None:
    provider = GmgnDexMarketProvider(cast(GmgnOpenApiGateway, _FailedGmgnGateway()))

    with pytest.raises(DexProviderTemporarilyUnavailable, match="provider response failed"):
        provider.token_profile(chain_id="solana", address="token")


def test_gmgn_adapter_serializes_profile_and_quote_lookups() -> None:
    client = _BlockingGmgnClient()
    provider = _gmgn_provider(client)
    second_started = Event()

    def quote_lookup() -> list[DexTokenQuote]:
        second_started.set()
        return provider.token_quotes([DexTokenQuoteRequest(chain_id="solana", address="quote-token")])

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider.token_profile, chain_id="solana", address="profile-token")
        assert client.first_lookup_entered.wait(timeout=1.0)
        second = executor.submit(quote_lookup)
        assert second_started.wait(timeout=1.0)
        try:
            assert not client.second_lookup_entered.wait(timeout=0.1)
        finally:
            client.release_first_lookup.set()
        assert first.result(timeout=1.0) is None
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
        lookup = executor.submit(provider.token_profile, chain_id="solana", address="profile-token")
        assert client.first_lookup_entered.wait(timeout=1.0)
        close = executor.submit(close_provider)
        assert close_started.wait(timeout=1.0)
        try:
            assert not client.close_entered.wait(timeout=0.1)
        finally:
            client.release_first_lookup.set()
        assert lookup.result(timeout=1.0) is None
        close.result(timeout=1.0)

    assert client.closed is True


def test_gmgn_adapter_token_info_cache_expires_through_public_interfaces() -> None:
    clock = _ManualClock()
    client = _RecordingGmgnClient()
    provider = _gmgn_provider(client, clock=clock)
    request = DexTokenQuoteRequest(chain_id="solana", address="shared-token")

    assert provider.token_profile(chain_id="solana", address="shared-token") is None
    assert provider.token_quotes([request]) == []
    assert client.calls == [("solana", "shared-token")]

    clock.value = 61.0
    assert provider.token_profile(chain_id="solana", address="shared-token") is None
    assert client.calls == [("solana", "shared-token"), ("solana", "shared-token")]


def test_gmgn_adapter_token_info_cache_is_bounded() -> None:
    client = _RecordingGmgnClient()
    provider = _gmgn_provider(client)

    for index in range(257):
        assert provider.token_profile(chain_id="solana", address=f"token-{index}") is None
    assert len(client.calls) == 257

    assert provider.token_profile(chain_id="solana", address="token-0") is None
    assert len(client.calls) == 258
    assert provider.token_profile(chain_id="solana", address="token-256") is None
    assert len(client.calls) == 258


def test_multi_target_quotes_use_the_bounded_bulk_provider_only() -> None:
    expected = [cast(DexTokenQuote, object())]
    primary = _QuoteProvider([])
    bulk_fallback = _QuoteProvider(expected)
    provider = FallbackDexQuoteProvider(primary=primary, fallback=bulk_fallback)
    requests = [
        DexTokenQuoteRequest(chain_id="solana", address="one"),
        DexTokenQuoteRequest(chain_id="solana", address="two"),
    ]

    assert provider.token_quotes(requests) == expected
    assert primary.calls == []
    assert bulk_fallback.calls == [requests]


def test_robinhood_chain_uses_the_official_okx_chain_index() -> None:
    assert okx_providers.okx_chain_index("robinhood") == "4663"
    assert okx_providers.okx_index_to_chain_id("4663") == "robinhood"


def test_okx_production_wiring_constructs_rest_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        providers=ProvidersConfig(
            okx=OkxProviderConfig(
                dex_api_key="configured-key",
                dex_secret_key="configured-secret",
                dex_passphrase="configured-passphrase",
            ),
            binance=BinanceProviderConfig(enabled=False),
        )
    )
    discovery = _QuoteProvider([])
    quotes = _QuoteProvider([])
    monkeypatch.setattr(okx_providers, "okx_dex_discovery_market", lambda _settings: discovery)
    monkeypatch.setattr(okx_providers, "okx_dex_quote_market", lambda _settings: quotes)

    providers = wire_asset_market(settings)

    assert providers.dex_discovery_market is not None
    assert providers.dex_quote_market is quotes


@pytest.mark.parametrize(
    ("gmgn_key", "binance_enabled", "expected"),
    (
        (None, False, ()),
        ("gmgn-key", False, ("gmgn_dex_profile",)),
        (None, True, ("binance_web3_profile",)),
        ("gmgn-key", True, ("gmgn_dex_profile", "binance_web3_profile")),
    ),
)
def test_configured_profile_provider_ids_are_the_single_enablement_policy(
    gmgn_key: str | None,
    binance_enabled: bool,
    expected: tuple[str, ...],
) -> None:
    settings = Settings(
        gmgn={"api_key": gmgn_key},
        providers={"binance": {"enabled": binance_enabled}},
    )

    assert configured_profile_provider_ids(settings) == expected


@pytest.mark.parametrize(("channels", "expected"), (((), False), (("twitter_monitor_basic",), True)))
def test_gmgn_stream_enablement_has_one_policy(channels: tuple[str, ...], expected: bool) -> None:
    assert gmgn_stream_enabled(Settings(upstream={"channels": channels})) is expected
