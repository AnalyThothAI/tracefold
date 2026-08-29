from __future__ import annotations

from tracefold.trading.catalog import (
    VenueInstrumentCatalogEntryV1,
    build_venue_catalog_snapshot,
)
from tracefold.trading.contracts import canonical_sha256


def _row(instrument_id: str, *, raw: str, error: str | None = None) -> VenueInstrumentCatalogEntryV1:
    return VenueInstrumentCatalogEntryV1(
        provider_instrument_id=instrument_id,
        provider_symbol=instrument_id,
        venue="binance.usdm",
        canonical_asset=None if error else "BTC",
        canonical_namespace=None if error else "native",
        product_kind="unknown" if error else "linear_perpetual",
        active=error is None,
        settlement_asset=None if error else "USDT",
        margin_asset=None if error else "USDT",
        raw_metadata_sha256=canonical_sha256({"raw": raw}),
        normalization_error=error,
    )


def test_catalog_digest_is_order_independent_and_preserves_every_provider_row() -> None:
    rows = (
        _row("BTCUSDT", raw="first"),
        _row("BTCUSDT", raw="duplicate"),
        _row("unknown:1", raw="malformed", error="provider_instrument_identity_missing"),
    )

    first = build_venue_catalog_snapshot(
        binding="BINANCE_USDM", captured_at_ms=1, stale_after_ms=21_600_000, instruments=rows
    )
    second = build_venue_catalog_snapshot(
        binding="BINANCE_USDM", captured_at_ms=1, stale_after_ms=21_600_000, instruments=tuple(reversed(rows))
    )

    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.provider_instrument_count == 3
    assert first.normalised_count == 2
    assert [row.raw_metadata_sha256 for row in first.instruments].count(rows[0].raw_metadata_sha256) == 1
    assert first.resolve("BTC") is not None
