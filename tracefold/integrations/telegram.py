"""Telegram Bot API adapter for one operator-bound News channel.

It serializes a `ReaderCard` into the one text shape this channel sends. It used to receive Feishu's
card JSON and read the card back out of it -- splitting the markdown body on ` · `, recognising the
direction, novelty and magnitude words by table, pulling the report count out of a full-width
"N 条报道" suffix by regex and stripping the `行情 ` prefix -- so one channel's serializer stood
downstream of another's. That cost the market families their whole card: a market subject became a
News "标的" block with three `暂无` prices, the event time was stripped so the reader saw the send
time, and every family colour but green/red/grey collapsed to a white circle (#562 §1, §5 row 14).

What it may know is the card model and the reader-facing formats (`ReaderCard`, `card_clock`,
`LINKABLE_TICKER_RE`, `quote_line`) plus the transport contracts. It may not know a renderer, a
delivery pipeline or the market loop; `tests/architecture/test_backend_boundaries.py` holds that.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from http.client import HTTPException, HTTPSConnection
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx

from tracefold.news import (
    COMMIT_PHASE_NOT_SENT,
    COMMIT_PHASE_UNKNOWN,
    LINKABLE_TICKER_RE,
    NOVELTY_ZH,
    UNTRADEABLE_NOTICE_ZH,
    ReaderCard,
    ReaderDeliveryPresentation,
    ReaderMarketMovement,
    ReaderTradeTarget,
    TelegramDeliveryReceipt,
    card_clock,
    quote_line,
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
# Telegram's own two ways of naming one channel: the Bot API id a private channel has, and the public
# `@name` a channel gets when its owner publishes it. The operator writes whichever their channel has
# (#562 §5 rows 1 and 11); everything after the preflight works from the id Telegram itself answered.
_PUBLIC_CHANNEL_USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
_TELEGRAM_HTML_TAG_RE = re.compile(r'</?(?:b|strong|blockquote)>|<a href="[^"]+">|</a>')
_BOT_API_METHODS = frozenset({"getChat", "getMe", "getChatMember", "sendMessage", "editMessageText"})
_NEWSLIQUID_RELAY_HOSTS = frozenset({"news-history.newsliquid.com"})
_NEWSLIQUID_REUTERS_PATH_RE = re.compile(r"^/b/nL[0-9A-Z]+$")

# One mark per card, from `family + tone` and nothing else -- the same two fields Feishu maps to its
# own template colour. The model's judgment marks a News card; a market family carries no judgment,
# so its family marks it. Before this the adapter mapped Feishu's colour *names* and knew only three
# of them, which is why every OI (blue) and smart-money (turquoise) card reached readers as ⚪.
_TONE_ICON: Final[dict[str, str]] = {"bullish": "🟢", "bearish": "🔴"}
_FAMILY_ICON: Final[dict[str, str]] = {
    "news": "⚪",
    "oi": "🔵",
    "liquidation": "🔴",
    "smart_money": "💠",
    "raw": "⚪",
}
# An escalated News card is marked by its escalation rather than by its direction: it is the one
# thing the reader is meant to see first, and the card model already carries the mark as a qualifier.
_ESCALATION_ICON: Final = "⚡"
_NOVELTY_ICON: Final[dict[str, str]] = {"new_fact": "🆕", "progression": "🔄", "restatement": "♻️"}
# The lead a card holds; the rest of a very long one is on the page the card links to.
_LEAD_MAX: Final = 1_800
_TITLE_MAX: Final = 240


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
        chat_id: int | str,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] | None = None,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        normalized_token = str(bot_token or "").strip()
        if not _BOT_TOKEN_RE.fullmatch(normalized_token):
            raise ValueError("news_push_telegram_bot_token_invalid")
        self._chat_id = _channel_target(chat_id)
        # The numeric id Telegram answers with in the preflight, which every response is then checked
        # against. A `@name` target only knows it after `getChat`; an id target is already it.
        self._resolved_chat_id: int | None = self._chat_id if isinstance(self._chat_id, int) else None
        # The receipt's target identity. The `v1` string is unchanged for a numeric target, because a
        # stored receipt is only editable by the sender whose digest it matches.
        self._target_sha256 = hmac.new(
            normalized_token.encode(),
            f"telegram-private-channel-v1:{self._chat_id}".encode(),
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
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        # `channel_payload` is the JSON the delivery ledgers freeze, which is Feishu's wire shape and
        # this channel's evidence rather than its message: Telegram sends text it renders from the
        # same `ReaderCard` that payload was serialized from.
        del channel_payload
        if not self._target_validated:
            raise TelegramDeliveryError(
                "news_delivery_telegram_target_not_prepared", commit_phase=COMMIT_PHASE_NOT_SENT
            )
        pushed_at_ms = int(self._wall_clock_ms())
        text = self._message(card, presentation, pushed_at_ms=pushed_at_ms)
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
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        """Replace one previously sent channel message while preserving its original push timestamp.

        The enrichment edit is the same rendering as the send, from the updated card: the quotes,
        trade targets, market movements and progression review the Deliverer resolved after the first
        send reach this channel as an updated `ReaderCard` plus its presentation, never as a second
        parse of the text this adapter itself wrote.
        """

        del channel_payload
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
        text = self._message(card, presentation, pushed_at_ms=pushed_at_ms)
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

    def _message(
        self,
        card: ReaderCard,
        presentation: ReaderDeliveryPresentation | None,
        *,
        pushed_at_ms: int,
    ) -> str:
        """One card as this channel's text. The chat id is the adapter's, so the parent link is too."""

        view = presentation or ReaderDeliveryPresentation()
        return _telegram_message(
            card,
            view=view,
            pushed_at_ms=pushed_at_ms,
            parent_url=_telegram_message_url(self._chat_id, view.progression_review_parent_message_id),
        )

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
        # Always the numeric id, whichever way the operator named the channel: Telegram answers a
        # `@name` request with the id, and the preflight is where this adapter learned it.
        if response_chat_id != self._resolved_chat_id:
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
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise TelegramDeliveryError("news_delivery_telegram_target_chat_mismatch")
        if isinstance(self._chat_id, int):
            if chat_id != self._chat_id:
                raise TelegramDeliveryError("news_delivery_telegram_target_chat_mismatch")
        elif f"@{str(chat.get('username') or '').strip()}".casefold() != self._chat_id:
            # A `@name` binds to whatever channel currently carries it, so the answer has to carry the
            # name back: a renamed or re-registered channel is a different target, not this one.
            raise TelegramDeliveryError("news_delivery_telegram_target_chat_mismatch")
        self._resolved_chat_id = chat_id
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
    card: ReaderCard,
    *,
    view: ReaderDeliveryPresentation,
    pushed_at_ms: int | None = None,
    parent_url: str | None = None,
) -> str:
    """One `ReaderCard` as this channel's message.

    The layout is Telegram's own -- a marked title, the review band, the lead, one block per asset,
    the judgment, and a footer -- but every word, number, symbol and time in it comes from the card or
    from `card_format`. `view` is the adapter-only context the Deliverer resolves around a News card
    (trade targets, movement returns, the progression review); a market card is complete on its own
    and arrives with none of it.
    """

    news = card.header.family == "news"
    sections = [f"{_header_icon(card)} <b>{_escape_html(_clip(_header_title(card), _TITLE_MAX))}</b>"]

    if card.untradeable:
        # The card's own sentence, in the one place a reader who already read this card will see it:
        # directly under the title, above everything the edit did not change (#562 §5 row 5).
        sections.append(f"<b>{_escape_html(UNTRADEABLE_NOTICE_ZH)}</b>")
    review_band = _telegram_review_band(card, view, parent_url=parent_url)
    if review_band:
        sections.append(review_band)
    if news:
        if card.lead:
            sections.append(_escape_html(_clip(card.lead, _LEAD_MAX)))
    else:
        # A market card's body is the model's own lines: the OI change and its measurement, the
        # liquidation count and largest reported figure, the account and its action timeline, or the
        # provider's unstructured text. This channel has no market layout of its own to impose.
        body = "\n".join(_escape_html(line) for line in card.market_lines())
        if body:
            sections.append(body)
        # The families whose card has no per-asset movement block show the market's own number here,
        # the moment their card carries one (#562 PR-B). A News card states the same 24h change
        # inside its asset block and would otherwise say it twice.
        quotes = quote_line(card.quotes)
        if quotes:
            sections.append(_escape_html(quotes))

    groups: list[str] = []
    if news and card.facts.tickers:
        groups.extend(
            _telegram_asset_blocks(
                card.facts.tickers,
                ticker_links=_trade_target_links(view.trade_targets),
                market_movements=view.market_movements,
                market_data_pending=view.market_data_state == "pending",
            )
        )
    elif news and view.market_scope:
        scope_line = _telegram_scope_html(view.market_scope)
        if scope_line:
            groups.append(scope_line)
    judgment = " · ".join(part for part in (card.direction_word(), card.magnitude_word()) if part)
    if judgment:
        groups.append(f"🧭 <b>方向</b>  {_escape_html(judgment)}")

    footer = [
        line
        for line in (
            _telegram_timing_html(
                # The card's own event time, which is what the reader is being told about. A News
                # delivery knows a more exact source stamp than the leader item carries and passes it;
                # a market card carried one all along and used to lose it here, so its Feishu copy read
                # 18:40 and its Telegram copy the 00:27 this process happened to send at (#562 §1).
                event_at_ms=view.news_at_ms or card.times.event_at_ms,
                pushed_at_ms=pushed_at_ms,
                event_label="新闻时间" if news else "事件时间",
            ),
            _telegram_source_line(card, news=news),
            _telegram_link_line(card, news=news),
        )
        if line
    ]
    if footer:
        groups.append("\n".join(footer))
    # One section per group rather than one joined block: both are joined with the same separator, so
    # the message is byte-identical, and clipping can then give up an asset block without giving up
    # the source line under it.
    sections.extend(groups)

    return _fit_telegram_message(sections)


def _fit_telegram_message(sections: Sequence[str]) -> str:
    """Join the card's sections into a message Telegram will accept, clipping if it must.

    Telegram refuses a message over 4096 characters, and this used to answer that by raising: the whole
    delivery settled `terminal` and the reader got nothing at all, for a card whose title, body and
    source link would all have fitted. A clipped card is worth more than no card (#562 §5 row 7).
    Sections are dropped whole, never cut mid-way, so the HTML handed to Telegram stays well-formed.

    The order is what the card is for: the title leads and the footer closes, and everything between
    them gives way from the bottom up -- the last asset block first, then the earlier ones, then the
    body, then the review band. Popping the tail instead would take the reader's source link with it
    while asset blocks above it survived, which is exactly backwards. Only when the title and the
    footer are alone and still over the bound does the footer go too, and the renderer bounds the
    title far below the limit, so a card always leaves this function with something on it.
    """

    working = list(sections)
    while len(working) > 2 and _telegram_text_length(working) > _TELEGRAM_TEXT_MAX:
        working.pop(-2)
    while len(working) > 1 and _telegram_text_length(working) > _TELEGRAM_TEXT_MAX:
        working.pop()
    return _SECTION_SEPARATOR.join(working).strip()


def _telegram_text_length(sections: Sequence[str]) -> int:
    return len(_plain_html_text(_SECTION_SEPARATOR.join(sections).strip()))


def _header_icon(card: ReaderCard) -> str:
    """The card's one mark, by `family + tone`."""

    if card.header.family == "news" and card.header.qualifier:
        return _ESCALATION_ICON
    return _TONE_ICON.get(card.header.tone) or _FAMILY_ICON.get(card.header.family, "⚪")


def _header_title(card: ReaderCard) -> str:
    """A News card is headed by its headline alone; its escalation is the icon, not a repeated mark."""

    return card.header.subject if card.header.family == "news" else card.title()


def _telegram_review_band(
    card: ReaderCard,
    view: ReaderDeliveryPresentation,
    *,
    parent_url: str | None,
) -> str:
    """The novelty claim and the state of its review, as one block below the title."""

    novelty = view.novelty or card.facts.novelty
    review_state = view.progression_review_state
    parent_headline = view.progression_from_headline
    parent_age = view.progression_review_parent_age_minutes
    if review_state in {"rejected", "unavailable"}:
        # A claim whose review did not confirm it is not shown as a claim, and the reason a reviewer
        # gave belongs to the operator's evidence, not to the reader's card.
        novelty = "new_fact"
        review_state = parent_headline = parent_age = parent_url = None
    elif review_state == "confirmed" and not parent_url:
        novelty = "new_fact"
        review_state = parent_headline = parent_age = None

    novelty_line = _telegram_novelty_html(
        novelty,
        progression_from_headline=(None if review_state else parent_headline),
    )
    review_line = _telegram_progression_review_html(
        review_state,
        parent_headline=parent_headline,
        parent_age_minutes=parent_age,
        parent_url=parent_url,
    )
    return "\n".join(line for line in (novelty_line, review_line) if line)


def _telegram_source_line(card: ReaderCard, *, news: bool) -> str:
    """`🔗 来源  CoinDesk · 2 条报道`.

    A News card's origin is a publisher, and the destination it links to is part of that same claim,
    so the two are checked against each other. A market card's origin is the provider and the market
    kind it reported, and the link on it opens this console's own detail page -- not a source -- so it
    is written as it stands and carried on its own line below.
    """

    origin = " ".join(part for part in card.facts.source if part)
    count = f" · {card.facts.report_count} 条报道" if not news or card.facts.report_count > 1 else ""
    if not news:
        return f"🔗 <b>来源</b>  {_escape_html(origin)}{count}" if origin else ""
    source_url = card.link.url if card.link is not None and _safe_https_url(card.link.url) else ""
    if not origin and not source_url:
        return ""
    return f"🔗 <b>来源</b>  {_telegram_source_html(origin, source_url)}{count}"


def _telegram_link_line(card: ReaderCard, *, news: bool) -> str:
    """A market card's own button, as the link this channel can show one as.

    Checked by `_safe_link_url` rather than the source rule: this button is the operator's own console
    origin (`api.public_url`, validated where it is configured), not a publisher a provider named. The
    no-non-default-port rule exists because a port in provider-supplied text is a redirect trick, and
    applying it here would silently drop the button off every deployment whose console answers on a
    port -- which is most of them.
    """

    link = card.link
    if news or link is None or not _safe_link_url(link.url):
        return ""
    return f'🔗 <a href="{html.escape(link.url, quote=True)}">{_escape_html(link.label)}</a>'


def _telegram_novelty_html(value: str | None, *, progression_from_headline: str | None) -> str:
    """The model's novelty judgment, in the card's word and this channel's mark for it."""

    icon = _NOVELTY_ICON.get(str(value or ""))
    word = NOVELTY_ZH.get(str(value or ""), "")
    if icon is None or not word:
        return ""
    if value == "progression":
        previous = _clip(str(progression_from_headline or "").strip(), 72)
        suffix = f" · 接续「{_escape_html(previous)}」" if previous else ""
        return f"{icon} <b>{word}</b>{suffix}"
    return f"{icon} <b>{word}</b>"


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


def _telegram_message_url(chat_id: int | str, message_id: int | None) -> str | None:
    """The link a reader can follow back to one earlier message in this same channel."""

    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        return None
    if isinstance(chat_id, str):
        return f"https://t.me/{chat_id.removeprefix('@')}/{message_id}"
    channel_id = str(chat_id)
    if _PRIVATE_CHANNEL_ID_RE.fullmatch(channel_id) is None:
        return None
    return f"https://t.me/c/{channel_id.removeprefix('-100')}/{message_id}"


def _channel_target(chat_id: object) -> int | str:
    """The one channel this sender is bound to, as a Bot API id or a public `@name`.

    This is the only place the shape is decided. Configuration reads whatever the operator wrote and
    keeps the process running; a target this adapter cannot address costs the delivery capability and
    nothing else (#562 §5 rows 1 and 8).
    """

    if isinstance(chat_id, int) and not isinstance(chat_id, bool):
        if _PRIVATE_CHANNEL_ID_RE.fullmatch(str(chat_id)) is None:
            raise ValueError("news_push_telegram_chat_id_invalid")
        return chat_id
    if isinstance(chat_id, str) and _PUBLIC_CHANNEL_USERNAME_RE.fullmatch(chat_id.strip()):
        # Case-folded here and nowhere else. A Telegram username is case-insensitive, so `@Feed` and
        # `@feed` are one channel -- but the target string is also what the receipt digest is built
        # from, and two spellings would mean two digests: re-casing the operator's configuration would
        # orphan every receipt already stored and silently stop the enrichment edits on them.
        return chat_id.strip().casefold()
    raise ValueError("news_push_telegram_chat_id_invalid")


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


def _telegram_timing_html(*, event_at_ms: int | None, pushed_at_ms: int | None, event_label: str) -> str:
    """When it happened and when this channel was told, both on the reader's clock, to the minute."""

    if not _positive_timestamp(pushed_at_ms):
        return ""
    pushed_text = _reader_clock(int(pushed_at_ms or 0))
    if not pushed_text:
        return ""
    event_text = _reader_clock(int(event_at_ms or 0)) if _positive_timestamp(event_at_ms) else ""
    return f"{event_label}  {event_text or '暂无'}\n推送时间  {pushed_text}"


def _positive_timestamp(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _reader_clock(value_ms: int) -> str:
    """`card_format.clock`, guarded: a stamp outside the platform's time range costs its own line."""

    try:
        return card_clock(value_ms)
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


def _safe_link_url(value: str) -> bool:
    """What holds for every URL this adapter hands to Telegram, whoever supplied it.

    Bounded, `http` or `https`, a real host, and no credentials in the URL: a userinfo section is a
    redirect trick in any link, and a URL this module cannot even parse is not a link at all.
    """

    if len(value) > _SOURCE_URL_MAX_LENGTH:
        return False
    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 -- a malformed port raises here, which is the check
    except ValueError:
        return False
    return bool(
        parsed.scheme in _SOURCE_URL_DEFAULT_PORT
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def _safe_https_url(value: str) -> bool:
    """Whether a card's *source* button carries a link this adapter will hand to Telegram.

    `http` is accepted beside `https`. Refusing it dropped the source button off legitimately
    plain-HTTP publishers' cards -- the reader lost the link, and the only thing gained was a transport
    opinion about somebody else's site, which the reader's own client is better placed to have. What
    stays on top of `_safe_link_url` is the port: a non-default port in a link a provider supplied is a
    redirect trick, not a publisher (#562 §5 row 11). A link the operator configured is not provider
    text and is checked by `_safe_link_url` alone.
    """

    if not _safe_link_url(value):
        return False
    parsed = urlsplit(value)
    return parsed.port in {None, _SOURCE_URL_DEFAULT_PORT.get(parsed.scheme)}


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
        if LINKABLE_TICKER_RE.fullmatch(ticker) is None or LINKABLE_TICKER_RE.fullmatch(base_symbol) is None:
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
