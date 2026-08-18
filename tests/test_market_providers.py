from __future__ import annotations

from threading import Lock
from typing import cast

import pytest

from tracefold.app.market_providers import wire_asset_market
from tracefold.app.provider_ownership import gmgn_stream_enabled
from tracefold.integrations.gmgn.openapi_client import (
    GmgnOpenApiClient,
    GmgnOpenApiError,
    GmgnTokenInfo,
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


def _info(address: str, *, price: float | None = 1.5) -> GmgnTokenInfo:
    return GmgnTokenInfo(
        chain="solana",
        address=address,
        symbol=address.upper(),
        name=None,
        icon_url=None,
        banner_url=None,
        decimals=None,
        price=price,
        previous_price=None,
        market_cap=None,
        liquidity=None,
        holder_count=None,
        circulating_supply=None,
        total_supply=None,
        max_supply=None,
        website=None,
        twitter_username=None,
        telegram=None,
        gmgn_url=None,
        geckoterminal_url=None,
        description=None,
        pool=None,
        dev=None,
        stat=None,
        link=None,
        raw={"symbol": address.upper()},
    )


class _ScriptedGmgnClient(_RecordingGmgnClient):
    """Per-address behaviour: a GmgnTokenInfo, None (unknown token), or an exception to raise."""

    def __init__(self, script: dict[str, object]) -> None:
        super().__init__()
        self.script = script
        self._state_lock = Lock()
        self._active_lookups = 0
        self.max_active_lookups = 0

    def lookup_token_info(self, *, chain: str, address: str) -> GmgnTokenInfoLookup:
        with self._state_lock:
            self.calls.append((chain, address))
            self._active_lookups += 1
            self.max_active_lookups = max(self.max_active_lookups, self._active_lookups)
        try:
            behaviour = self.script.get(address)
            if isinstance(behaviour, BaseException):
                raise behaviour
            return GmgnTokenInfoLookup(info=cast(GmgnTokenInfo | None, behaviour), cache_status="miss")
        finally:
            with self._state_lock:
                self._active_lookups -= 1


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


def test_gmgn_batch_quotes_are_sequential_in_input_order_and_skip_unknown_tokens() -> None:
    client = _ScriptedGmgnClient({"a": _info("a"), "b": None, "c": _info("c", price=2.0)})
    provider = _gmgn_provider(client)

    quotes = provider.token_quotes([_quote("a"), _quote("b"), _quote("c")])

    assert [quote.address for quote in quotes] == ["a", "c"]  # "b" is unknown to GMGN
    assert client.calls == [("solana", "a"), ("solana", "b"), ("solana", "c")]
    assert client.max_active_lookups == 1  # 1 req/s pacing: no fan-out


def test_gmgn_batch_isolates_per_token_failures() -> None:
    client = _ScriptedGmgnClient({"bad": GmgnOpenApiError("token route failed"), "good": _info("good")})
    provider = _gmgn_provider(client)

    quotes = provider.token_quotes([_quote("bad"), _quote("good")])

    assert [quote.address for quote in quotes] == ["good"]


def test_gmgn_batch_with_zero_successful_lookups_is_a_local_expected_failure() -> None:
    client = _ScriptedGmgnClient({"x": GmgnOpenApiError("circuit open"), "y": GmgnOpenApiError("circuit open")})
    provider = _gmgn_provider(client)

    with pytest.raises(DexProviderTemporarilyUnavailable, match="circuit open"):
        provider.token_quotes([_quote("x"), _quote("y")])


def test_gmgn_batch_stops_at_the_deadline_and_returns_the_finished_quotes() -> None:
    clock = _ManualClock()
    client = _ScriptedGmgnClient({"first": _info("first"), "second": _info("second"), "third": _info("third")})
    provider = GmgnDexMarketProvider(
        GmgnOpenApiGateway(
            cast(GmgnOpenApiClient, client),
            token_info_cache_ttl_seconds=60,
            retry_attempts=1,
            clock=clock,
            sleep=lambda _: None,
        ),
        batch_deadline_seconds=25.0,
        clock=lambda: clock.value,
    )

    def advance(seconds: float):
        def _lookup(*, chain: str, address: str) -> GmgnTokenInfoLookup:
            clock.value += seconds
            return GmgnTokenInfoLookup(info=_info(address), cache_status="miss")

        return _lookup

    client.lookup_token_info = advance(13.0)  # type: ignore[method-assign]
    quotes = provider.token_quotes([_quote("first"), _quote("second"), _quote("third")])

    assert [quote.address for quote in quotes] == ["first", "second"]  # 26 s elapsed: "third" waits for the next batch


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
