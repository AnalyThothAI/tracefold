"""Read-only token episodes. Auxiliary roster failures do not obscure events."""

from __future__ import annotations

import time
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..read_cursor import decode_read_cursor, encode_read_cursor
from ..responses import _etagged, _json
from ..schemas import common as api_schemas
from ..schemas import wallets as wallet_schemas

router = APIRouter()
_WalletsEnvelope = api_schemas.ApiEnvelope[wallet_schemas.NewsWalletsData]
_EventsEnvelope = api_schemas.ApiEnvelope[wallet_schemas.NewsWalletEventsData]
_DetailEnvelope = api_schemas.ApiEnvelope[wallet_schemas.NewsWalletEventDetailData]
WALLET_HISTORY_RANGES: Final = {"24h": 86_400_000, "72h": 259_200_000, "7d": 604_800_000}
WALLET_EVENTS_PAGE_MAX: Final = 200


@router.get("/news/wallets", response_model=_WalletsEnvelope)
def get_news_wallets(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        members = repos.news.chain_tape_roster_rows()
        tape = repos.news.chain_tape_state()
    return _etagged({"roster": _roster(members), "tape": tape}, request, envelope=_WalletsEnvelope)


@router.get("/news/wallets/events", response_model=_EventsEnvelope)
def get_news_wallet_events(
    request: Request,
    history_range: Literal["24h", "72h", "7d"] = "24h",
    limit: Annotated[int, Query(ge=1, le=WALLET_EVENTS_PAGE_MAX)] = 50,
    cursor: Annotated[str, Query(max_length=512)] = "",
    to_ms: Annotated[int, Query(ge=0, lt=2**63)] = 0,
) -> Response:
    _validate_query_params(request, supported={"token", "history_range", "limit", "cursor", "to_ms"})
    runtime = _authenticated_runtime(request)
    scope = ["wallet_events", history_range]
    position = decode_read_cursor(cursor, scope, error="news_wallet_events_cursor_invalid")
    until = position[0] if position else to_ms or int(time.time() * 1000)
    if position and to_ms and to_ms != until:
        raise ApiBadRequest("news_wallet_events_cursor_invalid", field="cursor")
    since = max(0, until - WALLET_HISTORY_RANGES[history_range])
    with runtime.repositories() as repos, repos.news.wallet_read_snapshot():
        events = repos.news.wallet_events(
            from_ms=since,
            to_ms=until,
            before_at_ms=position[2] if position else None,
            before_id=position[3] if position else None,
            limit=limit + 1,
        )
        totals = repos.news.wallet_event_totals(from_ms=since, to_ms=until)
    next_cursor = None
    if len(events) > limit:
        last = events[limit - 1]
        next_cursor = encode_read_cursor(
            scope, to_ms=until, value=0, at_ms=last["event_at_ms"], identity=last["item_id"]
        )
    return _etagged(
        {
            "events": [_event(row) for row in events[:limit]],
            "totals": totals,
            "next_cursor": next_cursor,
            "history_range": history_range,
            "history_from_ms": since,
            "history_to_ms": until,
            "limit": limit,
        },
        request,
        envelope=_EventsEnvelope,
    )


@router.get("/news/wallets/events/{episode_id}", response_model=_DetailEnvelope)
def get_news_wallet_event(
    request: Request,
    episode_id: str,
    fills_cursor: Annotated[str, Query(max_length=512)] = "",
    limit: Annotated[int, Query(ge=1, le=WALLET_EVENTS_PAGE_MAX)] = 100,
) -> Response:
    _validate_query_params(request, supported={"token", "fills_cursor", "limit"})
    runtime = _authenticated_runtime(request)
    scope = ["wallet_event_fills", episode_id]
    position = decode_read_cursor(fills_cursor, scope, error="news_wallet_fills_cursor_invalid")
    with runtime.repositories() as repos, repos.news.wallet_read_snapshot():
        event = repos.news.wallet_event(episode_id)
        if event is None:
            return _json({"ok": False, "error": "news_wallet_event_not_found"}, status_code=404)
        until = position[0] if position else event["latest_snapshot"]["cutoff_at_ms"]
        fills = repos.news.wallet_event_fills(
            chain_id=event["chain_id"],
            token=event["token"],
            from_ms=event["initial_snapshot"]["slow"]["from_ms"],
            to_ms=until,
            cutoff_block=event["latest_snapshot"]["cutoff_block"],
            cutoff_log=event["latest_snapshot"]["cutoff_log"],
            before_block=position[1] if position else None,
            before_log=position[2] if position else None,
            limit=limit + 1,
        )
        outcomes = repos.news.wallet_outcomes(episode_id)
    next_cursor = None
    if len(fills) > limit:
        last = fills[limit - 1]
        next_cursor = encode_read_cursor(
            scope, to_ms=until, value=last["block_number"], at_ms=last["log_index"], identity=last["tx_hash"]
        )
    return _etagged(
        {
            "event": _event(event),
            "fills": fills[:limit],
            "next_fills_cursor": next_cursor,
            "outcomes": outcomes,
        },
        request,
        envelope=_DetailEnvelope,
    )


def _event(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "chain_id",
        "token",
        "token_symbol",
        "trigger_tx_hash",
        "received_at_ms",
        "detected_at_ms",
        "last_effective_buy_at_ms",
        "ended_at_ms",
        "initial_snapshot",
        "latest_snapshot",
        "change_reason",
        "updated_at_ms",
        "intent_at_ms",
        "first_attempt_at_ms",
        "settled_at_ms",
        "reference_at_ms",
        "reference_source",
    )
    return {
        **{field: row[field] for field in fields},
        "episode_id": row["item_id"],
        "triggered_at_ms": row["event_at_ms"],
        "notification_state": row["notification_state"]
        or ("pending" if row["notification_eligible"] and not row["notification_error"] else "not_alerted"),
        "notification_reason": row["notification_error"] or row["notification_reason"],
        "attempts": row["attempts"] or 0,
        "reference_price": None if row["reference_price"] is None else str(row["reference_price"]),
    }


def _roster(members: list[dict[str, Any]]) -> dict[str, Any]:
    if not members:
        return {"roster_version": 0, "taken_at_ms": None, "provider": None, "members": []}
    return {
        "roster_version": int(members[0]["roster_version"]),
        "taken_at_ms": int(members[0]["taken_at_ms"]),
        "provider": str(members[0]["provider"]),
        "members": [
            {key: value for key, value in member.items() if key not in {"roster_version", "taken_at_ms"}}
            for member in members
        ],
    }
