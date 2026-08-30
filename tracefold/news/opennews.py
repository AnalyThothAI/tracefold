from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Literal, Protocol, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .events.javascript_text import (
    collapse_javascript_whitespace,
    javascript_trim,
    utf16_length,
    utf16_slice,
    web_usv_string,
)
from .models import NewsFeedEntry

OPENNEWS_SOURCE_ID = "news-opennews"
OPENNEWS_HISTORY_PAGE_SIZE = 100

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
_STATUS_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status(?:es)?/(?P<status>\d{5,25})(?:[/?#]|$)",
    re.IGNORECASE,
)
# X Snowflake: the top 41 bits of a status id are milliseconds since 2010-11-04T01:42:54.657Z.
_X_SNOWFLAKE_EPOCH_MS = 1_288_834_974_657
_SNOWFLAKE_SHIFT = 22
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class OpenNewsExpectedError(RuntimeError):
    """An expected provider/auth/transport/payload outcome without secret text."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class OpenNewsHistoryPayloadReason(StrEnum):
    JSON_INVALID = "json_invalid"
    ROOT_NOT_OBJECT = "root_not_object"
    SUCCESS_INVALID = "success_invalid"
    DATA_NOT_LIST = "data_not_list"
    PAGE_INVALID = "page_invalid"
    TOTAL_INVALID = "total_invalid"
    LIMIT_INVALID = "limit_invalid"
    PAGE_MISMATCH = "page_mismatch"
    PAGE_OVERFLOW = "page_overflow"
    HIT_NOT_OBJECT = "hit_not_object"
    HIT_CONTRACT_INVALID = "hit_contract_invalid"


class OpenNewsHistoryError(RuntimeError):
    """A bounded, sanitized Strategy-history outcome."""

    def __init__(
        self,
        code: str,
        *,
        payload_reason: OpenNewsHistoryPayloadReason | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.payload_reason = payload_reason

    @classmethod
    def invalid_payload(cls, reason: OpenNewsHistoryPayloadReason) -> Self:
        return cls(f"opennews_history_payload_{reason.value}", payload_reason=reason)


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
    # The artifact the frame is *about*, not the frame: two provider records carrying the same tweet share it.
    source_artifact_id: str = ""
    # When that artifact was published by its own platform, which is not `entry.published_at_ms` (the provider's
    # push time). Empty unless the artifact id carries the timestamp itself.
    source_published_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class OpenNewsStrategyHitPage:
    events: tuple[OpenNewsEvent, ...]
    page: int
    total: int
    has_more: bool


def parse_opennews_strategy_list(payload: object) -> tuple[dict[str, Any], ...]:
    """Strict about the envelope, tolerant about a row.

    A malformed response is a provider failure and raises. A single odd row is not: this list is now what
    recovery enumerates and what the status surface counts, and discarding all of it because one entry has a
    null `enabled` would blank the fact and, before the caller learns better, cost an outage window. Skip the
    row, keep the rest — which is what the Receiver's own inline parse did before this became shared.
    """

    data = _history_data(payload)
    strategies: list[dict[str, Any]] = []
    for value in data:
        if not isinstance(value, Mapping):
            continue
        strategy_id = normalize_opennews_wire_id(value.get("id"))
        enabled = value.get("enabled")
        if not strategy_id or not isinstance(enabled, bool):
            continue
        strategies.append(
            {
                "id": strategy_id,
                "name": _text(value.get("name"))[:128],
                "enabled": enabled,
            }
        )
    return tuple(sorted(strategies, key=lambda row: row["id"]))


def enabled_strategy_ids(payload: object) -> tuple[str, ...]:
    """The account's enabled Strategy IDs.

    This is the only list that decides anything: the socket pushes what the account has enabled, and Tracefold
    sends no subscription frame. Recovery reads it because the provider's hits endpoint is per-strategy.
    """

    return tuple(row["id"] for row in parse_opennews_strategy_list(payload) if row["enabled"])


def parse_opennews_strategy_hits(
    payload: object,
    *,
    expected_page: int | None = None,
) -> OpenNewsStrategyHitPage:
    if not isinstance(payload, Mapping):
        raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.ROOT_NOT_OBJECT)
    data = _history_data(payload)
    page = _history_nonnegative_int(
        payload.get("page"),
        minimum=1,
        reason=OpenNewsHistoryPayloadReason.PAGE_INVALID,
    )
    if expected_page is not None and page != expected_page:
        raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.PAGE_MISMATCH)
    if "total" not in payload and page == 1 and not data:
        total = 0
    else:
        total = _history_nonnegative_int(
            payload.get("total"),
            minimum=0,
            reason=OpenNewsHistoryPayloadReason.TOTAL_INVALID,
        )
    limit = _history_nonnegative_int(
        payload.get("limit"),
        minimum=1,
        reason=OpenNewsHistoryPayloadReason.LIMIT_INVALID,
    )
    offset = (page - 1) * limit
    remaining = max(0, total - offset)
    if limit > OPENNEWS_HISTORY_PAGE_SIZE or (page > 1 and offset >= total) or len(data) != min(limit, remaining):
        raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.PAGE_OVERFLOW)
    events: list[OpenNewsEvent] = []
    provider_record_ids: set[str] = set()
    for value in data:
        if not isinstance(value, Mapping):
            raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.HIT_NOT_OBJECT)
        event = parse_opennews_message({"method": "strategy.triggered", "params": value})
        if event is None or event.entry.published_at_ms is None:
            raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.HIT_CONTRACT_INVALID)
        if event.provider_record_id in provider_record_ids:
            continue
        provider_record_ids.add(event.provider_record_id)
        events.append(event)
    return OpenNewsStrategyHitPage(
        events=tuple(events),
        page=page,
        total=total,
        has_more=(page * limit) < total,
    )


def _history_data(payload: object) -> list[Any]:
    if not isinstance(payload, Mapping):
        raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.ROOT_NOT_OBJECT)
    if payload.get("success") is not True:
        raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.SUCCESS_INVALID)
    data = payload.get("data")
    if not isinstance(data, list):
        raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.DATA_NOT_LIST)
    if len(data) > OPENNEWS_HISTORY_PAGE_SIZE:
        raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.PAGE_OVERFLOW)
    return data


def _history_nonnegative_int(
    value: object,
    *,
    minimum: int,
    reason: OpenNewsHistoryPayloadReason,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OpenNewsHistoryError.invalid_payload(reason)
    return int(value)


def parse_opennews_message(message: object) -> OpenNewsEvent | None:
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
    strategy_id = normalize_opennews_wire_id(strategy.get("id"))
    if not strategy_id:
        return None
    provider_record_id = normalize_opennews_wire_id(params.get("id"))
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
    artifact_id, artifact_published_at_ms = source_artifact_identity(canonical_url)
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
        source_artifact_id=artifact_id,
        source_published_at_ms=artifact_published_at_ms,
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


def source_artifact_identity(canonical_url: str) -> tuple[str, int | None]:
    """`(artifact_id, published_at_ms)` for the artifact a frame points at, or `("", None)`.

    The provider re-emits the same tweet under new record ids and under inconsistent URL spellings —
    ``twitter.com`` vs ``x.com``, ``coindesk`` vs ``CoinDesk``; ``_article_url`` lowercases the host but not the
    path. 17 of 29 repeat ingests in a 30-day window differed only in that spelling, so the URL string is not an
    identity. The status id is: it is the platform's own primary key.

    An X status id is a Snowflake, so the artifact's real publication time falls out of the same parse. Measured
    over 3174 frames in 30 days the distribution is bimodal — 2491 within 10 s of the provider's push and 7
    beyond 16 h, nothing between — and never negative.
    """

    match = _STATUS_URL_RE.match(javascript_trim(canonical_url))
    if match is None:
        return "", None
    status_id = int(match.group("status"))
    published_at_ms = (status_id >> _SNOWFLAKE_SHIFT) + _X_SNOWFLAKE_EPOCH_MS
    return f"x:{status_id}", published_at_ms


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


def normalize_opennews_wire_id(value: object) -> str:
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
    "OPENNEWS_HISTORY_PAGE_SIZE",
    "OPENNEWS_SOURCE_ID",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsHistoryPayloadReason",
    "OpenNewsStrategyHistory",
    "OpenNewsStrategyHitPage",
    "enabled_strategy_ids",
    "normalize_opennews_wire_id",
    "parse_opennews_message",
    "parse_opennews_strategy_hits",
    "parse_opennews_strategy_list",
]
