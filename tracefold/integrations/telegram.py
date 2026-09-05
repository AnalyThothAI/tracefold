"""Telegram Bot API adapter for one operator-bound News channel."""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.client import HTTPException, HTTPSConnection
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from tracefold.news import (
    COMMIT_PHASE_NOT_SENT,
    COMMIT_PHASE_UNKNOWN,
    ReaderDeliveryPresentation,
    ReaderMarketMovement,
    ReaderTradeTarget,
    TelegramDeliveryReceipt,
)

_TELEGRAM_API_ORIGIN = "https://api.telegram.org"
_TELEGRAM_TIMEOUT_SECONDS = 6.5
_TELEGRAM_TOTAL_CALL_BUDGET_SECONDS = 7.0
_TELEGRAM_MIN_REQUEST_BUDGET_SECONDS = 0.05
_TELEGRAM_MAX_PHASE_TIMEOUT_SECONDS = 1.25
_TELEGRAM_TEXT_MAX = 4096
_TELEGRAM_RESPONSE_MAX_BYTES = 1024 * 1024
_SOURCE_URL_MAX_LENGTH = 2_048
_SOURCE_URL_DEFAULT_PORT = {"http": 80, "https": 443}
_SECTION_SEPARATOR = "\n\n"
_BOT_TOKEN_RE = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{30,80}$")
_PRIVATE_CHANNEL_ID_RE = re.compile(r"^-100[1-9][0-9]{5,15}$")
_TELEGRAM_TIME_RE = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")
_REPORTING_ORIGIN_RE = re.compile(r"^(?P<origin>.+)（(?P<count>[1-9][0-9]*) 条报道）$")
_LINKABLE_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
_TELEGRAM_HTML_TAG_RE = re.compile(r'</?(?:b|strong|blockquote)>|<a href="[^"]+">|</a>')
_BOT_API_METHODS = frozenset({"getChat", "getMe", "getChatMember", "sendMessage", "editMessageText"})
_DIRECTION_LABELS = frozenset({"利多", "利空", "中性", "不明确", "方向待定"})
_NOVELTY_LABELS = frozenset({"新事实", "新进展", "复述"})
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
class _TelegramFacts:
    direction: str = ""
    novelty: str = ""
    magnitude: str = ""
    assets: tuple[str, ...] = ()
    origin: str = ""
    report_count: int | None = None


class TelegramDeliveryError(RuntimeError):
    """A sanitized expected Telegram delivery failure, and what it proves about the message.

    Same additive shape as Feishu's, and for the same reason: `code` stays what the News Deliverer
    records, and `commit_phase` is the only honest way to tell a retry from a duplicate (#553).
    """

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        commit_phase: str = COMMIT_PHASE_UNKNOWN,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.commit_phase = commit_phase
        self.retryable = retryable


