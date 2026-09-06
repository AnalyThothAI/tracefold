"""What the wallet tape's four-hourly digest says, and what a model is allowed to say about it (§5.4).

The pure half, in the same relation to `digest_writer` that `rules` is in to `derive`: nothing here
reads a clock, opens a connection or calls a model. The caller hands in one window of rows and these
functions answer with numbered facts, the deterministic sentences those facts make, and -- given a
model's answer -- whether that answer stayed inside them.

**The program computes.** Every figure a reader sees is read out of PostgreSQL and rendered into a
fact with an id: how much each roster wallet bought and sold, the three separately-named cost bases
per position that moved, the cards the rules sent in the window, and what the price receipts came back
saying. **A model only writes sentences.** It is shown the pack as text and returns at most eight short
Chinese lines with the fact ids each line stands on; `ground` throws the whole answer away unless every
figure in every line appears in the facts that line cited. A thrown-away answer, a timeout, an
unconfigured endpoint and a day already at its call cap all end in the same place: `template_lines`,
which is the same facts as plain sentences.

That is why nothing here decides anything. A model chooses no threshold, no roster, no card and not
whether the digest is sent. Its whole authority is the wording, and the fallback proves the digest
never depended on it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from .. import card_format as fmt
from ..artifact_identity import canonical_json, canonical_sha
from ..wallet_contracts import DigestLine

DIGEST_KIND: Final = "digest"
# Every four hours, which is #572 §6.3's cadence: the roster traded in 16 of 20 hours the day the
# window sizes were measured, so an hourly digest would mostly restate itself.
DIGEST_INTERVAL_S_DEFAULT: Final = 14_400
# The Issue's own ceiling on model calls. It is not the digest's ceiling: a window past the cap still
# produces a digest, rendered from the template. What the cap bounds is the endpoint, which triage
# shares at concurrency one.
DIGEST_MAX_CALLS_PER_DAY_DEFAULT: Final = 24
DAY_MS: Final = 86_400_000
# However long the tape was down, one digest covers at most a day. A window without a bound would make
# the first digest after an outage a scan of the whole retention.
DIGEST_WINDOW_MAX_MS: Final = DAY_MS
# What one pack may carry, per section. The pack is a prompt: it has to fit, and a roster of forty
# wallets trading two hundred tokens would otherwise write one.
DIGEST_WALLETS_MAX: Final = 20
DIGEST_COSTS_MAX: Final = 12
DIGEST_CARDS_MAX: Final = 20
# How many handles the provider is asked about for the moving-average cost. The other two cost bases
# are computed from our own fills and cost nothing; this one is somebody else's small public server.
DIGEST_BAGS_MAX: Final = 8
DIGEST_LINES_MAX: Final = 8

# What counts as "a figure" in a line a model wrote: anything that starts `0x` -- an address, a
# transaction, or one of this provider's `0x`-prefixed handles -- or a number with its thousands
# separators and decimals attached. Anything matching this has to be present in a cited fact.
#
# The `0x` alternative comes first and swallows the whole token on purpose. Matching only hex would
# leave `0xVantaa` contributing the single digit `0`, which is both a false figure and a trivially
# satisfiable one; taking the identifier whole makes a misspelled handle as ungrounded as an invented
# number, which is what it is.
_FIGURE = re.compile(r"0x[0-9A-Za-z]{2,}|\d[\d,]*(?:\.\d+)?")

_UNKNOWN: Final = "未知"


# --- what the pack is made of -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalletWindowActivity:
    """One roster wallet's window, in counts and dollars. `unpriced` is a fact, not a defect."""

    wallet: str
    buys: int
    buy_usd: Decimal
    sells: int
    sell_usd: Decimal
    transfers_out: int
    unpriced: int


@dataclass(frozen=True, slots=True)
class TokenWindowFlow:
    """One (wallet, token) that moved in the window, with the retained history behind it.

    The window halves answer "what did this position do in the last four hours"; the lifetime halves
    are every fill this database still holds for the pair, which is what the net cash recovery line is
    computed from. Both are raw integers and stored `numeric`, never floats.
    """

    wallet: str
    token: str
    token_symbol: str
    token_decimals: int | None
    window_buy_usd: Decimal
    window_buy_raw: int
    window_sell_usd: Decimal
    lifetime_buy_usd: Decimal
    lifetime_sell_usd: Decimal
    lifetime_buy_raw: int
    lifetime_sell_raw: int
    lifetime_out_raw: int


@dataclass(frozen=True, slots=True)
class DigestCardRow:
    """One card the rules opened in the window, and whether a reader actually received it."""

    kind: str
    handle: str
    symbol: str
    ratio_bps: int | None
    basis: str | None
    peer_wallets: int
    usd: Decimal | None
    position_usd: Decimal | None
    tone: str
    sent: bool


