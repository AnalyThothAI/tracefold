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
        "unit_definition": "native_quote_v1",
        "market_status": "ok",
        "received_at_ms": 1_020_100,
        "request_receipts": [{"native_symbol": "BTCUSDT", "endpoint": "/fapi/v1/klines"}],
    }


def test_aligned_v1_endpoints_cannot_certify_the_unarchived_bars_or_environment() -> None:
    assert _audit_v1_path(_row(), _path()) == "historical_raw_bars_and_environment_unverified"


def test_single_reused_endpoint_is_named_as_a_historical_mismatch() -> None:
    old = _path()
    old["start_close_at_ms"] = old["end_close_at_ms"]

    assert _audit_v1_path(_row(), old) == "historical_endpoint_mismatch"


def test_missing_v1_archive_stays_unverifiable() -> None:
    assert _audit_v1_path(_row(), None) == "historical_v1_archive_missing"
