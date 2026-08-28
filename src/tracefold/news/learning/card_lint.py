"""Deterministic ReaderCard copy lint: the card rules a ruler can check without a model or a Gold label.

Until #306 Phase 1 the ReaderCard side of `accepted_review_metric` could score exactly one thing —
`factual_fidelity`, and only through the sealed equivalence judge (#203/#204). Everything else the card
contract asks for (no banned filler, no meta opening, no self-description, no emoji or URL, a headline
that keeps the original's numbers and stays inside its length band, a single-sentence `why_zh`) was
enforced only by prose inside the RulePack and by the reviewed coverage anchors around it. Prose is not a
ruler: an optimizer cannot be scored against a sentence, and a candidate that dropped every number from a
headline lost no points at all.

This module turns that subset into code. It is pure and framework-neutral by construction — no DSPy, no
database, no provider, no reviewer label — which is what lets the metric, the Objective Plan's mirrored
gate ladder and any offline report read the same answer.

Two severities, and the split is deliberate (#306 Phase 1, last checklist item):

``gate``   The card is not a reader card at all: it carries a URL, or it describes the writer as a model.
           Neither can occur in legitimate reader copy under any reading of the contract, so they zero the
           case the way `must_hold_send` does rather than being averaged against copy quality.
``score``  Everything else, the Chinese-language boundary included. Each applicable check is one point in
           the `reader_card_lint` component, so a candidate that keeps six of eight is measurably better
           than one that keeps two — the property a gate cannot express, and the reason the gate set is
           exactly two entries long rather than the whole contract.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

CARD_LINT_ID: Final[str] = "tracefold.news.reader_card_lint_v1"

HEADLINE_MIN_CHARS: Final[int] = 15
HEADLINE_MAX_CHARS: Final[int] = 60
WHY_MAX_CHARS: Final[int] = 140
# The evaluative/meta filler the card contract forbids, moved out of RulePack prose into a code table.
# Matched against a whitespace-stripped, casefolded, NFC-normalized copy of the text, so `RWA 叙事` and
# `RWA叙事` are the same hit.
BANNED_FILLER: Final[tuple[str, ...]] = (
    "值得关注",
    "值得警惕",
    "有明确信息价值",
    "重大进展",
    "具有重要意义",
    "利好",
    "利空",
    "看涨",
    "看跌",
    "或将",
    "有望",
    "市场普遍认为",
    "机构采用趋势",
    "rwa叙事",
    "信息疲劳",
    "单一来源",
    "风险提示",
    "直接读数",
    "关键读数",
    "直接信号",
    "风向标",
    "反映",
    "显示出",
)
# The one banned phrase the RulePack states with an ellipsis (`对…板块有影响`), so it cannot be a literal.
_BANNED_FILLER_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (re.compile(r"对.{0,12}板块有影响"),)

META_OPENINGS: Final[tuple[str, ...]] = ("该消息", "这条新闻", "本次事件", "该新闻", "本条消息", "此消息")

# High-confidence self-description only. A card that says `模型` about someone else's product is ordinary
# news; these phrases are the model talking about itself.
SELF_DESCRIPTION: Final[tuple[str, ...]] = (
    "人工智能助手",
    "语言模型",
    "大语言模型",
    "本模型",
    "该模型认为",
    "ai模型判断",
    "作为ai",
    "作为一个ai",
    "作为一名ai",
    "本次判断",
    "我的判断",
    "asanai",
    "asanaimodel",
    "iamanai",
    "languagemodel",
)

_URL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"://"),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.(?:com|net|org|io|cn|xyz|co|ai|app|info)\b", re.IGNORECASE),
)

# Conservative, block-level emoji and pictograph ranges. Arrows and CJK punctuation are deliberately
# outside them: a card is allowed to write `→` or `——`.
_EMOJI_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0xFE0F, 0xFE0F),
    (0x1F000, 0x1FAFF),
)

_CJK_RANGES: Final[tuple[tuple[int, int], ...]] = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))

# The three CJK sentence terminators and their ASCII counterparts. A bare `.` is deliberately not one:
# every decimal the card is required to keep would read as a sentence end. Nor is a semicolon — a
# semicolon-joined clause is still one sentence, and counting it would fail cards the contract allows.
_SENTENCE_TERMINALS: Final[str] = "\u3002\uff01\uff1f!?"

# A standalone numeric literal: a digit run that does not continue a word. Without the lookbehind every
# digit inside an identifier (`COVID19`, a hex digest, a model name) would become a number the headline is
# required to carry, and the check would fail cards that dropped nothing. Trailing letters stay allowed so
# `$1.5B`, `99-105 MMBOE` and `3%` keep their numbers.
_NUMBER_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![0-9A-Za-z])\d+(?:\.\d+)?")

_FULLWIDTH_DIGITS: Final[dict[int, str]] = {ord("０") + offset: str(offset) for offset in range(10)}

GateCode = Literal["card_lint_url", "card_lint_self_description"]

# Code-owned and content-addressed with the rest of the ruler: a check that appears or disappears changes
# the receipt.
# Every check this module can report, in the fixed order the metric publishes them.
SCORED_CHECKS: Final[tuple[str, ...]] = (
    "headline_language",
    "headline_length",
    "headline_number_count",
    "banned_filler",
    "meta_opening",
    "why_length",
    "why_single_sentence",
    "no_emoji",
)

GATE_CHECKS: Final[tuple[str, ...]] = ("card_lint_url", "card_lint_self_description")


@dataclass(frozen=True, slots=True)
class CardLintResult:
    """One card's complete deterministic verdict: the gate it tripped, and how it did on the rest."""

    gate: str
    gate_detail: str
    outcomes: tuple[tuple[str, str], ...]
    feedback: tuple[str, ...]

    @property
    def applicable(self) -> tuple[str, ...]:
        return tuple(name for name, outcome in self.outcomes if outcome != "lint_not_applicable")

    @property
    def passed(self) -> int:
        return sum(1 for _name, outcome in self.outcomes if outcome == "lint_pass")

    @property
    def score(self) -> float | None:
        """The `reader_card_lint` component score, or ``None`` when no check applied to this card."""

        denominator = len(self.applicable)
        return round(self.passed / denominator, 6) if denominator else None


