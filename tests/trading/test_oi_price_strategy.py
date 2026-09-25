"""The v4 plan constructor uses public source facts and complete closed bars."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from tracefold.trading.engine.features import catalyst_text_values
from tracefold.trading.engine.plans import build_entry_plans, directed_cross


def _bars(last_close: str = "100") -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = [
        {"event_at_ms": (index + 1) * 60_000, "close": "100", "high": "101", "low": "99"} for index in range(15)
    ]
    rows.append({"event_at_ms": 16 * 60_000, "close": last_close, "high": "103", "low": "98"})
    return tuple(rows)


def _plans(*, source: dict[str, object], bars: tuple[dict[str, object], ...] | None = None):
    return build_entry_plans(
        asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_revision="source-1",
        source_fact=source,
        source_first_visible_at_ms=930_000,
        root_expires_at_ms=1_500_000,
        perp_rows=_bars() if bars is None else bars,
    )


@pytest.mark.parametrize("kind", ["oi", "catalyst"])
def test_event_plans_use_closed_bars_and_atr_for_both_sources(kind: str) -> None:
    source = (
        {"kind": "oi", "oi_change_bps": -400, "measurement_definition": "exchange-open-interest-v1"}
        if kind == "oi"
        else {"kind": "catalyst", "headline": "A visible announcement"}
    )
    plans = _plans(source=source)
    assert len(plans) == 4
    assert {plan.side for plan in plans if plan.kind == "immediate_entry_v1"} == {"long", "short"}
    assert {plan.side for plan in plans if plan.kind == "closed_bar_cross_v1"} == {"long", "short"}
    assert all(plan.exit_plan.take_profit_bps == 2 * plan.exit_plan.stop_distance_bps for plan in plans)
    assert all(plan.exit_plan.max_holding_seconds == 14_400 for plan in plans)


def test_existing_cross_is_not_reused_as_a_future_watch() -> None:
    source = {"kind": "oi", "oi_change_bps": 400, "measurement_definition": "exchange-open-interest-v1"}
    plans = _plans(source=source, bars=_bars("102"))
    assert {plan.side for plan in plans if plan.kind == "closed_bar_cross_v1"} == {"short"}
    assert directed_cross(side="long", previous=Decimal(100), current=Decimal(102), level=Decimal(101))


def test_incomplete_or_gapped_bars_create_no_plan() -> None:
    source = {"kind": "oi", "oi_change_bps": 100, "measurement_definition": "exchange-open-interest-v1"}
    assert _plans(source=source, bars=_bars()[:4]) == ()
    rows = list(_bars())
    rows[8] = {**rows[8], "event_at_ms": 1_000_000}
    assert _plans(source=source, bars=tuple(rows)) == ()


def test_missing_oi_definition_creates_no_plan() -> None:
    assert _plans(source={"kind": "oi", "oi_change_bps": 100}) == ()


@pytest.mark.parametrize("text", [{"headline": "A reported announcement"}, {"why": "A reported explanation"}])
def test_public_catalyst_text_qualifies_source(text: dict[str, str]) -> None:
    assert _plans(source={"kind": "catalyst", **text})


@pytest.mark.parametrize(
    "text",
    [
        {},
        {"headline_zh": "News internal text"},
        {"title": "Provider title"},
        {"why_zh": "News internal explanation"},
        {"headline": "", "why": " \t\n"},
        {"headline": 123, "why": True},
        {"headline": None, "why": {"text": "not a string"}},
        {"headline": " ", "why": None, "headline_zh": "Must not rescue the public text"},
    ],
)
def test_missing_public_text_cannot_be_rescued_by_alias(text: dict[str, object]) -> None:
    source = {"kind": "catalyst", **text}
    before = deepcopy(source)
    assert _plans(source=source) == ()
    assert source == before


def test_public_text_projection_preserves_verbatim_values_without_aliases() -> None:
    source = {
        "kind": "catalyst",
        "headline": "  Public headline\n",
        "why": "Public explanation",
        "title": "Different provider title",
        "headline_zh": "Different internal headline",
        "why_zh": "Different internal explanation",
    }
    before = deepcopy(source)
    assert catalyst_text_values(source) == {"headline": source["headline"], "why": source["why"]}
    assert source == before
    assert catalyst_text_values({**source, "kind": "oi"}) == {}
