from __future__ import annotations

import pytest

from tracefold.news.market_review.instruments import InstrumentSearchIdentity, pair_base_symbol
from tracefold.news.search import compile_news_search


class _Instruments:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search_identity(self, symbol: str) -> InstrumentSearchIdentity | None:
        self.calls.append(symbol)
        if symbol in {"BTC", "BTCUSDT", "BTC/USDT", "BTC-USDT", "BTC_USDT"}:
            return InstrumentSearchIdentity(base_symbol="BTC", event_symbols=("BTC", "XBT"))
        return None


@pytest.mark.parametrize("query", ["BTC", "btc", "$BTC", "ＢＴＣ", "BTCUSDT", "BTC/USDT", "BTC-USDT", "BTC_USDT"])
def test_compile_news_search_resolves_one_exact_asset_path(query: str) -> None:
    plan = compile_news_search(q=query, symbol=None, instruments=_Instruments())

    assert plan is not None
    assert plan.mode == "asset"
    assert plan.event_symbols == ("BTC", "XBT")
    assert plan.resolved_symbols == ("BTC",)
    assert plan.symbol is None


@pytest.mark.parametrize("query", ["BTC ETF", "Bitcoin spot ETF", "比特币 现货 ETF", "BTCT", "ABTC", "%", "_"])
def test_compile_news_search_routes_unconfirmed_or_natural_language_to_text(query: str) -> None:
    plan = compile_news_search(q=query, symbol=None, instruments=_Instruments())

    assert plan is not None
    assert plan.mode == "text"
    assert plan.event_symbols == ()
    assert plan.resolved_symbols == ()


def test_compile_news_search_keeps_unknown_structured_symbol_exact() -> None:
    plan = compile_news_search(q=None, symbol="  unknown  ", instruments=_Instruments())

    assert plan is not None
    assert plan.mode == "asset"
    assert plan.normalized_query == "UNKNOWN"
    assert plan.event_symbols == ("UNKNOWN",)
    assert plan.resolved_symbols == ()
    assert plan.symbol == "  unknown  "


def test_compile_news_search_never_turns_structured_provider_prefix_into_empty_asset() -> None:
    plan = compile_news_search(q=None, symbol="XYZ-", instruments=_Instruments())

    assert plan is not None
    assert plan.mode == "asset"
    assert plan.normalized_query == "XYZ-"
    assert plan.event_symbols == ("XYZ-",)


def test_compile_news_search_collapses_text_whitespace_without_rewriting_the_echo() -> None:
    plan = compile_news_search(q="  Bitcoin\tspot   ETF  ", symbol=None, instruments=_Instruments())

    assert plan is not None
    assert plan.mode == "text"
    assert plan.normalized_query == "Bitcoin spot ETF"
    assert plan.q == "  Bitcoin\tspot   ETF  "


def test_compile_news_search_rejects_two_sources_and_omits_empty_search() -> None:
    instruments = _Instruments()

    with pytest.raises(ValueError, match="news_feed_search_conflict"):
        compile_news_search(q="BTC", symbol="BTC", instruments=instruments)
    assert compile_news_search(q="  ", symbol=None, instruments=instruments) is None


@pytest.mark.parametrize(
    ("pair", "base"),
    [
        ("BTCUSDT", "BTC"),
        ("BTC/USDT", "BTC"),
        ("BTC-USDT", "BTC"),
        ("BTC_USDT", "BTC"),
        ("BTCT", None),
        ("ABTC", None),
        ("BTC-EUR", None),
    ],
)
def test_pair_parser_reuses_only_the_bounded_quote_vocabulary(pair: str, base: str | None) -> None:
    assert pair_base_symbol(pair) == base
