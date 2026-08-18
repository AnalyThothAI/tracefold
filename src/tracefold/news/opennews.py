from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .identity import (
    collapse_javascript_whitespace,
    javascript_trim,
    utf16_length,
    utf16_slice,
    web_usv_string,
)
from .models import NewsFeedEntry

OPENNEWS_SOURCE_ID = "news-opennews"

_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
_MAX_COINS = 32
_MAX_HEADLINE_LEN = 500
_MAX_DESCRIPTION_LEN = 400
_MIN_DESCRIPTION_LEN = 40
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class OpenNewsExpectedError(RuntimeError):
    """An expected provider/auth/transport/payload outcome without secret text."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class OpenNewsHistoryError(RuntimeError):
    """A bounded, sanitized Strategy-history outcome."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OpenNewsStrategyHistory(Protocol):
    async def get_strategy_list(self, *, limit: int, page: int) -> Mapping[str, Any]: ...

    async def get_strategy_hits(
        self,
        *,
        strategy_id: str,
        limit: int,
        page: int,
    ) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class OpenNewsEvent:
    provider_record_id: str
    observation_kind: Literal["report"]
    provider_metadata: dict[str, Any]
    entry: NewsFeedEntry
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class OpenNewsStrategyHitPage:
    events: tuple[OpenNewsEvent, ...]
    page: int
    total: int
    has_more: bool


def parse_opennews_strategy_list(
    payload: object,
    *,
    strategy_ids: frozenset[str],
) -> tuple[dict[str, Any], ...]:
    data = _history_data(payload)
    strategies: list[dict[str, Any]] = []
    for value in data:
        if not isinstance(value, Mapping):
            raise OpenNewsHistoryError("opennews_history_payload_invalid")
        strategy_id = _wire_strategy_id(value.get("id"))
        enabled = value.get("enabled")
        if not strategy_id or not isinstance(enabled, bool):
            raise OpenNewsHistoryError("opennews_history_payload_invalid")
        if strategy_id not in strategy_ids:
            continue
        strategies.append(
            {
                "id": strategy_id,
                "name": _text(value.get("name"))[:128],
                "enabled": enabled,
            }
        )
    return tuple(sorted(strategies, key=lambda row: row["id"]))


def parse_opennews_strategy_hits(
    payload: object,
    *,
    strategy_ids: frozenset[str],
) -> OpenNewsStrategyHitPage:
    if not isinstance(payload, Mapping):
        raise OpenNewsHistoryError("opennews_history_payload_invalid")
    data = _history_data(payload)
    page = _history_nonnegative_int(payload.get("page"), minimum=1)
    total = _history_nonnegative_int(payload.get("total"), minimum=0)
    limit = _history_nonnegative_int(payload.get("limit"), minimum=1)
    events: list[OpenNewsEvent] = []
    for value in data:
        if not isinstance(value, Mapping):
            raise OpenNewsHistoryError("opennews_history_payload_invalid")
        event = parse_opennews_message(
            {"method": "strategy.triggered", "params": value},
            strategy_ids=strategy_ids,
        )
        if event is None:
            raise OpenNewsHistoryError("opennews_history_payload_invalid")
        events.append(event)
    return OpenNewsStrategyHitPage(
        events=tuple(events),
        page=page,
        total=total,
        has_more=(page * limit) < total,
    )


def _history_data(payload: object) -> list[Any]:
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        raise OpenNewsHistoryError("opennews_history_payload_invalid")
    data = payload.get("data")
    if not isinstance(data, list):
        raise OpenNewsHistoryError("opennews_history_payload_invalid")
    return data


def _history_nonnegative_int(value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OpenNewsHistoryError("opennews_history_payload_invalid")
    return int(value)


def parse_opennews_message(
    message: object,
    *,
    strategy_ids: frozenset[str],
) -> OpenNewsEvent | None:
    if message == "ping" or not isinstance(message, Mapping):
        return None
    if _text(message.get("method")) != "strategy.triggered":
        return None
    params = message.get("params")
    if not isinstance(params, Mapping):
        return None
    strategy = params.get("strategy")
    if not isinstance(strategy, Mapping):
        return None
    strategy_id = _wire_strategy_id(strategy.get("id"))
    if not strategy_id or strategy_id not in strategy_ids:
        return None
    provider_record_id = _wire_strategy_id(params.get("id"))
    if not provider_record_id:
        return None
    canonical_url = _article_url(_text(params.get("link")))
    raw_text = _content_text(params.get("text"))
    blocks = _logical_blocks(raw_text)
    title = javascript_trim(web_usv_string(utf16_slice(blocks[0], _MAX_HEADLINE_LEN))) if blocks else ""
    entry = NewsFeedEntry(
        guid=provider_record_id,
        link=canonical_url or None,
        title=title or None,
        description=_canonical_description(
            explicit="",
            remaining_blocks=blocks[1:],
            title=title,
        ),
        published_at_ms=_timestamp_ms(params.get("ts")),
        reporting_origin=_reporting_origin(params, canonical_url=canonical_url),
    )
    return OpenNewsEvent(
        provider_record_id=provider_record_id,
        observation_kind="report",
        provider_metadata=_provider_metadata(
            params,
            strategy=strategy,
            strategy_id=strategy_id,
        ),
        entry=entry,
        raw_text=raw_text[:20_000],
    )


def _timestamp_ms(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        try:
            number = float(value)
        except OverflowError:
            return None
        if not isfinite(number):
            return None
        return int(number * 1_000) if abs(number) < 100_000_000_000 else int(number)
    text = javascript_trim(str(value))
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            number = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
        return int(number * 1_000) if isfinite(number) else None
    if not isfinite(number):
        return None
    return int(number * 1_000) if abs(number) < 100_000_000_000 else int(number)


def _article_url(value: str) -> str:
    try:
        parsed = urlsplit(javascript_trim(value))
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
        )
    )
    path = parsed.path or "/"
    if path == "/" and not query:
        return ""
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


