from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from websockets.exceptions import ConcurrencyError, ProtocolError

from tracefold.app.market_providers import FallbackDexQuoteProvider
from tracefold.integrations.gmgn.openapi_client import GmgnOpenApiError
from tracefold.integrations.gmgn.openapi_gateway import GmgnOpenApiGateway
from tracefold.integrations.gmgn.providers import GmgnDexMarketProvider
from tracefold.integrations.okx import providers as okx_providers
from tracefold.integrations.okx.dex_ws_client import OkxDexWebSocketMarketProvider
from tracefold.market.provider_contracts import (
    DexProviderTemporarilyUnavailable,
    DexTokenQuote,
    DexTokenQuoteRequest,
    MarketCapability,
    MarketStreamExpectedError,
)
from tracefold.platform.config.settings import OkxProviderConfig, ProvidersConfig, Settings


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


def test_gmgn_provider_failure_is_a_local_expected_failure() -> None:
    provider = GmgnDexMarketProvider(cast(GmgnOpenApiGateway, _FailedGmgnGateway()))

    with pytest.raises(DexProviderTemporarilyUnavailable, match="provider response failed"):
        provider.token_profile(chain_id="solana", address="token")


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


def test_okx_production_wiring_keeps_rest_without_constructing_the_unavailable_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        providers=ProvidersConfig(
            okx=OkxProviderConfig(
                dex_api_key="configured-key",
                dex_secret_key="configured-secret",
                dex_passphrase="configured-passphrase",
            )
        )
    )
    discovery = _QuoteProvider([])
    quotes = _QuoteProvider([])
    monkeypatch.setattr(okx_providers, "okx_dex_discovery_market", lambda _settings: discovery)
    monkeypatch.setattr(okx_providers, "okx_dex_quote_market", lambda _settings: quotes)

    def fail_if_ws_is_constructed(_settings: Settings) -> None:
        raise AssertionError("OKX DEX WS must not be wired without provider whitelist access")

    monkeypatch.setattr(okx_providers, "okx_dex_ws_market", fail_if_ws_is_constructed)

    bundle = okx_providers.wire_okx_provider_bundle(settings)

    assert bundle.dex_discovery_market is not None
    assert bundle.dex_quote_market is quotes
    assert bundle.stream_dex_market is None
    assert bundle.health.capabilities == frozenset(
        {
            MarketCapability.SEARCH_DEX,
            MarketCapability.QUOTE_DEX_EXACT,
        }
    )


@pytest.mark.parametrize(
    "transport_error",
    [TimeoutError("timeout"), OSError("transport"), ProtocolError("websocket")],
)
def test_okx_ws_adapter_keeps_expected_transport_failures_inside_the_stream_lane(
    transport_error: Exception,
) -> None:
    class _FailedStreamProvider:
        async def replace_subscriptions(self, _targets: list[dict[str, str]]) -> None:
            raise transport_error

        async def iter_price_info(self):
            raise transport_error
            yield  # pragma: no cover - makes this an async generator

    adapter = okx_providers.OkxDexWebSocketMarketProviderAdapter(
        cast(OkxDexWebSocketMarketProvider, _FailedStreamProvider())
    )

    async def scenario() -> None:
        with pytest.raises(MarketStreamExpectedError):
            await adapter.replace_subscriptions([SimpleNamespace(chain_id="solana", address="token-address")])
        with pytest.raises(MarketStreamExpectedError):
            await anext(adapter.iter_price_info())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "unknown_error",
    [RuntimeError("programming_error"), ConcurrencyError("concurrent_recv")],
)
def test_okx_ws_adapter_does_not_hide_unknown_failures(unknown_error: Exception) -> None:
    class _BrokenStreamProvider:
        async def replace_subscriptions(self, _targets: list[dict[str, str]]) -> None:
            raise unknown_error

        async def iter_price_info(self):
            raise unknown_error
            yield  # pragma: no cover - makes this an async generator

    adapter = okx_providers.OkxDexWebSocketMarketProviderAdapter(
        cast(OkxDexWebSocketMarketProvider, _BrokenStreamProvider())
    )

    async def scenario() -> None:
        with pytest.raises(type(unknown_error), match=str(unknown_error)):
            await adapter.replace_subscriptions([])
        with pytest.raises(type(unknown_error), match=str(unknown_error)):
            await anext(adapter.iter_price_info())

    asyncio.run(scenario())


def test_okx_ws_client_does_not_reconnect_after_a_concurrency_invariant(monkeypatch) -> None:
    provider = OkxDexWebSocketMarketProvider(
        url="wss://example.invalid/ws",
        api_key="api-key",
        secret_key="secret-key",
        passphrase="passphrase",
        subscription_limit=10,
    )
    provider._websocket = object()

    async def fail_receive(_websocket):
        raise ConcurrencyError("concurrent_recv")

    async def forbidden_reconnect(*_args, **_kwargs):
        raise AssertionError("concurrency invariant must not reconnect")

    monkeypatch.setattr(provider, "_recv_application_message", fail_receive)
    monkeypatch.setattr(provider, "_reconnect_after_recoverable_error", forbidden_reconnect)

    with pytest.raises(ConcurrencyError, match="concurrent_recv"):
        asyncio.run(anext(provider.iter_price_info()))
