"""Content-block title extraction and pinned corpus prefix/suffix normalization."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Final

from .identity import comparison_title

MAX_TITLE_CHARS: Final = 500
MIN_CONTENT_TOKENS: Final = 3

_BREAK_RE = re.compile(r"<br\s*/?>|\r\n|\r|\n", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_LABEL_LINE_RE = re.compile(r"^.{0,40}[:：]\s*$")
_SOURCE_LABELS: Final = (
    "the block|decrypt|cointelegraph|coindesk|chainwire|prn|prnewswire|reuters|bloomberg|wsj|cnbc|first squawk"
    "|new|breaking|latest|just in|update|insight|live updates|source|alert|urgent|developing|exclusive|ap|bbc|cnn"
    "|techcrunch|fortune|crowdfundinsider|the street|jin10|金十数据|deitaone|walter bloomberg"
)
_EXCHANGES: Final = r"binance|bitget|robinhood|coinbase|okx|bybit|kraken|upbit|bithumb|gate\.io|kucoin|htx|mexc"
_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:reply|quote:|rt)\s+"
    r"|\*+\s*"
    r"|(?:" + _SOURCE_LABELS + r")\s*[:|\-–—]\s*"
    r"|\$[A-Z]{1,6}\s*[-–—]\s*"
    r")+",
    re.IGNORECASE,
)
# An exchange name is a *subject*, not a source label: strip "Binance: ..." only when the remainder still names
# the exchange (a redundant label such as "Binance: Binance Will List ..."); keep "Binance: Notice on ..." intact.
_EXCHANGE_LABEL_RE = re.compile(r"^(?P<name>" + _EXCHANGES + r")\s*[:|\-–—]\s*(?P<rest>.+)$", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<![\w@])\.?@(?=[A-Za-z0-9_]{1,32}\b)")
_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*(?:ft|bbg|rtrs|reuters|wsj|cnbc|kyodo|ria|fox news|bloomberg|the block|decrypt|coindesk|source)"
    r"\s*\.?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ExtractedTitle:
    title: str
    comparison: str
    first_line: str
    token_count: int
    url_slug: bool


def _clean_block(block: str) -> str:
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", block)).strip()


def _strip_noise(text: str) -> str:
    """Drop URLs, source labels, and social decoration; keep every subject (exchange names, @handles as words)."""

    stripped = _URL_RE.sub(" ", text).strip()
    stripped = _PREFIX_RE.sub("", stripped).strip()
    label = _EXCHANGE_LABEL_RE.match(stripped)
    if label and re.search(rf"\b{re.escape(label.group('name'))}\b", label.group("rest"), re.IGNORECASE):
        stripped = label.group("rest").strip()
    stripped = _HANDLE_RE.sub("", stripped)  # "@Krakenfx launches" -> "Krakenfx launches": the handle is the subject
    stripped = _PREFIX_RE.sub("", stripped).strip()
    stripped = _SUFFIX_RE.sub("", stripped).strip()
    return _SPACE_RE.sub(" ", stripped)


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def extract_title(text: str) -> ExtractedTitle:
    """Pick the first *content* block: skip URL-only, label-only, and prefix-only lines."""

    decoded = html.unescape(str(text or ""))
    blocks = [b for b in (_clean_block(raw) for raw in _BREAK_RE.split(decoded)) if b]
    first_line = blocks[0] if blocks else ""
    for block in blocks:
        candidate = _strip_noise(block)
        if _word_count(candidate) >= MIN_CONTENT_TOKENS and not _LABEL_LINE_RE.match(candidate):
            title = candidate[:MAX_TITLE_CHARS]
            comparison = comparison_title(title)
            return ExtractedTitle(title, comparison, first_line, _word_count(comparison), False)
    if first_line:
        match = _URL_RE.search(first_line)
        if match:
            path = re.sub(r"^https?://[^/]+", "", match.group(0))
            slug = _SPACE_RE.sub(" ", re.sub(r"[-_/?=&.]+", " ", path)).strip()
            if slug:
                title = slug[:MAX_TITLE_CHARS]
                comparison = comparison_title(title)
                return ExtractedTitle(title, comparison, first_line, _word_count(comparison), True)
        fallback = _strip_noise(first_line) or first_line
        title = fallback[:MAX_TITLE_CHARS]
        comparison = comparison_title(title)
        return ExtractedTitle(title, comparison, first_line, _word_count(comparison), False)
    return ExtractedTitle("", "", "", 0, False)


def description_after_title(text: str, *, limit: int = 600) -> str:
    decoded = html.unescape(str(text or ""))
    blocks = [b for b in (_clean_block(raw) for raw in _BREAK_RE.split(decoded)) if b]
    if len(blocks) <= 1:
        return ""
    return " ".join(blocks[1:])[:limit]


__all__ = [
    "MAX_TITLE_CHARS",
    "MIN_CONTENT_TOKENS",
    "ExtractedTitle",
    "description_after_title",
    "extract_title",
]
