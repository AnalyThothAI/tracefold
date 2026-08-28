"""The deterministic execution rules shared by live and BAR replay adapters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Final, Literal

from .contracts import canonical_sha256

EXECUTION_POLICY_VERSION = "trade_execution_policy_v1"
ENTRY_TTL_MS: Final = 60_000
STOP_LOSS_BPS: Final = 200
MAX_HOLDING_MS: Final = 180_000
MAX_ENTRY_DRIFT_BPS: Final = 25
MAX_SPREAD_BPS: Final = 30
TARGET_NOTIONAL_CEILING_USD: Final = Decimal("10")
EXECUTION_POLICY_SHA256: Final = canonical_sha256(
    {
        "version": EXECUTION_POLICY_VERSION,
        "entry_ttl_ms": ENTRY_TTL_MS,
        "stop_loss_bps": STOP_LOSS_BPS,
        "max_holding_ms": MAX_HOLDING_MS,
        "max_entry_drift_bps": MAX_ENTRY_DRIFT_BPS,
        "max_spread_bps": MAX_SPREAD_BPS,
        "target_notional_usd_ceiling": str(TARGET_NOTIONAL_CEILING_USD),
        "quantity": "floor(target_notional/ask,size_increment)",
        "minimums": "min_quantity_and_min_notional",
        "spread": "ask_minus_bid_over_mid_bps",
        "drift": "absolute_ask_minus_reference_over_reference_bps",
        "stop": "floor(entry*(10000-stop_bps)/10000,price_increment)",
        "holding": "opened_at_plus_max_holding",
        "economic_leg_identity": "tf-leg-intent-prefix",
    }
)
IntentLeg = Literal["entry", "stop", "close"]


@dataclass(frozen=True, slots=True)
class EntryPolicyDecision:
    quantity: Decimal | None
    reason: Literal["accepted", "intent_expired", "market_unacceptable", "quantity_unexecutable"]


def evaluate_entry(
    *,
    now_ms: int,
    created_at_ms: int,
    valid_until_ms: int,
    quote_at_ms: int,
    bid: Decimal,
    ask: Decimal,
    reference_price: Decimal,
    target_notional: Decimal,
    size_increment: Decimal,
    min_quantity: Decimal | None,
    min_notional: Decimal | None,
    max_spread_bps: int,
    max_drift_bps: int,
) -> EntryPolicyDecision:
    if now_ms >= valid_until_ms:
        return EntryPolicyDecision(None, "intent_expired")
    if quote_at_ms < created_at_ms or quote_at_ms > now_ms or bid <= 0 or ask <= 0 or ask < bid:
        return EntryPolicyDecision(None, "market_unacceptable")
    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10_000
    drift_bps = abs(ask - reference_price) / reference_price * 10_000
    if spread_bps > max_spread_bps or drift_bps > max_drift_bps:
        return EntryPolicyDecision(None, "market_unacceptable")
    raw_quantity = target_notional / ask
    quantity = (raw_quantity / size_increment).to_integral_value(rounding=ROUND_DOWN) * size_increment
    if (
        quantity <= 0
        or (min_quantity is not None and quantity < min_quantity)
        or (min_notional is not None and quantity * ask < min_notional)
    ):
        return EntryPolicyDecision(None, "quantity_unexecutable")
    return EntryPolicyDecision(quantity, "accepted")


def stop_price(*, entry_price: Decimal, stop_loss_bps: int, price_increment: Decimal) -> Decimal:
    raw = entry_price * (Decimal(10_000 - stop_loss_bps) / Decimal(10_000))
    return (raw / price_increment).to_integral_value(rounding=ROUND_DOWN) * price_increment


def max_holding_due(*, opened_at_ms: int, max_holding_ms: int, now_ms: int) -> bool:
    return now_ms >= opened_at_ms + max_holding_ms


def deterministic_client_order_id(
    intent_id: str,
    leg: IntentLeg,
    *,
    previous_client_order_id: str | None = None,
) -> str:
    marker = {"entry": "e", "stop": "s", "close": "c"}[leg]
    if leg == "stop":
        seed = canonical_sha256(
            {
                "intent_id": intent_id,
                "leg": "stop",
                "previous_client_order_id": previous_client_order_id,
            }
        )
        return f"tf-{marker}-{seed[:28]}"
    if previous_client_order_id is not None:
        raise ValueError("previous_client_order_id_only_valid_for_stop")
    return f"tf-{marker}-{intent_id[:28]}"


__all__ = [
    "ENTRY_TTL_MS",
    "EXECUTION_POLICY_SHA256",
    "EXECUTION_POLICY_VERSION",
    "MAX_ENTRY_DRIFT_BPS",
    "MAX_HOLDING_MS",
    "MAX_SPREAD_BPS",
    "STOP_LOSS_BPS",
    "TARGET_NOTIONAL_CEILING_USD",
    "EntryPolicyDecision",
    "IntentLeg",
    "deterministic_client_order_id",
    "evaluate_entry",
    "max_holding_due",
    "stop_price",
]
