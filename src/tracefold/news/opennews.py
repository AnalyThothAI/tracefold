from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import NewsFeedEntry

OPENNEWS_REST_LIMIT = 100

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


class OpenNewsExpectedError(RuntimeError):
    """An expected provider/auth/transport/payload outcome without secret text."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class OpenNewsEvent:
    provider_record_id: str
    observation_kind: Literal["report", "translation", "provider_annotation"]
    provider_metadata: dict[str, Any]
    entry: NewsFeedEntry | None


def parse_opennews_rest_response(payload: object) -> tuple[OpenNewsEvent, ...]:
    if not isinstance(payload, Mapping):
        raise OpenNewsExpectedError("opennews_rest_payload_invalid")
    value: object = payload.get("data", payload)
    if isinstance(value, Mapping):
        value = value.get("items", value.get("list", value.get("data", [])))
    if not isinstance(value, list):
        raise OpenNewsExpectedError("opennews_rest_payload_invalid")
    events: list[OpenNewsEvent] = []
    for row in value[:OPENNEWS_REST_LIMIT]:
        if not isinstance(row, Mapping):
            continue
        parsed = parse_opennews_message({"method": "news.update", "params": row})
        if parsed is not None:
            events.append(parsed)
    return tuple(events)


def parse_opennews_message(message: object) -> OpenNewsEvent | None:
    if message == "ping" or not isinstance(message, Mapping):
        return None
    method = _text(message.get("method"))
    if method == "strategy.triggered" or method not in {"news.update", "news.ai_update"}:
        return None
    params = message.get("params")
    if not isinstance(params, Mapping):
        return None
    provider_record_id = _text(params.get("id"))
    if not provider_record_id:
        return None
    if method == "news.ai_update":
        return OpenNewsEvent(
            provider_record_id=provider_record_id,
            observation_kind="provider_annotation",
            provider_metadata=_provider_metadata(params),
            entry=None,
        )
    if _text(params.get("engineType")).lower() != "news":
        return None
    canonical_url = _article_url(_text(params.get("link")))
    observation_kind: Literal["report", "translation"] = "translation" if _is_translation(params) else "report"
    entry = None
    if observation_kind == "report":
        entry = NewsFeedEntry(
            guid=provider_record_id,
            link=canonical_url or None,
            title=_text(params.get("text")) or None,
            description=_text(params.get("description")),
            published_at_ms=_timestamp_ms(params.get("ts")),
            reporting_origin=_reporting_origin(params, canonical_url=canonical_url),
            raw={},
        )
    return OpenNewsEvent(
        provider_record_id=provider_record_id,
        observation_kind=observation_kind,
        provider_metadata=_provider_metadata(params),
        entry=entry,
    )


def _timestamp_ms(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
        return int(number * 1_000) if abs(number) < 100_000_000_000 else int(number)
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp() * 1_000)
    return int(number * 1_000) if abs(number) < 100_000_000_000 else int(number)


def _article_url(value: str) -> str:
    parsed = urlsplit(value.strip())
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
    explicit = _text(params.get("newsType")).lower()
    if explicit:
        return explicit
    if canonical_url:
        return str(urlsplit(canonical_url).hostname or "").lower() or "opennews"
    return "opennews"


def _is_translation(params: Mapping[str, Any]) -> bool:
    return _text(params.get("newsType")).casefold() == "translation" or any(
        key in params for key in ("translation", "translationOf", "translatedFrom", "translatedText")
    )


def _provider_metadata(params: Mapping[str, Any]) -> dict[str, Any]:
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
        value = _text(ai.get(key))
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
    return result


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = [
    "OPENNEWS_REST_LIMIT",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "parse_opennews_message",
    "parse_opennews_rest_response",
]
