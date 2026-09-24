"""Conservative, replayable shadow net path and venue PAPER receipt evaluation.

Shadow prices are assumptions, never exchange fills. A PAPER result requires
reconciled venue fills, commissions and funding; missing components stay unknown.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import ExitPlan

EVALUATION_VERSION = "shadow_net_v1"


def _unevaluable(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "unevaluable", "reason": reason, "evaluation_version": EVALUATION_VERSION, **details}


def evaluate_shadow(
    *,
    side: str,
    decision_at_ms: int,
    scheduled_at_ms: int,
    decision_quote: dict[str, Any] | None,
    planned_quote: dict[str, Any] | None,
    mark_rows: tuple[dict[str, Any], ...],
    mark_status: str,
    funding_events: tuple[dict[str, Any], ...],
    funding_coverage_complete: bool,
    exit_plan: ExitPlan,
    fee_bps_per_side: Decimal | None,
    exit_spread_bps: Decimal | None,
    quote_environment: str,
    target_environment: str,
) -> dict[str, Any]:
    if side not in ("long", "short") or scheduled_at_ms < decision_at_ms:
        raise ValueError("shadow_identity_or_clock_invalid")
    context = {
        "decision_at_ms": decision_at_ms,
        "scheduled_at_ms": scheduled_at_ms,
        "order_latency_ms": scheduled_at_ms - decision_at_ms,
        "quote_environment": quote_environment,
        "target_environment": target_environment,
        "paper_comparable": quote_environment == target_environment,
        "source": "shadow_simulation",
    }
    if decision_quote is None or planned_quote is None:
        return _unevaluable("executable_quote_missing", **context)
    if fee_bps_per_side is None or exit_spread_bps is None:
        return _unevaluable("cost_assumption_missing", **context)
    if (
        not fee_bps_per_side.is_finite()
        or not exit_spread_bps.is_finite()
        or fee_bps_per_side < 0
        or exit_spread_bps < 0
    ):
        raise ValueError("shadow_cost_assumption_invalid")
    if mark_status != "ok" or not mark_rows:
        return _unevaluable("mark_path_incomplete", **context)
    if not funding_coverage_complete:
        return _unevaluable("funding_coverage_missing", **context)
    for quote, at_ms in ((decision_quote, decision_at_ms), (planned_quote, scheduled_at_ms)):
        quote_at = quote.get("received_at_ms")
        if not isinstance(quote_at, int) or abs(quote_at - at_ms) > 2_000:
            return _unevaluable("quote_time_unaligned", **context)
        try:
            bid, ask = Decimal(str(quote.get("bid"))), Decimal(str(quote.get("ask")))
        except InvalidOperation:
            return _unevaluable("quote_invalid", **context)
        if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0 or bid > ask:
            return _unevaluable("quote_invalid", **context)
    entry = Decimal(str(planned_quote["ask"] if side == "long" else planned_quote["bid"]))
    stop_fraction = Decimal(exit_plan.stop_distance_bps) / Decimal(10_000)
    take_fraction = Decimal(exit_plan.take_profit_bps) / Decimal(10_000)
    stop = entry * (1 - stop_fraction if side == "long" else 1 + stop_fraction)
    take = entry * (1 + take_fraction if side == "long" else 1 - take_fraction)
    deadline = scheduled_at_ms + exit_plan.max_holding_seconds * 1_000
    ordered = sorted(mark_rows, key=lambda row: int(row["event_at_ms"]))
    prior_at = scheduled_at_ms // 60_000 * 60_000
    exit_mark: Decimal | None = None
    exit_at: int | None = None
    exit_reason: str | None = None
    for row in ordered:
        at_ms = int(row["event_at_ms"])
        if at_ms <= scheduled_at_ms:
            continue
        if at_ms - prior_at > 60_000:
            return _unevaluable("mark_path_gap", **context)
        prior_at = at_ms
        try:
            high = Decimal(str(row["high"]))
            low = Decimal(str(row["low"]))
            close = Decimal(str(row["close"]))
        except (InvalidOperation, KeyError, TypeError):
            return _unevaluable("mark_bar_invalid", **context)
        if (
            not all(value.is_finite() for value in (high, low, close))
            or low <= 0
            or high < low
            or not low <= close <= high
        ):
            return _unevaluable("mark_bar_invalid", **context)
        # A 1m OHLC bar cannot order two touches; stop-first is conservative.
        stop_hit = low <= stop if side == "long" else high >= stop
        # The bar containing the planned entry also contains prices from
        # before that entry. Its adverse extreme is a conservative possible
        # stop, but its favorable extreme cannot prove a post-entry take.
        entry_partial_bar = scheduled_at_ms % 60_000 != 0 and at_ms == (scheduled_at_ms // 60_000 + 1) * 60_000
        take_hit = (high >= take if side == "long" else low <= take) and not entry_partial_bar
        if stop_hit:
            exit_mark, exit_reason = stop, "stop"
        elif take_hit:
            exit_mark, exit_reason = take, "take_profit"
        elif at_ms >= deadline:
            exit_mark, exit_reason = close, "max_holding"
        if exit_mark is not None:
            exit_at = at_ms
            break
    if exit_mark is None or exit_at is None or exit_reason is None:
        return _unevaluable("mark_endpoint_missing", **context)
    spread_fraction = exit_spread_bps / Decimal(20_000)
    exit_price = exit_mark * (1 - spread_fraction if side == "long" else 1 + spread_fraction)
    gross_bps = ((exit_price / entry - 1) if side == "long" else (1 - exit_price / entry)) * 10_000
    try:
        rates = [
            Decimal(str(event["funding_rate"])) * 10_000
            for event in funding_events
            if scheduled_at_ms < int(event["funding_at_ms"]) <= exit_at
        ]
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return _unevaluable("funding_event_invalid", **context)
    if any(not rate.is_finite() for rate in rates):
        return _unevaluable("funding_event_invalid", **context)
    funding_bps = sum(rates) * (1 if side == "long" else -1)
    fee_bps = fee_bps_per_side * 2
    net_bps = gross_bps - fee_bps - funding_bps
    return {
        **context,
        "status": "simulated",
        "entry_price": str(entry),
        "exit_mark": str(exit_mark),
        "exit_price_assumption": str(exit_price),
        "exit_at_ms": exit_at,
        "exit_reason": exit_reason,
        "gross_bps": str(gross_bps),
        "fee_bps": str(fee_bps),
        "funding_bps_paid": str(funding_bps),
        "net_bps": str(net_bps),
        "assumptions": {
            "entry": "planned_ask" if side == "long" else "planned_bid",
            "exit": "mark_with_conservative_spread",
            "both_triggers_same_bar": "stop_first",
            "entry_partial_bar": "stop_possible_take_unproven",
            "rejection": "not_simulated",
            "partial_fill": "not_simulated",
            "protection": "not_simulated",
            "fee_bps_per_side": str(fee_bps_per_side),
            "exit_spread_bps": str(exit_spread_bps),
        },
        "evaluation_version": EVALUATION_VERSION,
    }


def evaluate_paper_receipt(
    *,
    venue_reconciled: bool,
    entry_fills: tuple[dict[str, Any], ...],
    exit_fills: tuple[dict[str, Any], ...],
    funding_usd: Decimal | None,
    protection_confirmed: bool | None,
    side: str,
) -> dict[str, Any]:
    if side not in ("long", "short"):
        raise ValueError("paper_side_invalid")
    if not venue_reconciled:
        return _unevaluable("venue_reconciliation_missing", source="paper_venue")
    if protection_confirmed is not True:
        return _unevaluable("protection_receipt_missing", source="paper_venue")
    if not entry_fills or not exit_fills or funding_usd is None:
        return _unevaluable("fill_or_funding_missing", source="paper_venue")
    fills = entry_fills + exit_fills
    if any(fill.get("fee_usd") is None for fill in fills):
        return _unevaluable("commission_missing", source="paper_venue")
    entry_qty = sum(Decimal(str(fill["quantity"])) for fill in entry_fills)
    exit_qty = sum(Decimal(str(fill["quantity"])) for fill in exit_fills)
    if entry_qty <= 0 or exit_qty != entry_qty:
        return _unevaluable("partial_or_unclosed_position", source="paper_venue")
    entry_usd = sum(Decimal(str(fill["quantity"])) * Decimal(str(fill["price"])) for fill in entry_fills)
    exit_usd = sum(Decimal(str(fill["quantity"])) * Decimal(str(fill["price"])) for fill in exit_fills)
    fees_usd = sum(Decimal(str(fill["fee_usd"])) for fill in fills)
    gross_usd = exit_usd - entry_usd if side == "long" else entry_usd - exit_usd
    return {
        "status": "paper_venue_net",
        "source": "paper_venue",
        "evaluation_version": EVALUATION_VERSION,
        "quantity": str(entry_qty),
        "gross_usd": str(gross_usd),
        "fees_usd": str(fees_usd),
        "funding_usd": str(funding_usd),
        "net_usd": str(gross_usd - fees_usd + funding_usd),
    }
