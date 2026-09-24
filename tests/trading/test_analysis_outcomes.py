from tracefold.trading.engine.outcomes import price_path_label


def test_opportunity_label_never_uses_a_price_before_its_axis() -> None:
    bars = (
        {"event_at_ms": 60_000, "close": "100"},
        {"event_at_ms": 120_000, "close": "110"},
        {"event_at_ms": 180_000, "close": "121"},
    )
    label = price_path_label(bars, anchor_ms=61_000, horizon_seconds=60)
    assert label["status"] == "ok"
    assert label["start_close_at_ms"] == 120_000
    assert label["end_close_at_ms"] == 180_000
    assert label["return_bps"] == "1000.0"
    assert label["execution_claim"] is False


def test_missing_endpoint_is_not_a_zero_return() -> None:
    label = price_path_label(({"event_at_ms": 60_000, "close": "100"},), anchor_ms=60_000, horizon_seconds=60)
    assert label["status"] == "missing"
    assert "return_bps" not in label


def test_partial_or_single_terminal_bar_never_becomes_ok_zero() -> None:
    terminal_only = ({"event_at_ms": 900_000, "close": "100"},)
    assert price_path_label(terminal_only, anchor_ms=0, horizon_seconds=900)["status"] == "missing"
    complete_bars = (
        {"event_at_ms": 60_000, "close": "100"},
        {"event_at_ms": 120_000, "close": "105"},
    )
    assert (
        price_path_label(complete_bars, anchor_ms=0, horizon_seconds=60, market_status="partial")["status"] == "missing"
    )
