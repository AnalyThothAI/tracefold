"""Telegram Bot API adapters for News fanout and profile-bound trading."""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.client import HTTPException, HTTPSConnection
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from tracefold.news import (
    ReaderDeliveryPresentation,
    ReaderMarketMovement,
    ReaderTradeTarget,
    TelegramDeliveryCopyReceipt,
    TelegramDeliveryReceipt,
    telegram_card_facts,
)

_TELEGRAM_API_ORIGIN = "https://api.telegram.org"
_TELEGRAM_TIMEOUT_SECONDS = 6.5
_TELEGRAM_TOTAL_CALL_BUDGET_SECONDS = 7.0
_TELEGRAM_MIN_REQUEST_BUDGET_SECONDS = 0.05
_TELEGRAM_MAX_PHASE_TIMEOUT_SECONDS = 4.0
_TELEGRAM_TEXT_MAX = 4096
_TELEGRAM_RESPONSE_MAX_BYTES = 1024 * 1024
_SOURCE_URL_MAX_LENGTH = 2_048
_BOT_TOKEN_RE = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{30,80}$")
_BOT_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_PRIVATE_CHANNEL_ID_RE = re.compile(r"^-100[1-9][0-9]{5,15}$")
_PRIVATE_CHAT_ID_RE = re.compile(r"^[1-9][0-9]{5,15}$")
_TELEGRAM_NEGATIVE_TARGET_ID_RE = re.compile(r"^-[1-9][0-9]{5,15}$")
_LINKABLE_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
_BINANCE_FUTURES_PATH_RE = re.compile(r"^/en/futures/[A-Z0-9]{2,40}$")
_BINANCE_SPOT_PATH_RE = re.compile(r"^/en/trade/[A-Z0-9]{1,20}_[A-Z0-9]{1,20}$")
_TELEGRAM_HTML_TAG_RE = re.compile(r'</?(?:b|strong|blockquote)>|<a href="[^"]+">|</a>')
_BOT_API_METHODS = frozenset(
    {
        "answerCallbackQuery",
        "deleteMessage",
        "editMessageText",
        "getChat",
        "getChatMember",
        "getMe",
        "getUpdates",
        "sendMessage",
        "setMyCommands",
    }
)
_MAGNITUDE_LABELS = {
    "影响很小": "很小",
    "影响有限": "有限",
    "影响明显": "明显",
    "影响重大": "重大",
}
_HEADER_ICON = {"green": "🟢", "red": "🔴", "grey": "⚪"}
_READER_TIMEZONE = timezone(timedelta(hours=8))
_NEWSLIQUID_RELAY_HOSTS = frozenset({"news-history.newsliquid.com"})
_NEWSLIQUID_REUTERS_PATH_RE = re.compile(r"^/b/nL[0-9A-Z]+$")


@dataclass(frozen=True, slots=True)
class TelegramTradingUpdate:
    update_id: int
    callback_query_id: str
    actor_user_id: int
    chat_id: int
    message_id: int
    data: str
    authorized: bool
    chat_type: str = "private"
    update_kind: str = "callback"


class TelegramDeliveryError(RuntimeError):
    """A sanitized expected Telegram delivery failure."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _is_supported_telegram_target_id(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    normalized = str(value)
    return bool(_TELEGRAM_NEGATIVE_TARGET_ID_RE.fullmatch(normalized) or _PRIVATE_CHAT_ID_RE.fullmatch(normalized))


def _telegram_target_digest_input(chat_id: int) -> bytes:
    return f"telegram-target-v2:{chat_id}".encode()


def _telegram_call_result(
    *,
    client: httpx.Client,
    monotonic: Callable[[], float],
    method: str,
    payload: Mapping[str, Any],
    error_prefix: str,
    deadline_at: float,
) -> Any:
    remaining = deadline_at - monotonic()
    if remaining < _TELEGRAM_MIN_REQUEST_BUDGET_SECONDS:
        raise TelegramDeliveryError(f"{error_prefix}_budget_exhausted")
    # The credential-hiding transport below maps the four httpx phase values onto one
    # ``HTTPSConnection`` socket timeout.  Dividing the remaining budget by four here
    # therefore shortened every real Telegram call fourfold instead of bounding four
    # sequential phases.  Keep the socket inside the shared deadline while tolerating
    # the latency introduced by an operator VPN.
    phase_timeout = min(_TELEGRAM_MAX_PHASE_TIMEOUT_SECONDS, remaining)
    try:
        response = client.post(
            method,
            json=dict(payload),
            timeout=httpx.Timeout(
                connect=phase_timeout,
                read=phase_timeout,
                write=phase_timeout,
                pool=phase_timeout,
            ),
        )
    except (httpx.TimeoutException, httpx.TransportError):
        raise TelegramDeliveryError(f"{error_prefix}_transport_failed") from None
    status_code = int(response.status_code)
    rejection: object = None
    if status_code == 400:
        with suppress(ValueError):
            rejection = response.json()
    rejection_description = str(rejection.get("description") or "").lower() if isinstance(rejection, Mapping) else ""
    if (
        method == "editMessageText"
        and status_code == 400
        and isinstance(rejection, Mapping)
        and rejection.get("ok") is False
        and "message is not modified" in rejection_description
    ):
        raise TelegramDeliveryError(f"{error_prefix}_not_modified", status_code=status_code)
    if (
        method == "answerCallbackQuery"
        and status_code == 400
        and isinstance(rejection, Mapping)
        and rejection.get("ok") is False
        and ("query is too old" in rejection_description or "query id is invalid" in rejection_description)
    ):
        raise TelegramDeliveryError(f"{error_prefix}_expired", status_code=status_code)
    if status_code == 429 or status_code >= 500:
        raise TelegramDeliveryError(f"{error_prefix}_http_failed", status_code=status_code)
    if status_code < 200 or status_code >= 300:
        raise TelegramDeliveryError(f"{error_prefix}_http_rejected", status_code=status_code)
    try:
        body = response.json()
    except ValueError:
        raise TelegramDeliveryError(f"{error_prefix}_response_invalid", status_code=status_code) from None
    if not isinstance(body, Mapping) or body.get("ok") is not True:
        raise TelegramDeliveryError(f"{error_prefix}_business_rejected", status_code=status_code)
    return body.get("result")


class _TelegramHTTPSBotTransport(httpx.BaseTransport):
    """Inject the credential below httpx's URL/logging layer."""

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token
        self._ssl_context = ssl.create_default_context()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.strip("/")
        if method not in _BOT_API_METHODS or request.method != "POST":
            raise httpx.TransportError("telegram_transport_request_invalid", request=request)
        phase_timeouts = request.extensions.get("timeout") or {}
        configured_timeouts = [
            float(value)
            for value in phase_timeouts.values()
            if isinstance(value, int | float) and not isinstance(value, bool) and float(value) > 0
        ]
        socket_timeout = min(configured_timeouts, default=_TELEGRAM_MAX_PHASE_TIMEOUT_SECONDS)
        connection = HTTPSConnection(
            "api.telegram.org",
            443,
            timeout=socket_timeout,
            context=self._ssl_context,
        )
        try:
            connection.request(
                "POST",
                f"/bot{self._bot_token}/{method}",
                body=request.read(),
                headers=dict(request.headers),
            )
            provider_response = connection.getresponse()
            body = provider_response.read(_TELEGRAM_RESPONSE_MAX_BYTES + 1)
            if len(body) > _TELEGRAM_RESPONSE_MAX_BYTES:
                raise httpx.TransportError("telegram_transport_response_too_large", request=request)
            return httpx.Response(
                status_code=int(provider_response.status),
                headers=provider_response.getheaders(),
                content=body,
                extensions={
                    "http_version": b"HTTP/1.1",
                    "reason_phrase": str(provider_response.reason or "").encode("ascii", errors="replace"),
                },
            )
        except TimeoutError as exc:
            raise httpx.TimeoutException("telegram_transport_timeout", request=request) from exc
        except (HTTPException, OSError) as exc:
            raise httpx.TransportError("telegram_transport_failed", request=request) from exc
        finally:
            connection.close()


