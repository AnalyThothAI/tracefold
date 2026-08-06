from __future__ import annotations

from typing import cast

import pytest

from tracefold.app.market_providers import (
    FallbackDexQuoteProvider,
    wire_asset_market,
)
from tracefold.app.provider_ownership import configured_profile_provider_ids, gmgn_stream_enabled
from tracefold.integrations.gmgn.openapi_client import GmgnOpenApiError
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
