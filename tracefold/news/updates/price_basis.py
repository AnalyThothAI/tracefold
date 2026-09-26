"""Code-owned reading of whether a quoted market move states a fact beyond the number.

Ported from the retired Gate's price-report rule (#675 §3, §7). What makes a quote worth interrupting a
reader for is stated as text the cited source itself has to carry: a level crossed, a period record, a
de-peg, a freight rate, or a quantified flow. The vocabulary is bilingual because the first cut of it was
not: most cards the 2026-09-22 audit wrongly withheld were English or a Chinese variant a Chinese-only
list did not spell ("seven-month high", "rises above $82,000", "失守 100 美元关口").

This is admissibility, not importance, and it is code-owned because a model asked the same question kept
answering it from its own calibration.
"""

from __future__ import annotations

import re
from typing import Final

_PRICE_LEVEL_CROSSED: Final = (
    r"站上|跌破|突破|收复|失守|关口|首次突破"
    # "回落至 X 下方" and "跌回每桶 100 美元下方" are one shape with two verbs; both name the level crossed.
    r"|(回落|跌回|回落至|跌至|下探至)[^，。,.]{0,14}?下方|(升至|涨回|回升至|反弹至)[^，。,.]{0,14}?上方"
    r"|rises?\s+above|rose\s+above|falls?\s+below|fell\s+below|drops?\s+below"
    r"|reclaims?|reclaimed|back\s+above|first\s+time\s+since"
)
_PRICE_PERIOD_RECORD: Final = (
    r"创[^，。,.]{0,12}?(新高|新低|高位|低位|纪录)"
    r"|(历史|创纪录|阶段性)?(新高|新低)"
    r"|(年内|月内|周内)(新高|新低|高点|低点)"
    # "为2007年7月17日以来最高" / "逾十年来最低": the period is named before the superlative.
    r"|(以来|年来|月来)(最高|最低|新高|新低)"
    r"|record\s+(high|low)"
    r"|(highest|lowest)\s+(since|level|price)"
    # "seven-month high" is how the wires write it; a digits-only pattern missed every English one.
    r"|(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|multi)[\s-]+"
    r"(month|week|year|day|session)[\s-]+(high|low)"
    r"|最大(单日|日内|盘中|单周|单月)?(涨|跌)幅"
    r"|(largest|biggest)\s+(single-day\s+|daily\s+|intraday\s+)?(gain|drop|loss|rise|fall|decline)"
)
# A flow only counts when the text quantifies it. "Outflows continue" is a mood; "$648M withdrawn" is a fact.
_PRICE_FLOW_WORDS: Final = (
    r"清算|爆仓|净流入|净流出|增持|减持|提币|提取|转出|存入|持仓"
    r"|liquidat\w*|inflows?|outflows?|withdraw\w*|deposit\w*"
)
_PRICE_DIGIT: Final = r"[\d一二三四五六七八九十百千万亿]"
_PRICE_BASIS_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(_PRICE_LEVEL_CROSSED, re.IGNORECASE),
    re.compile(_PRICE_PERIOD_RECORD, re.IGNORECASE),
    # A stablecoin off its peg is a credit event wearing a price, whichever side of the number the word is on.
    re.compile(r"脱锚|depeg\w*|跌至\s*0\.9\d", re.IGNORECASE),
    # Freight is a physical-supply price; the Hormuz VLCC day rate is the fact, not the percentage.
    re.compile(r"租金|运费|freight", re.IGNORECASE),
    re.compile(rf"{_PRICE_DIGIT}[^\n]{{0,24}}?({_PRICE_FLOW_WORDS})", re.IGNORECASE),
    re.compile(rf"({_PRICE_FLOW_WORDS})[^\n]{{0,24}}?{_PRICE_DIGIT}", re.IGNORECASE),
)
_PRICE_PERCENT: Final = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# The owner's one exception (#675 §7): a same-day move of this size is itself the fact, but only where a
# whole market moved. A single stock is excluded by its market -- the Tencent card that opened #675 is +7%
# and stays withheld -- so the exception is carried by the primary asset's market, never by its size.
PRICE_MOVE_EXCEPTION_PERCENT: Final = 5.0
PRICE_MOVE_EXCEPTION_MARKETS: Final[frozenset[str]] = frozenset({"commodity", "index"})


def price_move_basis(text: str) -> bool:
    """True when the text states something about the move beyond the number itself."""

    value = str(text or "")
    return bool(value) and any(pattern.search(value) for pattern in _PRICE_BASIS_PATTERNS)


def states_large_daily_move(text: str) -> bool:
    """True when the text states a percentage move at or above the owner's exception size."""

    return any(float(value) >= PRICE_MOVE_EXCEPTION_PERCENT for value in _PRICE_PERCENT.findall(str(text or "")))
