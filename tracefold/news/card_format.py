"""The characters a reader card shows: money, percent, time, clipping, ticker shape, unknown placeholders.

One implementation, used by every card the reader can receive (#562 §3). Before this module the same
six rules existed three times -- once in the News first card, once in the market card, once in the
Telegram adapter's reverse parser -- and they had drifted: `$74,553.10` next to a raw decimal, `24h
+7.91%` next to `12.5%`, two copies of the same UTC+8 offset, `HH:MM` next to `HH:MM:SS`. A reader
who sees the same number written two ways on two cards has been given a reason to doubt both.

Everything here is pure and total: no clock read, no I/O, no exception a caller has to guard. A value
this module cannot render returns the empty string or the unknown placeholder, and the caller decides
whether that costs an entry or a line -- never the card.
"""

from __future__ import annotations

import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import isfinite
from typing import Final

from .market_review.pricing import parse_price

# The reader's clock (Asia/Shanghai); every stored stamp is UTC ms. The single offset constant: the
# two renderers each carried their own `8 * 3600`, which is exactly how two cards drift by an hour.
CARD_TZ_OFFSET_S: Final = 8 * 3600

# What a card says when it does not know. Each is a different sentence and none of them is a zero:
# "no figure was reported", "the venue was not named", "the measurement was not named", "the account
# carries no label", "the source was not named".
UNKNOWN_FIGURE: Final = "—"
UNKNOWN_VENUE: Final = "场所未知"
UNKNOWN_MEASUREMENT: Final = "口径未知"
UNKNOWN_ACCOUNT: Final = "未标注账户"
UNKNOWN_ORIGIN: Final = "-"

# A ticker a reader action can be built for: upper-case, bounded, no separator a URL would have to
# escape. The adapter that builds a trade link and the renderer that prints the symbol agree here.
LINKABLE_TICKER_RE: Final = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
LINKABLE_VENUE_SYMBOL_RE: Final = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@:/._-]{0,63}$")

# The window a change percentage was measured over, said in the reader's words. Never hard-coded to
# "24h": Binance publishes a rolling 24 h window and Hyperliquid the venue's own day, and about 8% of
# a week's card assets price on Hyperliquid. A basis we cannot name means the percentage is dropped,
# not guessed -- the price still renders. A basis `pricing` knows and this map does not would drop
# every percentage for that venue in silence, so the two key sets are pinned together in
# `test_card_change_basis_labels_cover_the_price_domain`.
CHANGE_BASIS_LABEL: Final[dict[str, str]] = {"rolling_24h": "24h", "provider_day": "日内"}


def clock(at_ms: int) -> str:
    """`HH:MM` on the reader's clock. Minutes, never seconds: a card is not a log line."""

    return time.strftime("%H:%M", time.gmtime(int(at_ms) / 1000 + CARD_TZ_OFFSET_S))


def price(value: object) -> str:
    """A provider price as the characters the reader sees, or `""` when there are none.

    Deliberately the same rule as the console's `formatPrice`
    (`web/src/features/news/model/newsPrice.ts`): >= 1000 keeps two decimals and thousands
    separators, >= 1 keeps up to four, below 1 up to six, and trailing zeros are dropped. The two
    surfaces must agree character for character -- a reader who sees 74,553.10 on a card and 74,553.1
    in the console has been given a reason to doubt both. `test_card_format_agrees_with_the_console`
    holds them to one shared table so editing one without the other fails.

    A price that rounds away to zero is not rendered at all; `""` means the caller drops the whole
    entry. So does a number too large to quantize: `parse_price` bounds a price to "finite and
    positive", not to a magnitude, and `Decimal("1e40").quantize(...)` raises rather than returning
    characters. This is called from a renderer, outside the quote read model's own guard, so it must
    never raise -- an unrenderable price costs its own entry, never the card.
    """

    parsed = parse_price(value)
    if parsed is None:
        return ""
    try:
        if parsed >= 1000:
            return f"{parsed.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,f}"
        places = Decimal("0.0001") if parsed >= 1 else Decimal("0.000001")
        text = f"{parsed.quantize(places, rounding=ROUND_HALF_UP):,f}"
    except (InvalidOperation, ValueError):
        return ""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "" if text == "0" else text


def change(value: object, basis: object) -> str:
    """`24h +7.91%` -- a percentage is never shown without the window it was measured over.

    Two decimals, matching the console's `formatChangePct`. An unknown or missing basis returns `""`:
    the price still renders, the number whose meaning we cannot state does not.
    """

    label = CHANGE_BASIS_LABEL.get(str(basis or ""))
    if label is None or isinstance(value, bool) or not isinstance(value, int | float):
        return ""
    pct = float(value)
    if not isfinite(pct):  # NaN / inf never reach a reader
        return ""
    return f"{label} {'+' if pct > 0 else ''}{pct:.2f}%"


def percent_from_bps(bps: int | None) -> str:
    """An integer basis-point fact as a percentage, trailing zeros dropped: `3.66%`, `3.6%`.

    Unlike `change` this carries no window label, because the measurement definition that owns the
    window is printed on its own line of the card that uses it.
    """

    if bps is None:
        return UNKNOWN_FIGURE
    text = f"{int(bps) / 100:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


def usd_compact(value: int | None) -> str:
    """A whole-dollar figure at reading scale: `6.59M`, `278.62M`, `1.04B`, or the digits below 1K."""

    if value is None:
        return UNKNOWN_FIGURE
    amount = int(value)
    for unit, scale in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(amount) >= scale:
            return f"{amount / scale:.2f}{unit}"
    return str(amount)


def decimal_text(value: str | None) -> str:
    """An exact decimal string with its trailing zeros dropped; never re-parsed and never rounded.

    The provider's own reported figure reaches the reader unchanged in magnitude. Turning `1000000`
    into `1.00M` here would silently claim a precision the report does not have.
    """

    if not value:
        return ""
    text = str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def clip(value: object, limit: int) -> str:
    """Whitespace collapsed to single spaces, then bounded with an ellipsis rather than cut mid-word.

    A figure longer than the card can hold is clipped, never dropped and never an error: an overflow
    costs the tail of one line, and the detail page holds the rest.
    """

    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "CARD_TZ_OFFSET_S",
    "CHANGE_BASIS_LABEL",
    "LINKABLE_TICKER_RE",
    "LINKABLE_VENUE_SYMBOL_RE",
    "UNKNOWN_ACCOUNT",
    "UNKNOWN_FIGURE",
    "UNKNOWN_MEASUREMENT",
    "UNKNOWN_ORIGIN",
    "UNKNOWN_VENUE",
    "change",
    "clip",
    "clock",
    "decimal_text",
    "percent_from_bps",
    "price",
    "usd_compact",
]
