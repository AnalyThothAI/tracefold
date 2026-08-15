from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceOperationOverrun,
)

PUSH_PAYLOAD_SCHEMA_VERSION = "news_item_push_v1"
PUSH_TRANSLATION_POLICY_VERSION = "title_zh_v4"

_TRANSLATION_TOTAL_TIMEOUT_SECONDS = 1.5
_DELIVERY_TOTAL_TIMEOUT_SECONDS = 7.5
_DELIVERY_OPERATION_TIMEOUT_SECONDS = 7.0
_MAX_TITLE_GRAPHEMES = 500
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,120}$")
_ANCHOR = re.compile(r"(?<![\w])(?:\$?[A-Z][A-Z0-9._/-]{1,14}|\d+(?:[.,]\d+)*(?:%|bp|bps)?)(?![\w])")


@dataclass(frozen=True, slots=True)
class NewsPushReceipt:
    provider: str
    receipt_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


class NewsPushTranslator(Protocol):
    async def translate(self, title: str) -> str: ...

    async def close(self) -> None: ...


class NewsPushSender(Protocol):
    def send(
        self,
        source_payload: Mapping[str, Any],
        presentation_snapshot: Mapping[str, Any],
    ) -> NewsPushReceipt: ...

    def close(self) -> None: ...


class NewsPushExternalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = _sanitize_error(code, fallback="news_item_push_external_error")
        super().__init__(self.code)


