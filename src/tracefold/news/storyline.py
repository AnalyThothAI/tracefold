"""Storyline keys and theme lexicon (pure); the status window itself is a repository query."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from .instruments import resolve_base_symbol

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
            r"hormuz|霍尔木兹|\bstrait\b|\biran|伊朗|irgc|khamenei|\boman|阿曼|opec"
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


# A model primary is free text (`TriageAsset.symbol` is any 1-16 characters) and this fallback is reached
# precisely when nothing grounded it, so it is the least validated string in the pipeline — and it becomes a
# duplicate-comparison group, an advisory-lock key and a console label. Accept only something shaped like a
# symbol; an exchange-qualified identifier we cannot group (`0001.HK`) falls through to the next step instead.
_SYMBOL_SHAPE: Final = re.compile(r"^[A-Z0-9]{1,10}$")


def _symbol_in_text(symbol: str, text: str) -> bool:
    """True when the base symbol appears in the text as its own uppercase token (a `$TICKER` cashtag counts: `$`
    is not a word character).

    Case-sensitive on purpose. Provider tags collide with ordinary English words — `NOT`, `ME`, `ID`, `IO`, `ON`,
    `AI` are all real symbols — and a case-insensitive match turned "he will not sell his stake" into evidence for
    `asset:NOT`, which is the exact mis-bucketing this fallback exists to prevent. A Chinese headline carrying the
    ticker still matches, because it carries it in caps. Strict on token boundaries too: `ETH` does not match
    `ETHEREUM`, `MU` does not match `MUSK`."""

    base = re.escape(symbol.replace("XYZ-", "").upper())
    return re.search(rf"(?<![A-Za-z0-9]){base}(?![A-Za-z0-9])", text) is not None


def storyline_key(
    *,
    title: str,
    headline_zh: str,
    scope: str,
    primary_assets: Sequence[str],
    family: str,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """Asset-level key when a non-CL primary asset exists and scope is not macro; else theme; else family.

    Called twice per Event: before Triage with the Gate's grounded assets (preliminary key, status bar only) and
    after Triage with the verdict's primary assets and scope (final key, written back to the Event and used by
    duplicate comparison, grouping and explicit mutes).

    ``aliases`` (#75) collapses the several symbols one issuer trades under before the key is formed. ``SKHY`` and
    ``SKHX`` are both real hl.xyz contracts for SK Hynix, so keeping them apart at the venue level is right — but
    separate ``asset:<symbol>`` groups prevent same-issuer duplicate comparison. ``None`` uses the built-in
    seeds, which is what every pure caller and every test gets.
    """

    primaries = sorted(resolve_base_symbol(a, aliases) for a in primary_assets if a.upper() not in _CL_SYMBOLS)
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
    aliases: Mapping[str, str] | None = None,
    degraded: bool = False,
) -> str:
    """Key computed after Triage. Grounded verdict primaries win; then a theme; then the model's own primaries
    even when the provider did not tag them; then a grounded tag that the text actually names; then
    ``macro:<family>``. ``aliases`` resolves symbols to one issuer first (#75); the last two steps are #100.

    ``degraded`` marks a rule-baseline verdict, whose ``assets`` are empty by construction (see
    ``triage_rules.fallback_verdict``). "The model named no primary" is evidence only when a model actually
    answered, so a degraded card keeps the pre-#100 fallback: the provider's tags are the only evidence there is.

    This key is a duplicate-comparison and operator-facing grouping, never a claim shown to the reader — the
    card's tickers come from ``delivery.card_assets`` (verdict primaries ∩ grounded), which this does not touch."""

    grounded = {resolve_base_symbol(a, aliases) for a in grounded_assets}
    primaries = [a for a in verdict_primaries if resolve_base_symbol(a, aliases) in grounded]
    if primaries and scope != "macro":
        return storyline_key(
            title=title,
            headline_zh=headline_zh,
            scope=scope,
            primary_assets=primaries,
            family=family,
            aliases=aliases,
        )
    themed = storyline_key(
        title=title, headline_zh=headline_zh, scope="macro", primary_assets=(), family=family, aliases=aliases
    )
    if themed.startswith("theme:"):
        return themed
    # No theme matched. The model named the subject even when the provider did not tag it, and its own primary is
    # a better bucket than an arbitrary grounded tag: OKX's listing notices all carry an `OKB` tag, so "Johnson &
    # Johnson appears on OKX" was keyed `asset:OKB`; VeChain's upgrade vote was keyed `asset:SKHY`. 16% of the
    # asset-keyed cards of a live day sat in a bucket that was not about them (#100).
    named = sorted(
        a
        for a in verdict_primaries
        if a.upper() not in _CL_SYMBOLS and _SYMBOL_SHAPE.match(a.upper().replace("XYZ-", ""))
    )
    if named:
        return storyline_key(
            title=title,
            headline_zh=headline_zh,
            scope="single_name",
            primary_assets=named,
            family=family,
            aliases=aliases,
        )
    # A model that answered and still named nothing is saying the headline has no tradable subject, so a provider
    # tag is only a storyline when the text is actually about it — the symbol appearing as its own token is the
    # cheap evidence for that. Everything else is the family bucket: `asset:BTC` was collecting Polish jets
    # scrambling and a lending protocol being drained, which polluted duplicate evidence for real BTC cards. A
    # false negative here costs a coarser group; a false positive contaminates another card's comparison set. A
    # degraded verdict is exempt: it has no `assets` to begin with, and "NVIDIA to invest $100bn" never spells
    # `NVDA`.
    text = f"{title} {headline_zh}"
    fallback = sorted(
        a for a in grounded_assets if a.upper() not in _CL_SYMBOLS and (degraded or _symbol_in_text(a, text))
    )
    if fallback:
        return storyline_key(
            title=title,
            headline_zh=headline_zh,
            scope="single_name",
            primary_assets=fallback,
            family=family,
            aliases=aliases,
        )
    return themed


__all__ = [
    "STORYLINE_LEXICON_VERSION",
    "THEMES",
    "final_storyline_key",
    "preliminary_storyline_key",
    "storyline_key",
]