def _normalized(value: str) -> str:
    """NFC, casefolded, whitespace-free, full-width digits folded to ASCII — one shape to match against."""

    text = unicodedata.normalize("NFC", str(value or "")).translate(_FULLWIDTH_DIGITS)
    return re.sub(r"\s+", "", text).casefold()


def _in_ranges(char: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in ranges)


def _numbers(value: str) -> tuple[str, ...]:
    """Decision-relevant numeric literals, thousands separators removed and full-width digits folded.

    Deliberately every literal rather than a guess at which ones matter: the card contract says *every*
    decision-relevant number survives, and a rule that tried to classify them would drop the one an
    operator cared about. The comparison below is a substring test, which is the lenient direction — the
    check fails only when a number is genuinely absent from the candidate headline.
    """

    text = unicodedata.normalize("NFC", str(value or "")).translate(_FULLWIDTH_DIGITS)
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    seen: list[str] = []
    for match in _NUMBER_PATTERN.finditer(text):
        token = match.group(0)
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def _url_hit(text: str) -> str:
    for pattern in _URL_PATTERNS:
        found = pattern.search(text)
        if found is not None:
            return found.group(0)
    return ""


def _self_description_hit(normalized: str) -> str:
    return next((marker for marker in SELF_DESCRIPTION if marker in normalized), "")


def _banned_filler_hits(normalized: str, raw: str) -> tuple[str, ...]:
    hits = [marker for marker in BANNED_FILLER if marker in normalized]
    hits.extend(found.group(0) for pattern in _BANNED_FILLER_PATTERNS if (found := pattern.search(raw)) is not None)
    return tuple(hits)


def _meta_opening_hit(headline: str, why: str) -> str:
    for text in (headline, why):
        opening = _normalized(text)
        hit = next((marker for marker in META_OPENINGS if opening.startswith(marker)), "")
        if hit:
            return hit
    return ""


def _emoji_hits(*texts: str) -> tuple[str, ...]:
    found = [char for text in texts for char in str(text or "") if _in_ranges(char, _EMOJI_RANGES)]
    return tuple(dict.fromkeys(found))


def _extra_sentences(why: str) -> int:
    """Terminals that are not the one closing punctuation mark a single sentence is allowed."""

    text = unicodedata.normalize("NFC", str(why or "")).strip()
    if not text:
        return 0
    body = text[:-1] if text[-1] in _SENTENCE_TERMINALS else text
    return sum(1 for char in body if char in _SENTENCE_TERMINALS) + body.count("\n")


