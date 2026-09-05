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

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from . import card_format as fmt
from .outcome import DIRECTION_ZH, MAGNITUDE_ZH, NOVELTY_ZH

CardFamily = Literal["news", "oi", "liquidation", "smart_money", "raw"]
# The model's own judgment about the news, and `none` for a card that carries no judgment at all --
# a degraded News card or any market card. A channel maps `family + tone` to its colour or icon.
CardTone = Literal["bullish", "bearish", "neutral", "unclear", "none"]

# One card's header is bounded by what every channel will show without folding.
TITLE_MAX: Final = 100
# The provider's own line on an unstructured card. The rest is on the detail page.
RAW_TEXT_MAX: Final = 220

# The family's word, for the families whose card is not headed by its own headline.
FAMILY_TITLE: Final[dict[str, str]] = {
    "oi": "持仓异动",
    "liquidation": "强平",
    "smart_money": "聪明钱",
    "raw": "市场原文",
}
# OI's own direction vocabulary. Deliberately not the verdict's `DIRECTION_ZH`: an open-interest
# change rises or falls, it is not bullish or bearish, and a market card claims no judgment.
OI_DIRECTION_ZH: Final[dict[str, str]] = {"rise": "上升", "fall": "下降"}
ACTION_ZH: Final[dict[str, str]] = {"open": "开", "close": "平"}
SIDE_ZH: Final[dict[str, str]] = {"long": "多", "short": "空"}

_QUOTE_LINE_PREFIX: Final = "行情 "
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

_LIQUIDATION_NOTE: Final = "各来源报告金额不相加：没有可信底层成交标识时只列报告数与最大单笔。"
_SMART_MONEY_NOTE: Final = "Close 只表示来源报告的平仓/减仓动作，不代表账户已全部清仓。"
_SMART_MONEY_UNVERIFIED: Final = "（来源标签，非已核实地址）"
_RAW_NOTE: Final = "未结构化，保留供应商原文"

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
    freshness: str = "fresh"
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
    account: str = ""
    account_verified: bool = False
    actions: tuple[ReaderCardAction, ...] = ()
    action_changes: int = 0
    opened_action: ReaderCardAction = ReaderCardAction()
    latest_action: ReaderCardAction = ReaderCardAction()


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
        rather than to any one part. The first raw smart-money cards in production were headed
        `市场原文· 原文 —`: an unstructured report names no instrument, and the missing space, the
        qualifier's own separator and the `—` placeholder were three pieces of punctuation standing
        in for a word that was never going to be there (#553). An absent field is absent.
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
        """

        market, span = self.market, self._span()
        venue = market.venue or fmt.UNKNOWN_VENUE
        if self.header.family == "oi":
            direction = OI_DIRECTION_ZH.get(market.direction or "", market.direction or "")
            head = f"{direction} {fmt.percent_from_bps(market.oi_change_bps)}"
            detail = (
                f"OI ${fmt.usd_compact(market.oi_value_usd)} · {venue} · "
                f"{market.measurement or fmt.UNKNOWN_MEASUREMENT}"
            )
            return [f"{head} · {span}", detail] if span else [head, detail]
        if self.header.family == "liquidation":
            side = market.side or ""
            return [
                f"{venue} · {SIDE_ZH.get(side, side)}单被强平 {self.facts.report_count} 笔 · {span}",
                f"最大单笔来源报告金额 ${market.notional}" if market.notional else "",
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
                    f"{entry.label} ${fmt.decimal_text(entry.notional)}"
                    if fmt.decimal_text(entry.notional)
                    else entry.label
                    for entry in market.actions
                ),
                _SMART_MONEY_NOTE,
            ]
        return [
            fmt.clip(self.lead, RAW_TEXT_MAX),
            " · ".join(part for part in (venue, market.kind, _RAW_NOTE) if part),
        ]

    def _span(self) -> str:
        if self.times.event_at_ms is None:
            return ""
        last = self.times.event_at_ms
        first = self.times.span_from_ms if self.times.span_from_ms is not None else last
        return fmt.clock(first) if first == last else f"{fmt.clock(first)}–{fmt.clock(last)}"


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
    return _QUOTE_LINE_PREFIX + " · ".join(entries)


__all__ = [
    "ACTION_ZH",
    "FAMILY_TITLE",
    "NOVELTY_ZH",
    "OI_DIRECTION_ZH",
    "RAW_TEXT_MAX",
    "SIDE_ZH",
    "TITLE_MAX",
    "UNTRADEABLE_NOTICE_ZH",
    "CardFamily",
    "CardTone",
    "ReaderCard",
    "ReaderCardAction",
    "ReaderCardFacts",
    "ReaderCardHeader",
    "ReaderCardLink",
    "ReaderCardMarket",
    "ReaderCardNote",
    "ReaderCardQuote",
    "ReaderCardTimes",
    "quote_line",
]
