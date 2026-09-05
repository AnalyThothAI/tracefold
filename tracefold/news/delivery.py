"""Reader-card rendering — the delivery contract (issue #57): one Event, one card, four lines (#113).

    header  ⚡? headline_zh            (model: one complete headline incl. the decisive fact, Chinese)
    line 1  why_zh                     (model: why it matters now and to whom, Chinese)
    line 2  利多 · 新进展 · 影响明显 · BTC ETH · CoinDesk, 2 条报道 · 14:32
            (code: direction, novelty, magnitude, tickers, source, local time)
    line 3  行情 BTC $74,553.10 24h +7.91%
            (code: the market's own number, only when a fresh quote exists — see `reader_card.quote_line`)

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

What this module does *not* own any more (#562 PR-A): the characters a number is written in (`card_format`),
the order and wording of a card's lines (`reader_card`), and Feishu's JSON (`feishu_card`). It selects the
Event's facts, decides which of them a reader is shown, and fills one `ReaderCard`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from .card_format import LINKABLE_TICKER_RE, LINKABLE_VENUE_SYMBOL_RE
from .feishu_card import feishu_card
from .market_review.pricing import parse_price, quote_change_24h_bps, return_bps
from .models import ReaderMarketMovement, ReaderTradeTarget
from .reader_card import (
    ReaderCard,
    ReaderCardFacts,
    ReaderCardHeader,
    ReaderCardLink,
    ReaderCardNote,
    ReaderCardQuote,
    ReaderCardTimes,
)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<!\w)@[\w]{1,32}")
_MARKDOWN_RE = re.compile(r"[*_`#>\[\]()]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")
_MAX_ASSETS = 4
_SOURCE_BUTTON_LABEL = "打开来源"
_ESCALATE_MARK = "⚡"
# A crypto asset is its own market and carries no proxy mark; an `equity` / `commodity` / `index` tag
# prices on a Binance TradFi perp or a Hyperliquid builder-DEX — a real traded contract (95% of a
# week's reactions found candles for them) but a proxy for a market that closes at 16:00 somewhere
# else, and the reader is told which of the two they are looking at.
_NATIVE_MARKET_CLASS = "crypto"


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


def reader_trade_targets(quotes: Sequence[Mapping[str, Any]]) -> tuple[ReaderTradeTarget, ...]:
    """Typed, catalogue-backed contract identities for adapter-only reader actions."""

    targets: list[ReaderTradeTarget] = []
    for quote in quotes[:_MAX_ASSETS]:
        if not isinstance(quote, Mapping):
            continue
        ticker = str(quote.get("requested_symbol") or "").strip()
        symbol = str(quote.get("symbol") or "").strip()
        base_symbol = str(quote.get("base_symbol") or "").strip()
        venue = str(quote.get("venue") or "")
        venue_symbol = str(quote.get("venue_symbol") or "").strip()
        quote_asset = str(quote.get("quote_asset") or "").strip()
        if venue.startswith("hl.") and venue not in {"hl.perp", "hl.spot"}:
            target_venue = "hl.builder"
        elif venue in {
            "binance.perp",
            "binance.spot",
            "hl.perp",
            "hl.spot",
            "okx.perp",
            "okx.spot",
            "lighter.perp",
            "lighter.spot",
            "bitget.perp",
            "bitget.spot",
        }:
            target_venue = venue
        else:
            continue
        if (
            LINKABLE_TICKER_RE.fullmatch(ticker) is None
            or LINKABLE_TICKER_RE.fullmatch(base_symbol) is None
            or LINKABLE_VENUE_SYMBOL_RE.fullmatch(venue_symbol) is None
            or not symbol
        ):
            continue
        if venue.startswith("binance.") and (
            ticker != base_symbol or symbol != base_symbol or venue_symbol != f"{base_symbol}{quote_asset}"
        ):
            continue
        if venue.startswith("okx.") and not venue_symbol.startswith(f"{base_symbol}-"):
            continue
        targets.append(
            ReaderTradeTarget(
                ticker=ticker,
                venue=target_venue,  # type: ignore[arg-type]
                venue_symbol=venue_symbol,
                base_symbol=base_symbol,
                quote_asset=quote_asset,
            )
        )
    return tuple(targets)


def reader_market_movements(
    assets: Sequence[str],
    quotes: Sequence[Mapping[str, Any]],
) -> tuple[ReaderMarketMovement, ...]:
    """Reader returns measured against the prices selected for this exact push.

    The fresh quote sampled immediately before send is the common endpoint. ``price_at_news`` anchors
    “新闻后”; ``price_one_hour_before_push`` anchors the trailing “1h”. Historical Event-Reaction horizons
    remain review data and never leak into these reader labels.
    """

    quote_by_ticker = {
        str(quote.get("requested_symbol") or "").strip(): quote
        for quote in quotes[:_MAX_ASSETS]
        if isinstance(quote, Mapping) and str(quote.get("requested_symbol") or "").strip()
    }
    movements: list[ReaderMarketMovement] = []
    for ticker in [str(asset).strip() for asset in assets[:_MAX_ASSETS] if str(asset).strip()]:
        quote = quote_by_ticker.get(ticker, {})
        current = parse_price(quote.get("price")) if quote.get("state") == "fresh" else None
        news_anchor = parse_price(quote.get("price_at_news"))
        hour_anchor = parse_price(quote.get("price_one_hour_before_push"))
        after_news_bps = return_bps(news_anchor, current) if current is not None and news_anchor is not None else None
        return_1h_bps = return_bps(hour_anchor, current) if current is not None and hour_anchor is not None else None
        one_hour_state: Literal["available", "unavailable"] = (
            "available" if return_1h_bps is not None else "unavailable"
        )
        movements.append(
            ReaderMarketMovement(
                ticker=ticker,
                after_news_bps=after_news_bps,
                return_1h_bps=return_1h_bps,
                change_24h_bps=quote_change_24h_bps(quote),
                one_hour_state=one_hour_state,
            )
        )
    return tuple(movements)


def reader_quotes(quotes: Sequence[Mapping[str, Any]]) -> tuple[ReaderCardQuote, ...]:
    """The quote read model's rows as card facts, bounded to the assets a card names.

    The ticker the facts line already printed, not the contract's base symbol: the two lines annotate
    the same assets and must line up. They differ for 0.34% of a week's priced assets — all issuer
    aliases (`XIAOMI` prices on `HK1810`), where the card's own ticker is the clearer of the two.
    Freshness is carried, not applied: `reader_card.quote_line` owns the rule that only a fresh quote
    reaches a reader, so every channel drops a stale one the same way.
    """

    return tuple(
        ReaderCardQuote(
            symbol=str(quote.get("requested_symbol") or quote.get("symbol") or "").strip(),
            price=str(quote.get("price") or ""),
            change_pct=(
                float(change)
                if isinstance(change := quote.get("change_pct"), int | float) and not isinstance(change, bool)
                else None
            ),
            change_basis=str(quote.get("change_basis") or "") or None,
            freshness=str(quote.get("state") or ""),
            proxy_market=str(quote.get("instrument_class") or "") != _NATIVE_MARKET_CLASS,
        )
        for quote in quotes[:_MAX_ASSETS]
        if isinstance(quote, Mapping)
    )


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


def news_reader_card(
    *,
    event: Mapping[str, Any],
    verdict: Mapping[str, Any],
    decision: str,
    grounded_assets: Sequence[str],
    assets: Sequence[str] | None = None,
    degraded: bool = False,
    quotes: Sequence[Mapping[str, Any]] = (),
) -> ReaderCard:
    """One Event's card, in facts. `quotes` are `PriceRepository.quotes_for_symbols` rows for the
    rendered assets, in that order; passing none renders exactly the v9 card, so the price is additive
    and never a precondition for delivery."""

    original_title = str(event.get("leader_title") or "")
    link = str(event.get("leader_url") or "")
    novelty: str | None = None
    if degraded:
        header_text = _wire_text(original_title, limit=100)
        lead = _wire_text(event.get("leader_description"), limit=140)
        direction: str | None = None
        magnitude: int | None = None
    else:
        direction = str(verdict.get("direction") or "unclear")
        magnitude = int(verdict.get("magnitude") or 0)
        novelty = str(verdict.get("novelty") or "") or None
        headline = sanitize_ai_text(verdict.get("headline_zh"), limit=60)
        lead = sanitize_ai_text(verdict.get("why_zh"), limit=140)
        header_text = headline or original_title
    return ReaderCard(
        header=ReaderCardHeader(
            family="news",
            subject=header_text,
            qualifier=_ESCALATE_MARK if decision == "escalate" else "",
            tone=direction or "none",  # type: ignore[arg-type]
        ),
        lead=lead,
        facts=ReaderCardFacts(
            direction=direction,
            novelty=novelty,
            magnitude=magnitude,
            tickers=tuple(assets if assets is not None else card_assets(verdict, grounded_assets)),
            source=(str(event.get("reporting_origin") or ""),),
            report_count=int(event.get("member_count") or 1),
        ),
        quotes=reader_quotes(quotes),
        link=ReaderCardLink(url=link, label=_SOURCE_BUTTON_LABEL) if link else None,
        note=ReaderCardNote(id=str(event.get("event_id", ""))),
        times=ReaderCardTimes(event_at_ms=event.get("leader_published_at_ms") or event.get("opened_at_ms")),
    )


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
    """The Event's `ReaderCard` in the wire shape the delivery ledger freezes and Feishu accepts."""

    return feishu_card(
        news_reader_card(
            event=event,
            verdict=verdict,
            decision=decision,
            grounded_assets=grounded_assets,
            assets=assets,
            degraded=degraded,
            quotes=quotes,
        )
    )


__all__ = [
    "card_assets",
    "news_reader_card",
    "reader_market_movements",
    "reader_quotes",
    "reader_trade_targets",
    "render_first_card",
    "sanitize_ai_text",
]
