"""The frozen instrument universe: what may carry capital, and what each exclusion actually proves.

#331 §3 closes four contract holes that let an instrument acquire capital authority nobody granted:
the News row's venue is verified rather than trusted from the caller's name, increments must parse as
positive Decimals, a conflicting duplicate fails the whole refresh instead of being resolved by row
order, and `supports_native_stop` has to be produced by the provider.
"""

from __future__ import annotations

import pytest

from tracefold.trading import ProviderInstrumentCandidateV1
from tracefold.trading.capabilities import build_execution_capability_snapshot


def _news(symbol: str, base: str) -> dict[str, object]:
    return {
        "venue": "binance.perp",
        "venue_symbol": symbol,
        "base_symbol": base,
        "instrument_class": "crypto",
        "quote_asset": "USDT",
        "status": "trading",
        "last_seen_ms": 1_900_000_000_000,
    }


def _provider(symbol: str, base: str) -> ProviderInstrumentCandidateV1:
    return ProviderInstrumentCandidateV1(
        instrument_id=f"{symbol}-PERP.BINANCE",
        native_symbol=symbol,
        base_currency=base,
        quote_currency="USDT",
        active=True,
        linear=True,
        inverse=False,
        perpetual=True,
        price_precision=2,
        size_precision=3,
        price_increment="0.01",
        size_increment="0.001",
        min_quantity="0.001",
        min_notional="5",
        supports_native_stop=True,
    )


def test_snapshot_partitions_the_full_provider_news_union_without_a_target_allowlist() -> None:
    snapshot = build_execution_capability_snapshot(
        news_rows=[_news("XRPUSDT", "XRP"), _news("ETHUSDT", "ETH"), _news("SOLUSDT", "SOL")],
        provider_rows=[_provider("DOGEUSDT", "DOGE"), _provider("SOLUSDT", "SOL"), _provider("ETHUSDT", "ETH")],
        app_revision="revision-1",
        app_image_digest="image-1",
        nautilus_wheel_identity="wheel-1",
    )

    assert set(snapshot.included) == {
        "ETHUSDT-PERP.BINANCE",
        "SOLUSDT-PERP.BINANCE",
    }
    assert snapshot.included["ETHUSDT-PERP.BINANCE"].quote_currency == "USDT"
    assert snapshot.included["ETHUSDT-PERP.BINANCE"].price_increment == "0.01"
    assert snapshot.included["ETHUSDT-PERP.BINANCE"].size_increment == "0.001"
    assert {key: row.reason for key, row in snapshot.excluded.items()} == {
        "DOGEUSDT-PERP.BINANCE": "missing_news_projection",
        "XRPUSDT-PERP.BINANCE": "missing_provider_instrument",
    }
    assert set(snapshot.included) | set(snapshot.excluded) == {
        "DOGEUSDT-PERP.BINANCE",
        "ETHUSDT-PERP.BINANCE",
        "SOLUSDT-PERP.BINANCE",
        "XRPUSDT-PERP.BINANCE",
    }


def test_snapshot_identity_is_byte_stable_when_both_providers_return_a_different_order() -> None:
    news = [_news("ETHUSDT", "ETH"), _news("SOLUSDT", "SOL")]
    provider = [_provider("ETHUSDT", "ETH"), _provider("SOLUSDT", "SOL")]
    kwargs = {
        "app_revision": "revision-1",
        "app_image_digest": "image-1",
        "nautilus_wheel_identity": "wheel-1",
    }

    forward = build_execution_capability_snapshot(news_rows=news, provider_rows=provider, **kwargs)
    reversed_rows = build_execution_capability_snapshot(
        news_rows=list(reversed(news)),
        provider_rows=list(reversed(provider)),
        **kwargs,
    )

    assert reversed_rows == forward
    assert reversed_rows.snapshot_sha256 == forward.snapshot_sha256


def test_inactive_provider_rows_are_in_the_frozen_candidate_partition() -> None:
    inactive = _provider("OLDUSDT", "OLD").model_copy(update={"active": False})
    matching_inactive = _provider("XRPUSDT", "XRP").model_copy(update={"active": False})

    snapshot = build_execution_capability_snapshot(
        news_rows=[_news("ETHUSDT", "ETH"), _news("XRPUSDT", "XRP")],
        provider_rows=[_provider("ETHUSDT", "ETH"), matching_inactive, inactive],
        app_revision="revision-1",
        app_image_digest="image-1",
        nautilus_wheel_identity="wheel-1",
    )

    assert "OLDUSDT-PERP.BINANCE" not in snapshot.included
    assert snapshot.excluded["OLDUSDT-PERP.BINANCE"].reason == "missing_news_projection"
    assert snapshot.excluded["XRPUSDT-PERP.BINANCE"].reason == "not_active"


def test_non_stablecoin_contract_is_mechanically_excluded_from_usdm_execution() -> None:
    provider = _provider("ETHBTC", "ETH").model_copy(update={"quote_currency": "BTC"})
    news = _news("ETHBTC", "ETH")
    news["quote_asset"] = "BTC"

    snapshot = build_execution_capability_snapshot(
        news_rows=[news, _news("ETHUSDT", "ETH")],
        provider_rows=[provider, _provider("ETHUSDT", "ETH")],
        app_revision="revision-1",
        app_image_digest="image-1",
        nautilus_wheel_identity="wheel-1",
    )

    assert set(snapshot.included) == {"ETHUSDT-PERP.BINANCE"}
    assert snapshot.excluded["ETHBTC-PERP.BINANCE"].reason == "unsupported_quote"


