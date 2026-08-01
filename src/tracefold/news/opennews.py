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
_SECRET_KEYS = frozenset({"authorization", "token", "access_token", "api_key"})
_TRANSPORT_KEYS = frozenset(
    {
        "connection_id",
        "fetch_id",
        "page",
        "received_at",
        "received_at_ms",
        "session_id",
    }
)


class OpenNewsExpectedError(RuntimeError):
    """An expected provider/auth/transport/payload outcome without secret text."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class OpenNewsEvent:
    provider_record_id: str
    source_item_key: str
    observation_kind: Literal["report", "translation", "provider_annotation"]
    raw: dict[str, Any]
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
    raw = _sanitize_mapping(params)
    provider_record_id = _text(params.get("id"))
    if not provider_record_id:
        return None
    if method == "news.ai_update":
        return OpenNewsEvent(
            provider_record_id=provider_record_id,
            source_item_key=f"dispatch:opennews:{provider_record_id}",
            observation_kind="provider_annotation",
            raw=raw,
            entry=None,
        )
    if _text(params.get("engineType")).lower() != "news":
        return None
    canonical_url = _article_url(_text(params.get("link")))
    source_item_key = f"url:{canonical_url}" if canonical_url else f"dispatch:opennews:{provider_record_id}"
    observation_kind: Literal["report", "translation"] = "translation" if _is_translation(params) else "report"
    entry = None
    if observation_kind == "report":
        entry = NewsFeedEntry(
            guid=source_item_key,
            link=canonical_url or None,
            title=_text(params.get("text")) or None,
            description="",
            published_at_ms=_timestamp_ms(params.get("ts")),
            reporting_origin=_reporting_origin(params, canonical_url=canonical_url),
            raw=raw,
        )
    return OpenNewsEvent(
        provider_record_id=provider_record_id,
        source_item_key=source_item_key,
        observation_kind=observation_kind,
        raw=raw,
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
    return any(key in params for key in ("translation", "translationOf", "translatedFrom", "translatedText"))


def _sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key.lower() in _SECRET_KEYS | _TRANSPORT_KEYS:
            continue
        if isinstance(raw_value, Mapping):
            result[key] = _sanitize_mapping(raw_value)
        elif isinstance(raw_value, list):
            result[key] = [_sanitize_mapping(item) if isinstance(item, Mapping) else item for item in raw_value]
        else:
            result[key] = raw_value
    return result


def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = [
    "OPENNEWS_REST_LIMIT",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "parse_opennews_message",
    "parse_opennews_rest_response",
]
