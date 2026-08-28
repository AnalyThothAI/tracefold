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
from urllib.parse import urlsplit

import httpx

from tracefold.news import ReaderDeliveryPresentation, ReaderMarketMovement, ReaderTradeTarget

_TELEGRAM_API_ORIGIN = "https://api.telegram.org"
_TELEGRAM_TIMEOUT_SECONDS = 6.5
_TELEGRAM_TOTAL_CALL_BUDGET_SECONDS = 7.0
_TELEGRAM_MIN_REQUEST_BUDGET_SECONDS = 0.05
_TELEGRAM_MAX_PHASE_TIMEOUT_SECONDS = 1.25
_TELEGRAM_TEXT_MAX = 4096
_TELEGRAM_RESPONSE_MAX_BYTES = 1024 * 1024
_SOURCE_URL_MAX_LENGTH = 2_048
_BOT_TOKEN_RE = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{30,80}$")
_PRIVATE_CHANNEL_ID_RE = re.compile(r"^-100[1-9][0-9]{5,15}$")
_TELEGRAM_TIME_RE = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")
_REPORTING_ORIGIN_RE = re.compile(r"^(?P<origin>.+)（(?P<count>[1-9][0-9]*) 条报道）$")
_LINKABLE_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
_BINANCE_FUTURES_PATH_RE = re.compile(r"^/en/futures/[A-Z0-9]{2,40}$")
_BINANCE_SPOT_PATH_RE = re.compile(r"^/en/trade/[A-Z0-9]{1,20}_[A-Z0-9]{1,20}$")
_TELEGRAM_HTML_TAG_RE = re.compile(r'</?(?:b|strong)>|<a href="[^"]+">|</a>')
_BOT_API_METHODS = frozenset({"getChat", "getMe", "getChatMember", "sendMessage"})
_DIRECTION_LABELS = frozenset({"利多", "利空", "中性", "不明确"})
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
            raise TelegramDeliveryError("news_delivery_telegram_target_not_prepared")
        view = presentation or ReaderDeliveryPresentation()
        pushed_at_ms = int(self._wall_clock_ms())
        text = _telegram_message(
            card,
            trade_targets=view.trade_targets,
            market_movements=view.market_movements,
            news_at_ms=view.news_at_ms,
            observed_at_ms=view.observed_at_ms,
            pushed_at_ms=pushed_at_ms,
        )
        deadline_at = self._monotonic() + _TELEGRAM_TOTAL_CALL_BUDGET_SECONDS
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
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