def _kwargs() -> dict[str, str]:
    return {
        "app_revision": "revision-1",
        "app_image_digest": "image-1",
        "nautilus_wheel_identity": "wheel-1",
    }


def test_a_news_row_from_another_venue_can_never_grant_binance_demo_authority() -> None:
    """#331 comment F2P 6. The caller's name is not a contract; the row's own venue is."""

    news = _news("ETHUSDT", "ETH")
    news["venue"] = "hl.perp"

    snapshot = build_execution_capability_snapshot(
        news_rows=[news, _news("SOLUSDT", "SOL")],
        provider_rows=[_provider("ETHUSDT", "ETH"), _provider("SOLUSDT", "SOL")],
        **_kwargs(),
    )

    assert set(snapshot.included) == {"SOLUSDT-PERP.BINANCE"}
    assert snapshot.excluded["ETHUSDT-PERP.BINANCE"].reason == "not_binance_perp_venue"


@pytest.mark.parametrize("increment", ["0", "-0.01", "", "abc"])
def test_a_non_positive_or_unparseable_increment_is_refused_before_it_can_size_anything(increment: str) -> None:
    """#331 comment F2P 7. Quantity is `notional / price` floored to the lot size."""

    base = _provider("ETHUSDT", "ETH").model_dump()
    with pytest.raises(ValueError, match="execution_capability_size_increment_invalid"):
        ProviderInstrumentCandidateV1.model_validate(base | {"size_increment": increment})
    with pytest.raises(ValueError, match="execution_capability_price_increment_invalid"):
        ProviderInstrumentCandidateV1.model_validate(base | {"price_increment": increment})


def test_a_negative_minimum_is_refused_and_an_absent_one_is_allowed() -> None:
    base = _provider("ETHUSDT", "ETH").model_dump()
    assert ProviderInstrumentCandidateV1.model_validate(base | {"min_quantity": None, "min_notional": None})
    with pytest.raises(ValueError, match="execution_capability_min_quantity_invalid"):
        ProviderInstrumentCandidateV1.model_validate(base | {"min_quantity": "-1"})


def test_identical_duplicates_collapse_to_one_byte_stable_snapshot() -> None:
    """#331 comment F2P 8. Two identical rows are one instrument, not a coin toss over row order."""

    once = build_execution_capability_snapshot(
        news_rows=[_news("ETHUSDT", "ETH")],
        provider_rows=[_provider("ETHUSDT", "ETH")],
        **_kwargs(),
    )
    twice = build_execution_capability_snapshot(
        news_rows=[_news("ETHUSDT", "ETH"), _news("ETHUSDT", "ETH")],
        provider_rows=[_provider("ETHUSDT", "ETH"), _provider("ETHUSDT", "ETH")],
        **_kwargs(),
    )
    assert twice.included == once.included


def test_a_conflicting_duplicate_fails_the_refresh_rather_than_picking_one() -> None:
    """The previous `setdefault` made the snapshot digest a function of query order."""

    conflicting = _news("ETHUSDT", "ETH")
    conflicting["base_symbol"] = "ETHW"

    with pytest.raises(ValueError, match="execution_capability_news_row_conflict"):
        build_execution_capability_snapshot(
            news_rows=[_news("ETHUSDT", "ETH"), conflicting],
            provider_rows=[_provider("ETHUSDT", "ETH")],
            **_kwargs(),
        )

    with pytest.raises(ValueError, match="execution_capability_provider_row_conflict"):
        build_execution_capability_snapshot(
            news_rows=[_news("ETHUSDT", "ETH")],
            provider_rows=[
                _provider("ETHUSDT", "ETH"),
                _provider("ETHUSDT", "ETH").model_copy(update={"price_precision": 8}),
            ],
            **_kwargs(),
        )


def test_an_instrument_with_no_proven_native_stop_is_excluded_rather_than_defaulted_in() -> None:
    """#331 comment F2P 9. A native stop is the only protection this lane has."""

    unproven = _provider("ETHUSDT", "ETH").model_copy(update={"supports_native_stop": False})

    snapshot = build_execution_capability_snapshot(
        news_rows=[_news("ETHUSDT", "ETH"), _news("SOLUSDT", "SOL")],
        provider_rows=[unproven, _provider("SOLUSDT", "SOL")],
        **_kwargs(),
    )

    assert set(snapshot.included) == {"SOLUSDT-PERP.BINANCE"}
    assert snapshot.excluded["ETHUSDT-PERP.BINANCE"].reason == "native_stop_unsupported"


def test_one_issuer_listed_against_two_quotes_resolves_deterministically_to_usdt() -> None:
    """The lane resolves a Case's instrument from this snapshot, so the choice may not be arbitrary."""

    usdc_news = _news("ETHUSDC", "ETH")
    usdc_news["quote_asset"] = "USDC"
    usdc_provider = _provider("ETHUSDC", "ETH").model_copy(update={"quote_currency": "USDC"})

    snapshot = build_execution_capability_snapshot(
        news_rows=[usdc_news, _news("ETHUSDT", "ETH")],
        provider_rows=[usdc_provider, _provider("ETHUSDT", "ETH")],
        **_kwargs(),
    )

    resolved = snapshot.resolve("crypto:ETH")
    assert resolved is not None and resolved.instrument_id == "ETHUSDT-PERP.BINANCE"
    assert snapshot.resolve("crypto:NOPE") is None
    assert snapshot.resolve("") is None
