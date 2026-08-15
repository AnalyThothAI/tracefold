from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

PUSH_PAYLOAD_SCHEMA_VERSION = "news_item_push_v1"

_DELIVERY_TOTAL_TIMEOUT_SECONDS = 7.5
_DELIVERY_OPERATION_TIMEOUT_SECONDS = 7.0
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,120}$")


@dataclass(frozen=True, slots=True)
class NewsPushReceipt:
    provider: str
    receipt_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


class NewsPushSender(Protocol):
    def send(
        self,
        source_payload: Mapping[str, Any],
        presentation: Mapping[str, Any],
    ) -> NewsPushReceipt: ...

    def close(self) -> None: ...


class NewsPushExternalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = _sanitize_error(code, fallback="news_item_push_external_error")
        super().__init__(self.code)


class NewsItemPush:
    """Deliver one Item after its exact shared presentation is resolved."""

    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        sender: NewsPushSender | None,
        delivery_available: bool,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if delivery_available and sender is None:
            raise ValueError("news_item_push_sender_required")
        self.db = db
        self.finite_operations = finite_operations
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
        presentation = dict(row["presentation"])
        _validate_shared_presentation(source_payload, presentation)
        attempted_at_ms = self._clock_ms()
        try:
            fenced = await self.db.run_business(
                "news_item_push_fence",
                self._fence_sync,
                str(row["item_id"]),
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
            return await self._terminalize(item_id=str(row["item_id"]), error_code=exc.code)
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
        if self.sender is not None:
            await self.finite_operations.run(
                "news_item_push_sender_close",
                self.sender.close,
                timeout_seconds=5.0,
                allow_shutdown=True,
            )

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

    def _fence_sync(self, item_id: str, attempted_at_ms: int) -> dict[str, Any] | None:
        with self.db.worker_session("news_item_push_fence", 0.5) as repos:
            return cast(
                dict[str, Any] | None,
                repos.news.fence_item_push(
                    item_id=item_id,
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
                repos.news.complete_item_push(item_id=item_id, receipt=receipt, now_ms=now_ms),
            )

    def _terminalize_sync(self, item_id: str, error_code: str, now_ms: int) -> bool:
        with self.db.worker_session("news_item_push_terminalize", 0.5) as repos:
            return cast(
                bool,
                repos.news.terminalize_item_push(
                    item_id=item_id,
                    error_code=error_code,
                    now_ms=now_ms,
                ),
            )


def _validate_shared_presentation(
    source_payload: Mapping[str, Any],
    presentation: Mapping[str, Any],
) -> None:
    if source_payload.get("schema_version") != PUSH_PAYLOAD_SCHEMA_VERSION:
        raise RuntimeError("news_item_push_source_schema_invalid")
    original_title = str(source_payload.get("original_title") or "")
    if not original_title or presentation.get("original_title") != original_title:
        raise RuntimeError("news_item_push_presentation_identity_invalid")
    if not str(presentation.get("display_title") or ""):
        raise RuntimeError("news_item_push_presentation_display_title_invalid")


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
    "NewsItemPush",
    "NewsPushExternalError",
    "NewsPushReceipt",
]
