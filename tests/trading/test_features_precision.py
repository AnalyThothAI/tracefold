"""Small signed rates and unavailable frames retain distinct frozen facts."""

from tracefold.trading.engine.features import extract_features, freeze_features
from tracefold.trading.engine.marketdata import MarketDataResult


def _result(*, status: str = "ok", payload: tuple[dict, ...] = ()) -> MarketDataResult:
    return MarketDataResult(
        status=status,
        payload=payload,
        schema_version="fixture",
        source_version="fixture",
        unit_definition="fixture",
        source_identity="binance-fixture",
        event_start_ms=500 if payload else None,
        event_end_ms=500 if payload else None,
        received_at_ms=600 if payload else None,
        missing_reasons=() if payload else ("missing",),
        request_receipts=(),
    )


def test_signed_half_basis_point_and_real_zero_survive_feature_freeze() -> None:
    missing = _result(status="missing")
    source = {"kind": "oi", "oi_change_bps": 0, "measurement_definition": "exchange-oi", "source_recorded_at_ms": 500}
    for rate, mark, expected in (
        ("0.00005", "100.005", "0.50000"),
        ("-0.00005", "99.995", "-0.50000"),
        ("0", "100", "0"),
    ):
        results = {name: missing for name in ("perp_bars", "spot_bars", "market_bars", "open_interest")}
        results["funding_basis"] = _result(
            payload=({"mark_price": mark, "index_price": "100", "last_funding_rate": rate},)
        )
        features = extract_features(results, source)
        target = expected if rate != "0" else "0"
        assert features["funding_rate_bps"] == target
        assert features["premium_bps"] == target
        assert features["source_oi_change_bps"] == 0
        frozen = freeze_features(
            snapshot_ref="a" * 64,
            knowledge_cutoff_ms=1_000,
            data_environment="live",
            source_first_visible_at_ms=600,
            source_fact=source,
            results=results,
            features=features,
        )
        by_id = {item.feature_id: item for item in frozen.values}
        assert by_id["funding_rate_bps"].status == "ok"
        assert by_id["funding_rate_bps"].value == features["funding_rate_bps"]
        assert by_id["binance_open_interest_quantity"].status == "missing"
        assert by_id["binance_open_interest_quantity"].value is None
        assert by_id["source_oi_change_bps"].value == "0"
