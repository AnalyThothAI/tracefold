from __future__ import annotations

from scripts.relabel_trading_price_paths import _audit_v1_path


def _row() -> dict[str, object]:
    return {
        "case_id": "case-1",
        "axis": "source",
        "horizon_seconds": 900,
        "old_status": "ok",
        "old_return_bps": "0",
        "source_observed_at_ms": 60_001,
        "decided_at_ms": None,
        "target_selection": {"instrument": {"native_symbol": "BTCUSDT", "environment": "demo"}},
    }


def _path() -> dict[str, object]:
    return {
        "case_id": "case-1",
        "axis": "source",
        "horizon_seconds": 900,
        "version": "price_path_v1",
        "label_version": "price_path_v1",
        "status": "ok",
        "axis_anchor_ms": 60_001,
        "target_ms": 960_001,
        "start_close_at_ms": 120_000,
        "end_close_at_ms": 1_020_000,
        "start_price": "100",
        "end_price": "100",
        "return_bps": "0",
        "source_identity": "binance_public_v1",
        "source_version": "binance_public_v1",
        "unit_definition": "native_quote_v1",
        "measure": "underlying_close_to_close_gross",
        "execution_claim": False,
        "costs_included": False,
        "market_status": "ok",
        "received_at_ms": 1_020_100,
        "request_receipts": [{"native_symbol": "BTCUSDT", "endpoint": "/fapi/v1/klines"}],
    }


def test_aligned_v1_endpoints_certify_only_the_gross_endpoint_label() -> None:
    audit = _audit_v1_path(_row(), _path())

    assert audit["status"] == "ok"
    assert audit["return_bps"] == "0"
    assert audit["historical_quality"] == "verified_endpoint_only"
    assert audit["full_path_quality"] == "unknown"
    assert audit["data_environment"] == "live"


def test_single_reused_endpoint_is_named_as_a_historical_mismatch() -> None:
    old = _path()
    old["start_close_at_ms"] = old["end_close_at_ms"]

    assert _audit_v1_path(_row(), old) == {
        "status": "missing",
        "reason": "historical_endpoint_mismatch",
        "historical_quality": "unverifiable",
    }


def test_missing_v1_archive_stays_unverifiable() -> None:
    assert _audit_v1_path(_row(), None)["reason"] == "historical_v1_archive_missing"


def test_partial_window_with_both_endpoints_is_valid_for_endpoint_only_return() -> None:
    old = _path()
    old["market_status"] = "partial"

    assert _audit_v1_path(_row(), old)["status"] == "ok"


def test_malformed_receipt_cannot_certify_historical_market_identity() -> None:
    old = _path()
    old["request_receipts"] = [None]

    assert _audit_v1_path(_row(), old)["reason"] == "historical_market_identity_unverified"