@dataclass(frozen=True, slots=True)
class DigestOutcomeRow:
    """The price receipts that landed in the window, per horizon, as a count and a median."""

    horizon: str
    receipts: int
    priced: int
    median_bps: int | None


@dataclass(frozen=True, slots=True)
class DigestWindowRows:
    """Everything one digest reads from PostgreSQL, in one checkout."""

    chain_id: int
    activity: tuple[WalletWindowActivity, ...] = ()
    flows: tuple[TokenWindowFlow, ...] = ()
    cards: tuple[DigestCardRow, ...] = ()
    outcomes: tuple[DigestOutcomeRow, ...] = ()
    tokens: int = 0
    unpriced: int = 0

    def is_empty(self) -> bool:
        """A window nobody traded in and no card was opened in. It gets no digest at all."""

        return not self.activity and not self.cards


@dataclass(frozen=True, slots=True)
class LastDigest:
    """Where the previous digest ended, and how many model calls the last day has already spent."""

    window_to_ms: int
    model_calls_last_day: int


@dataclass(frozen=True, slots=True)
class DigestFact:
    """One statement the digest may make, and the id a line cites it by."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class DigestPack:
    """The whole of what a model is shown, and the whole of what a line may stand on."""

    window_from_ms: int
    window_to_ms: int
    facts: tuple[DigestFact, ...]

    def as_json(self) -> str:
        return canonical_json(
            {
                "window": {
                    "from_ms": int(self.window_from_ms),
                    "to_ms": int(self.window_to_ms),
                    "hours": window_hours(self.window_from_ms, self.window_to_ms),
                },
                "facts": [{"id": fact.id, "text": fact.text} for fact in self.facts],
            }
        )

    def sha256(self) -> str:
        return canonical_sha({"facts": [[fact.id, fact.text] for fact in self.facts]})

    def by_id(self) -> dict[str, DigestFact]:
        return {fact.id: fact for fact in self.facts}


@dataclass(frozen=True, slots=True)
class DigestResult:
    """What one digest pass did, counted so a turn can report it on the tape's own state row."""

    digests: int = 0
    lines: int = 0
    model_called: bool = False
    model_used: bool = False


class DigestProgramPort(Protocol):
    """The one model call, as a protocol. The composition root knows which endpoint answers it."""

    async def summarize(self, *, facts_json: str) -> Sequence[DigestLine]: ...


class DigestBagsPort(Protocol):
    """The provider's open positions for one handle: the only external read the digest makes."""

    async def bags(self, handle: str) -> Sequence[Any]: ...


# --- rendering the pack -----------------------------------------------------------------------------


def build_pack(
    rows: DigestWindowRows,
    *,
    window_from_ms: int,
    window_to_ms: int,
    handles: Mapping[str, str],
    holding_costs: Mapping[tuple[str, str], Decimal | None],
) -> DigestPack:
    """Turn one window's rows into numbered facts. Pure: no clock, no connection, no model.

    Ordering is deliberate and is also the template's order: the window's own totals, the cards a
    reader was actually sent, the price receipts, the noise, then the wallets and the positions. The
    first four are what a reader who reads one line needs; the rest is detail.
    """

    facts: list[DigestFact] = [
        DigestFact(
            id="w0",
            text=(
                f"窗口 {fmt.clock(window_from_ms)}–{fmt.clock(window_to_ms)}"
                f"（{window_hours(window_from_ms, window_to_ms)} 小时），"
                f"活跃名单地址 {len(rows.activity)} 个，代币 {rows.tokens} 个"
            ),
        ),
        DigestFact(id="w1", text=_totals_text(rows)),
    ]
    facts.extend(_card_facts(rows.cards))
    facts.extend(_outcome_facts(rows.outcomes))
    if rows.unpriced or any(item.transfers_out for item in rows.activity):
        transfers = sum(item.transfers_out for item in rows.activity)
        facts.append(
            DigestFact(
                id="n1",
                text=f"未计价成交 {rows.unpriced} 笔，非交易转出 {transfers} 笔（不计入买卖金额）",
            )
        )
    facts.extend(_wallet_facts(rows.activity, handles=handles))
    facts.extend(_cost_facts(rows.flows, handles=handles, holding_costs=holding_costs))
    return DigestPack(window_from_ms=int(window_from_ms), window_to_ms=int(window_to_ms), facts=tuple(facts))


def _totals_text(rows: DigestWindowRows) -> str:
    buys = sum(item.buys for item in rows.activity)
    sells = sum(item.sells for item in rows.activity)
    buy_usd = fmt.money(sum((item.buy_usd for item in rows.activity), Decimal(0))) or "$0"
    sell_usd = fmt.money(sum((item.sell_usd for item in rows.activity), Decimal(0))) or "$0"
    return f"合计买入 {buys} 笔 {buy_usd}，卖出 {sells} 笔 {sell_usd}"


