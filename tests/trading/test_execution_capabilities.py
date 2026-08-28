from __future__ import annotations

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


def test_inactive_provider_only_rows_are_outside_the_frozen_active_candidate_union() -> None:
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
    assert "OLDUSDT-PERP.BINANCE" not in snapshot.excluded
    assert snapshot.excluded["XRPUSDT-PERP.BINANCE"].reason == "not_active"
