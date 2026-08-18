"""Storyline keys and theme lexicon (pure); the status window itself is a repository query."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

STORYLINE_LEXICON_VERSION: Final = "news_storyline_lexicon_v2"

THEMES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "crypto_treasury",
        re.compile(
            r"bitcoin treasur|btc treasur|crypto treasur|digital asset treasur|treasury (company|platform|strategy)"
            r"|treasury bitcoin|比特币储备|比特币财库|数字资产储备|加密储备",
            re.IGNORECASE,
        ),
    ),
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
    (
        "us_macro_data",
        re.compile(
            r"housing starts|building permits|import prices|export prices|jobless claims|retail sales|durable goods"
            r"|consumer (confidence|sentiment)|ism|pmi|gdp|营建许可|新屋开工|进口物价|零售销售|初请|耐用品",
            re.IGNORECASE,
        ),
    ),
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
    """Asset-level key when a non-CL primary asset exists and scope is not macro; else theme; else family.

    Called twice per Event: before Triage with the Gate's grounded assets (preliminary key, status bar only) and
    after Triage with the verdict's primary assets and scope (final key, written back to the Event and used by the
    storyline windows and throttling).
    """

    primaries = sorted(a.upper().replace("XYZ-", "") for a in primary_assets if a.upper() not in _CL_SYMBOLS)
    if primaries and scope != "macro":
        return f"asset:{primaries[0]}"
    text = f"{title} {headline_zh}".lower()
    for name, pattern in THEMES:
        if pattern.search(text):
            return f"theme:{name}"
    return f"macro:{family}"


def preliminary_storyline_key(*, title: str, grounded_assets: Sequence[str], asset_class: str, family: str) -> str:
    """Key computed before Triage (status bar only). Theme first: a geopolitical or macro headline the provider
    tagged with BTC/CL as *affected* assets belongs to its theme until Triage names a primary; the final key
    (``final_storyline_key``) then follows the verdict."""

    text = title.lower()
    for name, pattern in THEMES:
        if pattern.search(text):
            return f"theme:{name}"
    scope = "macro" if asset_class in {"macro", "none"} else "single_name"
    return storyline_key(title=title, headline_zh="", scope=scope, primary_assets=grounded_assets, family=family)


def final_storyline_key(
    *,
    title: str,
    headline_zh: str,
    scope: str,
    verdict_primaries: Sequence[str],
    grounded_assets: Sequence[str],
    family: str,
) -> str:
    """Key computed after Triage: verdict primaries that the Gate grounded win; otherwise any grounded asset;
    macro scope always falls back to a theme."""

    grounded = {a.upper().replace("XYZ-", "") for a in grounded_assets}
    primaries = [a for a in verdict_primaries if a.upper().replace("XYZ-", "") in grounded]
    if primaries and scope != "macro":
        return storyline_key(title=title, headline_zh=headline_zh, scope=scope, primary_assets=primaries, family=family)
    themed = storyline_key(title=title, headline_zh=headline_zh, scope="macro", primary_assets=(), family=family)
    if themed.startswith("theme:"):
        return themed
    # No theme matched: a grounded asset is a better storyline than the `macro:<family>` catch-all, even when the
    # model called the scope macro (a stock falling on a trial is still that stock's storyline).
    fallback = sorted(a for a in grounded_assets if a.upper() not in _CL_SYMBOLS)
    if fallback:
        return storyline_key(
            title=title, headline_zh=headline_zh, scope="single_name", primary_assets=fallback, family=family
        )
    return themed


__all__ = [
    "STORYLINE_LEXICON_VERSION",
    "THEMES",
    "final_storyline_key",
    "preliminary_storyline_key",
    "storyline_key",
]
