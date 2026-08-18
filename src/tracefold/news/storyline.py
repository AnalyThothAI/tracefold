"""Storyline keys and theme lexicon (pure); the status window itself is a repository query."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

STORYLINE_LEXICON_VERSION: Final = "news_storyline_lexicon_v1"

THEMES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "mideast_energy",
        re.compile(
            r"hormuz|霍尔木兹|strait|\biran|伊朗|irgc|khamenei|\boman|阿曼|\boil\b|crude|brent|wti|油价|原油|opec"
            r"|israel|hezbollah|以色列|中东|gulf|houthi|yemen",
            re.IGNORECASE,
        ),
    ),
    (
        "rates",
        re.compile(
            r"30[- ]year|10[- ]year|30年期|10年期|treasury|yields?\b|收益率|国债|jgb"
            r"|\bfed\b|fomc|powell|美联储|加息|降息"
            r"|boj|日本央行|ecb|rate (cut|hike|decision)|cpi|pce|nonfarm|payroll",
            re.IGNORECASE,
        ),
    ),
    ("trade", re.compile(r"tariff|关税|trade (deal|talks|war)|canada|加拿大|ustr", re.IGNORECASE)),
    ("china_macro", re.compile(r"\bchina|中国|pboc|国务院|央行|社融|工业产出|工业增加值", re.IGNORECASE)),
    ("metals", re.compile(r"\bgold\b|黄金|xau|silver|白银|copper|铜价|lme", re.IGNORECASE)),
    ("us_equity_macro", re.compile(r"nasdaq|s&p|\bdow\b|美股|kospi|欧股|stock futures|期货", re.IGNORECASE)),
)

_CL_SYMBOLS: Final = frozenset({"CL", "XYZ-CL"})


def storyline_key(
    *,
    title: str,
    headline_zh: str,
    scope: str,
    primary_assets: Sequence[str],
    family: str,
) -> str:
    """Asset-level key when a non-CL primary asset exists and scope is not macro; else theme; else family."""

    primaries = sorted(a.upper().replace("XYZ-", "") for a in primary_assets if a.upper() not in _CL_SYMBOLS)
    if primaries and scope != "macro":
        return f"asset:{primaries[0]}"
    text = f"{title} {headline_zh}".lower()
    for name, pattern in THEMES:
        if pattern.search(text):
            return f"theme:{name}"
    return f"macro:{family}"


def preliminary_storyline_key(*, title: str, grounded_assets: Sequence[str], asset_class: str, family: str) -> str:
    """Key computed before Triage (no headline_zh yet); used for the event_status window query."""

    scope = "macro" if asset_class in {"macro", "none"} else "single_name"
    return storyline_key(title=title, headline_zh="", scope=scope, primary_assets=grounded_assets, family=family)


__all__ = ["STORYLINE_LEXICON_VERSION", "THEMES", "preliminary_storyline_key", "storyline_key"]
