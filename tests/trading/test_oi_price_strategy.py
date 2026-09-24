from decimal import Decimal

import pytest

from tracefold.trading.engine.brief import build_brief
from tracefold.trading.engine.strategy import build_oi_price_candidates


def _bars(last_close: str = "102") -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = [
        {"event_at_ms": (index + 1) * 60_000, "close": "100", "high": "101", "low": "99"} for index in range(15)
    ]
    rows.append({"event_at_ms": 16 * 60_000, "close": last_close, "high": "103", "low": "98"})
    return tuple(rows)


def _candidates(*, bars=None, oi_change=400, market_oi=True):
    return build_oi_price_candidates(
        asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_fact={"kind": "oi", "oi_change_bps": oi_change, "oi_value_usd": 1_000_000},
        perp_rows=bars or _bars(),
        market_oi_available=market_oi,
    )


def test_oi_price_family_freezes_level_entry_and_exit() -> None:
    long, short = _candidates()
    assert long.strategy_version == "oi_price_confirmation_v1"
    assert long.entry_level == Decimal(101)
    assert long.entry_observed == Decimal(102)
    assert long.entry_ready and not short.entry_ready
    assert short.watch_eligible and short.entry_level == Decimal(99)
    assert long.exit_plan.stop_distance_bps == 200
    assert long.exit_plan.take_profit_bps == 400
    assert _candidates() == (long, short)


@pytest.mark.parametrize("oi_change,market_oi", [(-400, True), (400, False)])
def test_oi_fall_or_missing_quantity_cannot_enter(oi_change: int, market_oi: bool) -> None:
    assert all(
        not candidate.entry_ready and not candidate.watch_eligible
        for candidate in _candidates(oi_change=oi_change, market_oi=market_oi)
    )


def test_price_comparison_direction_is_not_reversed() -> None:
    long, short = _candidates(bars=_bars("98"))
    assert not long.entry_ready and short.entry_ready


def test_incomplete_closed_history_is_rejected() -> None:
    with pytest.raises(ValueError, match="strategy_closed_bar_history_incomplete"):
        _candidates(bars=_bars()[:4])


def test_catalyst_can_be_assessed_without_an_oi_entry_candidate() -> None:
    candidates = build_oi_price_candidates(
        asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_fact={"kind": "catalyst"},
        perp_rows=_bars(),
        market_oi_available=False,
    )
    assert candidates == ()
    brief = build_brief(
        target_asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_fact={"kind": "catalyst"},
        source_history=(),
        evidence={},
        features={},
        candidates=candidates,
    )
    assert '"candidate_menu":[]' in brief.text
