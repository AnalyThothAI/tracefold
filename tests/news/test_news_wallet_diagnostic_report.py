"""The `news wallets` report's own arithmetic, without a database (#649 §7.2).

The five sections exist to keep four different situations apart, so what is checked here is that they
stay apart: a roster that cannot reach either threshold reads differently from one whose addresses
are still warming up, lifetime totals are not folded into the window, and units are never added.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tracefold.app.cli.commands.news_wallets import (
    _flow_coverage,
    _roster_funnel,
    _send_queue,
    _triggerability,
)
from tracefold.platform.config.models import NewsChainTapeSettings

NOW = 1_900_000_000_000
HALF_HOUR = 1_800_000


def _member(
    *, wallet: str, quality: int | None, whale: int | None, closed: int, factor: float | None, monitoring: int | None
) -> dict[str, Any]:
    return {
        "roster_version": 203,
        "taken_at_ms": NOW - 3_600_000,
        "wallet": wallet,
        "handle": wallet,
        "closed_trades": closed,
        "profit_factor": factor,
        "rank_quality": quality,
        "rank_whale": whale,
        "known_at_ms": NOW - 7 * 86_400_000,
        "monitoring_from_ms": monitoring,
        "provider": "robinhoodtrenches",
    }


def _settings(**roster: Any) -> NewsChainTapeSettings:
    return NewsChainTapeSettings(roster=roster or {})


def test_the_roster_funnel_separates_the_last_attempt_from_the_last_complete_list() -> None:
    """A throttled hour is not a fresh list, and the report has to be able to say so."""

    members = [
        _member(wallet="a", quality=1, whale=1, closed=40, factor=2.5, monitoring=NOW - 86_400_000),
        _member(wallet="b", quality=None, whale=2, closed=40, factor=None, monitoring=NOW - 86_400_000),
        _member(wallet="c", quality=None, whale=3, closed=2, factor=None, monitoring=None),
    ]
    state = {
        "roster_last_attempt_at_ms": NOW,
        "roster_last_success_at_ms": NOW - 5 * 3_600_000,
        "roster_last_error": "robinhoodtrenches:roster_rate_limited",
    }

    report = _roster_funnel(members, state=state, settings=_settings())

    assert report["selected_addresses"] == 3
    # Counted separately, because "selected" and "cleared the closed-trade floor" are two questions.
    assert report["closed_trades_at_or_above_floor"] == 2
    assert (report["profit_factor_known"], report["profit_factor_unknown"]) == (1, 1)
    assert (report["quality"], report["whale"]) == (1, 3)
    assert report["window"] == "30d"
    assert report["refresh_last_attempt_at_ms"] > report["refresh_last_success_at_ms"]
    assert report["refresh_last_error"] == "robinhoodtrenches:roster_rate_limited"


def test_a_quality_pool_below_both_thresholds_reports_how_many_are_missing() -> None:
    """Production's 147 watched addresses with one quality address: the rule cannot fire."""

    members = [_member(wallet="a", quality=1, whale=1, closed=40, factor=2.5, monitoring=NOW - 86_400_000)]
    members += [
        _member(wallet=f"w{index}", quality=None, whale=index, closed=40, factor=None, monitoring=None)
        for index in range(2, 148)
    ]

    report = _triggerability(members, state={"scanned_at_ms": NOW}, settings=_settings(), now_ms=NOW)

    fast, slow = report["windows"]
    assert (fast["required_n"], fast["monitoring_supported"], fast["short_by"]) == (3, 1, 2)
    assert (slow["required_n"], slow["monitoring_supported"], slow["short_by"]) == (5, 1, 4)
    assert (fast["satisfiable"], slow["satisfiable"]) == (False, False)
    assert fast["quality_addresses"] == 1
    assert report["measured_against"] == "scanned_at_ms"


def test_an_address_still_warming_up_is_short_support_rather_than_absent() -> None:
    """A quality address monitored for ten minutes supports the 5m window and not the 30m one."""

    members = [
        _member(wallet=f"q{index}", quality=index, whale=None, closed=40, factor=2.5, monitoring=NOW - 600_000)
        for index in range(1, 6)
    ]

    report = _triggerability(members, state={"scanned_at_ms": NOW}, settings=_settings(), now_ms=NOW)

    fast, slow = report["windows"]
    assert (fast["quality_addresses"], fast["monitoring_supported"]) == (5, 5)
    assert (slow["quality_addresses"], slow["monitoring_supported"]) == (5, 0)
    assert (fast["satisfiable"], slow["satisfiable"]) == (True, False)


def test_the_host_clock_is_only_the_fallback_when_nothing_has_been_scanned() -> None:
    members = [_member(wallet="a", quality=1, whale=None, closed=40, factor=2.5, monitoring=NOW - HALF_HOUR)]

    report = _triggerability(members, state={}, settings=_settings(), now_ms=NOW)

    assert report["measured_against"] == "host_clock"
    assert report["measured_at_ms"] == NOW


def test_lifetime_discard_totals_are_never_presented_inside_the_window() -> None:
    """`ignored_inbound_total` accumulates for the life of the row; it is not a 24-hour rate."""

    coverage = {"fills": 1_098, "receipts": 900, "wallets": 120, "tokens": 1_160, "underived": 0}
    state = {"ignored_inbound_total": 55_000, "unknown_total": 900, "scanned_at_ms": NOW, "last_outcome": "success"}

    report = _flow_coverage(coverage, [{"reason": "conditions_not_met", "fills": 3_683}], state=state)

    assert report["counted_in_window"] == coverage
    assert report["lifetime_totals"] == {"ignored_inbound_total": 55_000, "unknown_total": 900}
    assert "ignored_inbound_total" not in report["counted_in_window"]
    assert report["collection"]["last_outcome"] == "success"


def test_the_send_queue_reports_the_head_age_and_its_due_time() -> None:
    queue = [
        {
            "delivery_key": "wallet-1",
            "market_kind": "wallet",
            "state": "pending",
            "attempts": 0,
            "next_attempt_at_ms": NOW + 5_000,
            "error": "evidence_not_derived",
            "created_at_ms": NOW - 20_000,
        },
        {
            "delivery_key": "oi-1",
            "market_kind": "oi",
            "state": "pending",
            "attempts": 0,
            "next_attempt_at_ms": NOW,
            "error": None,
            "created_at_ms": NOW - 1_000,
        },
    ]

    report = _send_queue(queue, now_ms=NOW)

    assert report["waiting"] == 2
    assert report["head"]["delivery_key"] == "wallet-1"
    assert (report["head"]["waiting_ms"], report["head"]["due_in_ms"]) == (20_000, 5_000)


def test_an_empty_deployment_reports_zeroes_rather_than_failing() -> None:
    assert _roster_funnel([], state={}, settings=_settings())["selected_addresses"] == 0
    assert _send_queue([], now_ms=NOW) == {"waiting": 0, "head": None, "queue": []}
    triggerability = _triggerability([], state={}, settings=_settings(), now_ms=NOW)
    assert [window["short_by"] for window in triggerability["windows"]] == [3, 5]
    assert triggerability["min_net_buy_usd"] == str(Decimal("1000"))