class TelegramNewsPushSender:
    """Send one News-card copy to the exact destination fixed at construction time."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: int,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] | None = None,
        wall_clock_ms: Callable[[], int] | None = None,
        trading_actions_enabled: bool = False,
        onchain_actions_enabled: bool = False,
    ) -> None:
        normalized_token = str(bot_token or "").strip()
        if not _BOT_TOKEN_RE.fullmatch(normalized_token):
            raise ValueError("news_push_telegram_bot_token_invalid")
        if not _is_supported_telegram_target_id(chat_id):
            raise ValueError("news_push_telegram_chat_id_invalid")
        self._chat_id = chat_id
        self._target_sha256 = hmac.new(
            normalized_token.encode(),
            _telegram_target_digest_input(chat_id),
            hashlib.sha256,
        ).hexdigest()
        self._target_validated = False
        self._trading_actions_enabled = bool(trading_actions_enabled)
        self._onchain_actions_enabled = bool(onchain_actions_enabled)
        self._monotonic = monotonic or time.monotonic
        self._wall_clock_ms = wall_clock_ms or (lambda: int(time.time() * 1000))
        selected_transport = transport if transport is not None else _TelegramHTTPSBotTransport(normalized_token)
        self._client = httpx.Client(
            base_url=f"{_TELEGRAM_API_ORIGIN}/",
            timeout=httpx.Timeout(_TELEGRAM_TIMEOUT_SECONDS),
            headers={"Accept-Encoding": "identity", "Content-Type": "application/json"},
            follow_redirects=False,
            transport=selected_transport,
        )

    def prepare(self) -> None:
        """Validate the target before the caller creates a durable sending row."""

        if self._target_validated:
            return
        deadline_at = self._monotonic() + _TELEGRAM_TOTAL_CALL_BUDGET_SECONDS
        self._validate_target(deadline_at=deadline_at)
        self._target_validated = True

    @property
    def target_sha256(self) -> str:
        return self._target_sha256

    def send_card(
        self,
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        if not self._target_validated:
            raise TelegramDeliveryError("news_delivery_telegram_target_not_prepared")
        view = presentation or ReaderDeliveryPresentation()
        pushed_at_ms = int(self._wall_clock_ms())
        text = _telegram_message(
            card,
            trade_targets=view.trade_targets,
            market_movements=view.market_movements,
            news_at_ms=view.news_at_ms,
            pushed_at_ms=pushed_at_ms,
            market_data_pending=view.market_data_state == "pending",
            market_scope=view.market_scope,
            novelty=view.novelty,
            progression_from_headline=view.progression_from_headline,
            progression_review_state=view.progression_review_state,
            progression_review_reason=view.progression_review_reason,
            progression_review_parent_age_minutes=view.progression_review_parent_age_minutes,
            progression_review_parent_url=_telegram_private_message_url(
                self._chat_id,
                view.progression_review_parent_message_id,
            ),
        )
        deadline_at = self._monotonic() + _TELEGRAM_TOTAL_CALL_BUDGET_SECONDS
        try:
            result = self._call_api(
                "sendMessage",
                self._message_payload(text),
                error_prefix="news_delivery_telegram",
                deadline_at=deadline_at,
            )
            message_id = self._validated_message_id(result, error_prefix="news_delivery_telegram")
        except TelegramDeliveryError:
            self._target_validated = False
            raise
        return TelegramDeliveryReceipt(
            provider="telegram",
            message_id=message_id,
            pushed_at_ms=pushed_at_ms,
            target_sha256=self._target_sha256,
        ).canonical()

    def edit_card(
        self,
        receipt: Mapping[str, Any],
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        """Replace one previously sent copy while preserving its original push timestamp."""

        if not self._target_validated:
            raise TelegramDeliveryError("news_delivery_telegram_target_not_prepared")
        try:
            parsed_receipt = TelegramDeliveryReceipt.model_validate(receipt)
        except ValueError:
            raise TelegramDeliveryError("news_delivery_telegram_edit_receipt_invalid") from None
        if parsed_receipt.target_sha256 != self._target_sha256:
            raise TelegramDeliveryError("news_delivery_telegram_edit_receipt_invalid")
        message_id = parsed_receipt.message_id
        pushed_at_ms = parsed_receipt.pushed_at_ms
        view = presentation or ReaderDeliveryPresentation()
        text = _telegram_message(
            card,
            trade_targets=view.trade_targets,
            market_movements=view.market_movements,
            news_at_ms=view.news_at_ms,
            pushed_at_ms=pushed_at_ms,
            market_data_pending=view.market_data_state == "pending",
            market_scope=view.market_scope,
            novelty=view.novelty,
            progression_from_headline=view.progression_from_headline,
            progression_review_state=view.progression_review_state,
            progression_review_reason=view.progression_review_reason,
            progression_review_parent_age_minutes=view.progression_review_parent_age_minutes,
            progression_review_parent_url=_telegram_private_message_url(
                self._chat_id,
                view.progression_review_parent_message_id,
            ),
        )
        deadline_at = self._monotonic() + _TELEGRAM_TOTAL_CALL_BUDGET_SECONDS
        result = self._call_api(
            "editMessageText",
            self._message_payload(text, message_id=message_id),
            error_prefix="news_delivery_telegram_edit",
            deadline_at=deadline_at,
        )
        self._validated_message_id(
            result,
            error_prefix="news_delivery_telegram_edit",
            expected_message_id=message_id,
        )
        return TelegramDeliveryReceipt(
            provider="telegram",
            message_id=message_id,
            pushed_at_ms=pushed_at_ms,
            edited_at_ms=int(self._wall_clock_ms()),
            target_sha256=self._target_sha256,
        ).canonical()

    def delete_card(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Delete exactly one previously receipted copy from the configured destination."""

        if not self._target_validated:
            raise TelegramDeliveryError("news_delivery_telegram_target_not_prepared")
        try:
            parsed_receipt = TelegramDeliveryReceipt.model_validate(receipt)
        except ValueError:
            raise TelegramDeliveryError("news_delivery_telegram_delete_receipt_invalid") from None
        if parsed_receipt.target_sha256 != self._target_sha256 or parsed_receipt.deleted_at_ms is not None:
            raise TelegramDeliveryError("news_delivery_telegram_delete_receipt_invalid")
        deadline_at = self._monotonic() + _TELEGRAM_TOTAL_CALL_BUDGET_SECONDS
        result = self._call_api_result(
            "deleteMessage",
            {"chat_id": self._chat_id, "message_id": parsed_receipt.message_id},
            error_prefix="news_delivery_telegram_delete",
            deadline_at=deadline_at,
        )
        if result is not True:
            raise TelegramDeliveryError("news_delivery_telegram_delete_response_invalid")
        return TelegramDeliveryReceipt(
            provider="telegram",
            message_id=parsed_receipt.message_id,
            pushed_at_ms=parsed_receipt.pushed_at_ms,
            edited_at_ms=parsed_receipt.edited_at_ms,
            deleted_at_ms=int(self._wall_clock_ms()),
            target_sha256=self._target_sha256,
        ).canonical()

    def _message_payload(self, text: str, *, message_id: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if message_id is not None:
            payload["message_id"] = message_id
        if self._trading_actions_enabled or self._onchain_actions_enabled:
            buttons: list[dict[str, str]] = []
            if self._trading_actions_enabled:
                buttons.append({"text": "详细数据", "callback_data": "tf:detail:v1"})
                buttons.append({"text": "合约交易", "callback_data": "tf:trade:v1"})
            if self._onchain_actions_enabled:
                buttons.append({"text": "链上路由", "callback_data": "tf:onchain:v1"})
            payload["reply_markup"] = {
                "inline_keyboard": [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
            }
        return payload

    def _validated_message_id(
        self,
        result: Mapping[str, Any],
        *,
        error_prefix: str,
        expected_message_id: int | None = None,
    ) -> int:
        chat = result.get("chat")
        message_id = result.get("message_id")
        if not isinstance(chat, Mapping) or isinstance(message_id, bool) or not isinstance(message_id, int):
            raise TelegramDeliveryError(f"{error_prefix}_response_invalid")
        response_chat_id = chat.get("id")
        if isinstance(response_chat_id, bool) or not isinstance(response_chat_id, int):
            raise TelegramDeliveryError(f"{error_prefix}_response_invalid")
        if response_chat_id != self._chat_id:
            raise TelegramDeliveryError(f"{error_prefix}_response_chat_mismatch")
        if expected_message_id is not None and message_id != expected_message_id:
            raise TelegramDeliveryError(f"{error_prefix}_response_message_mismatch")
        return message_id

    def _validate_target(self, *, deadline_at: float) -> None:
        chat = self._call_api(
            "getChat",
            {"chat_id": self._chat_id},
            error_prefix="news_delivery_telegram_preflight",
            deadline_at=deadline_at,
        )
        chat_id = chat.get("id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id != self._chat_id:
            raise TelegramDeliveryError("news_delivery_telegram_target_chat_mismatch")
        chat_type = str(chat.get("type") or "")
        private_chat = self._chat_id > 0
        if private_chat:
            if chat.get("type") != "private":
                raise TelegramDeliveryError("news_delivery_telegram_target_not_private_chat")
        elif chat_type not in {"channel", "group", "supergroup"}:
            raise TelegramDeliveryError("news_delivery_telegram_target_type_invalid")

        bot = self._call_api(
            "getMe",
            {},
            error_prefix="news_delivery_telegram_preflight",
            deadline_at=deadline_at,
        )
        bot_id = bot.get("id")
        if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot.get("is_bot") is not True:
            raise TelegramDeliveryError("news_delivery_telegram_bot_identity_invalid")
        if private_chat:
            return
        membership = self._call_api(
            "getChatMember",
            {"chat_id": self._chat_id, "user_id": bot_id},
            error_prefix="news_delivery_telegram_preflight",
            deadline_at=deadline_at,
        )
        member_user = membership.get("user")
        if (
            not isinstance(member_user, Mapping)
            or member_user.get("id") != bot_id
            or member_user.get("is_bot") is not True
        ):
            raise TelegramDeliveryError("news_delivery_telegram_bot_identity_invalid")
        status = str(membership.get("status") or "")
        if chat_type == "channel":
            if status not in {"administrator", "creator"}:
                raise TelegramDeliveryError("news_delivery_telegram_target_post_permission_missing")
            if status == "administrator" and membership.get("can_post_messages") is not True:
                raise TelegramDeliveryError("news_delivery_telegram_target_post_permission_missing")
        elif status not in {"member", "administrator", "creator"}:
            raise TelegramDeliveryError("news_delivery_telegram_target_post_permission_missing")

    def _call_api(
        self,
        method: str,
        payload: Mapping[str, Any],
        *,
        error_prefix: str,
        deadline_at: float,
    ) -> Mapping[str, Any]:
        result = self._call_api_result(
            method,
            payload,
            error_prefix=error_prefix,
            deadline_at=deadline_at,
        )
        if not isinstance(result, Mapping):
            raise TelegramDeliveryError(f"{error_prefix}_response_invalid")
        return result

    def _call_api_result(
        self,
        method: str,
        payload: Mapping[str, Any],
        *,
        error_prefix: str,
        deadline_at: float,
    ) -> Any:
        return _telegram_call_result(
            client=self._client,
            monotonic=self._monotonic,
            method=method,
            payload=payload,
            error_prefix=error_prefix,
            deadline_at=deadline_at,
        )

    def close(self) -> None:
        self._client.close()


class TelegramNewsFanoutSender:
    """Expose one delivery interface while preserving one receipt per exact Telegram target."""

    def __init__(self, senders: Sequence[TelegramNewsPushSender]) -> None:
        self._senders = tuple(senders)
        if not self._senders or len(self._senders) > 32:
            raise ValueError("news_push_telegram_targets_invalid")

    def prepare(self) -> None:
        with ThreadPoolExecutor(max_workers=len(self._senders), thread_name_prefix="telegram-preflight") as pool:
            futures = [pool.submit(sender.prepare) for sender in self._senders]
            for future in as_completed(futures):
                future.result()

    def send_card(
        self,
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        receipts: list[TelegramDeliveryReceipt | None] = [None] * len(self._senders)
        failure: Exception | None = None
        with ThreadPoolExecutor(max_workers=len(self._senders), thread_name_prefix="telegram-send") as pool:
            futures = {
                pool.submit(sender.send_card, card, presentation=presentation): index
                for index, sender in enumerate(self._senders)
            }
            for future in as_completed(futures):
                try:
                    receipts[futures[future]] = TelegramDeliveryReceipt.model_validate(future.result())
                except Exception as exc:
                    failure = exc
        if failure is not None:
            cleanup_failed = False
            for sender, receipt in zip(self._senders, receipts, strict=True):
                if receipt is None:
                    continue
                try:
                    sender.delete_card(receipt.canonical())
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                raise TelegramDeliveryError("news_delivery_telegram_fanout_ambiguous") from failure
            raise failure
        completed = tuple(receipt for receipt in receipts if receipt is not None)
        return _telegram_fanout_receipt(completed).canonical()

    def edit_card(
        self,
        receipt: Mapping[str, Any],
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        parsed = _telegram_fanout_receipts(receipt, expected=len(self._senders))
        if any(sender.target_sha256 != item.target_sha256 for sender, item in zip(self._senders, parsed, strict=True)):
            raise TelegramDeliveryError("news_delivery_telegram_fanout_target_mismatch")
        updated: list[TelegramDeliveryReceipt | None] = [None] * len(self._senders)
        with ThreadPoolExecutor(max_workers=len(self._senders), thread_name_prefix="telegram-edit") as pool:
            futures = {}
            for index, (sender, target_receipt) in enumerate(zip(self._senders, parsed, strict=True)):
                target_presentation = presentation
                if index > 0 and presentation is not None:
                    target_presentation = replace(presentation, progression_review_parent_message_id=None)
                futures[
                    pool.submit(
                        sender.edit_card,
                        target_receipt.canonical(),
                        card,
                        presentation=target_presentation,
                    )
                ] = index
            for future in as_completed(futures):
                updated[futures[future]] = TelegramDeliveryReceipt.model_validate(future.result())
        return _telegram_fanout_receipt(tuple(item for item in updated if item is not None)).canonical()

    def delete_card(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        parsed = _telegram_fanout_receipts(receipt, expected=len(self._senders))
        if any(sender.target_sha256 != item.target_sha256 for sender, item in zip(self._senders, parsed, strict=True)):
            raise TelegramDeliveryError("news_delivery_telegram_fanout_target_mismatch")
        deleted: list[TelegramDeliveryReceipt | None] = [None] * len(self._senders)
        with ThreadPoolExecutor(max_workers=len(self._senders), thread_name_prefix="telegram-delete") as pool:
            futures = {
                pool.submit(sender.delete_card, target_receipt.canonical()): index
                for index, (sender, target_receipt) in enumerate(zip(self._senders, parsed, strict=True))
            }
            for future in as_completed(futures):
                deleted[futures[future]] = TelegramDeliveryReceipt.model_validate(future.result())
        return _telegram_fanout_receipt(tuple(item for item in deleted if item is not None)).canonical()

    def close(self) -> None:
        for sender in self._senders:
            sender.close()


def _telegram_fanout_receipt(receipts: Sequence[TelegramDeliveryReceipt]) -> TelegramDeliveryReceipt:
    if not receipts:
        raise TelegramDeliveryError("news_delivery_telegram_fanout_receipt_invalid")
    primary = receipts[0]
    return TelegramDeliveryReceipt(
        provider="telegram",
        message_id=primary.message_id,
        pushed_at_ms=primary.pushed_at_ms,
        target_sha256=primary.target_sha256,
        edited_at_ms=primary.edited_at_ms,
        deleted_at_ms=primary.deleted_at_ms,
        copies=tuple(
            TelegramDeliveryCopyReceipt(
                message_id=item.message_id,
                pushed_at_ms=item.pushed_at_ms,
                target_sha256=item.target_sha256,
                edited_at_ms=item.edited_at_ms,
                deleted_at_ms=item.deleted_at_ms,
            )
            for item in receipts[1:]
        ),
    )


def _telegram_fanout_receipts(receipt: Mapping[str, Any], *, expected: int) -> tuple[TelegramDeliveryReceipt, ...]:
    try:
        batch = TelegramDeliveryReceipt.model_validate(receipt)
    except ValueError:
        raise TelegramDeliveryError("news_delivery_telegram_fanout_receipt_invalid") from None
    rows = (
        TelegramDeliveryReceipt(
            provider="telegram",
            message_id=batch.message_id,
            pushed_at_ms=batch.pushed_at_ms,
            target_sha256=batch.target_sha256,
            edited_at_ms=batch.edited_at_ms,
            deleted_at_ms=batch.deleted_at_ms,
        ),
        *(
            TelegramDeliveryReceipt(
                provider="telegram",
                message_id=copy.message_id,
                pushed_at_ms=copy.pushed_at_ms,
                target_sha256=copy.target_sha256,
                edited_at_ms=copy.edited_at_ms,
                deleted_at_ms=copy.deleted_at_ms,
            )
            for copy in batch.copies
        ),
    )
    if len(rows) != expected:
        raise TelegramDeliveryError("news_delivery_telegram_fanout_target_mismatch")
    return rows


class TelegramTradingClient:
    """One Bot API cursor serving independently configured Telegram private users."""

    def __init__(
        self,
        *,
        bot_token: str,
        authorized_user_ids: Sequence[int],
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        normalized_token = str(bot_token or "").strip()
        if not _BOT_TOKEN_RE.fullmatch(normalized_token):
            raise ValueError("manual_trading_telegram_bot_token_invalid")
        users = tuple(authorized_user_ids)
        if (
            not users
            or len(users) > 16
            or len(set(users)) != len(users)
            or any(isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0 for user_id in users)
        ):
            raise ValueError("manual_trading_telegram_authorized_users_invalid")
        self._authorized_user_ids = frozenset(users)
        self._target_digest_key = normalized_token.encode()
        self._monotonic = monotonic or time.monotonic
        selected_transport = transport if transport is not None else _TelegramHTTPSBotTransport(normalized_token)
        self._client = httpx.Client(
            base_url=f"{_TELEGRAM_API_ORIGIN}/",
            timeout=httpx.Timeout(_TELEGRAM_TIMEOUT_SECONDS),
            headers={"Accept-Encoding": "identity", "Content-Type": "application/json"},
            follow_redirects=False,
            transport=selected_transport,
        )

    def target_sha256_for(self, chat_id: int) -> str:
        if chat_id not in self._authorized_user_ids:
            raise ValueError("manual_trading_telegram_chat_unauthorized")
        return hmac.new(
            self._target_digest_key,
            _telegram_target_digest_input(chat_id),
            hashlib.sha256,
        ).hexdigest()

    def poll_updates(self, *, next_update_id: int) -> tuple[TelegramTradingUpdate, ...]:
        if isinstance(next_update_id, bool) or not isinstance(next_update_id, int) or next_update_id < 0:
            raise ValueError("manual_trading_telegram_update_cursor_invalid")
        result = self._call_result(
            "getUpdates",
            {
                "offset": next_update_id,
                "limit": 20,
                "timeout": 0,
                "allowed_updates": ["callback_query", "message"],
            },
            error_prefix="manual_trading_telegram_poll",
        )
        if not isinstance(result, list) or len(result) > 20:
            raise TelegramDeliveryError("manual_trading_telegram_poll_response_invalid")
        updates = tuple(self._parse_update(item) for item in result)
        update_ids = [update.update_id for update in updates]
        if update_ids != sorted(set(update_ids)) or any(update_id < next_update_id for update_id in update_ids):
            raise TelegramDeliveryError("manual_trading_telegram_poll_order_invalid")
        return updates

    def answer_callback(self, callback_query_id: str, *, text: str, show_alert: bool = False) -> None:
        callback_id = str(callback_query_id or "").strip()
        message = str(text or "").strip()
        if not callback_id or len(callback_id) > 128 or not message or len(message) > 200:
            raise ValueError("manual_trading_telegram_callback_answer_invalid")
        try:
            result = self._call_result(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": message, "show_alert": bool(show_alert)},
                error_prefix="manual_trading_telegram_callback_answer",
            )
        except TelegramDeliveryError as exc:
            if exc.code == "manual_trading_telegram_callback_answer_expired":
                return
            raise
        if result is not True:
            raise TelegramDeliveryError("manual_trading_telegram_callback_answer_response_invalid")

    def set_commands(self, *, chat_id: int, commands: Sequence[tuple[str, str]]) -> None:
        if chat_id not in self._authorized_user_ids:
            raise ValueError("manual_trading_telegram_chat_unauthorized")
        normalized: list[dict[str, str]] = []
        for command, description in commands:
            name = str(command or "").strip()
            label = str(description or "").strip()
            if _BOT_COMMAND_RE.fullmatch(name) is None or not 1 <= len(label) <= 256:
                raise ValueError("manual_trading_telegram_commands_invalid")
            normalized.append({"command": name, "description": label})
        names = [item["command"] for item in normalized]
        if not 1 <= len(normalized) <= 100 or len(set(names)) != len(names):
            raise ValueError("manual_trading_telegram_commands_invalid")
        result = self._call_result(
            "setMyCommands",
            {
                "commands": normalized,
                "scope": {"type": "chat", "chat_id": chat_id},
            },
            error_prefix="manual_trading_telegram_commands",
        )
        if result is not True:
            raise TelegramDeliveryError("manual_trading_telegram_commands_response_invalid")

    def send_development_test_card(
        self,
        *,
        chat_id: int,
        card: Mapping[str, Any],
        kind: str,
        now_ms: int,
    ) -> dict[str, Any]:
        if chat_id not in self._authorized_user_ids or kind not in {"futures", "onchain"}:
            raise ValueError("telegram_test_news_profile_invalid")
        text = _telegram_message(
            card,
            news_at_ms=now_ms,
            pushed_at_ms=now_ms,
        )
        buttons = (
            [
                {"text": "详细数据", "callback_data": "tf:detail:v1"},
                {"text": "合约交易", "callback_data": "tf:trade:v1"},
            ]
            if kind == "futures"
            else [{"text": "链上路由", "callback_data": "tf:onchain:v1"}]
        )
        result = self._call_result(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
                "reply_markup": {"inline_keyboard": [buttons]},
            },
            error_prefix="telegram_test_news_send",
        )
        message_id = self._interaction_message_id(
            result,
            expected_chat_id=chat_id,
            error_prefix="telegram_test_news_send",
        )
        return TelegramDeliveryReceipt(
            provider="telegram",
            message_id=message_id,
            pushed_at_ms=now_ms,
            target_sha256=self.target_sha256_for(chat_id),
        ).canonical()

    def delete_interaction(self, *, chat_id: int, message_id: int) -> None:
        if chat_id not in self._authorized_user_ids:
            raise ValueError("manual_trading_telegram_chat_unauthorized")
        result = self._call_result(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            error_prefix="manual_trading_telegram_delete",
        )
        if result is not True:
            raise TelegramDeliveryError("manual_trading_telegram_delete_response_invalid")

    def send_interaction_reply(
        self,
        *,
        chat_id: int,
        source_message_id: int,
        text: str,
        keyboard: Sequence[tuple[str, str]],
    ) -> int:
        if isinstance(source_message_id, bool) or not isinstance(source_message_id, int) or source_message_id <= 0:
            raise ValueError("manual_trading_telegram_source_message_invalid")
        payload = self._interaction_payload(chat_id=chat_id, text=text, keyboard=keyboard, allow_empty=False)
        payload["reply_parameters"] = {
            "message_id": source_message_id,
            "allow_sending_without_reply": False,
        }
        result = self._call_result(
            "sendMessage",
            payload,
            error_prefix="manual_trading_telegram_reply",
        )
        return self._interaction_message_id(
            result,
            expected_chat_id=chat_id,
            error_prefix="manual_trading_telegram_reply",
        )

    def send_plain_reply(self, *, chat_id: int, source_message_id: int, text: str) -> int:
        if isinstance(source_message_id, bool) or not isinstance(source_message_id, int) or source_message_id <= 0:
            raise ValueError("manual_trading_telegram_source_message_invalid")
        payload = self._interaction_payload(chat_id=chat_id, text=text, keyboard=(), allow_empty=True)
        payload.pop("reply_markup")
        payload["reply_parameters"] = {
            "message_id": source_message_id,
            "allow_sending_without_reply": False,
        }
        result = self._call_result(
            "sendMessage",
            payload,
            error_prefix="manual_trading_telegram_reply",
        )
        return self._interaction_message_id(
            result,
            expected_chat_id=chat_id,
            error_prefix="manual_trading_telegram_reply",
        )

    def edit_interaction(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: Sequence[tuple[str, str]],
    ) -> None:
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise ValueError("manual_trading_telegram_interaction_message_invalid")
        payload = self._interaction_payload(chat_id=chat_id, text=text, keyboard=keyboard, allow_empty=True)
        payload["message_id"] = message_id
        try:
            result = self._call_result(
                "editMessageText",
                payload,
                error_prefix="manual_trading_telegram_edit",
            )
        except TelegramDeliveryError as exc:
            if exc.code == "manual_trading_telegram_edit_not_modified":
                return
            raise
        observed = self._interaction_message_id(
            result,
            expected_chat_id=chat_id,
            error_prefix="manual_trading_telegram_edit",
        )
        if observed != message_id:
            raise TelegramDeliveryError("manual_trading_telegram_edit_message_mismatch")

    def _parse_update(self, value: object) -> TelegramTradingUpdate:
        if not isinstance(value, Mapping):
            raise TelegramDeliveryError("manual_trading_telegram_update_invalid")
        update_id = value.get("update_id")
        query = value.get("callback_query")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            raise TelegramDeliveryError("manual_trading_telegram_update_invalid")
        if isinstance(query, Mapping):
            return self._parse_callback_update(update_id, query)
        message = value.get("message")
        if isinstance(message, Mapping):
            return self._parse_message_update(update_id, message)
        raise TelegramDeliveryError("manual_trading_telegram_update_invalid")

    def _parse_callback_update(self, update_id: int, query: Mapping[str, Any]) -> TelegramTradingUpdate:
        callback_id = query.get("id")
        actor = query.get("from")
        message = query.get("message")
        data = query.get("data")
        if (
            not isinstance(callback_id, str)
            or not callback_id
            or len(callback_id) > 128
            or not isinstance(actor, Mapping)
            or not isinstance(message, Mapping)
            or not isinstance(data, str)
            or not 1 <= len(data.encode("utf-8")) <= 64
        ):
            raise TelegramDeliveryError("manual_trading_telegram_update_invalid")
        actor_id = actor.get("id")
        message_id = message.get("message_id")
        chat = message.get("chat")
        if (
            isinstance(actor_id, bool)
            or not isinstance(actor_id, int)
            or actor_id <= 0
            or actor.get("is_bot") is not False
            or isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
            or not isinstance(chat, Mapping)
        ):
            raise TelegramDeliveryError("manual_trading_telegram_update_invalid")
        update_chat_id = chat.get("id")
        chat_type = str(chat.get("type") or "")
        if isinstance(update_chat_id, bool) or not isinstance(update_chat_id, int) or not chat_type:
            raise TelegramDeliveryError("manual_trading_telegram_update_invalid")
        return TelegramTradingUpdate(
            update_id=update_id,
            callback_query_id=callback_id,
            actor_user_id=actor_id,
            chat_id=update_chat_id,
            chat_type=chat_type,
            message_id=message_id,
            data=data,
            authorized=(
                chat_type == "private" and update_chat_id == actor_id and actor_id in self._authorized_user_ids
            ),
        )

    def _parse_message_update(self, update_id: int, message: Mapping[str, Any]) -> TelegramTradingUpdate:
        actor = message.get("from")
        chat = message.get("chat")
        message_id = message.get("message_id")
        text = message.get("text")
        if (
            not isinstance(actor, Mapping)
            or actor.get("is_bot") is not False
            or not isinstance(chat, Mapping)
            or isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
            or not isinstance(text, str)
            or len(text) > 256
        ):
            raise TelegramDeliveryError("manual_trading_telegram_update_invalid")
        actor_id = actor.get("id")
        chat_id = chat.get("id")
        chat_type = str(chat.get("type") or "")
        if (
            isinstance(actor_id, bool)
            or not isinstance(actor_id, int)
            or actor_id <= 0
            or isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or not chat_type
        ):
            raise TelegramDeliveryError("manual_trading_telegram_update_invalid")
        command = text.strip().split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
        data = {
            "/start": "tf:help:v1",
            "/help": "tf:help:v1",
            "/test_futures": "tf:test:futures",
            "/test_onchain": "tf:test:onchain",
            "/positions": "tf:cmd:positions",
            "/history": "tf:cmd:history",
            "/trades": "tf:cmd:trades",
        }.get(command, "tf:message:ignored")
        authorized = chat_type == "private" and chat_id == actor_id and actor_id in self._authorized_user_ids
        return TelegramTradingUpdate(
            update_id=update_id,
            callback_query_id=f"message:{chat_id}:{message_id}",
            actor_user_id=actor_id,
            chat_id=chat_id,
            chat_type=chat_type,
            message_id=message_id,
            data=data,
            authorized=authorized,
            update_kind="message",
        )

    def _interaction_payload(
        self,
        *,
        chat_id: int,
        text: str,
        keyboard: Sequence[tuple[str, str]],
        allow_empty: bool,
    ) -> dict[str, Any]:
        message = str(text or "").strip()
        if not message or len(message) > _TELEGRAM_TEXT_MAX:
            raise ValueError("manual_trading_telegram_interaction_text_invalid")
        buttons: list[dict[str, str]] = []
        for label, callback_data in keyboard:
            normalized_label = str(label or "").strip()
            normalized_data = str(callback_data or "").strip()
            if (
                not normalized_label
                or len(normalized_label) > 64
                or not 1 <= len(normalized_data.encode("utf-8")) <= 64
            ):
                raise ValueError("manual_trading_telegram_keyboard_invalid")
            buttons.append({"text": normalized_label, "callback_data": normalized_data})
        if (not buttons and not allow_empty) or len(buttons) > 8:
            raise ValueError("manual_trading_telegram_keyboard_invalid")
        rows: list[list[dict[str, str]]] = []
        pending: list[dict[str, str]] = []
        for button in buttons:
            if button["callback_data"].startswith("tf:o:c:"):
                if pending:
                    rows.append(pending)
                    pending = []
                rows.append([button])
                continue
            pending.append(button)
            if len(pending) == 2:
                rows.append(pending)
                pending = []
        if pending:
            rows.append(pending)
        if chat_id not in self._authorized_user_ids:
            raise ValueError("manual_trading_telegram_chat_unauthorized")
        return {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": rows},
        }

    def _interaction_message_id(self, value: object, *, expected_chat_id: int, error_prefix: str) -> int:
        if not isinstance(value, Mapping):
            raise TelegramDeliveryError(f"{error_prefix}_response_invalid")
        message_id = value.get("message_id")
        chat = value.get("chat")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
            or not isinstance(chat, Mapping)
            or chat.get("id") != expected_chat_id
        ):
            raise TelegramDeliveryError(f"{error_prefix}_response_invalid")
        return message_id

    def _call_result(self, method: str, payload: Mapping[str, Any], *, error_prefix: str) -> Any:
        return _telegram_call_result(
            client=self._client,
            monotonic=self._monotonic,
            method=method,
            payload=payload,
            error_prefix=error_prefix,
            deadline_at=self._monotonic() + _TELEGRAM_TOTAL_CALL_BUDGET_SECONDS,
        )

    def close(self) -> None:
        self._client.close()


def _telegram_message(
    card: Mapping[str, Any],
    *,
    trade_targets: Sequence[ReaderTradeTarget] = (),
    market_movements: Sequence[ReaderMarketMovement] = (),
    news_at_ms: int | None = None,
    pushed_at_ms: int | None = None,
    market_data_pending: bool = False,
    market_scope: str | None = None,
    novelty: str | None = None,
    progression_from_headline: str | None = None,
    progression_review_state: str | None = None,
    progression_review_reason: str | None = None,
    progression_review_parent_age_minutes: int | None = None,
    progression_review_parent_url: str | None = None,
) -> str:
    title = _nested_text(card.get("header"), "title") or "Tracefold 新闻事件"
    header = card.get("header")
    template = str(header.get("template") or "") if isinstance(header, Mapping) else ""
    icon = _HEADER_ICON.get(template, "⚪")
    if title.startswith("⚡ "):
        icon = "⚡"
        title = title.removeprefix("⚡ ").strip()

    content_lines: list[str] = []
    source_url = ""
    elements = card.get("elements")
    if isinstance(elements, Sequence) and not isinstance(elements, str | bytes):
        for element in elements:
            if not isinstance(element, Mapping):
                continue
            tag = element.get("tag")
            if tag == "markdown":
                content = str(element.get("content") or "").strip()
                if content:
                    content_lines.extend(line.strip() for line in content.splitlines() if line.strip())
            elif tag == "action" and not source_url:
                source_url = _source_url(element.get("actions"))

    market_line = next((line for line in reversed(content_lines) if line.startswith("行情 ")), "")
    if market_line:
        content_lines.remove(market_line)
    facts_line = content_lines.pop() if content_lines else ""
    explanation = "\n".join(content_lines)
    facts = telegram_card_facts(facts_line)
    ticker_links = _trade_target_links(trade_targets)

    sections = [f"{icon} <b>{_escape_html(_clip(title, 240))}</b>"]
    displayed_novelty = novelty or facts.novelty
    displayed_review_state = progression_review_state
    displayed_parent_headline = progression_from_headline
    displayed_parent_age = progression_review_parent_age_minutes
    displayed_parent_url = progression_review_parent_url
    if displayed_review_state in {"rejected", "unavailable"}:
        displayed_novelty = "new_fact"
        displayed_review_state = None
        displayed_parent_headline = None
        displayed_parent_age = None
        displayed_parent_url = None
    elif displayed_review_state == "confirmed" and not displayed_parent_url:
        displayed_novelty = "new_fact"
        displayed_review_state = None
        displayed_parent_headline = None
        displayed_parent_age = None

    novelty_line = _telegram_novelty_html(
        displayed_novelty,
        progression_from_headline=(None if displayed_review_state else displayed_parent_headline),
    )
    progression_review_line = _telegram_progression_review_html(
        displayed_review_state,
        parent_headline=displayed_parent_headline,
        parent_age_minutes=displayed_parent_age,
        parent_url=displayed_parent_url,
    )
    if novelty_line and progression_review_line:
        sections.append(f"{novelty_line}\n{progression_review_line}")
    elif novelty_line:
        sections.append(novelty_line)
    elif progression_review_line:
        sections.append(progression_review_line)
    if explanation:
        sections.append(_escape_html(_clip(explanation, 1800)))

    metadata_groups: list[str] = []
    if facts.assets:
        metadata_groups.extend(
            _telegram_asset_blocks(
                facts.assets,
                market_line=market_line,
                ticker_links=ticker_links,
                market_movements=market_movements,
                market_data_pending=market_data_pending,
            )
        )
    elif market_scope:
        scope_line = _telegram_scope_html(market_scope)
        if scope_line:
            metadata_groups.append(scope_line)
    if facts.direction or facts.magnitude:
        direction = _escape_html(facts.direction or "不明确")
        magnitude = _escape_html(_MAGNITUDE_LABELS.get(facts.magnitude, facts.magnitude))
        metadata_groups.append(f"🧭 <b>方向</b>  {magnitude}{direction}")
    footer: list[str] = []
    timing = _telegram_timing_html(news_at_ms=news_at_ms, pushed_at_ms=pushed_at_ms)
    if timing:
        footer.append(timing)
    if facts.origin or source_url:
        source = _telegram_source_html(facts.origin, source_url)
        count = f" · {facts.report_count} 条报道" if facts.report_count is not None else ""
        footer.append(f"🔗 <b>来源</b>  {source}{count}")
    if footer:
        metadata_groups.append("\n".join(footer))
    if metadata_groups:
        sections.append("\n\n".join(metadata_groups))

    message = "\n\n".join(sections).strip()
    if len(_plain_html_text(message)) > _TELEGRAM_TEXT_MAX:
        raise TelegramDeliveryError("news_delivery_telegram_message_too_long")
    return message


def _telegram_novelty_html(value: str, *, progression_from_headline: str | None) -> str:
    if value in {"new_fact", "新事实"}:
        return "🆕 <b>新事实</b>"
    if value in {"progression", "新进展"}:
        previous = _clip(str(progression_from_headline or "").strip(), 72)
        suffix = f" · 接续「{_escape_html(previous)}」" if previous else ""
        return f"🔄 <b>新进展</b>{suffix}"
    if value in {"restatement", "复述"}:
        return "♻️ <b>复述</b>"
    return ""


def _telegram_progression_review_html(
    value: str | None,
    *,
    parent_headline: str | None,
    parent_age_minutes: int | None,
    parent_url: str | None,
) -> str:
    if value == "pending":
        return "<blockquote>⏳ <b>关联确认中</b></blockquote>"
    if value == "confirmed":
        age = _telegram_parent_age(parent_age_minutes)
        age_suffix = f" · {age} 前" if age else ""
        headline = _escape_html(_clip(str(parent_headline or "").strip(), 72)) or "上一条消息"
        parent = f'<a href="{parent_url}">此前：{headline}</a>' if parent_url else f"此前：{headline}"
        return f"<blockquote>✅ <b>已确认关联</b>\n↳ {parent}{age_suffix}</blockquote>"
    return ""


def _telegram_parent_age(value: int | None) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return ""
    if value == 0:
        return "&lt;1min"
    hours, minutes = divmod(value, 60)
    if hours and minutes:
        return f"{hours}h {minutes}mins"
    if hours:
        return f"{hours}h"
    return f"{minutes}mins"


def _telegram_private_message_url(chat_id: int, message_id: int | None) -> str | None:
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        return None
    channel_id = str(chat_id)
    if _PRIVATE_CHANNEL_ID_RE.fullmatch(channel_id) is None:
        return None
    return f"https://t.me/c/{channel_id.removeprefix('-100')}/{message_id}"


def _telegram_scope_html(value: str) -> str:
    label = {
        "macro": "宏观市场 · 暂无直接标的",
        "sector": "行业板块 · 暂无直接标的",
        "single_name": "暂未验证到具体标的",
    }.get(str(value or ""), "")
    return f"🌐 <b>影响范围</b>  {label}" if label else ""


def _telegram_asset_blocks(
    assets: Sequence[str],
    *,
    market_line: str,
    ticker_links: Mapping[str, str],
    market_movements: Sequence[ReaderMarketMovement],
    market_data_pending: bool,
) -> list[str]:
    movements = {
        movement.ticker: movement for movement in market_movements if isinstance(movement, ReaderMarketMovement)
    }
    blocks: list[str] = []
    for asset in assets:
        ticker = _telegram_ticker_html(asset, ticker_links)
        movement = movements.get(asset)
        if market_data_pending:
            after_news = "计算中"
            one_hour = "计算中"
            day_change = "计算中"
        elif movement is not None:
            after_news = _format_bps(movement.after_news_bps) if movement.after_news_bps is not None else "暂无"
            one_hour = (
                _format_bps(movement.return_1h_bps)
                if movement.return_1h_bps is not None
                else {
                    "not_due": "待到期",
                    "pending": "计算中",
                    "available": "暂无",
                    "unavailable": "暂无",
                }[movement.one_hour_state]
            )
            day_change = _format_bps(movement.change_24h_bps) if movement.change_24h_bps is not None else "暂无"
        else:
            after_news = "暂无"
            one_hour = "暂无"
            change_match = re.search(
                rf"(?<![A-Z0-9.-]){re.escape(asset)}\s+\$[0-9,.]+\s+24h\s+(?P<pct>[+-]?[0-9]+(?:\.[0-9]+)?%)(?![A-Z0-9.-])",
                market_line,
            )
            day_change = _escape_html(change_match.group("pct")) if change_match is not None else "暂无"
        blocks.append(f"🎯 <b>标的</b>  {ticker}\n新闻后 {after_news}\n1h {one_hour}，\n24h {day_change}")
    return blocks


def _format_bps(value: int) -> str:
    percentage = Decimal(value) / Decimal(100)
    return f"{'+' if value > 0 else ''}{percentage:.2f}%"


def _telegram_timing_html(
    *,
    news_at_ms: int | None,
    pushed_at_ms: int | None,
) -> str:
    if not _positive_timestamp(pushed_at_ms):
        return ""
    news_text = _format_reader_time(int(news_at_ms)) if _positive_timestamp(news_at_ms) else ""
    pushed_text = _format_reader_time(int(pushed_at_ms))
    if not pushed_text:
        return ""
    return f"新闻时间  {news_text or '暂无'}\n推送时间  {pushed_text}"


def _positive_timestamp(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _format_reader_time(value_ms: int) -> str:
    try:
        return datetime.fromtimestamp(value_ms / 1000, tz=_READER_TIMEZONE).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _telegram_source_html(origin: str, source_url: str) -> str:
    label = _clip(_normalized_source_label(origin, source_url), 160)
    if not _safe_https_url(source_url):
        return _escape_html(label)
    return f'<a href="{html.escape(source_url, quote=True)}">{_escape_html(label)}</a>'


def _normalized_source_label(origin: str, source_url: str) -> str:
    normalized = str(origin or "").strip()
    lowered = normalized.casefold()
    try:
        parsed = urlsplit(source_url)
        host = str(parsed.hostname or "").lower().removeprefix("www.")
    except ValueError:
        parsed, host = None, ""
    if host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com"):
        path_handle = parsed.path.strip("/").split("/", maxsplit=1)[0].removeprefix("@") if parsed else ""
        handle = path_handle if path_handle.casefold() not in {"", "home", "i", "search"} else ""
        if not handle and lowered not in {"", "x", "twitter", "opennews"}:
            handle = normalized.removeprefix("@").strip()
        return f"{handle} 的推特" if handle else "推特"
    aliases = {
        "jin10": "金十",
        "金十数据": "金十",
        "bloomberg": "彭博社",
        "彭博": "彭博社",
        "reuters": "路透社",
        "路透": "路透社",
    }
    if host in _NEWSLIQUID_RELAY_HOSTS:
        # This is a provider-owned relay, not a publisher. When OpenNews supplies a distinct source it is
        # provider-attested and can be named; otherwise say what is actually known instead of presenting the
        # transport domain as a newsroom or guessing a publisher from an opaque article id.
        if normalized and lowered not in {host, "newsliquid", "opennews"}:
            return aliases.get(lowered, normalized)
        # NewsLiquid's public viewer identifies its `nL…` wire ids as Reuters News. Keep this exact and
        # fail closed: another relay id shape remains unidentified instead of inheriting the Reuters brand.
        if parsed is not None and _NEWSLIQUID_REUTERS_PATH_RE.fullmatch(parsed.path):
            return "路透社"
        return "原始媒体未识别（NewsLiquid 中转）"
    if host in {"jin10.com", "jin10.com.cn"} or host.endswith((".jin10.com", ".jin10.com.cn")):
        return "金十"
    if host == "bloomberg.com" or host.endswith(".bloomberg.com"):
        return "彭博社"
    if host == "reuters.com" or host.endswith(".reuters.com"):
        return "路透社"
    if host == "coindesk.com" or host.endswith(".coindesk.com"):
        return "CoinDesk"
    if host == "ft.com" or host.endswith(".ft.com"):
        return "金融时报"
    if host == "theblock.co" or host.endswith(".theblock.co"):
        return "The Block"
    # A URL and a publisher label are one claim. Unknown destinations therefore show their hostname instead
    # of inheriting a trusted brand from provider text (for example `Reuters` + `reuters.com.evil.test`).
    if host:
        return host
    return aliases.get(lowered, normalized or "未知来源")


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _escape_html(value: str) -> str:
    return html.escape(value, quote=False)


def _telegram_ticker_html(value: str, ticker_links: Mapping[str, str]) -> str:
    """Link exact ticker tokens from typed targets; every other character stays escaped text."""

    if not ticker_links:
        return _escape_html(value)
    ticker_pattern = "|".join(re.escape(ticker) for ticker in sorted(ticker_links, key=len, reverse=True))
    pattern = re.compile(rf"(?<![A-Z0-9.-])(?P<ticker>{ticker_pattern})(?![A-Z0-9.-])")
    pieces: list[str] = []
    cursor = 0
    for match in pattern.finditer(value):
        pieces.append(_escape_html(value[cursor : match.start()]))
        ticker = match.group("ticker")
        url = ticker_links[ticker]
        pieces.append(f'<a href="{html.escape(url, quote=True)}">{_escape_html(ticker)}</a>')
        cursor = match.end()
    pieces.append(_escape_html(value[cursor:]))
    return "".join(pieces)


def _plain_html_text(value: str) -> str:
    return html.unescape(_TELEGRAM_HTML_TAG_RE.sub("", value))


def _nested_text(value: object, key: str) -> str:
    if not isinstance(value, Mapping):
        return ""
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        return ""
    return str(nested.get("content") or "").strip()


def _source_url(value: object) -> str:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ""
    for action in value:
        if not isinstance(action, Mapping) or action.get("tag") != "button":
            continue
        url = str(action.get("url") or "").strip()
        if not _safe_https_url(url):
            continue
        return url
    return ""


def _safe_https_url(value: str) -> bool:
    if len(value) > _SOURCE_URL_MAX_LENGTH:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _safe_binance_trade_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "www.binance.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and (
            _BINANCE_FUTURES_PATH_RE.fullmatch(parsed.path) is not None
            or _BINANCE_SPOT_PATH_RE.fullmatch(parsed.path) is not None
        )
    )


def _trade_target_links(trade_targets: Sequence[ReaderTradeTarget]) -> dict[str, str]:
    links: dict[str, str] = {}
    for target in trade_targets:
        if not isinstance(target, ReaderTradeTarget):
            continue
        ticker = target.ticker
        base_symbol = target.base_symbol
        quote_asset = target.quote_asset
        venue_symbol = target.venue_symbol
        if _LINKABLE_TICKER_RE.fullmatch(ticker) is None or _LINKABLE_TICKER_RE.fullmatch(base_symbol) is None:
            continue
        if target.venue.startswith("binance.") and (
            ticker != base_symbol or venue_symbol != f"{base_symbol}{quote_asset}"
        ):
            continue
        if target.venue == "binance.perp":
            url = f"https://www.binance.com/en/futures/{venue_symbol}"
        elif target.venue == "binance.spot":
            url = f"https://www.binance.com/en/trade/{base_symbol}_{quote_asset}"
        elif target.venue in {"hl.perp", "hl.spot", "hl.builder"}:
            url = f"https://app.hyperliquid.xyz/trade/{quote(venue_symbol, safe=':@')}"
        elif target.venue == "okx.perp":
            url = f"https://www.okx.com/trade-swap/{quote(venue_symbol.lower(), safe='-')}"
        elif target.venue == "okx.spot":
            url = f"https://www.okx.com/trade-spot/{quote(venue_symbol.lower(), safe='-')}"
        elif target.venue in {"lighter.perp", "lighter.spot"}:
            url = f"https://app.lighter.xyz/trade/{quote(base_symbol, safe='')}"
        elif target.venue == "bitget.perp":
            url = f"https://www.bitget.com/futures/usdt/{quote(venue_symbol.lower(), safe='')}"
        elif target.venue == "bitget.spot":
            url = f"https://www.bitget.com/spot/{quote(venue_symbol.lower(), safe='')}"
        else:
            continue
        if _safe_trade_url(url):
            links.setdefault(ticker, url)
    return links


def _safe_trade_url(value: str) -> bool:
    if _safe_binance_trade_url(value):
        return True
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    allowed_paths = {
        "app.hyperliquid.xyz": ("/trade/",),
        "www.okx.com": ("/trade-swap/", "/trade-spot/"),
        "app.lighter.xyz": ("/trade/",),
        "www.bitget.com": ("/futures/usdt/", "/spot/"),
    }
    prefixes = allowed_paths.get(str(parsed.hostname or ""))
    return bool(
        prefixes
        and parsed.scheme == "https"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and any(parsed.path.startswith(prefix) and len(parsed.path) > len(prefix) for prefix in prefixes)
    )


__all__ = [
    "TelegramDeliveryError",
    "TelegramNewsPushSender",
    "TelegramTradingClient",
    "TelegramTradingUpdate",
]
