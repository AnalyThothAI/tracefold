"""Telegram Bot API adapter for one operator-bound News channel."""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from http.client import HTTPException, HTTPSConnection
from typing import Any
from urllib.parse import urlsplit

import httpx

_TELEGRAM_API_ORIGIN = "https://api.telegram.org"
_TELEGRAM_TIMEOUT_SECONDS = 6.5
_TELEGRAM_TOTAL_CALL_BUDGET_SECONDS = 7.0
_TELEGRAM_MIN_REQUEST_BUDGET_SECONDS = 0.05
_TELEGRAM_MAX_PHASE_TIMEOUT_SECONDS = 1.25
_TELEGRAM_TEXT_MAX = 4096
_TELEGRAM_RESPONSE_MAX_BYTES = 1024 * 1024
_BOT_TOKEN_RE = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{30,80}$")
_PRIVATE_CHANNEL_ID_RE = re.compile(r"^-100[1-9][0-9]{5,15}$")
_TELEGRAM_TIME_RE = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")
_REPORTING_ORIGIN_RE = re.compile(r"^(?P<origin>.+)（(?P<count>[1-9][0-9]*) 条报道）$")
_BOT_API_METHODS = frozenset({"getChat", "getMe", "getChatMember", "sendMessage"})
_DIRECTION_LABELS = frozenset({"利多", "利空", "中性", "不明确"})
_HEADER_ICON = {"green": "🟢", "red": "🔴", "grey": "⚪"}


