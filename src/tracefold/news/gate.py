"""Gate v2: deterministic admission, priority, asset class, grounded assets (pure)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .models import Admission, AssetClass, EngineType

GATE_LEXICON_VERSION: Final = "news_gate_lexicon_v1"

ENERGY_LEXICON = re.compile(
    r"\b(oil|crude|brent|wti|opec|hormuz|strait|tanker|barrel|bpd|refiner\w*|pipeline|gasoline|diesel|lng|natural gas"
    r"|novorossiysk|houthi|red sea|shale|rig count|eia|iran|iraq|saudi|aramco|oman|qatar|kuwait|uae|energy)\b"
    r"|石油|原油|油价|布油|美油|霍尔木兹|欧佩克|油轮|伊朗",
    re.IGNORECASE,
)
MACRO_LEXICON = re.compile(
    r"\b(fed|fomc|powell|rate (cut|hike|decision)|cpi|ppi|pce|nonfarm|payrolls?|jobless|unemployment|gdp|ism|pmi"
    r"|retail sales|treasury|treasuries|yields?|tariffs?|trade (deal|talks)|ecb|boj|boe|pboc|lpr|mlf|dollar index|dxy"
    r"|bond)\b|美联储|加息|降息|非农|关税|国务院|央行|收益率|国债|通胀",
    re.IGNORECASE,
)
PR_TEMPLATE_LEXICON = re.compile(
    r"securities (investigation|class action)|law ?firm|levi & korsinsky|pomerantz|rosen law|shareholder alert"
    r"|investors? (who|that) (lost|suffered)|investor alert",
    re.IGNORECASE,
)
_HIGH_PRIORITY_MACRO = re.compile(r"yield|rate|fed|fomc|cpi|收益率|加息|降息|美联储", re.IGNORECASE)
_TICKER_TAG_STOP: Final = frozenset({"OPENAI", "ANTHROPIC", "GENIUS", "ACT", "NEAR", "W", "LIQUID", "SPOT"})


@dataclass(frozen=True, slots=True)
class GateInput:
    title: str
    engine_type: EngineType
    strategy_ids: tuple[str, ...]
    provider_score: float | None
    coins: tuple[Mapping[str, Any], ...]
    ingest_mode: str  # live | recovery
    watchlist_symbols: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class GateVerdict:
    admission: Admission
    priority: str  # high | normal
    asset_class: AssetClass
    grounded_assets: tuple[str, ...]
    macro_lexicon: bool
    energy_lexicon: bool
    pr_template: bool
    watchlist_hits: tuple[str, ...]
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def amqp_priority(self) -> int:
        return 5 if self.priority == "high" else 0


def _base_symbol(symbol: str) -> str:
    return symbol.replace("XYZ-", "").upper()


def _symbol_in_title(symbol: str, title: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9])\$?{re.escape(symbol)}(?![A-Za-z0-9])", title, re.IGNORECASE) is not None


def grounded_assets(title: str, coins: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Grade A/A+ assets whose symbol (or match text) appears in the title, or A+ tags; CL only in energy context."""

    energy = bool(ENERGY_LEXICON.search(title))
    out: list[str] = []
    for coin in coins:
        symbol = str(coin.get("symbol") or "").strip()
        if not symbol:
            continue
        base = _base_symbol(symbol)
        if base == "CL":
            if energy:
                out.append(symbol)
            continue
        if len(base) < 2 or base in _TICKER_TAG_STOP:
            continue
        grade = str(coin.get("grade") or "")
        match_text = str(coin.get("match") or "").strip()
        in_title = _symbol_in_title(base, title) or (bool(match_text) and match_text.lower() in title.lower())
        if grade in {"A", "A+"} and in_title:
            out.append(symbol)
        elif grade == "A+" and len(base) >= 3:
            out.append(symbol)  # provider top-confidence tag on a non-CL asset counts even without a literal ticker
    return tuple(sorted(set(out)))


def asset_class_of(grounded: Sequence[str], macro: bool) -> AssetClass:
    if any(s.startswith("XYZ-") for s in grounded):
        return "equity_or_commodity"
    if grounded:
        return "crypto"
    if macro:
        return "macro"
    return "none"


def evaluate_gate(inp: GateInput) -> GateVerdict:
    title = inp.title
    macro = bool(MACRO_LEXICON.search(title))
    energy = bool(ENERGY_LEXICON.search(title))
    pr_template = bool(PR_TEMPLATE_LEXICON.search(title))
    grounded = grounded_assets(title, inp.coins)
    score = float(inp.provider_score) if inp.provider_score is not None else 0.0
    watch_hits = tuple(sorted(s for s in grounded if _base_symbol(s) in inp.watchlist_symbols))
    reasons: list[str] = []

    if inp.ingest_mode == "recovery":
        admission: Admission = "recovery"
        reasons.append("recovery_never_delivers")
    elif inp.engine_type == "listing" or "1353" in inp.strategy_ids:
        admission = "listing_deterministic"
    elif pr_template and not grounded:
        admission = "suppressed_pr_template"
    elif not grounded and not macro:
        admission = "suppressed_ungrounded_meme" if inp.engine_type == "meme" else "suppressed_ungrounded"
    elif inp.engine_type == "market" and score and score < 80:
        admission = "suppressed_low_signal"
    else:
        admission = "candidate"

    high = score >= 90 or bool(watch_hits) or (macro and bool(_HIGH_PRIORITY_MACRO.search(title)))
    return GateVerdict(
        admission=admission,
        priority="high" if high else "normal",
        asset_class=asset_class_of(grounded, macro),
        grounded_assets=grounded,
        macro_lexicon=macro,
        energy_lexicon=energy,
        pr_template=pr_template,
        watchlist_hits=watch_hits,
        reasons=tuple(reasons),
    )


__all__ = [
    "ENERGY_LEXICON",
    "GATE_LEXICON_VERSION",
    "MACRO_LEXICON",
    "PR_TEMPLATE_LEXICON",
    "GateInput",
    "GateVerdict",
    "asset_class_of",
    "evaluate_gate",
    "grounded_assets",
]
