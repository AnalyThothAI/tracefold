"""Gate v4: deterministic evidence, queue scheduling, and orthogonal vetoes (pure).

The Gate no longer decides relevance and keeps no name table of its own: the provider already resolved entities into
``coins[]`` with a grade, so a B+/A/A+ tag (or a literal ``$TICKER`` cashtag) *is* the grounded asset; Triage — the
model — verifies which of them are primary. The only admissions that skip the model are recovery replays,
deterministic listing notices, law-firm PR templates without a grounded asset, and unscored or under-80 market
telemetry frames (#126). Provider-specific deterministic lanes compose after this policy.

The three word lists this policy still needs — the energy context that lets a ``CL`` tag ground, the macro reading
behind ``asset_class="macro"``, and the queue-order subset — are **not defined here**. They are flags on the
storyline registry (#509 D3): ``gate.energy_context`` on the energy topic and the Gulf places, ``gate.macro`` on the
central banks and the macro topics, ``gate.queue_high`` on the subset a desk wants ahead of the queue. The Gate reads
whatever ``events.storyline`` matched, so "energy" is one vocabulary with one owner instead of a regex here and a
theme there that disagreed about ``iranian``, 沙特, ``barrels`` and every central bank outside the Fed. Only the
law-firm PR templates stay regexes: they are sentence templates, not a storyline anyone groups by.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ..market_review.instruments import NON_CRYPTO_CLASSES
from ..models import Admission, AssetClass, EngineType
from .storyline import match_storyline, storyline_entry

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
_MARKET_TELEMETRY_MIN_SCORE: Final = 80.0


@dataclass(frozen=True, slots=True)
class GateFlags:
    """The three registry readings this policy makes of one text (#509 D3).

    ``energy`` is the context a bare ``CL`` tag needs before it counts as a grounded asset; ``macro`` is what
    ``asset_class_of`` turns into ``"macro"`` and what the model sees as evidence; ``queue_high`` is broker
    scheduling only. Each is "any matched entry carries this flag", so adding a word to the desk's vocabulary is
    a registry row, and the Gate and the storyline key can no longer disagree about what counts as energy.
    """

    energy: bool = False
    macro: bool = False
    queue_high: bool = False


def gate_lexicon_flags(text: str) -> GateFlags:
    """Read ``energy`` / ``macro`` / ``queue_high`` off the storyline registry entries ``text`` matches.

    The registry owns the matching rules — NFKC + case-fold, word boundaries for Latin aliases and substrings for
    every other script, longest alias first — so this function is only the disjunction. The v5 regexes it replaces
    read a *different* vocabulary from the storyline lexicon standing next to them: word-bounded ``iran`` that
    missed "Iranian", ``pboc`` and a bare 央行 in place of the other nine central banks, and an unbounded
    high-priority pattern whose ``rate`` matched "accelerate" and whose ``fed`` matched "federal". There is now
    one list, and one place to add to it.
    """

    energy = macro = queue_high = False
    for hit in match_storyline(text):
        entry = storyline_entry(hit.entry_id)
        gate = entry.gate if entry is not None else None
        if gate is None:
            continue
        energy = energy or gate.energy_context
        macro = macro or gate.macro
        queue_high = queue_high or gate.queue_high
    return GateFlags(energy=energy, macro=macro, queue_high=queue_high)


@dataclass(frozen=True, slots=True)
class GateInput:
    title: str
    engine_type: EngineType
    provider_score: float | None
    coins: tuple[Mapping[str, Any], ...]
    ingest_mode: str  # live | recovery
    watchlist_symbols: frozenset[str] = frozenset()
    raw_first_line: str = ""
    # #89: symbol -> instrument_class from the venue snapshot (aliases included). None falls back to the `XYZ-`
    # prefix heuristic, which is what every pure caller and a worker whose first snapshot has not landed gets.
    instrument_classes: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class GateVerdict:
    admission: Admission
    queue_priority: str  # high | normal; broker scheduling only
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
        return 5 if self.queue_priority == "high" else 0


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
    energy: bool | None = None,
) -> tuple[str, ...]:
    """Provider coins the pipeline treats as grounded: grade B+/A/A+ tags and any literal ``$TICKER`` cashtag.

    The provider already resolved names to symbols (Bitcoin -> BTC:A, Home Depot -> HD:A, SafePal -> SFP:A); the
    Gate does not second-guess that with its own name table. ``CL`` (crude) counts only in energy context and a
    short stop-list drops tags whose symbol collides with an English word. Triage decides which grounded assets are
    primary; ``decide()`` only trusts primaries that are also grounded. ``strong_only`` keeps A/A+ tags and
    cashtags — the ones allowed to open a preliminary storyline before Triage has spoken. ``energy`` lets a
    caller that already read the registry for this exact text hand the answer in; ``None`` reads it here.

    Existence on a venue is deliberately *not* a condition. #75 shipped that filter behind a flag and a dry-run
    killed it: of a full week's grounding tags, every one the provider had mapped to a venue itself (the ``XYZ-``
    form) was already listed, and the tags it would have removed were real equities with no crypto perp — Telix's
    half-year results, UWM's class action. The universe labels a tag (#89); it does not judge it.
    """

    text = f"{title} {raw_first_line}".strip()
    if energy is None:
        energy = gate_lexicon_flags(text).energy
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
    flags = gate_lexicon_flags(text)
    macro = flags.macro
    pr_strong = bool(PR_TEMPLATE_STRONG_LEXICON.search(text))
    pr_template = pr_strong or bool(PR_TEMPLATE_LEXICON.search(text))
    # One registry scan per event: the flags are all `grounded_assets` needed the text for, and matching it
    # three times cost ~70 µs an event on the live corpus.
    grounded = grounded_assets(title, inp.coins, raw_first_line=inp.raw_first_line, energy=flags.energy)
    strong = grounded_assets(title, inp.coins, raw_first_line=inp.raw_first_line, strong_only=True, energy=flags.energy)
    score = float(inp.provider_score) if inp.provider_score is not None else 0.0
    watch_hits = tuple(sorted(s for s in grounded if _base_symbol(s) in inp.watchlist_symbols))
    reasons: list[str] = []

    listing = inp.engine_type == "listing"
    if inp.ingest_mode == "recovery":
        admission: Admission = "recovery"
        reasons.append("recovery_never_delivers")
    elif listing:
        admission = "listing_deterministic"
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
    else:
        admission = "candidate"

    # `queue_high` entries are a subset of the `macro` ones (asserted on the registry), so the old
    # `macro and <high-priority pattern>` conjunction is the flag itself.
    high = score >= 90 or bool(watch_hits) or listing or flags.queue_high
    return GateVerdict(
        admission=admission,
        queue_priority="high" if high else "normal",
        asset_class=asset_class_of(grounded, macro, instrument_classes=inp.instrument_classes),
        grounded_assets=grounded,
        macro_lexicon=macro,
        energy_lexicon=flags.energy,
        pr_template=pr_template,
        watchlist_hits=watch_hits,
        reasons=tuple(reasons),
        strong_assets=strong,
    )


__all__ = [
    "PR_TEMPLATE_LEXICON",
    "PR_TEMPLATE_STRONG_LEXICON",
    "GateFlags",
    "GateInput",
    "GateVerdict",
    "asset_class_of",
    "evaluate_gate",
    "gate_lexicon_flags",
    "grounded_assets",
]