class TelegramDeliveryError(RuntimeError):
    """A sanitized expected Telegram delivery failure."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


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
    """Send one News card to the single channel fixed at construction time."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: int,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        normalized_token = str(bot_token or "").strip()
        if not _BOT_TOKEN_RE.fullmatch(normalized_token):
            raise ValueError("news_push_telegram_bot_token_invalid")
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or _PRIVATE_CHANNEL_ID_RE.fullmatch(str(chat_id)) is None
        ):
            raise ValueError("news_push_telegram_chat_id_invalid")
        self._chat_id = chat_id
        self._target_sha256 = hmac.new(
            normalized_token.encode(),
            f"telegram-private-channel-v1:{chat_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self._target_validated = False
        self._monotonic = monotonic or time.monotonic
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

    def send_card(self, card: Mapping[str, Any]) -> dict[str, Any]:
        if not self._target_validated:
            raise TelegramDeliveryError("news_delivery_telegram_target_not_prepared")
        text, source_button = _telegram_message(card)
        deadline_at = self._monotonic() + _TELEGRAM_TOTAL_CALL_BUDGET_SECONDS
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if source_button is not None:
            payload["reply_markup"] = {"inline_keyboard": [[source_button]]}
        try:
            result = self._call_api(
                "sendMessage",
                payload,
                error_prefix="news_delivery_telegram",
                deadline_at=deadline_at,
            )
            chat = result.get("chat")
            message_id = result.get("message_id")
            if not isinstance(chat, Mapping) or isinstance(message_id, bool) or not isinstance(message_id, int):
                raise TelegramDeliveryError("news_delivery_telegram_response_invalid")
            response_chat_id = chat.get("id")
            if isinstance(response_chat_id, bool) or not isinstance(response_chat_id, int):
                raise TelegramDeliveryError("news_delivery_telegram_response_invalid")
            if response_chat_id != self._chat_id:
                raise TelegramDeliveryError("news_delivery_telegram_response_chat_mismatch")
        except TelegramDeliveryError:
            self._target_validated = False
            raise
        return {"provider": "telegram", "message_id": message_id, "target_sha256": self._target_sha256}

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
        if chat.get("type") != "channel" or str(chat.get("username") or "").strip():
            raise TelegramDeliveryError("news_delivery_telegram_target_not_private_channel")

        bot = self._call_api(
            "getMe",
            {},
            error_prefix="news_delivery_telegram_preflight",
            deadline_at=deadline_at,
        )
        bot_id = bot.get("id")
        if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot.get("is_bot") is not True:
            raise TelegramDeliveryError("news_delivery_telegram_bot_identity_invalid")
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
        if membership.get("status") != "administrator" or membership.get("can_post_messages") is not True:
            raise TelegramDeliveryError("news_delivery_telegram_target_post_permission_missing")

    def _call_api(
        self,
        method: str,
        payload: Mapping[str, Any],
        *,
        error_prefix: str,
        deadline_at: float,
    ) -> Mapping[str, Any]:
        remaining = deadline_at - self._monotonic()
        if remaining < _TELEGRAM_MIN_REQUEST_BUDGET_SECONDS:
            raise TelegramDeliveryError(f"{error_prefix}_budget_exhausted")
        phase_timeout = min(_TELEGRAM_MAX_PHASE_TIMEOUT_SECONDS, remaining / 4)
        try:
            response = self._client.post(
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
        if status_code == 429 or status_code >= 500:
            raise TelegramDeliveryError(f"{error_prefix}_http_failed", status_code=status_code)
        if status_code < 200 or status_code >= 300:
            raise TelegramDeliveryError(f"{error_prefix}_http_rejected", status_code=status_code)
        try:
            response_payload = response.json()
        except ValueError:
            raise TelegramDeliveryError(f"{error_prefix}_response_invalid", status_code=status_code) from None
        if not isinstance(response_payload, Mapping) or response_payload.get("ok") is not True:
            raise TelegramDeliveryError(f"{error_prefix}_business_rejected", status_code=status_code)
        result = response_payload.get("result")
        if not isinstance(result, Mapping):
            raise TelegramDeliveryError(f"{error_prefix}_response_invalid", status_code=status_code)
        return result

    def close(self) -> None:
        self._client.close()


def _telegram_message(card: Mapping[str, Any]) -> tuple[str, dict[str, str] | None]:
    title = _nested_text(card.get("header"), "title") or "Tracefold 新闻事件"
    header = card.get("header")
    template = str(header.get("template") or "") if isinstance(header, Mapping) else ""
    icon = _HEADER_ICON.get(template, "⚪")
    if title.startswith("⚡ "):
        icon = "⚡"
        title = title.removeprefix("⚡ ").strip()

    content_lines: list[str] = []
    source_button: dict[str, str] | None = None
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
            elif tag == "action" and source_button is None:
                source_button = _source_button(element.get("actions"))

    market_line = next((line for line in reversed(content_lines) if line.startswith("行情 ")), "")
    if market_line:
        content_lines.remove(market_line)
    facts_line = content_lines.pop() if content_lines else ""
    explanation = "\n".join(content_lines)
    signal, source = _telegram_facts(facts_line)

    sections = [f"{icon} <b>{_escape_html(_clip(title, 240))}</b>"]
    if explanation:
        sections.append(_escape_html(_clip(explanation, 1800)))

    metadata: list[str] = []
    if signal:
        signal_label = "判断" if signal.split(" · ", maxsplit=1)[0] in _DIRECTION_LABELS else "标的"
        metadata.append(f"🧭 <b>{signal_label}</b>  {_escape_html(_clip(signal, 600))}")
    if market_line:
        market = market_line.removeprefix("行情 ").strip()
        if market:
            metadata.append(f"📊 <b>行情</b>  {_escape_html(_clip(market, 800))}")
    if source:
        metadata.append(f"🕒 <b>来源</b>  {_escape_html(_clip(source, 300))}")
    if metadata:
        sections.append("\n".join(metadata))

    message = "\n\n".join(sections).strip()
    if len(_plain_html_text(message)) > _TELEGRAM_TEXT_MAX:
        raise TelegramDeliveryError("news_delivery_telegram_message_too_long")
    return message, source_button


def _telegram_facts(value: str) -> tuple[str, str]:
    parts = [part.strip() for part in str(value or "").split(" · ") if part.strip()]
    if not parts:
        return "", ""
    time_text = parts.pop() if parts and _TELEGRAM_TIME_RE.fullmatch(parts[-1]) else ""
    origin = parts.pop() if time_text and parts else ""
    source_parts: list[str] = []
    if origin:
        match = _REPORTING_ORIGIN_RE.fullmatch(origin)
        if match is None:
            source_parts.append(origin)
        else:
            source_parts.extend((match.group("origin"), f"{match.group('count')} 条报道"))
    if time_text:
        source_parts.append(time_text)
    return " · ".join(parts), " · ".join(source_parts)


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _escape_html(value: str) -> str:
    return html.escape(value, quote=False)


def _plain_html_text(value: str) -> str:
    return html.unescape(re.sub(r"</?(?:b|strong)>", "", value))


def _nested_text(value: object, key: str) -> str:
    if not isinstance(value, Mapping):
        return ""
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        return ""
    return str(nested.get("content") or "").strip()


def _source_button(value: object) -> dict[str, str] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    for action in value:
        if not isinstance(action, Mapping) or action.get("tag") != "button":
            continue
        url = str(action.get("url") or "").strip()
        if not _safe_https_url(url):
            continue
        label_source = action.get("text")
        label = str(label_source.get("content") or "").strip() if isinstance(label_source, Mapping) else ""
        button_label = "查看原文 ↗" if not label or label == "打开来源" else label
        return {"text": button_label[:64], "url": url}
    return None


def _safe_https_url(value: str) -> bool:
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


__all__ = ["TelegramDeliveryError", "TelegramNewsPushSender"]
