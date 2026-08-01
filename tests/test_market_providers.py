from __future__ import annotations

from typing import cast

from tracefold.app.market_providers import FallbackDexQuoteProvider
from tracefold.market.provider_contracts import DexTokenQuote, DexTokenQuoteRequest


class _QuoteProvider:
    def __init__(self, result: list[DexTokenQuote]) -> None:
        self.result = result
        self.calls: list[list[DexTokenQuoteRequest]] = []

    def token_quotes(self, tokens: list[DexTokenQuoteRequest]) -> list[DexTokenQuote]:
        self.calls.append(list(tokens))
        return self.result

    def close(self) -> None:
        return None


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
