"""Conservative, replayable shadow net path and venue PAPER receipt evaluation.

Shadow prices are assumptions, never exchange fills. A PAPER result requires
reconciled venue fills, commissions and funding; missing components stay unknown.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from .contracts import ExitPlan

EVALUATION_VERSION = "shadow_net_v1"
SHADOW_MARK_RECEIPT_MAX_DELAY_MS = 120_000


def _unevaluable(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "unevaluable", "reason": reason, "evaluation_version": EVALUATION_VERSION, **details}


def evaluate_shadow(
    *,
    side: str,
    decision_at_ms: int,
    scheduled_at_ms: int,
    decision_quote: dict[str, Any] | None,
    planned_quote: dict[str, Any] | None,
    exit_quotes: tuple[dict[str, Any], ...],
    requested_notional_usdt: Decimal,
    mark_rows: tuple[dict[str, Any], ...],
    mark_status: str,
    funding_events: tuple[dict[str, Any], ...],
    funding_coverage_complete: bool,
    exit_plan: ExitPlan,
    fee_bps_per_side: Decimal | None,
    quote_environment: str,
    target_environment: str,
    instrument_rules: dict[str, Any] | None,
) -> dict[str, Any]:
    if side not in ("long", "short") or scheduled_at_ms < decision_at_ms:
        raise ValueError("shadow_identity_or_clock_invalid")
    context = {
        "decision_at_ms": decision_at_ms,
        "scheduled_at_ms": scheduled_at_ms,
        "order_latency_ms": scheduled_at_ms - decision_at_ms,
        "quote_environment": quote_environment,
        "target_environment": target_environment,
        "paper_comparable": quote_environment == target_environment == "demo",
        "source": "shadow_simulation",
    }
    if quote_environment != target_environment:
        return _unevaluable("environment_mismatch", **context)
    if decision_quote is None or planned_quote is None:
        return _unevaluable("executable_quote_missing", **context)
    if instrument_rules is None:
        return _unevaluable("instrument_rules_missing", **context)
    try:
        tick = Decimal(str(instrument_rules["price_tick_size"]))
        minimum_quantity = Decimal(str(instrument_rules["market_min_quantity"]))
        maximum_quantity = Decimal(str(instrument_rules["market_max_quantity"]))
        step = Decimal(str(instrument_rules["market_step_size"]))
        minimum_notional = Decimal(str(instrument_rules["minimum_notional"]))
    except (InvalidOperation, KeyError, TypeError):
        return _unevaluable("instrument_rules_invalid", **context)
    if (
        instrument_rules.get("trading_status") != "TRADING"
        or instrument_rules.get("contract_type") != "PERPETUAL"
        or instrument_rules.get("quote_asset") != "USDT"
        or instrument_rules.get("settlement_asset") != "USDT"
        or not all(value.is_finite() for value in (tick, minimum_quantity, maximum_quantity, step, minimum_notional))
        or min(tick, minimum_quantity, step, minimum_notional) <= 0
        or maximum_quantity < minimum_quantity
    ):
        return _unevaluable("instrument_rules_invalid", **context)
    if fee_bps_per_side is None:
        return _unevaluable("cost_assumption_missing", **context)
    if not fee_bps_per_side.is_finite() or fee_bps_per_side < 0:
        raise ValueError("shadow_cost_assumption_invalid")
    if not requested_notional_usdt.is_finite() or requested_notional_usdt <= 0:
        raise ValueError("shadow_notional_invalid")
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
            bid_size = Decimal(str(quote.get("bid_quantity")))
            ask_size = Decimal(str(quote.get("ask_quantity")))
        except (InvalidOperation, TypeError):
            return _unevaluable("quote_invalid", **context)
        if (
            not all(value.is_finite() for value in (bid, ask, bid_size, ask_size))
            or bid <= 0
            or ask <= 0
            or bid > ask
            or bid_size < 0
            or ask_size < 0
        ):
            return _unevaluable("quote_invalid", **context)
    entry = Decimal(str(planned_quote["ask"] if side == "long" else planned_quote["bid"]))
    quantity = (requested_notional_usdt / entry / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if quantity < minimum_quantity or quantity > maximum_quantity or quantity * entry < minimum_notional:
        return _unevaluable("market_quantity_filter_rejects_research_size", **context)
    available_entry = Decimal(str(planned_quote["ask_quantity"] if side == "long" else planned_quote["bid_quantity"]))
    if quantity > available_entry:
        return _unevaluable("entry_top_size_insufficient", **context)
    stop_fraction = Decimal(exit_plan.stop_distance_bps) / Decimal(10_000)
    take_fraction = Decimal(exit_plan.take_profit_bps) / Decimal(10_000)
    stop = entry * (1 - stop_fraction if side == "long" else 1 + stop_fraction)
    take = entry * (1 + take_fraction if side == "long" else 1 - take_fraction)
    stop = (stop / tick).to_integral_value(rounding=ROUND_CEILING if side == "long" else ROUND_FLOOR) * tick
    take = (take / tick).to_integral_value(rounding=ROUND_FLOOR if side == "long" else ROUND_CEILING) * tick
    if (side == "long" and not stop < entry < take) or (side == "short" and not take < entry < stop):
        return _unevaluable("protection_price_filter_rejects_levels", **context)
    deadline = scheduled_at_ms + exit_plan.max_holding_seconds * 1_000
    ordered = sorted(mark_rows, key=lambda row: int(row["event_at_ms"]))
    prior_at = scheduled_at_ms // 60_000 * 60_000
    exit_mark: Decimal | None = None
    exit_at: int | None = None
    exit_visible_at: int | None = None
    exit_reason: str | None = None
    for row in ordered:
        at_ms = int(row["event_at_ms"])
        if at_ms <= scheduled_at_ms:
            continue
        if at_ms - prior_at > 60_000:
            return _unevaluable("mark_path_gap", **context)
        prior_at = at_ms
        received_at_ms = row.get("received_at_ms")
        if not row.get("snapshot_ref") or not isinstance(received_at_ms, int):
            return _unevaluable("mark_path_incomplete", **context)
        try:
            high = Decimal(str(row["high"]))
            low = Decimal(str(row["low"]))
            close = Decimal(str(row["close"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return _unevaluable("mark_bar_invalid", **context)
        visible_at = received_at_ms
        if (
            not all(value.is_finite() for value in (high, low, close))
            or low <= 0
            or high < low
            or not low <= close <= high
            or visible_at < at_ms
        ):
            return _unevaluable("mark_bar_invalid", **context)
        if visible_at > at_ms + SHADOW_MARK_RECEIPT_MAX_DELAY_MS:
            return _unevaluable("mark_path_incomplete", **context)
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
            exit_visible_at = visible_at
            break
    if exit_mark is None or exit_at is None or exit_visible_at is None or exit_reason is None:
        return _unevaluable("mark_endpoint_missing", **context)
    eligible_quotes = sorted(
        (
            quote
            for quote in exit_quotes
            if isinstance(quote, dict) and quote.get("status") == "ok" and isinstance(quote.get("received_at_ms"), int)
        ),
        key=lambda quote: int(quote["received_at_ms"]),
    )
    exit_quote = next(
        (quote for quote in eligible_quotes if exit_visible_at <= int(quote["received_at_ms"]) <= exit_at + 90_000),
        None,
    )
    if exit_quote is None:
        return _unevaluable("exit_quote_missing", **context)
    if exit_quote.get("environment") != quote_environment or not exit_quote.get("quote_ref"):
        return _unevaluable("exit_quote_provenance_invalid", **context)
    try:
        exit_price = Decimal(str(exit_quote["bid"] if side == "long" else exit_quote["ask"]))
        exit_size = Decimal(str(exit_quote["bid_quantity"] if side == "long" else exit_quote["ask_quantity"]))
    except (InvalidOperation, KeyError, TypeError):
        return _unevaluable("exit_quote_invalid", **context)
    if not exit_price.is_finite() or not exit_size.is_finite() or exit_price <= 0 or exit_size < quantity:
        return _unevaluable("exit_top_size_insufficient_or_invalid", **context)
    if exit_reason == "stop":
        exit_price = min(exit_price, stop) if side == "long" else max(exit_price, stop)
    entry_notional_usdt = quantity * entry
    exit_notional_usdt = quantity * exit_price
    gross_usdt = (exit_notional_usdt - entry_notional_usdt) * (1 if side == "long" else -1)
    funding_usdt = Decimal(0)
    try:
        for event in funding_events:
            if not scheduled_at_ms < int(event["funding_at_ms"]) <= int(exit_quote["received_at_ms"]):
                continue
            rate = Decimal(str(event["funding_rate"]))
            funding_mark = Decimal(str(event["mark_price"]))
            if not rate.is_finite() or not funding_mark.is_finite() or funding_mark <= 0:
                return _unevaluable("funding_event_invalid", **context)
            funding_usdt -= (1 if side == "long" else -1) * quantity * funding_mark * rate
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return _unevaluable("funding_event_invalid", **context)
    fee_usdt = (entry_notional_usdt + exit_notional_usdt) * fee_bps_per_side / Decimal(10_000)
    net_usdt = gross_usdt - fee_usdt + funding_usdt
    gross_bps = gross_usdt / entry_notional_usdt * Decimal(10_000)
    fee_bps = fee_usdt / entry_notional_usdt * Decimal(10_000)
    funding_cashflow_bps = funding_usdt / entry_notional_usdt * Decimal(10_000)
    net_bps = gross_bps - fee_bps + funding_cashflow_bps
    return {
        **context,
        "status": "simulated",
        "cashflow_version": "fill_notional_and_settlement_mark_v1",
        "side": side,
        "entry_at_ms": scheduled_at_ms,
        "entry_price": str(entry),
        "quantity_base": str(quantity),
        "simulated_notional_usdt": str(entry_notional_usdt),
        "requested_notional_usdt": str(requested_notional_usdt),
        "price_tick_size": str(tick),
        "market_step_size": str(step),
        "stop_price": str(stop),
        "take_price": str(take),
        "stop_bps": exit_plan.stop_distance_bps,
        "exit_mark": str(exit_mark),
        "exit_price_assumption": str(exit_price),
        "exit_quote_ref": exit_quote["quote_ref"],
        "exit_quote_at_ms": exit_quote["received_at_ms"],
        "exit_quote_delay_ms": int(exit_quote["received_at_ms"]) - exit_at,
        "mark_trigger_at_ms": exit_at,
        "mark_trigger_visible_at_ms": exit_visible_at,
        "exit_at_ms": int(exit_quote["received_at_ms"]),
        "exit_reason": exit_reason,
        "gross_bps": str(gross_bps),
        "gross_usdt": str(gross_usdt),
        "fee_bps": str(fee_bps),
        "fees_usdt": str(fee_usdt),
        "funding_bps_paid": str(-funding_cashflow_bps),
        "funding_usdt": str(funding_usdt),
        "net_bps": str(net_bps),
        "net_usdt": str(net_usdt),
        "net_components_bps": {
            "gross": str(gross_bps),
            "entry_cost": "0",
            "exit_cost": "0",
            "fees": str(fee_bps),
            "funding_cashflow": str(funding_cashflow_bps),
        },
        "assumptions": {
            "entry": "planned_ask" if side == "long" else "planned_bid",
            "exit": "first_archived_executable_quote_after_mark_close_stop_price_capped",
            "both_triggers_same_bar": "stop_first",
            "entry_partial_bar": "stop_possible_take_unproven",
            "rejection": "not_simulated",
            "partial_fill": "top_book_size_covers_full_research_quantity",
            "protection": "not_simulated",
            "fee_bps_per_side": str(fee_bps_per_side),
            "fee_basis": "entry_and_exit_fill_notional",
            "funding_basis": "settlement_mark_price_times_base_quantity",
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