def _card_facts(cards: Sequence[DigestCardRow]) -> list[DigestFact]:
    exits = [card for card in cards if card.kind == "exit"]
    crowding = [card for card in cards if card.kind == "crowding"]
    sent = sum(1 for card in cards if card.sent)
    facts = [
        DigestFact(
            id="k0",
            text=f"窗口内退出卡 {len(exits)} 张、拥挤卡 {len(crowding)} 张，其中已送达 {sent} 张",
        )
    ]
    for index, card in enumerate(cards[:DIGEST_CARDS_MAX], start=1):
        facts.append(DigestFact(id=f"k{index}", text=_card_text(card)))
    return facts


def _card_text(card: DigestCardRow) -> str:
    handle = card.handle or _UNKNOWN
    symbol = card.symbol or _UNKNOWN
    state = "已送达" if card.sent else "未送达"
    if card.kind == "exit":
        action = "清仓" if card.ratio_bps is not None and card.ratio_bps >= 10_000 else "减仓"
        share = fmt.percent_from_bps(card.ratio_bps)
        basis = {"chain_balance": "链上余额", "site_reported": "持仓推算"}.get(card.basis or "", _UNKNOWN)
        value = fmt.money(card.position_usd)
        position = f"，卖前持仓约 {value}" if value else ""
        return f"退出卡：{handle} {action} {symbol} {share}，口径 {basis}{position}，{state}"
    total = fmt.money(card.usd)
    late = "，跟风偏晚" if card.tone == "late" else ""
    return f"拥挤卡：{card.peer_wallets} 个名单地址买入 {symbol}，合计 {total or _UNKNOWN}{late}，{state}"


def _outcome_facts(outcomes: Sequence[DigestOutcomeRow]) -> list[DigestFact]:
    facts: list[DigestFact] = []
    for index, row in enumerate(outcomes, start=1):
        median = _UNKNOWN if row.median_bps is None else fmt.percent_from_bps(row.median_bps)
        facts.append(
            DigestFact(
                id=f"o{index}",
                text=(
                    f"+{row.horizon} 回执 {row.receipts} 条，其中取到价格 {row.priced} 条，相对发卡时价格中位 {median}"
                ),
            )
        )
    return facts


def _wallet_facts(
    activity: Sequence[WalletWindowActivity],
    *,
    handles: Mapping[str, str],
) -> list[DigestFact]:
    facts: list[DigestFact] = []
    for index, item in enumerate(activity[:DIGEST_WALLETS_MAX], start=1):
        handle = handles.get(item.wallet) or item.wallet[:10]
        buy = fmt.money(item.buy_usd) or "$0"
        sell = fmt.money(item.sell_usd) or "$0"
        facts.append(
            DigestFact(
                id=f"a{index}",
                text=f"{handle} 买入 {item.buys} 笔 {buy}，卖出 {item.sells} 笔 {sell}",
            )
        )
    return facts


def _cost_facts(
    flows: Sequence[TokenWindowFlow],
    *,
    handles: Mapping[str, str],
    holding_costs: Mapping[tuple[str, str], Decimal | None],
) -> list[DigestFact]:
    """The three cost bases #572 §5.3 asks to be named separately, per position that moved.

    They answer three different questions and are never averaged into one "cost": what this window
    paid, what the provider says the remaining bag cost, and where the position stops owing money.
    Any of them may be unknown, and an unknown one is printed as unknown rather than left out -- a
    missing line reads as "no such position", which is a different statement.
    """

    facts: list[DigestFact] = []
    for index, flow in enumerate(flows[:DIGEST_COSTS_MAX], start=1):
        handle = handles.get(flow.wallet) or flow.wallet[:10]
        symbol = flow.token_symbol or flow.token[:10]
        entry = _window_entry_price(flow)
        holding = holding_costs.get((flow.wallet, flow.token))
        recovery, net_cash = _recovery_line(flow)
        parts = [
            f"观察期买入均价 {fmt.money(entry) or _UNKNOWN}",
            f"剩余持仓成本 {fmt.money(holding) or _UNKNOWN}",
        ]
        if recovery is not None:
            parts.append(f"净现金回收线 {fmt.money(recovery) or _UNKNOWN}")
        elif net_cash is not None:
            parts.append(f"净现金回收线 已清空，净现金 {fmt.money(net_cash) or '$0'}")
        else:
            parts.append(f"净现金回收线 {_UNKNOWN}")
        facts.append(DigestFact(id=f"c{index}", text=f"{handle} {symbol}：" + "；".join(parts)))
    return facts


