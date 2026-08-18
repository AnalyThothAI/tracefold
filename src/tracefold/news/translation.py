"""Title translation waterfall: DeepL (1.5 s) -> DeepSeek (5 s) -> original (pure logic; providers injected)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Protocol

import regex

DEEPL_DEADLINE_SECONDS = 1.5
DEEPSEEK_DEADLINE_SECONDS = 5.0
MAX_TITLE_GRAPHEMES = 500

_HAN = regex.compile(r"\p{Han}")
_HORIZONTAL_SPACE = regex.compile(r"[\p{Zs}\t\f\v]+")
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,120}$")


class TitleTranslationProvider(Protocol):
    async def translate(self, title: str) -> str: ...

    async def close(self) -> None: ...


class TitleTranslationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = sanitize_error(code, fallback="news_translation_provider_failed")
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class TranslationOutcome:
    display_title: str
    outcome: str  # translated | not_needed | fallback
    provider: str | None
    fallback_code: str | None


def looks_chinese(value: str) -> bool:
    letters = [character for character in value if character.isalpha()]
    if not letters:
        return False
    han = sum(_HAN.fullmatch(character) is not None for character in letters)
    return han > 0 and han * 2 >= len(letters)


def grapheme_count(value: str) -> int:
    return len(regex.findall(r"\X", value))


def sanitize_error(value: object, *, fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if _ERROR_CODE.fullmatch(normalized) else fallback


def validate_display_title(value: object) -> str:
    raw = str(value or "")
    if "\x00" in raw or "\r" in raw or "\n" in raw:
        raise TitleTranslationError("news_translation_output_invalid")
    normalized = _HORIZONTAL_SPACE.sub(" ", raw).strip()
    if not normalized or _HAN.search(normalized) is None or grapheme_count(normalized) > MAX_TITLE_GRAPHEMES:
        raise TitleTranslationError("news_translation_output_invalid")
    return normalized


async def translate_title(
    original_title: str,
    *,
    deepl: TitleTranslationProvider | None,
    deepseek: TitleTranslationProvider | None,
) -> TranslationOutcome:
    if looks_chinese(original_title):
        return TranslationOutcome(original_title, "not_needed", None, None)
    if grapheme_count(original_title) > MAX_TITLE_GRAPHEMES:
        return TranslationOutcome(original_title, "fallback", None, "news_translation_input_too_long")
    if deepl is None and deepseek is None:
        return TranslationOutcome(original_title, "fallback", None, "news_translation_provider_unavailable")
    last_error = "news_translation_provider_unavailable"
    for name, provider, deadline in (
        ("deepl", deepl, DEEPL_DEADLINE_SECONDS),
        ("deepseek", deepseek, DEEPSEEK_DEADLINE_SECONDS),
    ):
        if provider is None:
            continue
        try:
            translated = await asyncio.wait_for(provider.translate(original_title), timeout=deadline)
            return TranslationOutcome(validate_display_title(translated), "translated", name, None)
        except TitleTranslationError as exc:
            last_error = exc.code
        except TimeoutError:
            last_error = f"news_translation_{name}_timeout"
        except Exception:
            last_error = f"news_translation_{name}_failed"
    return TranslationOutcome(
        original_title, "fallback", None, sanitize_error(last_error, fallback="news_translation_provider_failed")
    )


__all__ = [
    "DEEPL_DEADLINE_SECONDS",
    "DEEPSEEK_DEADLINE_SECONDS",
    "MAX_TITLE_GRAPHEMES",
    "TitleTranslationError",
    "TitleTranslationProvider",
    "TranslationOutcome",
    "grapheme_count",
    "looks_chinese",
    "sanitize_error",
    "translate_title",
    "validate_display_title",
]
