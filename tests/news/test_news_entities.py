"""Entity provenance and retrieval relations cannot become stronger identity evidence."""

from __future__ import annotations

import pytest

from tracefold.news.entities import (
    EntityKey,
    asset_features,
    asset_retrieval_symbols,
    identity_value,
    source_asset_symbols,
    source_mentions_asset,
    stored_asset_codes,
)


def test_exact_key_equality_uses_namespace_and_identifier_only() -> None:
    a = asset_features("$SI", "crypto", basis_ref="ev:first")[0]
    b = asset_features(" si ", "crypto", basis_ref="ev:second")[0]
    assert a.key == b.key
    assert a.surface != b.surface and a.basis_ref != b.basis_ref
    assert a.key != EntityKey("asset:equity", "SI")


@pytest.mark.parametrize(
    ("first", "second", "market", "shared"),
    [
        ("XAUT", "GOLD", "crypto", "GOLD"),
        ("SKHX", "SKHY", "equity", "SKHY"),
        ("SIUSDT", "$SI", "crypto", "SI"),
        ("xyz:GOLD", "GOLD", "commodity", "GOLD"),
    ],
)
def test_underlying_issuer_venue_and_pair_relations_only_expand_candidates(
    first: str, second: str, market: str, shared: str
) -> None:
    assert asset_features(first, market)[0].key != asset_features(second, market)[0].key
    assert shared in asset_retrieval_symbols(first, market) & asset_retrieval_symbols(second, market)
    assert all(feature.key != asset_features(second, market)[0].key for feature in asset_features(first, market)[1:])


def test_stored_asset_codes_expand_catalogue_aliases_on_the_query_side() -> None:
    """Stored retrieval codes carry no alias (#771): a tag spelled XAU or XAUT relates to a GOLD query."""

    assert stored_asset_codes({"GOLD"}) == ("GOLD", "XAU", "XAUT")
    assert stored_asset_codes(asset_retrieval_symbols("CL", "commodity")) == ("BRENTOIL", "CL", "OIL", "USOIL", "WTI")
    assert stored_asset_codes(()) == ()
    # Aliases resolve one way: a query for the alias itself does not reach its target's other aliases.
    assert stored_asset_codes({"XAUT"}) == ("XAUT",)


def test_an_unspecified_chain_cannot_authorize_address_case_folding() -> None:
    address = "solana:AbCdEFGh123456789"
    assert asset_retrieval_symbols(address, "crypto") == {address}
    assert asset_features(address, "crypto")[0].key != asset_features(address.lower(), "crypto")[0].key
    assert identity_value(" " + address + " ") == address


@pytest.mark.parametrize(("pair", "base", "partial"), [("ABCFDUSD", "ABC", "ABCFD"), ("BTCBUSD", "BTC", "BTCB")])
def test_quote_pair_feature_uses_only_the_first_valid_suffix(pair: str, base: str, partial: str) -> None:
    symbols = asset_retrieval_symbols(pair, "crypto")
    assert base in symbols and partial not in symbols


def test_literal_cashtags_stop_at_cjk_and_punctuation_and_exclude_dollar_amounts() -> None:
    assert source_asset_symbols("Aster上线$SI现货，$SI/USDT、$BRK.B。Cost $6B or $50; $BTC.") == (
        "SI",
        "BRK.B",
        "BTC",
    )


def test_scoped_provider_tags_need_visible_ticker_or_existing_commodity_evidence() -> None:
    assert source_mentions_asset("NVDA", "equity", "英伟达$NVDA发布新品")
    assert source_mentions_asset("XAUT", "crypto", "Gold prices increase.")
    assert not source_mentions_asset("BETA", "equity", "NEAR withdrawals resume.")
    assert not source_mentions_asset("SI", "crypto", "SILVER prices increase.")
    assert not source_mentions_asset("solana:AbC123", "crypto", "solana:abc123")
