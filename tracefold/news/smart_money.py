"""Deterministic OpenNews smart-money account facts. Parse at admission; never call a model.

OpenNews strategy 2026 (`聪明钱监控`) reports what one labelled account did, one report per line::

    js-2 Open Long BTC $798.18K , Price $79,817.87
    js-2 Close Short BTC $2.21M , Price $78,986.89 , PNL -$1.84K
    js-2 Close Long XYZ-NBIS $6.27 , Price $208.95 , PNL -$0.16

The label, the action, the side, the native instrument and the two dollar figures are the whole
message; `PNL` appears on close reports only. The provider abbreviates any of the three dollar figures
with the same `K`/`M`/`B` it uses on the liquidation template. The provider also
emits account activity that is not a position report at all — ``Withdraw USDC`` is the measured
example — and it emits new templates without warning. Those are not failures of the account: they are
reports this module cannot turn into numbers, so it returns ``None`` and the Item is stored as a raw
card with a reason. Nothing here
guesses a missing number, and no field is defaulted: a report with no venue keeps ``None`` for venue
rather than borrowing one from a sibling report.

`Open`/`Close` name the *reported* action and nothing more. A `Close` is not "the account is flat":
the provider reports selected activity, never a position snapshot, so no consumer may add these
figures into an account total or a net flow.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from tracefold.news.liquidations import FIGURE, MAGNITUDE_SUFFIXES, MAX_INSTRUMENT_LEN, scaled_amount

PARSER_VERSION: Final = "smart_money_parser_v1"
SOURCE_CONTRACT_VERSION: Final = "opennews_smart_money_source_v1"

# Anchored on the whole line. The label may contain spaces (`js-2`, `whale 7`), so it is captured
# non-greedily up to the first `Open`/`Close` token, which is what makes the action word the anchor
# rather than a position count.
#
# Every dollar figure on this template -- notional, price and PNL alike -- may carry the provider's
# `K`/`M`/`B`, read through the one multiplier defined in `liquidations.py`. The comment that used to
# stand here said such a suffix "has never been measured here" and refused it on that ground. It had
# been measured: of the 113 distinct titles in the retained production window only 8 parsed, and the
# other 104 position reports -- `$798.18K`, `$2.21M`, `PNL +$50.41K` -- were stored raw with
# `smart_money_template_unmatched` and sent one card per record, outside the §4.4 account grouping
# (#553). The price was the one figure left out of that fix, on the argument that the provider spells
# prices in full. The cost of being wrong about that was the whole record -- action, side, notional and
# PNL included -- not just the price, for a suffix whose meaning is the same three powers of ten the
# line's other two figures already use (#562 §5 row 2). A suffix outside `K`/`M`/`B` is still refused
# on every figure: that one this module genuinely has not been shown.
_REPORT = re.compile(
    rf"^\s*(?P<label>\S(?:.*?\S)?)\s+(?P<action>Open|Close)\s+(?P<side>Long|Short)\s+"
    rf"(?P<instrument>\S{{1,64}})\s+\$(?P<notional>{FIGURE})(?P<notional_unit>[{MAGNITUDE_SUFFIXES}]?)\s*,\s*"
    rf"Price\s+\$(?P<price>{FIGURE})(?P<price_unit>[{MAGNITUDE_SUFFIXES}]?)"
    rf"(?:\s*,\s*PNL\s*(?P<pnl_sign>[+-])?\s*\$(?P<pnl>{FIGURE})(?P<pnl_unit>[{MAGNITUDE_SUFFIXES}]?))?\s*$",
    re.IGNORECASE,
)
_MAX_NUMERIC: Final = Decimal("1e24")
_MAX_LABEL_LEN: Final = 128
_MAX_ADDRESS_LEN: Final = 128

RAW_REASON_TEMPLATE_UNMATCHED: Final = "smart_money_template_unmatched"


@dataclass(frozen=True, slots=True)
class SmartMoneyFact:
    """One reported account action, with every field the provider actually proved.

    `raw_instrument` is the provider's own token, prefix and contract spelling intact; `symbol` is the
    same token normalized the way every other consumer of provider coin tags normalizes one. Both are
    stored because the display short name is not an identity and the native token is not a header.
    """

    source_key: str
    item_id: str
    fact_id: str
    trader_label: str
    account_address: str | None
    action: Literal["open", "close"]
    position_side: Literal["long", "short"]
    raw_instrument: str
    symbol: str
    reported_notional_usd: Decimal
    price: Decimal
    pnl_usd: Decimal | None
    source_venue: str | None
    event_at_ms: int
    received_at_ms: int
    provider_record_identity: str
    source_strategy_id: str
    notional_semantics: str = "provider_reported_usd_notional"
    price_semantics: str = "provider_reported_unspecified_price"
    completeness_assumption: str = "selected_account_reports_without_position_snapshot"
    parser_version: str = PARSER_VERSION
    source_contract_version: str = SOURCE_CONTRACT_VERSION


def source_key(*, item_id: str, fact_id: str) -> str:
    """Provider-record identity plus fact and parser generation, exactly as liquidation keys one."""

    return hashlib.sha256(f"{item_id}\x1f{fact_id}\x1f{PARSER_VERSION}".encode()).hexdigest()


def parse_smart_money(
    title: str,
    *,
    item_id: str,
    fact_id: str,
    source_strategy_id: str,
    provider_source: str,
    related_address: str | None,
    event_at_ms: int,
    received_at_ms: int,
    provider_record_identity: str | None = None,
) -> SmartMoneyFact | None:
    """Parse one Strategy 2026 position report, or return ``None`` for anything that is not one.

    ``None`` covers `Withdraw USDC`, a template that has drifted, and a figure this module cannot read
    without assuming a unit -- a suffix outside the provider's own `K`/`M`/`B` vocabulary. The caller
    stores the Item either way; the only difference is whether a typed row exists beside it. An
    over-long label or instrument token is clipped rather than refused: its width says nothing about
    whether the numbers on the line are readable.

    The two clocks are recorded, never compared (#544): `event_at_ms` is the provider's stamp and
    `received_at_ms` is when this host read the frame. A non-positive `event_at_ms` is a missing stamp
    and is refused; an early one is a fact about two clocks.
    """

    match = _REPORT.fullmatch(str(title or ""))
    if match is None or event_at_ms <= 0:
        return None
    label = match.group("label").strip()[:_MAX_LABEL_LEN]
    if not label:
        return None
    raw_instrument = match.group("instrument").strip()[:MAX_INSTRUMENT_LEN]
    symbol = raw_instrument.upper().removeprefix("XYZ-")
    if not symbol:
        return None
    try:
        notional = scaled_amount(match.group("notional"), match.group("notional_unit"))
        price = scaled_amount(match.group("price"), match.group("price_unit"))
        pnl = None if match.group("pnl") is None else scaled_amount(match.group("pnl"), match.group("pnl_unit"))
    except (InvalidOperation, KeyError):
        return None
    if notional <= 0 or price <= 0 or notional > _MAX_NUMERIC or price > _MAX_NUMERIC:
        return None
    if pnl is not None:
        if pnl > _MAX_NUMERIC:
            return None
        if match.group("pnl_sign") == "-":
            pnl = -pnl
    venue = str(provider_source or "").strip().lower()[:32]
    address = str(related_address or "").strip()[:_MAX_ADDRESS_LEN]
    return SmartMoneyFact(
        source_key=source_key(item_id=item_id, fact_id=fact_id),
        item_id=item_id,
        fact_id=fact_id,
        trader_label=label,
        account_address=address or None,
        action="open" if match.group("action").lower() == "open" else "close",
        position_side="long" if match.group("side").lower() == "long" else "short",
        raw_instrument=raw_instrument,
        symbol=symbol,
        reported_notional_usd=notional,
        price=price,
        pnl_usd=pnl,
        source_venue=venue or None,
        event_at_ms=int(event_at_ms),
        received_at_ms=int(received_at_ms),
        provider_record_identity=str(provider_record_identity or item_id),
        source_strategy_id=str(source_strategy_id),
    )


__all__ = [
    "PARSER_VERSION",
    "RAW_REASON_TEMPLATE_UNMATCHED",
    "SOURCE_CONTRACT_VERSION",
    "SmartMoneyFact",
    "parse_smart_money",
    "source_key",
]