class NewsItemPush:
    """One deep Item-scoped outbound turn.

    Callers know only reconciliation, one due turn, and lifecycle close.
    Selection, best-effort translation, the durable Feishu fence, rendering,
    external delivery, and settlement remain inside this module.
    """

    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        translator: NewsPushTranslator | None,
        sender: NewsPushSender | None,
        delivery_available: bool,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if delivery_available and sender is None:
            raise ValueError("news_item_push_sender_required")
        self.db = db
        self.finite_operations = finite_operations
        self.translator = translator
        self.sender = sender
        self.delivery_available = bool(delivery_available)
        self._clock_ms = clock_ms or _now_ms

    async def reconcile(
        self,
        *,
        now_ms: int | None = None,
    ) -> dict[str, int | bool | None]:
        effective_now_ms = self._clock_ms() if now_ms is None else int(now_ms)
        return cast(
            dict[str, int | bool | None],
            await self.db.run_business(
                "news_item_push_reconcile",
                self._reconcile_sync,
                self.delivery_available,
                effective_now_ms,
                operation_timeout_seconds=3.0,
            ),
        )

    async def turn(self) -> bool | None:
        if not self.delivery_available or self.sender is None:
            return False
        try:
            row = await self.db.run_business(
                "news_item_push_peek",
                self._peek_sync,
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
            return None
        if row is None:
            return False

        source_payload = dict(row["source_payload"])
        presentation = await self._presentation(source_payload)
        attempted_at_ms = self._clock_ms()
        try:
            fenced = await self.db.run_business(
                "news_item_push_fence",
                self._fence_sync,
                str(row["item_id"]),
                presentation,
                attempted_at_ms,
                operation_timeout_seconds=0.5,
            )
        except ResourceAdmissionTimeout:
            return None
        except ResourceOperationOverrun:
            raise RuntimeError("news_item_push_fence_outcome_unknown") from None
        if fenced is None:
            return False

        send_submitted = False

        def mark_send_submitted() -> None:
            nonlocal send_submitted
            send_submitted = True

        try:
            async with asyncio.timeout(_DELIVERY_TOTAL_TIMEOUT_SECONDS):
                receipt = await self.finite_operations.run(
                    "news_item_push_feishu_send",
                    self.sender.send,
                    source_payload,
                    presentation,
                    timeout_seconds=_DELIVERY_OPERATION_TIMEOUT_SECONDS,
                    on_submitted=mark_send_submitted,
                )
            if not isinstance(receipt, NewsPushReceipt) or receipt.provider.strip().lower() != "feishu":
                raise NewsPushExternalError("news_item_push_feishu_receipt_invalid")
        except NewsPushExternalError as exc:
            return await self._terminalize(
                item_id=str(row["item_id"]),
                error_code=exc.code,
            )
        except ResourceAdmissionTimeout:
            return await self._terminalize(
                item_id=str(row["item_id"]),
                error_code="news_item_push_feishu_admission_timeout",
            )
        except ResourceOperationOverrun:
            await self._terminalize(
                item_id=str(row["item_id"]),
                error_code="news_item_push_feishu_timeout",
            )
            # The native call may still own a thread. Stop this runtime after
            # preserving the terminal audit so another Item cannot overlap it.
            raise RuntimeError("news_item_push_feishu_operation_overrun") from None
        except TimeoutError:
            await self._terminalize(
                item_id=str(row["item_id"]),
                error_code="news_item_push_feishu_timeout",
            )
            if send_submitted:
                raise RuntimeError("news_item_push_feishu_operation_overrun") from None
            return True
        except Exception:
            return await self._terminalize(
                item_id=str(row["item_id"]),
                error_code="news_item_push_feishu_failed",
            )

        try:
            completed = await self.db.run_business(
                "news_item_push_complete",
                self._complete_sync,
                str(row["item_id"]),
                _receipt_payload(receipt),
                self._clock_ms(),
                operation_timeout_seconds=0.5,
            )
        except (ResourceAdmissionTimeout, ResourceOperationOverrun):
            raise RuntimeError("news_item_push_sent_settlement_unavailable") from None
        if not completed:
            raise RuntimeError("news_item_push_sent_fence_lost")
        return True

    async def close(self) -> None:
        if self.translator is not None:
            await self.translator.close()
        if self.sender is not None:
            await self.finite_operations.run(
                "news_item_push_sender_close",
                self.sender.close,
                timeout_seconds=5.0,
                allow_shutdown=True,
            )

    async def _presentation(self, source_payload: Mapping[str, Any]) -> dict[str, Any]:
        original_title = _source_title(source_payload)
        if _looks_chinese(original_title):
            return {
                "display_title": original_title,
                "outcome": "not_needed",
                "translation_policy_version": PUSH_TRANSLATION_POLICY_VERSION,
            }
        if _grapheme_count_exceeds(original_title, _MAX_TITLE_GRAPHEMES):
            return _fallback_presentation(
                original_title,
                "news_item_push_translation_input_too_long",
            )
        if self.translator is None:
            return _fallback_presentation(
                original_title,
                "news_item_push_translation_unavailable",
            )

        started_at_ms = self._clock_ms()
        try:
            async with asyncio.timeout(_TRANSLATION_TOTAL_TIMEOUT_SECONDS):
                translated = await self.translator.translate(original_title)
            display_title = _validated_translation(
                original_title=original_title,
                translated_title=translated,
            )
        except NewsPushExternalError as exc:
            return _fallback_presentation(
                original_title,
                exc.code,
                translation_duration_ms=max(0, self._clock_ms() - started_at_ms),
            )
        except TimeoutError:
            return _fallback_presentation(
                original_title,
                "news_item_push_translation_timeout",
                translation_duration_ms=max(0, self._clock_ms() - started_at_ms),
            )
        except Exception:
            return _fallback_presentation(
                original_title,
                "news_item_push_translation_failed",
                translation_duration_ms=max(0, self._clock_ms() - started_at_ms),
            )
        return {
            "display_title": display_title,
            "outcome": "translated",
            "translation_duration_ms": max(0, self._clock_ms() - started_at_ms),
            "translation_policy_version": PUSH_TRANSLATION_POLICY_VERSION,
        }

    async def _terminalize(self, *, item_id: str, error_code: str) -> bool:
        try:
            changed = await self.db.run_business(
                "news_item_push_terminalize",
                self._terminalize_sync,
                item_id,
                _sanitize_error(error_code, fallback="news_item_push_delivery_failed"),
                self._clock_ms(),
                operation_timeout_seconds=0.5,
            )
        except (ResourceAdmissionTimeout, ResourceOperationOverrun):
            raise RuntimeError("news_item_push_terminal_settlement_unavailable") from None
        if not changed:
            raise RuntimeError("news_item_push_terminal_fence_lost")
        return True

    def _reconcile_sync(
        self,
        delivery_available: bool,
        now_ms: int,
    ) -> dict[str, int | bool | None]:
        with self.db.worker_session("news_item_push_reconcile", 3.0) as repos:
            return cast(
                dict[str, int | bool | None],
                repos.news.reconcile_item_push(
                    delivery_available=delivery_available,
                    now_ms=now_ms,
                ),
            )

    def _peek_sync(self) -> dict[str, Any] | None:
        with self.db.worker_session("news_item_push_peek", 3.0) as repos:
            return cast(dict[str, Any] | None, repos.news.peek_item_push())

    def _fence_sync(
        self,
        item_id: str,
        presentation_snapshot: Mapping[str, Any],
        attempted_at_ms: int,
    ) -> dict[str, Any] | None:
        with self.db.worker_session("news_item_push_fence", 0.5) as repos:
            return cast(
                dict[str, Any] | None,
                repos.news.fence_item_push(
                    item_id=item_id,
                    presentation_snapshot=presentation_snapshot,
                    attempted_at_ms=attempted_at_ms,
                ),
            )

    def _complete_sync(
        self,
        item_id: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_item_push_complete", 0.5) as repos:
            return cast(
                bool,
                repos.news.complete_item_push(
                    item_id=item_id,
                    receipt=receipt,
                    now_ms=now_ms,
                ),
            )

    def _terminalize_sync(
        self,
        item_id: str,
        error_code: str,
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_item_push_terminalize", 0.5) as repos:
            return cast(
                bool,
                repos.news.terminalize_item_push(
                    item_id=item_id,
                    error_code=error_code,
                    now_ms=now_ms,
                ),
            )


def _source_title(source_payload: Mapping[str, Any]) -> str:
    if source_payload.get("schema_version") != PUSH_PAYLOAD_SCHEMA_VERSION:
        raise RuntimeError("news_item_push_source_schema_invalid")
    title = str(source_payload.get("original_title") or "").strip()
    if not title:
        raise RuntimeError("news_item_push_source_title_invalid")
    return title


def _fallback_presentation(
    original_title: str,
    code: str,
    *,
    translation_duration_ms: int | None = None,
) -> dict[str, Any]:
    presentation: dict[str, Any] = {
        "display_title": original_title,
        "fallback_code": _sanitize_error(
            code,
            fallback="news_item_push_translation_failed",
        ),
        "outcome": "fallback",
        "translation_policy_version": PUSH_TRANSLATION_POLICY_VERSION,
    }
    if translation_duration_ms is not None:
        presentation["translation_duration_ms"] = max(
            0,
            int(translation_duration_ms),
        )
    return presentation


def _validated_translation(
    *,
    original_title: str,
    translated_title: object,
) -> str:
    translated = " ".join(str(translated_title or "").split())
    try:
        translated.encode("utf-8")
    except UnicodeEncodeError:
        raise NewsPushExternalError("news_item_push_translation_output_invalid") from None
    if (
        not translated
        or "\x00" in translated
        or not _contains_han(translated)
        or _grapheme_count_exceeds(translated, _MAX_TITLE_GRAPHEMES)
    ):
        raise NewsPushExternalError("news_item_push_translation_output_invalid")
    for anchor in _required_anchors(original_title):
        if anchor not in translated:
            raise NewsPushExternalError("news_item_push_translation_anchors_changed")
    return translated


def _required_anchors(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0) for match in _ANCHOR.finditer(value)))


def _looks_chinese(value: str) -> bool:
    letters = [character for character in value if character.isalpha()]
    if not letters:
        return False
    han = sum(_is_han(character) for character in letters)
    return han >= max(1, len(letters) // 2)


def _contains_han(value: str) -> bool:
    return any(_is_han(character) for character in value)


def _is_han(character: str) -> bool:
    return "CJK UNIFIED IDEOGRAPH" in unicodedata.name(character, "")


def _grapheme_count_exceeds(value: str, limit: int) -> bool:
    return len(_graphemes(value)) > limit


def _graphemes(value: str) -> list[str]:
    clusters: list[str] = []
    current = ""
    join_next = False
    for character in value:
        codepoint = ord(character)
        extends = (
            bool(unicodedata.combining(character)) or 0xFE00 <= codepoint <= 0xFE0F or 0x1F3FB <= codepoint <= 0x1F3FF
        )
        if not current:
            current = character
        elif character == "\u200d":
            current += character
            join_next = True
        elif extends or join_next:
            current += character
            join_next = False
        else:
            clusters.append(current)
            current = character
    if current:
        clusters.append(current)
    return clusters


def _receipt_payload(receipt: NewsPushReceipt) -> dict[str, Any]:
    result: dict[str, Any] = {"provider": "feishu"}
    if receipt.receipt_id:
        result["receipt_id"] = str(receipt.receipt_id).strip()[:256]
    details = {
        key: int(value)
        for key in ("code", "status_code")
        if (value := receipt.details.get(key)) is not None and isinstance(value, int) and not isinstance(value, bool)
    }
    if details:
        result["details"] = details
    return result


def _sanitize_error(value: object, *, fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if _ERROR_CODE.fullmatch(normalized) else fallback


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "PUSH_PAYLOAD_SCHEMA_VERSION",
    "PUSH_TRANSLATION_POLICY_VERSION",
    "NewsItemPush",
    "NewsPushExternalError",
    "NewsPushReceipt",
]
