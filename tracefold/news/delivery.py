"""Reader-card rendering for one News update intent (#706): frozen copy plus code-owned facts.

    header  ⚡? headline_zh             (frozen copy; ⚡ only when the plan marked the update key)
    lead    one line per selected claim (frozen copy, exactly as frozen and sent)
    facts   新增 · BTC ETH · Reuters, its report count, 14:32
            (code: the change label from the update's own change kinds, the selected claims' primary
             assets, the source, the source time)
    quotes  行情 BTC $74,553.10 24h +7.91%
            (code: the market's own number, only when a fresh quote exists -- see `reader_card.quote_line`)

The frozen body is the payload: headline and claim lines are never re-generated, clipped or cleaned here.
No model direction, novelty, fact kind, scope enum, provider score or "AI" label reaches a card. The quote
line is display, never decision: nothing here is read back by planning, coverage or the key marker.

What this module does *not* own (#562 PR-A): the characters a number is written in (`card_format`), the
order and wording of a card's lines (`reader_card`), and Feishu's JSON (`feishu_card`). It selects the
update's facts, decides which of them a reader is shown, and fills one `ReaderCard`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from .card_format import LINKABLE_TICKER_RE, LINKABLE_VENUE_SYMBOL_RE
from .market_review.pricing import parse_price, quote_change_24h_bps, return_bps
from .models import MarketAsset, ReaderMarketMovement, ReaderTradeTarget, base_symbol, market_type_of
from .reader_card import (
    CARD_ASSETS_MAX,
    ChangeLabel,
    ReaderCard,
    ReaderCardFacts,
    ReaderCardHeader,
    ReaderCardLink,
    ReaderCardNote,
    ReaderCardTimes,
    reader_quotes,
)
from .updates.contracts import Claim, EventUpdate, Evidence
from .updates.notification import FrozenCard, NotificationPlan

# How many assets a card names, owned by the card model: the facts line and the quote line are two
# views of one bound.
_MAX_ASSETS = CARD_ASSETS_MAX
_SOURCE_BUTTON_LABEL = "打开来源"
KEY_MARK = "⚡"
_CORRECTION_KINDS = frozenset({"correction"})
# A change to something the reader may already hold: a parameter, phase or scope, a source conflict, a
# change in the evidence, or a restatement. A first report, an addition and an unresolved possible
# addition are `new`.
_UPDATE_KINDS = frozenset(
    {"parameter_change", "phase_change", "scope_change", "conflict", "evidence_change", "restatement"}
)


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


def selected_claims(update: EventUpdate, claim_refs: Sequence[str]) -> tuple[Claim, ...]:
    """The update's claims a frozen card was composed for, in the card's own order."""

    by_ref = {claim.ref: claim for claim in update.claims}
    return tuple(by_ref[ref] for ref in claim_refs if ref in by_ref)


def update_card_assets(update: EventUpdate, claim_refs: Sequence[str]) -> list[MarketAsset]:
    """The typed instruments a card names: the selected claims' own primary assets, first seen first.

    Only an asset whose market is known is shown, because the ticker on the card is also its quote
    target and a symbol whose market nobody established cannot be priced without guessing (#651 §6.2).
    Mentioned assets and other claims' assets are not the card's subject.
    """

    shown: list[MarketAsset] = []
    for claim in selected_claims(update, claim_refs):
        for asset in claim.fields.assets:
            if asset.role != "primary":
                continue
            typed = MarketAsset(base_symbol(asset.symbol), market_type_of(asset.market_type))
            if typed.symbol and typed.market_type != "unknown" and typed not in shown:
                shown.append(typed)
    return shown[:_MAX_ASSETS]


def update_primary_symbols(update: EventUpdate, claim_refs: Sequence[str]) -> tuple[str, ...]:
    """Every distinct primary symbol the selected claims name, typed or not."""

    symbols: list[str] = []
    for claim in selected_claims(update, claim_refs):
        for asset in claim.fields.assets:
            symbol = base_symbol(asset.symbol)
            if asset.role == "primary" and symbol and symbol not in symbols:
                symbols.append(symbol)
    return tuple(symbols)


def update_change_label(update: EventUpdate, claim_refs: Sequence[str]) -> ChangeLabel:
    """`correction` when a selected claim corrects, else `update` when one changes a prior, else `new`."""

    selected = set(claim_refs)
    kinds = {change.kind for change in update.changes if change.current_ref in selected}
    if kinds & _CORRECTION_KINDS:
        return "correction"
    if kinds & _UPDATE_KINDS:
        return "update"
    return "new"


def _cited_evidence(update: EventUpdate, claims: Sequence[Claim]) -> list[Evidence]:
    evidence = {item.ref: item for item in update.evidence}
    cited: list[Evidence] = []
    for claim in claims:
        for citation in claim.citations:
            item = evidence.get(citation.evidence_ref)
            if item is not None and item not in cited:
                cited.append(item)
    return cited


def _source_time(item: Evidence) -> int:
    return item.source.published_at_ms or item.source.first_available_at_ms


def update_news_at_ms(update: EventUpdate, claim_refs: Sequence[str]) -> int | None:
    """When the selected content was first reported: the earliest cited source time.

    A source's own publication time when it has one, otherwise when this system first had it. Never
    the adoption or send clock, and never refreshed by a later member.
    """

    cited = _cited_evidence(update, selected_claims(update, claim_refs))
    times = [_source_time(item) for item in cited if _source_time(item) > 0]
    return min(times) if times else None


def update_report_count(update: EventUpdate, claim_refs: Sequence[str]) -> int:
    """How many distinct source items cite, support or report the selected claims."""

    selected = set(claim_refs)
    refs = {item.ref for item in _cited_evidence(update, selected_claims(update, claim_refs))}
    refs |= {
        row.evidence_ref
        for row in update.evidence_relations
        if row.claim_ref in selected and row.relation in {"supports", "reports"}
    }
    return max(1, len(refs))


def frozen_card_lead(card: FrozenCard) -> str:
    """The frozen body below its headline: the selected claims' lines, byte for byte."""

    prefix = f"{card.headline_zh}\n\n"
    if not card.body.startswith(prefix):
        raise ValueError("news_frozen_card_body_mismatch")
    return card.body[len(prefix) :]


def news_update_card(
    card: FrozenCard,
    *,
    plan: NotificationPlan,
    update: EventUpdate,
    assets: Sequence[str] | None = None,
    quotes: Sequence[Mapping[str, Any]] = (),
    untradeable: bool = False,
) -> ReaderCard:
    """One update intent's card: the frozen copy, then the code-owned facts around it.

    `assets` is what the Deliverer already resolved for the quote read; passing none renders the
    selected claims' typed primary assets. `quotes` are `PriceRepository.quotes_for_symbols` rows for
    those assets; passing none renders no quote line, so the price is additive and never a
    precondition for delivery.
    """

    if card.intent_id != plan.intent_id or plan.update_ref != update.ref:
        raise ValueError("news_card_plan_update_mismatch")
    claims = selected_claims(update, card.claim_refs)
    cited = sorted(_cited_evidence(update, claims), key=lambda item: (_source_time(item), item.ref))
    source = cited[0].source if cited else None
    origin = "" if source is None else (source.origin_id or source.publisher_id)
    link = "" if source is None else str(source.url or "")
    tickers = tuple(
        assets if assets is not None else (asset.symbol for asset in update_card_assets(update, card.claim_refs))
    )
    return ReaderCard(
        header=ReaderCardHeader(family="news", subject=card.headline_zh, qualifier=KEY_MARK if plan.key else ""),
        lead=frozen_card_lead(card),
        facts=ReaderCardFacts(
            change=update_change_label(update, card.claim_refs),
            tickers=tickers[:_MAX_ASSETS],
            source=(origin,),
            report_count=update_report_count(update, card.claim_refs),
        ),
        quotes=reader_quotes(quotes),
        link=ReaderCardLink(url=link, label=_SOURCE_BUTTON_LABEL) if link else None,
        note=ReaderCardNote(id=update.event_id),
        times=ReaderCardTimes(event_at_ms=update_news_at_ms(update, card.claim_refs)),
        untradeable=untradeable,
    )


__all__ = [
    "KEY_MARK",
    "frozen_card_lead",
    "news_update_card",
    "reader_market_movements",
    "reader_trade_targets",
    "selected_claims",
    "update_card_assets",
    "update_change_label",
    "update_news_at_ms",
    "update_primary_symbols",
    "update_report_count",
]
