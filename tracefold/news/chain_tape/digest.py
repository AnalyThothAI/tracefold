"""Buy research facts and their selection (#614).

PostgreSQL computes full-window totals separately from bounded buy details. Every displayed sentence
is rendered here. A model can select and order buy fact IDs, but cannot rewrite a wallet, token,
direction, amount or price. The template uses the same facts and reserves the same buy-first budget.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

from .. import card_format as fmt
from ..artifact_identity import canonical_json, canonical_sha
from ..wallet_contracts import DIGEST_LINES_MAX, DigestLine

DIGEST_INTERVAL_S_DEFAULT: Final = 14_400
DIGEST_MAX_CALLS_PER_DAY_DEFAULT: Final = 24
DAY_MS: Final = 86_400_000
DIGEST_WINDOW_MAX_MS: Final = DAY_MS
DIGEST_COSTS_MAX: Final = 12
DIGEST_BAGS_MAX: Final = 8
# One overview, five buys, one related detail and one coverage statement fit the eight-line card.
DIGEST_BUYS_MAX: Final = 5
_UNKNOWN: Final = "未知"


@dataclass(frozen=True, slots=True)
class DigestWindowTotals:
    """Full-window counts. None of these are reconstructed from a Top N list."""

    buys: int = 0
    buy_usd: Decimal = Decimal(0)
    buy_wallets: int = 0
    buy_positions: int = 0
    sells: int = 0
    sell_usd: Decimal = Decimal(0)
    active_wallets: int = 0
    transfers_out: int = 0
    unpriced: int = 0
    cards: int = 0
    sent_cards: int = 0


@dataclass(frozen=True, slots=True)
class TokenWindowFlow:
    """One wallet/token bought in the window; retained history is not a position baseline.

    `priced_buy_raw` covers exactly the fills in `window_buy_usd`. Unpriced buys still contribute to
    `window_buy_raw` and counts, but can never dilute the price of the priced fills.
    """

    wallet: str
    token: str
    token_symbol: str
    token_decimals: int | None
    window_buy_usd: Decimal
    window_buy_raw: int
    priced_buy_raw: int
    buys: int
    unpriced_buys: int
    first_buy_at_ms: int
    last_buy_at_ms: int
    subsequent_sells: int
    subsequent_sell_usd: Decimal
    history_from_ms: int


@dataclass(frozen=True, slots=True)
class DigestOutcomeRow:
    """Outcomes grouped by observation kind, horizon and the explicitly recorded reference."""

    kind: str
    horizon: str
    reference_kind: str
    receipts: int
    priced: int
    comparable: int
    median_bps: int | None


@dataclass(frozen=True, slots=True)
class DigestWindowRows:
    chain_id: int
    totals: DigestWindowTotals = field(default_factory=DigestWindowTotals)
    flows: tuple[TokenWindowFlow, ...] = ()
    outcomes: tuple[DigestOutcomeRow, ...] = ()

    def is_empty(self) -> bool:
        """A sell-only window does not manufacture a buy digest."""

        return self.chain_id <= 0 or self.totals.buys <= 0 or not self.flows


@dataclass(frozen=True, slots=True)
class LastDigest:
    window_to_ms: int
    model_calls_last_day: int
    attempted_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class DigestFact:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class DigestPack:
    window_from_ms: int
    window_to_ms: int
    facts: tuple[DigestFact, ...]

    def as_json(self) -> str:
        return canonical_json(
            {
                "window": {"from_ms": self.window_from_ms, "to_ms": self.window_to_ms},
                "facts": [{"id": fact.id, "text": fact.text} for fact in self.facts],
            }
        )

    def sha256(self) -> str:
        return canonical_sha({"facts": [[fact.id, fact.text] for fact in self.facts]})

    def by_id(self) -> dict[str, DigestFact]:
        return {fact.id: fact for fact in self.facts}


@dataclass(frozen=True, slots=True)
class Grounding:
    lines: tuple[DigestLine, ...]
    kept: int
    dropped: int

    def accepted(self) -> bool:
        return self.kept > 0


class DigestProgramPort(Protocol):
    async def summarize(self, *, facts_json: str) -> Sequence[str]:
        """Select buy fact IDs. Reader-facing text is never a model output."""
        ...


class DigestBagsPort(Protocol):
    async def bags(self, handle: str) -> Sequence[Any]: ...


def build_pack(
    rows: DigestWindowRows,
    *,
    window_from_ms: int,
    window_to_ms: int,
    handles: Mapping[str, str],
    holding_costs: Mapping[tuple[str, str], Decimal | None],
) -> DigestPack:
    """Buy facts first; unrelated exits never enter the detail budget."""

    totals = rows.totals
    facts = [
        DigestFact(
            "w0",
            f"{fmt.clock(window_from_ms)}–{fmt.clock(window_to_ms)} 买入合计 {totals.buys} 笔，"
            f"已计价 {fmt.money(totals.buy_usd) or '$0'}，{totals.buy_wallets} 个地址、"
            f"{totals.buy_positions} 个钱包代币组合",
        ),
        DigestFact(
            "w1",
            f"事实包选取 {len(rows.flows)} / {totals.buy_positions} 个买入组合，摘要最多展示 {DIGEST_BUYS_MAX} 个；"
            f"未计价成交 {totals.unpriced} 笔。仅覆盖保留流水，观察前余额与历史连续性未知",
        ),
        DigestFact(
            "w2",
            f"全窗口活跃地址 {totals.active_wallets} 个，卖出 {totals.sells} 笔"
            f"已计价 {fmt.money(totals.sell_usd) or '$0'}，非交易转出 {totals.transfers_out} 笔；"
            f"观测卡 {totals.cards} 张，其中已送达 {totals.sent_cards} 张",
        ),
    ]
    for index, flow in enumerate(rows.flows[:DIGEST_COSTS_MAX], start=1):
        handle = handles.get(flow.wallet) or _UNKNOWN
        subject = f"{handle}（{flow.wallet}）{flow.token_symbol or _UNKNOWN}（{flow.token}）"
        price = fmt.money(_window_entry_price(flow)) or _UNKNOWN
        facts.append(
            DigestFact(
                f"b{index}",
                f"{subject} {fmt.clock(flow.first_buy_at_ms)}–{fmt.clock(flow.last_buy_at_ms)} "
                f"买入 {flow.buys} 笔，已计价 {fmt.money(flow.window_buy_usd) or '$0'}，"
                f"已计价部分均价 {price}，未计价 {flow.unpriced_buys} 笔；建仓状态未知",
            )
        )
        if flow.subsequent_sells:
            facts.append(
                DigestFact(
                    f"s{index}",
                    f"{subject} 本窗口首笔买入后卖出 {flow.subsequent_sells} 笔，"
                    f"已计价 {fmt.money(flow.subsequent_sell_usd) or '$0'}；剩余持仓未知",
                )
            )
        holding = fmt.money(holding_costs.get((flow.wallet, flow.token))) or _UNKNOWN
        facts.append(
            DigestFact(
                f"c{index}",
                f"{subject} 站点剩余持仓均价 {holding}（本次读取快照，非窗口末余额基线）；"
                f"最早保留流水 {datetime.fromtimestamp(flow.history_from_ms / 1000, UTC).isoformat(timespec='minutes')}"
                "（非建仓时间），净现金回收线未知",
            )
        )
    for index, row in enumerate(rows.outcomes, start=1):
        median = fmt.percent_from_bps(row.median_bps) if row.median_bps is not None else _UNKNOWN
        basis = "相对观察价" if row.reference_kind == "observed" else "历史参考价未知"
        facts.append(
            DigestFact(
                f"o{index}",
                f"{row.kind} +{row.horizon} 回执 {row.receipts} 条，取到价格 {row.priced} 条，"
                f"可比较 {row.comparable} 条，{basis}中位 {median}",
            )
        )
    return DigestPack(int(window_from_ms), int(window_to_ms), tuple(facts))


def _window_entry_price(flow: TokenWindowFlow) -> Decimal | None:
    if flow.token_decimals is None or flow.priced_buy_raw <= 0 or flow.window_buy_usd <= 0:
        return None
    quantity = Decimal(flow.priced_buy_raw) / (Decimal(10) ** flow.token_decimals)
    return flow.window_buy_usd / quantity


def window_hours(from_ms: int, to_ms: int) -> int:
    return max(1, round((int(to_ms) - int(from_ms)) / 3_600_000))


def _render_lines(pack: DigestPack, preferred: Sequence[str]) -> tuple[DigestLine, ...]:
    facts = pack.by_id()
    buys = list(dict.fromkeys([*preferred, *(fact.id for fact in pack.facts if fact.id.startswith("b"))]))
    selected_buys = buys[:DIGEST_BUYS_MAX]
    chosen = [identity for identity in ("w0", *selected_buys) if identity in facts]
    # Keep coverage visible even when the model selected the maximum number of buys.
    detail_slots = max(0, DIGEST_LINES_MAX - len(chosen) - int("w1" in facts))
    details = [f"s{identity[1:]}" for identity in selected_buys]
    details += [f"c{identity[1:]}" for identity in selected_buys]
    details += [fact.id for fact in pack.facts if fact.id.startswith("o")]
    chosen += [identity for identity in details if identity in facts][:detail_slots]
    if "w1" in facts:
        chosen.append("w1")
    return tuple(DigestLine(text=facts[identity].text, cites=(identity,)) for identity in chosen)


def template_lines(pack: DigestPack) -> tuple[DigestLine, ...]:
    return _render_lines(pack, ())


def ground(pack: DigestPack, fact_ids: Sequence[str]) -> Grounding:
    """Accept known buy IDs only; every displayed sentence is the program's exact fact text.

    Non-string answers (including the retired free-text line shape), invented IDs and duplicates
    cannot contribute text. A valid selection only changes buy ordering, never the line budget or
    the mandatory coverage statement.
    """

    known = pack.by_id()
    selected: list[str] = []
    dropped = 0
    for identity in tuple(fact_ids)[:DIGEST_LINES_MAX]:
        if (
            not isinstance(identity, str)
            or not identity.startswith("b")
            or identity not in known
            or identity in selected
        ):
            dropped += 1
            continue
        selected.append(identity)
    dropped += max(0, len(fact_ids) - DIGEST_LINES_MAX)
    return Grounding(lines=_render_lines(pack, selected), kept=len(selected), dropped=dropped)


__all__ = [
    "DAY_MS",
    "DIGEST_BAGS_MAX",
    "DIGEST_BUYS_MAX",
    "DIGEST_COSTS_MAX",
    "DIGEST_INTERVAL_S_DEFAULT",
    "DIGEST_LINES_MAX",
    "DIGEST_MAX_CALLS_PER_DAY_DEFAULT",
    "DIGEST_WINDOW_MAX_MS",
    "DigestBagsPort",
    "DigestFact",
    "DigestOutcomeRow",
    "DigestPack",
    "DigestProgramPort",
    "DigestWindowRows",
    "DigestWindowTotals",
    "Grounding",
    "LastDigest",
    "TokenWindowFlow",
    "build_pack",
    "ground",
    "template_lines",
    "window_hours",
]
