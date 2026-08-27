"""Reader-card rendering — the delivery contract (issue #57): one Event, one card, four lines (#113).

    header  ⚡? headline_zh            (model: one complete headline incl. the decisive fact, Chinese)
    line 1  why_zh                     (model: why it matters now and to whom, Chinese)
    line 2  利多 · 新进展 · 影响明显 · BTC ETH · CoinDesk, 2 条报道 · 14:32
            (code: direction, novelty, magnitude, tickers, source, local time)
    line 3  行情 BTC $74,553.10 24h +7.91%
            (code: the market's own number, only when a fresh quote exists — see `_quote_line`)

No original headline, no translated title, no scope/type enums, no provider score, no "AI" label, no follow-up
card. Pipeline internals live in the console and `tracefold news why`.

Line 3 is the only place a price reaches a reader, and it is **display, never decision** (#88/#113): nothing
here is read back by the Gate, Triage, `decide()`, a storyline key, the ⚡ header or any ranking. It is a
separate line rather than a chunk of the facts line on purpose — the market's number and the model's judgment
are different kinds of claim and must not blur into one another. On a week of live cards 68.7% carried a fresh
quote; the rest simply have no line, because a stale or absent price is worse than none.

Degraded Events (the model chain failed and the rule baseline still pushes) get the wire text instead of a
verdict view (issue #65): the header is the original headline, the body is the original description when there
is one, and the facts line carries only tickers / source / time — no direction, magnitude or novelty the model
never judged, and no "model unavailable" copy in the reader's face. The quote line still renders: the price is
our own fact and does not depend on the model having answered.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import isfinite
from typing import Any

from .market_review.pricing import parse_price
from .oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from .outcome import DIRECTION_ZH, MAGNITUDE_ZH, NOVELTY_ZH

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<!\w)@[\w]{1,32}")
_MARKDOWN_RE = re.compile(r"[*_`#>\[\]()]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")

_DIRECTION_COLOR = {"bullish": "green", "bearish": "red", "neutral": "grey", "unclear": "grey"}
_MAX_ASSETS = 4
_CARD_TZ_OFFSET_S = 8 * 3600  # the reader's clock (Asia/Shanghai); the source timestamp is UTC ms

# The window a change percentage was measured over, said in the reader's words. Never hard-coded to "24h":
# Binance publishes a rolling 24 h window and Hyperliquid the venue's own day, and about 8% of a week's card
# assets price on Hyperliquid. A basis we cannot name means the percentage is dropped, not guessed — the price
# still renders.
_CHANGE_BASIS_LABEL = {"rolling_24h": "24h", "provider_day": "日内"}
# A basis `pricing` knows and this map does not would drop every percentage for that venue in silence, so the
# two key sets are pinned together in `test_card_change_basis_labels_cover_the_price_domain`.
# The mark is about whose market this number comes from, not about the contract type — BTC also prices on a
# Binance perpetual, and for a crypto asset that *is* its own market, so it carries no mark. An `equity` /
# `commodity` / `index` tag prices on a Binance TradFi perp or a Hyperliquid builder-DEX: a real traded
# contract (95% of a week's reactions found candles for them) but a proxy for a market that closes at 16:00
# somewhere else, and the reader is told which of the two they are looking at.
_NATIVE_MARKET_CLASS = "crypto"
_PERP_MARK = "（永续）"
_QUOTE_LINE_PREFIX = "行情 "


def sanitize_ai_text(value: object, *, limit: int, fallback: str = "") -> str:
    """Deterministic clean of model text; any surviving URL falls back to the code-owned fallback."""

    raw = str(value or "")
    if _URL_RE.search(raw):
        return fallback[:limit]
    cleaned = _CONTROL_RE.sub(" ", raw)
    cleaned = _HANDLE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    return (cleaned or fallback)[:limit]


def _wire_text(value: object, *, limit: int) -> str:
    """Provider text for a degraded card: control characters, markdown and whitespace cleaned, URLs kept out of the
    header/body (the source button carries the link)."""

    cleaned = _URL_RE.sub("", _CONTROL_RE.sub(" ", str(value or "")))
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    return _SPACE_RE.sub(" ", cleaned).strip()[:limit]


def _format_price(value: object) -> str:
    """A provider price as the characters the reader sees.

    Deliberately the same rule as the console's `formatPrice` (`web/src/features/news/model/newsPrice.ts`):
    >= 1000 keeps two decimals and thousands separators, >= 1 keeps up to four, below 1 up to six, and trailing
    zeros are dropped. The two surfaces must agree character for character — a reader who sees 74,553.10 on a
    card and 74,553.1 in the console has been given a reason to doubt both. Editing one without the other is
    the drift this comment exists to prevent.

    A price that rounds away to zero is not rendered at all; `""` means the caller drops the whole entry. So
    does a number too large to quantize: `parse_price` bounds a price to "finite and positive", not to a
    magnitude, and `Decimal("1e40").quantize(...)` raises rather than returning characters. This function is
    called from the renderer, outside `_card_quotes`'s guard, so it must never raise — an unrenderable price
    costs its own entry, never the card.
    """

    price = parse_price(value)
    if price is None:
        return ""
    try:
        if price >= 1000:
            return f"{price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,f}"
        places = Decimal("0.0001") if price >= 1 else Decimal("0.000001")
        text = f"{price.quantize(places, rounding=ROUND_HALF_UP):,f}"
    except (InvalidOperation, ValueError):
        return ""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "" if text == "0" else text


def _format_change(value: object, basis: object) -> str:
    """`24h +7.91%` — a percentage is never shown without the window it was measured over.

    Two decimals, matching the console's `formatChangePct`. An unknown or missing basis returns `""`: the
    price still renders, the number whose meaning we cannot state does not.
    """

    label = _CHANGE_BASIS_LABEL.get(str(basis or ""))
    if label is None or isinstance(value, bool) or not isinstance(value, int | float):
        return ""
    pct = float(value)
    if not isfinite(pct):  # NaN / inf never reach a reader
        return ""
    return f"{label} {'+' if pct > 0 else ''}{pct:.2f}%"


def _quote_line(quotes: Sequence[Mapping[str, Any]]) -> str:
    """The market's own number for the assets already named on the facts line, or nothing at all.

    Only `fresh` quotes render (the freshness rule is `pricing.quote_state`, not this module's). A `stale`,
    `unavailable` or `unlisted` answer leaves no line, no placeholder and no zero — #88's whole point is that
    "we have not managed to quote this" and "this is worth nothing" are different sentences.

    The mark is attached to each proxy-priced asset rather than said once for the line. Saying it once reads
    as ambiguous the moment a line mixes the two: a trailing mark after `BTC ... · SAMSUNG ...` could belong
    to SAMSUNG or to both, and the reader has no way to tell. Repetition is the cheaper mistake.
    """

    parts: list[str] = []
    classes: list[str] = []
    for quote in quotes[:_MAX_ASSETS]:
        if not isinstance(quote, Mapping) or str(quote.get("state") or "") != "fresh":
            continue
        price = _format_price(quote.get("price"))
        if not price:
            continue
        # The ticker the facts line already printed, not the contract's base symbol: the two lines annotate the
        # same assets and must line up. They differ for 0.34% of a week's priced assets — all issuer aliases
        # (`XIAOMI` prices on `HK1810`), where the card's own ticker is the clearer of the two names.
        symbol = str(quote.get("requested_symbol") or quote.get("symbol") or "").strip()
        if not symbol:
            continue
        change = _format_change(quote.get("change_pct"), quote.get("change_basis"))
        # Space inside an entry, ` · ` between them. A middot in both places reads as four assets when there
        # are two: `AAPL $312.56 · 24h -1.25% · AMZN $260.77` has no visible seam.
        parts.append(f"{symbol} ${price} {change}" if change else f"{symbol} ${price}")
        classes.append(str(quote.get("instrument_class") or ""))
    if not parts:
        return ""
    for index, klass in enumerate(classes):
        if klass != _NATIVE_MARKET_CLASS:
            parts[index] += _PERP_MARK
    return _QUOTE_LINE_PREFIX + " · ".join(parts)


def card_assets(verdict: Mapping[str, Any], grounded_assets: Sequence[str]) -> list[str]:
    """Assets shown on the card: the verdict's primary assets that the Gate grounded (code fact ∩ model claim);
    when the model named no grounded primary, the grounded assets themselves — never provider noise alone."""

    grounded = {str(a).upper().replace("XYZ-", "") for a in grounded_assets}
    primaries = [
        str(a.get("symbol") or "").upper().replace("XYZ-", "")
        for a in (verdict.get("assets") or [])
        if isinstance(a, Mapping) and a.get("role") == "primary"
    ]
    shown = [s for s in dict.fromkeys(primaries) if s in grounded]
    if not shown and len(grounded) <= _MAX_ASSETS:
        shown = sorted(grounded)
    return shown[:_MAX_ASSETS]


def reader_assets(
    *,
    event_kind: str,
    verdict: Mapping[str, Any],
    grounded_assets: Sequence[str],
    program_version: str = "",
    verdict_program_sha256: str,
    expected_program_sha256: str,
    oi_signal: Mapping[str, Any] | None = None,
) -> list[str]:
    """Code-verified assets that the facts and quote lines may name.

    Ordinary News keeps the Gate-grounded intersection. OI telemetry has no provider coin tag, so its
    deterministic parser's stored rank-ledger row is the grounding fact; all three independent markers must
    agree before the symbol is exposed to the reader.
    """

    ordinary = card_assets(verdict, grounded_assets)
    if (
        event_kind != "oi"
        or str(program_version or "") != OI_PROGRAM_VERSION
        or not verdict_program_sha256
        or verdict_program_sha256 != expected_program_sha256
        or not isinstance(oi_signal, Mapping)
    ):
        return ordinary
    symbol = str(oi_signal.get("symbol") or "").strip().upper().removeprefix("XYZ-")
    primaries = {
        str(asset.get("symbol") or "").strip().upper().removeprefix("XYZ-")
        for asset in (verdict.get("assets") or [])
        if isinstance(asset, Mapping) and asset.get("role") == "primary"
    }
    return [symbol] if symbol and symbol in primaries else ordinary


def _facts_line(
    *,
    direction: str | None,
    magnitude: int | None,
    novelty: str | None,
    assets: Sequence[str],
    source: str,
    members: int,
    at_ms: int | None,
) -> str:
    parts: list[str] = []
    if direction is not None and magnitude is not None:
        parts.append(DIRECTION_ZH.get(direction, direction))
        # 28.8% of a week's cards advanced a story the reader already had one for, and the card said nothing
        # about it (#113). `新进展` is the model's own `novelty`, not a count: policy v7 already withheld the
        # near-duplicates, so what survives here is a genuine next step the reader can read as a delta.
        if novelty == "progression":
            parts.append(NOVELTY_ZH["progression"])
        parts.append(MAGNITUDE_ZH.get(magnitude, str(magnitude)))
    if assets:
        parts.append(" ".join(assets))
    origin = source or "-"
    parts.append(f"{origin}（{members} 条报道）" if members > 1 else origin)
    if at_ms:
        parts.append(time.strftime("%H:%M", time.gmtime(int(at_ms) / 1000 + _CARD_TZ_OFFSET_S)))
    return " · ".join(parts)


def render_first_card(
    *,
    event: Mapping[str, Any],
    verdict: Mapping[str, Any],
    decision: str,
    grounded_assets: Sequence[str],
    assets: Sequence[str] | None = None,
    degraded: bool = False,
    quotes: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """`quotes` are `PriceRepository.quotes_for_symbols` rows for the rendered assets, in that order.

    Passing none renders exactly the v9 card, so the price is additive and never a precondition for delivery.
    """

    original_title = str(event.get("leader_title") or "")
    link = str(event.get("leader_url") or "")
    novelty: str | None = None
    if degraded:
        header_text = _wire_text(original_title, limit=100)
        why = _wire_text(event.get("leader_description"), limit=140)
        direction: str | None = None
        magnitude: int | None = None
    else:
        direction = str(verdict.get("direction") or "unclear")
        magnitude = int(verdict.get("magnitude") or 0)
        novelty = str(verdict.get("novelty") or "") or None
        headline = sanitize_ai_text(verdict.get("headline_zh"), limit=60)
        why = sanitize_ai_text(verdict.get("why_zh"), limit=140)
        # An empty title_zh means "same as headline_zh" (#101), so it is a fallback only when headline_zh
        # sanitised away — a URL in it, say. Then the wire title is the honest last resort, as before.
        header_text = headline or sanitize_ai_text(verdict.get("title_zh"), limit=120) or original_title
    header_title = f"{'⚡ ' if decision == 'escalate' else ''}{header_text}"
    lines: list[str] = []
    if why:
        lines.append(why)
    lines.append(
        _facts_line(
            direction=direction,
            magnitude=magnitude,
            novelty=novelty,
            assets=list(assets) if assets is not None else card_assets(verdict, grounded_assets),
            source=str(event.get("reporting_origin") or ""),
            members=int(event.get("member_count") or 1),
            at_ms=event.get("leader_published_at_ms") or event.get("opened_at_ms"),
        )
    )
    market = _quote_line(quotes)
    if market:
        lines.append(market)
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(lines)}]
    if link:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开来源"},
                        "type": "default",
                        "url": link,
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"Tracefold · {event.get('event_id', '')[:8]}"}],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title[:100]},
            "template": _DIRECTION_COLOR.get(direction or "", "grey"),
        },
        "elements": elements,
    }


__all__ = ["card_assets", "reader_assets", "render_first_card", "sanitize_ai_text"]
