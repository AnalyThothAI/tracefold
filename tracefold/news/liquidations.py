"""Deterministic OpenNews liquidation facts. Parse at admission; never call a model.

Two Strategies publish this one template. 2000 (`实时清算`) is the tuple this module was written
against and has no measured traffic; 2083 (`Large-scale liquidation`) is where every liquidation in
the retained window actually came from, and it was classified as an unsupported market source and
dropped on the floor (#553). Both are recorded, each under its own `source_strategy_id`, because
which Strategy reported a forced trade is a fact about the report and not a reason to merge or
discard it.

The venue allowlist is gone with them. It admitted `binance` and `hyperliquid` and refused everything
else, so 12 `okx` and 1 `bybit` reports in the same window were refused for naming a venue this code
had not been told about -- as if a liquidation on an exchange we do not trade is not a liquidation.
The venue is now the provider's own string, stored as sent. Supporting a venue's *information* is not
supporting trading on it, and the two were never the same claim.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

PARSER_VERSION: Final = "liquidation_parser_v1"
# v2: the venue allowlist is deleted, the reporting Strategy is recorded, and the provider's own
# instrument token is kept beside the normalized symbol. All three change what a stored row means.
SOURCE_CONTRACT_VERSION: Final = "opennews_liquidation_source_v2"
# Why one Item classified as a liquidation carries no typed row. The Item is stored either way.
RAW_REASON_TEMPLATE_UNMATCHED: Final = "liquidation_template_unmatched"

# The provider's magnitude vocabulary. `K`/`M`/`B` are three powers of ten on every OpenNews template
# that abbreviates a dollar figure, so they are defined once here and read from here by
# `smart_money.py`: two copies of a multiplier are two chances for one template to start meaning a
# different number by the same letter.
MAGNITUDE_SUFFIXES: Final = "KMB"
_UNIT: Final[dict[str, Decimal]] = {
    "": Decimal(1),
    "K": Decimal(1_000),
    "M": Decimal(1_000_000),
    "B": Decimal(1_000_000_000),
}

_FRAME = re.compile(
    r"^\s*(?P<symbol>[A-Z0-9._-]{1,16})\s+Large\s+"
    r"(?P<side>Short|Long)\s+Liquidation\s+"
    rf"(?P<notional>\d+(?:\.\d+)?)(?P<unit>[{MAGNITUDE_SUFFIXES}]?)\s+at\s+\$(?P<price>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
_MAX_NUMERIC: Final = Decimal("1e24")
_MAX_VENUE_LEN: Final = 32


@dataclass(frozen=True, slots=True)
class LiquidationFact:
    source_key: str
    item_id: str
    fact_id: str
    symbol: str
    raw_instrument: str
    source_venue: str | None
    source_strategy_id: str
    liquidated_position_side: Literal["long", "short"]
    forced_order_side: Literal["buy", "sell"]
    notional_usd: Decimal
    quantity: Decimal | None
    price: Decimal
    event_at_ms: int
    received_at_ms: int
    provider_record_identity: str
    symbol_contract_identity: str
    position_side_semantics: str
    quantity_semantics: str
    notional_semantics: str
    price_semantics: str
    completeness_assumption: str
    throttle_assumption: str
    source_contract_version: str = SOURCE_CONTRACT_VERSION
    source_contract_complete: bool = False
    parser_version: str = PARSER_VERSION


def source_key(*, item_id: str, fact_id: str) -> str:
    """Provider-record identity plus fact and parser generation."""

    return hashlib.sha256(f"{item_id}\x1f{fact_id}\x1f{PARSER_VERSION}".encode()).hexdigest()


def scaled_amount(digits: str, suffix: str) -> Decimal:
    """One provider dollar figure as a number: `1.25` with `M` is 1 250 000, `99` with no suffix is 99.

    Thousands separators are stripped rather than validated -- which figures may carry one is a
    property of the caller's own template grammar, not of the multiplier. A non-numeric figure raises
    `InvalidOperation` and a suffix outside the vocabulary raises `KeyError`; both parsers fail closed
    on either, because a figure whose unit this code cannot name is exactly the number it must not
    guess.
    """

    return Decimal(digits.replace(",", "")) * _UNIT[suffix.upper()]


def parse_liquidation(
    title: str,
    *,
    item_id: str,
    fact_id: str,
    source_strategy_id: str,
    provider_source: str,
    event_at_ms: int,
    received_at_ms: int,
    provider_record_identity: str | None = None,
) -> LiquidationFact | None:
    """Parse the shared liquidation wire template, failing closed on ambiguous units only.

    The two clocks are not compared. `event_at_ms` is the venue's own stamp for the forced trade and
    `received_at_ms` is when this host read it; a venue clock running a few hundred milliseconds ahead
    of this one is a fact about the world, not a malformed frame, and refusing the frame for it
    discarded a real liquidation that had already happened (#544). A non-positive `event_at_ms` is
    still refused, because that is a missing stamp rather than an early one.
    """

    match = _FRAME.fullmatch(str(title or ""))
    venue = str(provider_source or "").strip().lower()[:_MAX_VENUE_LEN]
    if match is None or event_at_ms <= 0:
        return None
    try:
        notional = scaled_amount(match.group("notional"), match.group("unit"))
        price = scaled_amount(match.group("price"), "")
    except (InvalidOperation, KeyError):
        return None
    if notional <= 0 or price <= 0 or notional > _MAX_NUMERIC or price > _MAX_NUMERIC:
        return None
    position_side = match.group("side").lower()
    raw_instrument = match.group("symbol").strip()[:32]
    symbol = raw_instrument.upper().removeprefix("XYZ-")
    if not symbol:
        return None
    return LiquidationFact(
        source_key=source_key(item_id=item_id, fact_id=fact_id),
        item_id=item_id,
        fact_id=fact_id,
        symbol=symbol,
        raw_instrument=raw_instrument,
        source_venue=venue or None,
        source_strategy_id=str(source_strategy_id),
        liquidated_position_side=position_side,  # type: ignore[arg-type]
        forced_order_side="buy" if position_side == "short" else "sell",
        notional_usd=notional,
        quantity=None,
        price=price,
        event_at_ms=int(event_at_ms),
        received_at_ms=int(received_at_ms),
        provider_record_identity=str(provider_record_identity or item_id),
        # OpenNews names a base symbol and venue but not the exact listed contract.
        symbol_contract_identity=f"unresolved:{venue or 'unknown'}:{symbol}",
        position_side_semantics="template_position_side;short=>forced_buy;long=>forced_sell",
        quantity_semantics="not_provided",
        notional_semantics="provider_reported_usd_notional",
        price_semantics="provider_reported_unspecified_price",
        completeness_assumption="selected_events_without_heartbeat_sequence_or_coverage_sla",
        throttle_assumption="provider_throttle_unknown",
    )


__all__ = [
    "MAGNITUDE_SUFFIXES",
    "PARSER_VERSION",
    "RAW_REASON_TEMPLATE_UNMATCHED",
    "SOURCE_CONTRACT_VERSION",
    "LiquidationFact",
    "parse_liquidation",
    "scaled_amount",
    "source_key",
]
