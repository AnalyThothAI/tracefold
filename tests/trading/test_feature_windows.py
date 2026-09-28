"""Each market feature needs its own complete, contiguous observation window."""

from dataclasses import replace

import pytest

from tracefold.trading.engine.features import (
    PROFILE_VERSION,
    closed_bar_window,
    extract_features,
    freeze_features,
    price_plan_window,
)
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


def test_partial_early_gap_preserves_only_complete_tail_windows() -> None:
    rows = _rows(241)
    end = int(rows[-1]["event_at_ms"])
    partial = replace(
        _frame(rows),
        status="partial",
        payload=rows[10:],
        event_start_ms=int(rows[10]["event_at_ms"]),
        received_at_ms=20_000_000,
    )
    missing = _frame(())
    results = {name: missing for name in ("spot_bars", "market_bars", "open_interest", "funding_basis")}
    results["perp_bars"] = partial
    features = extract_features(results, {"kind": "oi"}, expected_ends={"perp_bars": end}, cutoff_ms=21_000_000)
    assert partial.status == "partial"
    assert features["perp_return_15m_bps"] is not None
    assert features["perp_return_60m_bps"] is not None
    assert features["perp_return_240m_bps"] is None
    frozen = freeze_features(
        snapshot_ref="a" * 64,
        knowledge_cutoff_ms=21_000_000,
        data_environment="live",
        source_first_visible_at_ms=1,
        source_fact={"kind": "oi", "source_recorded_at_ms": 1},
        results=results,
        features=features,
    )
    values = {value.feature_id: value for value in frozen.values}
    assert values["perp_return_15m_bps"].status == "ok"
    assert values["perp_return_240m_bps"].status == "missing"


def test_window_rejects_missing_tail_wrong_grid_future_and_identity() -> None:
    rows = _rows(61)
    end = int(rows[-1]["event_at_ms"])
    valid = _frame(rows)
    assert len(closed_bar_window(valid, count=16, end_ms=end, cutoff_ms=20_000_000)) == 16
    assert not closed_bar_window(
        replace(valid, status="partial", payload=rows[:-1], event_end_ms=int(rows[-2]["event_at_ms"])),
        count=16,
        end_ms=end,
    )
    assert not closed_bar_window(valid, count=16, end_ms=end + 60_000)
    assert not closed_bar_window(
        replace(valid, payload=(*rows[:-2], {**rows[-2], "event_at_ms": end - 59_999}, rows[-1])), count=16, end_ms=end
    )
    assert not closed_bar_window(valid, count=16, end_ms=end, cutoff_ms=1)
    assert not closed_bar_window(replace(valid, status="error"), count=16, end_ms=end)
    assert not closed_bar_window(valid, count=16, end_ms=end, unit_definition="different")


def test_plan_window_rejects_invalid_price_even_when_time_grid_is_complete() -> None:
    rows = tuple({**row, "high": "200", "low": "98"} for row in _rows(16))
    end = int(rows[-1]["event_at_ms"])
    assert (
        len(
            price_plan_window(
                _frame(rows), end_ms=end, cutoff_ms=20_000_000, source_identity="fixture", unit_definition="fixture"
            )
        )
        == 16
    )
    malformed = (*rows[:-1], {**rows[-1], "high": "NaN"})
    assert not price_plan_window(
        _frame(malformed), end_ms=end, cutoff_ms=20_000_000, source_identity="fixture", unit_definition="fixture"
    )
