"""Pinned JavaScript text semantics shared by News adapters and Brief code.

Story identity itself lives exclusively in ``story_projection``.  This module
contains no similarity score or clustering behavior.
"""

from __future__ import annotations

import math
import re
from typing import Final

import regex

JAVASCRIPT_WHITESPACE: Final = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
JAVASCRIPT_WHITESPACE_PATTERN: Final = f"[{re.escape(JAVASCRIPT_WHITESPACE)}]"
_UNICODE_LETTER_OR_NUMBER_RE = regex.compile(r"[^\p{L}\p{N}]")
_UNICODE_UPPERCASE_LETTER_RE = regex.compile(r"\p{Lu}")
_UNICODE_LOWERCASE_LETTER_RE = regex.compile(r"\p{Ll}")
_JS_WHITESPACE_RE = re.compile(rf"{JAVASCRIPT_WHITESPACE_PATTERN}+")

# Node at the pinned WorldMonitor revision uses Unicode 17.  Freeze newer
# lowercase mappings while Python 3.13 still ships Unicode 15.1.
_UNICODE_17_LOWER_TRANSLATION: Final = str.maketrans(
    {
        0x1C89: 0x1C8A,
        0xA7CB: 0x0264,
        0xA7CC: 0xA7CD,
        0xA7DA: 0xA7DB,
        0xA7DC: 0x019B,
        0xA7CE: 0xA7CF,
        0xA7D2: 0xA7D3,
        0xA7D4: 0xA7D5,
        **{codepoint: codepoint + 0x20 for codepoint in range(0x10D50, 0x10D66)},
        **{codepoint: codepoint + 0x1B for codepoint in range(0x16EA0, 0x16EB9)},
    }
)


def utf16_slice(text: str, stop: int) -> str:
    encoded = str(text).encode("utf-16-le", errors="surrogatepass")
    return encoded[: max(0, stop) * 2].decode("utf-16-le", errors="surrogatepass")


def web_usv_string(text: str) -> str:
    utf16 = str(text).encode("utf-16-le", errors="surrogatepass")
    return utf16.decode("utf-16-le", errors="replace")


def utf16_length(text: str) -> int:
    return len(str(text).encode("utf-16-le", errors="surrogatepass")) // 2


def utf16_sort_key(text: str) -> tuple[int, ...]:
    encoded = str(text).encode("utf-16-le", errors="surrogatepass")
    return tuple(encoded[index] | (encoded[index + 1] << 8) for index in range(0, len(encoded), 2))


def javascript_trim(text: str) -> str:
    return str(text).strip(JAVASCRIPT_WHITESPACE)


def collapse_javascript_whitespace(text: str) -> str:
    return javascript_trim(_JS_WHITESPACE_RE.sub(" ", str(text)))


def parse_javascript_number(text: str) -> float:
    normalized = javascript_trim(str(text))
    if not normalized:
        return 0.0
    for prefix, pattern, base in (
        ("0x", r"0[xX][0-9A-Fa-f]+", 16),
        ("0b", r"0[bB][01]+", 2),
        ("0o", r"0[oO][0-7]+", 8),
    ):
        if re.fullmatch(pattern, normalized):
            try:
                return float(int(normalized[len(prefix) :], base))
            except OverflowError:
                return math.inf
    if (
        re.fullmatch(
            r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|Infinity)",
            normalized,
        )
        is None
    ):
        return math.nan
    try:
        return float(normalized)
    except (OverflowError, ValueError):
        return math.nan


def javascript_lower(text: str) -> str:
    return str(text).lower().translate(_UNICODE_17_LOWER_TRANSLATION)


def javascript_is_letter_or_number(value: str) -> bool:
    r"""Mirror pinned JavaScript ``/[\p{L}\p{N}]/u.test(value)``."""

    return bool(value) and _UNICODE_LETTER_OR_NUMBER_RE.search(value) is None


def javascript_starts_with_uppercase_letter(value: str) -> bool:
    r"""Mirror pinned JavaScript ``/^\p{Lu}/u.test(value)``."""

    return bool(value) and _UNICODE_UPPERCASE_LETTER_RE.match(value) is not None


def javascript_starts_with_lowercase_letter(value: str) -> bool:
    r"""Mirror pinned JavaScript ``/^\p{Ll}/u.test(value)``."""

    return bool(value) and _UNICODE_LOWERCASE_LETTER_RE.match(value) is not None


__all__ = [
    "JAVASCRIPT_WHITESPACE",
    "JAVASCRIPT_WHITESPACE_PATTERN",
    "collapse_javascript_whitespace",
    "javascript_is_letter_or_number",
    "javascript_lower",
    "javascript_starts_with_lowercase_letter",
    "javascript_starts_with_uppercase_letter",
    "javascript_trim",
    "parse_javascript_number",
    "utf16_length",
    "utf16_slice",
    "utf16_sort_key",
    "web_usv_string",
]
