from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any, Protocol, cast

import regex

from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceOperationOverrun,
)

from .title_presentation_store import TitlePresentationStore

TITLE_PRESENTATION_POLICY_VERSION = "news_title_zh_v1"
DEEPL_DEADLINE_SECONDS = 1.5
DEEPSEEK_DEADLINE_SECONDS = 5.0
MAX_TITLE_GRAPHEMES = 500

_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,120}$")
_HAN = regex.compile(r"\p{Han}")
_HORIZONTAL_SPACE = regex.compile(r"[\p{Zs}\t\f\v]+")


class TitleTranslationProvider(Protocol):
    async def translate(self, title: str) -> str: ...

    async def close(self) -> None: ...


class TitleTranslationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = _sanitize_error(code, fallback="news_title_presentation_provider_failed")
        super().__init__(self.code)


class NewsItemTitlePresentation:
    """Resolve one durable display decision per exact Item title identity."""

    def __init__(
        self,
        *,
        db: Any,
        deepl: TitleTranslationProvider | None,
        deepseek: TitleTranslationProvider | None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.deepl = deepl
        self.deepseek = deepseek
        self._clock_ms = clock_ms or _now_ms

    async def reconcile(self, *, now_ms: int | None = None) -> int:
        effective_now_ms = self._clock_ms() if now_ms is None else int(now_ms)
        return cast(
            int,
            await self.db.run_business(
                "news_title_presentation_reconcile",
                self._reconcile_sync,
                effective_now_ms,
                operation_timeout_seconds=3.0,
            ),
        )

    async def turn(self) -> bool | None:
        try:
            row = await self.db.run_business(
                "news_title_presentation_peek",
                self._peek_sync,
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
            return None
        if row is None:
            return False

        original_title = str(row["original_title"])
        if _looks_chinese(original_title):
            return await self._resolve_without_provider(
                row,
                outcome="not_needed",
                fallback_code=None,
            )
        if _grapheme_count(original_title) > MAX_TITLE_GRAPHEMES:
            return await self._resolve_without_provider(
                row,
                outcome="fallback",
                fallback_code="news_title_presentation_input_too_long",
            )
        if self.deepl is None and self.deepseek is None:
            return await self._resolve_without_provider(
                row,
                outcome="fallback",
                fallback_code="news_title_presentation_provider_unavailable",
            )

        attempted_at_ms = self._clock_ms()
        try:
            fenced = await self.db.run_business(
                "news_title_presentation_fence",
                self._fence_sync,
                str(row["item_id"]),
                str(row["source_title_fingerprint"]),
                attempted_at_ms,
                operation_timeout_seconds=0.5,
            )
        except ResourceAdmissionTimeout:
            return None
        except ResourceOperationOverrun:
            raise RuntimeError("news_title_presentation_fence_outcome_unknown") from None
        if not fenced:
            return False

        display_title, provider, fallback_code = await self._provider_waterfall(original_title)
        resolved_at_ms = self._clock_ms()
        outcome = "translated" if provider is not None else "fallback"
        return await self._settle(
            row,
            expected_state="resolving",
            display_title=display_title,
            outcome=outcome,
            provider=provider,
            fallback_code=fallback_code,
            resolved_at_ms=resolved_at_ms,
            duration_ms=max(0, resolved_at_ms - attempted_at_ms),
        )

    async def close(self) -> None:
        closed: set[int] = set()
        for provider in (self.deepl, self.deepseek):
            if provider is None or id(provider) in closed:
                continue
            closed.add(id(provider))
            await provider.close()

    async def _provider_waterfall(
        self,
        original_title: str,
    ) -> tuple[str, str | None, str | None]:
        last_error = "news_title_presentation_provider_unavailable"
        for provider_name, provider, deadline in (
            ("deepl", self.deepl, DEEPL_DEADLINE_SECONDS),
            ("deepseek", self.deepseek, DEEPSEEK_DEADLINE_SECONDS),
        ):
            if provider is None:
                continue
            try:
                async with asyncio.timeout(deadline):
                    translated = await provider.translate(original_title)
                return _display_title(translated), provider_name, None
            except TitleTranslationError as exc:
                last_error = exc.code
            except TimeoutError:
                last_error = f"news_title_presentation_{provider_name}_timeout"
            except Exception:
                last_error = f"news_title_presentation_{provider_name}_failed"
        return (
            original_title,
            None,
            _sanitize_error(
                last_error,
                fallback="news_title_presentation_provider_failed",
            ),
        )

    async def _resolve_without_provider(
        self,
        row: dict[str, Any],
        *,
        outcome: str,
        fallback_code: str | None,
    ) -> bool | None:
        return await self._settle(
            row,
            expected_state="pending",
            display_title=str(row["original_title"]),
            outcome=outcome,
            provider=None,
            fallback_code=fallback_code,
            resolved_at_ms=self._clock_ms(),
            duration_ms=0,
        )

    async def _settle(
        self,
        row: dict[str, Any],
        *,
        expected_state: str,
        display_title: str,
        outcome: str,
        provider: str | None,
        fallback_code: str | None,
        resolved_at_ms: int,
        duration_ms: int,
    ) -> bool:
        try:
            resolved = await self.db.run_business(
                "news_title_presentation_resolve",
                self._resolve_sync,
                str(row["item_id"]),
                str(row["source_title_fingerprint"]),
                expected_state,
                display_title,
                outcome,
                provider,
                fallback_code,
                int(resolved_at_ms),
                int(duration_ms),
                operation_timeout_seconds=0.5,
            )
        except (ResourceAdmissionTimeout, ResourceOperationOverrun):
            raise RuntimeError("news_title_presentation_settlement_unavailable") from None
        if not resolved:
            raise RuntimeError("news_title_presentation_fence_lost")
        return True

    def _reconcile_sync(self, now_ms: int) -> int:
        with self.db.worker_session("news_title_presentation_reconcile", 3.0) as repos:
            return TitlePresentationStore(repos.conn).reconcile(
                now_ms=int(now_ms),
                policy_version=TITLE_PRESENTATION_POLICY_VERSION,
            )

    def _peek_sync(self) -> dict[str, Any] | None:
        with self.db.worker_session("news_title_presentation_peek", 3.0) as repos:
            return TitlePresentationStore(repos.conn).peek_pending()

    def _fence_sync(
        self,
        item_id: str,
        source_title_fingerprint: str,
        attempted_at_ms: int,
    ) -> bool:
        with self.db.worker_session("news_title_presentation_fence", 0.5) as repos:
            return TitlePresentationStore(repos.conn).fence(
                item_id=item_id,
                source_title_fingerprint=source_title_fingerprint,
                attempted_at_ms=attempted_at_ms,
            )

    def _resolve_sync(
        self,
        item_id: str,
        source_title_fingerprint: str,
        expected_state: str,
        display_title: str,
        outcome: str,
        provider: str | None,
        fallback_code: str | None,
        resolved_at_ms: int,
        duration_ms: int,
    ) -> bool:
        with self.db.worker_session("news_title_presentation_resolve", 0.5) as repos:
            return TitlePresentationStore(repos.conn).resolve(
                item_id=item_id,
                source_title_fingerprint=source_title_fingerprint,
                expected_state=expected_state,
                display_title=display_title,
                outcome=outcome,
                provider=provider,
                policy_version=TITLE_PRESENTATION_POLICY_VERSION,
                fallback_code=fallback_code,
                resolved_at_ms=resolved_at_ms,
                duration_ms=duration_ms,
            )


def _display_title(value: object) -> str:
    raw = str(value or "")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise TitleTranslationError("news_title_presentation_output_invalid") from None
    if "\x00" in raw or "\r" in raw or "\n" in raw:
        raise TitleTranslationError("news_title_presentation_output_invalid")
    normalized = _HORIZONTAL_SPACE.sub(" ", raw).strip()
    if not normalized or _HAN.search(normalized) is None:
        raise TitleTranslationError("news_title_presentation_output_invalid")
    if _grapheme_count(normalized) > MAX_TITLE_GRAPHEMES:
        raise TitleTranslationError("news_title_presentation_output_invalid")
    return normalized


def _looks_chinese(value: str) -> bool:
    letters = [character for character in value if character.isalpha()]
    if not letters:
        return False
    han = sum(_HAN.fullmatch(character) is not None for character in letters)
    return han > 0 and han * 2 >= len(letters)


def _grapheme_count(value: str) -> int:
    return len(regex.findall(r"\X", value))


def _sanitize_error(value: object, *, fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if _ERROR_CODE.fullmatch(normalized) else fallback


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "DEEPL_DEADLINE_SECONDS",
    "DEEPSEEK_DEADLINE_SECONDS",
    "MAX_TITLE_GRAPHEMES",
    "TITLE_PRESENTATION_POLICY_VERSION",
    "NewsItemTitlePresentation",
    "TitleTranslationError",
    "TitleTranslationProvider",
]
