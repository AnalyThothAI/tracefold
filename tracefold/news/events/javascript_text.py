"""Pinned JavaScript text semantics shared by News provider adapters and Event normalization.

This module contains no identity, similarity, or clustering behavior.
"""

from __future__ import annotations

import re
from typing import Final

JAVASCRIPT_WHITESPACE: Final = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
JAVASCRIPT_WHITESPACE_PATTERN: Final = f"[{re.escape(JAVASCRIPT_WHITESPACE)}]"
_JS_WHITESPACE_RE = re.compile(rf"{JAVASCRIPT_WHITESPACE_PATTERN}+")


def utf16_slice(text: str, stop: int) -> str:
    encoded = str(text).encode("utf-16-le", errors="surrogatepass")
    return encoded[: max(0, stop) * 2].decode("utf-16-le", errors="surrogatepass")


def web_usv_string(text: str) -> str:
    utf16 = str(text).encode("utf-16-le", errors="surrogatepass")
    return utf16.decode("utf-16-le", errors="replace")


def utf16_length(text: str) -> int:
    return len(str(text).encode("utf-16-le", errors="surrogatepass")) // 2


def javascript_trim(text: str) -> str:
    return str(text).strip(JAVASCRIPT_WHITESPACE)


def collapse_javascript_whitespace(text: str) -> str:
    return javascript_trim(_JS_WHITESPACE_RE.sub(" ", str(text)))


__all__ = [
    "JAVASCRIPT_WHITESPACE",
    "JAVASCRIPT_WHITESPACE_PATTERN",
    "collapse_javascript_whitespace",
    "javascript_trim",
    "utf16_length",
    "utf16_slice",
    "web_usv_string",
]
