"""The market read routes: two narrow GETs over Items and their typed facts.

Neither one asks a question of the editorial pipeline, of Trading, or of a model. `/api/news/market`
is answerable whenever PostgreSQL is, which is the whole point: a reader must be able to see what the
provider reported even when the model is unconfigured, the sender is down and Trading is faulted.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.news import MARKET_KINDS, MARKET_PAGE_MAX, MARKET_WINDOW_DEFAULT_MS, MARKET_WINDOW_MAX_MS

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged, _json
from ..schemas import common as api_schemas
from ..schemas import market as market_schemas

router = APIRouter()
_MarketEnvelope = api_schemas.ApiEnvelope[market_schemas.NewsMarketData]
_MarketItemEnvelope = api_schemas.ApiEnvelope[market_schemas.NewsMarketItemData]

# `news_items.item_id` is `sha256(source_id, provider record id)`. Bounding the path segment here keeps
# a 2 KB string from reaching an indexed lookup.
_ITEM_ID: Final = re.compile(r"^[0-9a-f]{64}$")
# The list orders on (received_at_ms, item_id) of a group's newest observation, so that pair is the
# cursor. Opaque on the wire for the same reason the feed's is: it is a position, not a filter.
_CURSOR_MAX_RECEIVED_AT_MS: Final = 1 << 62
# Every millisecond value below is bound into a PostgreSQL `bigint`. A caller-supplied integer past
# that range is a malformed request, and saying so is the difference between a named 400 and a 500
# from the driver -- the same reason the cursor's own decode failure is a named error.
_MAX_MS: Final = 2**63 - 1


@router.get("/news/market", response_model=_MarketEnvelope)
def get_news_market(
    request: Request,
    kind: Annotated[str, Query(max_length=64)] = "",
    from_ms: Annotated[int, Query(ge=0)] = 0,
    to_ms: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MARKET_PAGE_MAX)] = 50,
    cursor: Annotated[str, Query(max_length=200)] = "",
) -> Response:
    """Market observations in one window, consecutive observations of a group collapsed.

    The window is absolute rather than a "last N hours" offset, because a reader reviewing what
    arrived on Tuesday is asking a question a rolling offset cannot express. It defaults to the last
    72 h, spans at most 168 h in one request, and may sit anywhere inside the retention.

    `scan_truncated` is the honest name for the one thing a bounded page can still get wrong: a
    single run longer than one page's scan is split, so its `observation_count` is a floor. The
    per-kind `sources` block is not bounded that way and its counts are exact.

    "Not pushed" is not a filter and never becomes one. Whether a card was sent is reported per group
    and is not a precondition for reading the observation.
    """

    _validate_query_params(request, supported={"kind", "from_ms", "to_ms", "limit", "cursor", "token"})
    kinds = _parse_kinds(kind)
    if from_ms > _MAX_MS or to_ms > _MAX_MS:
        raise ApiBadRequest("news_market_window_invalid", field="to_ms" if to_ms > _MAX_MS else "from_ms")
    window_to = int(to_ms) if to_ms else int(time.time() * 1000)
    window_from = int(from_ms) if from_ms else window_to - MARKET_WINDOW_DEFAULT_MS
    if window_from >= window_to:
        raise ApiBadRequest("news_market_window_invalid", field="from_ms")
    if window_to - window_from > MARKET_WINDOW_MAX_MS:
        raise ApiBadRequest("news_market_window_too_wide", field="to_ms")
    cursor_received_at_ms, cursor_item_id = _decode_cursor(cursor)
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        groups, scan_truncated = repos.news.market_groups(
            kinds=kinds,
            from_ms=window_from,
            to_ms=window_to,
            cursor_received_at_ms=cursor_received_at_ms,
            cursor_item_id=cursor_item_id,
            limit=int(limit) + 1,
        )
        sources = repos.news.market_sources(from_ms=window_from, to_ms=window_to)
    page = groups[: int(limit)]
    next_cursor = None
    if len(groups) > int(limit):
        # The next page starts below the *oldest* member of the last run returned. Anchoring on the
        # newest member would re-scan the rest of that run and emit the same group twice.
        last = page[-1]
        next_cursor = _encode_cursor(int(last["oldest_received_at_ms"]), str(last["oldest_item_id"]))
    return _etagged(
        {
            "groups": page,
            "next_cursor": next_cursor,
            "sources": sources,
            "filters": {
                "kind": ",".join(kinds) if kinds else None,
                "from_ms": window_from,
                "to_ms": window_to,
                "limit": int(limit),
            },
            "scan_truncated": scan_truncated,
        },
        request,
        envelope=_MarketEnvelope,
    )


@router.get(
    "/news/market/{item_id}",
    response_model=_MarketItemEnvelope,
    responses={404: {"model": _MarketItemEnvelope, "description": "No market Item with this identity is retained."}},
)
def get_news_market_item(request: Request, item_id: str) -> Response:
    """One observation in full, with its group's retained timeline.

    Read by Item identity, so it is not bound by the list's window: a link into a group that last
    reported nine days ago still opens.
    """

    _validate_query_params(request, supported={"token"})
    normalized = str(item_id or "").strip().lower()
    if not _ITEM_ID.fullmatch(normalized):
        raise ApiBadRequest("news_market_item_invalid", field="item_id")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        detail = repos.news.market_item(item_id=normalized)
        timeline = [] if detail is None else repos.news.market_group_timeline(group_key=str(detail["group_key"]))
    if detail is None:
        return _json({"ok": False, "error": "news_market_item_not_found"}, status_code=404)
    observation = {key: value for key, value in detail.items() if key in _OBSERVATION_FIELDS}
    return _etagged(
        {
            "observation": observation,
            "provider_params": detail["provider_params"],
            "description": detail["description"],
            "raw_first_line": detail["raw_first_line"],
            "notification_status": detail["notification_status"],
            "notification_reason": detail["notification_reason"],
            "notification_delivery": _delivery(detail["notification_delivery"]),
            "notification_covered_item_ids": detail["notification_covered_item_ids"],
            "timeline": timeline,
        },
        request,
        envelope=_MarketItemEnvelope,
    )


_OBSERVATION_FIELDS: Final = frozenset(market_schemas.NewsMarketObservationData.model_fields)
_DELIVERY_FIELDS: Final = frozenset(market_schemas.NewsMarketDeliveryData.model_fields) - {"receipt_provider"}


def _delivery(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Publish the card and its attempts; publish which provider answered, never the receipt.

    A receipt carries channel identifiers -- a chat id hash, a message id -- and the console's
    question is whether a reader was told, not how to address the message.
    """

    if row is None:
        return None
    published = {key: value for key, value in row.items() if key in _DELIVERY_FIELDS}
    receipt = row.get("receipt")
    published["card"] = dict(row.get("card") or {})
    published["receipt_provider"] = (
        str(receipt.get("provider")) if isinstance(receipt, dict) and receipt.get("provider") else None
    )
    return published


def _parse_kinds(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    kinds = tuple(dict.fromkeys(part for part in str(value).split(",") if part))
    if any(part not in MARKET_KINDS for part in kinds):
        raise ApiBadRequest("news_market_kind_invalid", field="kind")
    return kinds


def _encode_cursor(received_at_ms: int, item_id: str) -> str:
    return base64.urlsafe_b64encode(f"{received_at_ms}|{item_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    """The first page starts above every row, so an absent cursor is the open upper bound."""

    if not cursor:
        return _CURSOR_MAX_RECEIVED_AT_MS, ""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        received_at_ms, _, item_id = base64.urlsafe_b64decode(padded.encode()).decode().partition("|")
        position = int(received_at_ms)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ApiBadRequest("news_market_cursor_invalid", field="cursor") from exc
    if not 0 <= position <= _MAX_MS:
        raise ApiBadRequest("news_market_cursor_invalid", field="cursor")
    return position, item_id


__all__ = ["router"]