def _telegram_message(
    card: Mapping[str, Any],
    *,
    trade_targets: Sequence[ReaderTradeTarget] = (),
    market_movements: Sequence[ReaderMarketMovement] = (),
    news_at_ms: int | None = None,
    observed_at_ms: int | None = None,
    pushed_at_ms: int | None = None,
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
    ticker_links = _binance_ticker_links(trade_targets)

    sections = [f"{icon} <b>{_escape_html(_clip(title, 240))}</b>"]
    if explanation:
        sections.append(_escape_html(_clip(explanation, 1800)))

    metadata_groups: list[str] = []
    if facts.assets:
        metadata_groups.append(
            "🎯 <b>标的</b>\n"
            + "\n".join(
                _telegram_asset_lines(
                    facts.assets,
                    market_line=market_line,
                    ticker_links=ticker_links,
                    market_movements=market_movements,
                )
            )
        )
    if facts.direction or facts.magnitude:
        direction = _escape_html(facts.direction or "不明确")
        magnitude = _escape_html(_MAGNITUDE_LABELS.get(facts.magnitude, facts.magnitude or "未知"))
        judgment = [f"🧭 <b>方向</b>  {direction}", f"📊 <b>影响程度</b>  {magnitude}"]
        if facts.novelty:
            judgment.append(f"🆕 <b>进展</b>  {_escape_html(facts.novelty)}")
        metadata_groups.append("\n".join(judgment))
    elif facts.novelty:
        metadata_groups.append(f"🆕 <b>进展</b>  {_escape_html(facts.novelty)}")
    if facts.origin or source_url:
        source = _telegram_source_html(facts.origin, source_url)
        count = f" · {facts.report_count} 条报道" if facts.report_count is not None else ""
        metadata_groups.append(f"🔗 <b>来源</b>  {source}{count}")
    timing = _telegram_timing_html(news_at_ms=news_at_ms, observed_at_ms=observed_at_ms, pushed_at_ms=pushed_at_ms)
    if timing:
        metadata_groups.append(f"⏱ <b>时间</b>\n{timing}")
    if metadata_groups:
        sections.append("\n\n".join(metadata_groups))

    message = "\n\n".join(sections).strip()
    if len(_plain_html_text(message)) > _TELEGRAM_TEXT_MAX:
        raise TelegramDeliveryError("news_delivery_telegram_message_too_long")
    return message


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
    assets = tuple(part for part in asset_text.split() if part and part != "-")
    return _TelegramFacts(
        direction=direction,
        novelty=novelty,
        magnitude=magnitude,
        assets=assets,
        origin=origin if origin != "-" else "",
        report_count=report_count,
    )


def _telegram_asset_lines(
    assets: Sequence[str],
    *,
    market_line: str,
    ticker_links: Mapping[str, str],
    market_movements: Sequence[ReaderMarketMovement],
) -> list[str]:
    movements = {
        movement.ticker: movement for movement in market_movements if isinstance(movement, ReaderMarketMovement)
    }
    lines: list[str] = []
    for asset in assets:
        ticker = _telegram_ticker_html(asset, ticker_links)
        movement = movements.get(asset)
        if movement is not None:
            after_news = (
                _format_bps(movement.after_news_bps)
                if movement.after_news_bps is not None
                else ("待计算" if movement.one_hour_state in {"not_due", "pending"} else "暂无")
            )
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
            lines.append(f"{ticker} 新闻后 {after_news}，1h {one_hour}，24h {day_change}")
            continue
        change_match = re.search(
            rf"(?<![A-Z0-9.-]){re.escape(asset)}\s+\$[0-9,.]+\s+24h\s+(?P<pct>[+-]?[0-9]+(?:\.[0-9]+)?%)(?![A-Z0-9.-])",
            market_line,
        )
        day_change = _escape_html(change_match.group("pct")) if change_match is not None else "暂无"
        lines.append(f"{ticker} 新闻后 待计算，1h 待到期，24h {day_change}")
    return lines


def _format_bps(value: int) -> str:
    percentage = Decimal(value) / Decimal(100)
    return f"{'+' if value > 0 else ''}{percentage:.2f}%"


def _telegram_timing_html(
    *,
    news_at_ms: int | None,
    observed_at_ms: int | None,
    pushed_at_ms: int | None,
) -> str:
    if not _positive_timestamp(pushed_at_ms):
        return ""
    news_text = _format_reader_time(int(news_at_ms)) if _positive_timestamp(news_at_ms) else ""
    pushed_text = _format_reader_time(int(pushed_at_ms))
    if not pushed_text:
        return ""
    if _positive_timestamp(observed_at_ms):
        processing_seconds = max(0, int(pushed_at_ms) - int(observed_at_ms)) // 1000
        processing = f"{processing_seconds} 秒"
    else:
        processing = "暂无"
    return f"新闻时间  {news_text or '暂无'}\n处理时长  {processing}\n推送时间  {pushed_text}"


def _positive_timestamp(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _format_reader_time(value_ms: int) -> str:
    try:
        return datetime.fromtimestamp(value_ms / 1000, tz=_READER_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
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


def _binance_ticker_links(trade_targets: Sequence[ReaderTradeTarget]) -> dict[str, str]:
    links: dict[str, str] = {}
    for target in trade_targets:
        if not isinstance(target, ReaderTradeTarget):
            continue
        ticker = target.ticker
        base_symbol = target.base_symbol
        quote_asset = target.quote_asset
        venue_symbol = target.venue_symbol
        if (
            _LINKABLE_TICKER_RE.fullmatch(ticker) is None
            or _LINKABLE_TICKER_RE.fullmatch(base_symbol) is None
            or _LINKABLE_TICKER_RE.fullmatch(quote_asset) is None
            or ticker != base_symbol
            or venue_symbol != f"{base_symbol}{quote_asset}"
        ):
            continue
        if target.venue == "binance.perp":
            url = f"https://www.binance.com/en/futures/{venue_symbol}"
        elif target.venue == "binance.spot":
            url = f"https://www.binance.com/en/trade/{base_symbol}_{quote_asset}"
        else:
            continue
        if _safe_binance_trade_url(url):
            links.setdefault(ticker, url)
    return links


__all__ = ["TelegramDeliveryError", "TelegramNewsPushSender"]
