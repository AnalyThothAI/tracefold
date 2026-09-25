"""`tracefold news wallets`: why the smart-money alert did or did not fire (#649 §7.2).

Read-only, one bounded window, no provider call and no write. It exists because "there are no alerts"
is at least four different situations and an empty event list is all of them at once:

* the published roster is smaller than the quorum, so the rule is unsatisfiable;
* it is not, but the addresses have not been monitored long enough to fill the window;
* the chain facts were collected and never derived, so the detector has not looked at them;
* an episode and an intent exist and the send queue has not got to them.

Five sections, in the order an operator asks the questions. Counts are per unit and never added
across units: fills, receipts, addresses, tokens, episodes and deliveries are six different things.
Lifetime totals from the tape state row are labelled as lifetime totals rather than presented inside
the 24-hour window.
"""

from __future__ import annotations

import time
from argparse import Namespace
from typing import Any

from tracefold.news.chain_tape.rules import supported_member_count
from tracefold.news.wallet_contracts import NET_BUY_WINDOW_MS
from tracefold.platform.config.loader import load_settings


def handle_wallets(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import repositories

    settings = load_settings(require_ws_token=False)
    chain_tape = settings.news.chain_tape
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - max(1, int(args.hours)) * 3_600_000
    with repositories(settings) as repos, repos.news.wallet_read_snapshot():
        news = repos.news
        members = news.chain_tape_roster_rows()
        tape = news.chain_tape_state()
        coverage = news.wallet_flow_coverage(from_ms=from_ms, to_ms=now_ms)
        derived = news.wallet_derived_reasons(from_ms=from_ms, to_ms=now_ms)
        funnel = news.wallet_episode_funnel(from_ms=from_ms, to_ms=now_ms)
        reasons = news.wallet_episode_reasons(from_ms=from_ms, to_ms=now_ms)
        queue = news.wallet_send_queue(limit=int(args.queue_limit))
    state = dict(tape or {})
    report = {
        "window": {"from_ms": from_ms, "to_ms": now_ms, "hours": int(args.hours)},
        "roster_funnel": _roster_funnel(members, state=state, settings=chain_tape),
        "triggerability": _triggerability(members, state=state, settings=chain_tape, now_ms=now_ms),
        "flow_coverage": _flow_coverage(coverage, derived, state=state),
        "decision_and_delivery": {
            "episodes": funnel,
            "unsent_reasons": reasons,
            "notifications_enabled": chain_tape.notifications_enabled,
        },
        "send_queue": _send_queue(queue, now_ms=now_ms),
    }
    return 0, {"ok": True, "data": report}


def _roster_funnel(members: list[dict[str, Any]], *, state: dict[str, Any], settings: Any) -> dict[str, Any]:
    """The locally published complete source response and its refresh outcome."""
    return {
        "roster_version": 0 if not members else int(members[0]["roster_version"]),
        "taken_at_ms": None if not members else int(members[0]["taken_at_ms"]),
        "window": settings.roster.window,
        "source_addresses": len(members),
        "refresh_last_attempt_at_ms": state.get("roster_last_attempt_at_ms"),
        "refresh_last_success_at_ms": state.get("roster_last_success_at_ms"),
        "refresh_last_error": state.get("roster_last_error"),
        "refresh_next_attempt_at_ms": state.get("roster_next_attempt_at_ms"),
    }


def _triggerability(
    members: list[dict[str, Any]], *, state: dict[str, Any], settings: Any, now_ms: int
) -> dict[str, Any]:
    """Coverage at the committed chain cutoff; collection freshness is separate."""
    reference = state.get("scanned_at_ms")
    required = settings.rules.net_buy_slow_n
    supported = supported_member_count(
        members,
        cutoff_at_ms=reference,
        coverage_from_ms=state.get("coverage_from_ms"),
        gap_at_ms=state.get("gap_at_ms"),
    )
    return {
        "measured_at_ms": reference,
        "measured_against": "scanned_at_ms",
        "collection_lag_ms": None if reference is None else now_ms - reference,
        "min_net_buy_usd": str(settings.rules.min_net_buy_usd),
        "trigger_max_age_s": settings.rules.trigger_max_age_s,
        "window": "30m",
        "window_ms": NET_BUY_WINDOW_MS,
        "required_n": required,
        "roster_addresses": len(members),
        "monitoring_supported": supported,
        "short_by": max(0, required - supported),
        "satisfiable": supported >= required,
    }


def _flow_coverage(coverage: dict[str, Any], derived: list[dict[str, Any]], *, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "counted_in_window": dict(coverage),
        "derived_reasons": derived,
        "collection": {
            "last_outcome": state.get("last_outcome"),
            "last_error": state.get("last_error"),
            "last_success_at_ms": state.get("last_success_at_ms"),
            "scanned_at_ms": state.get("scanned_at_ms"),
            "scanned_block": state.get("scanned_block"),
            "scanned_log": state.get("scanned_log"),
            "coverage_from_ms": state.get("coverage_from_ms"),
            "gap_at_ms": state.get("gap_at_ms"),
            "next_attempt_at_ms": state.get("next_attempt_at_ms"),
            "blocked_tx_hash": state.get("blocked_tx_hash"),
            "enrichment_error": state.get("enrichment_error"),
        },
        # Named for what they are. These accumulate for the life of the row and are not a rate.
        "lifetime_totals": {
            "ignored_inbound_total": state.get("ignored_inbound_total"),
            "unknown_total": state.get("unknown_total"),
        },
    }


def _send_queue(queue: list[dict[str, Any]], *, now_ms: int) -> dict[str, Any]:
    head = queue[0] if queue else None
    return {
        "waiting": len(queue),
        "head": None
        if head is None
        else {
            **head,
            "waiting_ms": now_ms - int(head["created_at_ms"]),
            "due_in_ms": int(head["next_attempt_at_ms"]) - now_ms,
        },
        "queue": queue,
    }


__all__ = ["handle_wallets"]
