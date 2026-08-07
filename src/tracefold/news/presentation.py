from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit

_BARE_URL = re.compile(r"https?://[^\s<>]+", flags=re.IGNORECASE)


class _PlainTextParser(HTMLParser):
    """Extract provider text without ever producing renderable markup."""

    _BREAK_TAGS = frozenset(
        {
            "br",
            "dd",
            "div",
            "dt",
            "hr",
            "li",
            "p",
            "td",
            "th",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in self._BREAK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BREAK_TAGS:
            self.parts.append(" ")


def normalize_news_display_text(value: object) -> str:
    """Return deterministic safe plain text while leaving persisted evidence untouched.

    This output is the News reading-layer title/description contract. Story title
    translation fingerprints this exact string, so every caller must use this one
    function instead of applying browser-only cleanup.
    """

    source = unicodedata.normalize("NFC", str(value or ""))
    parser = _PlainTextParser()
    try:
        parser.feed(source)
        parser.close()
    except (UnicodeError, ValueError):
        # HTMLParser is deliberately best effort. The fallback remains plain text
        # because angle brackets are converted to spacing below.
        extracted = source.replace("<", " ").replace(">", " ")
    else:
        extracted = "".join(parser.parts)
    without_controls = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character for character in extracted
    )
    without_bare_urls = _BARE_URL.sub(" ", without_controls)
    return " ".join(without_bare_urls.split())


def normalize_news_display_title(value: object) -> str:
    """Normalize a title and retain a safe label for URL-only provider rows."""

    normalized = normalize_news_display_text(value)
    if normalized:
        return normalized
    match = _BARE_URL.search(str(value or ""))
    if match is not None:
        try:
            hostname = str(urlsplit(match.group(0).rstrip(".,;:!?)]}")).hostname or "").strip().lower()
        except ValueError:
            hostname = ""
        if hostname:
            return hostname
    return "未命名新闻"


__all__ = ["normalize_news_display_text", "normalize_news_display_title"]