# Raised by httpx before any request byte is written. Every other transport failure happened at or
# after the write and cannot prove the message did not arrive.
_PRE_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.UnsupportedProtocol)


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
            # Connecting is its own step so its failure is its own exception. Everything below has
            # already written request bytes, and cannot claim the message did not arrive.
            try:
                connection.connect()
            except (HTTPException, OSError) as exc:
                raise httpx.ConnectError("telegram_transport_connect_failed", request=request) from exc
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
        wall_clock_ms: Callable[[], int] | None = None,
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

    def send_card(
        self,
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        if not self._target_validated:
            raise TelegramDeliveryError(
                "news_delivery_telegram_target_not_prepared", commit_phase=COMMIT_PHASE_NOT_SENT
            )
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
        """Replace one previously sent channel message while preserving its original push timestamp."""

        if not self._target_validated:
            raise TelegramDeliveryError(
                "news_delivery_telegram_target_not_prepared", commit_phase=COMMIT_PHASE_NOT_SENT
            )
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

    def _message_payload(self, text: str, *, message_id: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if message_id is not None:
            payload["message_id"] = message_id
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
        # The exact chat id, and that it is a channel rather than a group or a personal chat, is what
        # binds delivery to one target. Whether that channel also has a public @name is the operator's
        # own publishing decision, and refusing it turned a product choice into a dead capability
        # (#562 §5 row 11). Everything else this preflight proves -- the id, the type, the bot's own
        # identity and its permission to post -- is unchanged.
        if chat.get("type") != "channel":
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
        remaining = deadline_at - self._monotonic()
        if remaining < _TELEGRAM_MIN_REQUEST_BUDGET_SECONDS:
            # The call was never made, so nothing was sent -- but the budget is gone for this attempt,
            # and retrying it inside the same attempt would only exhaust it again.
            raise TelegramDeliveryError(
                f"{error_prefix}_budget_exhausted", commit_phase=COMMIT_PHASE_NOT_SENT, retryable=True
            )
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
        except _PRE_CONNECT_FAILURES:
            raise TelegramDeliveryError(
                f"{error_prefix}_transport_failed", commit_phase=COMMIT_PHASE_NOT_SENT, retryable=True
            ) from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise TelegramDeliveryError(f"{error_prefix}_transport_failed") from None

        status_code = int(response.status_code)
        if status_code == 429:
            raise TelegramDeliveryError(
                f"{error_prefix}_http_failed",
                status_code=status_code,
                commit_phase=COMMIT_PHASE_NOT_SENT,
                retryable=True,
            )
        if status_code >= 500:
            # Telegram's own tier answered. It can answer that way after accepting the message.
            raise TelegramDeliveryError(f"{error_prefix}_http_failed", status_code=status_code)
        if status_code < 200 or status_code >= 300:
            raise TelegramDeliveryError(
                f"{error_prefix}_http_rejected", status_code=status_code, commit_phase=COMMIT_PHASE_NOT_SENT
            )
        try:
            response_payload = response.json()
        except ValueError:
            raise TelegramDeliveryError(f"{error_prefix}_response_invalid", status_code=status_code) from None
        if not isinstance(response_payload, Mapping) or response_payload.get("ok") is not True:
            raise TelegramDeliveryError(
                f"{error_prefix}_business_rejected",
                status_code=status_code,
                commit_phase=COMMIT_PHASE_NOT_SENT,
            )
        return response_payload.get("result")

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
    facts = _telegram_facts(facts_line)
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
    explanation_index: int | None = None
    if explanation:
        explanation_index = len(sections)
        sections.append(_escape_html(_clip(explanation, 1800)))

    metadata_groups: list[str] = []
    if facts.assets:
        metadata_groups.extend(
            _telegram_asset_blocks(
                facts.assets,
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
    # One section per metadata group rather than one joined block: both are joined with the same
    # separator, so the message is byte-identical, and clipping can then give up an asset block without
    # giving up the source line under it.
    sections.extend(metadata_groups)

    return _fit_telegram_message(sections, explanation_index=explanation_index)


def _fit_telegram_message(sections: Sequence[str], *, explanation_index: int | None) -> str:
    """Join the card's sections into a message Telegram will accept, clipping if it must.

    Telegram refuses a message over 4096 characters, and this used to answer that by raising: the whole
    delivery settled `terminal` and the reader got nothing at all, for a card whose title, body and
    source link would all have fitted. A clipped card is worth more than no card (#562 §5 row 7).
    Sections are dropped whole, never cut mid-way, so the HTML handed to Telegram stays well-formed.
    """

    working = list(sections)
    if _telegram_text_length(working) <= _TELEGRAM_TEXT_MAX:
        return _SECTION_SEPARATOR.join(working).strip()
    # The title leads and the source closes; the metadata blocks between the body and the source give
    # way first, from the bottom up. Dropping from the tail instead would take the reader's link with
    # it, which is the one thing the card is for.
    body = 0 if explanation_index is None else explanation_index
    while len(working) >= body + 3 and _telegram_text_length(working) > _TELEGRAM_TEXT_MAX:
        working.pop(-2)
    # Backstop: with the metadata gone, whatever is left goes too, newest block first. The renderer
    # bounds the title and the body well under the limit, so this is the case that says a future
    # renderer changed those bounds -- and it still ships a card rather than none.
    while len(working) > 1 and _telegram_text_length(working) > _TELEGRAM_TEXT_MAX:
        working.pop()
    return _SECTION_SEPARATOR.join(working).strip()


def _telegram_text_length(sections: Sequence[str]) -> int:
    return len(_plain_html_text(_SECTION_SEPARATOR.join(sections).strip()))


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


def _telegram_facts(value: str) -> _TelegramFacts:
    parts = [part.strip() for part in str(value or "").split(" · ") if part.strip()]
    if not parts:
        return _TelegramFacts()
    if _TELEGRAM_TIME_RE.fullmatch(parts[-1]):
        parts.pop()
    origin = parts.pop() if parts else ""
    report_count: int | None = None
    match = _REPORTING_ORIGIN_RE.fullmatch(origin)
    if match is not None:
        origin = match.group("origin")
        report_count = int(match.group("count"))
    direction = parts.pop(0) if parts and parts[0] in _DIRECTION_LABELS else ""
    novelty = parts.pop(0) if parts and parts[0] in _NOVELTY_LABELS else ""
    magnitude = parts.pop(0) if parts and parts[0] in _MAGNITUDE_LABELS else ""
    asset_text = " ".join(parts).strip()
    # Reader assets are code-grounded exchange symbols. Fail closed when a future
    # presentation-label drift leaves arbitrary metadata in the positional tail.
    assets = tuple(part for part in asset_text.split() if _LINKABLE_TICKER_RE.fullmatch(part) is not None)
    return _TelegramFacts(
        direction=direction,
        novelty=novelty,
        magnitude=magnitude,
        assets=assets,
        origin=origin if origin != "-" else "",
        report_count=report_count,
    )


def _telegram_asset_blocks(
    assets: Sequence[str],
    *,
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
            day_change = "暂无"
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
    """Whether a card's source button carries a link this adapter will hand to Telegram.

    `http` is accepted beside `https`. Refusing it dropped the source button off legitimately
    plain-HTTP publishers' cards -- the reader lost the link, and the only thing gained was a transport
    opinion about somebody else's site, which the reader's own client is better placed to have. The
    rules that are about this adapter stay: no credentials in the URL, and no non-default port, because
    a userinfo or port in a provider-supplied link is a redirect trick, not a publisher (#562 §5 row 11).
    """

    if len(value) > _SOURCE_URL_MAX_LENGTH:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in _SOURCE_URL_DEFAULT_PORT
        and parsed.hostname
        and port in {None, _SOURCE_URL_DEFAULT_PORT.get(parsed.scheme)}
        and parsed.username is None
        and parsed.password is None
    )


def _trade_target_links(trade_targets: Sequence[ReaderTradeTarget]) -> dict[str, str]:
    """The one host allowlist for reader trade actions: a venue this adapter knows, one fixed template.

    `news.delivery.reader_trade_targets` already decided which catalogue contract may become a reader
    action and what its identity looks like. This function used to build a URL from that decision and
    then parse its own string back to re-check the host, the port, the userinfo, the query and the path
    -- a second copy of a rule that had already been applied (#562). What replaces it is structural:
    the host and path prefix are literals here and every interpolated segment is either matched against
    the code-owned ticker grammar or percent-encoded, so a built link cannot leave the venue it names
    no matter what a target carries.
    """

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
            url = f"https://www.binance.com/en/futures/{quote(venue_symbol, safe='')}"
        elif target.venue == "binance.spot":
            url = f"https://www.binance.com/en/trade/{base_symbol}_{quote(quote_asset, safe='')}"
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
        links.setdefault(ticker, url)
    return links


__all__ = ["TelegramDeliveryError", "TelegramNewsPushSender"]