def _window_entry_price(flow: TokenWindowFlow) -> Decimal | None:
    """观察期买入均价: this window's buy dollars over this window's bought quantity."""

    quantity = _scaled(flow.window_buy_raw, flow.token_decimals)
    if quantity is None or quantity <= 0 or flow.window_buy_usd <= 0:
        return None
    return flow.window_buy_usd / quantity


def _recovery_line(flow: TokenWindowFlow) -> tuple[Decimal | None, Decimal | None]:
    """净现金回收线: net cash still out, over what is still held. Negative is a real answer.

    A wallet that has already taken more dollars out of a position than it put in has no recovery
    line -- it owes nothing back -- so the second element carries the net cash instead. A position
    whose remaining quantity is zero or unknown has neither, which is what `(None, None)` says.
    """

    remaining = _scaled(
        int(flow.lifetime_buy_raw) - int(flow.lifetime_sell_raw) - int(flow.lifetime_out_raw),
        flow.token_decimals,
    )
    net_out = flow.lifetime_buy_usd - flow.lifetime_sell_usd
    if remaining is None or remaining <= 0:
        return None, (-net_out if flow.lifetime_buy_usd > 0 or flow.lifetime_sell_usd > 0 else None)
    return net_out / remaining, None


def _scaled(raw: int, decimals: int | None) -> Decimal | None:
    if decimals is None:
        return None
    return Decimal(int(raw)) / (Decimal(10) ** int(decimals))


def window_hours(from_ms: int, to_ms: int) -> int:
    """The window as whole hours, which is how a reader is told what the digest covers."""

    return max(1, round((int(to_ms) - int(from_ms)) / 3_600_000))


def template_lines(pack: DigestPack) -> tuple[DigestLine, ...]:
    """The digest with no model in it: the pack's own leading facts, as its own sentences.

    This is not a degraded mode to be recovered from. It is what the digest *is* -- every figure in
    it was computed before any call was considered -- and the model's contribution is that the same
    facts read as prose. `build_pack` puts the facts a reader needs first, so taking the first eight
    is a summary rather than a truncation.
    """

    return tuple(DigestLine(text=fact.text, cites=(fact.id,)) for fact in pack.facts[:DIGEST_LINES_MAX])


def ground(pack: DigestPack, lines: Sequence[DigestLine]) -> tuple[DigestLine, ...] | None:
    """The whole answer, or nothing. `None` means the caller renders the template instead.

    Two conditions, and both are about the pack rather than about style: every id a line cites has to
    be a fact, and every figure a line states has to appear in one of the facts that line cited. A
    single violation drops the *whole* digest rather than the offending line, because a reader cannot
    tell which sentence of a card was checked -- and because a model that invented one number has
    already shown what its other seven lines are worth.
    """

    if not lines or len(lines) > DIGEST_LINES_MAX:
        return None
    facts = pack.by_id()
    grounded: list[DigestLine] = []
    for line in lines:
        text = line.text.strip()
        if not text or not line.cites:
            return None
        cited = [facts[cite] for cite in line.cites if cite in facts]
        if len(cited) != len(line.cites):
            return None
        allowed = set().union(*(_figures(fact.text) for fact in cited)) if cited else set()
        if not _figures(text) <= allowed:
            return None
        grounded.append(DigestLine(text=text, cites=tuple(line.cites)))
    return tuple(grounded)


def _figures(text: str) -> set[str]:
    """Every number and hex identity in one string, normalised so `$23,531.60` and `23531.6` agree."""

    return {_normalized_figure(match.group(0)) for match in _FIGURE.finditer(text)}


def _normalized_figure(value: str) -> str:
    if value.lower().startswith("0x"):
        return value.lower()
    digits = value.replace(",", "")
    if "." in digits:
        digits = digits.rstrip("0").rstrip(".")
    return digits or "0"


__all__ = [
    "DAY_MS",
    "DIGEST_BAGS_MAX",
    "DIGEST_CARDS_MAX",
    "DIGEST_COSTS_MAX",
    "DIGEST_INTERVAL_S_DEFAULT",
    "DIGEST_KIND",
    "DIGEST_LINES_MAX",
    "DIGEST_MAX_CALLS_PER_DAY_DEFAULT",
    "DIGEST_WALLETS_MAX",
    "DIGEST_WINDOW_MAX_MS",
    "DigestBagsPort",
    "DigestCardRow",
    "DigestFact",
    "DigestOutcomeRow",
    "DigestPack",
    "DigestProgramPort",
    "DigestWindowRows",
    "LastDigest",
    "TokenWindowFlow",
    "WalletWindowActivity",
    "build_pack",
    "ground",
    "template_lines",
    "window_hours",
]
