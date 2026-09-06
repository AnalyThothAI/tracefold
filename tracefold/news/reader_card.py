"""One reader card, as facts: what a card says, before any channel decides how to show it (#562 §3).

Every card a reader receives -- the News first card and the four market families -- is one
`ReaderCard`. The two renderers fill it; a channel serializer turns it into that channel's own wire
shape. Before this value object each renderer built Feishu JSON directly and the Telegram adapter
parsed that JSON back into text, so "what does a card say" had three answers and the market card lost
its event time on the way through the third.

What lives here is the card's *own* language: the order of its lines, the words that stand for a
direction or an action, the separators between facts. What does not live here is any channel's
vocabulary -- no colour name, no icon, no button element, no JSON -- and no I/O of any kind. The
number, time and clipping rules are `card_format`'s, and are not restated.

`body_lines` is a text projection of the same facts, not a second model: a channel that renders the
structured fields itself (Telegram, #562 PR-C) reads `facts`, `quotes` and `market` instead, and the
two can never disagree because the projection is computed from those fields.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from . import card_format as fmt
from .market_contracts import MARKET_NEWS_PUSHED_MAX, MARKET_NEWS_WINDOW_MS
from .outcome import DIRECTION_ZH, MAGNITUDE_ZH, NOVELTY_ZH

CardFamily = Literal["news", "oi", "liquidation", "smart_money"]
# The model's own judgment about the news, and `none` for a card that carries no judgment at all --
# a degraded News card or any market card. A channel maps `family + tone` to its colour or icon.
CardTone = Literal["bullish", "bearish", "neutral", "unclear", "none"]

# One card's header is bounded by what every channel will show without folding.
TITLE_MAX: Final = 100
# How many assets one card names, on the facts line and on the quote line alike. A card is a summary;
# the fifth asset is on the page it links to.
CARD_ASSETS_MAX: Final = 4
# One already-pushed News headline, on an OI card that is about the same instrument. Shorter than the
# card's own title bound because these lines are context under the card's subject, not its subject:
# four lines of full headlines would make the OI numbers the smaller half of their own card (#582).
NEWS_HEADLINE_MAX: Final = 40

# The family's word, for the families whose card is not headed by its own headline.
FAMILY_TITLE: Final[dict[str, str]] = {
    "oi": "持仓异动",
    "liquidation": "强平",
    "smart_money": "聪明钱",
}
# OI's own direction vocabulary. Deliberately not the verdict's `DIRECTION_ZH`: an open-interest
# change rises or falls, it is not bullish or bearish, and a market card claims no judgment.
OI_DIRECTION_ZH: Final[dict[str, str]] = {"rise": "上升", "fall": "下降"}
ACTION_ZH: Final[dict[str, str]] = {"open": "开", "close": "平"}
SIDE_ZH: Final[dict[str, str]] = {"long": "多", "short": "空"}

QUOTE_LINE_PREFIX: Final = "行情 "
# A crypto asset is its own market and carries no proxy mark; an `equity` / `commodity` / `index` tag
# prices on a Binance TradFi perp or a Hyperliquid builder-DEX -- a real traded contract (95% of a
# week's reactions found candles for them) but a proxy for a market that closes at 16:00 somewhere
# else, and the reader is told which of the two they are looking at.
_NATIVE_MARKET_CLASS: Final = "crypto"
# The mark is about whose market this number comes from, not about the contract type -- BTC also
# prices on a Binance perpetual, and for a crypto asset that *is* its own market, so it carries no
# mark. A proxy-priced asset prices on a Binance TradFi perp or a Hyperliquid builder-DEX: a real
# traded contract but a proxy for a market that closes at 16:00 somewhere else, and the reader is
# told which of the two they are looking at. It is attached to each proxy entry rather than said once
# for the line: a trailing mark after `BTC ... · SAMSUNG ...` could belong to either.
_PERP_MARK: Final = "（永续）"

# What an authoritative five-venue absence puts on a card that already reached its reader. It is the
# card's own sentence rather than one channel's, because it replaced *deleting* the message (#562 §5
# row 5): a reader who saw a story about a name they cannot trade is better served by being told so
# than by watching the card vanish, and both channels have to tell them the same thing.
UNTRADEABLE_NOTICE_ZH: Final = "未找到可交易标的"

# Two prices can appear on one market card and they are different claims. `来源报告价` is the number
# inside the provider's own report -- the price it says the liquidation or the account action happened
# at. `行情` is what the market is quoting now, read fresh from the same quote snapshots the News
# card reads. Separate lines and distinct labels, because a reader who takes one for the other has
# been told the market moved when only the report was old (#562 §3). One number shape, though: both
# are `card_format.money`, so the only difference a reader sees between the two lines is the
# difference between the two claims.
_REPORTED_PRICE_PREFIX: Final = "来源报告价 "
_PNL_PREFIX: Final = "已实现 PNL "
# NewsLiquid's own two published percentages, in its own terms. `Whale Long Profit` is that
# percentage and nothing more -- not "every whale account is in profit", not a dollar PnL, not an
# account count; `oi_signals.OiSourceContract` holds the full sentence.
_WHALE_PROFIT_PREFIX: Final = "鲸鱼多头盈利 "
# `占比` would claim a share of a whole, and this number is routinely above 100% (143.9% in the
# production frame this repository tests against). It is the provider's `Whale/OI Ratio`: two
# quantities divided, said as such.
_WHALE_RATIO_PREFIX: Final = "鲸鱼持仓/OI "
# What the reader has already been told about this instrument, in the same window the port queried
# (#582 §3.3). The window is printed from the constant the statements bound themselves with, so the
# `48h` here and the numbers beside it are one claim. Titles only, and only of cards that were
# actually pushed: an Event nobody was told about is counted in the total and never quoted, because a
# headline on this line reads as "you have seen this" and an unpushed one would be a lie.
_NEWS_PREFIX: Final = f"相关新闻 {MARKET_NEWS_WINDOW_MS // 3_600_000}h"
_NEWS_HEADLINE_MARK: Final = "· "

_LIQUIDATION_NOTE: Final = "各来源报告金额不相加：没有可信底层成交标识时只列报告数与最大单笔。"
# What a `平` on this card does and does not mean (#553 §4.4). It is printed only by a card that
# printed a Close: on an open-only card it explained a word that is not there, which is how a caveat
# stops being read on the cards that do need it.
_SMART_MONEY_NOTE: Final = "Close 只表示来源报告的平仓/减仓动作，不代表账户已全部清仓。"
_SMART_MONEY_UNVERIFIED: Final = "（来源标签，非已核实地址）"

_NOTE_PREFIX: Final[dict[str, str]] = {"news": "Tracefold", "market": "Tracefold 市场"}
_NOTE_ID_MAX: Final[dict[str, int]] = {"news": 8, "market": 24}


@dataclass(frozen=True, slots=True)
class ReaderCardHeader:
    """Who this card is about. `subject` is the News headline or the market instrument."""

    family: CardFamily
    subject: str = ""
    qualifier: str = ""
    tone: CardTone = "none"


@dataclass(frozen=True, slots=True)
class ReaderCardFacts:
    """The code-owned line: what kind of claim this is, about what, from whom, when.

    `source` is the origin in the parts a card joins with a space -- one reporting origin for News,
    the provider and the market kind for a market card -- so neither renderer has to pre-join it.
    """

    direction: str | None = None
    novelty: str | None = None
    magnitude: int | None = None
    tickers: tuple[str, ...] = ()
    source: tuple[str, ...] = ()
    report_count: int = 1


@dataclass(frozen=True, slots=True)
class ReaderCardQuote:
    """One asset's market number as the quote read model answered it, unrounded and unfiltered.

    `freshness` is carried rather than applied by the caller so every channel drops a stale quote by
    the same rule: only `fresh` renders, and `stale` / `unavailable` / `unlisted` leave no line, no
    placeholder and no zero -- "we have not managed to quote this" and "this is worth nothing" are
    different sentences (#88).
    """

    symbol: str
    price: str
    change_pct: float | None = None
    change_basis: str | None = None
    # No default freshness, and deliberately not `"fresh"`: a quote whose state nobody stated has not
    # been shown to be current, and the one thing this field decides is whether a number reaches a
    # reader. `reader_quotes` always carries the read model's own answer.
    freshness: str = ""
    proxy_market: bool = False


@dataclass(frozen=True, slots=True)
class ReaderCardAction:
    """One reported account action, and the notional the report attached to it."""

    action: str | None = None
    side: str | None = None
    notional: str | None = None

    @property
    def label(self) -> str:
        return f"{ACTION_ZH.get(self.action or '', self.action or '')}{SIDE_ZH.get(self.side or '', self.side or '')}"


@dataclass(frozen=True, slots=True)
class ReaderCardHeadline:
    """One News card this reader already received, as the title they saw and when they saw it."""

    headline: str = ""
    at_ms: int = 0


@dataclass(frozen=True, slots=True)
class ReaderCardMarket:
    """The market families' own facts. Every field is what a provider reported, never a derived view."""

    kind: str = ""
    venue: str | None = None
    measurement: str | None = None
    direction: str | None = None
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    side: str | None = None
    notional: str = ""
    # The two OI columns the provider publishes beside the change and the value. Absent is absent: a
    # frame that carries neither prints no line rather than two unknown placeholders.
    whale_long_profit_bps: int | None = None
    whale_oi_ratio_bps: int | None = None
    # What the report itself said the price and the realised PNL were. Never a quote.
    reported_price: str = ""
    pnl: str = ""
    account: str = ""
    account_verified: bool = False
    actions: tuple[ReaderCardAction, ...] = ()
    action_changes: int = 0
    opened_action: ReaderCardAction = ReaderCardAction()
    latest_action: ReaderCardAction = ReaderCardAction()
    # What News has already said about this instrument inside the card's own news window: the titles
    # of the cards this reader received, and how many editorial Events named the instrument at all.
    # Display-only, like the quote beside it, and absent is absent -- a card that could not be told
    # carries the empty default and prints nothing rather than `已推 0 · 共 0` (#582 §3.3).
    news_pushed: tuple[ReaderCardHeadline, ...] = ()
    news_total: int = 0

    def reports_close(self) -> bool:
        """Whether a Close is among the actions this card actually printed.

        `actions` is the action line itself; the timeline's two ends are read only when the card
        printed them, which is when the account changed action at all. An action the card did not
        show cannot be the one a caveat below it is about.
        """

        timeline = (self.opened_action, self.latest_action) if self.action_changes else ()
        return any(entry.action == "close" for entry in (*self.actions, *timeline))


@dataclass(frozen=True, slots=True)
class ReaderCardLink:
    url: str
    label: str


@dataclass(frozen=True, slots=True)
class ReaderCardNote:
    """The identity line. `detail_id` is what an operator looks up when the card carries no link."""

    id: str = ""
    detail_id: str = ""


@dataclass(frozen=True, slots=True)
class ReaderCardTimes:
    """Event time on the reader's clock. `span_from_ms` opens the range a summary card covers."""

    event_at_ms: int | None = None
    span_from_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReaderCard:
    """One card, in facts. Pure: it holds no channel vocabulary and performs no I/O."""

    header: ReaderCardHeader
    lead: str = ""
    facts: ReaderCardFacts = field(default_factory=ReaderCardFacts)
    quotes: tuple[ReaderCardQuote, ...] = ()
    market: ReaderCardMarket = field(default_factory=ReaderCardMarket)
    link: ReaderCardLink | None = None
    note: ReaderCardNote = field(default_factory=ReaderCardNote)
    times: ReaderCardTimes = field(default_factory=ReaderCardTimes)
    # Set by the enrichment edit when the venue catalogues answered, authoritatively, that nothing on
    # this card can be traded. It leads the card, above the copy, in every channel.
    untradeable: bool = False

    def title(self) -> str:
        """The header text, bounded.

        A News card is headed by its own headline, with the escalation qualifier in front of it. A
        market card is headed by the family, the qualifier that applies, and the instrument when one
        is known -- every part optional except the family, and the separator belonging to the join
        rather than to any one part. Production headed its first unstructured cards
        `市场原文· 原文 —`: that report named no instrument, and the missing space, the qualifier's
        own separator and the `—` placeholder were three pieces of punctuation standing in for a word
        that was never going to be there (#553). An absent field is absent -- a smart-money card whose
        instrument the parser did not normalize is headed `聪明钱` and nothing else.
        """

        if self.header.family == "news":
            text = f"{self.header.qualifier} {self.header.subject}" if self.header.qualifier else self.header.subject
        else:
            text = " · ".join(
                part
                for part in (
                    FAMILY_TITLE.get(self.header.family, "市场"),
                    self.header.qualifier,
                    self.header.subject,
                )
                if part
            )
        return text[:TITLE_MAX]

    def note_text(self) -> str:
        """`Tracefold · <id>`, plus the detail id when there is no link to reach the page with."""

        scope = "news" if self.header.family == "news" else "market"
        note = f"{_NOTE_PREFIX[scope]} · {self.note.id[: _NOTE_ID_MAX[scope]]}"
        return f"{note} · {self.note.detail_id}" if self.link is None and self.note.detail_id else note

    def body_lines(self) -> tuple[str, ...]:
        """The card's reader-visible lines, in order, with the empty ones already dropped."""

        notice = UNTRADEABLE_NOTICE_ZH if self.untradeable else ""
        if self.header.family == "news":
            lines = [notice, self.lead, self._facts_line(), quote_line(self.quotes)]
        else:
            lines = [notice, *self.market_lines(), self._facts_line()]
        return tuple(line for line in lines if line)

    # -- the card's own words ---------------------------------------------------------------------
    #
    # A channel that lays the facts out itself instead of taking `body_lines` (Telegram, #562 PR-C)
    # still writes them in the card's vocabulary rather than a table of its own: the adapter used to
    # keep `影响明显` -> `明显` and a set of direction words next to a regex that read them back out
    # of rendered text, so a word could be renamed here and silently stop being recognised there.

    def direction_word(self) -> str:
        """`利多`, or nothing at all: a degraded card and every market card claim no direction."""

        direction = self.facts.direction
        return DIRECTION_ZH.get(direction, direction) if direction is not None else ""

    def magnitude_word(self) -> str:
        """`影响明显`. `0` is a magnitude the model stated, not a missing one."""

        magnitude = self.facts.magnitude
        return MAGNITUDE_ZH.get(magnitude, str(magnitude)) if magnitude is not None else ""

    # -- line composition -------------------------------------------------------------------------

    def _facts_line(self) -> str:
        """`利多 · 新进展 · 影响明显 · BTC ETH · CoinDesk`, its report count, and `14:32`.

        A market card names no direction, novelty or magnitude -- it carries no model judgment -- and
        always states its report count, because "how many reports is this one card standing for" is
        the whole of what a summary card promises. A News card states the count only when it stands
        for more than one report, where a count of one would be noise.
        """

        facts, market = self.facts, self.header.family != "news"
        parts: list[str] = []
        if facts.direction is not None and facts.magnitude is not None:
            parts.append(self.direction_word())
            # 28.8% of a week's cards advanced a story the reader already had one for, and the card
            # said nothing about it (#113). `新进展` is the model's own `novelty`, not a count.
            if facts.novelty == "progression":
                parts.append(NOVELTY_ZH["progression"])
            parts.append(self.magnitude_word())
        if facts.tickers:
            parts.append(" ".join(facts.tickers))
        origin = " ".join(part for part in facts.source if part) or fmt.UNKNOWN_ORIGIN
        parts.append(f"{origin}（{facts.report_count} 条报道）" if market or facts.report_count > 1 else origin)
        if market or self.times.event_at_ms:
            parts.append(fmt.clock(self.times.event_at_ms or 0))
        return " · ".join(parts)

    def market_lines(self) -> list[str]:
        """The market families' own lines, without the facts line every family ends on.

        Public because a channel that builds its own footer needs the body without it (#562 PR-C).

        Where the quote goes is a family decision, not a card-wide one (#562 PR-B). An OI card's
        quote annotates the instrument its whole body is about, so it follows the measurement. A
        liquidation or smart-money card first states what the *report* said -- its price, its PNL --
        and the quote comes after as the comparison, never above the number it is compared with.

        Absent lines are dropped here rather than by each caller. Every family has lines that exist
        only when the report carried the fact -- a quote, a reported price, a whale pair, a largest
        figure, an action timeline -- and a channel that joined the raw list would print a blank line
        where the missing fact would have been.
        """

        return [line for line in self._market_lines() if line]

    def _market_lines(self) -> list[str]:
        market, span, quote = self.market, self._span(), quote_line(self.quotes)
        venue = market.venue or fmt.UNKNOWN_VENUE
        if self.header.family == "oi":
            direction = OI_DIRECTION_ZH.get(market.direction or "", market.direction or "")
            head = f"{direction} {fmt.percent_from_bps(market.oi_change_bps)}"
            detail = (
                f"OI ${fmt.usd_compact(market.oi_value_usd)} · {venue} · "
                f"{market.measurement or fmt.UNKNOWN_MEASUREMENT}"
            )
            return [f"{head} · {span}" if span else head, detail, quote, self._whale_line(), *self._news_lines()]
        if self.header.family == "liquidation":
            side = market.side or ""
            return [
                f"{venue} · {SIDE_ZH.get(side, side)}单被强平 {self.facts.report_count} 笔 · {span}",
                f"最大单笔来源报告金额 {largest}" if (largest := fmt.money(market.notional)) else "",
                self._reported_line(),
                quote,
                _LIQUIDATION_NOTE,
            ]
        if self.header.family == "smart_money":
            account = market.account or fmt.UNKNOWN_ACCOUNT
            verified = "" if market.account_verified else _SMART_MONEY_UNVERIFIED
            # "首" is where the described timeline starts, which is what the last delivered card
            # ended on when there is one. Reading it off the first covered observation would print
            # `开空 → 开空` for a card whose whole subject is that the account stopped closing shorts.
            change_line = (
                f"动作变化 {market.action_changes} 次 · "
                f"首 {market.opened_action.label} → 末 {market.latest_action.label}"
                if market.action_changes
                else ""
            )
            return [
                f"{account}{verified} · {venue} · {span}",
                change_line,
                " · ".join(
                    f"{entry.label} {notional}" if (notional := fmt.money(entry.notional)) else entry.label
                    for entry in market.actions
                ),
                self._reported_line(),
                quote,
                _SMART_MONEY_NOTE if market.reports_close() else "",
            ]
        # Every family that has market lines is above. A News card has none and never asks: its body
        # is its own lead and facts line, and a market family that stopped existing prints nothing
        # rather than a line about a card that is not being sent (#582 §3.2).
        return []

    def _reported_line(self) -> str:
        """`来源报告价 $3,120.50 · 已实现 PNL -$412.75`, or nothing when the report carried neither.

        Both figures are `card_format.money`'s, the same rule the quote line under them uses: these
        two lines sit one above the other and are read against each other, so they cannot be written
        in two number systems.
        """

        market = self.market
        parts = [
            f"{_REPORTED_PRICE_PREFIX}{text}" if (text := fmt.money(market.reported_price)) else "",
            f"{_PNL_PREFIX}{pnl}" if (pnl := fmt.money(market.pnl)) else "",
        ]
        return " · ".join(part for part in parts if part)

    def _news_lines(self) -> list[str]:
        """`相关新闻 48h · 已推 2 · 共 5`, then one line per already-pushed headline. Or nothing.

        The count line is printed only when the window held an editorial Event at all: `已推 0 · 共 0`
        is four extra bytes to say that a token had no news, which is the ordinary case for most of
        the 43 instruments a day's OI cards name (#582 §1). A total without any pushed card is a real
        answer and does print -- "five Events, none of which you were told about" is exactly what a
        reader of an OI card wants to know, and it is why the two numbers are separate.

        The headlines are the cards the reader actually received, newest first, each clipped to
        `NEWS_HEADLINE_MAX` with the time they were pushed. No link and no button: this line is
        context on a card about an instrument, and a reader who wants the story opens the console.
        """

        market = self.market
        if market.news_total <= 0:
            return []
        counts = f"{_NEWS_PREFIX} · 已推 {len(market.news_pushed)} · 共 {market.news_total}"
        return [
            counts,
            *(
                f"{_NEWS_HEADLINE_MARK}{fmt.clip(item.headline, NEWS_HEADLINE_MAX)} {fmt.clock(item.at_ms)}"
                for item in market.news_pushed
                if item.headline
            ),
        ]

    def _whale_line(self) -> str:
        """The OI frame's two whale percentages, each printed only if the frame carried it."""

        market = self.market
        parts = [
            f"{_WHALE_PROFIT_PREFIX}{fmt.percent_from_bps(market.whale_long_profit_bps)}"
            if market.whale_long_profit_bps is not None
            else "",
            f"{_WHALE_RATIO_PREFIX}{fmt.percent_from_bps(market.whale_oi_ratio_bps)}"
            if market.whale_oi_ratio_bps is not None
            else "",
        ]
        return " · ".join(part for part in parts if part)

    def _span(self) -> str:
        if self.times.event_at_ms is None:
            return ""
        last = self.times.event_at_ms
        first = self.times.span_from_ms if self.times.span_from_ms is not None else last
        return fmt.clock(first) if first == last else f"{fmt.clock(first)}–{fmt.clock(last)}"


def reader_quotes(quotes: Sequence[Mapping[str, Any]]) -> tuple[ReaderCardQuote, ...]:
    """The quote read model's rows as card facts, bounded to the assets a card names.

    One mapping for every card a reader receives: the News first card fills it from the assets its
    verdict grounded, and the market card from the instrument its observation names (#562 PR-B). The
    ticker the facts line already printed, not the contract's base symbol: the two lines annotate the
    same assets and must line up. They differ for 0.34% of a week's priced assets -- all issuer
    aliases (`XIAOMI` prices on `HK1810`), where the card's own ticker is the clearer of the two.
    Freshness is carried, not applied: `quote_line` owns the rule that only a fresh quote reaches a
    reader, so every channel drops a stale one the same way.
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
        for quote in quotes[:CARD_ASSETS_MAX]
        if isinstance(quote, Mapping)
    )


def reader_news(payload: Mapping[str, Any]) -> tuple[tuple[ReaderCardHeadline, ...], int]:
    """The News port's answer as card facts, bounded to what a card may print (#582 §3.3).

    The twin of `reader_quotes`, for the same reason and in the same place: the loop reads a mapping
    from a port and this is the one function that turns it into the card's own values, so no caller
    has to know the read model's key names. Everything it cannot read is absent rather than wrong -- a
    row without a title is dropped, a total that is not a number is zero -- because this line is
    display and a broken read costs it and nothing else.
    """

    rows = payload.get("pushed")
    pushed = tuple(
        ReaderCardHeadline(headline=headline, at_ms=int(row.get("at_ms") or 0))
        for row in (rows if isinstance(rows, Sequence) and not isinstance(rows, str | bytes) else ())
        if isinstance(row, Mapping) and (headline := str(row.get("headline_zh") or "").strip())
    )
    total = payload.get("total")
    counted = int(total) if isinstance(total, int) and not isinstance(total, bool) else 0
    return pushed[:MARKET_NEWS_PUSHED_MAX], max(counted, 0)


def quote_line(quotes: Sequence[ReaderCardQuote]) -> str:
    """The market's own number for the assets already named on the facts line, or nothing at all.

    A separate line rather than a chunk of the facts line on purpose (#88/#113): the market's number
    and the model's judgment are different kinds of claim and must not blur into one another. A space
    inside an entry and ` · ` between them -- a middot in both places reads as four assets when there
    are two: `AAPL $312.56 · 24h -1.25% · AMZN $260.77` has no visible seam.
    """

    entries: list[str] = []
    proxies: list[bool] = []
    for quote in quotes:
        if quote.freshness != "fresh":
            continue
        price = fmt.price(quote.price)
        if not price or not quote.symbol:
            continue
        change = fmt.change(quote.change_pct, quote.change_basis)
        entries.append(f"{quote.symbol} ${price} {change}" if change else f"{quote.symbol} ${price}")
        proxies.append(quote.proxy_market)
    if not entries:
        return ""
    for index, proxy in enumerate(proxies):
        if proxy:
            entries[index] += _PERP_MARK
    return QUOTE_LINE_PREFIX + " · ".join(entries)


__all__ = [
    "ACTION_ZH",
    "CARD_ASSETS_MAX",
    "FAMILY_TITLE",
    "NEWS_HEADLINE_MAX",
    "NOVELTY_ZH",
    "OI_DIRECTION_ZH",
    "QUOTE_LINE_PREFIX",
    "SIDE_ZH",
    "TITLE_MAX",
    "UNTRADEABLE_NOTICE_ZH",
    "CardFamily",
    "CardTone",
    "ReaderCard",
    "ReaderCardAction",
    "ReaderCardFacts",
    "ReaderCardHeader",
    "ReaderCardHeadline",
    "ReaderCardLink",
    "ReaderCardMarket",
    "ReaderCardNote",
    "ReaderCardQuote",
    "ReaderCardTimes",
    "quote_line",
    "reader_news",
    "reader_quotes",
]
