"""Gate v4: deterministic evidence, priority, and orthogonal vetoes (pure).

The Gate no longer decides relevance and keeps no name table of its own: the provider already resolved entities into
``coins[]`` with a grade, so a B+/A/A+ tag (or a literal ``$TICKER`` cashtag) *is* the grounded asset; Triage — the
model — verifies which of them are primary. The lexicons only set priority, the energy context for ``CL``, and the
preliminary storyline theme. The only admissions that skip the model are recovery replays, deterministic listing
notices, law-firm PR templates without a grounded asset, and — behind an operator switch that defaults off —
low-score ungrounded social posts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .instruments import NON_CRYPTO_CLASSES
from .models import Admission, AssetClass, EngineType

GATE_LEXICON_VERSION: Final = "news_gate_lexicon_v2"

ENERGY_LEXICON = re.compile(
    r"\b(oil|crude|brent|wti|opec|hormuz|strait|tanker|barrel|bpd|refiner\w*|pipeline|gasoline|diesel|lng|natural gas"
    r"|novorossiysk|houthi|red sea|shale|rig count|eia|iran|iraq|saudi|aramco|oman|qatar|kuwait|uae|energy)\b"
    r"|石油|原油|油价|布油|美油|霍尔木兹|欧佩克|油轮|伊朗",
    re.IGNORECASE,
)
MACRO_LEXICON = re.compile(
    r"\b(fed|fomc|powell|rate (cut|hike|decision)|cpi|ppi|pce|nonfarm|payrolls?|jobless|unemployment|gdp|ism|pmi"
    r"|retail sales|treasury|treasuries|yields?|tariffs?|trade (deal|talks)|ecb|boj|boe|pboc|lpr|mlf|dollar index|dxy"
    r"|bond|housing starts|building permits|import prices|export prices|stagflation|recession)\b"
    r"|美联储|加息|降息|非农|关税|国务院|央行|收益率|国债|通胀|营建许可|新屋开工|进口物价|滞胀",
    re.IGNORECASE,
)
# Law-firm solicitation templates. The strong list (firm names, template phrases) is vetoed outright — a real
# company event never reads like this; the weak list ("class action", "law firm") only vetoes ungrounded titles.
PR_TEMPLATE_STRONG_LEXICON = re.compile(
    r"levi & korsinsky|pomerantz|rosen law|hagens berman|\bhbss\b|bragar eagel|glancy prongay|faruqi|kessler topaz"
    r"|schall law|portnoy law|johnson fistel|kahn swick|robbins geller|bernstein liebhard|the gross law"
    r"|securities (investigation|class action) notice|investigation notice|investor alert|shareholder alert"
    r"|deadline (alert|reminder)|class period|investors? (who|that) (lost|suffered)|lead plaintiff"
    r"|encourages investors|urged to contact|opportunity to lead|faces securities class action"
    r"|sued for securities fraud|alleged misrepresentations|investors learn of",
    re.IGNORECASE,
)
PR_TEMPLATE_LEXICON = re.compile(
    r"securities (investigation|class action)|law ?firm|class action|securities fraud lawsuit", re.IGNORECASE
)
_HIGH_PRIORITY_MACRO = re.compile(r"yield|rate|fed|fomc|cpi|收益率|加息|降息|美联储", re.IGNORECASE)
# Provider tags whose symbol collides with an ordinary English word, so the tag is usually about the word rather
# than the asset ("near-instant" -> NEAR, "SPOT GOLD" -> SPOT, "the Clarity bill" -> BILL). This is a *collision*
# list, not a tradability list: all of these except SPOT/CORE/PRIME are real listed contracts (OPENAI and ANTHROPIC
# trade on binance.perp and hl.vntl), so the instrument universe cannot replace it — the two conditions are
# independent and both apply (#75).
_TICKER_TAG_STOP: Final = frozenset(
    {"OPENAI", "ANTHROPIC", "GENIUS", "ACT", "NEAR", "W", "LIQUID", "SPOT", "CORE", "PRIME", "BILL", "FLOCK"}
)
_GROUNDING_GRADES: Final = frozenset({"B+", "A", "A+"})
_STRONG_GRADES: Final = frozenset({"A", "A+"})
_LOW_SIGNAL_SCORE: Final = 70.0
_MARKET_TELEMETRY_MIN_SCORE: Final = 80.0


@dataclass(frozen=True, slots=True)
class GateInput:
    title: str
    engine_type: EngineType
    strategy_ids: tuple[str, ...]
    provider_score: float | None
    coins: tuple[Mapping[str, Any], ...]
    ingest_mode: str  # live | recovery
    watchlist_symbols: frozenset[str] = frozenset()
    raw_first_line: str = ""
    suppress_low_signal: bool = False
    # #89: symbol -> instrument_class from the venue snapshot (aliases included). None falls back to the `XYZ-`
    # prefix heuristic, which is what every pure caller and a worker whose first snapshot has not landed gets.
    instrument_classes: Mapping[str, str] | None = None


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
    strong_assets: tuple[str, ...] = field(default_factory=tuple)  # A/A+ or cashtag: may open a preliminary storyline

    @property
    def amqp_priority(self) -> int:
        return 5 if self.priority == "high" else 0


def _base_symbol(symbol: str) -> str:
    return symbol.replace("XYZ-", "").upper()


def _cashtag_in_text(symbol: str, text: str) -> bool:
    return re.search(rf"\${re.escape(symbol)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None


def grounded_assets(
    title: str,
    coins: Sequence[Mapping[str, Any]],
    *,
    raw_first_line: str = "",
    strong_only: bool = False,
) -> tuple[str, ...]:
    """Provider coins the pipeline treats as grounded: grade B+/A/A+ tags and any literal ``$TICKER`` cashtag.

    The provider already resolved names to symbols (Bitcoin -> BTC:A, Home Depot -> HD:A, SafePal -> SFP:A); the
    Gate does not second-guess that with its own name table. ``CL`` (crude) counts only in energy context and a
    short stop-list drops tags whose symbol collides with an English word. Triage decides which grounded assets are
    primary; ``decide()`` only trusts primaries that are also grounded. ``strong_only`` keeps A/A+ tags and
    cashtags — the ones allowed to open a preliminary storyline before Triage has spoken.

    Existence on a venue is deliberately *not* a condition. #75 shipped that filter behind a flag and a dry-run
    killed it: of a full week's grounding tags, every one the provider had mapped to a venue itself (the ``XYZ-``
    form) was already listed, and the tags it would have removed were real equities with no crypto perp — Telix's
    half-year results, UWM's class action. The universe labels a tag (#89); it does not judge it.
    """

    text = f"{title} {raw_first_line}".strip()
    energy = bool(ENERGY_LEXICON.search(text))
    grades = _STRONG_GRADES if strong_only else _GROUNDING_GRADES
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
        if grade in grades or _cashtag_in_text(base, text):
            out.append(symbol)
    return tuple(sorted(set(out)))


def asset_class_of(
    grounded: Sequence[str], macro: bool, *, instrument_classes: Mapping[str, str] | None = None
) -> AssetClass:
    """Coin, stock, macro, or nothing — read from the instrument universe when it is available.

    The provider emits an ``XYZ-`` twin whenever it has mapped a tag to Hyperliquid's equity DEX itself, and that
    twin is a perfect signal when it comes. It just does not always come: a week of live traffic had 47 events
    whose only tag was a bare ``MRNA`` / ``CIEN`` / ``PANW``, read as crypto while the universe held them as
    equities all along (#89). A symbol the universe does not know at all keeps the old reading — the equities with
    no crypto perp are #91's subject, not something this function can invent.
    """

    if instrument_classes:
        seen = {instrument_classes.get(_base_symbol(s)) for s in grounded}
        if seen & NON_CRYPTO_CLASSES:
            return "equity_or_commodity"
        if "crypto" in seen:
            return "crypto"
    if any(s.startswith("XYZ-") for s in grounded):
        return "equity_or_commodity"
    if grounded:
        return "crypto"
    if macro:
        return "macro"
    return "none"


def evaluate_gate(inp: GateInput) -> GateVerdict:
    title = inp.title
    text = f"{title} {inp.raw_first_line}".strip()
    macro = bool(MACRO_LEXICON.search(text))
    energy = bool(ENERGY_LEXICON.search(text))
    pr_strong = bool(PR_TEMPLATE_STRONG_LEXICON.search(text))
    pr_template = pr_strong or bool(PR_TEMPLATE_LEXICON.search(text))
    grounded = grounded_assets(title, inp.coins, raw_first_line=inp.raw_first_line)
    strong = grounded_assets(title, inp.coins, raw_first_line=inp.raw_first_line, strong_only=True)
    score = float(inp.provider_score) if inp.provider_score is not None else 0.0
    watch_hits = tuple(sorted(s for s in grounded if _base_symbol(s) in inp.watchlist_symbols))
    reasons: list[str] = []

    listing = inp.engine_type == "listing" or "1353" in inp.strategy_ids
    # #137: strategy 1019 is the provider's OI Event Monitor, read off the frame's own metadata exactly
    # as 1353 marks a listing notice — provenance, not configuration. Its frames are a fixed-format
    # telemetry template, so they are admitted and judged by a deterministic rule instead of the model.
    telemetry = "1019" in inp.strategy_ids
    if inp.ingest_mode == "recovery":
        admission: Admission = "recovery"
        reasons.append("recovery_never_delivers")
    elif listing:
        admission = "listing_deterministic"
    elif telemetry:
        admission = "telemetry_deterministic"
    elif pr_strong or (pr_template and not grounded):
        admission = "suppressed_pr_template"
        reasons.append("law_firm_template" if pr_strong else "law_firm_template_without_asset")
    # A missing provider score is `0.0`, so the old `and score` guard skipped this rule for exactly the frames
    # it exists to hold back. It never mattered while an allowlist decided which Strategies could reach the
    # Gate at all; #126 removed that, so an unscored market frame would otherwise cost a Triage call and could
    # reach a reader. No score is not evidence of signal.
    elif inp.engine_type == "market" and score < _MARKET_TELEMETRY_MIN_SCORE:
        admission = "suppressed_low_signal"
        reasons.append("market_telemetry_below_min_score")
    elif (
        inp.suppress_low_signal
        and inp.engine_type == "meme"
        and not grounded
        and not macro
        and score < _LOW_SIGNAL_SCORE
    ):
        admission = "suppressed_low_signal"
        reasons.append("ungrounded_social_below_min_score")
    else:
        admission = "candidate"

    high = score >= 90 or bool(watch_hits) or listing or (macro and bool(_HIGH_PRIORITY_MACRO.search(text)))
    return GateVerdict(
        admission=admission,
        priority="high" if high else "normal",
        asset_class=asset_class_of(grounded, macro, instrument_classes=inp.instrument_classes),
        grounded_assets=grounded,
        macro_lexicon=macro,
        energy_lexicon=energy,
        pr_template=pr_template,
        watchlist_hits=watch_hits,
        reasons=tuple(reasons),
        strong_assets=strong,
    )


__all__ = [
    "ENERGY_LEXICON",
    "GATE_LEXICON_VERSION",
    "MACRO_LEXICON",
    "PR_TEMPLATE_LEXICON",
    "PR_TEMPLATE_STRONG_LEXICON",
    "GateInput",
    "GateVerdict",
    "asset_class_of",
    "evaluate_gate",
    "grounded_assets",
]