def lint_reader_card(*, headline_zh: str, why_zh: str, source_title: str = "") -> CardLintResult:
    """Apply the code-owned card copy contract to one candidate card.

    ``source_title`` is the immutable Event headline the card was written from. Without it the number
    check has nothing to compare against and reports ``lint_not_applicable`` rather than a pass: an absent
    input must not look like a satisfied requirement.
    """

    headline = str(headline_zh or "").strip()
    why = str(why_zh or "").strip()
    combined = f"{headline}\n{why}"
    normalized_combined = _normalized(combined)

    url = _url_hit(combined)
    if url:
        return _gated("card_lint_url", url, f"Reader copy must not contain a URL; found {url!r}.")
    self_description = _self_description_hit(normalized_combined)
    if self_description:
        return _gated(
            "card_lint_self_description",
            self_description,
            f"Reader copy must never describe the writer as a model or a judgment; found {self_description!r}.",
        )
    outcomes: list[tuple[str, str]] = []
    feedback: list[str] = []

    chinese = bool(headline) and any(_in_ranges(char, _CJK_RANGES) for char in headline)
    outcomes.append(("headline_language", "lint_pass" if chinese else "lint_fail"))
    if not chinese:
        feedback.append("headline_zh must be written in Chinese; the candidate headline carries no Chinese character.")

    length = len(headline)
    if HEADLINE_MIN_CHARS <= length <= HEADLINE_MAX_CHARS:
        outcomes.append(("headline_length", "lint_pass"))
    else:
        outcomes.append(("headline_length", "lint_fail"))
        feedback.append(
            f"headline_zh is {length} characters; keep it between {HEADLINE_MIN_CHARS} and "
            f"{HEADLINE_MAX_CHARS}, and never drop a number or a critical clause to get there."
        )

    # How many, not which. A faithful Chinese rendering routinely restates a number in a different
    # notation — `$1.5B` becomes `15亿美元`, `$520M` becomes `5.2亿`, `$200BN` becomes `2000亿`, `5.50%`
    # becomes `5.5%` — so a literal-identity test fails exactly the conversions the contract asks for, and
    # its feedback would teach the optimizer to copy the source's ASCII digits instead. Counting is the
    # deterministic shadow of "every decision-relevant number survives" that survives that rewrite, and it
    # still catches the failure the contract names by example: a headline that drops them.
    source_numbers = _numbers(source_title)
    if not source_numbers:
        outcomes.append(("headline_number_count", "lint_not_applicable"))
    else:
        kept = len(_numbers(headline))
        outcomes.append(("headline_number_count", "lint_fail" if kept < len(source_numbers) else "lint_pass"))
        if kept < len(source_numbers):
            feedback.append(
                f"headline_zh carries {kept} of the {len(source_numbers)} decision-relevant numbers the "
                "original headline stated; keep every amount, percentage, price level, deadline and count, "
                "converting the unit rather than dropping the figure."
            )

    filler = _banned_filler_hits(normalized_combined, combined)
    outcomes.append(("banned_filler", "lint_fail" if filler else "lint_pass"))
    if filler:
        feedback.append(
            f"Reader copy used banned evaluative filler ({', '.join(filler)}); state the concrete "
            "mechanism instead: who holds what, what happens next, which price or business result it feeds."
        )

    meta = _meta_opening_hit(headline, why)
    outcomes.append(("meta_opening", "lint_fail" if meta else "lint_pass"))
    if meta:
        feedback.append(f"Reader copy must not open with a meta phrase; it opened with {meta!r}.")

    why_length = len(why)
    outcomes.append(("why_length", "lint_pass" if why_length <= WHY_MAX_CHARS else "lint_fail"))
    if why_length > WHY_MAX_CHARS:
        feedback.append(f"why_zh is {why_length} characters; keep it within {WHY_MAX_CHARS}.")

    extra = _extra_sentences(why)
    outcomes.append(("why_single_sentence", "lint_fail" if extra else "lint_pass"))
    if extra:
        feedback.append("why_zh must be at most one plain sentence; it carries more than one.")

    emoji = _emoji_hits(headline, why)
    outcomes.append(("no_emoji", "lint_fail" if emoji else "lint_pass"))
    if emoji:
        feedback.append(f"Reader copy must not contain emoji or pictographs; found {''.join(emoji)}.")

    return CardLintResult(gate="", gate_detail="", outcomes=tuple(outcomes), feedback=tuple(feedback))


def _gated(code: str, detail: str, message: str) -> CardLintResult:
    """A gated card reports the gate and nothing else.

    The remaining checks are deliberately not evaluated: they would enter the component denominator of a
    case whose score is already zero, and publishing per-check outcomes for a card the ruler refused would
    make the `reader_card_lint` denominator disagree with the score every gate produces.
    """

    return CardLintResult(gate=code, gate_detail=detail, outcomes=(), feedback=(message,))


def card_lint_receipt() -> dict[str, object]:
    """The code-owned tables this lint is, in the shape the metric receipt embeds."""

    return {
        "card_lint_id": CARD_LINT_ID,
        "hard_gates": list(GATE_CHECKS),
        "scored_checks": list(SCORED_CHECKS),
        "banned_filler": list(BANNED_FILLER),
        "banned_filler_patterns": [pattern.pattern for pattern in _BANNED_FILLER_PATTERNS],
        "meta_openings": list(META_OPENINGS),
        "self_description": list(SELF_DESCRIPTION),
        "headline_chars": {"min": HEADLINE_MIN_CHARS, "max": HEADLINE_MAX_CHARS},
        "why_chars_max": WHY_MAX_CHARS,
        "headline_number_rule": "count_preserved_not_literal_identity",
    }


__all__ = [
    "BANNED_FILLER",
    "CARD_LINT_ID",
    "GATE_CHECKS",
    "HEADLINE_MAX_CHARS",
    "HEADLINE_MIN_CHARS",
    "META_OPENINGS",
    "SCORED_CHECKS",
    "SELF_DESCRIPTION",
    "WHY_MAX_CHARS",
    "CardLintResult",
    "card_lint_receipt",
    "lint_reader_card",
]
