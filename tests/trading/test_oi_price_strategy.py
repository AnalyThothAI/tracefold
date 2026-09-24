"""The initial event setup is shared by catalyst and OI, with fixed bar semantics."""

from decimal import Decimal

import pytest

from tracefold.trading.engine.strategy import build_event_price_candidates, range_cross_side


def _bars(last_close: str = "102") -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = [
        {"event_at_ms": (index + 1) * 60_000, "close": "100", "high": "101", "low": "99"} for index in range(15)
    ]
    rows.append({"event_at_ms": 16 * 60_000, "close": last_close, "high": "103", "low": "98"})
    return tuple(rows)


def _candidates(*, kind: str = "oi", bars=None, visible_at=930_000, oi_change=-400):
    source = (
        {"kind": "oi", "oi_change_bps": oi_change, "measurement_definition": "exchange-open-interest-v1"}
        if kind == "oi"
        else {"kind": "catalyst", "title": "A visible announcement"}
    )
    return build_event_price_candidates(
        asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_fact=source,
        source_first_visible_at_ms=visible_at,
        perp_rows=bars if bars is not None else _bars(),
    )


@pytest.mark.parametrize("kind", ["oi", "catalyst"])
def test_event_setup_uses_prior_15_bars_and_atr14_for_both_sources(kind: str) -> None:
    long, short = _candidates(kind=kind)
    assert long.strategy_version == "event_price_confirmation_v1"
    assert long.entry_level == Decimal(101)
    assert short.entry_level == Decimal(99)
    assert long.previous_close == Decimal(100)
    assert long.entry_ready and not short.entry_ready
    assert long.exit_plan.stop_distance_bps == 393
    assert long.exit_plan.take_profit_bps == 786
    assert long.exit_plan.max_holding_seconds == 14_400


def test_oi_direction_and_current_market_oi_do_not_grant_or_deny_entry() -> None:
    assert _candidates(oi_change=-400)[0].entry_ready
    assert _candidates(oi_change=400)[0].entry_ready


def test_source_visible_after_breakout_cannot_be_retroactively_traded_or_watched() -> None:
    long, short = _candidates(visible_at=970_000)
    assert not long.entry_ready and not short.entry_ready
    assert not long.watch_eligible and not short.watch_eligible
    assert long.strategy_gate_reason == "breakout_precedes_source"


def test_unbroken_range_can_watch_both_directions() -> None:
    long, short = _candidates(bars=_bars("100"))
    assert not long.entry_ready and not short.entry_ready
    assert long.watch_eligible and short.watch_eligible
    assert (
        range_cross_side(previous_close=Decimal(100), close=Decimal(102), upper=Decimal(101), lower=Decimal(99))
        == "long"
    )
    assert (
        range_cross_side(previous_close=Decimal(102), close=Decimal(103), upper=Decimal(101), lower=Decimal(99)) is None
    )


def test_negative_breakout_uses_same_strategy() -> None:
    long, short = _candidates(bars=_bars("98"))
    assert not long.entry_ready and short.entry_ready


def test_incomplete_or_gapped_closed_history_is_rejected() -> None:
    with pytest.raises(ValueError, match="strategy_closed_bar_history_incomplete"):
        _candidates(bars=_bars()[:4])
    rows = list(_bars())
    rows[8] = {**rows[8], "event_at_ms": 1_000_000}
    with pytest.raises(ValueError, match="strategy_closed_bar_gap"):
        _candidates(bars=tuple(rows))


def test_missing_oi_measurement_definition_cannot_create_an_entry_or_watch() -> None:
    candidates = build_event_price_candidates(
        asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_fact={"kind": "oi", "oi_change_bps": 100},
        source_first_visible_at_ms=930_000,
        perp_rows=_bars("100"),
    )
    assert all(not item.entry_ready and not item.watch_eligible for item in candidates)
