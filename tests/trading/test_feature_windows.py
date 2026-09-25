"""Each market feature needs its own complete, contiguous observation window."""

import pytest

from tracefold.trading.engine.features import PROFILE_VERSION, extract_features
from tracefold.trading.engine.marketdata import MarketDataResult


def _frame(rows: tuple[dict[str, str | int], ...]) -> MarketDataResult:
    return MarketDataResult(
        status="ok" if rows else "missing",
        payload=rows,
        schema_version="fixture",
        source_version="fixture",
        unit_definition="fixture",
        source_identity="fixture",
        event_start_ms=int(rows[0]["event_at_ms"]) if rows else None,
        event_end_ms=int(rows[-1]["event_at_ms"]) if rows else None,
        received_at_ms=10_000_000 if rows else None,
        missing_reasons=(),
        request_receipts=(),
    )


def _features(rows: tuple[dict[str, str | int], ...]) -> dict:
    missing = _frame(())
    return extract_features(
        {
            "perp_bars": _frame(rows),
            "spot_bars": missing,
            "market_bars": missing,
            "open_interest": missing,
            "funding_basis": missing,
        },
        {"kind": "oi"},
    )


def _rows(count: int) -> tuple[dict[str, str | int], ...]:
    return tuple(
        {
            "event_at_ms": (index + 1) * 60_000,
            "close": str(100 + index),
            "quote_volume": "10",
            "taker_buy_quote_volume": "4",
        }
        for index in range(count)
    )


@pytest.mark.parametrize("count", [16, 59, 60, 61])
def test_feature_windows_do_not_label_short_history_as_60_minutes(count: int) -> None:
    features = _features(_rows(count))
    assert features["profile_version"] == PROFILE_VERSION
    assert features["perp_return_15m_bps"] is not None
    assert features["perp_taker_buy_share_15m_bps"] == "4000"
    assert (features["perp_taker_buy_share_60m_bps"] is not None) == (count >= 60)
    assert (features["perp_return_60m_bps"] is not None) == (count >= 61)


def test_gap_or_invalid_volume_cannot_produce_complete_feature() -> None:
    rows = list(_rows(61))
    rows[-3] = {**rows[-3], "event_at_ms": int(rows[-3]["event_at_ms"]) + 1}
    features = _features(tuple(rows))
    assert features["perp_return_60m_bps"] is None
    assert features["perp_taker_buy_share_60m_bps"] is None

    rows = list(_rows(60))
    rows[-1] = {**rows[-1], "quote_volume": "0", "taker_buy_quote_volume": "1"}
    assert _features(tuple(rows))["perp_taker_buy_share_60m_bps"] is None

    rows[-1] = {**rows[-1], "quote_volume": "NaN", "taker_buy_quote_volume": "0"}
    assert _features(tuple(rows))["perp_taker_buy_share_60m_bps"] is None
