"""Deterministic FactUnits for explicit multi-fact provider items.

One provider Item is usually one fact.  Some wire digests are an explicit
numbered list, though, and treating the whole digest as one model question can
make the card, Event identity, Gate assets, and review target refer to different
bullets.  This module only splits the high-confidence shape: at least three
sequential, explicitly numbered blocks.  Everything else remains one unit.

There is deliberately no model call and no fuzzy sentence splitter here.  A
false negative leaves the old, inspectable whole-item behaviour; a false
positive would manufacture Events, so the threshold is intentionally strict.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Final

FACT_UNIT_VERSION: Final = "news_fact_unit_v1"

_BREAK_RE = re.compile(r"<br\s*/?>|\r\n|\r|\n", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_NUMBERED_RE = re.compile(r"^\s*(?P<number>\d{1,2})[.)、:：]\s*(?P<text>\S.*)$")
# `10:30 中国8月社会消费品零售总额` is a clock time, not item 10.  A 财经日程 lists consecutive hours, so the
# numbering reads as sequential and the whole calendar splits into Events whose titles have lost their hour
# ("30 中国8月社会消费品零售总额").  Leading zeros do not help: `01:30 / 02:00 / 03:00` parses as 1, 2, 3.
_CLOCK_RE = re.compile(r"^\s*\d{1,2}[:：]\d{2}(?!\d)")
_WORD_RE = re.compile(r"\w")
_MIN_EXPLICIT_UNITS = 3
_MIN_FACT_CHARS = 12
# Aligned with the model-visible `content` bound so the lead is never trimmed twice.
_MAX_LEAD_CHARS = 600


@dataclass(frozen=True, slots=True)
class FactUnit:
    """One immutable question extracted from a provider Item."""

    fact_id: str
    ordinal: int
    text: str
    context: str
    span_start: int
    span_end: int
    method: str

    def as_dict(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "ordinal": self.ordinal,
            "text": self.text,
            "context": self.context,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "method": self.method,
            "version": FACT_UNIT_VERSION,
        }


def _fact_id(*, item_id: str, ordinal: int, text: str, method: str) -> str:
    normalized = _SPACE_RE.sub(" ", text).strip()
    material = f"{FACT_UNIT_VERSION}\x1f{item_id}\x1f{method}\x1f{ordinal}\x1f{normalized}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _blocks(raw_text: str) -> list[tuple[str, int, int]]:
    """Return cleaned blocks with spans in the decoded provider text."""

    decoded = html.unescape(str(raw_text or ""))
    out: list[tuple[str, int, int]] = []
    cursor = 0
    for match in _BREAK_RE.finditer(decoded):
        raw = decoded[cursor : match.start()]
        cleaned = _SPACE_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()
        if cleaned:
            out.append((cleaned, cursor, match.start()))
        cursor = match.end()
    raw = decoded[cursor:]
    cleaned = _SPACE_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()
    if cleaned:
        out.append((cleaned, cursor, len(decoded)))
    return out


def _lead_context(blocks: list[tuple[str, int, int]], *, first_numbered_start: int) -> str:
    """The digest's own preamble: every word-bearing block above the first numbered one.

    The block sitting directly on top of the list is the lead that gives every bullet its subject
    ("BREAKING: Nvidia ... Details include:"); anything higher is the poster's framing.  A long preamble is
    therefore budgeted from the bottom up, so the lead is what survives.  Separator-only blocks — the
    provider emits a bare ``|`` line between a quote tweet's two authors — carry no subject and are dropped.
    """

    preamble = [block for block, start, _ in blocks if start < first_numbered_start and _WORD_RE.search(block)]
    if not preamble:
        return ""
    kept: list[str] = []
    used = 0
    for block in reversed(preamble):
        cost = len(block) + (1 if kept else 0)
        if used + cost > _MAX_LEAD_CHARS:
            break
        kept.append(block)
        used += cost
    if not kept:
        return preamble[-1][:_MAX_LEAD_CHARS]
    return " ".join(reversed(kept))


def extract_fact_units(*, item_id: str, raw_text: str, fallback_title: str) -> tuple[FactUnit, ...]:
    """Split only a high-confidence explicit numbered digest.

    The numbered sequence may have an unnumbered preamble before it — which
    becomes every unit's shared context — but every emitted unit must be a
    numbered block, numbering must be contiguous, and there must be at least
    three units.  Otherwise a single whole-item unit is returned.
    """

    blocks = _blocks(raw_text)
    numbered: list[tuple[int, str, int, int]] = []
    for block, start, end in blocks:
        match = None if _CLOCK_RE.match(block) else _NUMBERED_RE.match(block)
        if match is None:
            continue
        text = _SPACE_RE.sub(" ", match.group("text")).strip()
        if len(text) < _MIN_FACT_CHARS:
            numbered = []
            break
        numbered.append((int(match.group("number")), text, start, end))

    numbers = [row[0] for row in numbered]
    sequential = bool(numbers) and numbers == list(range(numbers[0], numbers[0] + len(numbers)))
    if len(numbered) >= _MIN_EXPLICIT_UNITS and sequential:
        lead = _lead_context(blocks, first_numbered_start=numbered[0][2])
        return tuple(
            FactUnit(
                fact_id=_fact_id(item_id=item_id, ordinal=index, text=text, method="explicit_numbered"),
                ordinal=index,
                text=text,
                context=lead,
                span_start=start,
                span_end=end,
                method="explicit_numbered",
            )
            for index, (_, text, start, end) in enumerate(numbered)
        )

    title = _SPACE_RE.sub(" ", fallback_title).strip() or "(untitled)"
    context_blocks = [block for block, _, _ in blocks if block != title]
    context = " ".join(context_blocks)[:600]
    decoded = html.unescape(str(raw_text or ""))
    return (
        FactUnit(
            fact_id=_fact_id(item_id=item_id, ordinal=0, text=title, method="whole_item"),
            ordinal=0,
            text=title,
            context=context,
            span_start=0,
            span_end=len(decoded),
            method="whole_item",
        ),
    )


__all__ = ["FACT_UNIT_VERSION", "FactUnit", "extract_fact_units"]