def _reporting_origin(params: Mapping[str, Any], *, canonical_url: str) -> str:
    explicit = _text(params.get("source")).lower()
    if explicit:
        return explicit[:128]
    if canonical_url:
        return str(urlsplit(canonical_url).hostname or "").lower() or "opennews"
    return "opennews"


def _logical_blocks(value: str) -> tuple[str, ...]:
    decoded = html.unescape(value)
    separated = _BREAK_RE.sub("\n", decoded).replace("\r\n", "\n").replace("\r", "\n")
    blocks = []
    for raw in separated.split("\n"):
        cleaned = html.unescape(raw)
        cleaned = _TAG_RE.sub(" ", cleaned)
        cleaned = _CONTROL_RE.sub(" ", cleaned)
        cleaned = collapse_javascript_whitespace(cleaned)
        if cleaned:
            blocks.append(cleaned)
    return tuple(blocks)


def _canonical_description(
    *,
    explicit: str,
    remaining_blocks: tuple[str, ...],
    title: str,
) -> str:
    explicit_blocks = _logical_blocks(explicit)
    description = javascript_trim(" ".join(explicit_blocks or remaining_blocks))
    if utf16_length(description) < _MIN_DESCRIPTION_LEN:
        return ""
    if collapse_javascript_whitespace(description.lower()) == collapse_javascript_whitespace(title.lower()):
        return ""
    return web_usv_string(utf16_slice(description, _MAX_DESCRIPTION_LEN))


def _provider_metadata(
    params: Mapping[str, Any],
    *,
    strategy: Mapping[str, Any],
    strategy_id: str,
) -> dict[str, Any]:
    ai_rating = params.get("aiRating")
    ai = ai_rating if isinstance(ai_rating, Mapping) else {}
    result: dict[str, Any] = {}
    score = _number(params.get("score"))
    if score is None:
        score = _number(ai.get("score"))
    if score is not None:
        result["score"] = score
    source = _text(params.get("source"))
    if source:
        result["source"] = source[:128]
    for key in ("signal", "grade"):
        value = _text(params.get(key)) or _text(ai.get(key))
        if value:
            result[key] = value[:32]
    coins = params.get("coins")
    if isinstance(coins, list):
        normalized: list[dict[str, Any]] = []
        for raw_coin in coins[:_MAX_COINS]:
            if not isinstance(raw_coin, Mapping):
                continue
            symbol = _text(raw_coin.get("symbol"))[:32]
            market_type = _text(raw_coin.get("market_type"))[:32]
            if not symbol or not market_type:
                continue
            coin: dict[str, Any] = {
                "symbol": symbol,
                "market_type": market_type,
            }
            match = _text(raw_coin.get("match"))
            if match:
                coin["match"] = match[:64]
            coin_score = _number(raw_coin.get("score"))
            if coin_score is not None:
                coin["score"] = coin_score
            for key in ("signal", "grade"):
                value = _text(raw_coin.get(key))
                if value:
                    coin[key] = value[:32]
            normalized.append(coin)
        if normalized:
            result["coins"] = normalized
    strategy_match: dict[str, str] = {"id": strategy_id}
    name = _text(strategy.get("name"))
    if name:
        strategy_match["name"] = name[:128]
    source_type = _text(strategy.get("sourceType")).lower()
    if source_type:
        strategy_match["source_type"] = source_type[:32]
    engine_type = _text(params.get("engineType")).lower()
    if engine_type:
        strategy_match["engine_type"] = engine_type[:32]
    result["strategies"] = [strategy_match]
    return result


def _wire_strategy_id(value: object) -> str:
    if isinstance(value, bool) or value is None or not isinstance(value, str | int):
        return ""
    normalized = javascript_trim(str(value))
    if not normalized or "\x00" in normalized or len(normalized) > 128:
        return ""
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return normalized


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not isfinite(number) or number < 0 or number > 100:
        return None
    return value


def _text(value: object) -> str:
    text = javascript_trim(str(value or ""))
    if not text or "\x00" in text:
        return ""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return text


def _content_text(value: object) -> str:
    text = javascript_trim(str(value or ""))
    if not text:
        return ""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return text


__all__ = [
    "OPENNEWS_SOURCE_ID",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "OpenNewsStrategyHitPage",
    "parse_opennews_message",
    "parse_opennews_strategy_hits",
    "parse_opennews_strategy_list",
]
